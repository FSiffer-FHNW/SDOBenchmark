import datetime as dt
import logging
import multiprocessing
import os
import re
import shutil
import urllib.request
from urllib.request import URLError
from typing import Tuple, List, Dict
import time
import math

import astropy.coordinates
import astropy.time
import astropy.units as u
import drms
from drms import DrmsExportError
import numpy as np
import pandas as pd
import sunpy.coordinates
import sunpy.instr.aia
import sunpy.map
import sunpy.physics.differential_rotation
from aia_lib import mov_img as aia_mov_img

from PIL import Image

from flares import util

logger = logging.getLogger(__name__)


def sample_path(sample_id: str, output_directory: str) -> str:
    ar_nr, p = sample_id.split("_",1)
    return os.path.join(output_directory, ar_nr, p)


class RequestSender(object):
    """Downloads FITS URLs for later use in the ImageDownloader"""
    SERIES_NAMES = (
        # TODO: HMI
        "aia.lev1_vis_1h",
        "aia.lev1_uv_24s",
        "aia.lev1_euv_12s"
    )

    def __init__(self, output_queue: multiprocessing.Queue, notify_email: str, cadence_hours: int):
        self._output_queue = output_queue
        self._notify_email = notify_email
        self._cadence_hours = cadence_hours

    def __call__(self, sample_input: Tuple[str, pd.Series]):
        sample_id, sample_values = sample_input
        logger.debug("Requesting data for sample %s", sample_id)

        retries = 0
        while True:
            try:
                # Perform request and provide URLs as result
                request_urls = self._perform_request(sample_id, sample_values.start, sample_values.end)
            except Exception as e:
                retries += 1
                #logger.info("Error fetching URLs for sample %s : %s", sample_id, e)
                if retries % 15 == 0:
                    logger.warning(f'Failed fetching URLs for sample %s after {retries} retries: %s', sample_id, e)
                    if isinstance(e, URLError) and isinstance(e.reason, ConnectionRefusedError):
                        logger.info('waiting for a while longer...')
                    else:
                        break
                time.sleep(0.5)
            else:
                logger.info(f'received URLs for {sample_id} after {retries} retries')
                self._output_queue.put((sample_id, request_urls))
                break

    def _perform_request(self, sample_id: str, start: dt.datetime, end: dt.datetime) -> List[str]:
        client = drms.Client(email=self._notify_email, verbose=True)
        input_hours = (end - start) // dt.timedelta(hours=1)

        # Submit requests
        requests = []
        for series_name in self.SERIES_NAMES:
            query = f"{series_name}[{start:%Y.%m.%d_%H:%M:%S_TAI}/{input_hours}h@{self._cadence_hours}h]{{image}}"
            requests.append(client.export(query, method="url_quick", protocol="as-is"))

        # Wait for all requests if they have to be processed
        urls = []
        for request in requests:
            if request.id is not None:
                # Actual request had to be made, wait for result
                logger.info("As-is data not available for sample %s, created request %s", sample_id, request.id)
                request.wait()

            if request.status != 4: # Empty set
                for _, url_row in request.urls.iterrows():
                    urls.append((url_row.record, url_row.url))

        return urls


