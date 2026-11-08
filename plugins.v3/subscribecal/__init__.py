import datetime
import statistics
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, Response, responses
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies.auth import get_current_active_superuser_async
from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.oper.subscribe import SubscribeOper
from app.modules.themoviedb import TmdbApi
from app.schemas.response import Response as ApiResponse
from app.schemas.subscribe import Subscribe
from app.schemas.types import EventType, MediaSource, MediaType
from app.sdk.cache import Cache, fresh
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.plugin.base import _PluginBase

result_cache = Cache(maxsize=1000, ttl=3600 * 12)
GITHUB_API_URL = "https://api.github.com"
# 本地时区
TZ = pytz.timezone(settings.TZ)


@dataclass
class CalendarInfo:
    """
    剧集信息
    :param release_date: 发布日期
    :param air_date: 播出日期
    :param episode_number: 集号(剧集组下不可靠)
    :param episode_type: 集状态
    :param title: 标题(季/电影)
    :param name: 集标题
    :param id: unique ID
    :param overview: 集简介
    :param runtime: 时长
    :param season_number: 季号
    """

    # 发布日期
    release_date: str | None = None
    # 播出日期
    air_date: str | None = None
    # 集号(剧集组下不可靠)
    episode_number: int | None = None
    # 集状态
    episode_type: str | None = None
    # 标题(季/电影)
    title: str | None = None
    # 集标题
    name: str | None = None
    # unique ID
    id: int | None = None
    # 集简介
    overview: str | None = None
    # 时长
    runtime: int | None = None
    # 季号
    season_number: int | None = None

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
        if self.air_date is None:
            self.air_date = self.release_date

    def utc_airdate(self, dalay_minutes: float) -> str:
        """utc时间"""
        return (
            TZ.localize(
                datetime.datetime.strptime(self.air_date, "%Y-%m-%d") + datetime.timedelta(minutes=dalay_minutes or 0)
            )
            .astimezone(pytz.utc)
            .strftime("%Y%m%dT%H%M%SZ")
        )


class CalendarEvent(BaseModel):
    """
    日历事件
    :param created: 创建时间
    :param dtstart: 开始时间
    :param dtend: 结束时间
    :param summary: 标题
    :param description: 描述(备注)
    :param season: 季
    :param episode: 集
    :param location: 地点
    :param uid: 唯一标识
    :param transp: 类型
    :param sequence: 序号
    :param status: 状态
    :param last_modified: 最后修改时间
    """

    # 创建时间
    created: str | None = None
    # 开始时间
    dtstart: str | None = None
    # 结束时间
    dtend: str | None = None
    # 标题
    summary: str | None = None
    # 描述(备注)
    description: str | None = None
    # 季
    season: int | None = None
    # 集
    episode: int | None = None
    # 地点
    location: str | None = None
    # 唯一标识
    uid: str | None = None
    # 类型
    transp: str = "OPAQUE"
    # 序号
    sequence: int = 0
    # 状态
    status: str = "CONFIRMED"
    # 最后修改时间
    last_modified: str | None = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self.uid is None:
            self.uid = str(uuid.uuid4())
            self.created = self._get_utc_time()

    def _get_utc_time(self):
        return datetime.datetime.now(pytz.utc).strftime("%Y%m%dT%H%M%SZ")

    def __setattr__(self, name, value):
        if name in self.__dict__ and self.__dict__[name] == value:
            return
        super().__setattr__(name, value)
        if name != "last_modified":
            super().__setattr__("last_modified", self._get_utc_time())

    def _created_to_ics(self) -> str | None:
        """创建时间"""
        if self.created:
            return f"CREATED;VALUE=DATE-TIME:{self.created}"

    def _dtstart_to_ics(self) -> str | None:
        if self.dtstart:
            return f"DTSTART;VALUE=DATE-TIME:{self.dtstart}"

    def _dtend_to_ics(self) -> str | None:
        if self.dtend:
            return f"DTEND;VALUE=DATE-TIME:{self.dtend}"

    def _summary_to_ics(self) -> str | None:
        """标题"""
        if self.summary:
            return f"SUMMARY:{self.summary}"

    def _description_to_ics(self) -> str | None:
        """备注"""
        if self.description:
            return f"DESCRIPTION:{self.description}"

    def _location_to_ics(self) -> str | None:
        """地点"""
        if self.location:
            return f"LOCATION:{self.location}"

    def _uid_to_ics(self) -> str | None:
        """唯一标识"""
        return f"UID:{self.uid or uuid.uuid4()}"

    def _transp_to_ics(self) -> str | None:
        """类型"""
        return f"TRANSP:{self.transp or 'OPAQUE'}"

    def _sequence_to_ics(self) -> str | None:
        """序号"""
        return f"SEQUENCE:{self.sequence or 0}"

    def _status_to_ics(self) -> str | None:
        """状态"""
        return f"STATUS:{self.status or 'CONFIRMED'}"

    def _last_modified_to_ics(self) -> str | None:
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
        for attr in CalendarEvent.model_fields.keys():
            method_name = f"_{attr}_to_ics"
            if hasattr(self, method_name):
                method = getattr(self, method_name)
                ics_line = method()
                if ics_line:
                    ics_lines.append(ics_line)
        ics_lines.append("END:VEVENT")
        return "\n".join(ics_lines)


