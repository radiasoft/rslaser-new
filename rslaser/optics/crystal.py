# -*- coding: utf-8 -*-
"""Definition of a crystal
Copyright (c) 2021 RadiaSoft LLC. All rights reserved
"""
import numpy as np
import array
import math
import copy
from pykern.pkcollections import PKDict
import srwlib
import scipy.constants as const
from scipy.interpolate import RectBivariateSpline
from scipy.interpolate import splrep
from scipy.interpolate import splev
from scipy.optimize import curve_fit
from scipy.special import gamma
from rsmath import lct as rslct
from rslaser.utils.validator import ValidatorBase
from rslaser.utils import srwl_uti_data as srwutil
from rslaser.optics.element import ElementException, Element
from fenics import *
from mshr import *

_N_SLICE_DEFAULT = 50
_N0_DEFAULT = 1.75
_N2_DEFAULT = 0.001
_CRYSTAL_DEFAULTS = PKDict(
    n0=[_N0_DEFAULT for _ in range(_N_SLICE_DEFAULT)],
    n2=[_N2_DEFAULT for _ in range(_N_SLICE_DEFAULT)],
    length=0.2,
    l_scale=1,
    nslice=_N_SLICE_DEFAULT,
    slice_index=0,
    # A = 9.99988571e-01,
    # B = 1.99999238e-01,
    # C = -1.14285279e-04,
    # D = 9.99988571e-01,
    A=0.99765495,
    B=1.41975385,
    C=-0.0023775,
    D=0.99896716,
    radial_n2_factor=1.3,
    population_inversion=PKDict(
        n_cells=64,
        mesh_extent=0.005,  # [m], crystal radius
        crystal_alpha=120.0,  # [1/m], 1.2 1/cm
        pump_waist=0.00164,  # [m]
        pump_wavelength=532.0e-9,  # [m]
        pump_energy=0.0211,  # [J], pump laser energy onto the crystal
        pump_type="dual",
        pump_gaussian_order=2.0,
        pump_offset_x=0.0,
        pump_offset_y=0.0,
        pump_rep_rate=1.0e3,  # Hz
    ),
)


