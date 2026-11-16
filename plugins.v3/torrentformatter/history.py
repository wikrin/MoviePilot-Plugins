"""
历史目标定位：追踪已知转种，按 Hash 查找，不能把离线当成不存在。
"""

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .models import TorrentInfo, identity_key


def _utc_now() -> datetime:
    """
    返回带时区的当前 UTC 时间，供摘要核验时间记录使用。

    :return: 当前 UTC 时间
    """
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class HistorySummaryResult:
    """
    一次下载器摘要核验的不可变结果。

    :param snapshots: 按小写 Hash 索引的无文件摘要
    :param error: 下载器查询异常；成功时为 None
    :param checked_at: 真正完成下载器查询的 UTC 时间
    :param cached: 本次读取是否复用了跨请求缓存
    """

    snapshots: dict[str, TorrentInfo]
    error: Exception | None
    checked_at: str
    cached: bool = False


@dataclass(frozen=True)
class _SummaryEntry:
    """
    摘要缓存中的单个下载器结果。

    :param result: 原始实时查询结果
    :param expires_at: 单调时钟到期值
    """

    result: HistorySummaryResult
    expires_at: float


@dataclass
class _SummaryFlight:
    """
    同一下载器身份的一次在途查询及其一次性交付结果。

    :param event: 查询完成、取消或失效时唤醒等待者
    :param epoch: 创建时的全局清理代次
    :param version: 创建时的下载器失效代次
    :param result: 完成后供已等待请求共享的结果
    :param completed: 查询是否已经真正完成
    :param cancelled: 是否因清理或失效而不再可交付
    """

    event: threading.Event
    epoch: int
    version: int
    result: HistorySummaryResult | None = None
    completed: bool = False
    cancelled: bool = False


