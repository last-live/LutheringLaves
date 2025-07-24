import os
import sys
import shutil
import hashlib
import json
import gzip
import io
from pathlib import Path
from urllib.request import urlopen, Request, HTTPError
from urllib.parse import urljoin,quote
import argparse

launcher_api = 'https://prod-cn-alicdn-gamestarter.kurogame.com/launcher/game/G152/10003_Y8xXrXk65DqFHEDgApn3cpK5lfczpFx5/index.json'
def get_result(url):
    try:
        req = Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Accept-Encoding': 'gzip'
        })
        with urlopen(req, timeout=10) as rsp:
            if rsp.status != 200:
                print(f"HTTP status {rsp.status} for {url}")
                return None
            content_encoding = rsp.headers.get('Content-Encoding', '').lower()
            data = rsp.read()
            if 'gzip' in content_encoding:
                try:
                    with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
                        data = f.read()
                except Exception as e:
                    print(f"Gzip decompression error: {str(e)}")
                    return None
            try:
                return json.loads(data.decode('utf-8'))
            except UnicodeDecodeError:
                try:
                    return json.loads(data.decode('gbk'))
                except:
                    print("Failed to decode JSON response")
                    return None        
    except HTTPError as e:
        print(f"HTTP Error {e.code}: {e.reason}")
        return None
    except Exception as e:
        print(f"Error fetching patch info: {str(e)}")
        return None

def select_cdn(cdnlist):
    available_nodes = [node for node in cdnlist if node['K1'] == 1 and node['K2'] == 1]
    if not available_nodes:
        return None
    
    max_priority = max(node['P'] for node in available_nodes)
    
    for node in available_nodes:
        if node['P'] == max_priority:
            return node['url']

def get_file_md5(file_path):
    md5_hash = hashlib.md5()
    try:
        with open(file_path, "rb") as file:
            for chunk in iter(lambda: file.read(4096), b""):
                md5_hash.update(chunk)
        return md5_hash.hexdigest()
    except FileNotFoundError:
        print(f"The file {file_path} does not exist.")
        return None

def download_file_with_resume(url, file_path, overwrite=False):
    directory = file_path.parent
    if not directory.exists():
        os.makedirs(directory)
    
    if os.path.exists(file_path):
        if not overwrite:
            print(f'{file_path} already exists. Skipping download.')
            return
        else:
            os.remove(file_path)
            print(f'{file_path} is deleted and start re-download.')
    
    temp_file_path = directory / f'{file_path.name}.temp'
    downloaded_bytes = 0
    if os.path.exists(temp_file_path):
        downloaded_bytes = os.path.getsize(temp_file_path)
    
    headers = {'User-Agent': 'Mozilla/5.0'}
    if downloaded_bytes > 0:
        headers['Range'] = f'bytes={downloaded_bytes}-'
    
    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=10) as rsp:
        
            if rsp.status == 206:
                content_length = rsp.headers.get('Content-Length')
                if content_length:
                    total_size = downloaded_bytes + int(content_length)
                else:
                    total_size = 0
            elif rsp.status == 200:
                content_length = rsp.headers.get('Content-Length')
                total_size = int(content_length) if content_length else 0
                if downloaded_bytes > 0:
                    print("Server doesn't support resume, restarting download")
                    downloaded_bytes = 0
            else:
                print(f"Unexpected HTTP status: {rsp.status}")
                return False
            
            mode = "ab" if downloaded_bytes > 0 else "wb"
            with open(temp_file_path, mode) as file:
                while True:
                    chunk = rsp.read(1024 * 1024)
                    if not chunk:
                        break
                    file.write(chunk)
                    downloaded_bytes += len(chunk)
                    if total_size > 0:
                        percent = (downloaded_bytes / total_size) * 100
                        print(f"\r{file_path} size:{total_size/1024/1024:.1f} MB {percent:.1f}%", end='', flush=True)
            print('',end='\n')
        
        shutil.move(temp_file_path, file_path)
        return True
    except Exception as e:
        print(f"Download error: {str(e)}")
        return False

if __name__ == '__main__':
    
    parser = argparse.ArgumentParser()
    parser.add_argument('--worktype', default='install',help='install or update')
    parser.add_argument('--folder', default='Wuthering Waves Game',help='set download folder')
    args = parser.parse_args()
    
    # create game folder
    game_folder = Path(args.folder)
    if not game_folder.exists():
        game_folder.mkdir()
    
    # get launcher info
    launcher_info = get_result(launcher_api)
    
    if not launcher_info:
        print("Failed to retrieve patch info")
        exit(1)
    
    # select best cdn node
    if launcher_info['default'].get('cdnList', None):
        cdn_node = select_cdn(launcher_info['default']['cdnList'])
        if not cdn_node:
            print('No available CDN node found.')
            exit(1)
        print(f'Selected CDN node: {cdn_node}')
    
    # get last version game filelist
    indexFile_uri = launcher_info['default']['config']['indexFile']
    indexFile = get_result(urljoin(cdn_node, indexFile_uri))
    if not indexFile:
        print("Failed to retrieve index file")
        exit(1)
        
    # download url middle path
    resources_base_path = launcher_info['default']['resourcesBasePath']
    
    if not indexFile.get('resource', None):
        print("No resource files found in index file")
        exit(1)
    
    # download game client file
    if args.worktype == 'install':
        print('Start downloading game client files...')
        length = len(indexFile['resource'])
        print(f'Total resource files: {length}')
        for i, file in enumerate(indexFile['resource']):
            download_url = urljoin(cdn_node, resources_base_path + "/" + file['dest'])
            download_url = quote(download_url, safe=':/')
            file_path = game_folder.joinpath(Path(file['dest']))
            print(f"Downloading file {i+1}/{length}: {file_path}")
            download_file_with_resume(url=download_url, file_path=file_path)
        
        # verification file md5
        for i, file in enumerate(indexFile['resource']):
            
            file_path = game_folder.joinpath(Path(file['dest']))
            current_md5 = get_file_md5(file_path)
            
            if current_md5 == file['md5']:
                print(f'{file_path} - MD5 match')
                continue

            print(f'{file_path} - MD5 mismatch (expected: {file["md5"]}, got: {current_md5})')
            download_url = urljoin(cdn_node, resources_base_path + "/" + file['dest'])
            download_url = quote(download_url, safe=':/')
            download_file_with_resume(url=download_url, file_path=file_path, overwrite=True)
            
            current_md5 = get_file_md5(file_path)
            if current_md5 == file['md5']:
                print(f'{file_path} - MD5 OK after re-download')
            else:
                print(f'{file_path} - Still MD5 mismatch after re-download')
    
    # update game client file
    if args.worktype == 'update':
        print('Starting update game client files...')
        length = len(indexFile['resource'])
        for i, file in enumerate(indexFile['resource']):
            file_path = game_folder.joinpath(Path(file['dest']))
            current_md5 = get_file_md5(file_path)
            print(f"Updataing file {i+1}/{length}: {file_path}")
            if current_md5 == file['md5']:
                print(f'{file_path} - MD5 match')
                continue

            print(f'{file_path} - MD5 mismatch (expected: {file["md5"]}, got: {current_md5})')
            download_url = urljoin(cdn_node, resources_base_path + "/" + file['dest'])
            download_url = quote(download_url, safe=':/')
            download_file_with_resume(url=download_url, file_path=file_path, overwrite=True)
            
            current_md5 = get_file_md5(file_path)
            if current_md5 == file['md5']:
                print(f'{file_path} - MD5 OK after re-download')
            else:
                print(f'{file_path} - Still MD5 mismatch after re-download')