class Crystal(Element):
    """
    Args:
        params (PKDict) with fields:
            n0 (float): array of on axis index of refractions in crystal slices
            n2 (float): array of quadratic variations of index of refractions, with n(r) = n0 - 1/2 n2 r^2  [m^-2]
            note: n0, n2 should be an array of length nslice; if nslice = 1, they should be single values
            length (float): total length of crystal [m]
            nslice (int): number of crystal slices
            l_scale: length scale factor for LCT propagation
    """

    _DEFAULTS = _CRYSTAL_DEFAULTS
    _INPUT_ERROR = ElementException

    def __init__(self, params=None):
        params = self._get_params(params)
        self._validate_params(params)
        self.params = params

        # Check if n2<0, throw an exception if true
        if (np.array(params.n2) < 0.0).any():
            raise self._INPUT_ERROR(f"You've specified negative value(s) for n2")

        self.length = params.length
        self.nslice = params.nslice
        self.l_scale = params.l_scale
        self.slice = []
        for j in range(self.nslice):
            p = params.copy()
            p.update(
                PKDict(
                    n0=params.n0[j],
                    n2=params.n2[j],
                    length=params.length / params.nslice,
                    slice_index=j,
                )
            )
            self.slice.append(CrystalSlice(params=p))

    def _get_params(self, params):
        def _update_n0_and_n2(params_final, params, field):
            if len(params_final[field]) != params_final.nslice:
                if not params.get(field):
                    # if no n0/n2 specified then we use default nlice times in array
                    params_final[field] = [
                        PKDict(
                            n0=_N0_DEFAULT,
                            n2=_N2_DEFAULT,
                        )[field]
                        for _ in range(params_final.nslice)
                    ]
                    return
                raise self._INPUT_ERROR(
                    f"you've specified an {field} unequal length to nslice"
                )

        o = params.copy() if type(params) == PKDict else PKDict()
        p = super()._get_params(params)
        if not o.get("nslice") and not o.get("n0") and not o.get("n2"):
            # user specified nothing, use defaults provided by _get_params
            return p
        if o.get("nslice"):
            # user specifed nslice, but not necissarily n0/n2
            _update_n0_and_n2(p, o, "n0")
            _update_n0_and_n2(p, o, "n2")
            return p
        if o.get("n0") or o.get("n2"):
            if len(p.n0) < p.nslice or len(p.n2) < p.nslice:
                p.nslice = min(len(p.n0), len(p.n2))
        return p

    def propagate(self, laser_pulse, prop_type, calc_gain=False, radial_n2=False):
        assert (laser_pulse.pulse_direction == 0.0) or (
            laser_pulse.pulse_direction == 180.0
        ), "ERROR -- Propagation not implemented for the pulse direction {}".format(
            laser_pulse.pulse_direction
        )

        if laser_pulse.pulse_direction == 0.0:
            slice_array = self.slice
        elif laser_pulse.pulse_direction == 180.0:
            slice_array = self.slice[::-1]

        for s in slice_array:

            if radial_n2:

                assert prop_type == "n0n2_srw", "ERROR -- Only implemented for n0n2_srw"
                laser_pulse_copies = PKDict(
                    n2_max=copy.deepcopy(laser_pulse),
                    n2_0=copy.deepcopy(laser_pulse),
                )

                temp_crystal_slice = copy.deepcopy(s)
                temp_crystal_slice.n2 = 0.0

                laser_pulse_copies.n2_max = s.propagate(
                    laser_pulse_copies.n2_max, prop_type, calc_gain
                )
                laser_pulse_copies.n2_0 = temp_crystal_slice.propagate(
                    laser_pulse_copies.n2_0, prop_type, calc_gain
                )

                laser_pulse = laser_pulse.combine_n2_variation(
                    laser_pulse_copies,
                    s.radial_n2_factor,
                    s.population_inversion.pump_waist,
                    s.population_inversion.pump_offset_x,
                    s.population_inversion.pump_offset_y,
                    s.n2,
                )
            else:
                laser_pulse = s.propagate(laser_pulse, prop_type, calc_gain)

            laser_pulse.resize_laser_mesh()
            laser_pulse.flatten_phase_edges()
        return laser_pulse

    def calc_n0n2_fenics(self, set_n=False, initial_temp=0.0, mesh_density=80):
        # initial_temp [degC],
        # mesh_density [int]: value ≥ 120 will produce more accurate results; slower, but closer to numerical conversion

        n_radpts = 201  # no. of radial points at which to extract data
        n_longpts = 201  # no. of longitudinal points at which to extract data
        num_long_slices = 180  # no. of longitudinal slices

        # values need to be in [cm]
        crystal_diameter = self.params.population_inversion.mesh_extent * 2.0 * 1.0e2
        crystal_length = self.length * 1.0e2
        pump_waist = self.params.population_inversion.pump_waist * 1.0e2
        # value needs to be in [1/cm]
        absorption_coefficient = self.params.population_inversion.crystal_alpha / 1.0e2
        # value needs to be in [J]
        pump_energy = self.params.population_inversion.pump_energy
        # value needs to be in [W]
        pump_power = pump_energy * self.params.population_inversion.pump_rep_rate

        mesh_tol = 2.0e-2  # mesh tolerance
        mesh = _calculate_mesh(crystal_length, crystal_diameter, mesh_density)
        xvals = mesh.coordinates()[:, 0]
        yvals = mesh.coordinates()[:, 1]
        zvals = mesh.coordinates()[:, 2]
        xmin, xmax = xvals.min(), xvals.max()
        ymin, ymax = yvals.min(), yvals.max()
        zmin, zmax = zvals.min(), zvals.max()
        xv = np.linspace(xmin * (1.0 - mesh_tol), xmax * (1.0 - mesh_tol), n_radpts)
        yv = np.linspace(ymin * (1.0 - mesh_tol), ymax * (1.0 - mesh_tol), n_radpts)
        zv = np.linspace(zmin * (1.0 - mesh_tol), zmax * (1.0 - mesh_tol), n_longpts)
        radial_pts = np.asarray([(x_, 0, 0) for x_ in xv])
        laser_range_min = (
            np.abs(radial_pts[:, 0] - (-0.5 * pump_waist))
        ).argmin()  # min index value of center data range  # JVT +/- 0.5*w_p
        laser_range_max = (
            np.abs(radial_pts[:, 0] - (0.5 * pump_waist))
        ).argmin()  # max index value of center data range  # JVT +/- 0.5*w_p

        if self.params.population_inversion.pump_rep_rate == 1.0e3:
            heat_load = _define_heat_load_expression(
                pump_waist, absorption_coefficient, crystal_length, pump_power
            )
            long_temp_profiles = _call_fenics(
                mesh, heat_load, crystal_diameter, initial_temp, zv, radial_pts
            )
        elif self.params.population_inversion.pump_rep_rate == 1.0:
            long_temp_profiles = _calc_temperature_change(
                pump_waist,
                absorption_coefficient,
                crystal_length,
                crystal_diameter,
                pump_energy,
                xv,
                zv,
            )
        else:
            print(
                "No method implemented for a rep rate of {}.".format(
                    self.params.population_inversion.pump_rep_rate
                )
            )

        integrated_temps = _calc_T(
            long_temp_profiles, crystal_length, num_long_slices, zv, radial_pts
        )

        # Calculate index of refraction for each slice, from T(r)
        n0_full_array, n2_full_array = _calc_n_from_T(
            num_long_slices,
            radial_pts,
            integrated_temps,
            laser_range_min,
            laser_range_max,
        )

        # fix negative n2 vals and ****divide through by 2 based on Gaussian duct definition n(r) = n0 - 1/2*n2*r^2**** - see rp-photonics.com
        n2_full_array = np.multiply(n2_full_array, -2.0)

        # Calculate the ABCD matrix for the total crystal (usable with abcd_lct if no gain)
        full_crystal_abcd_mat = _calc_full_abcd_mat(
            crystal_length, n0_full_array, n2_full_array
        )

        z_full_array = np.linspace(0.0, self.length, len(n0_full_array))
        n0_fit = splrep(z_full_array, n0_full_array)
        n2_fit = splrep(z_full_array, n2_full_array * 1.0e4)

        z_crystal_slice = (self.length / self.nslice) * (np.arange(self.nslice) + 0.5)
        n0_slice_array = splev(z_crystal_slice, n0_fit)
        n2_slice_array = splev(z_crystal_slice, n2_fit)

        if self.params.population_inversion.pump_type == "right":
            n0_output = n0_slice_array[::-1]
            n2_output = n2_slice_array[::-1]
        elif self.params.population_inversion.pump_type == "left":
            n0_output = n0_slice_array
            n2_output = n2_slice_array
        elif self.params.population_inversion.pump_type == "dual":
            n0_output = (n0_slice_array + n0_slice_array[::-1]) / 2.0
            n2_output = n2_slice_array + n2_slice_array[::-1]

        if set_n:
            for s in self.slice:
                s.n0 = n0_output[s.slice_index]
                s.n2 = n2_output[s.slice_index]

        return n0_output, n2_output, full_crystal_abcd_mat


