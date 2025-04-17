# 基础库
from cachetools import TTLCache
from dataclasses import dataclass
import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
import uuid

# 第三方库
from apscheduler.triggers.cron import CronTrigger
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Response, responses
import pytz
from pydantic import BaseModel

# 项目库
from app.chain.subscribe import Subscribe
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import eventmanager, Event
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.modules.themoviedb import TmdbApi
from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaType


result_cache = TTLCache(maxsize=1000, ttl=3600 * 12)
# 本地时区
TZ = pytz.timezone(settings.TZ)

@dataclass
class CalendarInfo:
    """剧集信息"""
    # 发布日期
    release_date: Optional[str] = None
    # 播出日期
    air_date: Optional[str] = None
    # 集号
    episode_number: Optional[int] = None
    # 集状态
    episode_type: Optional[str] = None
    # 标题(季/电影)
    title: Optional[str] = None
    # 集标题
    name: Optional[str] = None
    # TMDBID
    id: Optional[int] = None
    # 集简介
    overview: Optional[str] = None
    # 时长
    runtime: Optional[int] = 30
    # 季号
    season_number: Optional[int] = None

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
        if self.air_date is None:
            self.air_date = self.release_date


    def utc_airdate(self, dalay_minutes: float) -> str:
        """utc时间"""
        return TZ.localize(datetime.datetime.strptime(self.air_date, "%Y-%m-%d") + datetime.timedelta(minutes=dalay_minutes or 0)).astimezone(pytz.utc).strftime("%Y%m%dT%H%M%SZ")


class CalendarEvent(BaseModel):
    """日历事件"""
    # 创建时间
    created: Optional[str]
    # 开始时间
    dtstart: Optional[str]
    # 结束时间
    dtend: Optional[str]
    # 标题
    summary: Optional[str]
    # 描述(备注)
    description: Optional[str]
    # 地点
    location: Optional[str]
    # 唯一标识
    uid: Optional[str]
    # 类型
    transp: str = "OPAQUE"
    # 序号
    sequence: int = 0
    # 状态
    status: str = "CONFIRMED"
    # 最后修改时间
    last_modified: Optional[str]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self.uid is None:
            self.uid = str(uuid.uuid4())
            self.created = self._get_utc_time()

    def _get_utc_time(self):
        return datetime.datetime.now(pytz.utc).strftime('%Y%m%dT%H%M%SZ')

    def __setattr__(self, name, value):
        if name in self.__dict__ and self.__dict__[name] == value:
            return
        super().__setattr__(name, value)
        if name != 'last_modified':
            super().__setattr__('last_modified', self._get_utc_time())

    
    def _created_to_ics(self) -> str:
        """创建时间"""
        if self.created:
            return f"CREATED;VALUE=DATE-TIME:{self.created}"

    def _dtstart_to_ics(self) -> str:
        if self.dtstart:
            return f"DTSTART;VALUE=DATE-TIME:{self.dtstart}"

    def _dtend_to_ics(self) -> str:
        if self.dtend:
            return f"DTEND;VALUE=DATE-TIME:{self.dtend}"
        
    def _summary_to_ics(self) -> str:
        """标题"""
        if self.summary:
            return f"SUMMARY:{self.summary}"
        
    def _description_to_ics(self) -> str:
        """备注"""
        if self.description:
            return f"DESCRIPTION:{self.description}"
    
    def _location_to_ics(self) -> str:
        """地点"""
        if self.location:
            return f"LOCATION:{self.location}"
        
    def _uid_to_ics(self) -> str:
        """唯一标识"""
        return f"UID:{self.uid or uuid.uuid4()}"
        
    def _transp_to_ics(self) -> str:
        """类型"""
        return f"TRANSP:{self.transp or 'OPAQUE'}"
    
    def _sequence_to_ics(self) -> str:
        """序号"""
        return f"SEQUENCE:{self.sequence or 0}"
    
    def _status_to_ics(self) -> str:
        """状态"""
        return f"STATUS:{self.status or 'CONFIRMED'}"

    def _last_modified_to_ics(self) -> str:
        """最后修改时间"""
        return f"LAST-MODIFIED:{self.last_modified or self._get_utc_time()}"
    
    def ics_header(self) -> str:
        return (
        "\nBEGIN:VCALENDAR\n"
        + "PRODID:-//Anime wikrin//Anime broadcast time Calendar 2.0//CN\n"
        + "VERSION:2.0\n"
        + "CALSCALE:GREGORIAN\n"
        + "METHOD:PUBLISH\n"
        + "X-WR-CALNAME:Anime broadcast\n"
        + "X-WR-TIMEZONE:Asia/Shanghai\n"
        + "BEGIN:VTIMEZONE\n"
        + "TZID:Asia/Shanghai\n"
        + "X-LIC-LOCATION:Asia/Shanghai\n"
        + "BEGIN:STANDARD\n"
        + "TZOFFSETFROM:+0800\n"
        + "TZOFFSETTO:+0800\n"
        + "TZNAME:CST\n"
        + "DTSTART:19700101T000000\n"
        + "END:STANDARD\n"
        + "END:VTIMEZONE\n"
    )


    def to_ics(self) -> str:
        """转换为ics格式"""
        ics_lines = ["BEGIN:VEVENT"]
        for attr in self.__fields__.keys():
            method_name = f"_{attr}_to_ics"
            if hasattr(self, method_name):
                method = getattr(self, method_name)
                ics_line = method()
                if ics_line:
                    ics_lines.append(ics_line)
        ics_lines.append("END:VEVENT")
        return "\n".join(ics_lines)


