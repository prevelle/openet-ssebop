#--------------------------------
# Name:         tcorr_export_daily_image.py
# Purpose:      Compute/Export daily Tcorr images
#--------------------------------

import argparse
from builtins import input
import datetime
import logging
import math
import os
import pprint
import sys

import ee

import openet.ssebop as ssebop
import utils
# from . import utils


def main(ini_path=None, overwrite_flag=False, delay=0, key=None,
         cron_flag=False, reverse_flag=False):
    """Compute daily Tcorr images

    Parameters
    ----------
    ini_path : str
        Input file path.
    overwrite_flag : bool, optional
        If True, overwrite existing files if the export dates are the same and
        generate new images (but with different export dates) even if the tile
        lists are the same.  The default is False.
    delay : float, optional
        Delay time between each export task (the default is 0).
    key : str, optional
        File path to an Earth Engine json key file (the default is None).
    cron_flag : bool, optional
        If True, only compute Tcorr daily image if existing image does not have
        all available image (using the 'wrs2_tiles' property) and limit the
        date range to the last 64 days (~2 months).
    reverse_flag : bool, optional
        If True, process dates in reverse order.
    """
    logging.info('\nCompute daily Tcorr images')

    ini = utils.read_ini(ini_path)

    model_name = 'SSEBOP'
    # model_name = ini['INPUTS']['et_model'].upper()

    if (ini[model_name]['tmax_source'].upper() == 'CIMIS' and
            ini['INPUTS']['end_date'] < '2003-10-01'):
        logging.error(
            '\nCIMIS is not currently available before 2003-10-01, exiting\n')
        sys.exit()
    elif (ini[model_name]['tmax_source'].upper() == 'DAYMET' and
            ini['INPUTS']['end_date'] > '2017-12-31'):
        logging.warning(
            '\nDAYMET is not currently available past 2017-12-31, '
            'using median Tmax values\n')
        # sys.exit()
    # elif (ini[model_name]['tmax_source'].upper() == 'TOPOWX' and
    #         ini['INPUTS']['end_date'] > '2017-12-31'):
    #     logging.warning(
    #         '\nDAYMET is not currently available past 2017-12-31, '
    #         'using median Tmax values\n')
    #     # sys.exit()

    logging.info('\nInitializing Earth Engine')
    if key:
        logging.info('  Using service account key file: {}'.format(key))
        # The "EE_ACCOUNT" parameter is not used if the key file is valid
        ee.Initialize(ee.ServiceAccountCredentials('deadbeef', key_file=key))
    else:
        ee.Initialize()

    # Output Tcorr daily image collection
    tcorr_daily_coll_id = '{}/{}_daily'.format(
        ini['EXPORT']['export_coll'], ini[model_name]['tmax_source'].lower())

    # Get a Tmax image to set the Tcorr values to
    logging.debug('\nTmax properties')
    tmax_name = ini[model_name]['tmax_source']
    tmax_source = tmax_name.split('_', 1)[0]
    tmax_version = tmax_name.split('_', 1)[1]
    tmax_coll_id = 'projects/usgs-ssebop/tmax/{}'.format(tmax_name.lower())
    tmax_coll = ee.ImageCollection(tmax_coll_id)
    tmax_mask = ee.Image(tmax_coll.first()).select([0]).multiply(0)
    logging.debug('  Collection: {}'.format(tmax_coll_id))
    logging.debug('  Source: {}'.format(tmax_source))
    logging.debug('  Version: {}'.format(tmax_version))

    logging.debug('\nExport properties')
    export_geo = ee.Image(tmax_mask).projection().getInfo()['transform']
    export_crs = ee.Image(tmax_mask).projection().getInfo()['crs']
    export_shape = ee.Image(tmax_mask).getInfo()['bands'][0]['dimensions']
    export_extent = [
        export_geo[2], export_geo[5] + export_shape[1] * export_geo[4],
        export_geo[2] + export_shape[0] * export_geo[0], export_geo[5]]
    logging.debug('  CRS: {}'.format(export_crs))
    logging.debug('  Extent: {}'.format(export_extent))
    logging.debug('  Geo: {}'.format(export_geo))
    logging.debug('  Shape: {}'.format(export_shape))

    # # Limit export to a user defined study area or geometry?
    # export_geom = ee.Geometry.Rectangle(
    #     [-125, 24, -65, 50], proj='EPSG:4326', geodesic=False)  # CONUS
    # export_geom = ee.Geometry.Rectangle(
    #     [-124, 35, -119, 42], proj='EPSG:4326', geodesic=False)  # California

    # If cell_size parameter is set in the INI,
    # adjust the output cellsize and recompute the transform and shape
    try:
        export_cs = float(ini['EXPORT']['cell_size'])
        export_shape = [
            int(math.ceil(abs((export_shape[0] * export_geo[0]) / export_cs))),
            int(math.ceil(abs((export_shape[1] * export_geo[4]) / export_cs)))]
        export_geo = [export_cs, 0.0, export_geo[2], 0.0, -export_cs, export_geo[5]]
        logging.debug('  Custom export cell size: {}'.format(export_cs))
        logging.debug('  Geo: {}'.format(export_geo))
        logging.debug('  Shape: {}'.format(export_shape))
    except KeyError:
        pass

    # Get current asset list
    if ini['EXPORT']['export_dest'].upper() == 'ASSET':
        logging.debug('\nGetting asset list')
        # DEADBEEF - daily is hardcoded in the asset_id for now
        asset_list = utils.get_ee_assets(tcorr_daily_coll_id)
    else:
        raise ValueError('invalid export destination: {}'.format(
            ini['EXPORT']['export_dest']))

    # Get current running tasks
    tasks = utils.get_ee_tasks()
    if logging.getLogger().getEffectiveLevel() == logging.DEBUG:
        logging.debug('  Tasks: {}\n'.format(len(tasks)))
        input('ENTER')

    collections = [x.strip() for x in ini['INPUTS']['collections'].split(',')]

    # Limit by year and month
    try:
        month_list = sorted(list(utils.parse_int_set(ini['TCORR']['months'])))
    except:
        logging.info('\nTCORR "months" parameter not set in the INI,'
                     '\n  Defaulting to all months (1-12)\n')
        month_list = list(range(1, 13))
    try:
        year_list = sorted(list(utils.parse_int_set(ini['TCORR']['years'])))
    except:
        logging.info('\nTCORR "years" parameter not set in the INI,'
                     '\n  Defaulting to all available years\n')
        year_list = []

    # Key is cycle day, value is a reference date on that cycle
    # Data from: https://landsat.usgs.gov/landsat_acq
    # I only need to use 8 cycle days because of 5/7 and 7/8 are offset
    cycle_dates = {
        7: '1970-01-01',
        8: '1970-01-02',
        1: '1970-01-03',
        2: '1970-01-04',
        3: '1970-01-05',
        4: '1970-01-06',
        5: '1970-01-07',
        6: '1970-01-08',
    }
    # cycle_dates = {
    #     1:  '2000-01-06',
    #     2:  '2000-01-07',
    #     3:  '2000-01-08',
    #     4:  '2000-01-09',
    #     5:  '2000-01-10',
    #     6:  '2000-01-11',
    #     7:  '2000-01-12',
    #     8:  '2000-01-13',
    #     # 9:  '2000-01-14',
    #     # 10: '2000-01-15',
    #     # 11: '2000-01-16',
    #     # 12: '2000-01-01',
    #     # 13: '2000-01-02',
    #     # 14: '2000-01-03',
    #     # 15: '2000-01-04',
    #     # 16: '2000-01-05',
    # }
    cycle_base_dt = datetime.datetime.strptime(cycle_dates[1], '%Y-%m-%d')

    if cron_flag:
        # CGM - This seems like a silly way of getting the date as a datetime
        #   Why am I doing this and not using the commented out line?
        iter_end_dt = datetime.date.today().strftime('%Y-%m-%d')
        iter_end_dt = datetime.datetime.strptime(iter_end_dt, '%Y-%m-%d')
        iter_end_dt = iter_end_dt + datetime.timedelta(days=-1)
        # iter_end_dt = datetime.datetime.today() + datetime.timedelta(days=-1)
        iter_start_dt = iter_end_dt + datetime.timedelta(days=-64)
    else:
        iter_start_dt = datetime.datetime.strptime(
            ini['INPUTS']['start_date'], '%Y-%m-%d')
        iter_end_dt = datetime.datetime.strptime(
            ini['INPUTS']['end_date'], '%Y-%m-%d')
    logging.debug('Start Date: {}'.format(iter_start_dt.strftime('%Y-%m-%d')))
    logging.debug('End Date:   {}\n'.format(iter_end_dt.strftime('%Y-%m-%d')))


    for export_dt in sorted(utils.date_range(iter_start_dt, iter_end_dt),
                            reverse=reverse_flag):
        export_date = export_dt.strftime('%Y-%m-%d')
        next_date = (export_dt + datetime.timedelta(days=1)).strftime('%Y-%m-%d')
        # if ((month_list and export_dt.month not in month_list) or
        #         (year_list and export_dt.year not in year_list)):
        if month_list and export_dt.month not in month_list:
            logging.debug(f'Date: {export_date} - month not in INI - skipping')
            continue
        elif export_date >= datetime.datetime.today().strftime('%Y-%m-%d'):
            logging.debug(f'Date: {export_date} - unsupported date - skipping')
            continue
        elif export_date < '1984-03-23':
            logging.debug(f'Date: {export_date} - no Landsat 5+ images before '
                         '1984-03-16 - skipping')
            continue
        logging.info(f'Date: {export_date}')

        export_id = ini['EXPORT']['export_id_fmt'] \
            .format(
                product=tmax_name.lower(),
                date=export_dt.strftime('%Y%m%d'),
                export=datetime.datetime.today().strftime('%Y%m%d'),
                dest=ini['EXPORT']['export_dest'].lower())
        logging.debug('  Export ID: {}'.format(export_id))

        if ini['EXPORT']['export_dest'] == 'ASSET':
            asset_id = '{}/{}_{}'.format(
                tcorr_daily_coll_id, export_dt.strftime('%Y%m%d'),
                datetime.datetime.today().strftime('%Y%m%d'))
            logging.debug('  Asset ID: {}'.format(asset_id))

        if overwrite_flag:
            if export_id in tasks.keys():
                logging.debug('  Task already submitted, cancelling')
                ee.data.cancelTask(tasks[export_id])
            # This is intentionally not an "elif" so that a task can be
            # cancelled and an existing image/file/asset can be removed
            if (ini['EXPORT']['export_dest'].upper() == 'ASSET' and
                    asset_id in asset_list):
                logging.debug('  Asset already exists, removing')
                ee.data.deleteAsset(asset_id)
        else:
            if export_id in tasks.keys():
                logging.debug('  Task already submitted, exiting')
                continue
            elif (ini['EXPORT']['export_dest'].upper() == 'ASSET' and
                    asset_id in asset_list):
                logging.debug('  Asset already exists, skipping')
                continue

        # Build and merge the Landsat collections
        model_obj = ssebop.Collection(
            collections=collections,
            start_date=export_dt.strftime('%Y-%m-%d'),
            end_date=(export_dt + datetime.timedelta(days=1)).strftime(
                '%Y-%m-%d'),
            cloud_cover_max=float(ini['INPUTS']['cloud_cover']),
            geometry=tmax_mask.geometry(),
            # model_args=model_args,
            # filter_args=filter_args,
        )
        landsat_coll = model_obj.overpass(variables=['ndvi'])
        # wrs2_tiles_all = model_obj.get_image_ids()
        # pprint.pprint(landsat_coll.aggregate_array('system:id').getInfo())
        # input('ENTER')

        logging.debug('  Getting available WRS2 tile list')
        landsat_id_list = landsat_coll.aggregate_array('system:id').getInfo()
        wrs2_tiles_all = set([id.split('_')[-2] for id in landsat_id_list])
        if not wrs2_tiles_all:
            logging.info('  No available images - skipping')
            continue

        # If overwriting, start a new export no matter what
        # The default is to no overwrite, so this mode will not be used often
        if not overwrite_flag:
            # Check if there are any previous images for this date
            # If so, only build a new Tcorr image if there are new wrs2_tiles
            #   that were not used in the previous image.
            # Should this code only be run in cron mode or is this the expected
            #   operation when (re)running for any date range?
            # Should we only test the last image
            # or all previous images for the date?
            logging.debug('  Checking for previous exports/versions of daily image')
            tcorr_daily_coll = ee.ImageCollection(tcorr_daily_coll_id)\
                .filterDate(export_date, next_date)\
                .limit(1, 'date_ingested', False)
            tcorr_daily_info = tcorr_daily_coll.getInfo()

            if tcorr_daily_info['features']:
                # Assume we won't be building a new image and only set flag
                #   to True if the WRS2 tile lists are different
                export_flag = False

                # The ".limit(1, ..." on the tcorr_daily_coll above makes this
                # for loop and break statement unnecessary, but leaving for now
                for tcorr_img in tcorr_daily_info['features']:
                    # If the full WRS2 list is not present, rebuild the image
                    # This should only happen for much older Tcorr images
                    if 'wrs2_available' not in tcorr_img['properties'].keys():
                        logging.debug(
                            '    "wrs2_available" property not present in '
                            'previous export')
                        export_flag = True
                        break

                    wrs2_tiles_old = set(
                        tcorr_img['properties']['wrs2_available'].split(','))

                    if wrs2_tiles_all != wrs2_tiles_old:
                        logging.debug('  Tile Lists')
                        logging.debug('  Previous: {}'.format(
                            ', '.join(sorted(wrs2_tiles_old))))
                        logging.debug('  Available: {}'.format(
                            ', '.join(sorted(wrs2_tiles_all))))
                        logging.debug('  New: {}'.format(
                            ', '.join(sorted(wrs2_tiles_all.difference(wrs2_tiles_old)))))
                        logging.debug('  Dropped: {}'.format(
                            ', '.join(sorted(wrs2_tiles_old.difference(wrs2_tiles_all)))))

                        export_flag = True
                        break

                if not export_flag:
                    logging.debug('  No new WRS2 tiles/images - skipping')
                    continue
                # else:
                #     logging.debug('    Building new version')
            else:
                logging.debug('    No previous exports')

        def tcorr_img_func(image):
            t_stats = ssebop.Image.from_landsat_c1_toa(
                    ee.Image(image),
                    tdiff_threshold=float(ini[model_name]['tdiff_threshold'])) \
                .tcorr_stats
            t_stats = ee.Dictionary(t_stats) \
                .combine({'tcorr_p5': 0, 'tcorr_count': 0},
                         overwrite=False)
            tcorr = ee.Number(t_stats.get('tcorr_p5'))
            count = ee.Number(t_stats.get('tcorr_count'))

            # Remove the merged collection indices from the system:index
            scene_id = ee.List(
                ee.String(image.get('system:index')).split('_')).slice(-3)
            scene_id = ee.String(scene_id.get(0)).cat('_') \
                .cat(ee.String(scene_id.get(1))).cat('_') \
                .cat(ee.String(scene_id.get(2)))

            return tmax_mask.add(tcorr) \
                .rename(['tcorr']) \
                .clip(image.geometry()) \
                .set({
                    'system:time_start': image.get('system:time_start'),
                    'scene_id': scene_id,
                    'wrs2_tile': scene_id.slice(5, 11),
                    'spacecraft_id': image.get('SPACECRAFT_ID'),
                    'tcorr': tcorr,
                    'count': count,
                })
        # Test for one image
        # pprint.pprint(tcorr_img_func(ee.Image(landsat_coll \
        #     .filterMetadata('WRS_PATH', 'equals', 36) \
        #     .filterMetadata('WRS_ROW', 'equals', 33).first())).getInfo())
        # input('ENTER')

        # (Re)build the Landsat collection from the image IDs
        landsat_coll = ee.ImageCollection(landsat_id_list)
        tcorr_img_coll = ee.ImageCollection(landsat_coll.map(tcorr_img_func)) \
            .filterMetadata('count', 'not_less_than',
                            float(ini['TCORR']['min_pixel_count']))

        # If there are no Tcorr values, return an empty image
        tcorr_img = ee.Algorithms.If(
            tcorr_img_coll.size().gt(0),
            tcorr_img_coll.median(),
            tmax_mask.updateMask(0))

        def unique_properties(coll, property):
            return ee.String(ee.List(ee.Dictionary(
                coll.aggregate_histogram(property)).keys()).join(','))
        wrs2_tile_list = ee.String('').cat(unique_properties(
            tcorr_img_coll, 'wrs2_tile'))
        landsat_list = ee.String('').cat(unique_properties(
            tcorr_img_coll, 'spacecraft_id'))

        # Cast to float and set properties
        tcorr_img = ee.Image(tcorr_img).rename(['tcorr']).double() \
            .set({
                'system:time_start': utils.millis(export_dt),
                'date_ingested': datetime.datetime.today().strftime('%Y-%m-%d'),
                'date': export_dt.strftime('%Y-%m-%d'),
                'year': int(export_dt.year),
                'month': int(export_dt.month),
                'day': int(export_dt.day),
                'doy': int(export_dt.strftime('%j')),
                'cycle_day': ((export_dt - cycle_base_dt).days % 8) + 1,
                'landsat': landsat_list,
                'model_name': model_name,
                'model_version': ssebop.__version__,
                'tmax_source': tmax_source.upper(),
                'tmax_version': tmax_version.upper(),
                'wrs2_tiles': wrs2_tile_list,
                'wrs2_available': ','.join(sorted(wrs2_tiles_all)),
            })

        # Build export tasks
        if ini['EXPORT']['export_dest'] == 'ASSET':
            logging.debug('  Building export task')
            task = ee.batch.Export.image.toAsset(
                image=ee.Image(tcorr_img),
                description=export_id,
                assetId=asset_id,
                crs=export_crs,
                crsTransform='[' + ','.join(list(map(str, export_geo))) + ']',
                dimensions='{0}x{1}'.format(*export_shape),
            )
            logging.info('  Starting export task')
            utils.ee_task_start(task)

        # Pause before starting next task
        utils.delay_task(delay)
        logging.debug('')