class CrystalSlice(Element):
    """
    This class represents a slice of a crystal in a laser cavity.

    Args:
        params (PKDict) with fields:
            length
            n0 (float): on-axis index of refraction
            n2 (float): transverse variation of index of refraction [1/m^2]
            n(r) = n0 - 0.5 n2 r^2
            l_scale: length scale factor for LCT propagation

    To be added: alpha0, alpha2 laser gain parameters

    Note: Initially, these parameters are fixed. Later we will update
    these parameters as the laser passes through.
    """

    _DEFAULTS = _CRYSTAL_DEFAULTS
    _INPUT_ERROR = ElementException

    def __init__(self, params=None):
        params = self._get_params(params)
        self._validate_params(params)
        self.length = params.length
        self.slice_index = params.slice_index
        self.n0 = params.n0
        self.n2 = params.n2
        self.l_scale = params.l_scale
        # self.pop_inv = params._pop_inv
        self.A = params.A
        self.B = params.B
        self.C = params.C
        self.D = params.D
        self.radial_n2_factor = params.radial_n2_factor

        # Wavelength-dependent cross-section (P. F. Moulton, 1986)
        wavelength = np.array(
            [600, 625, 650, 700, 750, 800, 850, 900, 950, 1000, 1025, 1050]
        ) * (1.0e-9)
        cross_section = np.array(
            [
                0.0,
                0.02,
                0.075,
                0.437,
                0.845,
                0.99,
                0.815,
                0.6,
                0.415,
                0.276,
                0.255,
                0.247,
            ]
        ) * (4.8e-23)
        self.cross_section_fn = splrep(wavelength, cross_section)

        #  Assuming wfr0 exsts, created e.g. via
        #  wfr0=createGsnSrcSRW(sigrW,propLen,pulseE,poltype,photon_e_ev,sampFact,mx,my)
        # n_x = wfr0.mesh.nx  #  nr of grid points in x
        # n_y = wfr0.mesh.ny  #  nr of grid points in y
        # sig_cr_sec = np.ones((n_x, n_y), dtype=np.float32)

        # 2d mesh of excited state density (sigma)
        self._initialize_excited_states_mesh(params.population_inversion, params.nslice)

    def _left_pump(self, nslice, xv, yv):

        # z = distance from left of crystal to center of current slice (assumes all crystal slices have same length)
        z = self.length * (self.slice_index + 0.5)

        slice_front = z - (self.length / 2.0)
        slice_end = z + (self.length / 2.0)

        # calculate correction factor for representing a gaussian pulse with a series of flat-top slices
        correction_factor = (
            (
                np.exp(-self.population_inversion.crystal_alpha * slice_front)
                - np.exp(-self.population_inversion.crystal_alpha * slice_end)
            )
            / self.population_inversion.crystal_alpha
        ) / (np.exp(-self.population_inversion.crystal_alpha * z) * self.length)

        # integrate super-gaussian
        g_order = self.population_inversion.pump_gaussian_order
        integral_factor = (2 ** ((g_order - 2.0) / g_order) * gamma(2 / g_order)) / (
            g_order
            * (1 / (self.population_inversion.pump_waist**g_order)) ** (2.0 / g_order)
        )

        pump_wavelength = 532.0  # [nm]
        seed_wavelength = 800.0  # [nm]
        fraction_to_heating = (seed_wavelength - pump_wavelength) / seed_wavelength

        # Create a default mesh of [num_excited_states/m^3]
        pop_inversion_mesh = (
            (self.population_inversion.pump_wavelength / (const.h * const.c))
            * (
                (
                    (
                        1
                        - np.exp(
                            -self.population_inversion.crystal_alpha
                            * self.length
                            * nslice
                        )
                    )
                    * (1.0 - fraction_to_heating)
                    * self.population_inversion.pump_energy
                    * np.exp(
                        -2.0
                        * (
                            np.sqrt(
                                (xv - self.population_inversion.pump_offset_x) ** 2.0
                                + (yv - self.population_inversion.pump_offset_y) ** 2.0
                            )
                            / self.population_inversion.pump_waist
                        )
                        ** g_order
                    )
                )
                / (const.pi * integral_factor)
            )
            * np.exp(-self.population_inversion.crystal_alpha * z)
            * correction_factor
        ) / (self.length * nslice)

        return pop_inversion_mesh

    def _right_pump(self, nslice, xv, yv):

        # z = distance from right of crystal to center of current slice (assumes all crystal slices have same length)
        z = self.length * ((nslice - self.slice_index - 1) + 0.5)

        slice_front = z - (self.length / 2.0)
        slice_end = z + (self.length / 2.0)

        # calculate correction factor for representing a gaussian pulse with a series of flat-top slices
        correction_factor = (
            (
                np.exp(-self.population_inversion.crystal_alpha * slice_front)
                - np.exp(-self.population_inversion.crystal_alpha * slice_end)
            )
            / self.population_inversion.crystal_alpha
        ) / (np.exp(-self.population_inversion.crystal_alpha * z) * self.length)

        # integrate super-gaussian
        g_order = self.population_inversion.pump_gaussian_order
        integral_factor = (2 ** ((g_order - 2.0) / g_order) * gamma(2 / g_order)) / (
            g_order
            * (1 / (self.population_inversion.pump_waist**g_order)) ** (2.0 / g_order)
        )

        pump_wavelength = 532.0  # [nm]
        seed_wavelength = 800.0  # [nm]
        fraction_to_heating = (seed_wavelength - pump_wavelength) / seed_wavelength

        # Create a default mesh of [num_excited_states/m^3]
        pop_inversion_mesh = (
            (self.population_inversion.pump_wavelength / (const.h * const.c))
            * (
                (
                    (
                        1
                        - np.exp(
                            -self.population_inversion.crystal_alpha
                            * self.length
                            * nslice
                        )
                    )
                    * (1.0 - fraction_to_heating)
                    * self.population_inversion.pump_energy
                    * np.exp(
                        -2.0
                        * (
                            np.sqrt(
                                (xv - self.population_inversion.pump_offset_x) ** 2.0
                                + (yv - self.population_inversion.pump_offset_y) ** 2.0
                            )
                            / self.population_inversion.pump_waist
                        )
                        ** g_order
                    )
                )
                / (const.pi * integral_factor)
            )
            * np.exp(-self.population_inversion.crystal_alpha * z)
            * correction_factor
        ) / (self.length * nslice)

        return pop_inversion_mesh

    def _dual_pump(self, nslice, xv, yv):
        left_pump_mesh = self._left_pump(nslice, xv, yv)
        right_pump_mesh = self._right_pump(nslice, xv, yv)
        return left_pump_mesh + right_pump_mesh

    def _initialize_excited_states_mesh(self, population_inversion, nslice):
        self.population_inversion = population_inversion
        x = np.linspace(
            -population_inversion.mesh_extent,
            population_inversion.mesh_extent,
            population_inversion.n_cells,
        )
        xv, yv = np.meshgrid(x, x)

        self.pop_inversion_mesh = PKDict(
            dual=self._dual_pump,
            left=self._left_pump,
            right=self._right_pump,
        )[population_inversion.pump_type](nslice, xv, yv)

    def _propagate_attenuate(self, laser_pulse, calc_gain):
        # n_x = wfront.mesh.nx  #  nr of grid points in x
        # n_y = wfront.mesh.ny  #  nr of grid points in y
        # sig_cr_sec = np.ones((n_x, n_y), dtype=np.float32)
        # pop_inv = self.pop_inv
        # n0_phot = 0.0 *sig_cr_sec # incident photon density (3D), at a given transv. loc-n
        # eta = n0_phot *c_light *tau_pulse
        # gamma_degen = 1.0
        # en_gain = np.log( 1. +np.exp(sig_cr_sec *pop_inv *element.length) *(
        #             np.exp(gamma_degen *sig_cr_sec *eta) -1.0) ) /(gamma_degen *sig_cr_sec *eta)
        # return laser_pulse
        raise NotImplementedError(
            f'{self}.propagate() with prop_type="attenuate" is not currently supported'
        )

    def _propagate_placeholder(self, laser_pulse, calc_gain):
        # nslices = len(laser_pulse.slice)
        # for i in np.arange(nslices):
        #     print ('Pulse slice ', i+1, ' of ', nslices, ' propagated through crystal slice.')
        # return laser_pulse
        raise NotImplementedError(
            f'{self}.propagate() with prop_type="placeholder" is not currently supported'
        )

    def _propagate_n0n2_lct(self, laser_pulse, calc_gain):
        # print('prop_type = n0n2_lct')
        nslices_pulse = len(laser_pulse.slice)
        L_cryst = self.length
        n0 = self.n0
        n2 = self.n2
        # print('n0: %g, n2: %g' %(n0, n2))
        l_scale = self.l_scale

        photon_e_ev = laser_pulse.photon_e_ev

        ##Convert energy to wavelength
        hc_ev_um = 1.23984198  # hc [eV*um]
        phLambda = (
            hc_ev_um / photon_e_ev * 1e-6
        )  # wavelength corresponding to photon_e_ev in meters
        # print("Wavelength corresponding to %g keV: %g microns" %(photon_e_ev * 1e-3, phLambda / 1e-6))

        # calculate components of ABCD matrix corrected with wavelength and scale factor for use in LCT algorithm
        gamma = np.sqrt(n2 / n0)
        A = np.cos(gamma * L_cryst)
        B = (1 / gamma) * np.sin(gamma * L_cryst) * phLambda / (l_scale**2)
        C = -gamma * np.sin(gamma * L_cryst) / phLambda * (l_scale**2)
        D = np.cos(gamma * L_cryst)
        abcd_mat_cryst = np.array([[A, B], [C, D]])
        # print('A: %g' %A)
        # print('B: %g' %B)
        # print('C: %g' %C)
        # print('D: %g' %D)

        for i in np.arange(nslices_pulse):
            # i = 0
            thisSlice = laser_pulse.slice[i]
            if calc_gain:
                thisSlice = self.calc_gain(thisSlice)

            # construct 2d numpy complex E_field from pulse wfr object
            # pol = 6 in calc_int_from_wfr() for full electric
            # field (0 corresponds to horizontal, 1 corresponds to vertical polarization)
            wfr0 = thisSlice.wfr

            # horizontal component of electric field
            re0_ex, re0_mesh_ex = srwutil.calc_int_from_wfr(
                wfr0, _pol=0, _int_type=5, _det=None, _fname="", _pr=False
            )
            im0_ex, im0_mesh_ex = srwutil.calc_int_from_wfr(
                wfr0, _pol=0, _int_type=6, _det=None, _fname="", _pr=False
            )
            re0_2d_ex = np.array(re0_ex).reshape(
                (wfr0.mesh.nx, wfr0.mesh.ny), order="C"
            )
            im0_2d_ex = np.array(im0_ex).reshape(
                (wfr0.mesh.nx, wfr0.mesh.ny), order="C"
            )

            # vertical componenent of electric field
            re0_ey, re0_mesh_ey = srwutil.calc_int_from_wfr(
                wfr0, _pol=1, _int_type=5, _det=None, _fname="", _pr=False
            )
            im0_ey, im0_mesh_ey = srwutil.calc_int_from_wfr(
                wfr0, _pol=1, _int_type=6, _det=None, _fname="", _pr=False
            )
            re0_2d_ey = np.array(re0_ey).reshape(
                (wfr0.mesh.nx, wfr0.mesh.ny), order="C"
            )
            im0_2d_ey = np.array(im0_ey).reshape(
                (wfr0.mesh.nx, wfr0.mesh.ny), order="C"
            )

            Etot0_2d_x = re0_2d_ex + 1j * im0_2d_ex
            Etot0_2d_y = re0_2d_ey + 1j * im0_2d_ey

            xvals_slice = np.linspace(wfr0.mesh.xStart, wfr0.mesh.xFin, wfr0.mesh.nx)
            yvals_slice = np.linspace(wfr0.mesh.yStart, wfr0.mesh.yFin, wfr0.mesh.ny)

            dX = xvals_slice[1] - xvals_slice[0]  # horizontal spacing [m]
            dX_scale = dX / l_scale
            dY = yvals_slice[1] - yvals_slice[0]  # vertical spacing [m]
            dY_scale = dY / l_scale

            # define horizontal and vertical input signals
            in_signal_2d_x = (dX_scale, dY_scale, Etot0_2d_x)
            in_signal_2d_y = (dX_scale, dY_scale, Etot0_2d_y)

            # calculate 2D LCTs
            dX_out, dY_out, out_signal_2d_x = rslct.apply_lct_2d_sep(
                abcd_mat_cryst, abcd_mat_cryst, in_signal_2d_x
            )
            dX_out, dY_out, out_signal_2d_y = rslct.apply_lct_2d_sep(
                abcd_mat_cryst, abcd_mat_cryst, in_signal_2d_y
            )

            # extract propagated complex field and calculate corresponding x and y mesh arrays
            # we assume same mesh for both components of E_field
            hx = dX_out * l_scale
            hy = dY_out * l_scale
            # sig_arr_x = out_signal_2d_x
            # sig_arr_y = out_signal_2d_y
            ny, nx = np.shape(out_signal_2d_x)
            local_xv = rslct.lct_abscissae(nx, hx)
            local_yv = rslct.lct_abscissae(ny, hy)
            x_min = np.min(local_xv)
            x_max = np.max(local_xv)
            y_min = np.min(local_xv)
            y_max = np.max(local_xv)

            # return to SRW wavefront form
            ex_real = np.real(out_signal_2d_x).flatten(order="C")
            ex_imag = np.imag(out_signal_2d_x).flatten(order="C")

            ey_real = np.real(out_signal_2d_y).flatten(order="C")
            ey_imag = np.imag(out_signal_2d_y).flatten(order="C")

            ex_numpy = np.zeros(2 * len(ex_real))
            for i in range(len(ex_real)):
                ex_numpy[2 * i] = ex_real[i]
                ex_numpy[2 * i + 1] = ex_imag[i]

            ey_numpy = np.zeros(2 * len(ey_real))
            for i in range(len(ey_real)):
                ey_numpy[2 * i] = ey_real[i]
                ey_numpy[2 * i + 1] = ey_imag[i]

            ex = array.array("f", ex_numpy.tolist())
            ey = array.array("f", ey_numpy.tolist())

            wfr1 = srwlib.SRWLWfr(
                _arEx=ex,
                _arEy=ey,
                _typeE="f",
                _eStart=photon_e_ev,
                _eFin=photon_e_ev,
                _ne=1,
                _xStart=x_min,
                _xFin=x_max,
                _nx=nx,
                _yStart=y_min,
                _yFin=y_max,
                _ny=ny,
                _zStart=0.0,
                _partBeam=None,
            )

            thisSlice.wfr = wfr1

        # return wfr1
        return laser_pulse

    def _propagate_abcd_lct(self, laser_pulse, calc_gain):
        # print('prop_type = abcd_lct')
        nslices_pulse = len(laser_pulse.slice)
        l_scale = self.l_scale

        photon_e_ev = laser_pulse.photon_e_ev

        ##Convert energy to wavelength
        hc_ev_um = 1.23984198  # hc [eV*um]
        phLambda = (
            hc_ev_um / photon_e_ev * 1e-6
        )  # wavelength corresponding to photon_e_ev in meters
        # print("Wavelength corresponding to %g keV: %g microns" %(photon_e_ev * 1e-3, phLambda / 1e-6))

        # rescale ABCD matrix with wavelength and scale factor for use in LCT algorithm
        A = self.A
        B = self.B * phLambda / (l_scale**2)
        C = self.C / phLambda * (l_scale**2)
        D = self.D
        abcd_mat_cryst = np.array([[A, B], [C, D]])
        # print('A: %g' %A)
        # print('B: %g' %B)
        # print('C: %g' %C)
        # print('D: %g' %D)

        for i in np.arange(nslices_pulse):
            # i = 0
            thisSlice = laser_pulse.slice[i]
            if calc_gain:
                thisSlice = self.calc_gain(thisSlice)

            # construct 2d numpy complex E_field from pulse wfr object
            # pol = 6 in calc_int_from_wfr() for full electric
            # field (0 corresponds to horizontal, 1 corresponds to vertical polarization)
            wfr0 = thisSlice.wfr

            # horizontal component of electric field
            re0_ex, re0_mesh_ex = srwutil.calc_int_from_wfr(
                wfr0, _pol=0, _int_type=5, _det=None, _fname="", _pr=False
            )
            im0_ex, im0_mesh_ex = srwutil.calc_int_from_wfr(
                wfr0, _pol=0, _int_type=6, _det=None, _fname="", _pr=False
            )
            re0_2d_ex = np.array(re0_ex).reshape(
                (wfr0.mesh.nx, wfr0.mesh.ny), order="C"
            )
            im0_2d_ex = np.array(im0_ex).reshape(
                (wfr0.mesh.nx, wfr0.mesh.ny), order="C"
            )

            # vertical componenent of electric field
            re0_ey, re0_mesh_ey = srwutil.calc_int_from_wfr(
                wfr0, _pol=1, _int_type=5, _det=None, _fname="", _pr=False
            )
            im0_ey, im0_mesh_ey = srwutil.calc_int_from_wfr(
                wfr0, _pol=1, _int_type=6, _det=None, _fname="", _pr=False
            )
            re0_2d_ey = np.array(re0_ey).reshape(
                (wfr0.mesh.nx, wfr0.mesh.ny), order="C"
            )
            im0_2d_ey = np.array(im0_ey).reshape(
                (wfr0.mesh.nx, wfr0.mesh.ny), order="C"
            )

            Etot0_2d_x = re0_2d_ex + 1j * im0_2d_ex
            Etot0_2d_y = re0_2d_ey + 1j * im0_2d_ey

            xvals_slice = np.linspace(wfr0.mesh.xStart, wfr0.mesh.xFin, wfr0.mesh.nx)
            yvals_slice = np.linspace(wfr0.mesh.yStart, wfr0.mesh.yFin, wfr0.mesh.ny)

            dX = xvals_slice[1] - xvals_slice[0]  # horizontal spacing [m]
            dX_scale = dX / l_scale
            dY = yvals_slice[1] - yvals_slice[0]  # vertical spacing [m]
            dY_scale = dY / l_scale

            # define horizontal and vertical input signals
            in_signal_2d_x = (dX_scale, dY_scale, Etot0_2d_x)
            in_signal_2d_y = (dX_scale, dY_scale, Etot0_2d_y)

            # calculate 2D LCTs
            dX_out, dY_out, out_signal_2d_x = rslct.apply_lct_2d_sep(
                abcd_mat_cryst, abcd_mat_cryst, in_signal_2d_x
            )
            dX_out, dY_out, out_signal_2d_y = rslct.apply_lct_2d_sep(
                abcd_mat_cryst, abcd_mat_cryst, in_signal_2d_y
            )

            # extract propagated complex field and calculate corresponding x and y mesh arrays
            # we assume same mesh for both components of E_field
            hx = dX_out * l_scale
            hy = dY_out * l_scale
            # sig_arr_x = out_signal_2d_x
            # sig_arr_y = out_signal_2d_y
            ny, nx = np.shape(out_signal_2d_x)
            local_xv = rslct.lct_abscissae(nx, hx)
            local_yv = rslct.lct_abscissae(ny, hy)
            x_min = np.min(local_xv)
            x_max = np.max(local_xv)
            y_min = np.min(local_xv)
            y_max = np.max(local_xv)

            # return to SRW wavefront form
            ex_real = np.real(out_signal_2d_x).flatten(order="C")
            ex_imag = np.imag(out_signal_2d_x).flatten(order="C")

            ey_real = np.real(out_signal_2d_y).flatten(order="C")
            ey_imag = np.imag(out_signal_2d_y).flatten(order="C")

            ex_numpy = np.zeros(2 * len(ex_real))
            for i in range(len(ex_real)):
                ex_numpy[2 * i] = ex_real[i]
                ex_numpy[2 * i + 1] = ex_imag[i]

            ey_numpy = np.zeros(2 * len(ey_real))
            for i in range(len(ey_real)):
                ey_numpy[2 * i] = ey_real[i]
                ey_numpy[2 * i + 1] = ey_imag[i]

            ex = array.array("f", ex_numpy.tolist())
            ey = array.array("f", ey_numpy.tolist())

            wfr1 = srwlib.SRWLWfr(
                _arEx=ex,
                _arEy=ey,
                _typeE="f",
                _eStart=photon_e_ev,
                _eFin=photon_e_ev,
                _ne=1,
                _xStart=x_min,
                _xFin=x_max,
                _nx=nx,
                _yStart=y_min,
                _yFin=y_max,
                _ny=ny,
                _zStart=0.0,
                _partBeam=None,
            )

            thisSlice.wfr = wfr1

        # return wfr1
        return laser_pulse

    def _propagate_n0n2_srw(self, laser_pulse, calc_gain):
        # print('prop_type = n0n2_srw')
        nslices = len(laser_pulse.slice)
        L_cryst = self.length
        n0 = self.n0
        n2 = self.n2
        # print('n0: %g, n2: %g' %(n0, n2))

        for i in np.arange(nslices):
            thisSlice = laser_pulse.slice[i]
            if calc_gain:
                thisSlice = self.calc_gain(thisSlice)

            if n2 == 0:
                # print('n2 = 0')
                # A = 1.0
                # B = L_cryst
                # C = 0.0
                # D = 1.0
                optDrift = srwlib.SRWLOptD(L_cryst / n0)
                propagParDrift = [0, 0, 1.0, 0, 0, 1.0, 1.0, 1.0, 1.0, 0, 0, 0]
                # propagParDrift = [0, 0, 1., 0, 0, 1.1, 1.2, 1.1, 1.2, 0, 0, 0]
                optBL = srwlib.SRWLOptC([optDrift], [propagParDrift])
                # print("L_cryst/n0=",L_cryst/n0)
            else:
                # print('n2 .ne. 0')
                gamma = np.sqrt(n2 / n0)
                A = np.cos(gamma * L_cryst)
                B = (1 / gamma) * np.sin(gamma * L_cryst)
                C = -gamma * np.sin(gamma * L_cryst)
                D = np.cos(gamma * L_cryst)
                f1 = B / (1 - A)
                L = B
                f2 = B / (1 - D)

                optLens1 = srwlib.SRWLOptL(f1, f1)
                optDrift = srwlib.SRWLOptD(L)
                optLens2 = srwlib.SRWLOptL(f2, f2)

                propagParLens1 = [0, 0, 1.0, 0, 0, 1, 1, 1, 1, 0, 0, 0]
                propagParDrift = [0, 0, 1.0, 0, 0, 1, 1, 1, 1, 0, 0, 0]
                propagParLens2 = [0, 0, 1.0, 0, 0, 1, 1, 1, 1, 0, 0, 0]

                optBL = srwlib.SRWLOptC(
                    [optLens1, optDrift, optLens2],
                    [propagParLens1, propagParDrift, propagParLens2],
                )
                # optBL = createABCDbeamline(A,B,C,D)

            srwlib.srwl.PropagElecField(
                thisSlice.wfr, optBL
            )  # thisSlice s.b. a pointer, not a copy
            # print('Propagated pulse slice ', i+1, ' of ', nslices)
        return laser_pulse

    def _propagate_gain_calc(self, laser_pulse, calc_gain):
        # calculates gain regardles of calc_gain param value
        for i in np.arange(len(laser_pulse.slice)):
            thisSlice = laser_pulse.slice[i]
            thisSlice = self.calc_gain(thisSlice)
        return laser_pulse

    def propagate(self, laser_pulse, prop_type, calc_gain=False):
        return PKDict(
            attenuate=self._propagate_attenuate,
            placeholder=self._propagate_placeholder,
            abcd_lct=self._propagate_abcd_lct,
            n0n2_lct=self._propagate_n0n2_lct,
            n0n2_srw=self._propagate_n0n2_srw,
            gain_calc=self._propagate_gain_calc,
            default=super().propagate,
        )[prop_type](laser_pulse, calc_gain)

    def _interpolate_a_to_b(self, a, b):
        if a == "pop_inversion":
            # interpolate copy of pop_inversion to match lp_wfr
            temp_array = np.copy(self.pop_inversion_mesh)

            a_x = np.linspace(
                -self.population_inversion.mesh_extent,
                self.population_inversion.mesh_extent,
                self.population_inversion.n_cells,
            )
            a_y = a_x
            b_x = np.linspace(b.mesh.xStart, b.mesh.xFin, b.mesh.nx)
            b_y = np.linspace(b.mesh.yStart, b.mesh.yFin, b.mesh.ny)

        elif b == "pop_inversion":
            # interpolate copy of change_pop_inversion to match pop_inversion
            temp_array = np.copy(a.mesh)

            a_x = a.x
            a_y = a.y
            b_x = np.linspace(
                -self.population_inversion.mesh_extent,
                self.population_inversion.mesh_extent,
                self.population_inversion.n_cells,
            )
            b_y = b_x

        if not (np.array_equal(a_x, b_x) and np.array_equal(a_y, b_y)):

            # Create the spline for interpolation
            rect_biv_spline = RectBivariateSpline(a_x, a_y, temp_array)

            # Evaluate the spline at b gridpoints
            temp_array = rect_biv_spline(b_x, b_y)

            # Set any interpolated values outside the bounds of the original mesh to zero
            temp_array[b_x > np.max(a_x), :] = 0.0
            temp_array[b_x < np.min(a_x), :] = 0.0
            temp_array[:, b_y > np.max(a_y)] = 0.0
            temp_array[:, b_y < np.min(a_y)] = 0.0

        return temp_array

    def calc_gain(self, thisSlice):

        lp_wfr = thisSlice.wfr

        # Interpolate the excited state density mesh of the current crystal slice to
        # match the laser_pulse wavefront mesh
        temp_pop_inversion = self._interpolate_a_to_b("pop_inversion", lp_wfr)

        # Calculate gain
        cross_sec = splev(thisSlice._lambda0, self.cross_section_fn)  # [m^2]
        degen_factor = 1.67

        dx = (lp_wfr.mesh.xFin - lp_wfr.mesh.xStart) / lp_wfr.mesh.nx  # [m]
        dy = (lp_wfr.mesh.yFin - lp_wfr.mesh.yStart) / lp_wfr.mesh.ny  # [m]
        n_incident_photons = thisSlice.n_photons_2d.mesh / (dx * dy)  # [1/m^2]

        energy_gain = np.zeros(np.shape(n_incident_photons))
        gain_condition = np.where(n_incident_photons > 0)
        energy_gain[gain_condition] = (
            1.0 / (degen_factor * cross_sec * n_incident_photons[gain_condition])
        ) * np.log(
            1
            + np.exp(cross_sec * temp_pop_inversion[gain_condition] * self.length)
            * (
                np.exp(degen_factor * cross_sec * n_incident_photons[gain_condition])
                - 1.0
            )
        )

        # Calculate change factor for pop_inversion, note it has the same dimensions as lp_wfr
        change_pop_mesh = -(
            degen_factor * n_incident_photons * (energy_gain - 1.0) / self.length
        )
        change_pop_inversion = PKDict(
            mesh=change_pop_mesh,
            x=np.linspace(lp_wfr.mesh.xStart, lp_wfr.mesh.xFin, lp_wfr.mesh.nx),
            y=np.linspace(lp_wfr.mesh.yStart, lp_wfr.mesh.yFin, lp_wfr.mesh.ny),
        )

        # Interpolate the change to the excited state density mesh of the current crystal slice (change_pop_inversion)
        # so that it matches self.pop_inversion
        change_pop_inversion.mesh = self._interpolate_a_to_b(
            change_pop_inversion, "pop_inversion"
        )

        # Update the pop_inversion_mesh
        self.pop_inversion_mesh += change_pop_inversion.mesh

        # Update the number of photons
        thisSlice.n_photons_2d.mesh *= energy_gain

        # Update the wavefront itself: (KW To Do: make this a separate method?)
        #    First extract the electric fields

        # horizontal component of electric field
        re0_ex, re0_mesh_ex = srwutil.calc_int_from_wfr(
            lp_wfr, _pol=0, _int_type=5, _det=None, _fname="", _pr=False
        )
        im0_ex, im0_mesh_ex = srwutil.calc_int_from_wfr(
            lp_wfr, _pol=0, _int_type=6, _det=None, _fname="", _pr=False
        )
        gain_re0_ex = np.float64(re0_ex) * np.sqrt(energy_gain).flatten(order="C")
        gain_im0_ex = np.float64(im0_ex) * np.sqrt(energy_gain).flatten(order="C")

        # vertical componenent of electric field
        re0_ey, re0_mesh_ey = srwutil.calc_int_from_wfr(
            lp_wfr, _pol=1, _int_type=5, _det=None, _fname="", _pr=False
        )
        im0_ey, im0_mesh_ey = srwutil.calc_int_from_wfr(
            lp_wfr, _pol=1, _int_type=6, _det=None, _fname="", _pr=False
        )
        gain_re0_ey = np.float64(re0_ey) * np.sqrt(energy_gain).flatten(order="C")
        gain_im0_ey = np.float64(im0_ey) * np.sqrt(energy_gain).flatten(order="C")

        ex_numpy = np.zeros(2 * len(gain_re0_ex))
        for i in range(len(gain_re0_ex)):
            ex_numpy[2 * i] = gain_re0_ex[i]
            ex_numpy[2 * i + 1] = gain_im0_ex[i]

        ey_numpy = np.zeros(2 * len(gain_re0_ey))
        for i in range(len(gain_re0_ey)):
            ey_numpy[2 * i] = gain_re0_ey[i]
            ey_numpy[2 * i + 1] = gain_im0_ey[i]

        ex = array.array("f", ex_numpy.tolist())
        ey = array.array("f", ey_numpy.tolist())

        #    Pass changes to SRW
        wfr1 = srwlib.SRWLWfr(
            _arEx=ex,
            _arEy=ey,
            _typeE="f",
            _eStart=thisSlice.photon_e_ev,
            _eFin=thisSlice.photon_e_ev,
            _ne=1,
            _xStart=lp_wfr.mesh.xStart,
            _xFin=lp_wfr.mesh.xFin,
            _nx=lp_wfr.mesh.nx,
            _yStart=lp_wfr.mesh.yStart,
            _yFin=lp_wfr.mesh.yFin,
            _ny=lp_wfr.mesh.ny,
            _zStart=0.0,
            _partBeam=None,
        )

        thisSlice.wfr = wfr1
        return thisSlice


