# 基础库
import re
import shutil
import threading
from abc import ABCMeta, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 第三方库
from apscheduler.triggers.cron import CronTrigger
from lxml import etree
from sqlalchemy.orm import Session

# 项目库
from app.core.config import settings
from app.core.context import MediaInfo, TorrentInfo, Context
from app.core.event import eventmanager, Event
from app.core.meta.metabase import MetaBase
from app.core.metainfo import MetaInfo, MetaInfoPath
from app.db.downloadhistory_oper import DownloadHistoryOper, DownloadHistory, DownloadFiles
from app.db import db_update
from app.db.models.plugindata import PluginData
from app.helper.downloader import DownloaderHelper
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.modules.filemanager.transhandler import TransHandler
from app.modules.qbittorrent import Qbittorrent
from app.modules.transmission import Transmission
from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaType
from app.schemas.types import SystemConfigKey
from app.utils.http import RequestUtils
from app.utils.string import StringUtils
from app.utils.system import SystemUtils


@dataclass
class TorrentFile:
    """
    文件列表
    """
    # 文件名称(包含子文件夹路径和文件后缀)
    name: str
    # 文件大小
    size: int
    # 文件优先级
    priority: int = 0


@dataclass
class TorrentInfo:
    """
    种子信息
    """
    # 种子名称
    name: str
    # 种子保存路径
    save_path: str
    # 种子大小
    total_size: int
    # 种子哈希
    hash: str
    # Torrent 自动管理
    auto_tmm: bool = False
    # 种子分类
    category: str = ""
    # 种子标签
    tags: List[str] = field(default_factory=list)
    # 种子文件列表
    files: List[TorrentFile] = field(default_factory=list)


class Downloader(metaclass=ABCMeta):
    @abstractmethod
    def set_auto_tmm(self, torrent_hash: str, enable: bool) -> None:
        """
        设置种子自动管理(仅qBittorrent支持)
        :param torrent_hash: 种子hash
        :param enable: 是否开启自动管理
        """
        pass

    @abstractmethod
    def set_torrent_save_path(self, torrent_hash: str, location: str, move: bool = True) -> None:
        """
        设置种子保存路径
        :param torrent_hash: 种子hash
        :param location: 路径字符串(绝对路径)
        :param move: 是否移动种子文件(仅transmission有效)
        """
        pass

    @abstractmethod
    def torrents_rename(self, torrent_hash: str, old_path: str, new_torrent_name: str) -> None:
        """
        重命名种子名称(仅qBittorrent支持)
        :param torrent_hash: 种子hash
        :param old_path: 原路径
        :param new_torrent_name: 新种子名称
        """
        pass

    @abstractmethod
    def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        """
        重命名种子文件
        :param torrent_hash: 种子hash
        :param old_path: 原路径
        :param new_path: 新路径
        """
        pass

    @abstractmethod
    def torrents_info(self, torrent_hash: str = None) -> List[TorrentInfo]:
        """
        获取种子信息
        :param torrent_hash: 种子hash
        """
        pass


class QbittorrentDownloader(Downloader):
    def __init__(self, qbc: Qbittorrent):
        self.qbc = qbc.qbc

    def set_auto_tmm(self, torrent_hash: str, enable: bool) -> None:
        self.qbc.torrents_set_auto_management(torrent_hashes=torrent_hash, enable=enable)

    def set_torrent_save_path(self, torrent_hash: str, location: str, move: bool = True) -> None:
        self.qbc.torrents_set_location(torrent_hashes=torrent_hash, location=location)

    def torrents_rename(self, torrent_hash: str, old_path: str, new_torrent_name: str) -> None:
        self.qbc.torrents_rename(torrent_hash=torrent_hash, new_torrent_name=new_torrent_name)

    def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        self.qbc.torrents_rename_file(torrent_hash=torrent_hash, old_path=old_path, new_path=new_path)

    def torrents_info(self, torrent_hash: str = None) -> List[TorrentInfo]:
        """
        获取种子信息
        """
        torrents = []
        torrents_info = self.qbc.torrents_info(torrent_hashes=torrent_hash) if torrent_hash else self.qbc.torrents_info()
        if torrents_info:
            for torrent_info in torrents_info:
                torrents.append(TorrentInfo(
                    name = torrent_info.get('name'),
                    save_path = Path(torrent_info.get('save_path')).as_posix(),
                    total_size = torrent_info.get('total_size'),
                    hash=torrent_info.get('hash'),
                    auto_tmm=torrent_info.get('auto_tmm'),
                    category=torrent_info.get('category'),
                    tags=torrent_info.get('tags').split(","),
                    files= [
                        TorrentFile(
                            name=file.get('name'),
                            size=file.get('size'),
                            priority=file.get('priority'))
                        for file in torrent_info.files
                    ]
                ))
            return torrents


