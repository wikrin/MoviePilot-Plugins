# 基础库
from abc import ABCMeta, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

# 第三方库
from qbittorrentapi import TorrentDictionary
from transmission_rpc import Torrent

# 项目库
from app.core.context import MediaInfo, TorrentInfo, Context
from app.core.event import eventmanager, Event
from app.core.meta.metabase import MetaBase
from app.core.metainfo import MetaInfo, MetaInfoPath
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.modules.filemanager import FileManagerModule
from app.modules.qbittorrent import Qbittorrent
from app.modules.transmission import Transmission
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.schemas.types import SystemConfigKey


@dataclass
class TorrentFile():
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
class TorrentInfo():
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
    def set_auto_tmm(self, torrent_hashes: str, enable: bool) -> None:
        pass

    @abstractmethod
    def set_torrent_save_path(self, torrent_hashes: str, location: str) -> None:
        """
        设置种子保存路径
        """
        pass

    @abstractmethod
    def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        """
        重命名种子文件
        """
        pass

    @abstractmethod
    def torrents_info(self, torrent_hashes: str) -> Optional[TorrentInfo]:
        """
        获取种子信息
        """
        pass


class QbittorrentDownloader(Downloader):
    def __init__(self, qbc: Qbittorrent):
        self.qbc = qbc.qbc

    def set_auto_tmm(self, torrent_hashes: str, enable: bool) -> None:
        self.qbc.torrents_set_auto_management(torrent_hashes=torrent_hashes, enable=enable)

    def set_torrent_save_path(self, torrent_hashes: str, location: str) -> None:
        self.qbc.torrents_set_location(torrent_hashes=torrent_hashes, location=location)

    def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        self.qbc.torrents_rename_file(torrent_hash=torrent_hash, old_path=old_path, new_path=new_path)

    def torrents_info(self, torrent_hashes: str) -> Optional[TorrentInfo]:
        """
        根据哈希获取种子信息
        """
        torrent_info = self.qbc.torrents_info(torrent_hashes=torrent_hashes)
        if torrent_info:
            torrent_info: TorrentDictionary = torrent_info[0]
            torrent_info = TorrentInfo(
                name = torrent_info.get('name'),
                save_path = torrent_info.get('save_path'),
                total_size = torrent_info.get('total_size'),
                hash=torrent_info.get('hash'),
                auto_tmm=torrent_info.get('auto_tmm'),
                category=torrent_info.get('category'),
                tags=torrent_info.get('tags'),
                files= [
                    TorrentFile(
                        name=file.get('name'),
                        size=file.get('size'),
                        priority=file.get('priority'))
                    for file in torrent_info.files
                ]
            )
            return torrent_info


class TransmissionDownloader(Downloader):
    def __init__(self, trc: Transmission):
        self.trc = trc.trc

    def set_auto_tmm(self, torrent_hashes: str, enable: bool) -> None:
        """
        transmission_rpc 没有`Torrent自动管理`功能
        """
        pass

    def set_torrent_save_path(self, torrent_hashes: str, location: str) -> None:
        self.trc.move_torrent_data(ids=torrent_hashes, location=location)

    def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        self.trc.rename_torrent_path(torrent_id=torrent_hash, location=old_path, name=new_path)

    def torrents_info(self, torrent_hashes: str) -> Optional[TorrentInfo]:
        torrent_info: Torrent = self.trc.get_torrent(torrent_id=torrent_hashes)
        if torrent_info:
            torrent_info = TorrentInfo(
                name = torrent_info.name,
                save_path = torrent_info.download_dir,
                tags=torrent_info.group,
                total_size = torrent_info.total_size,
                hash=torrent_info.hashString,
                # 种子文件列表
                files= [
                    TorrentFile(
                        name=file.get('name'),
                        size=file.get('length'))
                    for file in torrent_info.fields.get('files')
                ]
            )
            return torrent_info