class TimeLineItem(BaseModel):
    """
    时间轴条目
    :param id: 订阅表ID
    :param dtstart: 起始时间
    :param dtend: 结束时间
    :param summary: 标题
    :param description: 描述
    :param location: 地点
    :param uid: 唯一标识
    :param year: 年份
    :param type: 类型
    :param season: 季号
    :param episode: 集号
    :param poster: 海报
    :param backdrop: 背景图
    :param vote: 评分，float
    :param state: 状态：N-新建 R-订阅中 P-待定 S-暂停
    """

    # 订阅表ID
    id: int
    # 起始时间
    dtstart: str | None = None
    # 结束时间
    dtend: str | None = None
    # 标题
    summary: str | None = None
    # 描述
    description: str | None = None
    # 地点
    location: str | None = None
    # 唯一标识
    uid: str | None = None
    # 年份
    year: str | None = None
    # 类型
    type: str | None = None
    # 季号
    season: int | None = None
    # 集号
    episode: int | None = None
    # 海报
    poster: str | None = None
    # 背景图
    backdrop: str | None = None
    # 评分，float
    vote: float = 0.0
    # 状态：N-新建 R-订阅中 P-待定 S-暂停
    state: str | None = None


class SubscribeCalConfigStatus(BaseModel):
    """
    订阅日历配置页使用的域名与 Gist 状态。
    :param public_domain: 公网订阅地址使用的域名
    :param public_url: 已包含后端 API key 的完整公网订阅地址
    :param gist_url: 已绑定的 Gist 原始订阅地址；未绑定时为空
    :param gist_available: 主项目 Token 是否已确认可用于 Gist 同步；null 表示仍在检查
    """

    public_domain: str = Field(description="公网订阅地址使用的域名")
    public_url: str = Field(description="已包含后端 API key 的完整公网订阅地址")
    gist_url: str = Field(description="已绑定的 Gist 原始订阅地址；未绑定时为空")
    gist_available: bool | None = Field(
        default=None,
        description="主项目 Token 是否已确认可用于 Gist 同步；null 表示仍在检查",
    )


class SubscribeCalGistStatus(BaseModel):
    """
    订阅日历的 GitHub Gist 检查结果。
    :param available: 主项目 Token 是否可用于当前 Gist 操作
    :param message: Gist 权限检查结果说明
    """

    available: bool = Field(description="主项目 Token 是否可用于当前 Gist 操作")
    message: str = Field(description="Gist 权限检查结果说明")


