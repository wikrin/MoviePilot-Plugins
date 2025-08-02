# 基础库
import asyncio
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Union

# 第三方库
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import tuple_
from sqlalchemy.orm import Session

# 项目库
from app.chain.mediaserver import MediaServerChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import eventmanager, Event
from app.db.models.subscribehistory import SubscribeHistory
from app.db.site_oper import SiteOper
from app.db.subscribe_oper import SubscribeOper
from app.db import db_query
from app.helper.service import ServiceConfigHelper
from app.log import logger
from app.modules.themoviedb.tmdbapi import TmdbApi
from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaType, NotificationType


class FollowUp(_PluginBase):
    # 插件名称
    plugin_name = "续作跟进"
    # 插件描述
    plugin_desc = "根据媒体库或订阅历史检查系列续作并通知订阅"
    # 插件图标
    plugin_icon = ""
    # 插件版本
    plugin_version = "1.1.6"
    # 插件作者
    plugin_author = "Attente"
    # 作者主页
    author_url = "https://github.com/wikrin"
    # 插件配置项ID前缀
    plugin_config_prefix = "followup_"
    # 加载顺序
    plugin_order = 99
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _scheduler = None
    _last_request_time = 0
    _request_lock = asyncio.Lock()
    _min_interval = 0.025

    # 配置属性
    _enabled: bool = False
    _after_days: int = 2
    _threshold_years: int = 15
    _cron: str = ""
    _onlyonce: bool = False
    _check_sub_history: bool = True
    _libraries: list = []
    _save_path: str = ""
    _sites: list = []

    CONFIG_KEYS = (
        "enabled",
        "after_days",
        "threshold_years",
        "cron",
        "onlyonce",
        "check_sub_history",
        "libraries",
        "save_path",
        "sites",
    )

    def init_plugin(self, config: dict = None):

        # 停止现有任务
        self.stop_service()
        self.load_config(config)

        self.tmdbapi = TmdbApi()

        if self._onlyonce:
            self.schedule_once()

    def load_config(self, config: dict):
        """加载配置"""
        if config:
            # 遍历配置中的键并设置相应的属性
            for key in self.CONFIG_KEYS:
                setattr(self, f"_{key}", config.get(key, getattr(self, f"_{key}")))
            # 获得所有站点
            site_ids = {site.id for site in SiteOper().list_order_by_pri()}
            # 过滤已删除的站点
            self._sites = [site_id for site_id in self._sites if site_id in site_ids]
            # 更新配置
            self.__update_config()

    def schedule_once(self):
        """调度一次性任务"""
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        logger.info("续作跟进，立即运行一次")
        self._scheduler.add_job(
            func=self.follow_up,
            trigger='date',
            run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
        )
        self._scheduler.start()

        # 关闭一次性开关
        self._onlyonce = False
        self.__update_config()

    def __update_config(self):
        """更新设置"""
        self.update_config({key: getattr(self, f"_{key}") for key in self.CONFIG_KEYS})

    def get_form(self):
        # 获取所有启用的媒体服务器及其库信息
        mediaservers = ServiceConfigHelper.get_mediaserver_configs() or []
        libraryitems = [
            {"title": library.name, "value": library.id, "subtitle": mediaserver.name}
            for mediaserver in mediaservers
            if mediaserver and mediaserver.enabled
            for library in (MediaServerChain().librarys(mediaserver.name) or [])
        ]

        # 列出所有站点
        sites_options = [
            {"title": site.name, "value": site.id}
            for site in SiteOper().list_order_by_pri()
        ]

        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 6, 'md': 4},
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
                                'props': {'cols': 6, 'md': 4},
                                'content': [
                                    {
                                        # 'component': 'VTextField', # 组件替换为VCronField
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动',
                                        },
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 6, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
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
                                'props': {'cols': 6, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'check_sub_history',
                                            'label': '检查订阅历史',
                                        },
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 6, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'after_days',
                                            'label': '提前提醒(天)',
                                            'type': 'number',
                                            'min': 1,
                                            'max': 30,
                                            'step': 1,
                                        },
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 6, 'md': 5},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'threshold_years',
                                            'label': '检查年限',
                                            'placeholder': '播出或上映时间超出年限则不再检查',
                                            'type': 'number',
                                            'min': 2,
                                            'max': 50,
                                            'step': 1,
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
                                'props': {'cols': 12, 'md': 12},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'libraries',
                                            'label': '选择媒体库',
                                            'chips': True,
                                            'multiple': True,
                                            'clearable': True,
                                            'items': libraryitems,
                                            'item-props': True
                                        }
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
                                            'model': 'save_path',
                                            'label': '保存目录',
                                            'placeholder': '留空自动',
                                        },
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'sites',
                                            'label': '选择站点',
                                            'chips': True,
                                            'multiple': True,
                                            'clearable': True,
                                            'items': sites_options,
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
            "after_days": 2,
            "threshold_years": 15,
            "cron": "",
            "onlyonce": False,
            "check_sub_history": True,
            "libraries": [],
            "save_path": "",
            "sites": [],
        }

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        if self._enabled:
            trigger = CronTrigger.from_crontab(self._cron) if self._cron else "interval"
            kwargs = {"hours": 24} if not self._cron else {}
            return [
                {
                    "id": "FollowUp",
                    "name": "续作跟进",
                    "trigger": trigger,
                    "func": self.follow_up,
                    "kwargs": kwargs,
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
        return [
            {
                "cmd": "/follow_up",
                "event": EventType.PluginAction,
                "desc": "续作跟进",
                "category": "",
                "data": {"action": "follow_up"}
            }
        ]

    def get_page(self):
        pass

    def get_state(self):
        return self._enabled

    @eventmanager.register(EventType.PluginAction)
    def action_event_handler(self, event: Event):
        """
        远程命令处理
        """
        event_data = event.event_data
        if not event_data or event_data.get("action") != "follow_up":
            return

        self.post_message(channel=event_data.get("channel"),
                          title=f"【续作跟进】开始执行 ...",
                          userid=event_data.get("user"))
        # 运行任务
        self.follow_up()

        self.post_message(channel=event_data.get("channel"),
                          title="【续作跟进】执行完成",
                          userid=event_data.get("user"))

    async def _fetch_tmdb_info(self, mtype: str, tmdbid: int) -> Optional[dict]:
        # 频率限制
        async with self._request_lock:
            now = time.time()
            elapsed = now - self._last_request_time
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
            self._last_request_time = time.time()

        try:
            if mtype == MediaType.MOVIE.value:
                if details := await self.tmdbapi.movie.async_details(tmdbid):
                    return {
                        "title_year": f"{details.get('title')} ({details.get('release_date')[:4]})",
                        "type": MediaType.MOVIE,
                        "tmdb_id": tmdbid,
                        "release_date": details.get("release_date"),
                        "belongs_to_collection": details.get("belongs_to_collection")
                    }
            elif mtype == MediaType.TV.value:
                if details := await self.tmdbapi.tv.async_details(tmdbid):
                    return {
                        "title_year": f"{details.get('name')} ({details.get('first_air_date')[:4]})",
                        "type": MediaType.TV,
                        "tmdb_id": tmdbid,
                        "last_air_date": details.get("last_air_date"),
                        "next_episode_to_air": details.get("next_episode_to_air")
                    }
            return None
        except Exception as e:
            logger.debug(f"获取TMDB信息失败 ({mtype} {tmdbid}): {e}")
            return None

    def follow_up(self):
        # 获取忽略列表
        _ignore = self.get_ignore_keys()
        # 获取系列合集
        collections = self.get_collections()
        # 获取需要跟进的媒体
        his = self._need_follow_up(_ignore, collections)
        if not his:
            logger.info("没有需要跟进的媒体项。")
            return

        self._filter_media(his, _ignore, collections)

        if collections:
            self.collection_follow_up(collections, _ignore)

        self.save_collections(collections)
        self.save_ignore_keys(_ignore)
        logger.info("续作跟进执行完成。")

    def _filter_media(self, his: set, _ignore: set, collections: dict):

        logger.info(f"开始对 {len(his)} 个条目进行预检...")

        async def _async_fetch_all():
            tasks = [self._fetch_tmdb_info(mtype, tmdbid) for mtype, tmdbid in his]
            return await asyncio.gather(*tasks, return_exceptions=True)

        results = asyncio.run(_async_fetch_all())

        items_for_full_recognition = []
        _collection_ids = set()
        his_list = list(his)

        for i, min_info in enumerate(results):
            key = his_list[i]

            if isinstance(min_info, Exception):
                logger.debug(f"获取TMDB信息失败 ({key}): {min_info}")
                continue

            if not min_info:
                continue

            # 电视剧或非系列电影检查年限
            if not min_info.get("belongs_to_collection"):
                air_date = min_info.get("last_air_date") or min_info.get("release_date")
                if air_date and not self.is_date_in_range(air_date, datetime.now(), 365 * self._threshold_years):
                    logger.info(f"{key} {min_info['title_year']} 已超过设定年限: {self._threshold_years} 年，不再跟进")
                    _ignore.add(key)
                    continue

            # 检查具体更新
            if min_info["type"] == MediaType.TV:
                next_episode = min_info.get("next_episode_to_air")
                if next_episode and self.is_date_in_range(next_episode.get("air_date"), threshold_days=self._after_days):
                    items_for_full_recognition.append(key)

            elif min_info["type"] == MediaType.MOVIE:
                collection = min_info.get("belongs_to_collection")
                if collection and collection["id"] not in _collection_ids:
                    _collection_ids.add(collection["id"])
                    items_for_full_recognition.append(key)

        logger.info(f"发现 {len(items_for_full_recognition)} 个有价值的条目。")

        if not items_for_full_recognition:
            return

        for mtype, tmdbid in items_for_full_recognition:

            mediainfo = self.chain.recognize_media(mtype=MediaType(mtype), tmdbid=tmdbid)
            if not mediainfo:
                continue

            if mediainfo.type == MediaType.MOVIE:
                self._handle_movie(mediainfo, _ignore, collections)
            else:
                self._handle_tv_show(mediainfo, _ignore)

    def _handle_movie(self, mediainfo: MediaInfo, _ignore: set, collections: dict):
        """处理电影逻辑"""
        collection_id, collection_name = self._get_collection_id(mediainfo, _ignore)
        if not collection_id:
            return

        if str(collection_id) not in collections:
            collections[str(collection_id)] = {"follow_up": True, "name": collection_name}
            logger.info(f"{mediainfo.tmdb_id} {mediainfo.title_year} 添加至系列合集 {collection_id} {collection_name}")

    def _handle_tv_show(self, mediainfo: MediaInfo, _ignore: set):
        """处理电视剧逻辑"""
        next_episode = mediainfo.next_episode_to_air

        if not (air_date := next_episode.get("air_date")):
            logger.info(f"{mediainfo.tmdb_id} {mediainfo.title_year} 没有新集或播出日期")
            return

        # 获取季号和集号
        season_number = next_episode.get("season_number", 1)
        episode_number = next_episode.get("episode_number", 1)

        # 补零格式化
        season_number_str = f"S{season_number:02d}"
        episode_number_str = f"E{episode_number:02d}"

        msg_title = f"🆕 {mediainfo.title_year} {season_number_str}{episode_number_str} 即将播出"
        msg_text = (
            f"🎬 标题：{next_episode['name'] or '暂无标题'}\n"
            f"📅 播出日期：{air_date[:10]}\n"
            f"👉 是否订阅该系列的最新作品？\n"
        )

        self._send_menu_message(mediainfo, msg_title, msg_text)

    def collection_follow_up(self, collections: dict[str, dict], ignore: set[tuple]):
        from app.chain.tmdb import TmdbChain
        tmdbchain = TmdbChain()

        for collection_id, followinfo in collections.items():
            if not followinfo.get("follow_up"):
                continue

            collection_info = tmdbchain.tmdb_collection(collection_id=int(collection_id))
            if not collection_info:
                continue

            # 查找最新电影
            latest_part = max(collection_info, key=lambda p: p.release_date or "0000-00-00")
            media_type = latest_part.type
            tmdbid = latest_part.tmdb_id

            latest_release_date = followinfo.get("latest_release_date") or "0000-00-00"
            latest_air_date = followinfo.get("air_date")

            if latest_part.release_date > latest_release_date:
                # 更新系列信息
                followinfo["parts"] = [self.build_key(p.type.value, p.tmdb_id) for p in collection_info]
                followinfo["latest_release_date"] = latest_part.release_date

            # 判断是否仍需追踪该系列
            if not self._should_track_media(latest_part):
                followinfo["follow_up"] = False

            if latest_air_date and not self.is_date_in_range(
                latest_air_date, threshold_days=self._after_days
            ):
                logger.info(
                    f"{followinfo.get('name') or collection_id} 没有新的系列电影上映")
                continue

            if not followinfo["follow_up"] or self.build_key(latest_part.type.value, latest_part.tmdb_id) in ignore:
                continue

            # 获取数字发行日期
            if media_type == MediaType.MOVIE:
                next_air_date, msg = self.find_earliest_date(tmdbid)
                followinfo["air_date"] = next_air_date
            else:
                next_air_date = None

            # 判断是否符合提醒时间
            if next_air_date is None or not self.is_date_in_range(next_air_date, threshold_days=self._after_days):
                logger.info(
                    f"{latest_part.title} 非院线发行日期: {next_air_date if next_air_date else '暂无'}，不符合符合提醒条件")
                continue

            msg_title = f"🆕 {followinfo.get('name')} 有新的电影即将上线！"
            msg_text = (
                f"🎬 最新电影：{latest_part.title_year}\n"
                f"{msg}\n"
                f"📅 日期：{next_air_date[:10]}\n\n"
                f"👉 是否订阅该系列的最新作品？"
                )

            self._send_menu_message(latest_part, msg_title, msg_text)

    def _get_collection_id(self, mediainfo: MediaInfo, ignore: set) -> tuple[Optional[int], Optional[str]]:
        """获取媒体的合集ID，若非系列电影则更新忽略列表"""
        tmdb_info = mediainfo.tmdb_info

        collection_id = tmdb_info["belongs_to_collection"].get("id")
        if not collection_id:
            logger.warn(f"{mediainfo.tmdb_id} {mediainfo.title_year} 未获取到所属合集ID, 等待下次检查")

        return collection_id, tmdb_info["belongs_to_collection"].get("name")

    def _should_track_media(self, mediainfo: MediaInfo, ignore: Optional[set] = None) -> bool:
        """判断是否在跟进时间范围内"""
        air_date = mediainfo.last_air_date or mediainfo.release_date
        if not air_date or not self.is_date_in_range(air_date, datetime.now(), 365 * self._threshold_years):
            logger.info(f"{mediainfo.title_year} 已超过设定年限: {self._threshold_years} 年，不再跟进")
            if ignore is not None:
                ignore.add((mediainfo.type.value, mediainfo.tmdb_id))
            return False

        return True

    def find_earliest_date(self, tmdbid: int):
        results = TmdbApi().movie.release_dates(tmdbid) or []
        _release_date, iso_3166_1, note, _type = "9999-12-31T23:59:59.999Z", "", "", 4
        for result in results:
            for _d in result.get("release_dates", []):
                if _d.get("type", 0) > 3 and (_date := _d.get("release_date")) and _date < _release_date:
                    _release_date, iso_3166_1, note, _type = _date, result.get("iso_3166_1"), _d.get("note"), _d.get("type")
        return _release_date if _release_date != "9999-12-31T23:59:59.999Z" else None, self.movie_release_info(iso_3166_1, note, _type)

    def _need_follow_up(self, ignore: set[tuple[str, int]], collections: dict[str, dict]) -> set[tuple[str, int]]:
        subscriptions = {(sub.type, sub.tmdbid) for sub in SubscribeOper().list()}

        # 系列包含的条目
        _collection_items = set()
        for collection in collections.values():
            if parts := collection.get("parts"):
                _collection_items |= {self.parse_key(k) for k in parts}

        # 忽略的条目
        _ignore = ignore & subscriptions
        if _ignore:
            for k in _ignore:
                self.del_data(self.build_key(*k))

        # 排除已订阅 已发送未处理 系列合集中的条目
        ignore |= {*subscriptions, *_collection_items}

        # 媒体服务器
        serveritems = self.get_media_server_items(exclude=ignore)
        subscribehis = { (sub.type, sub.tmdbid) for sub in self.get_subscribe_history(exclude=ignore) } if self._check_sub_history else set()
        return serveritems.union(subscribehis)

    @eventmanager.register(EventType.MessageAction)
    def message_action(self, event: Event):
        """
        处理消息按钮回调
        """
        event_data = event.event_data
        if not event_data or event_data.get("plugin_id") != self.__class__.__name__:
            return

        text_parts = (event_data.get("text") or "").split("|", 1)
        if len(text_parts) < 2:
            return
        action, _key = text_parts

        handler_map = {"add": self._handle_add, "ignore": self._handle_ignore}
        if handler := handler_map.get(action):
            handler(event_data.get("channel"), event_data.get("source"), event_data.get("userid"),
                    event_data.get("original_message_id"), event_data.get("original_chat_id"), _key)

    def _send_menu_message(self, mediainfo: MediaInfo, title: str, text: str):
        """
        发送主菜单
        """
        _key = self.build_key(mediainfo.type.value, mediainfo.tmdb_id)
        buttons = [[
            {"text": "📼 追加订阅", "callback_data": f"[PLUGIN]{self.__class__.__name__}|add|{_key}"},
            {"text": "💤 不再提醒", "callback_data": f"[PLUGIN]{self.__class__.__name__}|ignore|{_key}"}
        ]]
        self.post_message(title=title, text=text, mtype=NotificationType.Plugin, buttons=buttons)
        self.save_data(_key, self.clean_media_info(mediainfo))
        # 更新忽略列表
        self.update_ignore_keys(_key)

    def _handle_add(self, channel, source, userid, original_message_id, original_chat_id, _key: str):
        data = self.get_data(_key) or {}
        if not data:
            msg, buttons = "信息已过时", None
        else:
            sid, msg = SubscribeChain().add(**data, save_path=self._save_path, sites=self._sites, username=self.plugin_name)
            if sid:
                self.del_data(_key)
                self.chain.delete_message(channel, source, original_message_id, original_chat_id)
                return
            buttons = [[
                {"text": "📼 重试", "callback_data": f"[PLUGIN]{self.__class__.__name__}|add|{_key}"},
                {"text": "💤 忽略", "callback_data": f"[PLUGIN]{self.__class__.__name__}|ignore|{_key}"}
            ]]
        self.post_message(channel=channel, title="添加订阅失败", text=f"原因: {msg}", userid=userid, buttons=buttons,
                          original_message_id=original_message_id, original_chat_id=original_chat_id)

    def _handle_ignore(self, channel, source, userid, original_message_id, original_chat_id, _key):

        data = self.get_data(_key) or {}
        self.del_data(_key)
        self.update_ignore_keys(_key)

        self.post_message(
            channel=channel,
            source=source,
            title=f"已忽略订阅 {data['title']} ({data["year"]})" if data else f"已忽略订阅 {_key}",
            userid=userid,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id
        )

    def get_media_server_items(self, exclude: set[tuple] = set()) -> set[tuple[str, int]]:
        # 获取所有媒体服务器
        mediaservers = ServiceConfigHelper.get_mediaserver_configs()
        if not mediaservers:
            return
        items = set()
        serverchain = MediaServerChain()
        # 遍历媒体服务器
        for mediaserver in mediaservers:
            if not mediaserver:
                continue
            if not mediaserver.enabled:
                continue
            server_name = mediaserver.name
            libraries = serverchain.librarys(server_name)
            if not libraries:
                continue
            for library in libraries:
                if library.id not in self._libraries:
                    continue
                logger.info(f"正在获取 {server_name} 媒体库 {library.name} ...")

                for item in serverchain.items(server=server_name, library_id=library.id):
                    if not item or not item.tmdbid:
                        continue
                    # 类型
                    item_type = "电视剧" if item.item_type in ["Series", "show"] else "电影"
                    # 插入数据
                    if (_key := (item_type, item.tmdbid)) not in exclude:
                        items.add(_key)
        return items

    def clean_media_info(self, mediainfo: MediaInfo) -> dict:
        """
        清洗 mediainfo 对象，仅保留关键字段用于存储或传输
        """
        if not mediainfo:
            return {}
        season = mediainfo.number_of_seasons
        # 查询订阅历史
        history = self.get_subscribe_history(tmdbid=mediainfo.tmdb_id, type=mediainfo.type)
        total_episode = next((item.total_episode for item in history if item.season == season), 0)
        return {
            'title': mediainfo.title,
            'year': mediainfo.year,
            "tmdbid": mediainfo.tmdb_id,
            "doubanid": mediainfo.douban_id,
            "bangumiid": mediainfo.bangumi_id,
            "episode_group": mediainfo.episode_group,
            "season": season,
            "start_episode": total_episode + 1 if total_episode else 0,
            }

    @eventmanager.register(EventType.SiteDeleted)
    def site_deleted(self, event: Event):
        """
        删除对应站点
        """
        site_id = event.event_data.get("site_id")
        if site_id in self._sites:
            self._sites.remove(site_id)
            self.__update_config()

    def get_ignore_keys(self) -> set[tuple[str, int]]:
        _keys = self.get_data("ignore_keys") or []
        return {self.parse_key(key) for key in _keys}

    def save_ignore_keys(self, keys: set[tuple[str, int]]):
        _keys = [self.build_key(*key) for key in keys]
        self.save_data("ignore_keys", _keys)

    def update_ignore_keys(self, key: Union[tuple[str, int], str]):
        """将 key 添加到忽略列表中"""
        if isinstance(key, str):
            key = self.parse_key(key)
        self.save_ignore_keys(self.get_ignore_keys().union({key}))

    def get_collections(self) -> dict[str, dict]:
        return self.get_data("collections") or {}

    def save_collections(self, collections: dict):
        self.save_data("collections", collections)

    @db_query
    def get_subscribe_history(self, db: Session = None, tmdbid: int = None, type: MediaType = None, exclude: set[tuple] = set()) -> list[SubscribeHistory]:
        query = db.query(SubscribeHistory)
        conditions = []
        if tmdbid: conditions.append(SubscribeHistory.tmdbid == tmdbid)
        if type: conditions.append(SubscribeHistory.type == type.value)
        if exclude: conditions.append(tuple_(SubscribeHistory.type, SubscribeHistory.tmdbid).notin_(exclude))
        try:
            return query.filter(*conditions).all()
        except Exception as e:
            logger.error(f"获取订阅历史失败: {str(e)}")
            return []

    @staticmethod
    def build_key(mtype: str, tmdbid: int) -> str:
        return f"{mtype}.{tmdbid}"

    @staticmethod
    def parse_key(key_str: str) -> Optional[tuple[str, int]]:
        try:
            type_str, tmdbid_str = key_str.split(".", 1)
            return type_str, int(tmdbid_str)
        except ValueError:
            return None
        except Exception as e:
            print(f"解析key失败: {key_str}, 错误: {str(e)}")
            return None

    @staticmethod
    def movie_release_info(iso_code: str, note, type_id) -> str:
        type_name = {4: "数字发行", 5: "实体发行", 6: "电视播放"}
        iso_to_country_cn = {"US": "美国", "GB": "英国", "FR": "法国", "DE": "德国", "JP": "日本", "KR": "韩国", "CN": "中国", "HK": "中国香港", "TW": "中国台湾"}
        return (f"🌍 地区：{iso_to_country_cn.get(iso_code.upper(), '未知地区')}\n"
                f"📼 渠道：{note or '未知'}\n"
                f"🏷️ 类型：{type_name.get(int(type_id), '未知发行渠道')}")

    @staticmethod
    def is_date_in_range(air_date: Union[datetime, str], reference_date: Optional[Union[datetime, str]] = None, threshold_days: int = 2) -> bool:
        """
        两个日期接近或在未来指定天数内

        :param air_date: 目标日期
        :param reference_date: 参考日期
        :param threshold_days: 阈值天数
        :return: bool

        只传入 target_date 时，判断是否在未来 threshold_days 天内
        传入 target_date 和 reference_date 时，判断两个日期是否接近
        """
        try:
            # 解析目标日期
            if isinstance(air_date, datetime):
                date1 = air_date.date()
            elif isinstance(air_date, str):
                date1 = datetime.strptime(air_date[:10], "%Y-%m-%d").date()
            else:
                date1 = datetime.now().date()

            # 单日期模式：是否在未来threshold_days内
            if reference_date is None:
                today = datetime.now().date()
                delta = (date1 - today).days
                return 0 <= delta <= threshold_days

            # 双日期模式：两个日期是否接近
            if isinstance(reference_date, datetime):
                date2 = reference_date.date()
            elif isinstance(reference_date, str):
                date2 = datetime.strptime(reference_date[:10], '%Y-%m-%d').date()
            # 天数差
            delta = (date1 - date2).days
            return abs(delta) <= threshold_days

        except (ValueError, TypeError) as e:
            logger.error(f"日期格式错误: {str(e)}")
            return False
