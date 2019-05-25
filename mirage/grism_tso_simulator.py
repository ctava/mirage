#! /urs/bin/env python

"""Code for creating simulations of grism time series observations.


1. Make 2 temporary copies of the input yaml file.
   Copy 1 will contain all catalogs except the catalog with the TSO source
       and have the mode set to 'wfss'
   Copy 2 will contain only the TSO source catalog with mode set to 'wfss'

   NOTE THAT STEPS 2 AND 3 CAN BE COMBINED INTO A SINGLE CALL TO WFSS_SIMULATOR
2. Run the catalog_seed_generator on Copy 1. Result will be a padded countrate image
   containing only the non-TSO sources, which have constant brightness with time
   (dare we think about moving targets? - No, not now. Save for a later PR)
3. Run the disperser on the seed image with non-TSO sources. Result will be 2d
   dispersed countrate image

4. Run the catalog_seed_generator on Copy 2. Result will be a padded 2d countrate
   image containing only the TSO source
5. Read in transmission curve
6. Use batman to generate lightcurves from the transmission spectrum for each of
   a grid of wavelengths. (What grid? Dispersed image is 10A per res. element, so
   use that resolution?)
7. Run the disperser using the original, unaltered stellar spectrum. Set 'cache=True'.
   Result will be the dispersed countrate image of the TSO source for times when
   the lightcurve is 1.0 everywhere
8. Create an output array to hold the frame-by-frame seed image. Probably easier if this
   array holds just the signal accumulated in a given frame for that frame, rather than
   the cumulative signal in that frame. Note that there will
   most likely be file-splitting happening at this point...
9. Determine which frames of the exposure will take place with the unaltered stellar
   spectrum. This will be all frames where the associated lightcurve is 1.0 everywhere.
   These frames will be simply copies of the outputs from step 7 plus step 4. To get
   around the issue of file splitting, easier to just keep a list of frame index
   numbers which this situation applies to.
10.For each of the remaining frames, run the disperser with the appropriate lightcurve
   (after running interp1d to turn it into a function). The frame seed will be this
   output plus that from step 4
11.As you go, translate into a cumulative frame by frame seed image, and save to a
   fits file and reset the array variable as you get to the limit of each segment
   part.
12.Run the dark current prep step
13.Run the observation generator

"""


import copy
import os
import sys
import argparse
import yaml

from astropy.io import fits
import astropy.units as u
import batman
import numpy as np
from NIRCAM_Gsim.grism_seed_disperser import Grism_seed
from scipy.interpolate import interp1d

from mirage import wfss_simulator
from mirage.catalogs import spectra_from_catalog
from mirage.seed_image import catalog_seed_image
from mirage.dark import dark_prep
from mirage.ramp_generator import obs_generator
from mirage.utils import read_fits
from mirage.utils.constants import CATALOG_YAML_ENTRIES
from mirage.utils.utils import expand_environment_variable, read_yaml, write_yaml, get_frame_count_info, calc_frame_time
from mirage.yaml import yaml_update


