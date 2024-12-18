# 基础库
from abc import ABCMeta, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

# 第三方库
from apscheduler.triggers.cron import CronTrigger
from qbittorrentapi import TorrentDictionary
from sqlalchemy.orm import Session
from transmission_rpc import Torrent


# 项目库
from app.core.context import MediaInfo, TorrentInfo, Context
from app.core.event import eventmanager, Event
from app.core.meta.metabase import MetaBase
from app.core.metainfo import MetaInfo, MetaInfoPath
from app.db.downloadhistory_oper import DownloadHistoryOper, DownloadHistory, DownloadFiles
from app.db import db_query, db_update
from app.db.models.plugindata import PluginData
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.modules.filemanager import FileManagerModule
from app.modules.qbittorrent import Qbittorrent
from app.modules.transmission import Transmission
from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaType
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
    def set_auto_tmm(self, torrent_hash: str, enable: bool) -> None:
        pass

    @abstractmethod
    def set_torrent_save_path(self, torrent_hash: str, location: str) -> None:
        """
        设置种子保存路径
        """
        pass

    @abstractmethod
    def torrents_rename(self, torrent_hash: str, old_path: str, new_torrent_name: str) -> None:
        """
        重命名种子
        """
        pass

    @abstractmethod
    def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        """
        重命名种子文件
        """
        pass

    @abstractmethod
    def torrents_info(self, torrent_hash: str) -> Optional[TorrentInfo]:
        """
        获取种子信息
        """
        pass


