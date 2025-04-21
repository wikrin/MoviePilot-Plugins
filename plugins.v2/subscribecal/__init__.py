# 基础库
from cachetools import TTLCache
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import datetime
import statistics
import uuid

# 第三方库
from apscheduler.triggers.cron import CronTrigger
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Response, responses
from pydantic import BaseModel
import pytz

# 项目库
from app.chain.subscribe import Subscribe
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import eventmanager, Event
from app.core.plugin import PluginManager
from app.db.downloadhistory_oper import DownloadHistoryOper
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
    # 集号(剧集组下不可靠)
    episode_number: Optional[int] = None
    # 集状态
    episode_type: Optional[str] = None
    # 标题(季/电影)
    title: Optional[str] = None
    # 集标题
    name: Optional[str] = None
    # unique ID
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
    
    def ics_header(self, calname: str = "追剧日历") -> str:
        return (
        "\nBEGIN:VCALENDAR\n"
        + "PRODID:-//wikrin//TV broadcast time Calendar 2.0//CN\n"
        + "VERSION:2.0\n"
        + "CALSCALE:GREGORIAN\n"
        + "METHOD:PUBLISH\n"
        + "X-WR-CALNAME:%s\n"
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
    ) % (calname,)


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
    plugin_version = "1.0.4"
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
    _calname: str = "追剧日历"
    _interval_minutes: int = 15

    def init_plugin(self, config: dict = None):
        self.downloadhis = DownloadHistoryOper()
        self.subscribeoper = SubscribeOper()
        self.tmdbapi = TmdbApi()

        # 停止现有任务
        self.stop_service()
        # 保存配置
        self._save_tmp_config(config)
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
                "calname",
                "interval_minutes",
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
        config = self.get_config() or {}
        config.update({
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
                "cron": self._cron,
                "calc_time": self._calc_time,
                "calname": self._calname,
                "interval_minutes": self._interval_minutes,
            })
        self.update_config(config)
    
    def _save_tmp_config(self, config: dict[str, Any]):
        if not config:
            return config
        try:
            user_interval = {k: v for k, v in config.items() if k.startswith("_tmp.")}
            _items: dict[str, dict] = {}
            for key, value in user_interval.items():
                # 使用. 分割
                _, _subid, _key = key.split(".")
                if _subid not in _items:
                    _items[_subid] = {}
                _items[_subid][_key] = value
            if not _items:
                return
            _data = {subid: datetime.timedelta(
                days=int(interval.get("days", 0)),
                hours=int(interval.get("hours", 0)),
                minutes=int(interval.get("minutes", 0)),
            ).total_seconds() / 60 for subid, interval in _items.items() if interval.get("enabled")}
            # 保存插件数据
            self.save_data(key="UserInterval", value=_data)
            logger.info("设定延迟已保存")
        except Exception as e:
            logger.error(f"保存设定延迟失败: {e}")

    def _del_tmp_config(self, ids: list[str], user_interval: dict[str, Any]):
        try:
            pm = PluginManager()
            config: dict[str, Any] = pm.get_plugin_config(self.__class__.__name__)
            for _key in list(config.keys()):
                if not _key.startswith("_tmp."):
                    continue
                # 使用. 分割
                _subid = _key.split(".")[1]
                if _subid in ids:
                    del config[_key]
                    logger.debug(f"删除配置项: {_subid} - {_key}")
                if _subid in user_interval:
                    del user_interval[_subid]
                    logger.debug(f"删除数据项: {_subid} - {_key}")
            # 保存插件配置
            pm.save_plugin_config(self.__class__.__name__, config)
            # 更新数据
            self.save_data(key="UserInterval", value=user_interval)
            pm.init_plugin(self.__class__.__name__, config)
        except Exception as e:
            logger.error(f"移除设定延迟失败: {e}")

    def get_form(self):
        _url = f"api/v1/plugin/SubscribeCal/subscribe?apikey={settings.API_TOKEN}"
        _domain = settings.MP_DOMAIN() or f"http://{settings.HOST}:{settings.PORT}/"
        subs = self.subscribeoper.list()
        interval = self.get_data(key="Interval") or {}
        def _build_sub_card(sub: Subscribe):
            """构建订阅卡片"""
            interval_time = interval.get(str(sub.id), 0)
            days, remainder = divmod(interval_time, 60 * 24)
            hours, minutes = divmod(remainder, 60)
            # 天数表达
            d = int(days)
            days_str = "当天" if d == 0 else (f"第{d+1}天" if d > 0 else f"前{abs(d)}天")
            text = f"{days_str}  {int(hours):02d} : {int(minutes):02d}"
            return {
                'component': 'VCard',
                'props': {
                    'elevation': '2',  # 卡片阴影
                    'hover': True,      # 悬停效果
                    'style': 'transition: all 0.3s;'  # 平滑过渡
                },
                'content': [
                    # 图片区域
                    {
                        'component': 'VImg',
                        'props': {
                            'src': sub.backdrop or sub.poster,
                            'height': '180px',
                            'cover': True,
                            'gradient': 'to bottom, rgba(0,0,0,0), rgba(0,0,0,0.7)',  # 渐变遮罩
                            'aspect-ratio': '16/9'  # 固定宽高比
                        }
                    },
                    # 标题区域
                    {
                        'component': 'VCardTitle',
                        'props': {
                            'class': 'px-3 pt-2 pb-1'  # 内边距
                        },
                        'content': [
                            {
                                'component': 'div',
                                'props': {
                                    'class': 'd-flex align-center',
                                    'style': 'gap: 0.5rem;'
                                },
                                'content': [
                                    # 剧集类型图标
                                    {
                                        'component': 'VIcon' if sub.type == MediaType.TV.value else 'VImg',
                                        'props': {
                                            'icon': 'mdi-television' if sub.type == MediaType.TV.value else None,
                                            'src': 'https://example.com/movie-icon.png' if sub.type == MediaType.MOVIE else None,
                                            'size': '24',
                                            'class': 'text-primary'
                                        }
                                    },
                                    # 标题
                                    {
                                        'component': 'div',
                                        'text': f'{sub.name} ({sub.year})',
                                        'props': {
                                            'class': 'font-weight-bold',
                                            'style': 'font-size: 1.1rem; white-space: normal; word-break: break-word; line-height: 1.3;',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # 副标题
                    {
                        'component': 'VCardSubtitle',
                        'props': {
                            'class': 'px-3 pb-2'
                        },
                        'content': [
                            {
                                'component': 'div',
                                'props': {
                                    'class': 'd-flex align-center',
                                    'style': 'gap: 0.5rem;'
                                },
                                'content': [
                                    {
                                        'component': 'VIcon',
                                        'props': {
                                            'icon': 'mdi-clock-outline',
                                            'size': '18',
                                            'class': 'text-secondary'
                                        }
                                    },
                                    {
                                        'component': 'div',
                                        'text': f"统计播出时间: [{text}]",
                                        'props': {
                                            'style': 'font-size: 0.9rem;'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # 配置
                    {
                        'component': 'VCardActions',
                        'content': [
                            {
                                'component': 'VRow',
                                'props': {
                                    'dense': True,
                                    'align': 'center',
                                    'justify': 'space-between'
                                },
                                'content': [
                                    {
                                        'component': 'VCol',
                                        'props': {'cols': 'auto'},
                                        'content': [
                                            {
                                                'component': 'VSwitch',
                                                'props': {
                                                    'model': f"_tmp.{sub.id}.enabled",
                                                    'label': '启用',
                                                    'density': 'compact',
                                                    'hide-details': True,
                                                    'class': 'mr-2'
                                                }
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VCol',
                                        'props': {'cols': 'auto'},
                                        'content': [
                                            {
                                                'component': 'VTextField',
                                                'props': {
                                                    'model': f"_tmp.{sub.id}.days",
                                                    'placeholder': '天',
                                                    'type': 'number',
                                                    'min': 0,
                                                    'density': 'compact',
                                                    'variant': 'underlined',
                                                    'hide-details': True,
                                                    'style': 'width: 60px;',
                                                    'single-line': True
                                                }
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VCol',
                                        'props': {'cols': 'auto'},
                                        'content': [
                                            {
                                                'component': 'VSelect',
                                                'props': {
                                                    'model': f"_tmp.{sub.id}.hours",
                                                    'placeholder': '时',
                                                    'items': [{'title': f"{i:02d}", 'value': i} for i in range(24)],
                                                    'density': 'compact',
                                                    'variant': 'underlined',
                                                    'hide-details': True,
                                                    'menu-props': {'maxHeight': 200},
                                                    'style': 'width: 70px;'
                                                }
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VCol',
                                        'props': {'cols': 'auto'},
                                        'content': [
                                            {
                                                'component': 'VSelect',
                                                'props': {
                                                    'model': f"_tmp.{sub.id}.minutes",
                                                    'placeholder': '分',
                                                    'items': [{'title': f"{i:02d}", 'value': i} for i in range(0, 60, 5)],
                                                    'density': 'compact',
                                                    'variant': 'underlined',
                                                    'hide-details': True,
                                                    'menu-props': {'maxHeight': 200},
                                                    'style': 'width: 70px;'
                                                }
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }

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
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 6, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'calc_time',
                                            'label': '根据下载历史补充时间',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 6, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即更新一次数据',
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
                                'props': {'cols': 8, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'calname',
                                            'label': '日历名称',
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
                                            'model': 'interval_minutes',
                                            'label': '时间取整间隔(分钟)',
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
                                'component': 'VExpansionPanels',
                                'props': {
                                    'multiple': False,
                                    'variant': 'accordion'
                                },
                                'content': [
                                    {
                                        'component': 'VExpansionPanel',
                                        'content': [
                                            # 面板标题
                                            {
                                                'component': 'VExpansionPanelTitle',
                                                'content': [
                                                    {
                                                        'component': 'div',
                                                        'text': f"自定义订阅播出时间（{len(subs)}个）",
                                                        'props': {
                                                            'class': 'text-h6 font-weight-bold'
                                                        }
                                                    }
                                                ]
                                            },
                                            # 面板内容
                                            {
                                                'component': 'VExpansionPanelText',
                                                'content': [
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'grid grid-cols-2 gap-6 md:grid-cols-2 lg:grid-cols-3',
                                                        },
                                                        'content': [_build_sub_card(sub) for sub in subs]
                                                    }
                                                ]
                                            }
                                        ]
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
                                'props': {'cols': 12},
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
                                                'props': {
                                                    'innerHTML': (f'ICS文件可<a href="/api/v1/plugin/SubscribeCal/download/calendar.ics?apikey={settings.API_TOKEN}" target="_blank"><u>点此下载</u></a>' 
                                                                if self._enabled else '插件未启用')
                                                }
                                            }
                                        ]
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
                                'props': {'cols': 12},
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
                                                'props': {
                                                    'innerHTML': f'iCal链接：{_domain}{_url}<br>'
                                                                '1. 该链接包含API密钥，请妥善保管防止泄露⚠️⚠️<br>'
                                                                '2. 将iCal链接添加到支持订阅的日历应用（如Outlook、Google Calendar等）<br>'
                                                                f'3. 服务需公网访问，请将{_domain}替换为您的公网IP/域名'
                                                }
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "calc_time": False,
            "onlyonce": False,
            "cron": "",
            "calname": "追剧日历",
            "interval_minutes": 15,
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

    def generate_ics_content(self, calevent: dict[str, CalendarEvent]) -> str:
        """
        构建ics日历文本
        """
        _ics = CalendarEvent().ics_header(self._calname)
        for uniqueid, event in calevent.items():
            _ics += f"{event.to_ics()}\n"
        return _ics + "END:VCALENDAR"

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
        # 去除不存在订阅配置项
        if user_interval := self.get_data(key="UserInterval") or {}:
            sub_ids = [str(sub.id) for sub in subs]
            # 删除已删除的订阅配置项
        if ids := [i for i in user_interval.keys() if i not in sub_ids]:
            self._del_tmp_config(ids=ids, user_interval=user_interval)

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

    def serach_sub(self, sub: Subscribe, cache: bool = True) -> Optional[Tuple[MediaInfo, List[CalendarInfo]]]:
        """搜索订阅"""
        cachekey = f"__cache_{sub.tmdbid}_{sub.year}_{sub.season}__"
        mediainfo = self.chain.recognize_media(tmdbid=sub.tmdbid, doubanid=sub.doubanid, bangumiid=sub.bangumiid,
                                                episode_group=sub.episode_group, mtype=MediaType(sub.type), cache=cache)
        info = None
        if cache and result_cache and cachekey in result_cache:
            logger.info(f"{sub.name}({sub.year}) 使用缓存数据")
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
            logger.debug(f"{sub.name}({sub.year}) 已缓存")
            result_cache[cachekey] = info
        else:
            logger.warn(f"{sub.name}({sub.year}) 获取信息失败")
            return None
        return mediainfo, info

    def media_process(self, sub: Subscribe, mediainfo: MediaInfo, cal_info: list[CalendarInfo]) -> str:
        """
        :param: Subscribe对象
        :param cal_info: TMDB剧集播出时间
        :return: key, List[CalendarEvent]
        """
        _key = SubscribeCal.get_sub_key(sub)
        # 获取用户设定的延迟时间
        user_interval = self.get_data(key="UserInterval") or {}
        minutes = user_interval.get(str(sub.id), None) # 用户设置优先
        if self._calc_time and not minutes\
            and mediainfo.type == MediaType.TV:
            minutes = self.generate_average_time(sub, cal_info)
            if minutes is not None:
                data = self.get_data(key="Interval") or {}
                # 数据库会将int类键转换为str
                data[str(sub.id)] = minutes
                self.save_data(key="Interval", value=data)
        total_episodes = len(cal_info)
        event_data = self.get_event_data(key=_key) or {}
        for epinfo in cal_info:
            # 跳过无日期剧集
            if not epinfo.air_date: continue
            cal = event_data.get(str(epinfo.id), CalendarEvent())
            ## 后续可加入jinja2模板引擎
            title = f"[{epinfo.episode_number}/{total_episodes}]{mediainfo.title} ({mediainfo.year})" if mediainfo.type == MediaType.TV else f"{mediainfo.title} ({mediainfo.year})"
            # 全天事件
            if minutes is not None \
                and epinfo.runtime:
                # start - airdatetime
                dtend = epinfo.utc_airdate(minutes + epinfo.runtime)
            else:
                # 0:00 - 24:00
                dtend = epinfo.utc_airdate(60 * 24)
            cal.summary=title
            cal.description=epinfo.overview
            cal.dtstart=epinfo.utc_airdate(minutes)
            cal.dtend=dtend
            event_data[str(epinfo.id)] = cal.dict()
        # 保存事件数据
        logger.info(f"{mediainfo.title_year} 日历事件处理完成")
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
        计算剧集播放时间
        :param sub: Subscribe对象
        :param cal_info: TMDB剧集播出时间
        :return: float (分钟)
        """
        def verify_downloadhis_note(note: dict[str, str], source: str = "Subscribe") -> bool:
            """
            验证下载记录note是否符合预期
            """
            if not isinstance(note, dict):
                logger.debug(f"{note} is not dict")
                return False
            _source = note.get("source", "").split("|", 1)
            if source != _source[0]:
                logger.debug(f"{note} source is not {source}")
                return False
            return True

        def dynamic_statistical_analysis(delay_time: list[float]) -> Optional[float]:
            """
            动态统计分析方法 - 针对小样本
            主要处理3-15个样本的情况，寻找最稳定的播出时间
            """
            if not delay_time:
                logger.info("动态统计分析: 输入数据为空")
                return None

            # 数据预处理
            sorted_times = sorted(delay_time)
            n_samples = len(sorted_times)

            if n_samples < 3:
                result = sorted_times[0]  # 使用最小值作为保守估计
                logger.info(f"样本数不足3个，使用最小值: {result:.2f}")
                return result

            # 计算相邻值的时间差
            gaps = [sorted_times[i+1] - sorted_times[i] for i in range(n_samples-1)]
            median_gap = statistics.median(gaps)

            # 寻找最密集区间
            best_cluster = []
            min_stdev = float('inf')

            # 使用滑动窗口寻找最稳定的时间区间
            window_size = min(5, max(3, n_samples // 2))
            for i in range(n_samples - window_size + 1):
                window = sorted_times[i:i+window_size]
                stdev = statistics.stdev(window)
                # 更新结果为更稳定的区间
                if stdev < min_stdev:
                    min_stdev = stdev
                    best_cluster = window

            # 根据数据稳定性选择计算方法
            if min_stdev < 100:  # 时间差异小于100分钟，数据较稳定
                result = statistics.median(best_cluster)
                method = "密集区间中位数"
            else:
                # 数据波动较大时，使用加权平均
                weights = [1.0 - (abs(x - statistics.median(best_cluster)) / 
                                (max(best_cluster) - min(best_cluster))) 
                        for x in best_cluster]
                result = sum(x * w for x, w in zip(best_cluster, weights)) / sum(weights)
                method = "密集区间加权平均"

            logger.info(
                f"\n统计分析:\n"
                f"- 样本量: {n_samples}个\n"
                f"- 中位时间差: {median_gap:.1f}分钟\n"
                f"- 最佳区间方差: {min_stdev:.1f}\n"
                f"- 使用方法: {method}\n"
                f"- 密集区间: [{min(best_cluster):.1f}, {max(best_cluster):.1f}]\n"
                f"- 初步结果: {result:.2f}分钟"
            )

            # 应用取整
            if self._interval_minutes:
                orig_result = result
                result = self.quantize_to_interval(result, self._interval_minutes)
                logger.info(f"取整: {orig_result:.2f}m → {result:.2f}m (间隔{self._interval_minutes}分钟)")

            return result

        # 获取下载记录
        if histories := [his for his in self.downloadhis.get_last_by(mtype=sub.type, title=sub.name, year=sub.year, # title, year 参数适配v2.4.0-
                                                                    season=f"S{str(sub.season).rjust(2, '0')}",
                                                                    tmdbid=sub.tmdbid) if verify_downloadhis_note(his.note)]:
            # 初始化分集更新时间
            _his_dt: dict[int, datetime.datetime] = {}
            # 将订阅日期字符串转为datetime对象
            sub_date = datetime.datetime.strptime(sub.date, "%Y-%m-%d %H:%M:%S")
            for history in histories:
                history_date = datetime.datetime.strptime(history.date, "%Y-%m-%d %H:%M:%S")
                if not (history_date - sub_date).total_seconds() < 60 * 10: # 排除订阅添加后的首次更新
                    eps = history.episodes.split("-")
                    episodes = range(int(eps[0][1:]), int(eps[-1][1:]) + 1)
                    for ep in episodes:
                        _his_dt[ep] = history_date
            # 提取tmdb_info的集信息
            _eps_dt: dict = {i.episode_number: i.air_date for i in cal_info}
            # 处理为分钟
            delay_time = [
                self.quantize_to_interval(
                    (_dt - datetime.datetime.strptime(_eps_dt[_ep], "%Y-%m-%d")).total_seconds() / 60, # 得出每集的延迟时间(分钟)
                    self._interval_minutes
                ) # 转换为分钟并向下取整
                for _ep, _dt in _his_dt.items()
                if _ep in _eps_dt
            ]

            return dynamic_statistical_analysis(delay_time)
        else:
            logger.info(f"{sub.name} ({sub.year}) 没有订阅下载记录")
            return None

    @staticmethod
    def quantize_to_interval(time: float, interval_minutes: int = 15) -> float:
        """
        去余取整
        """
        return time - time % interval_minutes

    def save_keys(self, value: list[str]):
        self.save_data(key="__key__", value=value)

    @property
    def keys(self) -> list[str]:
        return self.get_data(key="__key__") or []

    @staticmethod
    def get_sub_key(sub: Subscribe) -> str:
        return f"__key_{sub.tmdbid}_{sub.year}_{sub.season}__"