class HistorySummaryCache:
    """
    实例级下载器摘要缓存，并合并相同身份的并发冷查询。

    缓存只保存无文件摘要和明确查询异常。清理或失效会切断旧查询的写回资格，
    因而重载、格式化及恢复后的旧在途结果不能重新进入缓存。
    """

    def __init__(
        self,
        ttl: float = 5.0,
        capacity: int = 32,
        summary_capacity: int = 10000,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _utc_now,
    ):
        """
        创建有界摘要缓存。

        :param ttl: 缓存有效秒数
        :param capacity: 最多保存的下载器身份数量
        :param summary_capacity: 全部缓存中最多保存的种子摘要数量
        :param clock: 判断过期的单调时钟
        :param wall_clock: 生成真实核验时间的 UTC 时钟
        """
        self.ttl = ttl
        self.capacity = capacity
        self.summary_capacity = summary_capacity
        self.clock = clock
        self.wall_clock = wall_clock
        self._items: OrderedDict[tuple[str, str], _SummaryEntry] = OrderedDict()
        self._inflight: dict[tuple[str, str], _SummaryFlight] = {}
        self._summary_count = 0
        self._epoch = 0
        self._versions: dict[str, int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _copy_result(result: HistorySummaryResult, cached: bool) -> HistorySummaryResult:
        """
        复制结果映射并设置本次缓存命中标记。

        :param result: 缓存中的原始结果
        :param cached: 调用方是否复用了该结果
        :return: 与缓存映射隔离的结果
        """
        return HistorySummaryResult(
            snapshots=dict(result.snapshots),
            error=result.error,
            checked_at=result.checked_at,
            cached=cached,
        )

    def _remove_locked(self, key: tuple[str, str]) -> None:
        """
        在持锁状态移除一个缓存项并维护摘要计数。

        :param key: 下载器名称与连接指纹
        """
        entry = self._items.pop(key, None)
        if entry:
            self._summary_count -= len(entry.result.snapshots)

    def _purge_locked(self, now: float) -> None:
        """
        在持锁状态清除过期结果。

        :param now: 当前单调时钟值
        """
        for key, entry in list(self._items.items()):
            if entry.expires_at <= now:
                self._remove_locked(key)

    def clear(self) -> None:
        """
        清除全部缓存，并使旧在途查询失去写回资格。
        """
        with self._lock:
            self._epoch += 1
            self._items.clear()
            self._summary_count = 0
            inflight = list(self._inflight.values())
            self._inflight.clear()
            for flight in inflight:
                flight.cancelled = True
        for flight in inflight:
            flight.event.set()

    def invalidate(self, downloaders: set[str]) -> None:
        """
        失效指定下载器的摘要及在途查询。

        :param downloaders: 状态可能已经改变的下载器名称
        """
        if not downloaders:
            return
        with self._lock:
            for downloader in downloaders:
                self._versions[downloader] = self._versions.get(downloader, 0) + 1
            for key in [key for key in self._items if key[0] in downloaders]:
                self._remove_locked(key)
            inflight = []
            for key, flight in list(self._inflight.items()):
                if key[0] in downloaders:
                    flight.cancelled = True
                    inflight.append(flight)
                    self._inflight.pop(key, None)
        for flight in inflight:
            flight.event.set()

    def read(
        self,
        downloader: str,
        identity: str,
        loader: Callable[[], list[TorrentInfo]],
        fresh: bool = False,
    ) -> HistorySummaryResult:
        """
        读取或实时查询一个下载器的无文件摘要。

        相同身份的并发冷查询只执行一次。超过容量的结果仍返回给当前请求，但不缓存。

        :param downloader: 下载器名称
        :param identity: 不含明文凭据的连接配置指纹
        :param loader: 发起一次摘要查询的回调
        :param fresh: 是否绕过已经存在的跨请求缓存
        :return: 摘要映射、异常和真实核验时间
        """
        key = (downloader, identity)
        while True:
            now = self.clock()
            with self._lock:
                self._purge_locked(now)
                entry = self._items.get(key)
                if fresh:
                    for old_key in [
                        old_key
                        for old_key in self._items
                        if old_key[0] == downloader
                    ]:
                        self._remove_locked(old_key)
                    entry = None
                if not fresh and entry and entry.expires_at > now:
                    self._items.move_to_end(key)
                    return self._copy_result(entry.result, cached=True)
                inflight = self._inflight.get(key)
                if inflight is None:
                    flight = _SummaryFlight(
                        event=threading.Event(),
                        epoch=self._epoch,
                        version=self._versions.get(downloader, 0),
                    )
                    self._inflight[key] = flight
                    owner = True
                else:
                    flight = inflight
                    owner = False
            if owner:
                break
            flight.event.wait()
            with self._lock:
                current_version = self._versions.get(downloader, 0)
                if (
                    flight.completed
                    and not flight.cancelled
                    and flight.epoch == self._epoch
                    and flight.version == current_version
                    and flight.result is not None
                ):
                    return self._copy_result(flight.result, cached=True)
            fresh = False

        try:
            error = None
            snapshots: dict[str, TorrentInfo] = {}
            try:
                snapshots = {
                    snapshot.hash.lower(): snapshot for snapshot in loader()
                }
            except Exception as caught:
                error = caught
            checked_at = self.wall_clock().astimezone(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
            result = HistorySummaryResult(
                snapshots=snapshots,
                error=error,
                checked_at=checked_at,
            )
            with self._lock:
                completed_at = self.clock()
                self._purge_locked(completed_at)
                current = self._inflight.get(key)
                current_version = self._versions.get(downloader, 0)
                may_store = (
                    current is flight
                    and not flight.cancelled
                    and flight.epoch == self._epoch
                    and flight.version == current_version
                    and len(snapshots) <= self.summary_capacity
                )
                if may_store:
                    for old_key in [
                        old_key
                        for old_key in self._items
                        if old_key[0] == downloader and old_key != key
                    ]:
                        self._remove_locked(old_key)
                    existing = self._items.get(key)
                    existing_count = len(existing.result.snapshots) if existing else 0
                    fits_entries = existing is not None or len(self._items) < self.capacity
                    fits_summaries = (
                        self._summary_count - existing_count + len(snapshots)
                        <= self.summary_capacity
                    )
                    if fits_entries and fits_summaries:
                        self._remove_locked(key)
                        self._items[key] = _SummaryEntry(
                            result=self._copy_result(result, cached=False),
                            expires_at=completed_at + self.ttl,
                        )
                        self._summary_count += len(snapshots)
                if current is flight:
                    self._inflight.pop(key, None)
                if not flight.cancelled:
                    flight.result = self._copy_result(result, cached=False)
                    flight.completed = True
            return result
        finally:
            flight.event.set()


class HistoryReadContext:
    """
    一次历史请求的插件数据快照与转种链缓存。

    :param loader: 与 ``_PluginBase.get_data`` 同形态的只读入口
    """

    def __init__(self, loader: Callable[..., Any]):
        """
        创建空的请求快照。

        :param loader: 支持 key 与 plugin_id 关键字参数的数据读取回调
        """
        self._loader = loader
        self._values: dict[tuple[str | None, str | None], Any] = {}
        self._errors: dict[tuple[str | None, str | None], Exception] = {}
        self._legacy_index: dict[str, dict[str, Any]] | None = None
        self._traces: dict[tuple[str, str], "TransferTrace"] = {}

    def read(self, key: str | None = None, plugin_id: str | None = None) -> Any:
        """
        每个插件与数据键只读取并复制一次，后续返回同一请求内的冻结事实。

        :param key: 插件数据键；None 表示读取该插件全部记录
        :param plugin_id: 外部插件标识；None 表示当前实例
        :return: 首次读取时与持久化对象隔离的请求快照
        """
        cache_key = (plugin_id, key)
        if cache_key in self._errors:
            raise self._errors[cache_key]
        if cache_key not in self._values:
            kwargs = {"key": key}
            if plugin_id is not None:
                kwargs["plugin_id"] = plugin_id
            try:
                self._values[cache_key] = deepcopy(self._loader(**kwargs))
            except Exception as error:
                self._errors[cache_key] = error
                raise
        return self._values[cache_key]

    def history_index(self) -> dict[str, dict[str, Any]]:
        """
        返回新版历史索引快照。

        :return: 按历史身份索引的元数据
        """
        return self.read(key="history_v2") or {}

    def _build_legacy_index(self) -> dict[str, dict[str, Any]]:
        """
        只读取旧归属表并按规范化 Hash 建立轻量索引。

        raw 备份延迟到列表项真正被选中、位置筛选或搜索备份字段时才读取。
        pending-only 记录仍需确认 raw 备份存在，保持旧版兼容语义。

        :return: 按复合历史身份索引的旧归属元数据
        """
        if self._legacy_index is not None:
            return self._legacy_index
        processed = self.read(key="processed") or {}
        pending = self.read(key="pending") or {}
        uncertain_hashes = {
            str(torrent_hash).lower()
            for torrent_hash in (self.read(key="legacy_ambiguous") or ())
        }
        storage_groups: dict[str, list[str]] = {}
        for storage_hash in set(processed) | set(pending):
            storage_hash = str(storage_hash)
            storage_groups.setdefault(storage_hash.lower(), []).append(storage_hash)
        records = {}
        for torrent_hash, storage_hashes in sorted(storage_groups.items()):
            storage_hashes = sorted(storage_hashes)
            names = set()
            for storage_hash in storage_hashes:
                for values in (processed, pending):
                    if name := values.get(storage_hash):
                        names.add(name)
            has_processed = any(
                storage_hash in processed for storage_hash in storage_hashes
            )
            uncertain = (
                len(names) != 1
                or len(storage_hashes) != 1
                or torrent_hash in uncertain_hashes
            )
            if (
                not has_processed
                and len(names) == 1
                and not uncertain
                and not self.read(key=storage_hashes[0])
            ):
                continue
            for name in sorted(names):
                records[identity_key(name, torrent_hash)] = {
                    "legacy": True,
                    "downloader": name,
                    "torrent_hash": torrent_hash,
                    "storage_hashes": tuple(storage_hashes),
                    "mode": "format",
                    "db_before": {},
                    "ambiguous": uncertain,
                }
        self._legacy_index = records
        return records

    def _legacy_with_backup(self, record: dict[str, Any]) -> dict[str, Any]:
        """
        为一个轻量旧归属按原始存储键补充 raw 备份。

        :param record: 旧归属元数据
        :return: 带 before 的请求内记录副本
        """
        result = dict(record)
        storage_hashes = record.get("storage_hashes") or ()
        result["before"] = (
            self.read(key=storage_hashes[0])
            if len(storage_hashes) == 1 and not record.get("ambiguous")
            else None
        )
        return result

    def legacy_records(
        self, include_backups: bool = False
    ) -> tuple[tuple[str, dict[str, Any]], ...]:
        """
        返回旧版归属；默认不读取每个 raw Hash 备份。

        :param include_backups: 是否为所有记录补充 raw 备份
        :return: 复合历史身份与旧记录元组
        """
        records = self._build_legacy_index()
        if include_backups:
            return tuple(
                (key, self._legacy_with_backup(record))
                for key, record in records.items()
            )
        return tuple(records.items())

    def record_by_id(self, history_id: str | None) -> dict[str, Any] | None:
        """
        按稳定历史身份读取新版操作备份或旧版共享备份。

        :param history_id: 历史复合身份
        :return: 带索引元数据的记录；不存在时返回 None
        """
        if not history_id:
            return None
        if metadata := self.history_index().get(history_id):
            record = self.read(key=f"operation:{metadata['operation_id']}") or {
                "before": None
            }
            record["_index"] = metadata
            return record
        record = self._build_legacy_index().get(history_id)
        return self._legacy_with_backup(record) if record else None

    def redirect(self, downloader: str, torrent_hash: str) -> dict[str, Any] | None:
        """
        读取一次转种插件的明确目标记录。

        :param downloader: 原下载器名称
        :param torrent_hash: 原稳定 Hash
        :return: 转种记录；无有效字典时返回 None
        """
        result = self.read(
            key=f"{downloader}-{torrent_hash}", plugin_id="TorrentTransfer"
        )
        return result if isinstance(result, dict) else None

    def trace(self, downloader: str, torrent_hash: str) -> "TransferTrace":
        """
        每个原始身份只解析一次转种链。

        :param downloader: 原下载器名称
        :param torrent_hash: 原稳定 Hash
        :return: 请求内冻结的转种链
        """
        key = (downloader, torrent_hash.lower())
        if key not in self._traces:
            self._traces[key] = trace_transfers(
                downloader, torrent_hash, self.redirect
            )
        return self._traces[key]


@dataclass(frozen=True)
class TransferTrace:
    """
    只读转种链及其原始记录，用于状态核验与确认签名。

    :param locations: 原位置到最终已知目标的顺序
    :param records: 对应原始转种记录，不直接返回客户端
    :param error: 循环/非法目标等错误
    """

    locations: tuple[tuple[str, str], ...]
    records: tuple[dict[str, Any], ...] = ()
    error: str = ""


@dataclass(frozen=True)
class HistoryTarget:
    """
    已核验的位置或明确的未知/缺失结果。

    :param state: available/transferred/missing/unavailable/conflict/busy
    :param message: 用户提示
    :param downloader: 实际或待核验的目标下载器
    :param torrent_hash: 稳定 Hash
    :param snapshot: 实时摘要/详情，未知或缺失时为空
    :param trace: 已知转种链
    """

    state: str
    message: str
    downloader: str
    torrent_hash: str
    snapshot: TorrentInfo | None
    trace: TransferTrace


def trace_transfers(
    downloader: str, torrent_hash: str, redirect: Callable[[str, str], dict | None]
) -> TransferTrace:
    """
    解析已知转种链；数字 ID 仅作为记录，不作为不稳定的实际身份。

    :param downloader: 原下载器
    :param torrent_hash: 原稳定 Hash
    :param redirect: 只读转种记录入口
    :return: 链与异常，不连接下载器
    """
    torrent_hash = torrent_hash.lower()
    locations = [(downloader, torrent_hash)]
    records = []
    if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", torrent_hash):
        return TransferTrace(
            tuple(locations), error="备份 Hash 格式无效，不能安全定位种子"
        )
    for depth in range(9):
        name, current_hash = locations[-1]
        record = redirect(name, current_hash)
        if not record:
            return TransferTrace(tuple(locations), tuple(records))
        records.append(record)
        target = record.get("to_download")
        reference = record.get("to_download_id")
        if not isinstance(target, str) or not target.strip() or reference is None:
            return TransferTrace(
                tuple(locations),
                tuple(records),
                "转种记录缺少有效目标，无法核验种子位置",
            )
        value = str(reference).lower()
        if re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", value):
            if value != torrent_hash:
                return TransferTrace(
                    tuple(locations),
                    tuple(records),
                    "转种目标 Hash 与备份不一致，拒绝使用其他种子",
                )
        elif not value.isdigit():
            return TransferTrace(
                tuple(locations), tuple(records), "转种记录中的目标 ID 无效，无法核验"
            )
        location = (target, torrent_hash)
        if location in locations or depth == 8:
            return TransferTrace(
                tuple(locations), tuple(records), "转种记录存在循环或链过长，请核对目标"
            )
        locations.append(location)
    return TransferTrace(tuple(locations), tuple(records), "转种链无法解析")


def locate_history_target(
    downloader: str,
    torrent_hash: str,
    redirect: Callable[[str, str], dict | None],
    lookup: Callable[[str, str], TorrentInfo | None],
    trace: TransferTrace | None = None,
) -> HistoryTarget:
    """
    优先最终转种目标；确认不存在才回退之前位置，离线时不猜测。

    :param downloader: 原下载器
    :param torrent_hash: 稳定 Hash
    :param redirect: 已知转种记录
    :param lookup: 按 Hash 读取摘要或完整详情，连接失败应抛异常
    :param trace: 可选的请求内已解析转种链
    :return: 已核验的目标
    """
    trace = trace or trace_transfers(downloader, torrent_hash, redirect)
    name, current_hash = trace.locations[-1]
    if trace.error:
        return HistoryTarget("conflict", trace.error, name, current_hash, None, trace)
    for offset, (name, current_hash) in enumerate(reversed(trace.locations)):
        try:
            snapshot = lookup(name, current_hash)
        except Exception as error:
            reason = getattr(error, "detail", None) or str(error)
            return HistoryTarget(
                "unavailable",
                f"下载器 {name} 不存在、离线或查询失败，无法确认种子是否已删除：{reason}",
                name,
                current_hash,
                None,
                trace,
            )
        if snapshot is None:
            continue
        if snapshot.hash.lower() != torrent_hash.lower():
            return HistoryTarget(
                "conflict",
                "查找到的种子 Hash 与备份不一致，拒绝使用其他种子",
                name,
                current_hash,
                None,
                trace,
            )
        if snapshot.moving or not snapshot.metadata_complete:
            return HistoryTarget(
                "busy",
                f"种子已定位到 {name}，但正在移动/校验或元信息未就绪",
                name,
                current_hash,
                snapshot,
                trace,
            )
        transferred = name != downloader
        message = (
            f"已转种，种子当前位于下载器 {name}"
            if transferred
            else f"种子仍存在于下载器 {name}"
        )
        if offset:
            message = (
                f"后续转种目标已不存在，已按 Hash 定位到下载器 {name}；恢复将使用此位置"
            )
        return HistoryTarget(
            "transferred" if transferred else "available",
            message,
            name,
            current_hash,
            snapshot,
            trace,
        )
    name, current_hash = trace.locations[-1]
    message = (
        "原下载器及所有已知转种目标中均未找到此种子"
        if len(trace.locations) > 1
        else f"下载器 {downloader} 中已不存在此种子"
    )
    return HistoryTarget(
        "missing",
        message + "；插件备份仍保留，可以删除记录",
        name,
        current_hash,
        None,
        trace,
    )
