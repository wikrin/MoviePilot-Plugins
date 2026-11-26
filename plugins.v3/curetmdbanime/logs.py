"""日志分页与 SSE 端点；响应生命周期拥有上游连接资源。"""

from __future__ import annotations

import re
from contextlib import AsyncExitStack
from datetime import datetime
from urllib.parse import urlencode

from anyio import CancelScope
from fastapi import Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from .management import ManagementClient
from .runtime import ServiceController, ServiceError, redact

_MAX_LOG_ID = (1 << 64) - 1
_LOG_LEVELS = {"debug", "info", "warn", "error", "fatal"}


class _LogStreamResponse(StreamingResponse):
    """响应生命周期拥有上游上下文，包括未开始读取正文就断开的情况。"""

    def __init__(self, iterator, resources):
        super().__init__(
            iterator,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
        )
        self._resources = resources
        self._closed = False

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        with CancelScope(shield=True):
            try:
                await self.body_iterator.aclose()
            finally:
                await self._resources.aclose()

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.aclose()


class LogAPI:
    def __init__(self, runtime: ServiceController, management: ManagementClient):
        self.runtime = runtime
        self.management = management

    @staticmethod
    def _log_params(request, limit, after, before, level, keyword):
        for name in ("limit", "after", "before"):
            if len(request.query_params.getlist(name)) > 1:
                raise HTTPException(400, f"日志参数 {name} 不能重复")
        if after is not None and before is not None:
            raise HTTPException(400, "after 和 before 不能同时使用")
        level = level.lower()
        if level and level not in _LOG_LEVELS:
            raise HTTPException(400, "不支持的日志级别")
        if len(keyword.encode("utf-8")) > 4096:
            raise HTTPException(400, "日志关键词不能超过 4096 字节")
        params = {"limit": limit, "level": level, "keyword": keyword}
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        return params

    @staticmethod
    def _validate_log_page(value, limit, after, before):
        def log_id(number):
            return type(number) is int and 0 <= number <= _MAX_LOG_ID

        def invalid():
            raise ServiceError("服务日志分页响应无效，请部署包含日志管理接口的新构建")

        items = value.get("items")
        retention = value.get("retention")
        if not isinstance(items, list) or len(items) > limit or not isinstance(retention, dict):
            invalid()
        if not all(log_id(retention.get(key)) for key in ("oldest_id", "newest_id")):
            invalid()
        if retention["oldest_id"] > retention["newest_id"]:
            invalid()
        for key in ("file_count", "max_files", "max_size_bytes"):
            if type(retention.get(key)) is not int or retention[key] < (0 if key == "file_count" else 1):
                invalid()
        if retention["file_count"] > retention["max_files"]:
            invalid()
        if not log_id(value.get("next_after")) or value["next_after"] > retention["newest_id"]:
            invalid()
        if type(value.get("max_limit")) is not int or not limit <= value["max_limit"] <= 2000:
            invalid()
        if type(value.get("has_more")) is not bool or type(value.get("gap")) is not bool:
            invalid()
        reset = value.get("reset", "")
        if reset not in ("", "history_pruned", "cursor_ahead") or value["gap"] != bool(reset):
            invalid()
        previous = 0
        for item in items:
            if not isinstance(item, dict) or not log_id(item.get("id")) or item["id"] <= previous:
                invalid()
            previous = item["id"]
            if not retention["oldest_id"] <= previous <= value["next_after"]:
                invalid()
            if before is not None and previous >= before:
                invalid()
            if after is not None and reset != "cursor_ahead" and previous <= after:
                invalid()
            if (
                not isinstance(item.get("level"), str)
                or item["level"] not in _LOG_LEVELS
                or not isinstance(item.get("message"), str)
            ):
                invalid()
            if not isinstance(item.get("timestamp"), str):
                invalid()
            try:
                if datetime.fromisoformat(item["timestamp"]).tzinfo is None:
                    invalid()
            except ValueError:
                invalid()
        if value["has_more"] and not items:
            invalid()
        reverse = "next_before" in value or "has_older" in value
        if before is not None and not reverse:
            raise ServiceError("服务尚不支持加载更早日志，请更新可执行文件。")
        if reverse:
            if not log_id(value.get("next_before")) or type(value.get("has_older")) is not bool:
                invalid()
            if after is None and value["next_before"] != (items[0]["id"] if items else 0):
                invalid()
            if not items and value["has_older"]:
                invalid()
            if before is not None and value["has_more"] != value["has_older"]:
                invalid()
        return value

    def get_logs(
        self,
        request: Request,
        limit: int = Query(default=200, ge=1, le=2000),
        after: int | None = Query(default=None, ge=0, le=_MAX_LOG_ID),
        before: int | None = Query(default=None, ge=1, le=_MAX_LOG_ID),
        level: str = Query(default="", max_length=20),
        keyword: str = Query(default="", max_length=4096),
    ):
        """只读二进制持久化日志，支持最近页及双向游标。"""
        params = self._log_params(request, limit, after, before, level, keyword)
        if not self.runtime.command_lock.acquire(blocking=False):
            raise HTTPException(409, "服务正在操作或检测，请稍后重试")
        try:
            value = self.management.request("/management/logs?" + urlencode(params))
            return self._validate_log_page(value, limit, after, before)
        except ServiceError as error:
            raise HTTPException(error.status_code, redact(str(error))) from error
        finally:
            self.runtime.command_lock.release()

    def _stream_target(self):
        if not self.runtime.command_lock.acquire(blocking=False):
            raise ServiceError("服务正在操作或检测，请稍后重试", 409)
        try:
            self.management.health("log_stream")
            return self.runtime.target()
        finally:
            self.runtime.command_lock.release()

    async def get_log_stream(
        self,
        request: Request,
        limit: int = Query(default=200, ge=1, le=2000),
        after: int | None = Query(default=None, ge=0, le=_MAX_LOG_ID),
        before: int | None = Query(default=None, ge=1, le=_MAX_LOG_ID),
        level: str = Query(default="", max_length=20),
        keyword: str = Query(default="", max_length=4096),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ):
        """代理实时 SSE；历史由 after 补发，before 仅用于 JSON 查询。"""
        if before is not None:
            raise HTTPException(400, "实时日志流不接受 before，请使用日志查询接口")
        if after is None and last_event_id:
            if not re.fullmatch(r"[0-9]+", last_event_id) or int(last_event_id) > _MAX_LOG_ID:
                raise HTTPException(400, "Last-Event-ID 不是有效日志 ID")
            after = int(last_event_id)
        params = self._log_params(request, limit, after, None, level, keyword)
        resources = AsyncExitStack()
        owned = False
        try:
            target = await run_in_threadpool(self._stream_target)
            response = await resources.enter_async_context(self.runtime.open_log_stream(params, target))
            result = _LogStreamResponse(self.runtime.iter_log_stream(response, target), resources)
            owned = True
            return result
        except ServiceError as error:
            raise HTTPException(error.status_code, redact(str(error))) from error
        except Exception as error:
            raise HTTPException(502, "日志流连接失败或超时") from error
        finally:
            if not owned:
                with CancelScope(shield=True):
                    await resources.aclose()