def arg_parse():
    """"""
    parser = argparse.ArgumentParser(
        description='Compute/export daily Tcorr images',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '-i', '--ini', type=utils.arg_valid_file,
        help='Input file', metavar='FILE')
    parser.add_argument(
        '--delay', default=0, type=float,
        help='Delay (in seconds) between each export tasks')
    parser.add_argument(
        '--key', type=utils.arg_valid_file, metavar='FILE',
        help='JSON key file')
    parser.add_argument(
        '--cron', default=False, action='store_true',
        help='Cron mode')
    parser.add_argument(
        '--reverse', default=False, action='store_true',
        help='Process dates in reverse order')
    parser.add_argument(
        '-o', '--overwrite', default=False, action='store_true',
        help='Force overwrite of existing files')
    parser.add_argument(
        '-d', '--debug', default=logging.INFO, const=logging.DEBUG,
        help='Debug level logging', action='store_const', dest='loglevel')
    args = parser.parse_args()

    # Prompt user to select an INI file if not set at command line
    # if not args.ini:
    #     args.ini = utils.get_ini_path(os.getcwd())

    return args


if __name__ == "__main__":
    args = arg_parse()

    logging.basicConfig(level=args.loglevel, format='%(message)s')
    logging.info('\n{0}'.format('#' * 80))
    logging.info('{0:<20s} {1}'.format(
        'Run Time Stamp:', datetime.datetime.now().isoformat(' ')))
    logging.info('{0:<20s} {1}'.format('Current Directory:', os.getcwd()))
    logging.info('{0:<20s} {1}'.format(
        'Script:', os.path.basename(sys.argv[0])))

    main(ini_path=args.ini, overwrite_flag=args.overwrite, delay=args.delay,
         key=args.key, cron_flag=args.cron, reverse_flag=args.reverse)
