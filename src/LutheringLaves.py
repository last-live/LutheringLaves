
import os
import shutil
import hashlib
import json
import gzip
import io
import argparse
import logging
import subprocess
import threading
from enum import Enum
from pathlib import Path
from urllib.request import urlopen, Request, HTTPError
from urllib.parse import urljoin,quote

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler('launcher.log', encoding='utf-8'), logging.StreamHandler()]
)
logger = logging.getLogger("LutheringLaves")

# 适配 nuitka 打包后证书丢失与文件路径异常问题
import certifi
os.environ["SSL_CERT_FILE"] = certifi.where()
import sys
base_dir = os.path.dirname(sys.argv[0])
logger.info(f"base dir: {base_dir}")

WW_LAUNCHER_DOWNLOAD_API = 'https://prod-cn-alicdn-gamestarter.kurogame.com/launcher/launcher/10003_Y8xXrXk65DqFHEDgApn3cpK5lfczpFx5/G152/index.json'
WW_RESOURCE_INDEX_HOSTS = (
    'https://prod-volcdn-gamestarter.kurogame.xyz',
    'https://prod-tencentcdn-gamestarter.kurogame.com',
    'https://prod-cn-alicdn-gamestarter.kurogame.com',
)
WW_RESOURCE_INDEX_PATH = 'launcher/game/10003_oLNgHF1CESo51DGHN2odtp40e3oI1HfZ/G152/official/index.json'
LOCAL_MD5_CHUNK_SIZE = 4 * 1024 * 1024
PATCH_NOTIFY_INTERVAL = 64 * 1024 * 1024
QUALITY_LEVELS = ('uhd', 'hd', 'sd')
DEFAULT_QUALITY = 'hd'
QUALITY_NAMES = {
    'uhd': '极致(UHD)',
    'hd': '高清(HD)',
    'sd': '流畅(SD)',
}
PATCH_APPLIED_FILE = 'applied.json'

class _NotifyWriter:
    __slots__ = ('_writer', '_notify', '_pending')

    def __init__(self, writer, notify):
        self._writer = writer
        self._notify = notify
        self._pending = 0

    def __enter__(self):
        self._writer.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._writer.__exit__(exc_type, exc, tb)

    def write(self, data: bytes) -> None:
        self._writer.write(data)
        self._pending += len(data)
        if self._pending >= PATCH_NOTIFY_INTERVAL:
            self._pending = 0
            self._notify()

class LauncherState(Enum):
    STARTGAME = 0
    GAMERUNNING =1
    NEEDINSTALL = 2
    DOWNLOADING = 3
    VALIDATING = 4
    NEEDUPDATE = 5
    UPDATING = 6
    MERGEING = 7
    NETWORKERROR = 8
    NEEDREPAIR = 9

class ProgressInfo:
    def __init__(self):
        self.total_size = 0
        self.finished_size = 0
        self.total_count = 0
        self.finished_count = 0
        