class QbittorrentDownloader(Downloader):
    def __init__(self, qbc: Qbittorrent):
        self.qbc = qbc.qbc

    def set_auto_tmm(self, torrent_hash: str, enable: bool) -> None:
        self.qbc.torrents_set_auto_management(torrent_hashes=torrent_hash, enable=enable)

    def set_torrent_save_path(self, torrent_hash: str, location: str) -> None:
        self.qbc.torrents_set_location(torrent_hashes=torrent_hash, location=location)
    
    def torrents_rename(self, torrent_hash: str, old_path: str, new_torrent_name: str) -> None:
        self.qbc.torrents_rename(torrent_hash=torrent_hash, new_torrent_name=new_torrent_name)

    def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        self.qbc.torrents_rename_file(torrent_hash=torrent_hash, old_path=old_path, new_path=new_path)

    def torrents_info(self, torrent_hash: str) -> Optional[TorrentInfo]:
        """
        根据哈希获取种子信息
        """
        torrent_info = self.qbc.torrents_info(torrent_hash=torrent_hash)
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

    def set_auto_tmm(self, torrent_hash: str, enable: bool) -> None:
        """
        transmission_rpc 没有`Torrent自动管理`功能
        """
        pass

    def set_torrent_save_path(self, torrent_hash: str, location: str) -> None:
        self.trc.move_torrent_data(ids=torrent_hash, location=location)

    def torrents_rename(self, torrent_hash: str, old_path: str, new_torrent_name: str) -> None:
        self.trc.rename_torrent_path(torrent_id=torrent_hash, location=old_path, name=new_torrent_name)

    def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        self.trc.rename_torrent_path(torrent_id=torrent_hash, location=old_path, name=new_path)

    def torrents_info(self, torrent_hash: str) -> Optional[TorrentInfo]:
        torrent_info: Torrent = self.trc.get_torrent(torrent_id=torrent_hash)
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
    plugin_version = "1.0.4"
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
    _cron: str = ""
    _cron_enabled: bool = False
    _event_enabled: bool = False
    _rename_torrent: bool = False
    _rename_file: bool = False
    _format_save_path: str = ""
    _format_torrent_name: str = ""
    _format_movie_path: str = ""
    _format_tv_path: str = ""
    _last_id: int = 0

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
                "format_save_path",
                "format_torrent_name",
                "format_movie_path",
                "format_tv_path",
                "last_id",
            ):
                setattr(self, f"_{key}", config.get(key, getattr(self, f"_{key}")))

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
                                'props': {'cols': 8, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'event_enabled',
                                            'label': '启用事件监听',
                                        },
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 8, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'cron_enabled',
                                            'label': '启用定时任务',
                                        },
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 8, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '0 8 * * *',
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
                                'props': {'cols': 8, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'rename_torrent',
                                            'label': '种子重命名',
                                        },
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 8, 'md': 4},
                                'content': [
                                    # {
                                    #     'component': 'VSwitch',
                                    #     'props': {
                                    #         'model': '',
                                    #         'label': '占位',
                                    #     },
                                    # }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 8, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'rename_file',
                                            'label': '种子文件重命名(实验功能)',
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
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'format_save_path',
                                            'label': '自定义保存路径格式',
                                            'placeholder': '使用Jinja2语法',
                                            'clearable': True,
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
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'format_torrent_name',
                                            'label': '自定义种子标题重命名格式',
                                            'placeholder': '使用Jinja2语法',
                                            'clearable': True,
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
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'format_movie_path',
                                            'label': '自定义电影文件重命名格式',
                                            'placeholder': '使用Jinja2语法',
                                            'clearable': True,
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
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'format_tv_path',
                                            'label': '自定义电视剧文件重命名格式',
                                            'placeholder': '使用Jinja2语法',
                                            'clearable': True,
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
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'warning',
                                            'variant': 'tonal',
                                            'text': '谨慎开启 种子文件重命名, 会导致无法辅种和其他意料之外的问题, 增加种子维护难度'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'warning',
                                            'variant': 'tonal',
                                            'text': '种子重命名 重命名种子在下载器显示的标题,qBittorrent 不会影响保存路径和种子文件结构; Transmission 会修改种子文件结构, 有辅种需求不要开启'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '事件监听 为MP添加下载任务触发, 仅处理添加下载的种子, 定时任务 为获取转种/辅种 插件数据并处理'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ],
            },
        ], {
            "corn": "0 8 * * *",
            "cron_enabled": False,
            "event_enabled": False,
            "rename_torrent": False,
            "rename_file": False,
            "format_torrent_name": self._format_torrent_name or "{{ title }}{% if year %} ({{ year }}){% endif %} - {% if __meta__.begin_season %}S{{ __meta__.begin_season }}{% endif %}{% if __meta__.end_season %} - S{{ __meta__.end_season }}{% endif %}{% if __meta__.begin_episode %}E{{ __meta__.begin_episode }}{% endif %}{% if __meta__.end_episode %} - E{{ __meta__.end_episode }}{% endif %}",
            "format_save_path": self._format_save_path or "{{title}}{% if year %} ({{year}}){% endif %}",
            "format_movie_path": self._format_movie_path or "{{title}}{% if year %} ({{year}}){% endif %}{% if part %}-{{part}}{% endif %}{% if videoFormat %} - {{videoFormat}}{% endif %}{{fileExt}}",
            "format_tv_path": self._format_tv_path or "Season {{season}}/{{title}} - {{season_episode}}{% if part %}-{{part}}{% endif %}{% if episode %} - 第 {{episode}} 集{% endif %}{{fileExt}}",
        }

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
        return self._event_enabled or self._cron_enabled

    @eventmanager.register(EventType.DownloadAdded)
    def event_process_main(self, event: Event):
        """
        处理事件
        """
        if not self._event_enabled \
            and not event:
            return
        event_data = event.event_data or {}
        hash = event_data.get("hash")
        downloader = event_data.get("downloader")
        # 获取待处理数据
        if self._event_enabled:
            context: Context = event_data.get("context")
            if self.main(downloader, hash, meta=context.meta_info, media_info=context.media_info):
                return
        # 保存未完成数据
        pending = self.get_data(key="pending").value or {}
        pending[hash] = downloader
        self.update_data(pending)

    def cron_process_main(self):
        """
        定时任务处理流程
        """
        _failures = {}
        # 使用插件数据
        plugin_ids = ["TorrentTransfer", "IYUUAutoSeed"]
        # 获取待处理数据
        pending = self.get_data(key="pending")
        pending = pending.value if pending else {}
        # 获取插件数据
        plugin_data = self.get_plugin_data(db=self.plugindata._db, plugin_ids=plugin_ids, last_id=self._last_id)
        if plugin_data:
            self._last_id = plugin_data[-1].id
            pending.update(self.process_plugin_data(plugin_data))
        if pending:
            for hash, downloader in pending.items():
                if not self.main(downloader, hash):
                    _failures[hash] = downloader
        if _failures:
            self.update_data(_failures)

    def process_plugin_data(self, plugins_data: List[PluginData]) -> Dict[str, str]:
        """
        处理从数据库获取的插件数据
        """
        data = {}
        for pd in plugins_data:
            if pd.plugin_id == "TorrentTransfer":
                data[pd.value.get("to_download_id")] = pd.value.get("to_download")
            else:
                downloader = pd.value[0].get("downloader")
                for hash in pd.value[0].get("torrents"):
                    data[hash] = downloader
        return data

    def main(self, downloader: str, hash: str, meta: MetaBase = None, media_info: MediaInfo = None) -> bool:
        """
        处理单个种子
        :param downloader: 下载器名称
        :param hash: 种子哈希
        :param meta: 文件元数据
        :param media_info: 媒体信息
        :return: 处理结果
        """
        success = False
        self.get_downloader(downloader)
        if self.downloader:
            success = True
            logger.info(f"已连接下载器: {downloader}")
        if success:
            torrent_info = self.downloader.torrents_info(hash)
            # 缺少细节处理, 例如种子被手动删除或转移
            if not torrent_info:
                logger.error(f"种子信息获取失败，种子哈希：{hash}")
                success = False
        if success and not meta:
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

    def update_data(self, key: str = "pending", value: dict = None):
        """
        更新插件数据
        """
        if not value:
            return
        plugin_data: PluginData = self.get_data(key=key)
        if plugin_data:
            plugin_data.value.update(value)
            self.save_data(key=key, value=value)
        else:
            self.save_data(key=key, value=value)

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
        logger.debug(rename_dict)
        return FileManagerModule.get_rename_path(template_string, rename_dict)

    def format_torrent_all(self, torrent_info: TorrentInfo, meta: MetaBase, media_info: MediaInfo) -> bool:
        _torrent_hash = torrent_info.hash
        _torrent_name = torrent_info.name
        _auto_tmm = torrent_info.auto_tmm
        _format_file_path = self._format_movie_path if media_info.type == MediaType.MOVIE else self._format_tv_path
        success = True
        # 关闭 Torrent自动管理
        if success and _auto_tmm:
            try:
                logger.info(f"正在为种子 {_torrent_name} 关闭 Torrent自动管理")
                self.downloader.set_auto_tmm(torrent_hash=_torrent_hash, enable=False)
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
            # 查询数据库
            downloadhis, downfiles = self.fetch_data(torrent_hash=_torrent_hash)
            if new_path != save_path:
                try:
                    new_path = str(new_path)
                    logger.info(f"开始更改种子 {_torrent_name} 保存路径：{save_path} ==> {new_path}")
                    self.downloader.set_torrent_save_path(torrent_hash=_torrent_hash, location=new_path)
                    # 更新路径信息
                    downloadhis, downfiles = self.update_path(downloadhis=downloadhis, downfiles=downfiles, old_path=torrent_info.save_path, new_path=new_path)
                    logger.info(f"更改种子保存路径成功：{_torrent_name}，新路径：{new_path}")
                except Exception as e:
                    logger.error(f"更改种子保存路径失败：{str(e)}")
                    success = False
        # 重命名种子文件
        if success and self._rename_file and _format_file_path:
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
                    file_meta = MetaInfoPath(file_path)
                    _file_new_path = self.format_path(
                        template_string=_format_file_path,
                        meta=file_meta,
                        mediainfo=media_info,
                        file_ext=file_suffix)
                    new_file_path = str(_file_new_path)
                    old_path = str(file_path)
                    # 跳过已重命名的文件
                    if new_file_path in old_path:
                        continue
                    self.downloader.rename_file(torrent_hash=_torrent_hash, old_path=_file_name, new_path=new_file_path)
                    # 更新路径信息
                    downloadhis, downfiles = self.update_path(downloadhis=downloadhis, downfiles=downfiles, old_path=_file_name, new_path=new_file_path)
                    logger.info(f"种子文件重命名成功：{_file_name} ==> {new_file_path}")
                except Exception as e:
                    logger.error(f"种子文件 {_file_name} 重命名失败：{str(e)}")
                    success = False
        # 重命名种子名称
        if success and self._rename_torrent:
            logger.info(f"{_torrent_name} 开始重命名种子名称")
            new_name = self.format_path(
                    template_string=self._format_torrent_name,
                    meta=meta,
                    mediainfo=media_info)
            try:
                logger.critical(str(new_name))
                if str(new_name) != _torrent_name:
                    self.downloader.torrents_rename(torrent_hash=_torrent_hash, old_path=_torrent_name, new_torrent_name=str(new_name))
                    logger.info(f"重命名成功：{_torrent_name} ==> {new_name}")
            except Exception as e:
                logger.error(f"重命名失败：{str(e)}")
                success = False
        # 更新数据库
        self.update_db(torrent_hash=_torrent_hash, downloadhis=downloadhis, downfiles=downfiles)
        return success
    
    def update_path(self, downloadhis: Dict[int, dict], downfiles: dict, old_path: str, new_path: str) -> Tuple[Dict[int, dict], Dict[int, dict]]:

        def safe_replace(d: dict, old: str, new: str):
            """
            替换路径
            """
            for k, v in d.items():
                if isinstance(v, str):
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
    @db_query
    def get_plugin_data(db: Session, plugin_ids: list[str], last_id: int = 0) -> List[PluginData]:
        return db.query(PluginData).filter(
            (PluginData.id > last_id) &
            (PluginData.plugin_id.in_(plugin_ids))
        ).all()

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