class TransmissionDownloader(Downloader):
    def __init__(self, trc: Transmission):
        self.trc = trc.trc

    def set_auto_tmm(self, torrent_hash: str, enable: bool) -> None:
        """
        transmission_rpc 没有`Torrent自动管理`功能
        """
        pass

    def set_torrent_save_path(self, torrent_hash: str, location: str, move: bool = True) -> None:
        self.trc.move_torrent_data(ids=torrent_hash, location=location, move=move)

    def torrents_rename(self, torrent_hash: str, old_path: str, new_torrent_name: str) -> None:
        """
        transmission_rpc 没有`重命名种子`功能
        """
        pass

    def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        self.trc.rename_torrent_path(torrent_id=torrent_hash, location=old_path, name=new_path)

    def torrents_info(self, torrent_hash: str = None) -> List[TorrentInfo]:
        torrents = []
        try:
            if torrent_hash:
                # 单个种子查询需要异常处理
                try:
                    torrent_info = self.trc.get_torrent(torrent_id=torrent_hash)
                    torrents_info = [torrent_info] if torrent_info else []
                except KeyError:
                    # 种子未找到，返回空列表
                    logger.debug(f"Transmission 中未找到种子: {torrent_hash}")
                    torrents_info = []
            else:
                # 获取所有种子
                torrents_info = self.trc.get_torrents()

            if torrents_info:
                for torrent_info in torrents_info:
                    torrents.append(TorrentInfo(
                        name = torrent_info.name,
                        save_path = Path(torrent_info.download_dir).as_posix(),
                        tags=torrent_info.labels if torrent_info.labels else [''],
                        total_size = torrent_info.total_size,
                        hash=torrent_info.hashString,
                        # 种子文件列表
                        files= [
                            TorrentFile(
                                name=file.get('name'),
                                size=file.get('length'))
                            for file in torrent_info.fields.get('files')
                            if '_____padding_file_' not in file.get('name') # 排除padding文件
                        ]
                    ))
        except Exception as e:
            logger.error(f"获取 Transmission 种子信息时出错: {str(e)}")

        return torrents