class SubscribeCalConfig(BaseModel):
    """
    订阅日历插件的运行配置。
    :param enabled: 是否启用订阅日历
    :param onlyonce: 是否在初始化后立即执行一次更新
    :param cron: 日历数据刷新的 Cron 表达式；为空时每 12 小时刷新
    :param calc_time: 是否根据下载记录推算剧集播出时间
    :param calname: 生成的日历名称
    :param interval_minutes: 推算播出时间时使用的分钟取整间隔
    :param dashboard_size: 插件仪表盘占用的宽度
    :param gist_enabled: 是否在日历更新后同步到 GitHub Gist
    :param gist_id: 已绑定的 GitHub Gist 标识；为空时创建新 Gist
    :param gist_url: 已绑定 Gist 中日历文件的原始订阅地址
    """

    model_config = ConfigDict(extra="ignore")

    # 启用插件
    enabled: bool = Field(default=False)
    # 立即执行一次更新
    onlyonce: bool = Field(default=False)
    # 日历数据刷新的 Cron 表达式
    cron: str = Field(default="")
    # 根据下载记录推算剧集播出时间
    calc_time: bool = Field(default=False)
    # 日历名称
    calname: str = Field(default="追剧日历")
    # 推算播出时间时使用的分钟取整间隔
    interval_minutes: int = Field(default=15)
    # 插件仪表盘占用的宽度
    dashboard_size: int = Field(default=6)
    # 是否在日历更新后同步到 GitHub Gist
    gist_enabled: bool = Field(default=False)
    # 已绑定的 GitHub Gist 标识；为空时创建新 Gist
    gist_id: str = Field(default="")
    # 已绑定 Gist 中日历文件的原始订阅地址
    gist_url: str = Field(default="")


