import datetime
import statistics
import uuid
from dataclasses import dataclass

from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Response, responses
from pydantic import BaseModel
import pytz

from app.chain.subscribe import Subscribe
from app.core.cache import Cache, fresh
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.modules.themoviedb import TmdbApi
from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaType


result_cache = Cache(maxsize=1000, ttl=3600 * 12)
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
    runtime: Optional[int] = None
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
    created: Optional[str] = None
    # 开始时间
    dtstart: Optional[str] = None
    # 结束时间
    dtend: Optional[str] = None
    # 标题
    summary: Optional[str] = None
    # 描述(备注)
    description: Optional[str] = None
    # 季
    season: Optional[int] = None
    # 集
    episode: Optional[int] = None
    # 地点
    location: Optional[str] = None
    # 唯一标识
    uid: Optional[str] = None
    # 类型
    transp: str = "OPAQUE"
    # 序号
    sequence: int = 0
    # 状态
    status: str = "CONFIRMED"
    # 最后修改时间
    last_modified: Optional[str] = None

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


    def _created_to_ics(self) -> Optional[str]:
        """创建时间"""
        if self.created:
            return f"CREATED;VALUE=DATE-TIME:{self.created}"

    def _dtstart_to_ics(self) -> Optional[str]:
        if self.dtstart:
            return f"DTSTART;VALUE=DATE-TIME:{self.dtstart}"

    def _dtend_to_ics(self) -> Optional[str]:
        if self.dtend:
            return f"DTEND;VALUE=DATE-TIME:{self.dtend}"

    def _summary_to_ics(self) -> Optional[str]:
        """标题"""
        if self.summary:
            return f"SUMMARY:{self.summary}"

    def _description_to_ics(self) -> Optional[str]:
        """备注"""
        if self.description:
            return f"DESCRIPTION:{self.description}"

    def _location_to_ics(self) -> Optional[str]:
        """地点"""
        if self.location:
            return f"LOCATION:{self.location}"

    def _uid_to_ics(self) -> Optional[str]:
        """唯一标识"""
        return f"UID:{self.uid or uuid.uuid4()}"

    def _transp_to_ics(self) -> Optional[str]:
        """类型"""
        return f"TRANSP:{self.transp or 'OPAQUE'}"

    def _sequence_to_ics(self) -> Optional[str]:
        """序号"""
        return f"SEQUENCE:{self.sequence or 0}"

    def _status_to_ics(self) -> Optional[str]:
        """状态"""
        return f"STATUS:{self.status or 'CONFIRMED'}"

    def _last_modified_to_ics(self) -> Optional[str]:
        """最后修改时间"""
        return f"LAST-MODIFIED:{self.last_modified or self._get_utc_time()}"

    @staticmethod
    def ics_header(calname: str = "追剧日历") -> str:
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


class TimeLineItem(BaseModel):
    """时间轴条目"""
    # 订阅表ID
    id: int
    # 起始时间
    dtstart: Optional[str] = None
    # 结束时间
    dtend: Optional[str] = None
    # 标题
    summary: Optional[str] = None
    # 描述
    description: Optional[str] = None
    # 地点
    location: Optional[str] = None
    # 唯一标识
    uid: Optional[str] = None
    # 年份
    year: Optional[str] = None
    # 类型
    type: Optional[str] = None
    # 季号
    season: Optional[int] = None
    # 集号
    episode: Optional[int] = None
    # 海报
    poster: Optional[str] = None
    # 背景图
    backdrop: Optional[str] = None
    # 评分，float
    vote: float = 0.0
    # 状态：N-新建 R-订阅中 P-待定 S-暂停
    state: Optional[str] = None