class Launcher:
    
    _instance = None
    _initialized = False
    
    def __new__(cls, game_folder):
        if cls._instance is None:
            cls._instance = super(Launcher, cls).__new__(cls)
        return cls._instance
    
    def __init__(self, game_folder):
        if Launcher._initialized:
            return
        
        self.launcher_info = None
        for host in WW_RESOURCE_INDEX_HOSTS:
            info = self.get_result(host + '/' + WW_RESOURCE_INDEX_PATH)
            if info:
                self.launcher_info = info
                logger.info(f'resource index loaded from: {host}')
                break
        
        if self.launcher_info is None:
            self.cdn_node = ''
            self.state = LauncherState.NETWORKERROR
            return
        
        self.cdn_node = self.select_cdn()
        
        # create game folder
        self.game_folder_path = Path(base_dir) / Path(game_folder)
        logger.info(f"Game folder path: {self.game_folder_path}")
        if not self.game_folder_path.exists():
            logger.info(f"Creating game folder: {self.game_folder_path}")
            self.game_folder_path.mkdir()
            
        self.temp_folder_path = None
        
        # 资源包信息
        self.resource_packs = self.launcher_info.get('resourcePacks') or {}
        self.pack_resources_cache = {}
        self.pack_raw_cache = {}
        self.dest_base_map = {}
        self.current_version = (self.resource_packs.get('common') or {}).get('version')
        if not self.current_version:
            self.state = LauncherState.NETWORKERROR
            logger.error('资源索引中缺少 common 资源包信息')
            return
        self.local_version = self.get_localVersion()
        self.local_pack_versions = self.get_localPackVersions()
        
        self.download_game_progress = ProgressInfo()
        self.verify_game_progress = ProgressInfo()
        self.update_game_progress = ProgressInfo()
        self.update_game_progress_patch = ProgressInfo()
        
        self.support_incremental_patching = False
        self.patch_targets = []
        self.patch_covered_dests = set()
        self.verify_failures = []
        self.progress_callback = None
        self.downloaded_bytes = 0
        self.chunk_md5_cache = {}
        
        self.init_launcher_settings()
        self.init_launcher_state()
        self.init_incremental_update()
        self.init_background()
    
    def init_launcher_state(self):
        self.state = LauncherState.STARTGAME
        logger.info('set launcher state to STARTGAME')
        
        quality = self.get_download_quality()
        resource_list = self.get_resource_list()
        if resource_list is None:
            self.state = LauncherState.NETWORKERROR
            logger.error('resource index unavailable, set launcher state to NETWORKERROR')
            return
        
        if self.local_version is None:
            for file in resource_list:
                file_path = self.game_folder_path.joinpath(Path(file['dest']))
                if not file_path.exists():
                    self.state = LauncherState.NEEDINSTALL
                    logger.info("set launcher state to NEEDINSTALL")
                    return
        else:
            if self.current_version != self.local_version:
                self.state = LauncherState.NEEDUPDATE
                logger.info("set launcher state to NEEDUPDATE")
                return
            # 切换档位后需要补对应资源
            for pack in ('common', quality):
                if not self.is_pack_ready(pack):
                    self.state = LauncherState.NEEDUPDATE
                    logger.info(f'resource pack {pack} not complete, set launcher state to NEEDUPDATE')
                    return
            problems = self.gamedir_layout_diff()
            if problems:
                self.state = LauncherState.NEEDREPAIR
                logger.info(f'game dir layout mismatch, set launcher state to NEEDREPAIR: {problems}')
                return
    
    def init_launcher_settings(self):
        settings_file_path = Path(base_dir) / 'settings.json'
        if not settings_file_path.exists():
            default_settings = {
                "proton_version": "",
                "proton_path": "",
                "steamappid": "0",
                "proton_media_use_gst": "0",
                "proton_enable_wayland": "0",
                "proton_no_d3d12": "0",
                "mangohud": "0",
                "download_quality": DEFAULT_QUALITY
            }

            if self.get_latest_proton():
                default_settings['proton_version'] = self.get_latest_proton()['version']
                default_settings['proton_path'] = self.get_latest_proton()['proton_path']

            with open(settings_file_path, 'w', encoding='utf-8') as f:
                json.dump(default_settings, f, ensure_ascii=False, indent=4)
                
        with open(settings_file_path, 'r', encoding='utf-8') as f:
            settings = json.load(f)
        # 兼容旧设置
        if settings.get('download_quality') not in QUALITY_LEVELS:
            settings['download_quality'] = DEFAULT_QUALITY
        self.settings = settings
            
    def init_background(self):
        background_config = {
            'background': 'background.webp',
            'slogan': 'slogan.png'
        }
        self.background_config = background_config
        launcher_download_info = self.get_result(WW_LAUNCHER_DOWNLOAD_API)
        if launcher_download_info is None: return
        if launcher_download_info.get('functionCode', None) is None: return
        
        function_codes = launcher_download_info['functionCode']['background']
        background_info_api = f'https://prod-cn-alicdn-gamestarter.kurogame.com/launcher/10003_Y8xXrXk65DqFHEDgApn3cpK5lfczpFx5/G152/background/{function_codes}/zh-Hans.json'
        background_info = self.get_result(background_info_api)
        
        if background_info is None: return
        
        background_file_name = background_info['firstFrameImage'].split('/')[-1]
        slogan_file_name = background_info['slogan'].split('/')[-1]

        bd_download = self.download_file_with_resume(background_info['firstFrameImage'], Path(base_dir) / Path('resource') / Path(background_file_name))
        sg_download = self.download_file_with_resume(background_info['slogan'], Path(base_dir) / Path('resource') / Path(slogan_file_name))
        
        if bd_download and sg_download:
            self.background_config['background'] = background_file_name
            self.background_config['slogan'] = slogan_file_name
        
        
    def get_download_quality(self):
        quality = self.settings.get('download_quality', DEFAULT_QUALITY) if hasattr(self, 'settings') else DEFAULT_QUALITY
        return quality if quality in QUALITY_LEVELS else DEFAULT_QUALITY

    def pack_base_url(self, name):
        base = (self.resource_packs.get(name) or {}).get('baseUrl') or ''
        if base and not base.endswith('/'):
            base += '/'
        return base

    def cdn_candidates(self):
        """按优先级排列的可用 CDN 节点"""
        nodes = []
        cdnlist = (self.launcher_info or {}).get('cdnList') or []
        for node in sorted((n for n in cdnlist if n['K1'] == 1 and n['K2'] == 1),
                           key=lambda n: n['P'], reverse=True):
            if node['url'] not in nodes:
                nodes.append(node['url'])
        if self.cdn_node in nodes:
            nodes.remove(self.cdn_node)
            nodes.insert(0, self.cdn_node)
        return nodes

    def fetch_verified(self, path, expect_md5, what):
        """从 CDN 获取文件并校验 md5"""
        for node in self.cdn_candidates() or ([self.cdn_node] if self.cdn_node else []):
            raw = self.get_result_bytes(urljoin(node, path))
            if raw is None:
                continue
            if expect_md5 and hashlib.md5(raw).hexdigest() != expect_md5:
                logger.warning(f'{what} 在 {node} 与索引声明不一致（节点缓存可能未更新），尝试其它节点')
                continue
            if node != self.cdn_node:
                logger.info(f'{what} 由 {node} 获取（原节点内容不一致）')
                self.cdn_node = node
            return raw, node
        return None, None

    def get_pack_resources(self, name):
        if name in self.pack_resources_cache:
            return self.pack_resources_cache[name]
        pack = self.resource_packs.get(name)
        if not pack:
            logger.error(f'资源索引中不存在资源包: {name}')
            return None
        raw, _ = self.fetch_verified(pack['indexFile'], pack.get('indexFileMd5'), f'资源包清单 {name}')
        if raw is None:
            logger.error(f'资源包清单下载失败: {name}')
            return None
        try:
            data = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeError) as e:
            logger.error(f'资源包清单解析失败: {name} ({e})')
            return None
        resources = data.get('resource') or []
        self.pack_resources_cache[name] = resources
        self.pack_raw_cache[name] = raw
        return resources

    def get_resource_list(self):
        resource_list = []
        self.dest_base_map = {}
        for name in ('common', self.get_download_quality()):
            resources = self.get_pack_resources(name)
            if resources is None:
                return None
            base = self.pack_base_url(name)
            for resource in resources:
                self.dest_base_map.setdefault(resource['dest'], base)
                resource_list.append(resource)
        return resource_list

    def is_pack_files_present(self, name):
        resources = self.get_pack_resources(name)
        if resources is None:
            return False
        for resource in resources:
            file_path = self.game_folder_path.joinpath(Path(resource['dest']))
            try:
                if file_path.stat().st_size != int(resource['size']):
                    return False
            except OSError:
                return False
        return True

    def is_pack_ready(self, name):
        local_pack_versions = getattr(self, 'local_pack_versions', None)
        marker = local_pack_versions.get(name) if local_pack_versions is not None else None
        if marker is None and local_pack_versions is None and name in ('common', 'hd'):
            marker = getattr(self, 'local_version', None)
        target = (self.resource_packs.get(name) or {}).get('version')
        if not marker or not target or marker != target:
            return False
        return self.is_pack_files_present(name)

    def build_download_url(self, dest):
        base = self.dest_base_map.get(dest) or self.pack_base_url('common')
        url = urljoin(self.cdn_node, base + dest)
        return quote(url, safe=':/')

    def download_game(self):
        logger.info('Start downloading game client files...')
        self.state = LauncherState.DOWNLOADING
        self.downloaded_bytes = 0
        self.download_game_progress.finished_size = 0
        self.download_game_progress.finished_count = 0
        resource_list = self.get_resource_list()
        if resource_list is None:
            raise RuntimeError('无法获取官方资源清单，请稍后重试')
        self.download_game_progress.total_count = len(resource_list)
        self.download_game_progress.total_size = 0
        for resource in resource_list:
            self.download_game_progress.total_size += resource['size']
        length = self.download_game_progress.total_count
        logger.info(f'Total resource files: {length}')
        for file in resource_list:
            download_url = self.build_download_url(file['dest'])
            file_size = int(file['size'])
            file_path = self.game_folder_path.joinpath(Path(file['dest']))
            downloaded_count = self.download_game_progress.finished_count
            logger.info(f"Downloading file {downloaded_count + 1} / {length}: {file_path}")
            self.download_file_with_resume(url=download_url, file_path=file_path, flag='download', file_size=file_size)
            self.download_game_progress.finished_count += 1
    
    def get_progress(self, flag):
        table = {
            'download': self.download_game_progress,
            'update': self.update_game_progress,
            'update_patch': self.update_game_progress_patch,
            'verify': self.verify_game_progress,
        }
        progress = table.get(flag)
        if progress is None:
            raise RuntimeError(f'未知的进度类型: {flag}')
        return progress

    def estimate_pending_download(self, resource_list):
        """估算待下载体积与文件数"""
        pending_bytes = 0
        pending_count = 0
        for resource in resource_list:
            file_path = self.game_folder_path.joinpath(Path(resource['dest']))
            try:
                local_size = file_path.stat().st_size
            except OSError:
                local_size = None
            size = int(resource['size'])
            if local_size is None or local_size != size:
                pending_bytes += size
                pending_count += 1
        return pending_bytes, pending_count

    def update_game(self, skip_dests=None, flag='update', totals=None):
        """增量更新"""
        logger.info('Starting update game client files...')
        self.state = LauncherState.UPDATING
        resource_list = self.get_resource_list()
        if resource_list is None:
            raise RuntimeError('无法获取官方资源清单，请稍后重试')
        skip = set(skip_dests or ())
        targets = [resource for resource in resource_list if resource['dest'] not in skip]
        progress = self.get_progress(flag)
        if totals is None:
            progress.total_size, progress.total_count = self.estimate_pending_download(targets)
            progress.finished_size = 0
            progress.finished_count = 0
        else:
            progress.total_size, progress.total_count = int(totals[0]), int(totals[1])
        logger.info(
            '待下载估算: %.2f GB / %d 个文件（%d 个文件已由补丁覆盖并跳过）'
            % (progress.total_size / 1024 ** 3, progress.total_count, len(resource_list) - len(targets))
        )
        length = len(targets)
        for index, file in enumerate(targets):
            file_path = self.game_folder_path.joinpath(Path(file['dest']))
            file_size = int(file['size'])
            logger.info(f"Updataing file {index + 1} / {length}: {file_path}")
            # 无文件时下载整个文件
            if not file_path.exists():
                logger.warning(f'{file_path} not found, downloading whole file')
                self.download_file_with_resume(
                    url=self.build_download_url(file['dest']),
                    file_path=file_path,
                    flag=flag
                )
                progress.finished_count += 1
                continue
            # 有 chunkInfos 文件做分块增量
            if file.get('chunkInfos'):
                downloaded = self.update_file_by_chunks(
                    self.build_download_url(file['dest']),
                    file_path,
                    file,
                    flag=flag
                )
                if downloaded >= 0:
                    progress.finished_count += 1
                    logger.info(f'{file_path} chunk-level update done, downloaded {downloaded / 1024 / 1024:.1f} MB')
                    continue
                logger.warning(f'{file_path} chunk-level update failed, fallback to whole file download')
            # 回退整文件md5
            current_md5 = self.get_file_md5(file_path)
            if current_md5 == file['md5']:
                progress.finished_count += 1
                logger.info(f'{file_path} MD5 match')
                continue
            logger.warning(f'{file_path} MD5 mismatch (expected: {file["md5"]}, got: {current_md5})')
            self.download_file_with_resume(
                url=self.build_download_url(file['dest']),
                file_path=file_path,
                overwrite=True,
                flag=flag
            )
            progress.finished_count += 1
    
    def patch_temp_path(self) -> Path:
        if not self.temp_folder_path:
            self.temp_folder_path = self.game_folder_path.parent / 'temp_folder'
        return self.temp_folder_path

    def load_applied_groups(self) -> set:
        cached = getattr(self, '_applied_groups', None)
        if cached is not None:
            return cached
        applied = set()
        try:
            path = self.patch_temp_path() / PATCH_APPLIED_FILE
            applied = {(item['pack'], item['dest']) for item in json.loads(path.read_text(encoding='utf-8'))}
        except (OSError, ValueError, TypeError, KeyError):
            applied = set()
        self._applied_groups = applied
        return applied

    def mark_group_applied(self, pack, dest) -> None:
        applied = set(self.load_applied_groups())
        applied.add((pack, dest))
        self._applied_groups = applied
        path = self.patch_temp_path() / PATCH_APPLIED_FILE
        temp = path.with_name(path.name + '.tmp')
        temp.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(
            json.dumps([{'pack': pack_, 'dest': dest_} for pack_, dest_ in sorted(applied)]),
            encoding='utf-8'
        )
        os.replace(temp, path)

    def group_applied(self, pack, group) -> bool:
        if (pack, group['dest']) not in self.load_applied_groups():
            return False
        for file in group.get('dstFiles') or []:
            try:
                size = self.game_folder_path.joinpath(Path(file['dest'])).stat().st_size
            except OSError:
                return False
            if size != int(file['size']):
                return False
        return True

    def applied_group_dests(self, target) -> set:
        return {group['dest'] for group in target['manifest'].get('groupInfos', [])
                if self.group_applied(target['pack'], group)}

    def download_patch(self, on_resource_done=None):
        if not self.patch_targets:
            raise RuntimeError('增量补丁不可用')
        # 硬盘空间检查
        for target in self.patch_targets:
            ext = target['entry'].get('ext') or {}
            required = int(ext.get('requiredDiskSpace') or 0)
            if required > 0:
                free = shutil.disk_usage(str(self.game_folder_path)).free
                if free < required:
                    required_gib = required / 1024 ** 3
                    free_gib = free / 1024 ** 3
                    raise RuntimeError(f'磁盘空间不足: 更新需要 {required_gib:.2f} GiB, 当前可用 {free_gib:.2f} GiB')
        self.temp_folder_path = self.game_folder_path.parent / 'temp_folder'
        temp_root = self.temp_folder_path
        if not temp_root.exists():
            temp_root.mkdir()
        progress = self.update_game_progress_patch
        patch_count = 0
        patch_size = 0
        for target in self.patch_targets:
            skip_dests = self.applied_group_dests(target)
            for file in target['manifest'].get('resource', []):
                if file['dest'] in skip_dests:
                    continue
                patch_count += 1
                patch_size += int(file['size'])
        if not progress.total_size:
            progress.total_size = patch_size
            progress.total_count = patch_count
        done = 0
        for target in self.patch_targets:
            pack = target['pack']
            pack_temp = temp_root / pack
            if not pack_temp.exists():
                pack_temp.mkdir()
            pack_base = target['entry'].get('baseUrl') or ''
            if pack_base and not pack_base.endswith('/'):
                pack_base += '/'
            resource_list = target['manifest'].get('resource', [])
            skip_dests = self.applied_group_dests(target)
            for file in resource_list:
                file_size = int(file['size'])
                if file['dest'] in skip_dests:
                    logger.info(f'补丁组 {file["dest"]} 上次已应用，跳过下载')
                    continue
                if 'fromFolder' in file:
                    base = file['fromFolder']
                    if not base.endswith('/'):
                        base += '/'
                    download_url = urljoin(self.cdn_node, base + file['dest'])
                    file_path = self.game_folder_path.joinpath(Path(file['dest']))
                else:
                    download_url = urljoin(self.cdn_node, pack_base + file['dest'])
                    file_path = pack_temp.joinpath(Path(file['dest']))
                download_url = quote(download_url, safe=':/')
                done += 1
                logger.info(f'Downloading patch resource {done}/{patch_count} [{pack}]: {file_path}')
                if not self.download_file_with_resume(
                    url=download_url,
                    file_path=file_path,
                    flag='update_patch',
                    file_size=file_size
                ):
                    raise RuntimeError(f'补丁资源下载失败: {file_path}')
                if 'fromFolder' in file:
                    self.verify_patch_file(file_path, file, download_url)
                    self.patch_covered_dests.add(file['dest'])
                progress.finished_count += 1
                if on_resource_done is not None:
                    on_resource_done(pack, file['dest'])

    def verify_patch_file(self, file_path, file_info, download_url):
        """校验文件"""
        expect_md5 = file_info.get('md5')
        if not expect_md5:
            return
        if self.get_file_md5(file_path) == expect_md5:
            return
        logger.warning(f'{file_path} 补丁资源 MD5 不符，重新下载')
        self.download_file_with_resume(
            url=download_url,
            file_path=file_path,
            overwrite=True,
            flag='update_patch'
        )
        if self.get_file_md5(file_path) != expect_md5:
            raise RuntimeError(f'补丁资源校验失败: {file_path}')

    def merge_patch(self, wait_group_ready=None) -> set:
        if not self.patch_targets:
            raise RuntimeError('增量补丁清单不可用')
        if not self.temp_folder_path:
            self.temp_folder_path = self.game_folder_path.parent / 'temp_folder'
        for target in self.patch_targets:
            pack = target['pack']
            pack_temp = self.temp_folder_path / pack
            group_infos = target['manifest'].get('groupInfos', [])
            length = len(group_infos)
            for i, group in enumerate(group_infos):
                group_dest = group['dest']
                if self.group_applied(pack, group):
                    logger.info(f'补丁组 {i+1}/{length} 上次已应用，跳过: {group_dest}')
                    self.patch_covered_dests.update(f['dest'] for f in group.get('dstFiles', []))
                    continue
                if wait_group_ready is not None and not wait_group_ready(pack, group_dest):
                    logger.warning('组补丁应用已中止（下载失败）')
                    return self.patch_covered_dests
                patch_path = pack_temp.joinpath(Path(group_dest))
                if not patch_path.exists():
                    raise RuntimeError(f'缺少组补丁文件: {patch_path}')
                logger.info(f'Applying patch group {i+1}/{length} [{pack}]: {group_dest}')
                produced = self.apply_group_patch(patch_path, group)
                self.patch_covered_dests |= produced or set()
                self.mark_group_applied(pack, group_dest)
                patch_path.unlink(missing_ok=True)
            for rel in target['manifest'].get('deleteFiles', []):
                delete_path = self.game_folder_path.joinpath(Path(rel))
                if delete_path.exists():
                    logger.info(f'Deleting obsolete file: {rel}')
                    delete_path.unlink()
        # 清理临时目录
        if self.temp_folder_path and self.temp_folder_path.exists():
            shutil.rmtree(str(self.temp_folder_path), ignore_errors=True)
        self.temp_folder_path = None
        self._applied_groups = None
        return self.patch_covered_dests

    def sync_patch(self) -> set:
        if not self.patch_targets:
            raise RuntimeError('增量补丁不可用')
        groups = [(target['pack'], group['dest']) for target in self.patch_targets
                  for group in target['manifest'].get('groupInfos', [])]
        ready = {key: threading.Event() for key in groups}
        failure = []
        abort = threading.Event()
        download_done = threading.Event()

        def resource_done(pack, dest):
            if failure:
                raise failure[0]
            event = ready.get((pack, dest))
            if event is not None:
                event.set()

        def wait_group_ready(pack, dest):
            event = ready.get((pack, dest))
            if event is None:
                return True  # 不在下载清单里，交给 merge_patch 自行报错
            while not event.wait(0.5):
                if abort.is_set():
                    return False
                if download_done.is_set():
                    raise RuntimeError(f'组补丁未下载完成: {dest}')
            return True

        def applier():
            try:
                self.merge_patch(wait_group_ready=wait_group_ready)
            except BaseException as e:
                failure.append(e)
                abort.set()

        logger.info(f'边下载边应用: {len(groups)} 个组补丁')
        thread = threading.Thread(target=applier, name='patch-applier', daemon=True)
        thread.start()
        try:
            self.download_patch(on_resource_done=resource_done)
        except BaseException:
            abort.set()
            raise
        finally:
            download_done.set()
            thread.join()
        if failure:
            raise failure[0]
        return self.patch_covered_dests

    def apply_group_patch(self, patch_path, group) -> set:
        try:
            from . import krpdiff
        except ImportError:
            import krpdiff
        src_info = {f['dest']: (f['md5'], int(f['size'])) for f in group.get('srcFiles', [])}
        dst_info = {f['dest']: (f['md5'], int(f['size'])) for f in group.get('dstFiles', [])}
        dst_chunk_infos = {f['dest']: f.get('chunkInfos') for f in group.get('dstFiles', [])}
        game_dir = self.game_folder_path

        def verify_old(path, expected_size):
            """应用前校验旧文件"""
            rel_path = os.path.relpath(path, str(game_dir)).replace(os.sep, '/')
            try:
                digest, size = krpdiff.md5_file(path)
            except OSError as e:
                raise krpdiff.KrpdiffError(f'旧文件不可读: {rel_path} ({e})') from e
            expect = src_info.get(rel_path)
            if expect:
                if digest != expect[0] or size != expect[1]:
                    raise krpdiff.KrpdiffError(
                        f'旧文件 MD5 不符: {rel_path} (实际 {digest}/{size}, 期望 {expect[0]}/{expect[1]})')
            elif expected_size is not None and size != expected_size:
                raise krpdiff.KrpdiffError(
                    f'旧文件大小不符: {rel_path} (实际 {size}, 期望 {expected_size})')
            return size

        def open_writer(rel_path, size):
            expect = dst_info.get(rel_path)
            if expect is None:
                logger.warning(f'补丁输出未在清单中登记: {rel_path}')
            target = game_dir.joinpath(Path(rel_path))
            writer = krpdiff.PatchWriter(
                target,
                expect[1] if expect else size,
                expect[0] if expect else None,
            )
            return _NotifyWriter(writer, self.notify_progress)

        with open(patch_path, 'rb') as patch_file:
            patch = krpdiff.parse_dir_patch(patch_file)
            old_stream = krpdiff.OldStream.from_refs(patch.old_ref_files, game_dir, verify=verify_old)
            try:
                produced = patch.apply_streaming(old_stream, open_writer)
            finally:
                old_stream.close()

        covered = set(produced)
        for rel_path in produced:
            chunk_infos = dst_chunk_infos.get(rel_path)
            if chunk_infos:
                self.cache_chunks_md5(game_dir.joinpath(Path(rel_path)), chunk_infos)
        for copy in patch.copy_files:
            src_path = game_dir.joinpath(Path(copy.old_path))
            dst_path = game_dir.joinpath(Path(copy.new_path))
            if src_path == dst_path:
                continue
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(src_path), str(dst_path))
        # 空文件与目录
        for rel in patch.empty_files:
            file_path = game_dir.joinpath(Path(rel))
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(b'')
        for rel in patch.new_dirs:
            if rel:
                game_dir.joinpath(Path(rel)).mkdir(parents=True, exist_ok=True)
        # 可执行位
        if patch.execute_files and os.name == 'posix':
            for rel in patch.execute_files:
                file_path = game_dir.joinpath(Path(rel))
                if file_path.exists():
                    file_path.chmod(file_path.stat().st_mode | 0o111)
        return covered

    def patch_plan(self) -> dict:
        """计算本次增量更新预估下载量"""
        patch_bytes = 0
        patch_count = 0
        covered = set()
        for target in self.patch_targets:
            manifest = target['manifest']
            skip_dests = self.applied_group_dests(target)
            for file in manifest.get('resource', []):
                if file['dest'] in skip_dests:
                    continue
                patch_bytes += int(file['size'])
                patch_count += 1
                if 'fromFolder' in file:
                    covered.add(file['dest'])
            for group in manifest.get('groupInfos', []):
                for file in group.get('dstFiles', []):
                    covered.add(file['dest'])
        resource_list = self.get_resource_list() or []
        remaining = [resource for resource in resource_list if resource['dest'] not in covered]
        extra_bytes, extra_count = self.estimate_pending_download(remaining)
        return {
            'patch_bytes': patch_bytes,
            'patch_count': patch_count,
            'covered': covered,
            'extra_bytes': extra_bytes,
            'extra_count': extra_count,
        }

    def update_game_with_patch(self):
        self.state = LauncherState.UPDATING
        used_patch = False
        self.patch_covered_dests = set()
        self.downloaded_bytes = 0
        progress = self.update_game_progress_patch
        if self.patch_targets:
            plan = self.patch_plan()
            total_bytes = plan['patch_bytes'] + plan['extra_bytes']
            total_count = plan['patch_count'] + plan['extra_count']
            progress.total_size = total_bytes
            progress.total_count = total_count
            progress.finished_size = 0
            progress.finished_count = 0
            logger.info(
                '本次更新预估下载 %.2f GB（官方增量补丁 %.2f GB + 其余资源 %.2f GB）'
                % (total_bytes / 1024 ** 3, plan['patch_bytes'] / 1024 ** 3, plan['extra_bytes'] / 1024 ** 3)
            )
            try:
                self.sync_patch()
                used_patch = True
                logger.info(f'组补丁增量更新完成，已覆盖 {len(self.patch_covered_dests)} 个文件')
            except Exception as e:
                logger.exception(f'组补丁增量更新失败，回退到常规更新流程: {e}')
                self.patch_covered_dests = set()
                try:
                    if self.temp_folder_path and self.temp_folder_path.exists():
                        shutil.rmtree(str(self.temp_folder_path), ignore_errors=True)
                    self.temp_folder_path = None
                    self._applied_groups = None
                except Exception:
                    pass
        if used_patch:
            self.update_game(
                skip_dests=self.patch_covered_dests,
                flag='update_patch',
                totals=(progress.total_size, progress.total_count),
            )
        else:
            self.update_game() # again
        return used_patch

    def verify_one_file(self, file):
        file_path = self.game_folder_path.joinpath(Path(file['dest']))
        download_url = self.build_download_url(file['dest'])
        if not file_path.exists():
            logger.warning(f'{file_path} not found, downloading whole file')
            ok = self.download_file_with_resume(url=download_url, file_path=file_path, flag='verify')
            if not ok or self.get_file_md5(file_path) != file['md5']:
                self.verify_failures.append(file['dest'])
                logger.error(f'{file_path} 校验失败')
            return
        # 有 chunkInfos 的文件分块校验/修复
        if file.get('chunkInfos'):
            if self.update_file_by_chunks(download_url, file_path, file, flag='verify') >= 0:
                return

        current_md5 = self.get_file_md5(file_path)

        if current_md5 == file['md5']:
            logger.info(f'{file_path} MD5 match')
            return

        logger.warning(f'{file_path} MD5 mismatch (expected: {file["md5"]}, got: {current_md5})')
        self.download_file_with_resume(url=download_url, file_path=file_path, overwrite=True, flag='verify')

        if self.get_file_md5(file_path) == file['md5']:
            logger.info(f'{file_path} MD5 OK after re-download')
        else:
            logger.error(f'{file_path} Still MD5 mismatch after re-download')
            self.verify_failures.append(file['dest'])

    def verify_gamefile(self):
        self.state = LauncherState.VALIDATING
        resource_list = self.get_resource_list()
        if resource_list is None:
            raise RuntimeError('无法获取官方资源清单，请稍后重试')
        self.verify_failures = []
        self.downloaded_bytes = 0
        self.verify_game_progress.total_size = 0
        self.verify_game_progress.finished_size = 0
        self.verify_game_progress.finished_count = 0
        
        chunk_paks = []
        
        for resource in resource_list:
            self.verify_game_progress.total_size += resource['size']
            if resource['dest'].startswith('Client/Content/Paks/'):
                chunk_paks.append(resource['dest'].split('/')[-1])

        # 删除无效的pak文件
        paks_dir = self.game_folder_path / 'Client' / 'Content' / 'Paks'
        if paks_dir.exists():
            for chunk_pak in os.listdir(paks_dir):
                if chunk_pak not in chunk_paks:
                    remove_file = paks_dir / chunk_pak
                    logger.warning(f'Chunk pak {chunk_pak} will be removed')
                    if remove_file.exists():
                        remove_file.unlink()
    
        for file in resource_list:
            file_size = int(file['size'])
            before = self.verify_game_progress.finished_size
            self.verify_one_file(file)
            counted = self.verify_game_progress.finished_size - before
            if file_size > counted:
                self.update_progress(flag='verify', value=file_size - counted)

        if self.verify_failures:
            logger.error(f'有 {len(self.verify_failures)} 个文件校验失败，本次不写入版本号，避免把不完整安装标记为最新')
        else:
            self.update_localVersion()

    def has_verify_failures(self):
        return bool(getattr(self, 'verify_failures', None))
    
    def get_result_bytes(self, url):
        try:
            req = Request(url, headers={
                'User-Agent': 'Mozilla/5.0',
                'Accept-Encoding': 'gzip'
            })
            with urlopen(req, timeout=10) as rsp:
                if rsp.status != 200:
                    logger.error(f"HTTP status {rsp.status} for {url}")
                    return None
                content_encoding = rsp.headers.get('Content-Encoding', '').lower()
                data = rsp.read()
                if 'gzip' in content_encoding:
                    try:
                        with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
                            data = f.read()
                    except Exception as e:
                        logger.error(f"Gzip decompression error: {str(e)}")
                        return None
                return data
        except HTTPError as e:
            logger.error(f"HTTP Error {e.code}: {e.reason}")
            return None
        except Exception as e:
            logger.error(f"Error fetching {url}: {str(e)}")
            return None
    
    def get_result(self, url):
        data = self.get_result_bytes(url)
        if data is None:
            return None
        try:
            return json.loads(data.decode('utf-8'))
        except UnicodeDecodeError:
            try:
                return json.loads(data.decode('gbk'))
            except Exception:
                logger.error("Failed to decode JSON response")
                return None
    
    def select_cdn(self):
        if self.launcher_info is None: return None
        
        cdnlist = self.launcher_info.get('cdnList', None)
        
        if not cdnlist: return None
        
        available_nodes = [node for node in cdnlist if node['K1'] == 1 and node['K2'] == 1]
        if not available_nodes:
            return None
        
        max_priority = max(node['P'] for node in available_nodes)
        
        for node in available_nodes:
            if node['P'] == max_priority:
                return node['url']
    
    def get_localVersion(self):
        file_path = self.game_folder_path / "launcherDownloadConfig.json"
        if not os.path.exists(file_path):
            return None
        with open(file_path, 'r', encoding='utf-8') as file:
            data = json.load(file)
            return data.get('version', None)
    
    def get_localPackVersions(self):
        data = {}
        file_path = self.game_folder_path / "launcherDownloadConfig.json"
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as file:
                    data = json.load(file)
            except (ValueError, OSError):
                data = {}
        packs = {}
        local_packs = data.get('packs')
        if isinstance(local_packs, dict):
            for name, version in local_packs.items():
                if version:
                    packs[name] = version
        bundles = data.get('bundles')
        if isinstance(bundles, dict):
            for bundle_name, record in bundles.items():
                if not isinstance(record, dict) or not record.get('version'):
                    continue
                names = record.get('resourcePacks') or self.bundle_resource_packs(bundle_name)
                for name in names:
                    packs.setdefault(name, record['version'])
        for name in ('common',) + QUALITY_LEVELS:
            version = self.read_pack_version_record(name)
            if version:
                packs[name] = version
        return packs or None

    def pack_records_dir(self):
        return self.game_folder_path / 'launcherDownloadConfig'

    def pack_cache_dir(self, name):
        version = (self.resource_packs.get(name) or {}).get('version') or ''
        return self.game_folder_path / 'launcherDownload' / name / version

    def bundle_resource_packs(self, bundle_name):
        definitions = (getattr(self, 'launcher_info', None) or {}).get('bundles') or {}
        for defined_name, definition in definitions.items():
            if defined_name.lower() == bundle_name.lower() and definition.get('resourcePacks'):
                return list(definition['resourcePacks'])
        return ['common', bundle_name.lower()]

    def read_pack_version_record(self, name):
        path = self.pack_records_dir() / f'{name}.json'
        try:
            with open(path, 'r', encoding='utf-8') as file:
                data = json.load(file)
        except (ValueError, OSError):
            return None
        version = data.get('version') if isinstance(data, dict) else None
        return version or None

    def write_pack_version_record(self, name, version):
        path = self.pack_records_dir() / f'{name}.json'
        content = {"packName": name, "version": version}
        try:
            if path.exists():
                try:
                    with open(path, 'r', encoding='utf-8') as file:
                        if json.load(file) == content:
                            return
                except (ValueError, OSError):
                    pass
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as file:
                json.dump(content, file, ensure_ascii=False, separators=(',', ':'))
        except OSError as e:
            logger.warning(f'写入资源包版本记录失败 {name}: {e}')

    def clear_pack_records(self, names):
        for name in names:
            path = self.pack_records_dir() / f'{name}.json'
            try:
                if path.exists():
                    path.unlink()
            except OSError as e:
                logger.warning(f'删除资源包版本记录失败 {name}: {e}')
            cache_dir = self.pack_cache_dir(name)
            if cache_dir.is_dir():
                shutil.rmtree(cache_dir, ignore_errors=True)

    def write_pack_manifest_cache(self, name):
        raw = getattr(self, 'pack_raw_cache', {}).get(name)
        if not raw:
            return
        path = self.pack_cache_dir(name) / 'OriginResource.json'
        try:
            if path.exists():
                with open(path, 'rb') as file:
                    if file.read() == raw:
                        return
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'wb') as file:
                file.write(raw)
        except OSError as e:
            logger.warning(f'写入资源包清单缓存失败 {name}: {e}')

    def build_local_bundles(self, packs, verified_names):
        bundles = {}
        for name in QUALITY_LEVELS:
            members = self.bundle_resource_packs(name.upper())
            if not set(members) <= set(verified_names):
                continue
            version = (self.resource_packs.get(name) or {}).get('version')
            if not version:
                continue
            bundles[name.upper()] = {
                "version": version,
                "state": "",
                "resourcePacks": members,
            }
        return bundles
    
    def key_file_check_list(self):
        config = (getattr(self, 'launcher_info', None) or {}).get('config') or {}
        if str(config.get('keyFileCheckSwitch', 0)) != '1':
            return []
        return list(config.get('keyFileCheckList') or [])

    def gamedir_layout_diff(self):
        problems = []
        config_path = self.game_folder_path / 'launcherDownloadConfig.json'
        data = {}
        if not config_path.exists():
            problems.append('缺少 launcherDownloadConfig.json')
        else:
            try:
                with open(config_path, 'r', encoding='utf-8') as file:
                    data = json.load(file) or {}
            except (ValueError, OSError):
                problems.append('launcherDownloadConfig.json 内容无法解析')
                data = {}

        quality = self.get_download_quality()
        bundle_name = quality.upper()
        members = self.bundle_resource_packs(bundle_name)
        bundle_version = (self.resource_packs.get(quality) or {}).get('version')

        if data:
            if not data.get('appId'):
                problems.append('launcherDownloadConfig.json 缺少 appId')
            bundle = (data.get('bundles') or {}).get(bundle_name)
            if not isinstance(bundle, dict):
                problems.append(f'bundles 里没有 {bundle_name} 档位记录')
            else:
                if bundle_version and bundle.get('version') != bundle_version:
                    problems.append(f'bundles.{bundle_name}.version 与索引不一致')
                if sorted(bundle.get('resourcePacks') or []) != sorted(members):
                    problems.append(f'bundles.{bundle_name}.resourcePacks 与索引定义不一致')

        for name in members:
            version = (self.resource_packs.get(name) or {}).get('version')
            if not version:
                continue
            if self.read_pack_version_record(name) != version:
                problems.append(f'缺少资源包版本文件 launcherDownloadConfig/{name}.json')
            try:
                cached = (self.pack_cache_dir(name) / 'OriginResource.json').read_bytes()
            except OSError:
                cached = None
            raw = (self.pack_raw_cache or {}).get(name)
            if not cached:
                problems.append(f'缺少资源包清单缓存 launcherDownload/{name}/{version}/OriginResource.json')
            elif raw is not None and cached != raw:
                problems.append(f'资源包清单缓存 {name} 与索引中的清单不一致')

        for rel in self.key_file_check_list():
            try:
                if self.game_folder_path.joinpath(Path(rel)).stat().st_size == 0:
                    problems.append(f'关键文件为空 {rel}')
            except OSError:
                problems.append(f'缺少关键文件 {rel}')
        return problems

    def repair_layout(self):
        logger.info('repairing install layout...')
        for name in ('common', self.get_download_quality()):
            self.get_pack_resources(name)
        self.update_localVersion()
        problems = self.gamedir_layout_diff()
        logger.info(f'repair finished, remaining problems: {problems}')
        return problems

    def update_localVersion(self):
        packs = dict(self.local_pack_versions) if self.local_pack_versions else {}
        if self.local_pack_versions is None and self.local_version:
            for name in ('common', 'hd'):
                packs.setdefault(name, self.local_version)
        for name in ('common', self.get_download_quality()):
            target_version = (self.resource_packs.get(name) or {}).get('version')
            if target_version and self.is_pack_files_present(name):
                packs[name] = target_version
        self.local_pack_versions = packs
        verified_names = set()
        for name in ('common', self.get_download_quality()):
            version = packs.get(name)
            if version and self.is_pack_files_present(name):
                verified_names.add(name)
                self.write_pack_version_record(name, version)
                self.write_pack_manifest_cache(name)
        temp = {
            "version":self.current_version,
            "reUseVersion":"",
            "state":"",
            "isPreDownload":False,
            "appId":"10003",
            "packs":packs,
            "bundles":self.build_local_bundles(packs, verified_names)
        }
        file_path = self.game_folder_path / "launcherDownloadConfig.json"
        with open(file_path, 'w', encoding='utf-8') as file:
            json.dump(temp, file, ensure_ascii=False)
        if self.current_version:
            self.local_version = self.current_version
    
    def get_quality_status(self):
        resource_packs = getattr(self, 'resource_packs', None)
        if not resource_packs:
            return None
        result = []
        for quality in QUALITY_LEVELS:
            pack = resource_packs.get(quality) or {}
            size = pack.get('size')
            result.append({
                'key': quality,
                'name': QUALITY_NAMES.get(quality, quality),
                'size': int(size) if size else None,
                'downloaded': self.is_pack_ready(quality),
            })
        return result
    
    def release_quality_packs(self, names):
        if self.state == LauncherState.GAMERUNNING:
            raise RuntimeError('游戏正在运行，无法释放资源')
        pack_dirs = {
            'uhd': 'Client/Content/UHD/',
            'hd': 'Client/Content/HD/',
            'sd': 'Client/Content/SD/',
        }
        active_quality = self.get_download_quality()
        deleted_count = 0
        freed_bytes = 0
        for name in names:
            if name == active_quality:
                logger.warning(f'不允许释放当前使用中的档位: {name}')
                continue
            prefix = pack_dirs.get(name)
            resources = self.get_pack_resources(name) if prefix else None
            if not resources:
                continue
            for resource in resources:
                dest = resource['dest']
                if not dest.startswith(prefix):
                    logger.warning(f'跳过非档位目录文件: {dest}')
                    continue
                file_path = self.game_folder_path.joinpath(Path(dest))
                if file_path.exists():
                    try:
                        freed_bytes += file_path.stat().st_size
                        file_path.unlink()
                        deleted_count += 1
                    except OSError as e:
                        logger.error(f'删除失败 {file_path}: {e}')
            if self.local_pack_versions is not None:
                self.local_pack_versions.pop(name, None)
            self.clear_pack_records([name])
        self.update_localVersion()
        return deleted_count, freed_bytes
    
    def download_file_with_resume(self, url, file_path, overwrite=False, flag=None, file_size=None):
        directory = file_path.parent
        if not directory.exists():
            os.makedirs(directory)
        
        if os.path.exists(file_path):
            local_size = os.path.getsize(file_path)
            if not overwrite and file_size and local_size != int(file_size):
                logger.warning(f'{file_path} size mismatch ({local_size} != {file_size}), re-downloading')
                os.remove(file_path)
                temp_leftover = directory / f'{file_path.name}.temp'
                if temp_leftover.exists():
                    os.remove(temp_leftover)
            elif not overwrite:
                if file_size and flag == 'download':
                    self.update_progress(flag=flag, value=int(file_size))
                logger.info(f'{file_path} already exists. Skipping download.')
                return True
            else:
                os.remove(file_path)
                logger.info(f'{file_path} is deleted and start re-download.')
        
        temp_file_path = directory / f'{file_path.name}.temp'
        downloaded_bytes = 0
        if os.path.exists(temp_file_path):
            downloaded_bytes = os.path.getsize(temp_file_path)
            
        headers = {'User-Agent': 'Mozilla/5.0'}
        if downloaded_bytes > 0:
            headers['Range'] = f'bytes={downloaded_bytes}-'
            
        content_length = 0
        
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=10) as rsp:
                if rsp.status == 206:
                    content_length = int(rsp.headers.get('Content-Length'))
                    total_size = downloaded_bytes + content_length
                elif rsp.status == 200:
                    content_length = int(rsp.headers.get('Content-Length'))
                    total_size = content_length if content_length else 0
                    if downloaded_bytes > 0:
                        logger.warning("Server doesn't support resume, restarting download")
                        downloaded_bytes = 0
                else:
                    logger.error(f"Unexpected HTTP status: {rsp.status}")
                    return False
                
                mode = "ab" if downloaded_bytes > 0 else "wb"
                with open(temp_file_path, mode) as file:
                    while True:
                        chunk = rsp.read(1024 * 1024)
                        if not chunk:
                            break
                        file.write(chunk)
                        downloaded_bytes += len(chunk)
                        self.downloaded_bytes += len(chunk)
                        self.update_progress(flag=flag, value=len(chunk))
                        if total_size > 0:
                            percent = (downloaded_bytes / total_size) * 100
                            logger.info(f"{file_path} size:{total_size/1024/1024:.1f} MB {percent:.1f}%")
            
            if total_size > 0 and downloaded_bytes < total_size:
                logger.warning(f'{file_path} 传输中断（{downloaded_bytes}/{total_size} 字节），保留 .temp 便于续传')
                return False
            shutil.move(temp_file_path, file_path)
            return True
        except Exception as e:
            logger.error(f"Download error: {str(e)}")
            if getattr(e, 'code', None) == 416 and downloaded_bytes == content_length:
                if temp_file_path.exists(): 
                    shutil.move(temp_file_path, file_path)
            return False

    def _md5_range(self, f, start, end):
        """计算chunk md5"""
        length = end - start + 1
        md5_hash = hashlib.md5()
        f.seek(start)
        remaining = length
        while remaining > 0:
            data = f.read(min(LOCAL_MD5_CHUNK_SIZE, remaining))
            if not data:
                break
            md5_hash.update(data)
            remaining -= len(data)
        return None if remaining > 0 else md5_hash.hexdigest()

    def compute_chunks_md5(self, file_path, chunk_infos):
        try:
            st = os.stat(file_path)
        except OSError:
            return [None] * len(chunk_infos)
        cache_key = (str(file_path), st.st_size, st.st_mtime_ns)
        cached = self.chunk_md5_cache.get(cache_key)
        if cached is not None and len(cached) == len(chunk_infos):
            return cached
        results = []
        with open(file_path, 'rb') as f:
            for ci in chunk_infos:
                results.append(self._md5_range(f, int(ci['start']), int(ci['end'])))
        self.chunk_md5_cache[cache_key] = results
        return results

    def cache_chunks_md5(self, file_path, chunk_infos):
        try:
            st = os.stat(file_path)
        except OSError:
            return
        self.chunk_md5_cache[(str(file_path), st.st_size, st.st_mtime_ns)] = [ci['md5'] for ci in chunk_infos]

    def download_range_into(self, url, f, start, end, flag=None):
        """下载 [start, end] 并写入文件"""
        headers = {'User-Agent': 'Mozilla/5.0', 'Range': f'bytes={start}-{end}'}
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=10) as rsp:
                if rsp.status != 206:
                    logger.warning(f'Server ignored Range request (status={rsp.status}), fallback required')
                    return -1
                f.seek(start)
                remaining = end - start + 1
                got = 0
                while remaining > 0:
                    data = rsp.read(min(1024 * 1024, remaining))
                    if not data:
                        break
                    f.write(data)
                    got += len(data)
                    self.downloaded_bytes += len(data)
                    remaining -= len(data)
                    self.update_progress(flag=flag, value=len(data))
                if remaining > 0:
                    return -1
                f.flush()
                return got
        except Exception as e:
            logger.error(f'Chunk download error ({start}-{end}): {str(e)}')
            return -1

    @staticmethod
    def is_chunks_cover_file(chunk_infos, file_size):
        """判断 chunkInfos 是否恰好完整、连续地覆盖整个文件"""
        if not chunk_infos:
            return False
        if int(chunk_infos[0]['start']) != 0:
            return False
        if int(chunk_infos[-1]['end']) != file_size - 1:
            return False
        for i in range(len(chunk_infos) - 1):
            if int(chunk_infos[i + 1]['start']) != int(chunk_infos[i]['end']) + 1:
                return False
        return True

    def update_file_by_chunks(self, url, file_path, file_info, flag=None):
        """分块增量更新文件: 返回下载字节数/失败-1"""
        chunk_infos = file_info.get('chunkInfos')
        if not chunk_infos:
            return -1
        target_size = int(file_info['size'])
        try:
            local_size = os.path.getsize(file_path)
        except OSError:
            return -1
        try:
            if local_size != target_size:
                logger.info(f'{file_path} resizing {local_size} -> {target_size}')
                with open(file_path, 'r+b') as f:
                    f.truncate(target_size)
            current = self.compute_chunks_md5(file_path, chunk_infos)
            need_ranges = []
            need_indexes = []
            for i, ci in enumerate(chunk_infos):
                if current[i] == ci['md5']:
                    continue
                need_indexes.append(i)
                start = int(ci['start'])
                end = int(ci['end'])
                if need_ranges and need_ranges[-1][1] + 1 == start:
                    need_ranges[-1][1] = end
                else:
                    need_ranges.append([start, end])
            total_chunks = len(chunk_infos)
            if not need_indexes:
                logger.info(f'{file_path} all {total_chunks} chunks match, skipped')
                self.cache_chunks_md5(file_path, chunk_infos)
                return 0
            need_bytes = sum(e - s + 1 for s, e in need_ranges)
            logger.info(
                f'{file_path} chunk diff: reused {total_chunks - len(need_indexes)}/{total_chunks} chunks, '
                f'download {need_bytes / 1024 / 1024:.1f} MB in {len(need_ranges)} range(s), '
                f'whole file {target_size / 1024 / 1024:.1f} MB'
            )
            # 差异过大时或许不做分块更好
            # if need_bytes >= target_size * some_ratio: return -1
            downloaded = 0
            with open(file_path, 'r+b') as f:
                for start, end in need_ranges:
                    got = self.download_range_into(url, f, start, end, flag=flag)
                    if got < 0:
                        logger.warning(f'{file_path} range download failed, falling back to whole file')
                        return -1
                    downloaded += got
            with open(file_path, 'rb') as f:
                for i in need_indexes:
                    ci = chunk_infos[i]
                    if self._md5_range(f, int(ci['start']), int(ci['end'])) != ci['md5']:
                        logger.warning(f'{file_path} chunk {i} still mismatch after download')
                        return -1
            if not self.is_chunks_cover_file(chunk_infos, target_size):
                if self.get_file_md5(file_path) != file_info['md5']:
                    logger.warning(f'{file_path} whole-file MD5 mismatch after chunk update')
                    return -1
            self.cache_chunks_md5(file_path, chunk_infos)
            return downloaded
        except Exception as e:
            logger.error(f'Chunk update error for {file_path}: {str(e)}')
            return -1
    
    def check_patch_applicable(self, name, manifest):
        missing = []
        bad_size = []
        checked = 0
        for group in manifest.get('groupInfos', []):
            if self.group_applied(name, group):
                continue
            for file in group.get('srcFiles', []):
                checked += 1
                file_path = self.game_folder_path.joinpath(Path(file['dest']))
                try:
                    local_size = file_path.stat().st_size
                except OSError:
                    missing.append(file['dest'])
                    continue
                expect_size = file.get('size')
                if expect_size is not None and local_size != int(expect_size):
                    bad_size.append(file['dest'])
        if missing or bad_size:
            logger.warning(
                f'资源包 {name} 的官方增量补丁不适用于本地文件（校验 {checked} 个旧文件：缺失 {len(missing)} 个，'
                f'大小不符 {len(bad_size)} 个），将使用常规更新'
            )
            for rel in (missing + bad_size)[:5]:
                logger.warning(f'不适用: {rel}')
            return False
        logger.info(f'资源包 {name} 增量补丁预检通过（{checked} 个旧文件齐备）')
        return True

    def init_incremental_update(self):
        self.support_incremental_patching = False
        self.patch_targets = []
        if not self.local_version:
            return
        if self.local_version == self.current_version:
            return
        targets = []
        for name in ('common', self.get_download_quality()):
            pack = self.resource_packs.get(name) or {}
            entry = None
            for candidate in (pack.get('patchConfig') or []):
                if candidate.get('version') == self.local_version and (candidate.get('ext') or {}):
                    entry = candidate
                    break
            if entry is None:
                logger.info(f'资源包 {name} 无适用于 {self.local_version} 的官方增量补丁，将由常规更新补齐')
                continue
            raw, _ = self.fetch_verified(
                entry['indexFile'],
                entry.get('indexFileMd5'),
                f'资源包 {name} 增量补丁清单'
            )
            if raw is None:
                logger.warning(f'资源包 {name} 增量补丁清单下载失败，将由常规更新补齐')
                continue
            try:
                manifest = json.loads(raw.decode('utf-8'))
            except (ValueError, UnicodeError):
                logger.warning(f'资源包 {name} 增量补丁清单解析失败，将由常规更新补齐')
                continue
            if not self.check_patch_applicable(name, manifest):
                continue
            targets.append({'pack': name, 'entry': entry, 'manifest': manifest})
        if not targets:
            logger.info('没有可用的官方增量补丁，将使用常规更新')
            return
        self.patch_targets = targets
        self.support_incremental_patching = True
        logger.info(f'启用官方增量补丁更新: {[target["pack"] for target in targets]}')
    
    def set_progress_callback(self, callback):
        self.progress_callback = callback

    def notify_progress(self, flag='update_patch'):
        if self.progress_callback:
            self.progress_callback(self._progress_snapshot(), flag)

    def _progress_snapshot(self):
        return {
            "download": self.download_game_progress,
            "verify": self.verify_game_progress,
            "update": self.update_game_progress,
            "update_patch": self.update_game_progress_patch,
        }

    def update_progress(self, flag, value):
        if flag == "download":
            self.download_game_progress.finished_size += value
        elif flag == "update":
            self.update_game_progress.finished_size += value
        elif flag == "verify":
            self.verify_game_progress.finished_size += value
        elif flag == "update_patch":
            self.update_game_progress_patch.finished_size += value

        if flag in ("update", "update_patch"):
            progress = self.update_game_progress if flag == "update" else self.update_game_progress_patch
            if progress.finished_size > progress.total_size:
                progress.total_size = progress.finished_size

        if self.progress_callback:
            self.progress_callback(self._progress_snapshot(), flag)

    def get_file_md5(self, file_path):
        md5_hash = hashlib.md5()
        try:
            with open(file_path, "rb") as file:
                for chunk in iter(lambda: file.read(4096), b""):
                    md5_hash.update(chunk)
            return md5_hash.hexdigest()
        except FileNotFoundError:
            logger.error(f"The file {file_path} does not exist.")
            return None
    
    def start_game_process(self):
        logger.info("Launching game...")
        if os.name == "nt":
            game_exe = self.game_folder_path / "Wuthering Waves.exe"
            self.game_process = subprocess.Popen(f'"{game_exe}" -krqlv={self.get_download_quality()}', shell=True)
        if os.name == "posix":
            base_dir = os.path.dirname(sys.argv[0])
            
            game_exe_path =  Path(base_dir) / "Wuthering Waves Game" / "Wuthering Waves.exe"
            steam_dir_path = Path(os.path.expanduser("~")) / '.steam' / 'steam'
            proton_path = self.settings.get('proton_path', '')
            
            steamAppid = self.settings.get('steamappid', '0')
            
            compatdata_path = Path(base_dir) / "compatdata" / steamAppid
            wine_prefix = compatdata_path / "pfx" 
            
            if not compatdata_path.exists():
                compatdata_path.mkdir(parents=True, exist_ok=True)
            compatdata_path = compatdata_path.resolve()
            
            os.environ["STEAM_COMPAT_DATA_PATH"] = str(compatdata_path)
            os.environ["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_dir_path)
            os.environ["STEAM_PROTON_PATH"] = str(proton_path)
            os.environ["WINEPREFIX"] = str(wine_prefix)
            
            if steamAppid != '0':
                os.environ["STEAMAPPID"] = steamAppid
            if self.settings.get('proton_media_use_gst', '0') == '1':
                os.environ["PROTON_MEDIA_USE_GST"] = "1"
            if self.settings.get('proton_enable_wayland', '0') == '1':
                os.environ["PROTON_ENABLE_WAYLAND"] = "1"
            if self.settings.get('proton_no_d3d12', '0') == '1':
                os.environ["PROTON_NO_D3D12"] = "1"
            if self.settings.get('mangohud', '0') == '1':
                os.environ["MANGOHUD"] = "1"
            
            os.environ["STEAMDECK"] = "1"
            
            logger.info(f"compatdata_path: {compatdata_path}")
            # steam_launch_command = f"SteamDeck=1 /home/deck/.local/share/Steam/ubuntu12_32/steam-launch-wrapper \
            #     -- /home/deck/.local/share/Steam/ubuntu12_32/reaper \
            #     SteamLaunch AppId={AppId} \
            #     -- /home/deck/.local/share/Steam/steamapps/common/SteamLinuxRuntime_sniper/_v2-entry-point \
            #     --verb=waitforexitandrun \
            #     -- {proton_path} \
            #     waitforexitandrun \
            #     {game_exe}"
            try:
                self.game_process = subprocess.Popen([
                    proton_path,
                    "waitforexitandrun",
                    game_exe_path,
                    f"-krqlv={self.get_download_quality()}"
                ])
                logger.info(f"Launched game with Proton: {proton_path} run {game_exe_path}")
            except Exception as e:
                logger.error(f"Failed to launch game with Proton: {e}")
                
    def stop_game_process(self):
        if hasattr(self, 'game_process') and self.game_process:
            logger.info("Stopping game process...")
            os.system("kill -9 $(pgrep -f 'Wuthering Waves')")
            self.game_process.terminate()
            self.game_process.wait()
            logger.info("Game process stopped.")
            self.state = LauncherState.STARTGAME
            
    def find_available_proton(self):
        # Find all available Proton versions
        geproton_versions = []
        geproton_dir_path = Path(os.path.expanduser("~")) / '.steam' / 'steam' / 'compatibilitytools.d'
        for entry in os.listdir(geproton_dir_path):
            if entry.startswith("GE-Proton"):
                proton_file_path = geproton_dir_path / entry / "proton"
                version_file_path = geproton_dir_path / entry / "version"
                if proton_file_path.exists() and version_file_path.exists():
                    with open(version_file_path, 'r') as f:
                        version_content = f.read().strip()
                        timestamp_str, version = version_content.split(' ')
                    proton_version_dict = {'proton_path': str(proton_file_path), 'timestamp':timestamp_str, 'version':version}
                    geproton_versions.append(proton_version_dict)
        geproton_versions.sort(key=lambda x: x['timestamp'], reverse=True)
        
        proton_versions = []
        proton_dir_path = Path(os.path.expanduser("~")) / '.steam' / 'steam' / 'steamapps' / 'common'
        for entry in os.listdir(proton_dir_path):
            if entry.startswith("Proton"):
                proton_file_path = proton_dir_path / entry / "proton"
                version_file_path = proton_dir_path / entry / "version"
                if proton_file_path.exists() and version_file_path.exists():
                    with open(version_file_path, 'r') as f:
                        version_content = f.read().strip()
                        timestamp_str, version = version_content.split(' ')
                    proton_version_dict = {'proton_path':str(proton_file_path), 'timestamp':timestamp_str, 'version':version}
                    proton_versions.append(proton_version_dict)
        proton_versions.sort(key=lambda x: x['timestamp'], reverse=True)
        
        return geproton_versions, proton_versions
    
    def has_available_proton(self):
        geproton_versions, proton_versions = self.find_available_proton()
        if len(geproton_versions) == 0 or len(proton_versions) == 0:
            return False
        
    def get_latest_proton(self):
        geproton_versions, proton_versions = self.find_available_proton()
        if len(geproton_versions) > 0:
            return geproton_versions[0]
        if len(proton_versions) > 0:
            return proton_versions[0]
        return None
    
    def update_settings(self):
        settings_file_path = Path(base_dir) / 'settings.json'
        with open(settings_file_path, 'w', encoding='utf-8') as f:
            json.dump(self.settings, f, ensure_ascii=False, indent=4)

if __name__ == '__main__':
    
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', default='install',help='install or update or patch-update')
    parser.add_argument('--folder', default='Wuthering Waves Game',help='set download folder')
    args = parser.parse_args()
    
    launcher = Launcher(game_folder=args.folder)
    launcher.verify_gamefile()
    # download game client file
    if args.mode == 'install':
        launcher.download_game()
        launcher.verify_gamefile()
        launcher.update_localVersion()  
    
    # update game client file
    if args.mode == 'update':
        launcher.update_game()
        launcher.verify_gamefile()
        launcher.update_localVersion()  
    
    # Incremental updates
    if args.mode == 'patch-update':
        launcher.update_game_with_patch()
        launcher.verify_gamefile()
        launcher.update_localVersion()