def _calculate_mesh(crystal_length, crystal_diameter, mesh_density):
    # geometry dimensions:
    #    crystal_length [cm]
    #    crystal_diameter [cm]

    # derived parameters
    rad = crystal_diameter / 2.0  # radius [cm]
    lh = crystal_length / 2.0  # half-length [cm]
    rad2 = rad**2.0  # radius squared [cm^2]

    # cylinder = Cylinder('coordinate of center of the top circle',
    #                     'coordinate of center of the bottom circle',
    #                     'radius of the circle at the top',
    #                     'radius of the circle at the bottom')
    geometry = Cylinder(Point(0.0, 0.0, lh), Point(0.0, 0.0, -lh), rad, rad)
    return generate_mesh(geometry, mesh_density)


def _define_heat_load_expression(
    pump_waist, absorption_coefficient, crystal_length, pump_power
):

    w_p = pump_waist  # updated beam width [cm]
    alpha_h = absorption_coefficient  # absorption coefficient [1/cm]
    half_length = crystal_length / 2.0  # half-length [cm]

    # calculate incremental temperature deposition
    pump_wavelength = 532.0  # [nm]
    seed_wavelength = 800.0  # [nm]
    P_abs = (
        pump_power * (seed_wavelength - pump_wavelength) / seed_wavelength
    )  # absorbed power [W]
    V_eff = (np.pi * w_p**2.0 / (2.0 * alpha_h)) * (
        1.0 - np.exp(-alpha_h * crystal_length)
    )  # effective volume [cm^3]

    K_c_tisaph = (
        33.0 / 100.0
    )  # thermal conductivity [W/cm/K] https://www.rp-photonics.com/titanium_sapphire_lasers.html

    dQ_incr = P_abs / V_eff  # incremental heat deposition [W/cm^3]
    dT_incr = dQ_incr / K_c_tisaph  # incremental temperature deposition [K/cm^2]

    gsn_bella_heat_load = Expression(
        "dT * exp( -2.0 * (x[0] * x[0] + x[1] * x[1]) / (w_p * w_p)) * exp(-alpha_h * (x[2] + lh))",
        degree=1,
        dT=dT_incr,
        w_p=w_p,
        alpha_h=alpha_h,
        lh=half_length,
    )
    return gsn_bella_heat_load