class SubscribeCal(_PluginBase):
    # 插件名称
    plugin_name = "订阅日历"
    # 插件描述
    plugin_desc = "根据订阅生成日历, 以供导入至设备日历中"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/wikrin/MoviePilot-Plugins/main/icons/calendar_a.png"
    # 插件版本
    plugin_version = "1.2.2"
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
    _search_sub_region = "plugin.subscribecal.search_sub"

    # 配置属性
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = ""
    _calc_time: bool = False
    _calname: str = "追剧日历"
    _interval_minutes: int = 15
    _dashboard_size: int = 6

    def init_plugin(self, config: dict):
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
                "calname",
                "interval_minutes",
                "dashboard_size"
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
                "calname": self._calname,
                "interval_minutes": self._interval_minutes,
                "dashboard_size": self._dashboard_size,
            }
        )

    @staticmethod
    def get_render_mode()  -> tuple[str, str]:
        return "vue", "dist/assets"

    def get_form(self):
        return [], {}

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
        return [
            {
                "path": "/subscribe",
                "endpoint": self.get_ics,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "订阅日历",
                "description": "获取订阅日历",
            },
            {
                "path": "/download/calendar.ics",
                "endpoint": self.download_ics,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "下载日历",
                "description": "下载订阅日历ICS",
            },
            {
                "path": "/grouped_events",
                "endpoint": self.get_grouped_events,
                "methods": ["GET"],
                "auth": "bear",  # 鉴权类型：apikey/bear
                "summary": "获取日历事件",
                "description": "以日期分组获取日历事件",
            },
        ]

    def get_command(self):
        pass

    def get_page(self):
        pass

    def get_state(self):
        return self._enabled

    def get_dashboard(self, key: str, **kwargs) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], Optional[List[dict]]]]:
        """
        获取插件仪表盘页面，需要返回：1、仪表板col配置字典；2、全局配置（布局、自动刷新等）；3、仪表板页面元素配置含数据json（vuetify）或 None（vue模式）
        """
        return (
            {"cols": self._dashboard_size * 2, "md": self._dashboard_size},
            {
                "refresh": 1800,
                "border": True,
                "render_mode": "vue",
                "pluginConfig": {},
            },
            None,
        )

    def generate_ics_content(self, calevent: dict[str, CalendarEvent]) -> str:
        """
        构建ics日历文本
        """
        _ics = CalendarEvent.ics_header(self._calname)
        for uniqueid, event in calevent.items():
            _ics += f"{event.to_ics()}\n"
        return _ics + "END:VCALENDAR"

    def get_ics(self):
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
        subs = SubscribeOper().list()
        # 暂存key
        _tmp_keys = []
        for sub in subs:
            # 跳过洗版
            if sub.best_version: continue
            _info = self.search_sub(sub=sub, cache=cache)
            if not _info: continue
            key = self.media_process(sub, _info)
            if key is None:
                continue
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
        if sub := SubscribeOper().get(event.event_data.get("subscribe_id")):
            _info = self.search_sub(sub=sub, cache=False)
            key = self.media_process(sub, _info)
            if key is None:
                return

            self.keys.append(key)
            # 保存数据
            self.save_keys(self.keys)

    def search_sub(self, sub: Subscribe, cache: bool = True) -> Optional[List[CalendarInfo]]:
        """搜索订阅"""
        cache_params = {"key": self.get_sub_key(sub), "region": self._search_sub_region}
        info = None
        if cache and result_cache and result_cache.exists(**cache_params):
            info = result_cache.get(**cache_params)
            logger.info(f"{sub.name} ({sub.year}) 使用缓存数据")
        else:
            with fresh(not cache):
                if sub.episode_group and (result := TmdbApi().get_tv_group_detail(group_id=sub.episode_group, season=sub.season)):
                    # 将剧集组中季号更新为当前季
                    list(map(lambda x: x.update(season_number=sub.season), result.get('episodes', [])))
                else:
                    result = self.chain.tmdb_info(tmdbid=sub.tmdbid, mtype=MediaType(sub.type), season=sub.season)
            if result:
                info = [CalendarInfo(**info) for info in result.get('episodes', [result])]
        if info:
            result_cache.set(value=info, **cache_params)
            logger.debug(f"{sub.name} ({sub.year}) 已缓存")
        else:
            logger.warn(f"{sub.name} ({sub.year}) 获取信息失败")
        return info

    def media_process(self, sub: Subscribe, cal_info: list[CalendarInfo]) -> Optional[str]:
        """
        :param: Subscribe对象
        :param cal_info: TMDB剧集播出时间
        :return: key, List[CalendarEvent]
        """
        if not cal_info:
            return
        title_year = f"{sub.name} ({sub.year})"
        _key = self.get_sub_key(sub)
        # 剧集播出时间
        minutes = (
            self.generate_average_time(sub, cal_info)
            if self._calc_time and sub.type == MediaType.TV.value
            else None
        )
        # 剧集时长
        valid_runtimes = self.compute_median_runtime(cal_info)
        # 剧集总集数
        total_episodes = len(cal_info)
        event_data = self.get_event_data(key=_key) or {}
        for epinfo in cal_info:
            # 跳过无日期剧集
            if not epinfo.air_date: continue
            cal = event_data.get(str(epinfo.id), CalendarEvent())
            # 标题
            title = f"[{epinfo.episode_number}/{total_episodes}]{title_year}" if sub.type == MediaType.TV.value else f"{title_year}"
            runtime = epinfo.runtime or valid_runtimes
            # 全天事件
            if minutes is not None:
                if runtime:
                    # start - airdatetime
                    dtend = epinfo.utc_airdate(minutes + runtime)
                else:
                    # start - 23:59
                    dtend = epinfo.utc_airdate(-minutes % 1440 + minutes - 1)
            else:
                # 0:00 - 23:59
                dtend = epinfo.utc_airdate(1440 - 1)
            cal.summary=title
            cal.dtstart=epinfo.utc_airdate(minutes)
            cal.dtend=dtend
            cal.season=epinfo.season_number
            cal.episode=epinfo.episode_number
            event_data[str(epinfo.id)] = cal
        # 保存事件数据
        self.save_data(key=_key, value={k: v.dict() for k, v in event_data.items()})
        logger.info(f"{title_year} 日历事件处理完成")
        return _key

    def get_grouped_events(self, before_days: int = 3, after_days: int = 3) -> list[TimeLineItem]:
        """
        返回给前端特定数据
        """
        timeline_items: list[TimeLineItem] = []
        # 日期范围(最近一周)
        date_range = self.get_date_strings(before_days, after_days)
        # 获取所有订阅
        subs = SubscribeOper().list()
        for sub in subs:
            # 跳过洗版
            if sub.best_version: continue
            _key = self.get_sub_key(sub)
            event_data = self.get_event_data(key=_key) or {}
            for _, epinfo in event_data.items():
                date = self.format_date_from_dtstart(epinfo.dtstart)
                if date in date_range:
                    timeline_items.append(TimeLineItem(**{**sub.to_dict(), **epinfo.dict()}))

        return timeline_items

    @staticmethod
    def compute_median_runtime(cal_items: list[CalendarInfo]):
        try:
            return statistics.median([item.runtime for item in cal_items if item.runtime])
        except Exception as e:
            logger.error(f"获取剧集时长中位数失败: {e}")
            return None

    @staticmethod
    def format_date_from_dtstart(dtstart: str) -> str:
        """
        使用字符串切片从 dtstart 提取日期并格式化为 'YYYY-MM-DD'

        :param dtstart: 格式为 "%Y%m%dT%H%M%SZ" 的时间字符串，如 "20250404T123000Z"
        :return: 格式为 'YYYY-MM-DD' 的日期字符串
        """
        return f"{dtstart[0:4]}-{dtstart[4:6]}-{dtstart[6:8]}"

    @staticmethod
    def get_date_strings(before_days: int = 1, after_days: int = 1) -> set[str]:
        """
        获取指定时间范围内的日期字符串列表

        :param days_before: 今天之前的天数（0 表示不包含今天以前）
        :param days_after:  今天之后的天数（0 表示不包含今天以后）
        :return: 包含日期字符串的集合，如 {'2025-04-04', '2025-04-05', '2025-04-06'}
        """
        today = datetime.date.today()
        date_list = [(today + datetime.timedelta(days=i)) for i in range(-before_days, after_days + 1)]
        return {str(date) for date in date_list}

    def save_events(self, value: dict[str, dict[str, CalendarEvent]]):
        # 序列化事件数据
        for key, d in value.items():
            data = {k: v.json() for k, v in d.items()}
            self.save_data(key=key, value=data)

    def get_event_data(self, key: str) -> dict[str, CalendarEvent]:
        """获取日历事件数据"""

        _data = self.get_data(key=key) or {}
        return {k: CalendarEvent.parse_obj(v) for k, v in _data.items()}

    def get_events(self, keys: Optional[list] = None) -> Dict[str, CalendarEvent]:
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
                f"- 最佳区间标准差: {min_stdev:.1f}\n"
                f"- 使用方法: {method}\n"
                f"- 密集区间: [{min(best_cluster):.1f}, {max(best_cluster):.1f}]\n"
                f"- 初步结果: {result:.2f}分钟"
            )

            return result

        # 获取下载记录
        if histories := [his for his in DownloadHistoryOper().get_last_by(mtype=sub.type, title=sub.name, year=sub.year, # title, year 参数适配v2.4.0-
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
            # 剔除订阅补全的历史记录
            if _his_dt:
                sorted_keys = sorted(_his_dt.keys(), reverse=True)
                # 用于记录前一个值
                prev_value = None

                for key in sorted_keys:
                    current_value = _his_dt[key]
                    if prev_value is not None and  current_value > prev_value:
                        # 当前值大于前一个值，则排除该键
                        del _his_dt[key]
                        logger.info(f"{sub.name} - 第 {key} 集 疑似订阅补全记录，已排除, 下载日期: {current_value}")
                        continue

                    # 更新记录值
                    prev_value = current_value

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
