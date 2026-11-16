"""
种子格式化工作台：预览、单种子执行、自动处理与历史恢复编排。
"""

import threading
from copy import deepcopy
from pathlib import Path
from typing import Literal
from urllib.parse import unquote
from uuid import uuid4

from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, HTTPException, Query
from sqlalchemy import or_, update

from app.api.dependencies.auth import get_current_active_superuser
from app.db.models.downloadhistory import DownloadFiles, DownloadHistory
from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.oper.plugindata import PluginDataOper
from app.db.oper.transferhistory import TransferHistoryOper
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.query import DownloadHistoryFilter, QueryPageRequest
from app.schemas.types import EventType, MediaSource, MediaType, SystemConfigKey
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.media import MediaInfo, MetaBase, MetaInfo, MetaInfoPath
from app.sdk.plugin.base import _PluginBase
from app.sdk.services import DownloaderHelper

from .downloaders import (
    Downloader,
    DownloaderUnavailable,
    QbittorrentDownloader,
    TransmissionDownloader,
    resolve_downloader,
)
from .execution import (
    PreviewExpired,
    PreviewStore,
    execute_plan,
    matches_target,
    project_database_paths,
    reconcile_execution,
)
from .history import (
    HistoryReadContext,
    HistorySummaryCache,
    HistorySummaryResult,
    HistoryTarget,
    locate_history_target,
)
from .models import (
    DeleteHistoryBatchItem,
    DeleteHistoryBatchRequest,
    DeleteHistoryBatchResponse,
    DeleteHistoryRequest,
    DeleteHistoryResult,
    DeleteHistoryResponse,
    ExecutionResponse,
    FormatterRules,
    FrozenPlan,
    HistoryItem,
    HistoryListResponse,
    HistoryLocation,
    HistoryLookupRequest,
    HistoryLookupResponse,
    HistoryLookupResult,
    HistoryPresence,
    MediaBrief,
    PreviewRequest,
    PreviewResponse,
    ProcessedTorrentData,
    ProcessRequest,
    RecoverRequest,
    RulesResponse,
    TargetRequest,
    TorrentData,
    TorrentDetail,
    TorrentFile,
    TorrentFormatterConfig,
    TorrentInfo,
    TorrentListResponse,
    TorrentSummary,
    identity_key,
    snapshot_from_dict,
)
from .planner import (
    SkipFile,
    build_format_preview,
    exclusion_reason,
    fingerprint,
    snapshot_fingerprint,
)
from .recovery import build_recovery_preview

__all__ = [
    "TorrentFormatter",
    "TorrentFile",
    "TorrentInfo",
    "Downloader",
    "QbittorrentDownloader",
    "TransmissionDownloader",
]