def _calc_temperature_change(
    pump_waist,
    absorption_coefficient,
    crystal_length,
    crystal_diameter,
    pump_energy,
    xv,
    zv,
):

    # c_p is temperature and doping dependent, for now approximate with a single value
    specific_heat_capacity = 0.7788  # J/g/K (at 300K, for sapphire)
    sapphire_density = 3.98  # g/cc
    crystal_volume = np.pi * (crystal_diameter / 2.0) ** 2.0 * crystal_length  # cm^3
    mass = crystal_volume * sapphire_density  # grams

    w_p = pump_waist  # updated beam width [cm]
    alpha_h = absorption_coefficient  # absorption coefficient [1/cm]
    half_length = crystal_length / 2.0  # half-length [cm]

    pump_wavelength = 532.0  # [nm]
    seed_wavelength = 800.0  # [nm]
    J_abs = (
        pump_energy * (seed_wavelength - pump_wavelength) / seed_wavelength
    )  # absorbed energy [J]

    xv_2d, zv_2d = np.meshgrid(xv, zv)

    radial_term = np.exp(-2.0 * (xv_2d**2.0) / (w_p * w_p))
    longitudinal_term = np.exp(-alpha_h * (zv_2d + half_length))
    magnitude_term = J_abs / (specific_heat_capacity * mass)

    long_temp_profiles = magnitude_term * radial_term * longitudinal_term

    return long_temp_profiles.T