class GrismTSO():
    def __init__(self, paramfiles, SED_file=None, SED_normalizing_catalog_column=None,
                 final_SED_file=None, SED_dict=None, save_dispersed_seed=True, source_stamps_file=None,
                 extrapolate_SED=True, override_dark=None, disp_seed_filename=None, orders=["+1", "+2"],
                 create_continuum_seds=True):

        # Use the MIRAGE_DATA environment variable
        env_var = 'MIRAGE_DATA'
        self.datadir = os.environ.get(env_var)
        if self.datadir is None:
            raise ValueError(("WARNING: {} environment variable is not set."
                              "This must be set to the base directory"
                              "containing the darks, cosmic ray, PSF, etc"
                              "input files needed for the simulation."
                              "These files must be downloaded separately"
                              "from the Mirage package.".format(env_var)))

        # Set the user-input parameters
        self.create_continuum_seds = create_continuum_seds
        self.SED_file = SED_file
        self.SED_dict = SED_dict
        self.SED_normalizing_catalog_column = SED_normalizing_catalog_column
        self.final_SED_file = final_SED_file
        self.override_dark = override_dark
        self.save_dispersed_seed = save_dispersed_seed
        self.source_stamps_file = source_stamps_file
        self.disp_seed_filename = disp_seed_filename
        self.extrapolate_SED = extrapolate_SED
        self.fullframe_apertures = ["NRCA5_FULL", "NRCB5_FULL", "NIS_CEN"]
        self.orders = orders

        # Make sure the right combination of parameter files and SED file
        # are given
        self.param_checks()

        # Attempt to find the crossing filter and dispersion direction
        # from the input paramfiles. Adjust any imaging mode parameter
        # files to have the mode set to wfss. This will ensure the seed
        # images will be the proper (expanded) dimensions
        self.paramfiles = self.find_param_info()

        # Make sure inputs are correct
        self.check_inputs()

    def calculate_exposure_time(self):
        """Calculate the total exposure time of the observation being
        simulated. Include time for resets between integrations

        Returns
        -------
        exposure_time : float
            Exposure time for the total exposuure, including reset frames,
            in seconds
        """
        self.frametime = calc_frame_time(self.instrument, self.aperture, self.seed_dimensions[1],
                                         self.seed_dimensions[0], self.namps)
        return self.frametime * self.total_frames

    def create_seed(self):
        """MAIN FUNCTION"""

        # Get parameters necessary to create the TSO data
        orig_parameters = self.get_param_info()
        subarray_definition_file = ...
        subarray_table = read_subarray_definition_file(subarray_definition_file)
        orig_parameters = get_subarray_info(orig_parameters, subarray_table)

        # Determine file splitting information. First get some basic info
        # on the exposure
        self.numints = orig_parameters['Readout']['nint']
        self.numgroups = orig_parameters['Readout']['ngroup']
        self.numframes = orig_parameters['Readout']['nframe']
        self.numskips = orig_parameters['Readout']['nskip']
        self.numresets = orig_parameters['Readout']['resets_bet_ints']
        self.frames_per_group, self.frames_per_int, self.total_frames = get_frame_count_info(self.numints,
                                                                                             self.numgroups,
                                                                                             self.numframes,
                                                                                             self.numskips,
                                                                                             self.numresets)
        # Make 2 copies of the input parameter file, separating the TSO
        # source from the other sources
        self.split_param_file(orig_parameters)

        # Run the catalog_seed_generator on the non-TSO (background) sources
        background_direct = catalog_seed_image.Catalog_seed()
        background_direct.paramfile = self.background_paramfile
        background_direct.make_seed()

        # Run the disperser on the background sources
        background_dispersed = self.run_disperser(background_direct.seed_file, orders=self.orders,
                                                  create_continuum_seds=True)

        # Run the catalog_seed_generator on the TSO source
        tso_direct = catalog_seed_image.Catalog_seed()
        tso_direct.paramfile = self.tso_paramfile
        tso_direct.make_seed()

        # Dimensions are (y, x)
        self.seed_dimensions = tso_direct.nominal_dims

        # Read in the transmission spectrum that goes with the TSO source
        tso_params = read_yaml(self.tso_paramfile)
        tso_catalog_file = tso_params['simSignals']['tso_grism_catalog']
        tso_catalog = ascii.read(tso_catalog_file)
        #self.check_tso_catalog_inputs(tso_catalog)
        transmission_file = tso_catalog['Transmission_spectrum'].data
        transmission_spectrum = read_hdf5_or_ascii(transmission_file)

        # Calculate the total exposure time, including resets, to check
        # against the times provided in the catalog file.
        total_exposure_time = self.calculate_exposure_time() * u.second

        # Check to be sure the start and end times provided in the catalog
        # are enough to cover the length of the exposure.
        tso_catalog = self.tso_catalog_check(tso_catalog, total_exposure_time)

        # Use batman to create lightcurves from the transmission spectrum
        lightcurves, times = self.make_lightcurves(tso_catalog, self.frametime)

        # Determine which frames of the exposure will take place with the unaltered stellar
        # spectrum. This will be all frames where the associated lightcurve is 1.0 everywhere.
        transit_frames, unaltered_frames = self.find_transit_frames(lightcurves)

        # Run the disperser using the original, unaltered stellar spectrum. Set 'cache=True'
        grism_seed_object = self.run_disperser(tso_direct.seed_file, orders=self.orders)
        no_transit_signal = copy.deepcopy(grism_seed_object.final)

        # Calculate file splitting info
        self.file_splitting()

        # Prepare for creating output files
        segment_file_dir = orig_parameters['Output']['directory']
        if self.params['Readout']['pupil'][0].upper() == 'F':
            usefilt = 'pupil'
        else:
            usefilt = 'filter'
        segment_file_base = orig_parameters['Output']['file'].replace('.fits', '_')
        segment_file_base = '{}_{}_'.format(segment_file_base, orig_parameters['Readout'][usefilt])
        segment_file_base = os.path.join(segment_file_dir, segment_file_base)

        # Loop over frames and integrations up to the size of the segment
        # file.
        print('lightcurves shape:', lightcurves.shape)

        move below into seed_builder function

        ints_per_segment = self.int_segment_indexes[:-1] - self.int_segment_indexes[1:]
        groups_per_segment = self.grp_segment_indexes[:-1] - self.grp_segment_indexes[1:]
        print("Integration segment indexes: ", self.int_segment_indexes)
        print("ints_per_segment: ", ints_per_segment)
        print("Group segment indexes: ", self.grp_segment_indexes)
        print("groups_per_segment: ", groups_per_segment)
        total_frame_counter = 0
        previous_segment = 1
        segment_part_number = 0
        for i, int_dim in enumerate(ints_per_segment):
            int_end = self.int_segment_indexes[i+1]
            for j, grp_dim in enumerate(groups_per_segment):
                print("Integrations: {}, Groups: {}".format(int_dim, grp_dim))
                segment_seed = np.zeros((int_dim, grp_dim, self.seed_dimensions[0], self.seed_dimensions[1]))

                we need to deal with reset frames here. previous_frame signal should reset to zero,
                and no dispersion nor segment_seed population is necessary


                for integ in np.arange(int_dim):
                    previous_frame = np.zeros(self.seed_dimensions)
                    for frame in np.arange(grp_dim):
                        print('TOTAL FRAME COUNTER: ', total_frame_counter)
                        print('integ and frame: ', integ, frame)
                        # If a frame is from the part of the lightcurve
                        # with no transit, then the signal in the frame
                        # comes from no_transit_signal
                        if total_frame_counter in unaltered_frames:
                            print("{} is unaltered.".format(total_frame_counter))
                            frame_only_signal = (background_dispersed.final + no_transit_signal) * self.frametime
                        # If the frame is from a part of the lightcurve
                        # where the transit is happening, then call the
                        # cached disperser with the appropriate lightcurve
                        elif total_frame_counter in transit_frames:
                            print("{} is during the transit".format(total_frame_counter))
                            lightcurve = lightcurves[total_frame_counter, :]
                            lc_interp = interp1d(times, lightcurve)
                            for order in self.orders:
                                grism_seed_object.this_one[order].disperse_all_from_cache(lc_interp)
                            frame_only_signal = (background_dispersed.final + grism_seed_object.final) * self.frametime

                        # Now add the signal from this frame to that in the
                        # previous frame in order to arrive at the total
                        # cumulative signal
                        segment_seed[integ, frame, :, :] = previous_frame + frame_only_signal
                        previous_frame = copy.deepcopy(segment_seed[integ, frame, :, :])

                    # At the end of each integration, increment the
                    # total_frame_counter by the number of resets between
                    # integrations
                    total_frame_counter += self.numresets
                    print('RESET FRAME! ', total_frame_counter)

                # At the end of the segment/part, save the segment_seed
                # to a fits file.
                segment_number = np.where(int_end <= self.file_segment_indexes)[0][0]
                if segment_number == previous_segment:
                    segment_part_number += 1
                else:
                    segment_part_number = 1
                    previous_segment = copy.deepcopy(segment_number)

                print('Segment and part numbers: ', segment_number, segment_part_number)
                segment_file_name = '{}seg{}_part{}_seed_image.fits'.format(segment_file_base,
                                                                            str(segment_number).zfill(3),
                                                                            str(segment_part_number).zfill(3))
                save the file here. Maybe need to move seed_catalog_images saveSeedImage into utils?






    def file_splitting(self):
        """Determine file splitting details based on calculated data
        volume
        """
        frames_per_group = self.frames_per_int / self.numgroups
        self.split_seed, self.grp_segment_indexes, self.int_segment_indexes = find_file_splits(self.seed_dimensions[1],
                                                                                               self.seed_dimensions[0],
                                                                                               self.frames_per_int,
                                                                                               self.numints,
                                                                                               frames_per_group=frames_per_group)
        self.total_seed_segments = (len(group_segment_indexes) - 1) * (len(integration_segment_indexes) - 1)

        # If the file needs to be split, check to see what the splitting
        # would be in the case of groups rather than frames. This will
        # help align the split files between the seed image and the dark
        # object later (which is split by groups).
        if split_seed:
            split_seed_g, group_segment_indexes_g, self.file_segment_indexes = find_file_splits(self.seed_dimensions[1],
                                                                                                self.seed_dimensions[0],
                                                                                                self.numgroups,
                                                                                                self.numints)
        else:
            self.file_segment_indexes = np.array([0, self.numints])

    @staticmethod
    def find_transit_frames(lightcurve_collection):
        """
        """
        no_transit = []
        transit = []
        for row in range(lightcurve_collection.shape[0]):
            lc = lightcurve_collection[row, :]
            if np.all(lc == 1.0):
                no_transit.append(row)
            else:
                transit.append(row)
        return transit, no_transit

    def get_param_info(self):
        """Collect needed information out of the parameter file. Check
        parameter values for correctness

        Returns
        -------
        parameters : dict
            Nested dictionary of parameters in the input yaml file
        """
        self.catalog_files = []
        parameters = read_yaml(self.paramfile)

        cats = [parameters['simSignals'][cattype] for cattype in CATALOG_YAML_ENTRIES]
        cats = [e for e in cats if e.lower() != 'none']
        self.catalog_files.extend(cats)

        self.instrument = parameters['Inst']['instrument'].lower()
        self.aperture = parameters['Inst']['array_name']
        self.namps = parameters['Readout']['namp']
        if self.instrument == 'niriss':
            self.module = None
        elif self.instrument == 'nircam':
            self.module = parameters['Inst']['array_name'][3]
        else:
            raise ValueError("ERROR: Grism TSO mode not supported for {}".format(self.instrument))

        filter_name = parameters['Readout']['filter']
        pupil_name = parameters['Readout']['pupil']
        #dispname = ('{}_dispsersed_seed_image.fits'.format(parameters['Output']['file'].split('.fits')[0]))
        #self.default_dispersed_filename = os.path.join(parameters['Output']['directory'], dispname)

        # In reality, the grism elements are in NIRCam's pupil wheel, and NIRISS's
        # filter wheel. But in the APT xml file, NIRISS grisms are in the pupil
        # wheel and the crossing filter is listed in the filter wheel. At that
        # point, NIRISS and NIRCam are consistent, so let's keep with this reversed
        # information
        if self.instrument == 'niriss':
            self.crossing_filter = pupil_name.upper()
            self.dispersion_direction = filter_name[-1].upper()
        elif slf.instrument == 'nircam':
            self.crossing_filter = filter_name.upper()
            self.dispersion_direction = pupil_name[-1].upper()
        return parameters


    @staticmethod
    def make_lightcurves(catalog, frame_time):
        """Given a transmission spectrum, create a series of lightcurves
        using ``batman``.

        Parameters
        ----------
        catalog : astropy.table.Table
            Table containing info from the TSO source catalog

        Returns
        -------
        lightcurves : numpy.ndarray
            2D array containing the light curve at each wavelengthin the
            transmission spectrum
        """
        params = batman.TransitParams()

        # planet radius (in units of stellar radii)
        # DUMMY VALUE FOR MODEL INSTANTIATION
        params.rp = 0.1

        params.a = catalog['Semimajor_axis_in_stellar_radii']  # semi-major axis (in units of stellar radii)
        params.inc = catalog['Orbital_inclination_deg']        # orbital inclination (in degrees)
        params.ecc = catalog['Eccentricity']                   # eccentricity
        params.w = catalog['Longitude_of_periastron']          # longitude of periastron (in degrees)
        params.limb_dark = catalog['Limb_darkening_model']     # limb darkening model

        # Limb darkening coefficients [u1, u2, u3, u4]
        params.u = [np.float(e) for e in catalog['Limb_darkening_coeffs'].split(',')]

        # Get the time units from the catalog
        time_units = u.Unit(catalog['Time_units'])
        start_time = catalog['Start_time'] * time_units
        end_time = catalog['End_time'] * time_units

        # Convert times to units of seconds to make working
        # with frametimes later easier
        start_time = start_time.to(u.second).value
        end_time = end_time.to(u.second).value
        params.t0 = catalog['Inferior_conjunction'].to(u.second).value  # time of inferior conjunction
        params.per = catalog['Orbital_period'].to(u.second).value       # orbital period

        # The time resolution must be one frametime since we will need one
        # lightcurve for each frame later
        time = np.linspace(start_time, end_time, frame_time)  # times at which to calculate light curve
        model = batman.TransitModel(params, time)

        # Step along the transmission spectrum in wavelength space and
        # calculate a lightcurve at each step
        lightcurves = np.ones((len(time), len(transmissions)))
        for i, radius in enumerate(transmission):
            params.rp = radius                          # updates planet radius
            new_flux = model.light_curve(params)        # recalculates light curve
            lightcurves[:, i] = new_flux
        return lightcurves, time

    def param_checks(self):
        """Check validity of inputs
        """
        if self.orders not in [["+1"], ["+2"], ["+1", "+2"], None]:
            raise ValueError(("ERROR: Orders to be dispersed must be either None or some combination "
                              "of '+1', '+2'"))

    def run_disperser(self, direct_file, orders=["+1", "+2"], create_continuum_seds=False):
        """
        """
        # Stellar spectrum hdf5 file will be required, so no need to create one here.
        # Create hdf5 file with spectra of all sources if requested
        if create_continuum_seds:
            self.SED_file = spectra_from_catalog.make_all_spectra(self.catalog_files, input_spectra=self.SED_dict,
                                                                  input_spectra_file=self.SED_file,
                                                                  extrapolate_SED=self.extrapolate_SED,
                                                                  output_filename=self.final_SED_file,
                                                                  normalizing_mag_column=self.SED_normalizing_catalog_column)

        # Location of the configuration files needed for dispersion
        loc = os.path.join(self.datadir, "{}/GRISM_{}/".format(self.instrument,
                                                               self.instrument.upper()))

        # Determine the name of the background file to use, as well as the
        # orders to disperse.
        if self.instrument == 'nircam':
            dmode = 'mod{}_{}'.format(self.module, self.dispersion_direction)
            background_file = ("{}_{}_back.fits"
                               .format(self.crossing_filter, dmode))
        elif self.instrument == 'niriss':
            dmode = 'GR150{}'.format(self.dispersion_direction)
            background_file = "{}_{}_medium_background.fits".format(self.crossing_filter.lower(), dmode.lower())
            print('Background file is {}'.format(background_file))
        orders = self.orders

        # Create dispersed seed image from the direct images
        disp_seed = Grism_seed(direct_file, self.crossing_filter,
                               dmode, config_path=loc, instrument=self.instrument.upper(),
                               extrapolate_SED=self.extrapolate_SED, SED_file=self.SED_file,
                               SBE_save=self.source_stamps_file)
        for order in orders:
            disp_seed.this_one[order].dispere_all(cache=True)
        #disp_seed.observation(orders=orders)
        #disp_seed.finalize(Back=background_file)
        return disp_seed

    def seed_builder(self, not_in_transit, in_transit):
        """not_in_transit is a list of frame numbers

        UGH, lots of stuff would have to be passed in here, or made
        into class variables

        background_dispersed
        no_transit_signal
        lightcurves
        times
        grism_seed_object


        """
        ints_per_segment = self.int_segment_indexes[:-1] - self.int_segment_indexes[1:]
        groups_per_segment = self.grp_segment_indexes[:-1] - self.grp_segment_indexes[1:]
        print("Integration segment indexes: ", self.int_segment_indexes)
        print("ints_per_segment: ", ints_per_segment)
        print("Group segment indexes: ", self.grp_segment_indexes)
        print("groups_per_segment: ", groups_per_segment)
        total_frame_counter = 0
        previous_segment = 1
        segment_part_number = 0
        for i, int_dim in enumerate(ints_per_segment):
            int_end = self.int_segment_indexes[i+1]
            for j, grp_dim in enumerate(groups_per_segment):
                print("Integrations: {}, Groups: {}".format(int_dim, grp_dim))
                segment_seed = np.zeros((int_dim, grp_dim, self.seed_dimensions[0], self.seed_dimensions[1]))

                we need to deal with reset frames here. previous_frame signal should reset to zero,
                and no dispersion nor segment_seed population is necessary


                for integ in np.arange(int_dim):
                    previous_frame = np.zeros(self.seed_dimensions)
                    for frame in np.arange(grp_dim):
                        print('TOTAL FRAME COUNTER: ', total_frame_counter)
                        print('integ and frame: ', integ, frame)
                        # If a frame is from the part of the lightcurve
                        # with no transit, then the signal in the frame
                        # comes from no_transit_signal
                        if total_frame_counter in not_in_transit:
                            print("{} is unaltered.".format(total_frame_counter))
                            frame_only_signal = (background_dispersed.final + no_transit_signal) * self.frametime
                        # If the frame is from a part of the lightcurve
                        # where the transit is happening, then call the
                        # cached disperser with the appropriate lightcurve
                        elif total_frame_counter in in_transit:
                            print("{} is during the transit".format(total_frame_counter))
                            lightcurve = lightcurves[total_frame_counter, :]
                            lc_interp = interp1d(times, lightcurve)
                            for order in self.orders:
                                grism_seed_object.this_one[order].disperse_all_from_cache(lc_interp)
                            frame_only_signal = (background_dispersed.final + grism_seed_object.final) * self.frametime

                        # Now add the signal from this frame to that in the
                        # previous frame in order to arrive at the total
                        # cumulative signal
                        segment_seed[integ, frame, :, :] = previous_frame + frame_only_signal
                        previous_frame = copy.deepcopy(segment_seed[integ, frame, :, :])

                    # At the end of each integration, increment the
                    # total_frame_counter by the number of resets between
                    # integrations
                    total_frame_counter += self.numresets
                    print('RESET FRAME! ', total_frame_counter)

                # At the end of the segment/part, save the segment_seed
                # to a fits file.
                segment_number = np.where(int_end <= self.file_segment_indexes)[0][0]
                if segment_number == previous_segment:
                    segment_part_number += 1
                else:
                    segment_part_number = 1
                    previous_segment = copy.deepcopy(segment_number)

                print('Segment and part numbers: ', segment_number, segment_part_number)
                segment_file_name = '{}seg{}_part{}_seed_image.fits'.format(segment_file_base,
                                                                            str(segment_number).zfill(3),
                                                                            str(segment_part_number).zfill(3))
                you have to save segment_seed here rather than returning it since you are in the loop

    def split_param_file(self, params):
        """Create 2 copies of the input parameter file. One will contain
        all but the TSO source, while the other will contain only the TSO
        source.
        """
        # Read in the initial parameter file
        #params = read_yaml(self.paramfile)

        file_dir, filename = os.path.split(self.paramfile)
        suffix = filename.split('.')[-1]

        # Copy #1 - contains all source catalogs other than TSO sources
        # and be set to wfss mode.
        background_params = copy.deepcopy(params)
        background_params['simSignals']['tso_grism_catalog'] = None
        background_params['Inst']['mode'] = 'wfss'
        self.background_paramfile = self.paramfile.replace('.{}'.format(suffix),
                                                           '_background_sources.{}'.format(suffix))
        write_yaml(background_params, self.background_paramfile)

        # Copy #2 - contaings only the TSO grism source catalog,
        # is set to wfss mode, and has no background
        params['Inst']['mode'] = 'wfss'
        params['simSignals']['bkgdrate'] = 0.
        params['simSignals']['zodiacal'] = None
        params['simSignals']['scattered'] = None
        other_catalogs = ['pointsource', 'galaxyListFile', 'extended', 'tso_imaging_catalog',
                          'movingTargetList', 'movingTargetSersic', 'movingTargetExtended',
                          'movingTargetToTrack']
        for catalog in other_catalogs:
            params['simSignals'][catalog] = None

        self.tso_paramfile = self.paramfile.replace('.{}'.format(suffix),
                                                    '_tso_grism_sources.{}'.format(suffix))
        write_yaml(params, self.tso_paramfile)

    @staticmethod
    def tso_catalog_check(catalog, exp_time):
        """Check that the start and end times specified in the TSO catalog file (which are
        used to calculate the lightcurves) are long enough to encompass the entire exposure.
        If not, extend the end time to the required time.
        """
        time_unit_str = catalog['Time_units']

        # Catch common unit errors
        if time_unit_str.lower() in ['seconds', 'minutes', 'hours', 'days']:
            time_unit_str = time_unit_str[0:-1]
            catalog['Time_units'] = time_unit_str
        catalog_time_units = u.Unit(time_unit_str)
        catalog_total_time = (tso_catalog['End_time'] - tso_catalog['Start_time']) * catalog_time_units

        # Make sure the lightcurve time is at least as long as the exposure
        # time
        if exp_time > catalog_total_time:
            print(('WARNING: Lightcurve duration specified in TSO catalog file is less than '
                   'the total duration of the exposure. Adding extra time to the end of the '
                   'lightcurve to match.'))
            tso_catalog['End_time'] = tso_catalog['Start_time'] + total_exposure_time.to(catalog_time_units).value

        # Make sure the time of inferior conjunction is betwen
        # the starting and ending times
        if ((catalog['Inferior_conjunction'] < catalog['Start_time']) or
           (catalog['Inferior_conjunction'] > catalog['End_time'])):
            raise ValueError(("ERROR: the inferior conjucntion time in the TSO catalog is "
                              "outside the bounds of the starting and ending times."))
        return tso_catalog