class SubscribeCal(_PluginBase):
    # 插件名称
    plugin_name = "订阅日历"
    # 插件描述
    plugin_desc = "根据订阅生成日历, 以供导入至设备日历中"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/wikrin/MoviePilot-Plugins/main/icons/calendar_a.png"
    # 插件版本
    plugin_version = "1.0.1"
    # 插件作者
    plugin_author = "Attente"
    # 作者主页
    author_url = "https://github.com/wikrin"
    # 插件配置项ID前缀
    plugin_config_prefix = "subscribecal_"
    # 加载顺序
    plugin_order = 42
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler = None

    # 配置属性
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = ""
    _calc_time: bool = False

    def init_plugin(self, config: dict = None):
        self.subscribeoper = SubscribeOper()
        self.tmdbapi = TmdbApi()
        self.transferchain = TransferChain()
        self.transferhis = self.transferchain.transferhis

        # 停止现有任务
        self.stop_service()
        self.load_config(config)

        if self._onlyonce:
            self.schedule_once()

    def load_config(self, config: dict):
        """加载配置"""
        if config:
            # 遍历配置中的键并设置相应的属性
            for key in (
                "enabled",
                "onlyonce",
                "cron",
                "calc_time",
            ):
                setattr(self, f"_{key}", config.get(key, getattr(self, f"_{key}")))

    def schedule_once(self):
        """调度一次性任务"""
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        logger.info("订阅日历，立即运行一次")
        self._scheduler.add_job(
            func=self.full_update,
            trigger='date',
            run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ))
            + datetime.timedelta(seconds=3),
        )
        self._scheduler.start()

        # 关闭一次性开关
        self._onlyonce = False
        self.__update_config()

    def __update_config(self):
        """更新设置"""
        self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
                "cron": self._cron,
                "calc_time": self._calc_time,
            }
        )

    def get_form(self):
        _url= f"api/v1/plugin/SubscribeCal/subscribe?apikey={settings.API_TOKEN}"
        _domain = settings.MP_DOMAIN() or f"http://{settings.HOST}:{settings.PORT}/"
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 4, 'md': 2},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 6, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'calc_time',
                                            'label': '根据入库补充时间',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 6, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '更新一次数据',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 8, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '数据更新周期',
                                            'placeholder': '五位cron表达式, 留空自动',
                                        }
                                    }
                                ]
                            },
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
                                            'type': 'success' if self._enabled else 'error',
                                            'variant': 'tonal',
                                            'text': True
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'props': {'innerHTML': 
                                                    (f'ICS文件可<a href="/api/v1/plugin/SubscribeCal/download/calendar.ics?apikey={settings.API_TOKEN}" target="_blank"><u>点此下载</u></a>') if self._enabled else f'插件未启用'
                                                }
                                            }
                                        ]
                                    }
                                ]
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
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'style': 'display: none !important' if not self._enabled else None,
                                            'text': True
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'props': {'innerHTML':
                                                    f'iCal链接：{_domain}{_url}<br>'
                                                    f'1. 该链接包含API密钥，请妥善保管防止泄露⚠️⚠️<br>'
                                                    f'2. 将iCal链接添加到支持订阅的日历应用（如Outlook、Google Calendar等）<br>'
                                                    f'3. 服务需公网访问，请将{_domain}替换为您的公网IP/域名'
                                                }
                                            }
                                        ]
                                    }
                                ]
                            },
                        ],
                    },
                ]
            }
        ], {
            "enabled": False,
            "calc_time": False,
            "onlyonce": False,
            "cron": "",
        }

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        if self._enabled:
            trigger = CronTrigger.from_crontab(self._cron) if self._cron else "interval"
            kwargs = {"hours": 12} if not self._cron else {}
            return [
                {
                    "id": "SubscribeCal",
                    "name": "日历数据刷新",
                    "trigger": trigger,
                    "func": self.full_update,
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
        return[{
            "path": "/subscribe",
            "endpoint": self.get_ics,
            "methods": ["GET"],
            "summary": "订阅日历",
            "description": "获取订阅日历",
            },{
            "path": "/download/calendar.ics",
            "endpoint": self.download_ics,
            "methods": ["GET"],
            "summary": "下载日历",
            "description": "下载订阅日历ICS",
            }]

    def get_command(self):
        pass

    def get_page(self):
        pass

    def get_state(self):
        return self._enabled

    def get_ics(self,) -> str:
        """获取ics内容"""
        _events = self.get_events(self.keys)
        return Response(content=self.generate_ics_content(_events), media_type="text/plain")

    def download_ics(self) -> responses.StreamingResponse:
        from io import StringIO
        """获取ics内容"""
        _events = self.get_events(self.keys)
        ics_file = StringIO(self.generate_ics_content(_events))
        return responses.StreamingResponse(ics_file, media_type="text/calendar", headers={"Content-Disposition": "attachment; filename=calendar.ics"})
 
    def full_update(self, cache: bool = False):
        """
        日历全量更新
        """
        # 获取订阅
        subs = self.subscribeoper.list()
        # 暂存key
        _tmp_keys = []
        for sub in subs:
            # 跳过洗版
            if sub.best_version: continue
            _info = self.serach_sub(sub=sub, cache=cache)
            if not _info: continue
            key = self.media_process(sub=sub, mediainfo=_info[0], cal_info=_info[1])
            _tmp_keys.append(key)
        # 去除删除的订阅
        if _d := set(self.keys) - set(_tmp_keys):
            for key in _d:
                self.del_data(key)
        # 保存key
        self.save_keys(_tmp_keys)
        logger.info("数据更新完成")

    @eventmanager.register(EventType.SubscribeAdded)
    def sub_add_event(self, event: Event):
        if not event or not self._enabled:
            return
        if sub := self.subscribeoper.get(event.event_data.get("subscribe_id", None)):
            _info = self.serach_sub(sub=sub, cache=False)
            key = self.media_process(sub=sub, mediainfo=_info[0], cal_info=_info[1])
            self.keys.append(key)
            # 保存数据
            self.save_keys(self.keys)

    @eventmanager.register([EventType.SubscribeDeleted, EventType.SubscribeComplete])
    def sub_del_event(self, event: Event):
        if not event or not self._enabled:
            return
        if sub := self.subscribeoper.get(event.event_data.get("subscribe_id", None)):
            key = SubscribeCal.get_sub_key(sub=sub)
            keys = self.keys
            if key in keys:
                # 移除日历数据
                self.del_data(key)
                # 移除key
                keys.remove(key)
                # 更新key
                self.save_keys(keys)

    def serach_sub(self, sub: Subscribe, cache: bool = True) -> Optional[Tuple[MediaInfo, List[CalendarInfo]]]:
        """搜索订阅"""
        cachekey = f"__cache_{sub.tmdbid}_{sub.year}_{sub.season}__"
        mediainfo = self.chain.recognize_media(tmdbid=sub.tmdbid, doubanid=sub.doubanid, bangumiid=sub.bangumiid,
                                                episode_group=sub.episode_group, mtype=MediaType(sub.type), cache=cache)
        info = None
        if cache and result_cache and cachekey in result_cache:
            info = result_cache[cachekey]
        elif sub.type == MediaType.TV.value:
            if sub.episode_group:
                if result := self.tmdbapi.get_tv_group_detail(group_id=sub.episode_group, season=sub.season):
                    list(map(lambda x: x.update(season_number=sub.season), result.get('episodes', [])))
            else:
                result = self.chain.tmdb_info(tmdbid=sub.tmdbid, mtype=MediaType(sub.type), season=sub.season)
            # 电视剧补充info集信息
            info = [CalendarInfo(**epinfo) for epinfo in result.get('episodes', [])]
        else:
            info = [CalendarInfo(**mediainfo.tmdb_info)]
        if info:
            result_cache[cachekey] = info
        else:
            return None
        return mediainfo, info

    def media_process(self, sub: Subscribe, mediainfo: MediaInfo, cal_info: list[CalendarInfo]) -> str:
        """
        :param: Subscribe对象
        :param cal_info: TMDB剧集播出时间
        :return: key, List[CalendarEvent]
        """
        _key = SubscribeCal.get_sub_key(sub)
        minutes = 0
        if self._calc_time \
            and mediainfo.type == MediaType.TV:
            minutes = self.generate_average_time(sub, cal_info)
        total_episodes = len(cal_info)
        event_data = self.get_event_data(key=_key) or {}
        for epinfo in cal_info:
            cal = event_data.get(str(epinfo.id), CalendarEvent())
            ## 后续可加入jinja2模板引擎
            title = f"[{epinfo.episode_number}/{total_episodes}]{mediainfo.title} ({mediainfo.year})" if mediainfo.type == MediaType.TV else f"{mediainfo.title} ({mediainfo.year})"
            # 全天事件
            if not minutes:
                # 0:00 - 24:00
                dtend = epinfo.utc_airdate(60 * 24)
            elif epinfo.runtime:
                # start - airdatetime
                dtend = epinfo.utc_airdate(minutes + epinfo.runtime)
            else:
                # stdate - 24:00
                dtend = epinfo.utc_airdate(60 * 24 - minutes)
            cal.summary=title
            cal.description=epinfo.overview
            cal.dtstart=epinfo.utc_airdate(minutes)
            cal.dtend=dtend
            event_data[str(epinfo.id)] = cal.dict()
        # 保存事件数据
        self.save_data(key=_key, value=event_data)
        return _key

    def save_events(self, value: dict[str, dict[str, CalendarEvent]]):
        # 序列化事件数据
        for key, d in value.items():
            value = {k: v.json() for k, v in d.items()}
            self.save_data(key=key, value=value)

    def get_event_data(self, key: str) -> dict[str, CalendarEvent]:
        """获取日历事件数据"""

        _data = self.get_data(key=key) or {}
        if _data:
            return {k: CalendarEvent.parse_obj(v) for k, v in _data.items()}

    def get_events(self, keys: list[str] = None) -> Optional[Dict[str, CalendarEvent]]:
        events: dict[str, CalendarEvent] = {}
        if not keys:
            self.full_update(cache=True)
            keys = self.keys
        for key in keys:
            events.update(self.get_event_data(key=key))
        return events

    def generate_average_time(self, sub: Subscribe, cal_info: list[CalendarInfo]) -> float:
        """
        生成剧集播放随机时间范围
        :param sub: Subscribe对象
        :param cal_info: TMDB剧集播出时间
        :return: tuple[float, float](min, max)
        """
        def adjust_average_time(delay_time: Set[float]) -> float:
            # 检查剩余元素数量
            if not delay_time:
                return 0
            # 移除极值
            if len(delay_time) > 3:
                delay_time.discard(min(delay_time))
                delay_time.discard(max(delay_time))
            # 计算平均
            total_sum = sum(delay_time)
            count = len(delay_time)
            average = total_sum / count
            # 返回平均
            return average

        # 获取整理记录
        histories = self.transferhis.get_by(mtype=sub.type, season=f"S{str(sub.season).rjust(2, '0')}", tmdbid=sub.tmdbid)
        # 初始化分集更新时间
        _his_dt: dict[str, datetime.datetime] = {}
        if histories:
            # 提取tmdb_info的集信息
            _eps_dt: dict = {str(i.episode_number): i.air_date for i in cal_info}
            # 将订阅日期字符串转为datetime对象
            sub_date = datetime.datetime.strptime(sub.date, "%Y-%m-%d %H:%M:%S")
            for history in histories:
                history_date = datetime.datetime.strptime(history.date, "%Y-%m-%d %H:%M:%S")
                if history.status and (history_date - sub_date).total_seconds() > 43200: # 12小时
                    eps = history.episodes.split("-")
                    episodes = range(int(eps[0][1:]), int(eps[-1][1:]) + 1)
                    for ep in episodes:
                        _his_dt[str(ep)] = history_date
        # 计算更新时间范围
        delay_time = {(_dt - datetime.datetime.strptime(_eps_dt[_ep], "%Y-%m-%d")).total_seconds() // 60
                      for _ep, _dt in _his_dt.items() if _ep in _eps_dt}

        return adjust_average_time(delay_time)

    @staticmethod
    def generate_ics_content(calevent: dict[str, CalendarEvent]) -> str:
        """
        构建ics日历文本
        """
        _ics = CalendarEvent().ics_header()
        for uniqueid, event in calevent.items():
            _ics += f"{event.to_ics()}\n"
        return _ics + "END:VCALENDAR"

    def save_keys(self, value: list[str]):
        self.save_data(key="__key__", value=value)

    @property
    def keys(self) -> list[str]:
        return self.get_data(key="__key__") or []

    @staticmethod
    def get_sub_key(sub: Subscribe) -> str:
        return f"__key_{sub.tmdbid}_{sub.year}_{sub.season}__"