def _call_fenics(mesh, heat_load, crystal_diameter, initial_temp, zv, radial_pts):

    # define function space on mesh
    V = FunctionSpace(mesh, "P", 1)

    rad = crystal_diameter / 2.0  # radius [cm]
    rad2 = rad**2.0  # radius squared [cm^2]

    # define Dirichlet boundary condition for sides at r_max
    bc_tol = 2.0 * rad * (rad / 40.0)  # 2 * rad * delta(rad)

    def boundary_D(x, on_boundary):
        return on_boundary and near(x[0] * x[0] + x[1] * x[1], rad2, bc_tol)

    boundary_condition = DirichletBC(V, Constant(initial_temp), boundary_D)

    # define variational problem
    fenics_solution = Function(V)
    v = TestFunction(V)

    # source term
    f = heat_load

    # differential eqn to be solved
    F = (
        dot(grad(fenics_solution), grad(v)) * dx - f * v * dx
    )  # w/ Dirichlet + initial condition

    # execute simulation
    set_log_level(30)
    solve(F == 0.0, fenics_solution, boundary_condition)

    # note: throws an error to evaluate at the limits of radial_pts -
    # reduce the value of radial_pts by some factor, rad_fac
    rad_fac = 0.9

    # longitudinal temperature profiles of a range of radii values ranging from +/- r_max
    long_temp_profiles = np.zeros((len(radial_pts), len(zv)))
    for j in range(len(radial_pts)):
        long_temp_profiles[j] = [
            fenics_solution(pt)
            for pt in [(radial_pts[j][0] * rad_fac, 0.0, z_) for z_ in zv]
        ]

    return long_temp_profiles