class ImageLoader(object):
    RECORD_PARSE_REGEX = re.compile(r"^.+\[(.+)\]\[(.+)\].+$")
    RECORD_DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

    def __init__(
            self,
            input_queue: multiprocessing.Queue,
            output_queue: multiprocessing.Queue,
            output_directory: str,
            fits_directory: str
    ):
        self._input_queue = input_queue
        self._output_queue = output_queue
        self._output_directory = output_directory
        self._fits_directory = fits_directory

    def __call__(self, *args, **kwargs):
        logging.debug("Image loader started")
        while True:
            current_input = self._input_queue.get()
            logging.info(f'Remaining URL sets in queue: {self._input_queue.qsize()}')

            # Check if done
            if current_input is None:
                break

            sample_id, records = current_input
            logger.debug("Downloading images of sample %s", sample_id)

            fits_directory = sample_path(sample_id, self._fits_directory)
            try:
                # Download image
                self._download_images(fits_directory, records)

                # Enqueue next work item
                self._output_queue.put(sample_id)

            except Exception as e:
                logger.error("Error while downloading data for sample %s (is skipped): %s", sample_id, e)

                # Delete sample directory because it contains inconsistent data
                # TODO: Just delete this 1 input data, not the entire fits folder...
                #shutil.rmtree(sample_directory, ignore_errors=True)

    def _download_images(self, fits_directory: str, records: List[Tuple[str, str]]):
        fits_directory = os.path.join(fits_directory, "_fits_temp")
        os.makedirs(fits_directory, exist_ok=True)

        logger.info(f'Downloading {len(records)} FITS files into {fits_directory}...')

        for record, url in records:
            # TODO: This does not work with HMI
            record_match = self.RECORD_PARSE_REGEX.match(record)

            if record_match is None:
                raise Exception(f"Invalid record format '{record}'")

            record_date_raw, record_wavelength = record_match.groups()
            record_date = dt.datetime.strptime(record_date_raw, self.RECORD_DATE_FORMAT)

            output_file_name = f"{record_date:%Y-%m-%dT%H%M%S}_{record_wavelength}.fits"
            fp = os.path.join(fits_directory, output_file_name)
            if not os.path.isfile(fp): #TODO: Check for corruption, incomplete files
                retries = 0
                while True:
                    try:
                        urllib.request.urlretrieve(url, os.path.join(fits_directory, output_file_name))
                    except Exception as e:
                        retries += 1
                        #logger.info("Error fetching FITS %s : %s", url, e)
                        if retries % 15 == 0:
                            logger.warning(f'Failed fetching FITS %s after {retries} retries: %s', url, e)
                            if isinstance(e, URLError) and isinstance(e.reason, ConnectionRefusedError):
                                logger.info('waiting for a while longer...')
                            else:
                                break
                        time.sleep(0.5)
                    else:
                        logger.info(f'{retries} retries')
                        break
            else:
                logger.debug(f'Already found {fp}')

        logger.info("Downloaded %d files to %s", len(records), fits_directory)


