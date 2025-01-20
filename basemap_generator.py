import os
import subprocess
from tqdm import tqdm
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import json
import time
from datetime import datetime

class ProcessTracker:
    def __init__(self, output_dir):
        self.progress_file = os.path.join(output_dir, 'download_progress.json')
        self.completed_urls = self.load_progress()

    def load_progress(self):
        if os.path.exists(self.progress_file):
            with open(self.progress_file, 'r') as f:
                return set(json.load(f))
        return set()

    def save_progress(self):
        with open(self.progress_file, 'w') as f:
            json.dump(list(self.completed_urls), f)

    def mark_completed(self, url):
        self.completed_urls.add(url)
        self.save_progress()

    def is_completed(self, url):
        return url in self.completed_urls

def create_retry_session(retries=3, backoff_factor=0.3, timeout=300):
    session = requests.Session()
    retry = Retry(total=retries, backoff_factor=backoff_factor, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.timeout = timeout
    return session

def download_tif(url, output_path, max_retries=3):
    session = create_retry_session()
    for attempt in range(max_retries):
        try:
            response = session.get(url, stream=True)
            response.raise_for_status()
            total_size = int(response.headers.get('content-length', 0))
            
            with open(output_path, 'wb') as file, tqdm(
                desc=output_path,
                total=total_size,
                unit='iB',
                unit_scale=True,
                unit_divisor=1024,
            ) as progress_bar:
                for data in response.iter_content(chunk_size=1024):
                    size = file.write(data)
                    progress_bar.update(size)
            return True
        except requests.RequestException as e:
            print(f"Download attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 5
                print(f"Waiting {wait_time} seconds before retry...")
                time.sleep(wait_time)
            else:
                print("Max retries reached. Moving to next file.")
                return False

def get_tif_urls():
    """Get direct NAIP TIF URLs for a specific area in Kentucky"""
    stac_search_url = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
    
    kentucky_polygon = {
        "type": "Polygon",
        "coordinates": [[
            [-85.76975230223191, 37.63831975175371],
            [-85.79792299732526, 37.556622960281146],
            [-85.77917808348003, 37.558211386666116],
            [-85.7731342906098, 37.58369784583759],
            [-85.76039471747762, 37.61128549203865],
            [-85.74833151247218, 37.63142760638745],
            [-85.51777647369674, 37.62988516059704],
            [-85.51709126957944, 37.64263083576242],
            [-85.64388882701948, 37.64311140194164],
            [-85.76975230223191, 37.63831975175371]
        ]]
    }

    all_urls = []
    latest_year = None
    session = create_retry_session()

    search_params = {
        "collections": ["naip"],
        "intersects": kentucky_polygon,
        "limit": 100,
        "query": {
            "datetime": {"gte": "2018-01-01"},
            #"naip:state": {"eq": "KY"}
        }
    }

    try:
        response = session.post(stac_search_url, json=search_params)
        response.raise_for_status()
        
        print(f"API Response Status Code: {response.status_code}")
        print(f"API Response Content: {response.text[:500]}...")  # Print first 500 characters
        
        features = response.json().get('features', [])
        print(f"Number of features found: {len(features)}")
        
        if features:
            sorted_features = sorted(features, key=lambda x: x['properties']['datetime'], reverse=True)

            if latest_year is None:
                latest_year = datetime.strptime(sorted_features[0]['properties']['datetime'], '%Y-%m-%dT%H:%M:%SZ').year
            
            print(f"Latest year: {latest_year}")

            region_features = [f for f in sorted_features 
                           if datetime.strptime(f['properties']['datetime'], '%Y-%m-%dT%H:%M:%SZ').year == latest_year]

            print(f"Number of features for the latest year: {len(region_features)}")

            for feature in region_features:
                url = feature['assets']['image']['href']
                if url not in all_urls:
                    all_urls.append(url)
                    
        print(f"Number of unique URLs found: {len(all_urls)}")
                    
    except requests.exceptions.RequestException as e:
        print(f"Error fetching data: {e}")

    return all_urls

def process_tifs(tif_urls, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    tracker = ProcessTracker(output_dir)
    input_files = []

    for i, url in enumerate(tif_urls, 1):
        if tracker.is_completed(url):
            print(f"Skipping already completed image {i} of {len(tif_urls)}")
            continue

        print(f"Processing image {i} of {len(tif_urls)}")
        tif_path = os.path.join(output_dir, f"input_{i}.tif")
        if download_tif(url, tif_path):
            input_files.append(tif_path)
            tracker.mark_completed(url)

    if not input_files:
        print("No new files to process.")
        return

    with open('input_files.txt', 'w') as f:
        f.write('\n'.join(input_files))

    merged_tif = os.path.join(output_dir, "final_merge.tif")
    subprocess.run([
        'gdalwarp', '-overwrite', '-r', 'lanczos',
        '-co', 'COMPRESS=LZW',
        '-co', 'TILED=YES',
        '-co', 'BLOCKXSIZE=256',
        '-co', 'BLOCKYSIZE=256',
        '-co', 'PREDICTOR=2',
        '-co', 'BIGTIFF=YES',
        '-t_srs', 'EPSG:3857',
        '-tr', '0.3', '0.3',
        '-tap',
        '-multi',
        '-wo', 'NUM_THREADS=ALL_CPUS',
        '-of', 'GTiff',
        '-dstnodata', '0',
        '-srcnodata', '0',
        '-input_file_list', 'input_files.txt',
        merged_tif
    ])

    subprocess.run([
        'gdaladdo', '-r', 'average', '-ro',
        '--config', 'COMPRESS_OVERVIEW', 'LZW',
        '--config', 'PREDICTOR_OVERVIEW', '2',
        merged_tif,
        '2', '4', '8', '16', '32', '64', '128'
    ])

    final_mbtiles = os.path.join(output_dir, "final_merge_raster.mbtiles")
    subprocess.run([
        'gdal_translate', '-of', 'MBTILES',
        '-co', 'TILE_FORMAT=JPG',
        '-co', 'QUALITY=95',
        '-co', 'ZOOM_LEVEL_STRATEGY=LOWER',
        '-co', 'RESAMPLING=CUBIC',
        '-co', 'COMPRESS=LZW',
        '-co', 'MINZOOM=1',
        '-co', 'MAXZOOM=16',
        merged_tif, final_mbtiles
    ])

    subprocess.run([
        'gdaladdo', '-r', 'cubic',
        '--config', 'COMPRESS_OVERVIEW', 'JPEG',
        '--config', 'JPEG_QUALITY_OVERVIEW', '95',
        final_mbtiles,
        '2', '4', '8', '16', '32', '64', '128', '256', '512', '1024', '2048', '4096', '8192', '16384', '32768'
    ])

    print(f"Process complete. Output files in {output_dir}")

def main():
    output_dir = os.environ.get('OUTPUT_DIR', './output2')
    tif_urls = get_tif_urls()
    if not tif_urls:
        print("No NAIP images found.")
        return
    process_tifs(tif_urls, output_dir)

if __name__ == "__main__":
    main()