def _calc_T(long_temp_profiles, crystal_length, num_long_slices, zv, radial_pts):

    dslice_ind = int(
        (len(radial_pts) - 1) / num_long_slices
    )  # size of of slice indeces

    # divide long_temp_profiles into n longitudinal slices
    uz_array = np.zeros((num_long_slices, dslice_ind, len(radial_pts)))
    uz_array[0, :, :] = np.transpose(long_temp_profiles[:, 0:dslice_ind])
    for j in range(num_long_slices - 1):
        uz_array[j + 1, :, :] = np.transpose(
            long_temp_profiles[:, (dslice_ind * (j + 1)) : (dslice_ind * (j + 2))]
        )

    # calculate longitudinal step size, dz
    dz = (zv[len(zv) - 1] - zv[0]) / (len(zv) - 1.0)

    radial_pts_x = np.array(radial_pts)[:, 0]

    # integrate longitudinal temperature profiles
    integrated_temps = np.zeros((num_long_slices, len(radial_pts_x)))
    for j in range(num_long_slices):
        for i in range(len(radial_pts_x)):
            integrated_temps[j, i] = (
                np.sum(uz_array[j, :, i]) * dz / (crystal_length / num_long_slices)
            )

    return integrated_temps


def _calc_n_from_T(
    num_long_slices, radial_pts, integrated_temps, laser_range_min, laser_range_max
):
    # Calculate index of refraction for each slice from T(r), using a formula from Tapping (1986)
    # chi_T taken from: 'Thermal lens effect model of Ti:sapphire for use in high-power laser amplifiers' - Jeong 2018

    chi_T = 1.28e-5
    n_int_vals = np.zeros((num_long_slices, len(integrated_temps[0, :])))
    for j in range(num_long_slices):
        for i in range(len(integrated_temps[0, :])):
            n_int_vals[j, i] = (
                1.75991
                + (chi_T * integrated_temps[j, i])
                + (3.1e-9 * integrated_temps[j, i] ** 2.0)
            )

    def _quad_int0(x, A, B):
        # quad fit function
        y = A * x**2.0 + B
        return y

    radial_pts_x = np.array(radial_pts)[:, 0]

    # create arrays for n_int(r) for region within laser radius and apply quadratic fit
    # extract n0 and n2 values
    n2_vals = np.zeros((num_long_slices))
    n0_vals = np.zeros((num_long_slices))
    parameters_q_intn = np.zeros((num_long_slices, 2))
    covariance_q_intn = np.zeros((num_long_slices, 2, 2))
    for j in range(num_long_slices):
        parameters_q_intn[j, :], covariance_q_intn[j, :] = curve_fit(
            _quad_int0,
            radial_pts_x[laser_range_min:laser_range_max],
            n_int_vals[j, laser_range_min:laser_range_max],
        )
        n2_vals[j] = parameters_q_intn[j, 0]
        n0_vals[j] = parameters_q_intn[j, 1]

    return n0_vals, n2_vals