class OutputProcessor(object):
    # see _FITS_to_image
    IMAGE_PARAMS = {
        "94": {
            'dataMin': 0.1,
            'dataMax': 30,
            'dataScalingType': 3 # 0 - linear, 1 - sqrt, 3 - log10
        },
        "131": {
            'dataMin': 0.7,
            'dataMax': 500,
            'dataScalingType': 3 # 0 - linear, 1 - sqrt, 3 - log10
        },
        "171": {
            'dataMin': 1,
            'dataMax': 1600,
            'dataScalingType': 3 # 0 - linear, 1 - sqrt, 3 - log10
        },
        "193": {
            'dataMin': 20,
            'dataMax': 2500,
            'dataScalingType': 3 # 0 - linear, 1 - sqrt, 3 - log10
        },
        "211": {
            'dataMin': 7,
            'dataMax': 1500,
            'dataScalingType': 3 # 0 - linear, 1 - sqrt, 3 - log10
        },
        "304": {
            'dataMin': 0.8,
            'dataMax': 250,
            'dataScalingType': 3 # 0 - linear, 1 - sqrt, 3 - log10
        },
        "335": {
            'dataMin': 0.4,
            'dataMax': 80,
            'dataScalingType': 3 # 0 - linear, 1 - sqrt, 3 - log10
        },
        "1600": {
            'dataMin': 10,
            'dataMax': 400,
            'dataScalingType': 3 # 0 - linear, 1 - sqrt, 3 - log10
        },
        "1700": {
            'dataMin': 220,
            'dataMax': 5000,
            'dataScalingType': 3 # 0 - linear, 1 - sqrt, 3 - log10
        },
        "4500": {
            'dataMin': 4000,
            'dataMax': 20000,
            'dataScalingType': 3 # 0 - linear, 1 - sqrt, 3 - log10
        }
    }



    OUTPUT_SHAPE = (512, 512)

    def __init__(
            self,
            input_queue: multiprocessing.Queue,
            output_directory: str,
            fits_directory: str,
            meta_data: pd.DataFrame,
            noaa_regions: Dict[int, Tuple[dt.datetime, dt.datetime, List[dict]]],
            cadence_hours: int
    ):
        self._input_queue = input_queue
        self._output_directory = output_directory
        self._fits_directory = fits_directory
        self._meta_data = meta_data
        self._noaa_regions = noaa_regions
        self._cadence_hours = cadence_hours

    def __call__(self, *args, **kwargs):
        logging.debug("Output processor started")
        while True:
            sample_id = self._input_queue.get()
            logging.info(f'Remaining URL sets in queue: {self._input_queue.qsize()}')

            # Check if done
            if sample_id is None:
                break

            logger.debug("Processing sample %s", sample_id)

            sample_directory = sample_path(sample_id, self._output_directory)
            fits_directory = os.path.join(sample_path(sample_id, self._fits_directory), "_fits_temp")

            try:
                # Process output
                self._process_output(sample_id, fits_directory, sample_directory)
            except Exception as e:
                logger.error("Error while processing data for sample %s (is skipped): %s", sample_id, e)

                # Delete sample directory because it contains inconsistent data
                #TODO:shutil.rmtree(sample_directory, ignore_errors=True)
            finally:
                # Delete fits directory in any case to avoid space issues
                #TODO:shutil.rmtree(fits_directory, ignore_errors=True)
                print(f'Would have deleted FITS files {fits_directory}')

    def _process_output(self, sample_id: str, input_directory: str, output_directory: str):

        # 1. Create a time line by time steps, each (available) wavelength
        # 2. For each time step
        # 3.    For each wavelength
        # 4.        Check if image is usable (in FITS header)
        # 5.        Convert to level 1.5 data (for AIA)
        # 6.        Rotate active region position to image time
        # 7.        Cut out part of image
        # 8.    Save all cuts into numpy array

        sample_meta_data = self._meta_data.loc[sample_id]
        _, _, region_events = self._noaa_regions[sample_meta_data.noaa_num]

        # Create a list of available times per wavelength
        # TODO: This ignores HMI
        available_times = {wavelength: [] for wavelength in self.IMAGE_PARAMS.keys()}
        for current_file in os.listdir(input_directory):
            current_datetime_raw, current_wavelength = os.path.splitext(current_file)[0].split("_")
            current_datetime = dt.datetime.strptime(current_datetime_raw, "%Y-%m-%dT%H%M%S")
            available_times[current_wavelength].append((current_datetime, current_file))

        # Assign images to actual time steps
        num_outputs = (sample_meta_data.end - sample_meta_data.start) // dt.timedelta(hours=self._cadence_hours)
        time_steps = [(sample_meta_data.start + dt.timedelta(hours=offset), dict()) for offset in range(num_outputs)]
        for current_wavelength, current_available_times in available_times.items():
            if len(current_available_times) == num_outputs:
                # Data for full duration available
                for idx, (_, current_file) in enumerate(sorted(current_available_times)):
                    time_steps[idx][1][current_wavelength] = current_file
            else:
                # Use closest time step to each image
                # TODO: Could actually check record interval for missing values
                for current_datetime, current_file in current_available_times:
                    _, current_step_images = min(time_steps, key=lambda step: abs(step[0] - current_datetime))
                    assert current_wavelength not in current_step_images
                    current_step_images[current_wavelength] = current_file

        # Process each time step
        for current_datetime, current_images in time_steps:
            output_arrays = dict()

            # Process each wavelength
            for current_wavelength, current_file in current_images.items():
                try:
                    current_map: sunpy.map.sources.AIAMap = sunpy.map.Map(os.path.join(input_directory, current_file))
                except Exception as e:
                    logger.warning(f"Unable to load file {os.path.join(input_directory, current_file)}, skipping...")
                    continue
                #TODO: Cannot open resource

                # Check if map is usable
                if not self._is_usable(current_map):
                    logger.warning("Discarding wavelength %s for sample %s", current_wavelength, sample_id)
                    continue

                # Convert to level 1.5
                if current_map.processing_level != 1.5:
                    current_map = sunpy.instr.aia.aiaprep(current_map)

                # Find coordinates of closest active region event which started before the image
                image_time = current_map.date
                closest_region_event = max(
                    (event for event in region_events if event["starttime"] <= image_time),
                    key=lambda event: event["starttime"]
                )
                region_position = astropy.coordinates.SkyCoord(
                    float(closest_region_event["hpc_x"]) * u.arcsec,
                    float(closest_region_event["hpc_y"]) * u.arcsec,
                    frame="helioprojective",
                    obstime=closest_region_event["starttime"]
                )
                region_position_rotated = sunpy.physics.differential_rotation.solar_rotate_coordinate(
                    region_position,
                    image_time
                )

                # Transform target position to pixels, in carthesion coordinates (origin bottom left)
                center_x, center_y = current_map.world_to_pixel(region_position_rotated)
                center_x, center_y = int(center_x.to_value()), int(center_y.to_value())
                assert center_x - self.OUTPUT_SHAPE[1] / 2 >= 0
                assert center_y - self.OUTPUT_SHAPE[0] / 2 >= 0
                assert center_x + self.OUTPUT_SHAPE[1] / 2 < current_map.data.shape[1]
                assert center_y + self.OUTPUT_SHAPE[0] / 2 < current_map.data.shape[0]

                # Cut patch out of image
                # Sunpy maps assume a carthesian coordinate system which is already incorporated in the pixel conversion
                patch_start_y = center_y - self.OUTPUT_SHAPE[0] // 2
                patch_start_x = center_x - self.OUTPUT_SHAPE[1] // 2
                img = current_map.data[
                    patch_start_y:patch_start_y + self.OUTPUT_SHAPE[0],
                    patch_start_x:patch_start_x + self.OUTPUT_SHAPE[1],
                ]
                assert img.shape == self.OUTPUT_SHAPE

                # Image processing steps
                img = self._FITS_to_image(img, current_map)

                # Save patch
                #output_arrays[current_wavelength] = current_patch

                # what happens when converting to other formats?
                patch_16bitint = (np.round(img * 32767)).astype(np.int16)
                #logger.info(f'Total diff int16={np.sum(np.abs(patch_16bitint - img))}')
                #logger.info(f'Max diff int16={np.amax(np.abs(patch_16bitint - img))}')

                # Save as image
                output_file_path = os.path.join(output_directory, current_datetime.strftime("%Y-%m-%dT%H%M%S") + "__" + str(current_wavelength))
                im = Image.fromarray(patch_16bitint)
                im.save(output_file_path, "PNG")

            # Save patches as compressed numpy file
            #output_file_path = os.path.join(output_directory, current_datetime.strftime("%Y-%m-%dT%H%M%S"))
            #np.savez_compressed(output_file_path, **output_arrays)

        logger.info("Created sample %s output", sample_id)

    @classmethod
    def _is_usable(cls, target: sunpy.map.sources.AIAMap) -> bool:
        # Check header values and quality flags to be mostly sure the image is usable
        # TODO: Are those checks enough? Are there better methods to check for faulty images?
        return \
            target.meta["ACS_MODE"] == "SCIENCE" \
            and target.meta["ACS_ECLP"] != "YES" \
            and target.meta["ACS_SUNP"] == "YES" \
            and target.meta["QUALITY"] & (1 << 18) == 0  # Calibration flag

    def _FITS_to_image(self, img: np.ndarray, current_map: sunpy.map.sources.AIAMap):
        'Returns 2d array in [0,1] range'
        # Templates:
        # http://www.heliodocs.com/php/xdoc_print.php?file=$SSW/sdo/aia/idl/pubrel/aia_intscale.pro
        # https://github.com/Helioviewer-Project/jp2gen/blob/master/idl/sdo/aia/hv_aia_list2jp2_gs2.pro
        # Actually, decided to go with own visualization.
        # TODO Recalculate the FITS header CRPIX values if need be
        img = np.flipud(img)
        img = img / current_map.meta["EXPTIME"] #  normalize for exposure
        wavelength = str(current_map.meta["wavelnth"])
        pms = self.IMAGE_PARAMS[wavelength]
        '''if wavelength == '171':
            img = img - 5
            img[img < 0.1] = 0.1
            img = np.clip(np.sqrt(img),1,40)
        else:'''
        img = np.clip(img, pms['dataMin'], pms['dataMax'])
        if pms['dataScalingType'] == 1:
            img = np.sqrt(img)
            # normalize to [0,1]
            img = (img - math.sqrt(pms['dataMin'])) / math.sqrt(pms['dataMax'] - pms['dataMin'])
        elif pms['dataScalingType'] == 3:
            img = np.log10(img)
            # normalize to [0,1]
            img = (img - math.log10(pms['dataMin'])) / math.log10(pms['dataMax'] - pms['dataMin'])

        return img