class FormatDownPath(_PluginBase):
    # 插件名称
    plugin_name = "自定义下载路径"
    # 插件描述
    plugin_desc = "根据格式修改下载器保存路径"
    # 插件图标
    plugin_icon = "DownloaderHelper.png"
    # 插件版本
    plugin_version = "1.0.0"
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
    _scheduler = None

    # 配置属性
    _enabled: bool = False
    _rename_file: bool = False
    _format_save_path: str = "{{title}}{% if year %} ({{year}}){% endif %}"
    _format_file_path: str = "{% if season %}Season {{season}}/{% endif %}{{title}} - {{season_episode}}{% if part %}-{{part}}{% endif %}{% if episode %} - 第 {{episode}} 集{% endif %}{% if videoFormat %} - {{videoFormat}}{% endif %}{{fileExt}}"

    def init_plugin(self, config: dict = None):

        self.downloader_helper = DownloaderHelper()
        # 停止现有任务
        self.stop_service()
        self.load_config(config)

    def load_config(self, config: dict):
        """加载配置"""
        if config:
            # 遍历配置中的键并设置相应的属性
            for key in (
                "enabled",
                "rename_file",
                "format_save_path",
                "format_file_path",
            ):
                setattr(self, f"_{key}", config.get(key, getattr(self, f"_{key}")))

    def __update_config(self):
        """更新设置"""
        self.update_config(
            {
                "enabled": self._enabled,
                "format_path": self._format_save_path,
                "format_file_path": self._format_file_path,
            }
        )

    def get_form(self):
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        },
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'rename_file',
                                            'label': '种子文件重命名',
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'format_save_path',
                                            'label': '自定义保存路径格式',
                                            'placeholder': '使用Jinja2语法',
                                        },
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'format_file_path',
                                            'label': '自定义文件重命名格式',
                                            'placeholder': '使用Jinja2语法',
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            },
        ], {
            "enabled": False,
            "rename_file": False,
            "format_save_path": self._format_save_path,
            "format_file_path": self._format_file_path,
        }

    def stop_service(self):
        """退出插件"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"退出插件失败：{str(e)}")

    def get_api(self):
        pass

    def get_command(self):
        pass

    def get_page(self):
        pass

    def get_state(self):
        return self._enabled

    @eventmanager.register(EventType.DownloadAdded)
    def main(self, event: Event):
        """
        处理事件
        """
        if not self._enabled or not event:
            return
        event_data = event.event_data or {}
        downloader_name = event_data.get("downloader")
        torrent_hashes = event_data.get("hash")
        context: Context = event_data.get("context")
        self.downloader = self.get_downloader(downloader_name)
        if not self.downloader:
            logger.error(f"连接下载器 {downloader_name} 失败")
            return
        torrent_info = self.downloader.torrents_info(torrent_hashes)
        if not torrent_info:
            logger.error(f"种子信息获取失败，种子哈希：{torrent_hashes}")
            return
        if self.format_torrent_all(torrent_info=torrent_info, meta=context.meta_info, media_info=context.media_info):
            logger.info(f"种子 {torrent_info.name} 格式化成功")

    def get_downloader(self, downloader: str):
        """
        获取下载器
        """
        service = self.downloader_helper.get_service(name=downloader)
        if service:
            self.service_info = service.config
            is_qbittorrent = self.downloader_helper.is_downloader("qbittorrent", self.service_info)
            if is_qbittorrent:
                self.downloader: Downloader = QbittorrentDownloader(qbc=service.instance)
            else:
                self.downloader: Downloader = TransmissionDownloader(trc=service.instance)
        else:
            logger.error(f"下载器 {downloader} 不存在")
            return

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
        def format_dict(meta: MetaBase, mediainfo: MediaInfo, file_ext: str = None) -> Dict[str, Any]:
            return FileManagerModule._FileManagerModule__get_naming_dict(
                meta=meta, mediainfo=mediainfo, file_ext=file_ext)

        rename_dict = format_dict(meta=meta, mediainfo=mediainfo, file_ext=file_ext)
        return FileManagerModule.get_rename_path(template_string, rename_dict)

    def format_torrent_all(self, torrent_info: TorrentInfo, meta: MetaBase, media_info: MediaInfo) -> bool:
        _torrent_hash = torrent_info.hash
        _torrent_name = torrent_info.name
        _auto_tmm = torrent_info.auto_tmm
        success = True
        # 关闭 Torrent自动管理
        if success and _auto_tmm:
            try:
                logger.info(f"正在为种子 {_torrent_name} 关闭 Torrent自动管理")
                self.downloader.set_auto_tmm(torrent_hashes=_torrent_hash, enable=False)
                logger.info(f"Torrent自动管理 关闭成功 - {_torrent_name}")
            except Exception as e:
                logger.error(f"Torrent自动管理 关闭失败，种子：{_torrent_name}，hash: {_torrent_hash}，错误：{str(e)}")
                success = False
        if success:
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
            new_path = save_path / new_file_path
            if new_path != save_path:
                try:
                    new_path = str(new_path)
                    logger.info(f"开始更改种子 {_torrent_name} 保存路径：{save_path} ==> {new_path}")
                    self.downloader.set_torrent_save_path(torrent_hashes=_torrent_hash, location=new_path)
                    logger.info(f"更改种子保存路径成功：{_torrent_name}，新路径：{new_path}")
                except Exception as e:
                    logger.error(f"更改种子保存路径失败：{str(e)}")
                    success = False
        # 重命名种子文件
        if success and self._rename_file and self._format_file_path:
            logger.info(f"{_torrent_name} 开始重命名种子文件")
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
                    meta = MetaInfoPath(file_path)
                    _file_new_path = self.format_path(
                        template_string=self._format_file_path,
                        meta=meta,
                        mediainfo=media_info,
                        file_ext=file_suffix)

                    self.downloader.rename_file(torrent_hash=_torrent_hash, old_path=_file_name, new_path=str(_file_new_path))
                    logger.info(f"种子文件重命名成功：{_file_name} ==> {_file_new_path}")
                except Exception as e:
                    logger.error(f"种子文件 {_file_name} 重命名失败：{str(e)}")
                    success = False
        return success

if __name__ == "__main__":
    # 测试用例
    fdp = FormatDownPath()
    fdp.init_plugin()
    fdp._enabled = True
    fdp._rename_file = True
    fdp._format_save_path = "{{title}}{% if year %} ({{year}}){% endif %}"
    fdp._format_file_path = "{% if season %}Season {{season}}/{% endif %}{{title}} - {{season_episode}}{% if part %}-{{part}}{% endif %}{% if episode %} - 第 {{episode}} 集{% endif %}{% if videoFormat %} - {{videoFormat}}{% endif %}{{fileExt}}"
    # fdp.get_downloader("local")
    fdp.get_downloader("tr")
    torrent_info = fdp.downloader.torrents_info(
        torrent_hashes="28d087144d2a4b047702f4aca4d5a9e691877342"
    )
    meta = MetaInfo(
        "Orb.On.the.Movements.of.the.Earth.S01.2024.1080p.WEB-DL.H264.AAC-ADWeb"
    )
    media_info = fdp.chain.recognize_media(meta=meta)
    fdp.format_torrent_all(torrent_info=torrent_info, meta=meta, media_info=media_info)