def _calc_full_abcd_mat(crystal_length, n0_vals, n2_vals):
    # Calculate the ABCD matrix for the entire crystal

    num_long_slices = len(n0_vals)
    gamma_vals = np.zeros(num_long_slices)
    abcd_mats = np.zeros((num_long_slices, 2, 2))
    for j in range(num_long_slices):
        # extract gamma_vals
        gamma_vals[j] = np.sqrt(n2_vals[j] / n0_vals[j])
        gamma_z = gamma_vals[j] * (crystal_length / num_long_slices)

        # calculate ABCD matrices for each crystal slice
        abcd_mats[j, 0, 0] = np.cos(gamma_z)
        abcd_mats[j, 0, 1] = (1.0 / n0_vals[j] / gamma_vals[j]) * np.sin(gamma_z)
        abcd_mats[j, 1, 0] = (-(n0_vals[j] * gamma_vals[j])) * np.sin(gamma_z)
        abcd_mats[j, 1, 1] = np.cos(gamma_z)

    # calculate total ABCD matrix by multiplying matrices for individual slices in order M_n-1 * M_n-2 * ... * M_0
    abcd_mat_tot_full = np.array(
        [[1, 0], [0, 1]]
    )  # initialize total ABCD mat as identity for first multiplication with M_n-1
    for j in range(num_long_slices):
        abcd_mat_tot_full = np.matmul(
            abcd_mat_tot_full, abcd_mats[num_long_slices - j - 1, :, :]
        )

    return abcd_mat_tot_full