class FormatDownPath(_PluginBase):
    # 插件名称
    plugin_name = "路径名称格式化"
    # 插件描述
    plugin_desc = "根据自定义格式修改MP下载种子的保存路径、种子名称、种子文件名(实验功能)"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/wikrin/MoviePilot-Plugins/main/icons/alter_1.png"
    # 插件版本
    plugin_version = "1.2.0"
    # 插件作者
    plugin_author = "Attente"
    # 作者主页
    author_url = "https://github.com/wikrin"
    # 插件配置项ID前缀
    plugin_config_prefix = "formatdownpath_"
    # 加载顺序
    plugin_order = 33
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _lock = threading.Lock()

    # 配置属性
    _cron: str = ""
    _cron_enabled: bool = False
    _event_enabled: bool = False
    _rename_torrent: bool = False
    _rename_file: bool = False
    _downloader: list = []
    _exclude_tags: str = ""
    _exclude_dirs: str = ""
    _format_save_path: str = ""
    _format_torrent_name: str = ""
    _format_movie_path: str = ""
    _format_tv_path: str = ""
    _site_subtitle_xpath = [
        '//td[@class="rowhead"][text()="字幕"]/following-sibling::td//a/@href',
        '//div[contains(@class, "torrent-subtitles")]//a[contains(@href, "download")]/@href',
    ]

    def init_plugin(self, config: dict = None):

        self.downloader_helper = DownloaderHelper()
        self.downloadhis = DownloadHistoryOper()
        # 停止现有任务
        self.stop_service()
        self.load_config(config)

    def load_config(self, config: dict):
        """加载配置"""
        if config:
            # 遍历配置中的键并设置相应的属性
            for key in (
                "cron",
                "cron_enabled",
                "event_enabled",
                "rename_torrent",
                "rename_file",
                "downloader",
                "exclude_dirs",
                "exclude_tags",
                "format_save_path",
                "format_torrent_name",
                "format_movie_path",
                "format_tv_path",
            ):
                setattr(self, f"_{key}", config.get(key, getattr(self, f"_{key}")))

    @staticmethod
    def get_render_mode() -> Tuple[str, Optional[str]]:
        """
        获取插件渲染模式
        :return: 1、渲染模式，支持：vue/vuetify，默认vuetify；2、vue模式下编译后文件的相对路径，默认为`dist/assets`，vuetify模式下为None
        """
        return "vue", "dist/assets"

    def get_form(self):
        return [], {}

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        if self._cron_enabled:
            return [
                {
                    "id": "FormatDownPath",
                    "name": "转种/辅种自定义格式化",
                    "trigger": CronTrigger.from_crontab(self._cron or "0 8 * * *"),
                    "func": self.cron_process_main,
                    "kwargs": {},
                }
            ]
        return []

    def stop_service(self):
        """退出插件"""
        pass

    def get_api(self):
        return[{
            "path": "/recover_from_history",
            "endpoint": self.recover_from_history,
            "methods": ["POST"],
            "auth": "bear",
            "summary": "从记录中恢复",
            "description": "根据记录恢复修改的种子",
            },
            {
                "path": "/processed_data",
                "endpoint": self.get_processed_data,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取种子记录",
                "description": "获取已处理成功的种子记录",
            },
            {
                "path": "/torrent_data",
                "endpoint": self.get_torrent_data,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取种子信息",
                "description": "根据种子hash获取种子信息",
            }]

    def get_command(self):
        pass

    def get_page(self):
        pass

    def get_state(self):
        return self._event_enabled or self._cron_enabled

    def on_download_added(self, **kwargs):
        return self._event_enabled if self._event_enabled else None

    def get_module(self) -> Dict[str, Any]:
        """
        获取插件模块声明，用于胁持系统模块实现（方法名：方法实现）
        """
        return {
            "download_added": self.on_download_added,
        }

    @eventmanager.register(EventType.DownloadAdded)
    def event_process_main(self, event: Event):
        """
        处理事件
        """
        if not self._event_enabled \
            or not event:
            return
        event_data = event.event_data or {}
        torrent_hash = event_data.get("hash")
        downloader = event_data.get("downloader")
        # 获取待处理数据
        context: Context = event_data.get("context")
        if self.main(downloader=downloader, torrent_hash=torrent_hash, meta=context.meta_info, media_info=context.media_info):
            # 保存已处理数据
            self.update_data(key="processed", value={torrent_hash: downloader})

        # 下载字幕
        self.download_subtitle(context=context, torrent_hash=torrent_hash)

    def cron_process_main(self):
        """
        定时任务处理下载器中的种子
        """
        # 失败数据列表
        _failures: dict[str, str] = {}
        # 获取待处理数据
        pending: dict[str, str] = self.get_data(key="pending") or {}
        # 获取已处理数据
        processed: dict[str, str] = self.get_data(key="processed") or {}
        _processed_num = 0

        def create_hash_mapping() -> Dict[str, List[str]]:
            """
            生成源种子hash表
            """
            # 辅种插件数据
            assist: List[PluginData] = self.get_data(key=None, plugin_id="IYUUAutoSeed") or []
            # 辅种数据映射表 key: 源种hash, value: 辅种hash列表
            _mapping: dict[str, List[str]] = {}

            if assist:
                for seed_data in assist:
                    hashes = []
                    for a in seed_data.value:
                        hashes.extend(a.get("torrents", []))
                    if seed_data.key in _mapping:
                        # 辅种插件中使用的源种子hash是下载的, 将辅种hash列表合并
                        _mapping[seed_data.key].extend(hashes)
                    else:
                        # 辅种插件中使用的源种子hash不是字典的键, 需要再次判断是不是辅种产生的种子
                        for _current_hash, _hashes in _mapping.items():
                            if seed_data.key in _hashes:
                                _mapping[_current_hash].extend(hashes)
                                break
                        else: # 不是辅种产生的种子, 作为源种子添加
                            _mapping[seed_data.key] = hashes
            return _mapping

        # 预处理集合
        processed_hashes = set(processed.keys()) if processed else set()
        pending_hashes = set(pending.keys()) if pending else set()
        # 预处理下载器列表
        valid_downloaders: List[Tuple[str, Downloader]] = []
        for d in self._downloader:
            self.set_downloader(d)
            if self.downloader is not None:
                valid_downloaders.append((d, self.downloader))
            else:
                logger.warning(f"下载器: {d} 不存在或未启用")
        if not valid_downloaders:
            logger.warning("没有有效的下载器")
            return

        # 获取源种子hash表
        assist_mapping = create_hash_mapping()
        # 构建反向映射：种子哈希到源哈希
        seed_to_source_hash = {}
        if assist_mapping:
            for source_hash, seeds in assist_mapping.items():
                for seed in seeds:
                    seed_to_source_hash[seed] = source_hash

        # 遍历有效下载器并处理种子
        for d_name, downloader in valid_downloaders:
            torrents_info = [
                torrent
                for torrent in downloader.torrents_info()
                if torrent.hash not in processed_hashes or torrent.hash in pending_hashes
            ]
            if not torrents_info:
                logger.info(f"下载器 {d_name} 没有待处理的种子")
                continue

            # 设置为当前下载器
            self.downloader = downloader
            logger.info(f"下载器 {d_name} 待处理种子数量: {len(torrents_info)}")
            for torrent_info in torrents_info:
                _hash = seed_to_source_hash.get(torrent_info.hash, torrent_info.hash)
                downloadhis = DownloadHistoryOper().get_by_hash(_hash or torrent_info.hash)
                # 执行处理
                if self.main(torrent_info=torrent_info, downloadhis=downloadhis):
                    # 添加到已处理数据库
                    processed[torrent_info.hash] = d_name
                    # 本次处理成功计数
                    _processed_num += 1
                else:
                    # 添加到失败数据库
                    _failures[torrent_info.hash] = d_name
        # 更新数据库
        if _failures:
            self.update_data("pending", _failures)
            logger.info(f"失败 {len(_failures)} 个")
        if processed:
            # 保存已处理数据库
            self.update_data("processed", processed)
            logger.info(f"成功 {_processed_num} 个, 合计 {len(processed)} 个种子已保存至历史")

    def main(self, downloader: str = None, downloadhis: DownloadHistory = None,
             torrent_hash: str =None, torrent_info: TorrentInfo = None,
             meta: MetaBase = None, media_info: MediaInfo = None) -> bool:
        """
        处理单个种子
        :param downloader: 下载器名称
        :param torrent_hash: 种子哈希
        :param torrent_info: 种子信息
        :param meta: 文件元数据
        :param media_info: 媒体信息
        :return: 处理结果
        """
        # 全程加锁
        with self._lock:
            success = True
            if downloader:
                # 设置下载器
                self.set_downloader(downloader)
            if self.downloader is None:
                success = False
                logger.warn(f"未连接下载器")
            if success and not torrent_info:
                if torrent_hash:
                    torrent_info = self.downloader.torrents_info(torrent_hash)
                    # 种子被手动删除或转移
                    if not torrent_info:
                        success = False
                        logger.warn(f"下载器 {downloader} 不存在该种子: {torrent_hash}")
                        return True
                    # 取第一个种子
                    torrent_info = torrent_info[0]
            # 保存目录排除
            if success and self._exclude_dirs:
                for exclude_dir in self._exclude_dirs.split("\n"):
                    if exclude_dir and exclude_dir in str(torrent_info.save_path):
                        success = False
                        logger.info(f"{torrent_info.name} 保存路径: {torrent_info.save_path} 命中排除目录：{exclude_dir}")
                        return True
            # 标签排除
            if success and self._exclude_tags and \
                (common_tags := {tag.strip() for tag in self._exclude_tags.split(",") if tag} & set(torrent_info.tags)):
                success = False
                logger.info(f"{torrent_info.tags} 命中排除标签：{common_tags}")
                return True
            # 备份种子数据
            self.save_data(key=torrent_info.hash, value=asdict(torrent_info))
            if success and downloadhis:
                # 使用历史记录的识别信息
                meta = MetaInfo(title=downloadhis.torrent_name, subtitle=downloadhis.torrent_description)
                media_info = self.chain.recognize_media(meta=meta, mtype=MediaType(downloadhis.type),
                                                        tmdbid=downloadhis.tmdbid, doubanid=downloadhis.doubanid)
            if success and not meta:
                logger.warn(f"未找到与之关联的下载种子 {torrent_info.name} 元数据识别可能不准确")
                meta = MetaInfo(torrent_info.name)
                if not meta:
                    logger.error(f"元数据获取失败，种子名称：{torrent_info.name}")
                    success = False
            if success and not media_info:
                media_info = self.chain.recognize_media(meta=meta)
                if not media_info:
                    logger.error(f"识别媒体信息失败，种子名称：{torrent_info.name}")
                    success = False
            if success:
                if self.format_torrent_all(torrent_info=torrent_info, meta=meta, media_info=media_info):
                    logger.info(f"种子 {torrent_info.name} 处理完成")
                    return True
            # 处理失败
            return False

    def format_torrent_all(self, torrent_info: TorrentInfo, meta: MetaBase, media_info: MediaInfo) -> bool:
        _torrent_hash = torrent_info.hash
        _torrent_name = torrent_info.name
        _format_file_path = self._format_movie_path if media_info.type == MediaType.MOVIE else self._format_tv_path
        need_update = False
        success = True
        # 关闭 Torrent自动管理
        if success and torrent_info.auto_tmm:
            try:
                logger.info(f"正在为种子 {_torrent_name} 关闭 Torrent自动管理")
                self.downloader.set_auto_tmm(torrent_hash=_torrent_hash, enable=False)
                logger.info(f"Torrent自动管理 关闭成功 - {_torrent_name}")
            except Exception as e:
                logger.error(f"Torrent自动管理 关闭失败，种子：{_torrent_name}，hash: {_torrent_hash}，错误：{str(e)}")
                success = False
        # 查询数据库
        downloadhis, downfiles = self.fetch_data(torrent_hash=_torrent_hash)
        # 附加并格式化种子保存路径
        if success and self._format_save_path:
            # 种子当前保存路径
            save_path = Path(torrent_info.save_path)
            # 种子新保存路径
            new_file_path = self.format_path(
                    template_string=self._format_save_path,
                    meta=meta,
                    mediainfo=media_info)
            _original_parts = save_path.parts
            _new_parts = new_file_path.parts
            # 计算公共路径长度
            common_length = 0
            for i in range(min(len(_original_parts), len(_new_parts))):
                if _original_parts[-(i + 1)] == _new_parts[i]:
                    common_length += 1
                else:
                    break
            # 去除重复部分
            if common_length:
                new_file_path = Path(*_new_parts[common_length:])
                logger.info(f"存在 {common_length} 个公共路径，去除重复部分：{_original_parts[-common_length:]}")
            new_path = save_path / new_file_path
            if new_path != save_path:
                try:
                    new_path = new_path.as_posix()
                    self.downloader.set_torrent_save_path(torrent_hash=_torrent_hash, location=new_path)
                    # 更新路径信息
                    downloadhis, downfiles = self.update_path(downloadhis=downloadhis, downfiles=downfiles, old_path=torrent_info.save_path, new_path=new_path)
                    need_update = True
                    logger.info(f"更改种子保存路径成功：{torrent_info.save_path} ==> {new_path}")
                except Exception as e:
                    logger.error(f"更改种子保存路径失败：{str(e)}")
                    success = False
        # 重命名种子文件
        if success and self._rename_file and _format_file_path:
            logger.info(f"{_torrent_name} 开始重命名种子文件...")
            torrent_files: list[TorrentFile] = torrent_info.files
            for file in torrent_files:
                _file_name = file.name
                # 使用系统整理屏蔽词
                transfer_exclude_words = self.systemconfig.get(SystemConfigKey.TransferExcludeWords)
                if transfer_exclude_words:
                    for keyword in transfer_exclude_words:
                        if not keyword:
                            continue
                        if keyword and re.search(r"%s" % keyword, _file_name, re.IGNORECASE):
                            logger.info(f"{_file_name} 命中屏蔽词 {keyword}，跳过")
                            break
                try:
                    file_path = Path(_file_name)
                    file_suffix = file_path.suffix
                    file_meta = MetaInfoPath(file_path)
                    _file_new_path = self.format_path(
                        template_string=_format_file_path,
                        meta=file_meta,
                        mediainfo=media_info,
                        file_ext=file_suffix)
                    new_file_path = str(_file_new_path)
                    old_path = file_path.as_posix()
                    # 跳过已重命名的文件
                    if new_file_path in old_path:
                        continue
                    self.downloader.rename_file(torrent_hash=_torrent_hash, old_path=_file_name, new_path=new_file_path)
                    # 更新路径信息
                    downloadhis, downfiles = self.update_path(downloadhis=downloadhis, downfiles=downfiles, old_path=_file_name, new_path=new_file_path)
                    need_update = True
                    logger.info(f"种子文件重命名成功：{_file_name} ==> {new_file_path}")
                except Exception as e:
                    logger.error(f"种子文件 {_file_name} 重命名失败：{str(e)}")
                    success = False
        # 重命名种子名称
        if success and self._rename_torrent:
            new_name = self.format_path(
                    template_string=self._format_torrent_name,
                    meta=meta,
                    mediainfo=media_info)
            try:
                if str(new_name) != _torrent_name:
                    self.downloader.torrents_rename(torrent_hash=_torrent_hash, old_path=_torrent_name, new_torrent_name=str(new_name))
                    logger.info(f"种子重命名成功：{_torrent_name} ==> {new_name}")
            except Exception as e:
                logger.error(f"种子重命名失败：{str(e)}")
                success = False
        # 更新数据库
        if need_update:
            self.update_db(torrent_hash=_torrent_hash, downloadhis=downloadhis, downfiles=downfiles)
        return success

    def download_subtitle(self, context: Context, torrent_hash: str):
        """
        添加下载任务成功后，从站点下载字幕，保存到下载目录
        :param context:  上下文，包括识别信息、媒体信息、种子信息
        :param torrent_hash: 种子hash
        """
        if not settings.DOWNLOAD_SUBTITLE:
            return

        download_history: DownloadHistory = self.downloadhis.get_by_hash(download_hash=torrent_hash)
        if not download_history:
            return

        download_dir = Path(download_history.path)
        # 没有详情页不处理
        torrent = context.torrent_info
        if not torrent.page_url:
            return
        # 字幕下载目录
        logger.info("开始从站点下载字幕：%s" % torrent.page_url)
        # 读取网站代码
        request = RequestUtils(cookies=torrent.site_cookie, ua=torrent.site_ua)
        res = request.get_res(torrent.page_url)
        if res and res.status_code == 200:
            if not res.text:
                logger.warn(f"读取页面代码失败：{torrent.page_url}")
                return
            html: etree._Element = etree.HTML(res.text)
            try:
                sublink_list = []
                for xpath in self._site_subtitle_xpath:
                    sublinks: list[str] = html.xpath(xpath)
                    if sublinks:
                        for sublink in sublinks:
                            if not sublink:
                                continue
                            if not sublink.startswith("http"):
                                base_url = StringUtils.get_base_url(torrent.page_url)
                                if sublink.startswith("/"):
                                    sublink = "%s%s" % (base_url, sublink)
                                else:
                                    sublink = "%s/%s" % (base_url, sublink)
                            sublink_list.append(sublink)
            finally:
                if html is not None:
                    del html
            # 下载所有字幕文件
            for sublink in sublink_list:
                logger.info(f"找到字幕下载链接：{sublink}，开始下载...")
                # 下载
                ret = request.get_res(sublink)
                if ret and ret.status_code == 200:
                    # 保存ZIP
                    file_name = TorrentHelper.get_url_filename(ret, sublink)
                    if not file_name:
                        logger.warn(f"链接不是字幕文件：{sublink}")
                        continue
                    if file_name.lower().endswith(".zip"):
                        # ZIP包
                        zip_file = settings.TEMP_PATH / file_name
                        # 保存
                        zip_file.write_bytes(ret.content)
                        # 解压路径
                        zip_path = zip_file.with_name(zip_file.stem)
                        # 解压文件
                        shutil.unpack_archive(zip_file, zip_path, format='zip')
                        # 目录仍然不存在，则创建目录
                        if not download_dir.exists():
                            download_dir.mkdir(parents=True, exist_ok=True)
                        # 遍历转移文件
                        for sub_file in SystemUtils.list_files(zip_path, settings.RMT_SUBEXT):
                            target_sub_file = download_dir / sub_file.name
                            if target_sub_file.exists():
                                logger.info(f"字幕文件已存在：{target_sub_file}")
                                continue
                            logger.info(f"转移字幕 {sub_file} 到 {target_sub_file} ...")
                            SystemUtils.copy(sub_file, target_sub_file)
                        # 删除临时文件
                        try:
                            shutil.rmtree(zip_path)
                            zip_file.unlink()
                        except Exception as err:
                            logger.error(f"删除临时文件失败：{str(err)}")
                    else:
                        sub_file = settings.TEMP_PATH / file_name
                        # 保存
                        sub_file.write_bytes(ret.content)
                        target_sub_file = download_dir / sub_file.name
                        logger.info(f"转移字幕 {sub_file} 到 {target_sub_file}")
                        SystemUtils.copy(sub_file, target_sub_file)
                else:
                    logger.error(f"下载字幕文件失败：{sublink}")
                    continue
            if sublink_list:
                logger.info(f"{torrent.page_url} 页面字幕下载完成")
            else:
                logger.warn(f"{torrent.page_url} 页面未找到字幕下载链接")
        elif res is not None:
            logger.warn(f"连接 {torrent.page_url} 失败，状态码：{res.status_code}")
        else:
            logger.warn(f"无法打开链接：{torrent.page_url}")

    def recover_from_history(self, request: Dict[str, str]):
        """
        从处理历史中恢复
        :param downloader: 下载器
        :param torrent_hash: 种子哈希
        """
        # 全程加锁
        with self._lock:
            torrent_hash = request.get("torrent_hash")
            downloader = request.get("downloader")
            if result := self.get_data(key=torrent_hash) or {}:
                his_info = TorrentInfo(**{
                        k: v if k != 'files' else [TorrentFile(**f) for f in v]
                        for k, v in result.items()
                    })
                # 查询转钟记录
                transfer_history = self.get_data(key=f"{downloader}-{torrent_hash}",
                                                plugin_id="TorrentTransfer")
                if transfer_history and isinstance(transfer_history, dict):
                    logger.info(f"查询到转种记录 {transfer_history}")
                    downloader = transfer_history['to_download']
                    torrent_hash = transfer_history['to_download_id']

                # 设置下载器
                self.set_downloader(downloader)
            else:
                msg = f"未找到种子 {torrent_hash} 的处理历史"
                logger.warn(msg)
                return False, msg
            if self.downloader is None:
                msg = f"下载器: {downloader} 不存在或未启用"
                logger.warn(msg)
                return False, msg
            if new_info := self.downloader.torrents_info(torrent_hash=his_info.hash):
                new_info = new_info[0]
                # 查询数据库
                downloadhis, downfiles = self.fetch_data(torrent_hash=torrent_hash)
            else:
                self.delete_data(key="processed", torrent_hash=torrent_hash)
                msg = f"下载器 {downloader} 不存在该种子: {torrent_hash}, 记录已删除"
                logger.warn(msg)
                return True, msg
            if new_info == his_info:
                self.delete_data(key="processed", torrent_hash=torrent_hash)
                msg= "与备份一致，跳过恢复, 记录已删除"
                logger.warn(msg)
                return True, msg
            success = True
            need_update = False
            # 恢复种子文件
            if success and len(new_info.files) == len(his_info.files):
                for n, o in zip(new_info.files, his_info.files):
                    if n.name == o.name:
                        continue
                    try:
                        self.downloader.rename_file(torrent_hash=new_info.hash, old_path=n.name, new_path=o.name)
                        downloadhis, downfiles = self.update_path(downloadhis=downloadhis, downfiles=downfiles, old_path=n.name, new_path=o.name)
                        need_update = True
                        logger.info(f"种子文件恢复成功：{n.name} ==> {o.name}")
                    except Exception as e:
                        msg = f"种子文件：{n.name} 恢复失败: {str(e)}"
                        success = False
            # 恢复种子保存路径
            if success and new_info.save_path != his_info.save_path:
                try:
                    self.downloader.set_torrent_save_path(torrent_hash=new_info.hash, location=his_info.save_path)
                    downloadhis, downfiles = self.update_path(downloadhis=downloadhis, downfiles=downfiles, old_path=new_info.save_path, new_path=his_info.save_path)
                    need_update = True
                    logger.info(f"保存路径恢复成功：{new_info.save_path} ==> {his_info.save_path}")
                except Exception as e:
                    msg = f"保存路径恢复失败: {str(e)}"
                    success = False
            # 恢复种子名称
            if success and new_info.name != his_info.name:
                try:
                    self.downloader.torrents_rename(torrent_hash=new_info.hash, old_path=new_info.name, new_torrent_name=his_info.name)
                    logger.info(f"恢复种子名称成功：{new_info.name} ==> {his_info.name}")
                except Exception as e:
                    msg = f"种子名称：{new_info.name} 恢复失败: {str(e)}"
                    success = False

            if need_update:
                self.update_db(torrent_hash=torrent_hash, downloadhis=downloadhis, downfiles=downfiles)
                if success:
                    # 删除处理记录
                    self.delete_data(key="processed",torrent_hash=torrent_hash)
                    msg = f"恢复完成, 记录已删除"
                    logger.info(msg)
                    return True, msg
                else:
                    return False, msg

    def get_processed_data(self) -> dict[str, str]:
        """
        获取已处理的种子哈希列表
        """
        return self.get_data(key="processed") or {}

    def get_torrent_data(self, torrent_hash: str) -> Optional[dict[str, Any]]:
        """
        获取种子数据
        """
        data: dict = self.get_data(key=torrent_hash)
        if data:
            return {"name": data.get('name'), "files_count": len(data.get('files')), "save_path": data.get('save_path')}

    def fetch_data(self, torrent_hash: str) -> Optional[Tuple[Dict[int, dict], Dict[int, dict]]]:
        """
        使用哈希查询数据库中的下载记录和文件记录
        """
        # 查询下载历史记录
        download_history: DownloadHistory = self.downloadhis.get_by_hash(download_hash=torrent_hash)
        his = {download_history.id: {"path": download_history.path}} if download_history else {}
        # 查询文件下载记录
        download_files: List[DownloadFiles] = self.downloadhis.get_files_by_hash(download_hash=torrent_hash)
        downfiles = {file.id: {"fullpath": file.fullpath, "savepath": file.savepath, "filepath": file.filepath} for file in download_files} if download_files else {}
        return his, downfiles

    def set_downloader(self, downloader: str):
        """
        获取下载器
        """
        if service := self.downloader_helper.get_service(name=downloader):
            if self.downloader_helper.is_downloader("qbittorrent", service.config):
                if service.instance.qbc:
                    self.downloader: Downloader = QbittorrentDownloader(qbc=service.instance)
                    return
            elif service.instance.trc:
                self.downloader: Downloader = TransmissionDownloader(trc=service.instance)
                return
        # 暂时设为None, 跳过
        self.downloader = None

    def update_data(self, key: str, value: dict = None):
        """
        更新插件数据
        """
        if not value:
            return
        plugin_data: dict = self.get_data(key=key) or {}
        if plugin_data:
            plugin_data.update(value)
            self.save_data(key=key, value=plugin_data)
        else:
            self.save_data(key=key, value=value)

    def delete_data(self, key: str, torrent_hash: str):
        """
        删除插件数据
        :param key: 插件数据键
        :param torrent_hash: 种子哈希
        """
        # 删除种子备份数据
        self.del_data(key=torrent_hash)
        # 从完成记录中移除
        plugin_data: dict = self.get_data(key=key) or {}
        if torrent_hash in plugin_data:
            del plugin_data[torrent_hash]
            self.save_data(key=key, value=plugin_data)

    def update_db(self, torrent_hash: str, downloadhis: Optional[Dict[int, dict]], downfiles: Optional[Dict[int, dict]]):
        """
        更新数据库
        """
        db = self.plugindata._db
        if downloadhis:
            for id, data in downloadhis.items():
                self.update_download_history_by_hash(db=db, db_id=id, torrent_hash=torrent_hash, payload=data)

        if downfiles:
            for id, data in downfiles.items():
                self.update_download_file_by_hash(db=db, db_id=id, torrent_hash=torrent_hash, payload=data)
    @staticmethod
    def format_path(
        template_string: str,
        meta: MetaBase,
        mediainfo: MediaInfo,
        file_ext: str = None,
    ) -> Path:
        """
        根据媒体信息，返回Format字典
        :param template_string: Jinja2 模板字符串
        :param meta: 文件元数据
        :param mediainfo: 识别的媒体信息
        :param file_ext: 文件扩展名
        """
        return TransHandler().get_rename_path(
            template_string=template_string,
            rename_dict=TransHandler().get_naming_dict(
                meta=meta, mediainfo=mediainfo, file_ext=file_ext
            ),
        )

    @staticmethod
    def update_path(downloadhis: Dict[int, dict], downfiles: dict, old_path: str, new_path: str) -> Tuple[Dict[int, dict], Dict[int, dict]]:

        def safe_replace(d: dict[str, str], old: str, new: str):
            """
            替换路径
            """
            for k, v in d.items():
                if old in v:
                    p = d[k]
                    d[k] = v.replace(old, new)
                    logger.debug(f"替换: {p} ==> {d[k]}")

        # 更新下载历史记录
        if downloadhis:
            for d in downloadhis.values():
                safe_replace(d, old_path, new_path)

        # 更新下载文件记录
        if downfiles:
            for d in downfiles.values():
                safe_replace(d, old_path, new_path)
        return downloadhis, downfiles

    @staticmethod
    @db_update
    def update_download_file_by_hash(db: Session, db_id: int, torrent_hash: str, payload: Dict[str, Any]):
        payload = {k: v for k, v in payload.items() if v is not None}
        db.query(DownloadFiles).filter(
            DownloadFiles.download_hash == torrent_hash \
                and DownloadFiles.id == db_id).update(payload)

    @staticmethod
    @db_update
    def update_download_history_by_hash(db: Session, db_id: int, torrent_hash: str, payload: Dict[str, Any]):
        payload = {k: v for k, v in payload.items() if v is not None}
        db.query(DownloadHistory).filter(
            DownloadHistory.download_hash == torrent_hash \
                and DownloadHistory.id == db_id).update(payload)