class SubscribeCal(_PluginBase):
    # 插件名称
    plugin_name = "订阅日历"
    # 插件描述
    plugin_desc = "根据订阅生成日历, 以供导入至设备日历中"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/wikrin/MoviePilot-Plugins/main/icons/calendar_a.png"
    # 插件版本
    plugin_version = "2.0.1"
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

    def __init__(self):
        super().__init__()
        self.config = SubscribeCalConfig()

    def init_plugin(self, config: dict):
        # 停止现有任务
        self.stop_service()
        self.load_config(config)

        if self.config.onlyonce:
            self.schedule_once()

    def load_config(self, config: dict):
        """加载配置"""
        self.config = SubscribeCalConfig(**{**self.config.model_dump(), **(config or {})})

    def schedule_once(self):
        """调度一次性任务"""
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        logger.info("订阅日历，立即运行一次")
        self._scheduler.add_job(
            func=self.full_update,
            trigger="date",
            run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3),
        )
        self._scheduler.start()

        # 关闭一次性开关
        self.config.onlyonce = False
        self.__update_config()

    def __update_config(self):
        """更新设置"""
        self.update_config(self.config.model_dump())

    @staticmethod
    def get_render_mode() -> tuple[str, str]:
        return "vue", "dist/assets"

    def get_form(self):
        return [], {}

    def get_service(self) -> list[dict[str, Any]]:
        """
        注册插件公共服务
        """
        if self.config.enabled:
            trigger = CronTrigger.from_crontab(self.config.cron) if self.config.cron else "interval"
            kwargs = {"hours": 12} if not self.config.cron else {}
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
            logger.error(f"退出插件失败：{e!s}")

    def get_api(self):
        return [
            {
                "path": "/subscribe",
                "endpoint": self.get_ics,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "订阅日历",
                "description": "获取订阅日历",
                "response_model": None,
                "response_class": Response,
                "responses": {
                    200: {
                        "content": {
                            "text/calendar": {
                                "schema": {"type": "string"},
                            }
                        }
                    }
                },
            },
            {
                "path": "/download/calendar.ics",
                "endpoint": self.download_ics,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "下载日历",
                "description": "下载订阅日历ICS",
                "response_model": None,
                "response_class": responses.StreamingResponse,
                "responses": {
                    200: {
                        "content": {
                            "text/calendar": {
                                "schema": {"type": "string"},
                            }
                        }
                    }
                },
            },
            {
                "path": "/config/status",
                "endpoint": self.get_config_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查询订阅日历配置状态",
                "description": "流式返回公网订阅链接和 GitHub Gist 权限状态",
                "response_model": None,
                "response_class": responses.StreamingResponse,
                "responses": {
                    200: {
                        "content": {
                            "application/x-ndjson": {
                                "schema": {"type": "string"},
                            }
                        }
                    }
                },
                "dependencies": [Depends(get_current_active_superuser_async)],
            },
            {
                "path": "/gist/sync",
                "endpoint": self.sync_gist,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "同步订阅日历到 GitHub Gist",
                "description": "检查 GitHub Token 的 Gist 权限并创建或更新 Secret Gist",
                "response_model": ApiResponse[str],
            },
            {
                "path": "/grouped_events",
                "endpoint": self.get_grouped_events,
                "methods": ["GET"],
                "auth": "bear",  # 鉴权类型：apikey/bear
                "summary": "获取日历事件",
                "description": "以日期分组获取日历事件",
                "response_model": list[TimeLineItem],
            },
        ]

    def get_command(self):
        pass

    def get_page(self):
        pass

    def get_state(self):
        return self.config.enabled

    def get_dashboard(self, key: str, **kwargs) -> tuple[dict[str, Any], dict[str, Any], list[dict] | None] | None:
        """
        获取插件仪表盘页面，需要返回：1、仪表板col配置字典；2、全局配置（布局、自动刷新等）；3、仪表板页面元素配置含数据json（vuetify）或 None（vue模式）
        """
        return (
            {"cols": self.config.dashboard_size * 2, "md": self.config.dashboard_size},
            {
                "refresh": 1800,
                "border": True,
                "render_mode": "vue",
                "pluginConfig": {},
            },
            None,
        )

    def iter_ics_content(self, calevent: dict[str, CalendarEvent] | None = None) -> Iterator[str]:
        """
        逐段生成 ICS 内容，避免下载时构造完整字符串。

        :param calevent: 可选日历事件；未提供时在输出日历头后读取事件
        :return: ICS 文本分块迭代器
        """
        yield CalendarEvent.ics_header(self.config.calname)
        if calevent is None:
            calevent = self.get_events(self.keys)
        for event in calevent.values():
            yield f"{event.to_ics()}\n"
        yield "END:VCALENDAR"

    def generate_ics_content(self, calevent: dict[str, CalendarEvent]) -> str:
        """
        构建ics日历文本
        """
        return "".join(self.iter_ics_content(calevent))

    def get_ics(self):
        """获取ics内容"""
        _events = self.get_events(self.keys)
        return Response(content=self.generate_ics_content(_events), media_type="text/calendar")

    def download_ics(self) -> responses.StreamingResponse:
        """获取ics内容"""
        return responses.StreamingResponse(
            self.iter_ics_content(),
            media_type="text/calendar",
            headers={"Content-Disposition": "attachment; filename=calendar.ics"},
        )

    def _github_gist_context(
        self,
    ) -> tuple[Any | None, str | None, bool | None, bool, str]:
        """
        检查主项目 GitHub Token，并返回 Gist 操作共用的权限结果。

        OAuth/PAT Classic 的 scope 可直接检查；Fine-grained Token 不返回 scope，
        状态查询会提示无法确认，但显式同步仍会尝试实际写入。已绑定 Gist 必须归当前用户且为 Secret。

        :return: GitHub 客户端、用户名、Gist 权限、绑定状态及后端提示
        """
        token = settings.GITHUB_TOKEN
        if not token:
            logger.warning("订阅日历 Gist 检查失败：主项目尚未配置 GitHub Token")
            return None, None, False, False, "主项目尚未配置 GitHub Token。"

        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        client = RequestUtils(headers=headers, proxies=settings.PROXY, timeout=30)
        try:
            user_response = client.get_res(f"{GITHUB_API_URL}/user")
            if not user_response or user_response.status_code != 200:
                status_code = user_response.status_code if user_response else "无响应"
                logger.warning(f"订阅日历 Gist 检查失败：GitHub 用户接口返回 {status_code}")
                return (
                    None,
                    None,
                    False,
                    False,
                    "GitHub Token 无效或无法访问 GitHub API。",
                )
            user_data = user_response.json()
            login = user_data.get("login")
            if not login:
                logger.warning("订阅日历 Gist 检查失败：GitHub 未返回用户标识")
                return None, None, False, False, "GitHub 未返回当前 Token 的用户信息。"

            granted_scopes = {
                scope.strip() for scope in user_response.headers.get("X-OAuth-Scopes", "").split(",") if scope.strip()
            }
            gist_permission = "gist" in granted_scopes if granted_scopes else None
            if gist_permission is False:
                logger.warning(f"订阅日历 Gist 检查失败：GitHub 用户 {login} 的 Token 缺少 gist 权限")
                return (
                    client,
                    login,
                    False,
                    False,
                    "当前 GitHub Token 缺少 gist 权限，请在主项目重新授权或更换 Token。",
                )

            gist_bound = not self.config.gist_id
            if self.config.gist_id:
                existing_response = client.get_res(f"{GITHUB_API_URL}/gists/{self.config.gist_id}")
                if not existing_response or existing_response.status_code != 200:
                    status_code = existing_response.status_code if existing_response else "无响应"
                    logger.warning(f"订阅日历 Gist 检查失败：读取已绑定 Gist 返回 {status_code}")
                    return (
                        client,
                        login,
                        gist_permission,
                        False,
                        "无法读取已绑定的 Gist，请检查 Token 权限或重新同步。",
                    )
                existing_gist = existing_response.json()
                owner = (existing_gist.get("owner") or {}).get("login")
                if owner != login:
                    logger.warning(f"订阅日历 Gist 检查失败：已绑定 Gist 与当前用户 {login} 不匹配")
                    return (
                        client,
                        login,
                        gist_permission,
                        False,
                        "已绑定的 Gist 不属于当前 GitHub Token 用户。",
                    )
                if existing_gist.get("public") is not False:
                    logger.warning("订阅日历 Gist 检查失败：已绑定 Gist 不是 Secret 状态")
                    return (
                        client,
                        login,
                        gist_permission,
                        False,
                        "已绑定的 Gist 不是 Secret Gist，请先解除绑定后重新创建。",
                    )
                gist_bound = True

            if gist_permission is None:
                logger.info(f"订阅日历 Gist 权限无法预检：GitHub 用户 {login}，已绑定 Gist={gist_bound}")
                return (
                    client,
                    login,
                    None,
                    gist_bound,
                    "Token 有效，但无法预检其 Gist 写权限；请改用包含 gist scope 的 Token。",
                )
            logger.info(f"订阅日历 Gist 权限检查通过：GitHub 用户 {login}，已绑定 Gist={gist_bound}")
            return (
                client,
                login,
                True,
                gist_bound,
                "GitHub Token 有效且包含 gist 权限。",
            )
        except Exception as error:
            logger.error(f"检查 GitHub Gist 权限失败：{error!s}")
            return None, None, False, False, "暂时无法检查 GitHub Token 和 Gist 权限。"

    def iter_config_status(self, domain: str | None = None, check_gist: bool | None = None) -> Iterator[str]:
        """
        先输出公网订阅地址，再输出 Gist 网络检查结果。

        :param domain: 可选公网域名；复制用户编辑过的公网地址时由前端传入
        :param check_gist: 是否检查 GitHub Gist；未提供时使用插件同步开关
        :return: 按行分隔的 NDJSON 状态事件
        """
        public_domain = (domain or settings.APP_DOMAIN or "").strip()
        public_url = ""
        if public_domain and settings.API_TOKEN:
            base_url = public_domain
            if not base_url.startswith(("http://", "https://")):
                base_url = f"http://{base_url}"
            endpoint = f"{base_url.rstrip('/')}{settings.API_V1_STR}/plugin/SubscribeCal/subscribe"
            public_url = f"{endpoint}?{urlencode({'apikey': settings.API_TOKEN})}"
        config_status = SubscribeCalConfigStatus(
            public_domain=public_domain,
            public_url=public_url,
            gist_url=self.config.gist_url,
        )
        yield f'{{"type":"config","data":{config_status.model_dump_json()}}}\n'

        if check_gist is None:
            check_gist = self.config.gist_enabled
        if check_gist:
            _, _, gist_permission, gist_bound, message = self._github_gist_context()
        else:
            gist_permission, gist_bound, message = False, False, "Gist 同步未启用。"
        gist_status = SubscribeCalGistStatus(
            available=gist_permission is True and gist_bound,
            message=message,
        )
        yield f'{{"type":"gist","data":{gist_status.model_dump_json()}}}\n'

    def get_config_status(
        self,
        domain: str | None = None,
        check_gist: bool | None = None,
    ) -> responses.StreamingResponse:
        """
        流式返回公网配置和 GitHub Gist 权限状态。

        :param domain: 可选公网域名；复制用户编辑过的公网地址时由前端传入
        :param check_gist: 是否检查 GitHub Gist；未提供时使用插件同步开关
        :return: 逐条输出状态事件的 NDJSON 响应
        """
        return responses.StreamingResponse(
            self.iter_config_status(domain, check_gist),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def sync_gist(self) -> ApiResponse[str]:
        """
        将当前日历发布到主项目 GitHub Token 所属用户的 Secret Gist。

        Token 和已绑定 Gist 的身份校验与状态查询共用同一后端路径；Fine-grained Token 的写权限由本次实际写入确认。

        :return: 是否同步成功及可订阅的 ICS 地址
        """
        client, login, gist_permission, gist_bound, message = self._github_gist_context()
        if gist_permission is False:
            logger.warning(f"订阅日历 Gist 同步已取消：{message}")
            return ApiResponse(success=False, message=message)
        if self.config.gist_id and not gist_bound:
            logger.warning(f"订阅日历 Gist 同步已取消：{message}")
            return ApiResponse(success=False, message=message)

        try:
            events: dict[str, CalendarEvent] = {}
            for key in self.keys:
                events.update(self.get_event_data(key=key))
            action = "更新" if self.config.gist_id else "创建"
            logger.info(f"开始{action} GitHub Secret Gist：事件数 {len(events)}")
            gist_filename = self.__class__.__name__ + ".ics"
            gist_payload = {
                "description": "MoviePilot 订阅日历",
                "public": False,
                "files": {
                    gist_filename: {"content": self.generate_ics_content(events)},
                },
            }
            if self.config.gist_id:
                response = client.request(
                    method="patch",
                    url=f"{GITHUB_API_URL}/gists/{self.config.gist_id}",
                    json=gist_payload,
                )
            else:
                response = client.post_res(f"{GITHUB_API_URL}/gists", json=gist_payload)
            if not response or response.status_code not in (200, 201):
                status_code = response.status_code if response else "无响应"
                logger.warning(f"GitHub Gist {action}失败：接口返回 {status_code}")
                if response and response.status_code == 403:
                    message = "GitHub Token 没有创建或更新 Gist 的权限，请检查 gist 权限。"
                else:
                    message = "GitHub Gist 同步失败，请检查 Token 权限及网络连接。"
                return ApiResponse(success=False, message=message)

            gist_data = response.json()
            if gist_data.get("public") is not False:
                logger.error("GitHub Gist 同步失败：接口返回的 Gist 不是 Secret 状态")
                return ApiResponse(
                    success=False,
                    message="GitHub 返回的 Gist 不是 Secret 状态，已停止保存订阅地址。",
                )
            self.config.gist_id = gist_data["id"]
            self.config.gist_url = (
                f"https://gist.githubusercontent.com/{login}/{self.config.gist_id}/raw/{gist_filename}"
            )
            self.__update_config()
            logger.info(
                f"订阅日历已同步到 GitHub Secret Gist：用户 {login}，Gist {self.config.gist_id}，事件数 {len(events)}"
            )
            return ApiResponse(
                success=True,
                message="Gist 同步成功。",
                data=self.config.gist_url,
            )
        except Exception as error:
            logger.error(f"同步订阅日历到 GitHub Gist 失败：{error!s}")
            return ApiResponse(
                success=False,
                message="GitHub Gist 同步失败，请检查网络和 Token 权限。",
            )

    def full_update(self, cache: bool = False):
        """
        日历全量更新
        """
        # 获取订阅
        subs = SubscribeOper().list()
        logger.info(f"订阅日历开始全量更新：订阅数 {len(subs)}，缓存模式={cache}")
        # 暂存key
        _tmp_keys = []
        skipped_best_version = 0
        skipped_non_tmdb = 0
        skipped_missing_info = 0
        skipped_missing_key = 0
        for sub in subs:
            # 跳过洗版
            if sub.best_version:
                skipped_best_version += 1
                continue
            # 跳过非TMDB
            if sub.media_source != MediaSource.TMDB.value:
                skipped_non_tmdb += 1
                continue
            _info = self.search_sub(sub=sub, cache=cache)
            if not _info:
                skipped_missing_info += 1
                continue
            key = self.media_process(sub, _info)
            if key is None:
                skipped_missing_key += 1
                continue
            _tmp_keys.append(key)
        # 去除删除的订阅
        if _d := set(self.keys) - set(_tmp_keys):
            for key in _d:
                self.del_data(key)
        else:
            _d = set()
        # 保存key
        self.save_keys(_tmp_keys)
        logger.info(
            "订阅日历全量更新完成："
            f"有效订阅 {len(_tmp_keys)}，删除过期订阅 {len(_d)}，"
            f"跳过洗版 {skipped_best_version}、非 TMDB {skipped_non_tmdb}、"
            f"无媒体信息 {skipped_missing_info}、无有效事件键 {skipped_missing_key}"
        )
        if self.config.gist_enabled:
            self.sync_gist()

    @eventmanager.register(EventType.SubscribeAdded)
    def sub_add_event(self, event: Event):
        if not event or not self.config.enabled:
            logger.debug("订阅日历忽略新增订阅事件：事件为空或插件未启用")
            return
        subscribe_id = event.event_data.get("subscribe_id")
        logger.info(f"订阅日历收到新增订阅事件：订阅 ID {subscribe_id}")
        if sub := SubscribeOper().get(subscribe_id):
            _info = self.search_sub(sub=sub, cache=False)
            key = self.media_process(sub, _info)
            if key is None:
                logger.warning(f"新增订阅 {sub.name} ({sub.year}) 未生成日历事件")
                return

            self.keys.append(key)
            # 保存数据
            self.save_keys(self.keys)
            logger.info(f"新增订阅 {sub.name} ({sub.year}) 已加入日历")
            if self.config.gist_enabled:
                self.sync_gist()
        else:
            logger.warning(f"订阅日历未找到新增订阅记录：订阅 ID {subscribe_id}")

    def search_sub(self, sub: Subscribe, cache: bool = True) -> list[CalendarInfo] | None:
        """搜索订阅"""
        cache_params = {"key": self.get_sub_key(sub), "region": self._search_sub_region}
        info = None
        if cache and result_cache and result_cache.exists(**cache_params):
            info = result_cache.get(**cache_params)
            logger.debug(f"{sub.name} ({sub.year}) 使用缓存媒体信息")
        else:
            logger.debug(
                f"查询订阅媒体信息：{sub.name} ({sub.year})，来源={'剧集组' if sub.episode_group else 'TMDB详情'}"
            )
            with fresh(not cache):
                if sub.episode_group and (
                    result := TmdbApi().get_tv_group_detail(group_id=sub.episode_group, season=sub.season)
                ):
                    # 将剧集组中季号更新为当前季
                    list(
                        map(
                            lambda x: x.update(season_number=sub.season),
                            result.get("episodes", []),
                        )
                    )
                else:
                    result = self.chain.tmdb_info(
                        tmdbid=sub.media_id,
                        mtype=MediaType(sub.type),
                        season=sub.season,
                    )
            if result:
                info = [CalendarInfo(**info) for info in result.get("episodes", [result])]
        if info:
            result_cache.set(value=info, **cache_params)
            logger.debug(f"{sub.name} ({sub.year}) 获取到 {len(info)} 条媒体信息并已缓存")
        else:
            logger.warning(f"{sub.name} ({sub.year}) 未获取到媒体信息")
        return info

    def media_process(self, sub: Subscribe, cal_info: list[CalendarInfo]) -> str | None:
        """
        :param: Subscribe对象
        :param cal_info: TMDB剧集播出时间
        :return: key, List[CalendarEvent]
        """
        if not cal_info:
            logger.warning(f"{sub.name} ({sub.year}) 没有媒体信息，跳过日历事件处理")
            return
        title_year = f"{sub.name} ({sub.year})"
        _key = self.get_sub_key(sub)
        # 剧集播出时间
        minutes = (
            self.generate_average_time(sub, cal_info)
            if self.config.calc_time and sub.type == MediaType.TV.value
            else None
        )
        # 剧集时长
        valid_runtimes = self.compute_median_runtime(cal_info)
        # 剧集总集数
        total_episodes = len(cal_info)
        event_data = self.get_event_data(key=_key) or {}
        skipped_missing_air_date = 0
        for epinfo in cal_info:
            # 跳过无日期剧集
            if not epinfo.air_date:
                skipped_missing_air_date += 1
                continue
            cal = event_data.get(str(epinfo.id), CalendarEvent())
            # 标题
            title = (
                f"[{epinfo.episode_number}/{total_episodes}]{title_year}"
                if sub.type == MediaType.TV.value
                else f"{title_year}"
            )
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
            cal.summary = title
            cal.dtstart = epinfo.utc_airdate(minutes)
            cal.dtend = dtend
            cal.season = epinfo.season_number
            cal.episode = epinfo.episode_number
            event_data[str(epinfo.id)] = cal
        # 保存事件数据
        self.save_data(key=_key, value={k: v.model_dump() for k, v in event_data.items()})
        logger.info(
            f"{title_year} 日历事件处理完成：写入 {total_episodes - skipped_missing_air_date} 条，"
            f"跳过无播出日期 {skipped_missing_air_date} 条"
        )
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
            if sub.best_version:
                continue
            _key = self.get_sub_key(sub)
            event_data = self.get_event_data(key=_key) or {}
            for _, epinfo in event_data.items():
                date = self.format_date_from_dtstart(epinfo.dtstart)
                if date in date_range:
                    timeline_items.append(TimeLineItem(**{**sub.to_dict(), **epinfo.model_dump()}))

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
            data = {k: v.model_dump_json() for k, v in d.items()}
            self.save_data(key=key, value=data)

    def get_event_data(self, key: str) -> dict[str, CalendarEvent]:
        """获取日历事件数据"""

        _data = self.get_data(key=key) or {}
        return {k: CalendarEvent.model_validate(v) for k, v in _data.items()}

    def get_events(self, keys: list | None = None) -> dict[str, CalendarEvent]:
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

        def dynamic_statistical_analysis(delay_time: list[float]) -> float | None:
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
            gaps = [sorted_times[i + 1] - sorted_times[i] for i in range(n_samples - 1)]
            median_gap = statistics.median(gaps)

            # 寻找最密集区间
            best_cluster = []
            min_stdev = float("inf")

            # 使用滑动窗口寻找最稳定的时间区间
            window_size = min(5, max(3, n_samples // 2))
            for i in range(n_samples - window_size + 1):
                window = sorted_times[i : i + window_size]
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
                weights = [
                    1.0 - (abs(x - statistics.median(best_cluster)) / (max(best_cluster) - min(best_cluster)))
                    for x in best_cluster
                ]
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
        if histories := [
            his
            for his in DownloadHistoryOper().get_last_by(
                mtype=sub.type,
                title=sub.name,
                year=sub.year,  # title, year 参数适配v2.4.0-
                season=f"S{str(sub.season).rjust(2, '0')}",
                media_source=sub.media_source,
                media_id=sub.media_id,
            )
            if verify_downloadhis_note(his.note)
        ]:
            # 初始化分集更新时间
            _his_dt: dict[int, datetime.datetime] = {}
            # 将订阅日期字符串转为datetime对象
            sub_date = datetime.datetime.strptime(sub.date, "%Y-%m-%d %H:%M:%S")
            for history in histories:
                history_date = datetime.datetime.strptime(history.date, "%Y-%m-%d %H:%M:%S")
                if not (history_date - sub_date).total_seconds() < 60 * 10:  # 排除订阅添加后的首次更新
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
                    if prev_value is not None and current_value > prev_value:
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
                    (_dt - datetime.datetime.strptime(_eps_dt[_ep], "%Y-%m-%d")).total_seconds()
                    / 60,  # 得出每集的延迟时间(分钟)
                    self.config.interval_minutes,
                )  # 转换为分钟并向下取整
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
        return f"__key_{sub.media_source}_{sub.media_id}_{sub.year}_{sub.season}__"