class TorrentFormatter(_PluginBase):
    """
    共享规则驱动的单种子工作台，所有修改都先备份且可回读。
    """

    # 插件名称
    plugin_name = "种子格式化"
    # 插件描述
    plugin_desc = (
        "根据自定义格式修改MP下载种子的保存路径、种子名称、种子文件名(实验功能)"
    )
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/wikrin/MoviePilot-Plugins/main/icons/alter_1.png"
    # 插件版本
    plugin_version = "2.1.0"
    # 插件作者
    plugin_author = "Attente"
    # 作者主页
    author_url = "https://github.com/wikrin"
    # 插件配置项ID前缀
    plugin_config_prefix = "torrentformatter_"
    # 加载顺序
    plugin_order = 33
    # 可使用的用户级别
    auth_level = 1

    config: TorrentFormatterConfig

    def __init__(self):
        """
        创建实例级完整配置，命名、调度和排除属性不再分散存储。
        """
        super().__init__()
        self.config = TorrentFormatterConfig()

    def init_plugin(self, config: dict | None = None):
        """
        先校验配置，再重载实例缓存；错误配置不破坏已生效状态。

        :param config: 已保存的扁平配置
        """
        validated = TorrentFormatterConfig.model_validate(config or {})
        if not hasattr(self, "_preview_store"):
            self._lock = threading.RLock()
            self._preview_store = PreviewStore()
        if not hasattr(self, "_history_summary_cache"):
            self._history_summary_cache = HistorySummaryCache()
        with self._lock:
            self.stop_service()
            self.downloadhis = DownloadHistoryOper()
            self.transferhis = TransferHistoryOper()
            self.load_config(validated)

    def load_config(self, config: dict | TorrentFormatterConfig | None):
        """
        原子替换完整 Pydantic 配置，兼容已保存键名、空模板和关闭开关。

        :param config: 全局配置，不接收本次临时规则
        """
        self.config = TorrentFormatterConfig.model_validate(config or {})

    @staticmethod
    def get_render_mode() -> tuple[str, str | None]:
        """
        保留宿主联邦组件入口。

        :return: Vue 渲染模式及部署目录
        """
        return "vue", "dist/assets"

    def get_form(self):
        """
        全局配置由联邦 Config 提供。

        :return: 空的传统表单
        """
        return [], {}

    def get_page(self):
        """
        数据页由联邦 Page 提供。
        """
        return None

    def get_state(self):
        """
        自动模式是否开启；手动 API 不依赖此开关。

        :return: 自动处理状态
        """
        return self.config.event_enabled or self.config.cron_enabled

    def get_service(self) -> list[dict]:
        """
        注册已有定时处理服务。

        :return: 调度声明
        """
        if not self.config.cron_enabled:
            return []
        return [
            {
                "id": "TorrentFormatter",
                "name": "种子格式化自动扫描",
                "trigger": CronTrigger.from_crontab(self.config.cron or "0 8 * * *"),
                "func": self.cron_process_main,
                "kwargs": {},
            }
        ]

    def stop_service(self):
        """
        清除内存预览，持久化日志留给回查和恢复。
        """
        if store := getattr(self, "_preview_store", None):
            store.clear()
        if cache := getattr(self, "_history_summary_cache", None):
            cache.clear()

    def get_api(self):
        """
        声明准确的请求/响应契约，保留三项旧接口的响应形态。

        :return: bearer 鉴权路由
        """
        routes = [
            ("/rules", self.get_rules, "GET", RulesResponse, "读取有效规则"),
            (
                "/torrents",
                self.get_torrents,
                "GET",
                TorrentListResponse,
                "分页选择种子",
            ),
            (
                "/torrent_detail",
                self.get_torrent_detail,
                "GET",
                TorrentDetail,
                "读取种子实时详情",
            ),
            (
                "/preview",
                self.preview_torrent,
                "POST",
                PreviewResponse,
                "只读预览格式化或恢复",
            ),
            (
                "/process",
                self.process_torrent,
                "POST",
                ExecutionResponse,
                "执行已确认的单种子计划",
            ),
            (
                "/operation_status",
                self.get_operation_status,
                "GET",
                ExecutionResponse,
                "回查实际执行结果",
            ),
            (
                "/history",
                self.get_history,
                "GET",
                HistoryListResponse,
                "分页读取新旧历史",
            ),
            (
                "/history_lookup",
                self.history_lookup,
                "POST",
                HistoryLookupResponse,
                "按身份实时核验历史",
            ),
            (
                "/delete_history_batch",
                self.delete_history_batch,
                "POST",
                DeleteHistoryBatchResponse,
                "批量删除插件备份记录",
            ),
            (
                "/delete_history",
                self.delete_history,
                "POST",
                DeleteHistoryResponse,
                "仅删除插件备份记录",
            ),
            (
                "/recover_from_history",
                self.recover_from_history,
                "POST",
                tuple[bool, str],
                "从备份恢复",
            ),
            (
                "/processed_data",
                self.get_processed_data,
                "GET",
                ProcessedTorrentData,
                "兼容旧处理记录",
            ),
            (
                "/torrent_data",
                self.get_torrent_data,
                "GET",
                TorrentData | None,
                "兼容旧备份摘要",
            ),
        ]
        # 与宿主插件配置使用同一权限：临时 Jinja2 规则及源目录操作只允许管理员。
        return [
            {
                "path": path,
                "endpoint": endpoint,
                "methods": [method],
                "auth": "bear",
                "summary": summary,
                "response_model": model,
                "dependencies": [Depends(get_current_active_superuser)],
            }
            for path, endpoint, method, model, summary in routes
        ]

    def get_module(self) -> dict:
        """
        保留删除种子时的记录清理钩子。

        :return: 系统模块钩子
        """
        return {"remove_torrents": self.on_remove_torrents}

    @staticmethod
    def _adapter(name: str) -> Downloader:
        """
        将不可用连接转换为明确的 API 错误，不回退到其他下载器。

        :param name: 精确下载器名称
        :return: 局部适配器
        """
        try:
            return resolve_downloader(name)
        except DownloaderUnavailable as error:
            raise HTTPException(503, str(error)) from error

    @staticmethod
    def _live(adapter: Downloader, torrent_hash: str) -> TorrentInfo:
        """
        获取单种子详情，连接异常不是空列表/成功。

        :param adapter: 局部连接
        :param torrent_hash: 精确 Hash
        :return: 当前快照
        """
        try:
            torrent = adapter.get_torrent(torrent_hash)
        except Exception as error:
            raise HTTPException(502, f"读取下载器失败：{error}") from error
        if torrent is None:
            raise HTTPException(404, "指定下载器中已不存在该种子；不会删除备份")
        return torrent

    def get_rules(self) -> RulesResponse:
        """
        返回工作台允许覆盖的有效规则，不暴露下载器凭据。

        :return: 命名规则和排除说明
        """
        return RulesResponse(
            effective=self.effective_rules(),
            exclude_dirs=self.config.exclude_dirs,
            exclude_tags=self.config.exclude_tags,
            exclude_words=list(self.exclude_words()),
        )

    def get_torrents(
        self,
        downloader: str = Query(min_length=1, max_length=200),
        query: str = Query(default="", max_length=200),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=25, ge=1, le=100),
    ) -> TorrentListResponse:
        """
        一次摘要列表调用后搜索分页，文件仅在选中详情时加载。

        :param downloader: 下载器名称
        :param query: 名称或 Hash 搜索
        :param page: 页码
        :param page_size: 每页数量
        :return: 列表与能力声明
        """
        adapter = self._adapter(downloader)
        try:
            items = adapter.list_torrents()
        except Exception as error:
            raise HTTPException(502, f"无法读取种子列表：{error}") from error
        query = query.strip().casefold()
        items = sorted(
            (
                item
                for item in items
                if not query or query in item.name.casefold() or query in item.hash
            ),
            key=lambda item: (item.name.casefold(), item.hash),
        )
        context = HistoryReadContext(self.get_data)
        index = context.history_index()
        processed_ids = set(index)
        origins = [(row["downloader"], row["torrent_hash"]) for row in index.values()]
        origins.extend(
            (record["downloader"], record["torrent_hash"])
            for _, record in context.legacy_records()
        )
        for name, value in origins:
            processed_ids.update(
                identity_key(target, target_hash)
                for target, target_hash in context.trace(name, value).locations
            )
        start = (page - 1) * page_size
        return TorrentListResponse(
            items=[
                TorrentSummary(
                    hash=item.hash,
                    name=item.name,
                    save_path=item.save_path,
                    total_size=item.total_size,
                    state=item.state,
                    processed=identity_key(downloader, item.hash) in processed_ids,
                )
                for item in items[start : start + page_size]
            ],
            total=len(items),
            page=page,
            page_size=page_size,
            capabilities=adapter.capabilities,
        )

    def get_torrent_detail(
        self,
        downloader: str = Query(min_length=1, max_length=200),
        torrent_hash: str = Query(pattern=r"^(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})$"),
    ) -> TorrentDetail:
        """
        读取实时布局与缓存识别提示，不在详情请求中做修改。

        :param downloader: 下载器名称
        :param torrent_hash: 精确 Hash
        :return: 快照、能力与历史提示
        """
        target = TargetRequest(downloader=downloader, torrent_hash=torrent_hash)
        adapter = self._adapter(target.downloader)
        torrent = self._live(adapter, target.torrent_hash)
        found = self._find_record(target.downloader, target.torrent_hash)
        return TorrentDetail(
            torrent=torrent,
            capabilities=adapter.capabilities,
            processed=found is not None,
            history_id=found[0] if found else None,
            media=self._media_hint(torrent, found[1] if found else None),
            warnings=["已有处理记录，请先恢复后再重新格式化"] if found else [],
        )

    def preview_torrent(self, request: PreviewRequest) -> PreviewResponse:
        """
        仅生成计划和短期内存 token，无修改/备份写入。

        :param request: 媒体、临时规则或恢复模式
        :return: 前后状态与逐项差异
        """
        if request.mode == "recover":
            preview, history_id, db_restore = self._make_recovery_preview(request)
            return self._freeze_preview(preview, history_id, db_restore)
        adapter = self._adapter(request.downloader)
        torrent = self._live(adapter, request.torrent_hash)
        return self._freeze_preview(
            self._make_format_preview(request, adapter, torrent)
        )

    def process_torrent(self, request: ProcessRequest) -> ExecutionResponse:
        """
        执行格式化 token，不接受客户端提供的路径。

        :param request: 已确认 token
        :return: 幂等执行回执
        """
        return self._execute_token(request.preview_token, "format")

    def recover_from_history(self, request: RecoverRequest) -> tuple[bool, str]:
        """
        新客户端使用恢复 token；旧客户端即时规划并保留 tuple 回执。

        :param request: 原记录身份与可选 token
        :return: 完全确认恢复才返回 True
        """
        if isinstance(request, dict):
            request = RecoverRequest.model_validate(request)
        token = request.preview_token
        if token:
            try:
                plan, previous = self._preview_store.lookup(token)
            except PreviewExpired as error:
                return False, str(error)
            if (
                previous
                and plan.preview.mode == "recover"
                and plan.history_id
                == identity_key(request.downloader, request.torrent_hash)
            ):
                return previous.status == "success", previous.message
            found = self._find_record(request.downloader, request.torrent_hash)
            if (
                plan.preview.mode != "recover"
                or found is None
                or plan.history_id != found[0]
            ):
                return False, "恢复预览与目标记录不匹配，请重新预览"
        else:
            preview = self.preview_torrent(
                PreviewRequest(
                    downloader=request.downloader,
                    torrent_hash=request.torrent_hash,
                    mode="recover",
                )
            )
            if preview.status != "ready" or not preview.preview_token:
                return False, preview.message
            token = preview.preview_token
        result = self._execute_token(token, "recover")
        return result.status == "success", result.message

    def get_operation_status(
        self, operation_id: str = Query(pattern=r"^[a-f0-9]{32}$")
    ) -> ExecutionResponse:
        """
        回查已提交操作，不重试 RPC，也不继续未执行项。

        :param operation_id: 操作标识
        :return: 实际结果
        """
        record = deepcopy(self.get_data(key=f"operation:{operation_id}"))
        if not record:
            raise HTTPException(404, "操作记录不存在或已清理")
        if not self._lock.acquire(blocking=False):
            return ExecutionResponse(
                status="verifying",
                message="执行或恢复仍在进行，请稍后回查，不要再次提交",
                operation_id=operation_id,
                mode=record["mode"],
                downloader=record["downloader"],
                torrent_hash=record["torrent_hash"],
                operations=record.get("operations", []),
                can_recover=True,
            )
        try:
            adapter = self._adapter(record["downloader"])
            try:
                actual = adapter.get_torrent(record["torrent_hash"])
            except Exception as error:
                raise HTTPException(
                    502, f"结果回查失败，备份仍保留：{error}"
                ) from error
            key = record.get("history_id") or identity_key(
                record["downloader"], record["torrent_hash"]
            )
            metadata = (self.get_data(key="history_v2") or {}).get(key)
            active = bool(
                metadata and metadata.get("last_operation_id") == operation_id
            )
            observed = reconcile_execution(record, actual)
            if active:
                record["operations"] = [log.model_dump() for log in observed.operations]
                record["running"] = False
                if actual and (
                    not record["operations"]
                    or any(log.status == "confirmed" for log in observed.operations)
                ):
                    # 只同步已观测的布局，不重试/继续任何移动或改名调用。
                    try:
                        self._sync_database(record, actual)
                        record["database_error"] = ""
                    except Exception as error:
                        record["database_error"] = str(error)
                observed = reconcile_execution(record, actual)
                record.update(status=observed.status, message=observed.message)
                self._persist_execution(record)
                if observed.status == "success" and record["mode"] == "recover":
                    self._finish_recovery(record)
                self._preview_store.complete(operation_id, observed)
            return observed
        finally:
            self._lock.release()

    def _make_recovery_preview(
        self, request: PreviewRequest
    ) -> tuple[PreviewResponse, str, dict]:
        """
        读取原备份并追踪转种目标，ID 被重用时拒绝恢复。

        :param request: 原记录身份或已解析目标身份
        :return: 逆向计划、原记录键、原数据库字段
        """
        found = self._find_record(request.downloader, request.torrent_hash)
        if found is None or not found[1].get("before"):
            raise HTTPException(
                404, "备份不存在或已损坏，不能安全恢复；不会自动删除记录"
            )
        history_id, record = found
        original = snapshot_from_dict(record["before"])
        metadata = record.get("_index") or record
        name, torrent_hash = metadata["downloader"], metadata["torrent_hash"]

        def lookup(target_name: str, target_hash: str) -> TorrentInfo | None:
            """
            只按稳定 Hash 查询，数字 ID 重用不能命中别的种子。

            :param target_name: 已知下载器
            :param target_hash: 备份 Hash
            :return: 完整快照或确定不存在
            """
            try:
                return self._adapter(target_name).get_torrent(target_hash)
            except HTTPException:
                raise
            except Exception as error:
                raise HTTPException(
                    502, f"目标查询失败，备份仍保留：{error}"
                ) from error

        target = locate_history_target(name, torrent_hash, self._redirect, lookup)
        if target.snapshot is None:
            code = (
                404
                if target.state == "missing"
                else 503
                if target.state == "unavailable"
                else 409
            )
            raise HTTPException(code, target.message)
        name, torrent_hash, current = (
            target.downloader,
            target.torrent_hash,
            target.snapshot,
        )
        adapter = self._adapter(name)
        media = (
            MediaBrief.model_validate(record["media"]) if record.get("media") else None
        )
        preview = build_recovery_preview(
            downloader=name,
            current=current,
            original=original,
            capabilities=adapter.capabilities,
            rules=self.effective_rules(),
            media=media,
            legacy=bool(record.get("legacy") or metadata.get("legacy")),
        )
        latest_id = metadata.get("last_operation_id")
        latest = self.get_data(key=f"operation:{latest_id}") if latest_id else record
        if latest and any(
            item.get("status") == "unknown" for item in latest.get("operations", ())
        ):
            preview.warnings = [
                *preview.warnings,
                "上一操作有未确认项，请先核对下载器实际状态，再确认此恢复计划；不会自动继续原操作。",
            ]
        return preview, history_id, deepcopy(record.get("db_before") or {})

    def _finish_recovery(self, record: dict):
        """
        二次核验布局及记录字段后清理，旧回查不能清掉新的处理记录。

        :param record: 已完成的恢复操作
        """
        actual = self._live(self._adapter(record["downloader"]), record["torrent_hash"])
        if not matches_target(actual, snapshot_from_dict(record["after"])):
            raise HTTPException(409, "恢复后状态又发生变化，保留备份；请重新预览")
        desired = self._desired_database(record, actual)
        current_db = self._database_snapshot(
            record["downloader"], record["torrent_hash"]
        )
        if any(
            current_db.get(group, {}).get(key) != values
            for group in ("history", "files")
            for key, values in desired.get(group, {}).items()
        ):
            raise HTTPException(409, "下载记录尚未确认恢复，保留备份")
        key = record["history_id"]
        metadata = (self.get_data(key="history_v2") or {}).get(key)
        if metadata and metadata["operation_id"] != record.get("source_operation_id"):
            return
        original = self._record_by_id(key)
        if original and fingerprint(original.get("before")) != record.get(
            "source_backup_fingerprint"
        ):
            raise HTTPException(409, "原备份已被替换，不清理该记录")
        name, torrent_hash = key.rsplit(":", 1)
        self._clear_history(key, unquote(name), torrent_hash)
        self._set_pending(record["downloader"], record["torrent_hash"], None)

    def get_history(
        self,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        query: str = Query(default="", max_length=200),
        result_filter: Literal[
            "all", "success", "partial", "verifying", "failed", "legacy"
        ] = "all",
        presence_filter: Literal[
            "all",
            "restorable",
            "available",
            "transferred",
            "missing",
            "unavailable",
            "conflict",
            "busy",
            "backup_missing",
        ] = "all",
        fresh: bool = Query(default=False),
    ) -> HistoryListResponse:
        """
        搜索/筛选后分页，并按需核验种子位置。

        普通浏览可复用五秒内的下载器摘要，手动刷新通过 ``fresh`` 强制实时查询。
        空搜索不会读取转种链；位置筛选仍核验全部匹配记录后计算准确总数。

        :param page: 页码
        :param page_size: 每页数量
        :param query: 名称、Hash、下载器或保存路径
        :param result_filter: 原处理结果
        :param presence_filter: 当前种子状态
        :param fresh: 是否绕过跨请求下载器摘要缓存
        :return: 分页历史及位置提示
        """
        context = HistoryReadContext(self.get_data)
        items = []
        index = context.history_index()
        for key, data in index.items():
            items.append(
                HistoryItem(
                    id=key,
                    downloader=data["downloader"],
                    torrent_hash=data["torrent_hash"],
                    name=data["name"],
                    save_path=data["save_path"],
                    target_save_path=data.get("target_save_path", ""),
                    files_count=data["files_count"],
                    status=data.get("status", "verifying"),
                    created_at=data.get("created_at", ""),
                    legacy=bool(data.get("legacy")),
                )
            )
        for key, record in context.legacy_records():
            if key not in index:
                before = record.get("before") or {}
                items.append(
                    HistoryItem(
                        id=key,
                        downloader=record["downloader"],
                        torrent_hash=record["torrent_hash"],
                        name=before.get("name") or record["torrent_hash"],
                        save_path=before.get("save_path") or "",
                        files_count=len(before.get("files") or ()),
                        status="legacy",
                        legacy=True,
                        restorable=bool(before),
                    )
                )
        term = query.strip().casefold()
        matching = []
        for item in items:
            if result_filter == "legacy" and not item.legacy:
                continue
            if result_filter not in ("all", "legacy") and item.status != result_filter:
                continue
            if not term:
                matching.append(item)
                continue
            trace = context.trace(item.downloader, item.torrent_hash)
            text = " ".join(
                [
                    item.name,
                    item.torrent_hash,
                    item.downloader,
                    item.save_path,
                    item.target_save_path,
                    *(name for name, _ in trace.locations),
                    *(str(row.get("to_download") or "") for row in trace.records),
                ]
            )
            if term in text.casefold():
                matching.append(item)
                continue
            if item.legacy:
                record = context.record_by_id(item.id)
                enriched = (
                    self._history_item_from_record(item.id, record) if record else item
                )
                backup_text = f"{enriched.name} {enriched.save_path}"
                if term in backup_text.casefold():
                    matching.append(enriched)
        matching.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        summaries: dict[str, HistorySummaryResult] = {}
        start = (page - 1) * page_size
        if presence_filter != "all":
            matching = [
                self._history_presence_item(
                    item, summaries, context=context, fresh=fresh
                )
                for item in matching
            ]
            matching = (
                [item for item in matching if item.restorable]
                if presence_filter == "restorable"
                else [
                    item for item in matching if item.presence.state == presence_filter
                ]
            )
            selected = matching[start : start + page_size]
        else:
            selected = [
                self._history_presence_item(
                    item, summaries, context=context, fresh=fresh
                )
                for item in matching[start : start + page_size]
            ]
        return HistoryListResponse(
            items=selected, total=len(matching), page=page, page_size=page_size
        )

    def _history_summary(
        self,
        downloader: str,
        summaries: dict[str, HistorySummaryResult],
        fresh: bool,
    ) -> HistorySummaryResult:
        """
        在请求内复用一个下载器结果，并按连接身份访问五秒缓存。

        :param downloader: 下载器名称
        :param summaries: 本次请求已经取得的下载器结果
        :param fresh: 是否绕过跨请求缓存
        :return: 无文件摘要、异常和真实核验时间
        """
        if downloader not in summaries:
            identity = self._config_fingerprint(downloader)
            summaries[downloader] = self._history_summary_cache.read(
                downloader=downloader,
                identity=identity,
                loader=lambda: self._adapter(downloader).list_torrents(),
                fresh=fresh,
            )
        return summaries[downloader]

    def _history_presence_from_target(
        self,
        item: HistoryItem,
        record: dict | None,
        target: HistoryTarget,
        trace,
        evidence: list[HistorySummaryResult] | None = None,
    ) -> HistoryItem:
        """
        使用同一记录、转种链及实时目标生成恢复状态和删除签名。

        :param item: 原历史摘要
        :param record: 请求快照中的备份记录
        :param target: 下载器实时定位结果
        :param trace: 与目标定位共用的转种链
        :param evidence: 实际访问过的下载器摘要结果
        :return: 带位置状态和确认签名的历史项
        """
        backed_up = bool(record and record.get("before"))
        valid_hash = (
            not backed_up
            or str(record["before"].get("hash") or "").lower()
            == item.torrent_hash.lower()
        )
        state = (
            "backup_missing"
            if not backed_up
            else "conflict"
            if not valid_hash
            else target.state
        )
        message = target.message
        if not backed_up:
            message = "原备份缺失或归属不明确，不能恢复。" + message
        elif not valid_hash:
            message = "备份与记录 Hash 不一致，不能安全恢复"
        evidence = evidence or []
        checked_at = min(
            (result.checked_at for result in evidence if result.checked_at), default=""
        )
        presence = HistoryPresence(
            state=state,
            message=message,
            target_downloader=target.downloader,
            target_hash=target.torrent_hash,
            locations=[
                HistoryLocation(downloader=name, torrent_hash=value)
                for name, value in trace.locations
            ],
            can_delete=target.state != "busy",
            checked_at=checked_at,
            cached=any(result.cached for result in evidence),
        )
        presence.delete_signature = fingerprint(
            {
                "plugin": self.__class__.__name__,
                "history": self._history_signature(
                    item.id, record=record, trace=trace
                ),
                "state": state,
                "target": (target.downloader, target.torrent_hash),
                "locations": trace.locations,
            }
        )
        return item.model_copy(
            update={
                "presence": presence,
                "restorable": backed_up
                and valid_hash
                and target.state in ("available", "transferred"),
            }
        )

    def _check_history_item(
        self,
        item: HistoryItem,
        summaries: dict[str, HistorySummaryResult],
        context: HistoryReadContext,
        fresh: bool,
    ) -> tuple[HistoryItem, HistoryTarget]:
        """
        以同一请求快照和无文件摘要核验一条历史。

        :param item: 待核验的历史摘要
        :param summaries: 请求级下载器摘要结果
        :param context: 请求级插件数据快照
        :param fresh: 是否绕过跨请求摘要缓存
        :return: 带 presence 的列表项及内部实时目标
        """
        evidence = []
        record = context.record_by_id(item.id)
        if record:
            item = self._history_item_from_record(item.id, record)
        trace = context.trace(item.downloader, item.torrent_hash)

        def lookup(name: str, torrent_hash: str) -> TorrentInfo | None:
            """
            每下载器每请求只取得一次无文件摘要，离线异常不会变成空列表。

            :param name: 下载器名称
            :param torrent_hash: 稳定 Hash
            :return: 快照或确定缺失
            """
            result = self._history_summary(name, summaries, fresh)
            evidence.append(result)
            if result.error:
                raise result.error
            return result.snapshots.get(torrent_hash.lower())

        target = locate_history_target(
            item.downloader,
            item.torrent_hash,
            context.redirect,
            lookup,
            trace=trace,
        )
        return (
            self._history_presence_from_target(
                item=item,
                record=record,
                target=target,
                trace=trace,
                evidence=evidence,
            ),
            target,
        )

    def _history_presence_item(
        self,
        item: HistoryItem,
        summaries: dict[str, HistorySummaryResult],
        context: HistoryReadContext | None = None,
        fresh: bool = False,
    ) -> HistoryItem:
        """
        返回一条历史的实时位置、恢复能力和删除确认摘要。

        :param item: 原历史摘要
        :param summaries: 请求级下载器摘要结果
        :param context: 可选的请求级插件数据快照
        :param fresh: 是否绕过跨请求摘要缓存
        :return: 核验状态及确认签名
        """
        checked, _ = self._check_history_item(
            item,
            summaries,
            context=context or HistoryReadContext(self.get_data),
            fresh=fresh,
        )
        return checked

    @staticmethod
    def _history_item_from_record(history_id: str, record: dict) -> HistoryItem:
        """
        从请求快照中的备份与索引构造一条历史摘要。

        :param history_id: 稳定历史身份
        :param record: 新版操作记录或旧版备份
        :return: 尚未核验下载器位置的历史列表项
        """
        metadata = record.get("_index") or record
        before = record.get("before") or {}
        legacy = bool(record.get("legacy") or metadata.get("legacy"))
        torrent_hash = str(metadata["torrent_hash"]).lower()
        return HistoryItem(
            id=history_id,
            downloader=metadata["downloader"],
            torrent_hash=torrent_hash,
            name=before.get("name") or metadata.get("name") or torrent_hash,
            save_path=before.get("save_path") or metadata.get("save_path") or "",
            target_save_path=metadata.get("target_save_path", ""),
            files_count=int(
                metadata.get("files_count", len(before.get("files") or ()))
            ),
            status=metadata.get("status", "legacy" if legacy else "verifying"),
            created_at=metadata.get("created_at", ""),
            legacy=legacy,
            restorable=bool(before),
        )

    def history_lookup(self, request: HistoryLookupRequest) -> HistoryLookupResponse:
        """
        按稳定身份实时核验跨页选择，不进行任何持久化修改。

        :param request: 最多一百个不同历史身份
        :return: 与请求顺序一致的存在性和实时列表项
        """
        if isinstance(request, dict):
            request = HistoryLookupRequest.model_validate(request)
        context = HistoryReadContext(self.get_data)
        summaries: dict[str, HistorySummaryResult] = {}
        results = []
        for history_id in request.history_ids:
            record = context.record_by_id(history_id)
            if record is None:
                results.append(
                    HistoryLookupResult(
                        history_id=history_id,
                        status="not_found",
                        message="插件备份记录已不存在；这不表示下载器中的种子不存在",
                    )
                )
                continue
            checked = self._history_presence_item(
                self._history_item_from_record(history_id, record),
                summaries,
                context=context,
                fresh=True,
            )
            results.append(
                HistoryLookupResult(
                    history_id=history_id,
                    status="found",
                    message="已实时核验当前插件备份和种子位置",
                    item=checked,
                )
            )
        return HistoryLookupResponse(results=results)

    def _validate_history_deletions(
        self, items: list[DeleteHistoryBatchItem]
    ) -> tuple[
        dict[str, DeleteHistoryResult],
        dict[str, dict],
    ]:
        """
        在事务外实时核验批量删除请求，并保留业务失败项。

        :param items: 已通过边界校验的不同历史身份
        :return: 已确定结果及可进入写事务的候选映射
        """
        context = HistoryReadContext(self.get_data)
        summaries: dict[str, HistorySummaryResult] = {}
        results: dict[str, DeleteHistoryResult] = {}
        candidates = {}
        for request_item in items:
            history_id = request_item.history_id
            record = context.record_by_id(history_id)
            if record is None:
                results[history_id] = DeleteHistoryResult(
                    status="not_found",
                    history_id=history_id,
                    message="插件备份记录已不存在；未据此判断种子是否存在",
                )
                continue
            checked, target = self._check_history_item(
                self._history_item_from_record(history_id, record),
                summaries,
                context=context,
                fresh=True,
            )
            if not checked.presence.can_delete:
                results[history_id] = DeleteHistoryResult(
                    status="busy",
                    history_id=history_id,
                    message="种子正在移动、校验或等待元信息，暂不能删除恢复备份",
                    presence=checked.presence,
                )
                continue
            if checked.presence.delete_signature != request_item.delete_signature:
                results[history_id] = DeleteHistoryResult(
                    status="conflict",
                    history_id=history_id,
                    message="备份、种子位置或转种记录已改变，请重新核验并确认",
                    presence=checked.presence,
                )
                continue
            candidates[history_id] = {
                "request": request_item,
                "record": record,
                "item": checked,
                "target": target,
            }
        return results, candidates

    @staticmethod
    def _stage_history_cleanup(
        oper: PluginDataOper,
        plugin_id: str,
        state: dict[str, object],
        candidates: list[dict],
        related: dict[str, set[str]],
    ) -> tuple[set[str], set[str]]:
        """
        合并清理当前实例的索引、授权备份及关联日志。

        每个索引至多写入一次。旧版共享 Hash 的剩余归属不明确时保留原备份并使
        剩余身份失效，不能把恢复能力错误授予另一个下载器。

        :param oper: 绑定调用方 Session 的局部插件数据 Oper
        :param plugin_id: 当前物理或分身实例标识
        :param state: 写事务内一次扫描得到的当前实例全部数据
        :param candidates: 已在事务内复核通过的删除候选
        :param related: 一次日志扫描得到的历史身份到操作键映射
        :return: 实际删除身份及必须重新核验的保留身份
        """
        deleted_ids = {candidate["request"].history_id for candidate in candidates}
        index = deepcopy(state.get("history_v2") or {})
        pending_v2 = deepcopy(state.get("pending_v2") or {})
        processed = deepcopy(state.get("processed") or {})
        pending = deepcopy(state.get("pending") or {})
        uncertain = set(state.get("legacy_ambiguous") or ())
        delete_keys = set()
        invalidated = set()
        for candidate in candidates:
            history_id = candidate["request"].history_id
            item = candidate["item"]
            if metadata := index.pop(history_id, None):
                delete_keys.add(f"operation:{metadata['operation_id']}")
            delete_keys.update(related.get(history_id, ()))
            pending_v2.pop(history_id, None)
            torrent_hash = item.torrent_hash
            downloader = item.downloader
            storage_hashes = set(candidate["record"].get("storage_hashes") or ())
            storage_hashes.update(
                str(storage_hash)
                for values in (processed, pending)
                for storage_hash in values
                if str(storage_hash).lower() == torrent_hash.lower()
            )
            if not storage_hashes:
                storage_hashes.add(torrent_hash)
            owners = {
                values.get(storage_hash)
                for values in (processed, pending)
                for storage_hash in storage_hashes
            } - {None, ""}
            referenced = downloader in owners
            for values in (processed, pending):
                for storage_hash in storage_hashes:
                    if values.get(storage_hash) == downloader:
                        values.pop(storage_hash)
            remaining = {
                values.get(storage_hash)
                for values in (processed, pending)
                for storage_hash in storage_hashes
            } - {None, ""}
            if referenced and not remaining:
                delete_keys.update(storage_hashes)
                uncertain = {
                    value
                    for value in uncertain
                    if str(value).lower() != torrent_hash.lower()
                }
            elif referenced and (
                len(owners) > 1
                or any(
                    str(value).lower() == torrent_hash.lower()
                    for value in uncertain
                )
            ):
                uncertain.add(torrent_hash)
                for name in remaining:
                    retained_id = identity_key(name, torrent_hash)
                    if retained_id not in index:
                        invalidated.add(retained_id)
        invalidated.difference_update(deleted_ids)
        updates = {
            "history_v2": index,
            "pending_v2": pending_v2,
            "processed": processed,
            "pending": pending,
            "legacy_ambiguous": sorted(uncertain),
        }
        for key, value in updates.items():
            if (key in state or (key == "legacy_ambiguous" and value)) and state.get(
                key
            ) != value:
                oper.save(plugin_id, key, value)
        for key in sorted(delete_keys):
            if key in state:
                oper.del_data(plugin_id, key)
        return deleted_ids, invalidated

    def _commit_history_deletions(self, candidates: dict[str, dict]) -> dict:
        """
        在单一插件数据事务内重查并清理全部仍有效的候选。

        下载器状态已经在事务外实时查询；事务内只重读持久化备份和转种记录，排除
        并发变化的项目后一次提交全部有效清理。

        :param candidates: 事务外实时核验通过的候选映射
        :return: 事务内逐项状态、失效身份及受影响目标
        """
        plugin_id = self.__class__.__name__

        def stage(session):
            """
            使用调用方 Session 重读并合并当前实例的全部修改。

            :param session: 由插件数据事务执行器持有的同步 Session
            :return: 提交后生成回执所需的事务结果
            """
            oper = PluginDataOper(session)
            rows = oper.get_data_all(plugin_id) or ()
            state = {row.key: deepcopy(row.value) for row in rows}
            foreign: dict[tuple[str, str], object] = {}

            def load(key=None, plugin_id=None):
                """
                从本次事务快照读取当前实例，外部转种键也只查询一次。

                :param key: 插件数据键
                :param plugin_id: 外部插件标识
                :return: 事务内持久化值
                """
                target_id = plugin_id or self.__class__.__name__
                if target_id == self.__class__.__name__:
                    return deepcopy(state.get(key)) if key is not None else deepcopy(rows)
                cache_key = (target_id, key)
                if cache_key not in foreign:
                    foreign[cache_key] = deepcopy(oper.get_data(target_id, key))
                return deepcopy(foreign[cache_key])

            context = HistoryReadContext(load)
            statuses = {}
            valid = []
            related: dict[str, set[str]] = {}
            running = set()
            for key, value in state.items():
                if not key.startswith("operation:") or not isinstance(value, dict):
                    continue
                owner = value.get("history_id")
                if not owner and value.get("mode") == "format":
                    owner = identity_key(
                        str(value.get("downloader") or ""),
                        str(value.get("torrent_hash") or ""),
                    )
                if owner:
                    related.setdefault(owner, set()).add(key)
                    if value.get("running"):
                        running.add(owner)
            for history_id, candidate in candidates.items():
                if history_id in running:
                    busy_presence = candidate["item"].presence.model_copy(
                        update={
                            "state": "busy",
                            "message": "写入前发现关联操作仍在执行，请稍后重新核验",
                            "can_delete": False,
                            "delete_signature": "",
                        }
                    )
                    statuses[history_id] = ("busy", busy_presence)
                    continue
                record = context.record_by_id(history_id)
                if record is None:
                    statuses[history_id] = ("not_found", None)
                    continue
                persisted_item = self._history_item_from_record(history_id, record)
                trace = context.trace(
                    persisted_item.downloader, persisted_item.torrent_hash
                )
                target = candidate["target"]
                if trace != target.trace:
                    statuses[history_id] = ("conflict", None)
                    continue
                checked = self._history_presence_from_target(
                    item=persisted_item,
                    record=record,
                    target=target,
                    trace=trace,
                )
                checked = checked.model_copy(
                    update={
                        "presence": checked.presence.model_copy(
                            update={
                                "checked_at": candidate["item"].presence.checked_at,
                                "cached": candidate["item"].presence.cached,
                            }
                        )
                    }
                )
                if (
                    checked.presence.delete_signature
                    != candidate["request"].delete_signature
                ):
                    statuses[history_id] = ("conflict", checked.presence)
                    continue
                persisted = {**candidate, "record": record, "item": checked}
                valid.append(persisted)
                statuses[history_id] = ("deleted", None)
            deleted, invalidated = self._stage_history_cleanup(
                oper, self.__class__.__name__, state, valid, related
            )
            targets = {
                (name, torrent_hash)
                for candidate in valid
                for name, torrent_hash in candidate["target"].trace.locations
            }
            return {
                "statuses": statuses,
                "deleted": deleted,
                "invalidated": invalidated,
                "targets": targets,
            }

        return self.plugindata._execute_sync_write(stage)

    def _delete_history_items_locked(
        self, items: list[DeleteHistoryBatchItem]
    ) -> DeleteHistoryBatchResponse:
        """
        在已持有实例执行锁时完成实时核验和单事务清理。

        :param items: 最多一百个不同历史身份
        :return: 保留请求顺序的部分成功回执
        """
        results, candidates = self._validate_history_deletions(items)
        transaction = {
            "statuses": {},
            "deleted": set(),
            "invalidated": set(),
            "targets": set(),
        }
        if candidates:
            try:
                transaction = self._commit_history_deletions(candidates)
            except Exception as error:
                logger.error(f"批量删除插件历史事务失败，已整体回滚：{error}")
                for history_id in candidates:
                    results[history_id] = DeleteHistoryResult(
                        status="failed",
                        history_id=history_id,
                        message="插件存储写入失败，本批有效记录已整体回滚",
                    )
            else:
                for history_id, (status, presence) in transaction["statuses"].items():
                    if status == "deleted":
                        results[history_id] = DeleteHistoryResult(
                            status="deleted",
                            history_id=history_id,
                            message="插件备份及关联日志已删除；未删除种子或文件",
                        )
                    elif status == "not_found":
                        results[history_id] = DeleteHistoryResult(
                            status="not_found",
                            history_id=history_id,
                            message="写入前确认插件备份已不存在；未据此判断种子是否存在",
                        )
                    elif status == "busy":
                        results[history_id] = DeleteHistoryResult(
                            status="busy",
                            history_id=history_id,
                            message="写入前发现关联操作仍在执行，本条备份已保留",
                            presence=presence,
                        )
                    else:
                        results[history_id] = DeleteHistoryResult(
                            status="conflict",
                            history_id=history_id,
                            message="写入前备份或转种记录又发生变化，请重新核验",
                            presence=presence,
                        )
                self._history_summary_cache.invalidate(
                    {name for name, _ in transaction["targets"]}
                )
        ordered = [results[item.history_id] for item in items]
        removed = [
            result.history_id
            for result in ordered
            if result.status in ("deleted", "not_found")
        ]
        for history_id in set(removed) | set(transaction["invalidated"]):
            self._preview_store.invalidate_history(history_id)
        failures = [
            result for result in ordered if result.status not in ("deleted", "not_found")
        ]
        status = "success" if not failures else "partial" if removed else "failed"
        message = (
            f"已移除 {len(removed)} 条插件历史，{len(failures)} 条保留并返回原因"
            if failures
            else f"已确认移除 {len(removed)} 条插件历史；未删除种子或文件"
        )
        return DeleteHistoryBatchResponse(
            status=status,
            message=message,
            results=ordered,
            removed_history_ids=removed,
            invalidated_history_ids=sorted(transaction["invalidated"]),
            affected_targets=[
                HistoryLocation(downloader=name, torrent_hash=torrent_hash)
                for name, torrent_hash in sorted(transaction["targets"])
            ],
        )

    def delete_history_batch(
        self, request: DeleteHistoryBatchRequest
    ) -> DeleteHistoryBatchResponse:
        """
        一次锁定并批量删除当前实例中仍通过实时复核的插件备份。

        :param request: 最多一百条不同身份、最新签名及明确确认
        :return: 部分成功与逐项中文原因
        """
        if isinstance(request, dict):
            request = DeleteHistoryBatchRequest.model_validate(request)
        if not self._lock.acquire(blocking=False):
            raise HTTPException(409, "插件正在执行操作，整批备份均未删除")
        try:
            return self._delete_history_items_locked(request.items)
        finally:
            self._lock.release()

    def delete_history(self, request: DeleteHistoryRequest) -> DeleteHistoryResponse:
        """
        兼容原单条接口，并复用批量删除的实时复核与事务流程。

        :param request: 身份、最新签名及明确确认
        :return: 插件记录删除回执
        """
        if isinstance(request, dict):
            request = DeleteHistoryRequest.model_validate(request)
        if not self._lock.acquire(blocking=False):
            raise HTTPException(409, "插件正在执行操作，不能删除备份；请稍后刷新")
        try:
            response = self._delete_history_items_locked(
                [
                    DeleteHistoryBatchItem(
                        history_id=request.history_id,
                        delete_signature=request.delete_signature,
                    )
                ]
            )
            result = response.results[0]
            if result.status == "not_found":
                raise HTTPException(404, "该历史记录已不存在，请刷新")
            if result.status in ("conflict", "busy"):
                raise HTTPException(409, result.message)
            if result.status == "failed":
                raise HTTPException(500, result.message)
            return DeleteHistoryResponse(history_id=result.history_id, message=result.message)
        finally:
            self._lock.release()

    def get_processed_data(self) -> dict[str, str]:
        """
        旧接口只能表示单一下载器，歧义 Hash 不擅自选择。

        :return: 唯一 Hash 到下载器映射
        """
        owners: dict[str, set[str]] = {}
        for torrent_hash, downloader in (self.get_data(key="processed") or {}).items():
            owners.setdefault(torrent_hash, set()).add(downloader)
        for record in (self.get_data(key="history_v2") or {}).values():
            owners.setdefault(record["torrent_hash"], set()).add(record["downloader"])
        return {
            torrent_hash: next(iter(names))
            for torrent_hash, names in owners.items()
            if len(names) == 1
        }

    def get_torrent_data(
        self, torrent_hash: str, downloader: str | None = None
    ) -> TorrentData | None:
        """
        保留旧摘要形态，下载器可选；歧义查询返回 None。

        :param torrent_hash: Hash
        :param downloader: 可选精确下载器
        :return: 原始备份摘要
        """
        downloader = downloader or self.get_processed_data().get(torrent_hash.lower())
        found = (
            self._find_record(downloader, torrent_hash.lower()) if downloader else None
        )
        data = found[1].get("before") if found else None
        return (
            TorrentData(
                name=data.get("name") or "",
                files_count=len(data.get("files") or ()),
                save_path=data.get("save_path") or "",
            )
            if data
            else None
        )

    @eventmanager.register(EventType.DownloadAdded)
    def event_process_main(self, event: Event):
        """
        已有下载事件使用同一只读计划/执行器，不另做一套改名逻辑。

        :param event: 下载添加事件
        """
        if not self.config.event_enabled or not event:
            return
        data = event.event_data or {}
        context = data.get("context")
        self.main(
            downloader=data.get("downloader"),
            torrent_hash=data.get("hash"),
            meta=context.meta_info if context else None,
            media_info=context.media_info if context else None,
        )

    def cron_process_main(self):
        """
        按配置扫描摘要，只为未处理种子读取文件；已修改的失败先恢复。
        """
        for name in self.config.downloader:
            try:
                adapter = resolve_downloader(name)
                for summary in adapter.list_torrents():
                    if not self._has_history(name, summary.hash):
                        self._auto_process(name, adapter, summary.hash)
            except Exception as error:
                logger.warning(f"下载器 {name} 自动扫描失败：{error}")

    def main(
        self,
        downloader: str | None = None,
        downloadhis: DownloadHistory | None = None,
        torrent_hash: str | None = None,
        torrent_info: TorrentInfo | None = None,
        meta: MetaBase | None = None,
        media_info: MediaInfo | None = None,
    ) -> bool:
        """
        保留单种子自动入口签名，局部适配器不污染其他请求。

        :param downloader: 下载器名称
        :param downloadhis: 已有媒体下载历史
        :param torrent_hash: Hash
        :param torrent_info: 已查询快照
        :param meta: 事件元数据
        :param media_info: 事件媒体
        :return: 真正完成或无变更；排除/失败不冒充成功
        """
        name = downloader or (downloadhis.downloader if downloadhis else None)
        if not name:
            service = DownloaderHelper().get_service(name=None)
            name = service.name if service else None
        if not name:
            logger.warning("未指定可用下载器，跳过格式化")
            return False
        try:
            adapter = resolve_downloader(name)
            result = self._auto_process(
                name,
                adapter,
                torrent_hash or (torrent_info.hash if torrent_info else ""),
                torrent_info,
                downloadhis,
                meta,
                media_info,
            )
            return result.status in {"success", "no_changes"}
        except Exception as error:
            logger.warning(f"种子格式化失败：{error}")
            return False

    def _auto_process(
        self,
        name: str,
        adapter: Downloader,
        torrent_hash: str,
        torrent: TorrentInfo | None = None,
        downloadhis: DownloadHistory | None = None,
        meta: MetaBase | None = None,
        media_info: MediaInfo | None = None,
    ) -> PreviewResponse | ExecutionResponse:
        """
        自动流程使用同一个锁、计划与备份策略。

        :param name: 下载器
        :param adapter: 局部连接
        :param torrent_hash: Hash
        :param torrent: 可选已查询详情
        :param downloadhis: 媒体历史
        :param meta: 可选元数据
        :param media_info: 可选媒体
        :return: 跳过、无变更或实际执行回执
        """
        with self._lock:
            torrent = torrent or self._live(adapter, torrent_hash)
            request = PreviewRequest(downloader=name, torrent_hash=torrent.hash)
            preview = self._make_format_preview(
                request, adapter, torrent, downloadhis, meta, media_info
            )
            if preview.status == "ready":
                frozen = self._freeze_preview(preview)
                return self._execute_token(frozen.preview_token, "format")
            logger.info(f"{torrent.name}：{preview.message}")
            excluded = (
                self._has_history(name, torrent.hash)
                or exclusion_reason(
                    torrent, self.config.exclude_dirs, self.config.exclude_tags
                )
                or torrent.moving
                or not torrent.metadata_complete
            )
            self._set_pending(
                name,
                torrent.hash,
                preview.message
                if preview.status == "blocked" and not excluded
                else None,
            )
            return preview

    def _set_pending(self, downloader: str, torrent_hash: str, message: str | None):
        """
        未发生修改的失败允许下次识别重试，复合身份不覆盖别的下载器。

        :param downloader: 下载器
        :param torrent_hash: Hash
        :param message: 失败原因；None 删除待处理状态
        """
        values = deepcopy(self.get_data(key="pending_v2") or {})
        key = identity_key(downloader, torrent_hash)
        if message:
            values[key] = {
                "downloader": downloader,
                "torrent_hash": torrent_hash,
                "message": message,
            }
        else:
            values.pop(key, None)
        self.save_data(key="pending_v2", value=values)

    def on_remove_torrents(
        self, hashs: str | list, delete_file: bool = True, downloader: str | None = None
    ):
        """
        删除文件时仅清理确定目标；保留文件/转种时保留恢复备份。

        :param hashs: 被删除的 Hash
        :param delete_file: 是否删除物理数据
        :param downloader: 可选明确下载器；缺失且歧义时不清理
        """
        if not delete_file:
            return
        with self._lock:
            for torrent_hash in [hashs] if isinstance(hashs, str) else hashs:
                torrent_hash = torrent_hash.lower()
                name = downloader or self.get_processed_data().get(torrent_hash)
                if not name:
                    logger.warning(f"种子 {torrent_hash} 删除来源不明确，保留备份")
                    continue
                self._clear_history(
                    identity_key(name, torrent_hash), name, torrent_hash
                )
                models = self._database_models(name, torrent_hash)
                for row in models["files"].values():
                    if row.state == 0:
                        self.downloadhis.delete_downloadfile(row.id)
                for row in models["history"].values():
                    self.downloadhis.delete_history(row.id)
                self._set_pending(name, torrent_hash, None)

    def _legacy_records(
        self, context: HistoryReadContext | None = None
    ) -> tuple[tuple[str, dict], ...]:
        """
        只读兼容旧 processed/pending Hash 备份，不迁移或删除。

        :param context: 可选的请求级插件数据快照
        :return: 复合身份与旧记录迭代器
        """
        return (context or HistoryReadContext(self.get_data)).legacy_records(
            include_backups=True
        )

    def _redirect(self, downloader: str, torrent_hash: str) -> dict | None:
        """
        复用转种插件的目标记录，暂不连接目标下载器。

        :param downloader: 原下载器
        :param torrent_hash: 原 Hash
        :return: 目标记录
        """
        result = self.get_data(
            key=f"{downloader}-{torrent_hash}", plugin_id="TorrentTransfer"
        )
        return result if isinstance(result, dict) else None

    def _record_by_id(
        self,
        history_id: str | None,
        context: HistoryReadContext | None = None,
    ) -> dict | None:
        """
        按原记录身份读取备份，不按歧义 Hash 取另一下载器记录。

        :param history_id: 复合身份
        :param context: 可选的请求级插件数据快照
        :return: 原记录或 None
        """
        return (context or HistoryReadContext(self.get_data)).record_by_id(history_id)

    def _find_record(
        self, downloader: str, torrent_hash: str
    ) -> tuple[str, dict] | None:
        """
        查找精确身份或转种后的同种子目标；不猜测同 Hash 的其他下载器。

        :param downloader: 实际或原下载器
        :param torrent_hash: Hash
        :return: 原记录身份与备份
        """
        context = HistoryReadContext(self.get_data)
        key = identity_key(downloader, torrent_hash)
        if record := context.record_by_id(key):
            return key, record
        candidates = [
            (key, data["downloader"], data["torrent_hash"])
            for key, data in context.history_index().items()
        ]
        candidates.extend(
            (key, data["downloader"], data["torrent_hash"])
            for key, data in context.legacy_records()
        )
        for original_id, name, original_hash in candidates:
            trace = context.trace(name, original_hash)
            if (downloader, torrent_hash.lower()) in trace.locations:
                return original_id, context.record_by_id(original_id)
        return None

    def _has_history(self, downloader: str, torrent_hash: str) -> bool:
        """
        已有完整或部分修改的种子必须先恢复。

        :param downloader: 下载器
        :param torrent_hash: Hash
        :return: 是否存在处理记录
        """
        return self._find_record(downloader, torrent_hash) is not None

    def _history_signature(
        self,
        history_id: str | None,
        context: HistoryReadContext | None = None,
        record: dict | None = None,
        trace=None,
    ) -> str:
        """
        恢复预览同时校验备份本身与最新操作，防止使用被替换的记录。

        :param history_id: 原记录身份
        :param context: 可选的请求级插件数据快照
        :param record: 可选的同一请求记录副本
        :param trace: 可选的同一请求转种链
        :return: 备份状态摘要
        """
        context = context or HistoryReadContext(self.get_data)
        if record is None:
            record = context.record_by_id(history_id)
        if not record:
            return fingerprint(None)
        metadata = record.get("_index") or record
        trace = trace or context.trace(
            metadata["downloader"], metadata["torrent_hash"]
        )
        return fingerprint(
            {
                "before": record.get("before"),
                "index": record.get("_index"),
                "transfers": trace.records,
                "transfer_error": trace.error,
            }
        )

    def _clear_history(self, key: str, downloader: str, torrent_hash: str):
        """
        仅移除已确定的历史身份，不连带清理别的下载器同 Hash。

        :param key: 原记录复合身份
        :param downloader: 原下载器
        :param torrent_hash: 原 Hash
        """
        index = deepcopy(self.get_data(key="history_v2") or {})
        metadata = index.pop(key, None)
        if metadata:
            self.save_data(key="history_v2", value=index)
            self.del_data(key=f"operation:{metadata['operation_id']}")
        legacy = {
            name: deepcopy(self.get_data(key=name) or {})
            for name in ("processed", "pending")
        }
        owners = {values.get(torrent_hash) for values in legacy.values()} - {None, ""}
        referenced = downloader in owners
        uncertain = set(self.get_data(key="legacy_ambiguous") or ())
        for name, values in legacy.items():
            if values.get(torrent_hash) == downloader:
                values.pop(torrent_hash)
                self.save_data(key=name, value=values)
        remaining = {values.get(torrent_hash) for values in legacy.values()} - {
            None,
            "",
        }
        if referenced and not remaining:
            self.del_data(key=torrent_hash)
            if torrent_hash in uncertain:
                uncertain.discard(torrent_hash)
                self.save_data(key="legacy_ambiguous", value=sorted(uncertain))
        elif referenced and (len(owners) > 1 or torrent_hash in uncertain):
            # 共用旧 Hash 备份不能因删除一个归属而被另一个归属误认为可恢复。
            uncertain.add(torrent_hash)
            self.save_data(key="legacy_ambiguous", value=sorted(uncertain))

    def _config_fingerprint(self, downloader: str) -> str:
        """
        校验已保存规则/排除及连接，摘要不暴露凭据。

        :param downloader: 下载器
        :return: 稳定配置摘要
        """
        service = DownloaderHelper().get_service(name=downloader)
        connection = getattr(service, "config", None)
        if hasattr(connection, "model_dump"):
            connection = connection.model_dump(mode="json")
        return fingerprint(
            {
                "rules": self.effective_rules().model_dump(),
                "exclude_dirs": self.config.exclude_dirs,
                "exclude_tags": self.config.exclude_tags,
                "words": self.exclude_words(),
                "scrap_follow_tmdb": settings.SCRAP_FOLLOW_TMDB,
                "connection": connection,
            }
        )

    def _database_models(
        self, downloader: str, torrent_hash: str
    ) -> dict[str, dict[str, object]]:
        """
        所有同 Hash 历史按下载器精确筛选，不误改辅种源。

        :param downloader: 下载器
        :param torrent_hash: Hash
        :return: 稳定 ID 与模型
        """
        histories, page = [], 1
        while True:
            items, total = self.downloadhis.query(
                DownloadHistoryFilter(download_hash=torrent_hash),
                QueryPageRequest(page=page, count=200),
            )
            histories.extend(item for item in items if item.downloader == downloader)
            if page * 200 >= total or not items:
                break
            page += 1
        return {
            "history": {str(item.id): item for item in histories},
            "files": {
                str(item.id): item
                for item in self.downloadhis.get_files_by_hash(torrent_hash)
                if item.downloader == downloader
            },
        }

    def _database_snapshot(self, downloader: str, torrent_hash: str) -> dict:
        """
        读取目标记录原始字段，预览无写入。

        :param downloader: 下载器
        :param torrent_hash: Hash
        :return: 路径快照
        """
        fields = {"history": ("path",), "files": ("fullpath", "savepath", "filepath")}
        return {
            group: {
                key: {field: getattr(item, field) for field in fields[group]}
                for key, item in rows.items()
            }
            for group, rows in self._database_models(downloader, torrent_hash).items()
        }

    @staticmethod
    def _desired_database(record: dict, actual: TorrentInfo) -> dict:
        """
        从原始快照投影实际布局，恢复完成时使用已备份字段。

        :param record: 执行日志
        :param actual: 实际布局
        :return: 目标路径字段
        """
        restored = (
            record["mode"] == "recover"
            and record.get("db_restore")
            and matches_target(actual, snapshot_from_dict(record["after"]))
        )
        desired = (
            deepcopy(record["db_before"])
            if restored
            else project_database_paths(
                record["db_before"], snapshot_from_dict(record["before"]), actual
            )
        )
        if restored:
            for group in ("history", "files"):
                for key in desired.get(group, {}):
                    if original := record["db_restore"].get(group, {}).get(key):
                        desired[group][key] = deepcopy(original)
        return desired

    def _sync_database(self, record: dict, actual: TorrentInfo):
        """
        写入已确认布局，字段级预期值保护人工并发修改。

        :param record: 原始及上次已写入值
        :param actual: 实际快照
        """
        desired = self._desired_database(record, actual)
        patches = []
        for group in ("history", "files"):
            for key, values in desired.get(group, {}).items():
                expected = record["db_current"][group][key]
                changes = {
                    field: value
                    for field, value in values.items()
                    if value != expected.get(field)
                }
                if changes:
                    patches.append((group, key, changes, deepcopy(expected)))
        if patches:
            self._write_database_cas(
                record["downloader"], record["torrent_hash"], patches
            )
            for group, key, changes, _ in patches:
                record["db_current"][group][key].update(changes)

    def _write_database_cas(self, downloader: str, torrent_hash: str, patches: list):
        """
        在同一事务中用 SQL 条件更新，读取后并发修改也不能被覆盖。

        :param downloader: 目标下载器
        :param torrent_hash: 目标 Hash
        :param patches: 字段修改及允许的原值/已投影值
        """

        def stage(session):
            """
            多条路径记录一起提交，任一冲突整体回滚。

            :param session: Oper 提供的事务会话
            """
            for group, key, values, expected in patches:
                model = DownloadHistory if group == "history" else DownloadFiles
                conditions = [
                    model.id == int(key),
                    model.downloader == downloader,
                    model.download_hash == torrent_hash,
                ]
                conditions.extend(
                    or_(
                        getattr(model, field) == expected.get(field),
                        getattr(model, field) == value,
                    )
                    for field, value in values.items()
                )
                result = session.execute(
                    update(model)
                    .where(*conditions)
                    .values(**values)
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise ValueError(
                        f"下载记录 {group}:{key} 已删除、改动或不属于当前下载器，停止写入"
                    )

        self.downloadhis._execute_sync_write(stage)

    def _persist_execution(self, record: dict):
        """
        原始日志持久化先于复合索引，索引失败不发起 RPC。

        :param record: 原始快照及操作日志
        """
        index = deepcopy(self.get_data(key="history_v2") or {})
        key = record.get("history_id") or identity_key(
            record["downloader"], record["torrent_hash"]
        )
        if record["mode"] == "recover":
            original = self._record_by_id(key)
            if "source_operation_id" not in record:
                if not original or not original.get("before"):
                    raise ValueError("原始恢复备份已丢失，停止执行")
                record["source_backup_fingerprint"] = fingerprint(original["before"])
                if key not in index:
                    # 只在确认执行时迁移旧备份；保持原始布局而非本次恢复前布局。
                    source_id = uuid4().hex
                    source = {
                        **deepcopy(record),
                        "operation_id": source_id,
                        "mode": "format",
                        "legacy": True,
                        "downloader": original["downloader"],
                        "torrent_hash": original["torrent_hash"],
                        "before": deepcopy(original["before"]),
                        "after": deepcopy(record["before"]),
                        "db_before": deepcopy(original.get("db_before") or {}),
                        "operations": [],
                    }
                    self.save_data(key=f"operation:{source_id}", value=source)
                    index[key] = {
                        "operation_id": source_id,
                        "downloader": original["downloader"],
                        "torrent_hash": original["torrent_hash"],
                        "name": original["before"]["name"],
                        "save_path": original["before"]["save_path"],
                        "target_save_path": record["before"]["save_path"],
                        "files_count": len(original["before"].get("files") or ()),
                        "created_at": record["created_at"],
                        "legacy": True,
                    }
                record["source_operation_id"] = index[key]["operation_id"]
        self.save_data(
            key=f"operation:{record['operation_id']}", value=deepcopy(record)
        )
        if record["mode"] == "format":
            index[key] = {
                "operation_id": record["operation_id"],
                "downloader": record["downloader"],
                "torrent_hash": record["torrent_hash"],
                "name": record["before"]["name"],
                "save_path": record["before"]["save_path"],
                "target_save_path": record["after"]["save_path"],
                "files_count": len(record["before"]["files"]),
                "created_at": record["created_at"],
            }
        if key in index:
            index[key].update(
                status=record["status"], last_operation_id=record["operation_id"]
            )
            self.save_data(key="history_v2", value=index)

    def _freeze_preview(
        self,
        preview: PreviewResponse,
        history_id: str | None = None,
        db_restore: dict | None = None,
    ) -> PreviewResponse:
        """
        冻结渲染与校验要素，仅就绪预览进入内存。

        :param preview: 只读预览
        :param history_id: 恢复记录身份
        :param db_restore: 原始数据库字段
        :return: 带 token 的副本
        """
        if preview.status != "ready":
            return preview
        plan = FrozenPlan(
            preview=preview,
            fingerprint=snapshot_fingerprint(preview.before),
            config_fingerprint=self._config_fingerprint(preview.downloader),
            history_id=history_id,
            history_fingerprint=self._history_signature(history_id)
            if history_id
            else "",
            db_before=self._database_snapshot(preview.downloader, preview.torrent_hash),
            db_restore=db_restore or {},
        )
        return self._preview_store.put(plan).preview

    def _execute_token(self, token: str, mode: str) -> ExecutionResponse:
        """
        锁内重查目标与配置，过期/状态变化必须重新预览。

        :param token: 不透明 token
        :param mode: format/recover
        :return: 幂等回执
        """
        if not self._lock.acquire(blocking=False):
            raise HTTPException(409, "插件正在执行其他操作，请稍后再试")
        try:
            plan, previous = self._preview_store.lookup(token)
            if plan.preview.mode != mode:
                raise HTTPException(409, "预览类型不匹配，请重新预览")
            if previous is not None:
                return previous
            preview = plan.preview
            adapter = self._adapter(preview.downloader)
            actual = self._live(adapter, preview.torrent_hash)
            if actual.moving or snapshot_fingerprint(actual) != plan.fingerprint:
                raise HTTPException(409, "种子状态已改变或正在移动，请重新预览")
            if self._config_fingerprint(preview.downloader) != plan.config_fingerprint:
                raise HTTPException(409, "已保存规则、排除项或连接已改变，请重新预览")
            if fingerprint(
                self._database_snapshot(preview.downloader, preview.torrent_hash)
            ) != fingerprint(plan.db_before):
                raise HTTPException(409, "下载记录已改变，请重新预览")
            if mode == "format" and self._has_history(
                preview.downloader, preview.torrent_hash
            ):
                raise HTTPException(409, "种子已有处理记录，请先恢复")
            if mode == "recover" and (
                not self._record_by_id(plan.history_id)
                or self._history_signature(plan.history_id) != plan.history_fingerprint
            ):
                raise HTTPException(409, "恢复记录已改变，请重新预览")
            self._preview_store.claim(token)
            self._history_summary_cache.invalidate({preview.downloader})
            try:
                result, record = execute_plan(
                    adapter=adapter,
                    plan=plan,
                    operation_id=token,
                    persist=self._persist_execution,
                    sync_database=self._sync_database,
                )
            finally:
                # 执行期间开始的摘要可能观察到中间状态，完成后再次失效。
                self._history_summary_cache.invalidate({preview.downloader})
            if result.status == "success":
                if mode == "recover":
                    self._finish_recovery(record)
                else:
                    self._set_pending(preview.downloader, preview.torrent_hash, None)
            self._preview_store.complete(token, result)
            return result
        except PreviewExpired as error:
            raise HTTPException(409, str(error)) from error
        finally:
            self._lock.release()

    def effective_rules(self) -> FormatterRules:
        """
        构造请求级有效规则，不修改全局属性。

        :return: 不可变规则
        """
        return self.config.naming_rules()

    def exclude_words(self) -> tuple[str, ...]:
        """
        复用系统屏蔽词，兼容旧的多行文本。

        :return: 正则元组
        """
        values = self.systemconfig.get(SystemConfigKey.TransferExcludeWords) or ()
        return tuple(
            str(value)
            for value in (values.splitlines() if isinstance(values, str) else values)
            if value
        )

    def assist_sources(self) -> dict[str, str]:
        """
        复用 IYUU 记录追溯媒体，循环关联不会无限追溯。

        :return: 辅种到源 Hash 的映射
        """
        parents: dict[str, str] = {}
        for record in self.get_data(plugin_id="IYUUAutoSeed") or ():
            source = str(record.key).lower()
            for value in record.value or ():
                for seed in value.get("torrents", ()):
                    parents[str(seed).lower()] = source
        result = {}
        for seed in parents:
            source, seen = parents[seed], {seed}
            while source in parents and source not in seen:
                seen.add(source)
                source = parents[source]
            result[seed] = source
        return result

    def _media_hint(
        self, torrent: TorrentInfo, record: dict | None
    ) -> MediaBrief | None:
        """
        从缓存历史提供识别提示，不进行 TMDB 网络搜索。

        :param torrent: 当前种子
        :param record: 可选处理历史
        :return: 媒体提示
        """
        if record and record.get("media"):
            return MediaBrief.model_validate(record["media"])
        history = self.downloadhis.get_by_hash(torrent.hash)
        if history and history.type in (MediaType.MOVIE.value, MediaType.TV.value):
            return MediaBrief(
                media_source=str(history.media_source or MediaSource.TMDB),
                media_id=str(history.media_id or ""),
                media_type="movie" if history.type == MediaType.MOVIE.value else "tv",
                title=history.title or "",
                year=str(history.year or ""),
                poster=getattr(history, "poster", None) or "",
                source="history",
            )
        return None

    def _resolve_media(
        self,
        torrent: TorrentInfo,
        request: PreviewRequest,
        downloadhis: DownloadHistory | None = None,
        meta: MetaBase | None = None,
        media_info: MediaInfo | None = None,
    ) -> tuple[MetaBase, MediaInfo, MediaBrief]:
        """
        手动身份优先，其次历史/辅种，最后种子名识别；不修改宿主缓存。

        :param torrent: 当前种子
        :param request: 本次输入
        :param downloadhis: 可选已有历史
        :param meta: 事件元数据
        :param media_info: 事件媒体
        :return: 元数据、媒体副本与可展示来源
        """
        source = "recognized"
        if request.media:
            meta = MetaInfo(torrent.name)
            mtype = (
                MediaType.MOVIE if request.media.media_type == "movie" else MediaType.TV
            )
            meta.type = mtype
            media_info = self.chain.recognize_media(
                meta=meta,
                mtype=mtype,
                media_source=MediaSource.TMDB,
                media_id=request.media.media_id,
            )
            source = "manual"
        elif not media_info:
            if downloadhis is None:
                downloadhis = self.downloadhis.get_by_hash(torrent.hash)
                if downloadhis is None:
                    origin = self.assist_sources().get(torrent.hash)
                    if origin:
                        downloadhis = self.downloadhis.get_by_hash(origin)
                        source = "assisted"
            if downloadhis:
                source = "assisted" if source == "assisted" else "history"
                meta = MetaInfo(
                    title=downloadhis.torrent_name or torrent.name,
                    subtitle=downloadhis.torrent_description or "",
                )
                mtype = MediaType(downloadhis.type)
                media_source, media_id = (
                    getattr(downloadhis, "media_source", None),
                    getattr(downloadhis, "media_id", None),
                )
                if not media_id:
                    media_id = getattr(downloadhis, "tmdbid", None) or getattr(
                        downloadhis, "doubanid", None
                    )
                    media_source = (
                        MediaSource.TMDB
                        if getattr(downloadhis, "tmdbid", None)
                        else MediaSource.Douban
                    )
                kwargs = (
                    {
                        "media_source": MediaSource(media_source),
                        "media_id": str(media_id),
                    }
                    if media_source and media_id
                    else {}
                )
                media_info = self.chain.recognize_media(
                    meta=meta, mtype=mtype, **kwargs
                )
            else:
                meta = meta or MetaInfo(torrent.name)
                media_info = self.chain.recognize_media(meta=meta)
        if not media_info or media_info.type not in (MediaType.MOVIE, MediaType.TV):
            raise ValueError("无法识别电影/剧集媒体，请手动指定 TMDB ID 或搜索媒体")
        meta, media_info = (
            deepcopy(meta or MetaInfo(torrent.name)),
            deepcopy(media_info),
        )
        if not settings.SCRAP_FOLLOW_TMDB:
            media_source = getattr(media_info, "media_source", None) or MediaSource.TMDB
            media_id = getattr(media_info, "media_id", None) or getattr(
                media_info, "tmdb_id", None
            )
            if media_id:
                transfer = self.transferhis.get_by_media_identity(
                    media_source=MediaSource(media_source),
                    media_id=str(media_id),
                    mtype=media_info.type.value,
                )
                if transfer:
                    media_info.title = transfer.title
        brief = MediaBrief(
            media_source=str(
                getattr(media_info, "media_source", None) or MediaSource.TMDB
            ),
            media_id=str(
                getattr(media_info, "media_id", None)
                or getattr(media_info, "tmdb_id", None)
                or ""
            ),
            media_type="movie" if media_info.type == MediaType.MOVIE else "tv",
            title=media_info.title or "",
            year=str(media_info.year or ""),
            poster=getattr(media_info, "poster_path", None) or "",
            source=source,
        )
        return meta, media_info, brief

    def _make_format_preview(
        self,
        request: PreviewRequest,
        adapter: Downloader,
        torrent: TorrentInfo,
        downloadhis: DownloadHistory | None = None,
        meta: MetaBase | None = None,
        media_info: MediaInfo | None = None,
    ) -> PreviewResponse:
        """
        整体排除不调用媒体/模板扩展；文件不可识别则保留原布局。

        :param request: 本次规则
        :param adapter: 局部连接
        :param torrent: 快照
        :param downloadhis: 自动历史
        :param meta: 事件元数据
        :param media_info: 事件媒体
        :return: 无 token 的计划
        """
        rules = request.overrides.apply(self.effective_rules())
        reason = (
            "种子已有处理记录，请先恢复再按新规则处理"
            if self._has_history(request.downloader, torrent.hash)
            else ""
        )
        reason = reason or exclusion_reason(
            torrent, self.config.exclude_dirs, self.config.exclude_tags
        )
        reason = reason or ("种子正在移动或校验，请等待完成" if torrent.moving else "")
        reason = reason or (
            "磁力种子元信息尚未就绪" if not torrent.metadata_complete else ""
        )
        brief = None
        if not reason:
            try:
                meta, media_info, brief = self._resolve_media(
                    torrent, request, downloadhis, meta, media_info
                )
            except Exception as error:
                reason = str(error)

        def render_torrent(template: str) -> str:
            """
            调用宿主命名扩展，仅在生成计划时渲染。

            :param template: 模板
            :return: 已渲染路径
            """
            return str(self.format_path(template, meta, media_info))

        def render_file(file: TorrentFile, template: str) -> str:
            """
            使用逐文件季集，不按文件顺序猜测。

            :param file: 原文件
            :param template: 模板
            :return: 合法目标候选
            """
            file_path = Path(file.name)
            file_meta = MetaInfoPath(file_path)
            file_meta.type = media_info.type
            if media_info.type == MediaType.TV:
                if file_meta.begin_episode is None:
                    raise SkipFile("未识别到文件集数，保留原路径；不会按文件顺序猜测")
                if file_meta.begin_season is None:
                    season = (
                        request.season
                        if request.season is not None
                        else (meta.begin_season if meta.end_season is None else None)
                    )
                    if season is None:
                        raise SkipFile("未识别到文件季号，请补充季号后重新预览")
                    file_meta.begin_season, file_meta.total_season = season, 1
            return str(
                self.format_path(template, file_meta, media_info, file_path.suffix)
            )

        return build_format_preview(
            downloader=request.downloader,
            torrent=torrent,
            rules=rules,
            capabilities=adapter.capabilities,
            render_torrent=render_torrent,
            render_file=render_file,
            media=brief,
            exclude_words=self.exclude_words(),
            blocked_reason=reason,
        )

    @staticmethod
    def format_path(
        template_string: str,
        meta: MetaBase,
        mediainfo: MediaInfo,
        file_ext: str | None = None,
    ) -> Path:
        """
        复用宿主命名变量与 Jinja2/扩展事件，而非另写模板引擎。

        :param template_string: 模板
        :param meta: 源元数据
        :param mediainfo: 已解析媒体
        :param file_ext: 文件扩展名
        :return: 宿主渲染的路径
        """
        handler = TransHandler()
        return handler.get_rename_path(
            template_string=template_string,
            rename_dict=handler.get_naming_dict(
                meta=meta, mediainfo=mediainfo, file_ext=file_ext
            ),
        )
