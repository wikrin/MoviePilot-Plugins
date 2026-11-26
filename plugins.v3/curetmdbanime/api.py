"""
MoviePilot 实例 HTTP 接口；组件绑定方法直接作为路由端点。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Literal

from app.sdk.logging import logger
from fastapi import HTTPException, Query
from fastapi.responses import StreamingResponse

from .config import CleanupRequest, ControlRequest, CureTMDbAnimeConfig
from .history import HistoryStore
from .logs import LogAPI
from .management import ManagementClient
from .monitor import UpstreamMonitor
from .patch import MonkeyPatchManager
from .runtime import ServiceController, ServiceError, redact


class PluginAPI:
    """
    提供实例作用域的服务管理与聚合历史接口。
    """

    def __init__(
        self,
        *,
        runtime: ServiceController,
        patch_manager: MonkeyPatchManager,
        management: ManagementClient,
        monitor: UpstreamMonitor,
        config_provider: Callable[[], CureTMDbAnimeConfig],
        history_provider: Callable[[], HistoryStore | None],
    ):
        """
        注入运行组件与当前配置、历史记录对象，避免重载后读取旧实例。
        """
        self.runtime = runtime
        self.patch_manager = patch_manager
        self.management = management
        self.monitor = monitor
        self.logs = LogAPI(runtime, management)
        self._config_provider = config_provider
        self._history_provider = history_provider

    @property
    def config(self) -> CureTMDbAnimeConfig:
        """
        读取当前已保存配置。
        """
        return self._config_provider()

    @property
    def history(self) -> HistoryStore | None:
        """
        读取当前历史存储，初始化前返回空值。
        """
        return self._history_provider()

    def get_api(self):
        """
        声明 bearer 鉴权的实例路由。
        """
        # 沿用插件 bearer 鉴权，不额外引入管理员权限限制。
        routes = [
            ("/status", self.get_status, "GET"),
            ("/probe", self.probe_service, "POST"),
            ("/logs", self.logs.get_logs, "GET"),
            ("/upstreams", self.get_upstreams, "GET"),
            ("/upstreams/history", self.get_upstream_history, "GET"),
            ("/control", self.control_service, "POST"),
            ("/history", self.get_history, "GET"),
            ("/history/records", self.get_history_records, "GET"),
            ("/history/cleanup", self.cleanup_history, "POST"),
        ]
        return [
            {
                "path": path,
                "endpoint": endpoint,
                "methods": [method],
                "auth": "bear",
                "summary": endpoint.__doc__ or path,
                "response_model": dict,
            }
            for path, endpoint, method in routes
        ] + [
            {
                "path": "/logs/stream",
                "endpoint": self.logs.get_log_stream,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "实时接收二进制应用日志并按游标补发",
                "response_model": None,
                "response_class": StreamingResponse,
                "responses": {200: {"content": {"text/event-stream": {}}}},
            }
        ]

    def get_status(self):
        """
        读取当前服务状态与管理能力。
        """
        status = self.runtime.snapshot()
        return {
            **status,
            "enabled": self.config.enabled,
            "patched": self.patch_manager.is_patched(),
            "correction_enabled": self.config.enable_correction,
        }

    def probe_service(self):
        """
        手动检测已保存的服务地址，不写入上游自动历史。
        """
        if not self.runtime.command_lock.acquire(blocking=False):
            raise HTTPException(409, "服务正在操作或检测，请稍后重试")
        try:
            return {"health": self.management.probe()}
        except ServiceError as error:
            raise HTTPException(502, str(error)) from error
        finally:
            self.runtime.command_lock.release()

    def get_upstreams(self):
        """
        手动检测 TMDB、TVDB、Bangumi，不计入自动历史和统计。
        """
        if not self.runtime.command_lock.acquire(blocking=False):
            raise HTTPException(409, "服务正在操作或检测，请稍后重试")
        try:
            return self.management.request("/management/upstreams", timeout=8)
        except ServiceError as error:
            raise HTTPException(error.status_code, str(error)) from error
        finally:
            self.runtime.command_lock.release()

    def get_upstream_history(self):
        """
        只读最近 24 小时的半小时自动监测历史，不触发探测。
        """
        try:
            return self.monitor.snapshot()
        except ServiceError as error:
            raise HTTPException(error.status_code, str(error)) from error

    def control_service(self, request: ControlRequest):
        """
        控制托管二进制；外部容器不支持生命周期操作。
        """
        if self.config.service_mode != "local":
            raise HTTPException(409, "外部服务由容器部署工具管理，不能操作本地二进制")
        if not self.runtime.command_lock.acquire(blocking=False):
            raise HTTPException(409, "服务正在执行其他操作，请稍后刷新")
        try:
            action = request.action
            if action in {"start", "restart"} and not self.config.enabled:
                raise HTTPException(409, "请先启用插件并保存配置")
            if action == "start" and self.runtime.thread and self.runtime.thread.is_alive():
                raise HTTPException(409, "服务任务已在运行，无需重复启动")
            if action in {"stop", "restart", "update", "download"}:
                self.runtime.stop()
            if action in {"download", "update"}:
                self.runtime.event = threading.Event()
                self.runtime.download()
                self.runtime.state = "stopped"
            if action in {"start", "restart"}:
                self.monitor.prepare()
                self.runtime.start(self.config, self.management.token())
            return self.get_status()
        except HTTPException:
            raise
        except Exception as error:
            self.runtime.state, self.runtime.error = "error", redact(str(error))
            self.runtime.log("error", self.runtime.error)
            raise HTTPException(502, "操作失败，请查看插件日志") from error
        finally:
            self.runtime.command_lock.release()

    def get_history(
        self,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        query: str = Query(
            default="",
            max_length=200,
            description="媒体名、原资源标题或副标题的完整字面子串，大小写不敏感",
        ),
        result: Literal["", "changed", "unchanged", "skipped", "failed"] = "",
    ):
        """
        按媒体分页查询聚合资源及分类统计，不强制保存尚未保存的记录。

        :param query: 不拆词的字面子串，百分号和下划线不是通配符
        :param result: 曾出现的分类；空值显示资源最新摘要
        :return: 媒体页、资源数、累计处理次数及待保存状态
        """
        if not self.history:
            raise HTTPException(503, "历史存储尚未初始化")
        try:
            return self.history.groups(page, page_size, query, result)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        except Exception as error:
            logger.warning(f"修正历史读取失败：{redact(str(error))}")
            raise HTTPException(503, "历史读取暂不可用，请查看插件日志后重试") from error

    def get_history_records(
        self,
        media_key: str = Query(min_length=1, max_length=200),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        result: Literal["", "changed", "unchanged", "skipped", "failed"] = "",
        query: str = Query(
            default="",
            max_length=200,
            description="继承媒体列表搜索条件，仅返回匹配资源",
        ),
    ):
        """
        读取媒体内匹配的资源对象，每个资源只返回一行。

        :param media_key: 列表返回的稳定媒体标识
        :param query: 与列表一致的字面搜索条件
        :param result: 曾出现分类；选中时显示该分类最新摘要
        :return: 聚合资源页及待保存状态，不返回每次处理的单独明细
        """
        if not self.history:
            raise HTTPException(503, "历史存储尚未初始化")
        try:
            return self.history.records(media_key, page, page_size, result, query=query)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        except Exception as error:
            logger.warning(f"修正资源明细读取失败：{redact(str(error))}")
            raise HTTPException(503, "历史明细暂不可用，请查看插件日志后重试") from error

    def cleanup_history(self, request: CleanupRequest):
        """
        清理资源或指定分类，部分媒体失败时明确报告部分完成。

        :param request: 已确认的清理范围；筛选覆盖所有匹配页
        :return: 受影响对象、删除分类、移除调用次数及失败媒体
        """
        if not request.confirmed:
            raise HTTPException(400, "请确认历史清理范围")
        if not self.history:
            raise HTTPException(503, "历史存储尚未初始化")
        try:
            return self.history.cleanup(request)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        except Exception as error:
            logger.warning(f"修正历史清理失败：{redact(str(error))}")
            raise HTTPException(503, "历史清理暂不可用，未确认的范围不能视为成功，请重试") from error
