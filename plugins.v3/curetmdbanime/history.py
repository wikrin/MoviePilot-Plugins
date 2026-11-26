"""
按媒体汇总修正资源，并用有容量上限的内存缓冲区暂存记录，由一个保存流程写入插件数据。

查询不会强制保存尚未保存的记录。保存结果不确定时，通过批次编号核对结果，避免重复累计；
同一历史记录不支持多个进程同时写入。
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from copy import deepcopy
from uuid import uuid4

from app.sdk.logging import logger


class HistoryStore:
    """
    按资源分类累计修正次数，不保存每次处理的单独明细。

    历史数据保存在插件数据中，内存只暂存尚未保存的更改，不常驻加载完整历史。
    缓冲区有容量上限，由一个保存流程写入插件数据；保存结果确认前，更改仍占用缓冲区容量。

    :cvar buffer_max_bytes: 包含正在保存但尚未确认的数据在内的估算容量上限，单位为字节
    :cvar buffer_max_objects: 包含正在保存但尚未确认的资源在内的对象数量上限
    :cvar idle_seconds: 媒体最后更新后至少空闲多久才保存记录，单位为秒
    :cvar max_wait_seconds: 尚未保存的媒体记录从首次接收起最多等待多久，单位为秒
    :cvar flush_interval_seconds: 定期保存任务检查尚未保存记录的间隔，单位为秒
    """

    retention_days = 21
    storage_prefix = "correction_history:"
    buffer_max_bytes = 4 * 1024 * 1024
    buffer_max_objects = 1024
    idle_seconds = 3
    max_wait_seconds = 30
    flush_interval_seconds = 1
    result_names = ("changed", "unchanged", "skipped", "failed")

    def __init__(
        self,
        plugin,
        max_records: int = 10000,
        *,
        clock: Callable[[], float] | None = None,
        scheduler_available: bool = True,
        request_flush: Callable[[], None] | None = None,
    ) -> None:
        """
        创建历史记录对象和有容量上限的内存缓冲区，不访问数据库。

        :param plugin: 提供键值持久化接口的插件
        :param max_records: 插件中最多保留的资源记录数，不限制累计处理次数
        :param clock: 可注入的秒级时钟，默认系统时钟
        :param scheduler_available: 定期保存任务是否已注册并可用
        :param request_flush: 缓冲区接近上限时安排保存的通知，不在缓冲锁内调用
        """
        self.plugin = plugin
        self.max_records = max_records
        self.clock = clock or time.time
        self.scheduler_available = scheduler_available
        self.request_flush = request_flush
        self.lock = threading.RLock()
        self._buffer = threading.Condition(threading.RLock())
        self._pending = {}
        self._inflight = {}
        self._objects = 0
        self._bytes = 0
        # 纳秒级起始序号可在重载后区分同一时间的处理先后，不必为每次处理生成 UUID。
        self._order = time.time_ns()
        self._closed = False
        self._pressure = False
        self._last_error = ""
        self._rejected = 0
        self._retry_at = 0
        self._failures = 0
        self._warn_at = float("-inf")
        self._count = None

    @classmethod
    def key_for(cls, media_key: str) -> str:
        """
        返回媒体专用历史键，不触及插件其他数据。

        :param media_key: 媒体类型和 TMDB 身份或未识别身份哈希
        :return: 带历史前缀的持久键
        """
        return cls.storage_prefix + media_key

    @staticmethod
    def _estimate(value):
        """
        按容器和文本估算记录占用，并为保存、编码和查询过程中的副本预留空间。
        """
        size = sys.getsizeof(value)
        if isinstance(value, dict):
            size += sum(HistoryStore._estimate(key) + HistoryStore._estimate(item) for key, item in value.items())
        elif isinstance(value, (list, tuple)):
            size += sum(HistoryStore._estimate(item) for item in value)
        return size

    @staticmethod
    def _latest(resource, result=""):
        results = resource["results"]
        return (
            results[result]
            if result
            else max(
                results.values(),
                key=lambda row: (row["latest_at"], row["latest_order"]),
            )
        )

    @staticmethod
    def matches(record: dict, query: str = "", result: str = "", media_key: str = "") -> bool:
        """
        统一按完整字面子串及曾出现分类匹配资源摘要。

        :param record: 含原始资源字段及分类计数的摘要
        :param query: 大小写不敏感的字面关键词，不拆词
        :param result: 空字符串或指定结果分类
        :param media_key: 可选的精确媒体身份
        :return: 任一原始文本命中且满足身份和分类条件
        """
        term = query.lower()
        return (
            (not media_key or record.get("media_key") == media_key)
            and (not result or record.get("results", {}).get(result, {}).get("count", 0) > 0)
            and (
                not term
                or any(
                    term in str(record.get(field, "")).lower()
                    for field in ("title", "resource_title", "resource_subtitle")
                )
            )
        )

    def set_scheduler_available(self, available: bool) -> None:
        """
        更新定期保存任务是否可用，不创建后台线程或额外保存流程。

        :param available: 定期保存任务已注册并可用时为 True
        """
        with self._buffer:
            self.scheduler_available = bool(available)

    def set_request_flush(self, callback: Callable[[], None] | None) -> None:
        """
        设置缓冲区接近上限时的保存任务通知，可在创建后绑定历史记录对象编号。

        :param callback: 无参数的保存任务通知；None 表示没有通知渠道
        """
        with self._buffer:
            self.request_flush = callback

    def status(self) -> dict:
        """
        返回尚未保存记录的状态；正在保存但尚未确认的数据仍计入容量。

        :return: 尚未保存的资源数、估算字节数、最近错误、拒收次数和定期保存任务状态
        """
        with self._buffer:
            return {
                "pending_objects": self._objects,
                "pending_bytes": self._bytes,
                "last_error": self._last_error,
                "rejected": self._rejected,
                "scheduler_available": self.scheduler_available,
            }

    def _reject(self, reason=""):
        """
        在故障降级或不可容纳单对象时统计拒收并限频告警。
        """
        self._rejected += 1
        if reason:
            self._last_error = reason
        now = self.clock()
        if now >= self._warn_at + 60:
            self._warn_at = now
            logger.warning(f"未能接收修正历史记录，记录尚未保存：{self._last_error}")
        return False

    def _notify(self):
        """
        请求尽快调度；注册失败不破坏修正调用。
        """
        if self.request_flush:
            try:
                self.request_flush()
            except Exception as error:
                logger.warning(f"未能安排历史记录保存任务：{error}")

    def record(self, **fields) -> bool:
        """
        接收资源的完整原始标题信息，并累计一个结果分类。

        正常接收记录不会访问数据库。达到缓冲区上限时，在缓冲锁外最多尝试保存一次后重试；
        保存失败后不反复阻塞识别。

        :param fields: 媒体字段、原始资源二字段、可靠标记及结果摘要
        :return: 已接收返回 True；对象已停止接收记录或故障导致容量不足时返回 False
        """
        result = fields["result"]
        if result not in self.result_names:
            raise ValueError("未知历史结果分类")
        title = str(fields.get("resource_title", fields.get("title", "")) or "")
        subtitle = str(fields.get("resource_subtitle", "") or "")
        identity = json.dumps([title, subtitle], ensure_ascii=False, separators=(",", ":"))
        resource_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        resource = {
            "resource_title": title,
            "resource_subtitle": subtitle,
            "search_text": (title + "\n" + subtitle).lower(),
            "results": {},
        }
        media_key = fields["media_key"]
        while True:
            with self._buffer:
                if self._closed:
                    return False
                now = self.clock()
                media = self._pending.get(media_key)
                previous = media["resources"].get(resource_id) if media else None
                candidate = deepcopy(previous or resource)
                self._order += 1
                candidate["results"][result] = {
                    "count": candidate["results"].get(result, {}).get("count", 0) + 1,
                    "latest_at": now,
                    "latest_order": self._order,
                    **{
                        name: fields.get(name, "")
                        for name in (
                            "original_range",
                            "final_range",
                            "reason",
                            "source",
                            "strategy",
                        )
                    },
                }
                # 三倍估算覆盖冻结副本及 JSON 编码，媒体文本按每资源重复计入以保守覆盖。
                metadata = {
                    "media_key": media_key,
                    "tmdb_id": fields.get("tmdb_id", ""),
                    "title": fields.get("title", ""),
                    "search_title": fields.get("title", "").lower(),
                }
                size = 3 * (self._estimate(candidate) + self._estimate(metadata)) + 512
                old_size = media["sizes"].get(resource_id, 0) if media else 0
                growth = size - old_size
                object_growth = int(previous is None)
                if size > self.buffer_max_bytes:
                    return self._reject("单个资源超过历史缓冲字节预算")
                if (
                    self._bytes + growth <= self.buffer_max_bytes
                    and self._objects + object_growth <= self.buffer_max_objects
                ):
                    if media is None:
                        media = {
                            "media_key": media_key,
                            "tmdb_id": fields.get("tmdb_id", ""),
                            "title": fields.get("title", ""),
                            "resources": {},
                            "sizes": {},
                            "first_at": now,
                            "touched_at": now,
                        }
                        self._pending[media_key] = media
                    media.update(title=fields.get("title", ""), touched_at=now)
                    media["resources"][resource_id] = candidate
                    media["sizes"][resource_id] = size
                    self._bytes += growth
                    self._objects += object_growth
                    notify = (
                        self._bytes >= self.buffer_max_bytes * 0.8 or self._objects >= self.buffer_max_objects * 0.8
                    ) and not self._pressure
                    self._pressure |= notify
                    break
                self._pressure = True
                if self._failures:
                    return self._reject()
            self._notify()
            # 达到容量上限时，在缓冲锁外最多尝试一次保存；之前保存失败后不重复尝试 I/O。
            if not self._capacity_flush():
                with self._buffer:
                    if self._closed:
                        return False
                    return self._reject()
        if notify:
            self._notify()
        return True

    def _capacity_flush(self):
        """
        开始保存前再次检查失败状态，避免等待中的调用连续重试无法保存的数据。
        """
        with self.lock:
            with self._buffer:
                if self._closed or self._failures:
                    return False
            return self._flush_locked(force=True)

    def _validate(self, value):
        """
        检查插件中保存的历史格式；不兼容或损坏的数据会报错，不自动转换或删除。
        """
        if value is None:
            return None
        if not isinstance(value, dict) or not isinstance(value.get("resources"), dict):
            raise ValueError("历史数据格式与当前版本不兼容，请检查并单独清理旧数据")
        for resource in value["resources"].values():
            if (
                not isinstance(resource, dict)
                or not isinstance(resource.get("results"), dict)
                or not resource["results"]
                or any(
                    name not in self.result_names
                    or not isinstance(summary, dict)
                    or not isinstance(summary.get("count"), int)
                    or summary["count"] <= 0
                    or not isinstance(summary.get("latest_at"), (int, float))
                    or not isinstance(summary.get("latest_order"), int)
                    for name, summary in resource["results"].items()
                )
            ):
                raise ValueError("历史资源或分类数据与当前版本不兼容，请检查插件数据")
        return value

    def _read(self, media_key):
        return self._validate(self.plugin.get_data(key=self.key_for(media_key)))

    @staticmethod
    def _merge(base, delta):
        """
        合并同一媒体的新记录，按分类累计次数，并按时间和接收顺序保留最近处理信息。
        """
        value = (
            deepcopy(base)
            if base
            else {
                "media_key": delta["media_key"],
                "tmdb_id": delta.get("tmdb_id", ""),
                "title": delta.get("title", ""),
                "resources": {},
            }
        )
        if delta.get("title"):
            value["title"] = delta["title"]
        value["search_title"] = value.get("title", "").lower()
        for resource_id, resource in delta["resources"].items():
            target = value["resources"].setdefault(resource_id, {**resource, "results": {}})
            for name, summary in resource["results"].items():
                previous = target["results"].get(name)
                if previous:
                    latest = max(
                        (previous, summary),
                        key=lambda row: (row["latest_at"], row["latest_order"]),
                    )
                    target["results"][name] = {
                        **latest,
                        "count": previous["count"] + summary["count"],
                    }
                else:
                    target["results"][name] = deepcopy(summary)
        return value

    def _freeze(self, media_key):
        """
        将尚未保存的记录移入保存批次；批次编号用于核对保存结果，确认前仍计入缓冲区容量。
        """
        delta = self._pending.pop(media_key, None)
        if delta:
            batch = {
                "id": uuid4().hex,
                "delta": delta,
                "kind": "write",
                "bytes": sum(delta["sizes"].values()),
                "objects": len(delta["resources"]),
            }
            self._inflight[media_key] = batch
            return batch
        return None

    @staticmethod
    def _applied(base, batch):
        return (base or {}).get("last_batch_id") == batch["id"] or (
            batch["kind"] == "cleanup" and batch["empty"] and base is None
        )

    def _view(self, base, batch, pending):
        """
        核对尚未确认的保存结果，再合并正在保存和尚未保存的记录；查询不强制保存。
        """
        if batch and not self._applied(base, batch):
            base = self._merge(base, batch["delta"])
        if pending:
            base = self._merge(base, pending)
        return base

    def _ack(self, media_key, batch):
        with self._buffer:
            if self._inflight.get(media_key) is batch:
                del self._inflight[media_key]
                self._bytes -= batch["bytes"]
                self._objects -= batch["objects"]
                self._buffer.notify_all()

    def _write_batch(self, media_key, batch):
        """
        同一批次只累计一次；保存失败或结果不确定后，可在后续读取时核对保存结果。
        """
        base = self._read(media_key)
        if not self._applied(base, batch):
            if batch["kind"] == "cleanup" and not batch["delta"]["resources"] and not batch["stats"]["deleted"]:
                self._ack(media_key, batch)
                return
            value = self._merge(base, batch["delta"])
            if batch["kind"] == "cleanup":
                value, _ = batch["transform"](value)
            value["last_batch_id"] = batch["id"]
            if value["resources"]:
                self.plugin.save_data(key=self.key_for(media_key), value=value)
            else:
                self.plugin.del_data(key=self.key_for(media_key))
            if self._count is not None:
                self._count += len(value["resources"]) - len((base or {}).get("resources", {}))
        self._ack(media_key, batch)

    def _error(self, error):
        with self._buffer:
            self._last_error = str(error)
            self._failures += 1
            self._retry_at = self.clock() + min(60, 2 ** min(self._failures - 1, 6))
            self._count = None
            self._buffer.notify_all()

    def _flush_locked(self, force=False):
        """
        逐个保存并确认各媒体的记录；某个媒体失败不撤销其他已保存的记录，失败数据保留以便重试。
        """
        with self._buffer:
            now = self.clock()
            if not force and now < self._retry_at:
                return False
            keys = set(self._inflight)
            keys.update(
                key
                for key, media in self._pending.items()
                if force
                or self._pressure
                or now - media["touched_at"] >= self.idle_seconds
                or now - media["first_at"] >= self.max_wait_seconds
            )
        success = True
        for media_key in sorted(keys):
            with self._buffer:
                media = self._pending.get(media_key)
                followup = (
                    media_key in self._inflight
                    and media is not None
                    and (
                        force
                        or self._pressure
                        or now - media["touched_at"] >= self.idle_seconds
                        or now - media["first_at"] >= self.max_wait_seconds
                    )
                )
                batch = self._inflight.get(media_key) or self._freeze(media_key)
            if batch:
                try:
                    self._write_batch(media_key, batch)
                    if followup:
                        with self._buffer:
                            following = self._freeze(media_key)
                        if following:
                            self._write_batch(media_key, following)
                except Exception as error:
                    self._error(error)
                    success = False
        if success:
            with self._buffer:
                self._last_error = ""
                self._failures = 0
                self._retry_at = 0
                self._pressure = False
            # 只在首次统计或资源数量可能超限时扫描；常规调用不对全部记录排序。
            if self._count is None or (keys and self._count > self.max_records):
                success = self._prune_locked()
        return success

    def flush(self, force: bool = False) -> bool:
        """
        保存到期媒体的记录；旧历史记录对象的任务或已关闭对象不得继续保存。

        :param force: 跳过空闲等待和失败后的重试间隔，用于缓冲区压力或关闭时最后保存
        :return: 本次记录均保存并确认成功时返回 True；失败时记录仍保留在当前历史记录对象中
        """
        with self.lock:
            with self._buffer:
                if self._closed:
                    return False
            return self._flush_locked(force)

    def stop_admission(self) -> None:
        """
        停止接收新记录，以便解除生产钩子；此操作不访问数据库。
        """
        with self._buffer:
            self._closed = True
            self._buffer.notify_all()

    def close(self) -> bool:
        """
        停止接收新记录，等待正在保存但尚未确认的数据，并保存所有剩余记录。

        保存失败时保留当前历史记录对象中的数据，允许同一对象重试。

        :return: 全部记录均已保存并确认时返回 True；失败时返回 False，不能替换当前历史记录对象
        """
        self.stop_admission()
        with self.lock:
            if not self._flush_locked(force=True):
                return False
            with self._buffer:
                # 保存一个批次期间可能已有后续记录进入缓冲区；关闭时要继续保存，直到没有待确认数据。
                remaining = bool(self._pending or self._inflight)
            return not remaining or self._flush_locked(force=True)

    def _snapshot(self, media_key=""):
        with self._buffer:
            if media_key:
                return (
                    {media_key: deepcopy(self._pending[media_key])} if media_key in self._pending else {},
                    {media_key: deepcopy(self._inflight[media_key])} if media_key in self._inflight else {},
                )
            return deepcopy(self._pending), deepcopy(self._inflight)

    def _load_view(self, query="", media_key=""):
        """
        在单个 I/O 锁内读取匹配媒体和尚未保存的记录，每个媒体最多读取一次。
        """
        pending, inflight = self._snapshot(media_key)
        if media_key:
            bases = {media_key: self._read(media_key)}
        else:
            rows = self.plugin.query_data(key_prefix=self.storage_prefix, query=query.lower())
            bases = {row["key"][len(self.storage_prefix) :]: self._validate(row["value"]) for row in rows}
            for key in (pending.keys() | inflight.keys()) - bases.keys():
                bases[key] = self._read(key)
        return {
            key: value
            for key, base in bases.items()
            if (value := self._view(base, inflight.get(key), pending.get(key))) is not None
        }

    def _rows(self, media, query="", result="", *, include_expired=False):
        """
        为列表和明细生成一致的资源信息；查询隐藏过期记录，但不修改插件数据。
        """
        cutoff = self.clock() - self.retention_days * 86400
        rows = []
        for resource_id, resource in media["resources"].items():
            if not resource["results"]:
                continue
            latest = self._latest(resource)
            if not include_expired and latest["latest_at"] < cutoff:
                continue
            row = {
                "id": resource_id,
                "media_key": media["media_key"],
                "title": media.get("title", ""),
                "tmdb_id": media.get("tmdb_id", ""),
                **resource,
            }
            if not self.matches(row, query, result):
                continue
            selected = self._latest(resource, result)
            counts = {name: summary["count"] for name, summary in resource["results"].items()}
            name = result or max(
                resource["results"],
                key=lambda key: (
                    resource["results"][key]["latest_at"],
                    resource["results"][key]["latest_order"],
                ),
            )
            row.update(
                {
                    **selected,
                    "result": name,
                    "created_at": selected["latest_at"],
                    "latest": latest["latest_at"],
                    "count": sum(counts.values()),
                    "counts": counts,
                    "selected_count": selected["count"],
                }
            )
            row.pop("search_text", None)
            rows.append(row)
        return sorted(rows, key=lambda row: (-row["latest"], row["id"]))

    @staticmethod
    def _stats(rows):
        counts = Counter()
        calls = Counter()
        for row in rows:
            counts.update(row["counts"].keys())
            calls.update(row["counts"])
        return {
            "resource_count": len(rows),
            "counts": dict(counts),
            "call_count": sum(calls.values()),
            "call_counts": dict(calls),
        }

    def groups(self, page: int = 1, page_size: int = 20, query: str = "", result: str = "") -> dict:
        """
        返回媒体分页；分类统计受关键词约束但独立于当前分类选择。

        :param page: 从一开始的媒体页码
        :param page_size: 每页媒体数
        :param query: 不拆词、不去前后空格的字面关键词
        :param result: 空字符串或曾出现的结果分类
        :return: 媒体、资源对象、累计处理统计和待保存状态
        """
        with self.lock:
            views = self._load_view(query)
        stats = {
            "resource_count": 0,
            "counts": Counter(),
            "call_count": 0,
            "call_counts": Counter(),
        }
        groups = []
        for key, media in views.items():
            rows = self._rows(media, query)
            row_stats = self._stats(rows)
            for name in ("counts", "call_counts"):
                stats[name].update(row_stats[name])
            selected = [row for row in rows if self.matches(row, result=result)]
            if selected:
                selected_stats = self._stats(selected)
                for name in ("resource_count", "call_count"):
                    stats[name] += selected_stats[name]
                last = selected[0]
                summary = self._latest(last, result)
                last_result = result or last["result"]
                groups.append(
                    {
                        "media_key": key,
                        "title": media["title"],
                        "tmdb_id": media["tmdb_id"],
                        "latest": last["latest"],
                        "count": len(selected),
                        "last": {
                            "result": last_result,
                            "created_at": summary["latest_at"],
                            **{
                                name: summary.get(name, "")
                                for name in (
                                    "original_range",
                                    "final_range",
                                    "reason",
                                    "source",
                                    "strategy",
                                )
                            },
                        },
                        **selected_stats,
                    }
                )
        groups.sort(key=lambda group: (-group["latest"], group["media_key"]))
        start = (page - 1) * page_size
        return {
            "items": groups[start : start + page_size],
            "total": len(groups),
            "page": page,
            "page_size": page_size,
            **stats,
            "retention_days": self.retention_days,
            "max_records": self.max_records,
            "status": self.status(),
        }

    def records(
        self,
        media_key: str,
        page: int = 1,
        page_size: int = 20,
        result: str = "",
        query: str = "",
    ) -> dict:
        """
        展开单个媒体的匹配资源，不扫描其他媒体、不强制保存记录，也不清理过期数据。

        :param media_key: 精确媒体身份
        :param page: 从一开始的资源页码
        :param page_size: 每页资源数
        :param result: 空字符串或指定分类，指定时展示该分类最新摘要
        :param query: 与媒体列表一致的完整字面关键词
        :return: 每资源一行的分类次数及最新摘要
        """
        with self.lock:
            media = self._load_view(media_key=media_key).get(media_key)
        rows = self._rows(media, query, result) if media else []
        start = (page - 1) * page_size
        return {
            "items": rows[start : start + page_size],
            "total": len(rows),
            "page": page,
            "page_size": page_size,
            **self._stats(rows),
            "status": self.status(),
        }

    def _prepare_cleanup(self, media_key, base, transform):
        """
        确定本次要处理的记录；后续新记录进入下一批，保存失败时保留这次尚未确认的更改。
        """
        with self._buffer:
            old = self._inflight.get(media_key)
            if old and old["kind"] == "cleanup":
                raise RuntimeError("上次清理结果尚未确认，请刷新后重试")
            pending = self._pending.get(media_key)
            reserved_bytes = 0
            reserved_objects = 0
            if not old and not pending:
                # 即使只清理已保存的数据，也要保留批次编号以核对结果，并为失败记录预留缓冲区空间。
                reserved_bytes = 4096 + 3 * self._estimate(media_key)
                reserved_objects = 1
                if (
                    self._bytes + reserved_bytes > self.buffer_max_bytes
                    or self._objects + reserved_objects > self.buffer_max_objects
                ):
                    raise BufferError("历史清理的确认缓冲区空间不足，未保存该媒体的更改")
                self._bytes += reserved_bytes
                self._objects += reserved_objects
            pending = self._pending.pop(media_key, None)
        delta = old["delta"] if old and not self._applied(base, old) else None
        if pending:
            delta = self._merge(delta, pending)
        if delta is None:
            delta = {"media_key": media_key, "resources": {}}
        batch = {
            "id": uuid4().hex,
            "kind": "write",
            "delta": delta,
            "bytes": reserved_bytes + (old["bytes"] if old else 0) + sum((pending or {}).get("sizes", {}).values()),
            "objects": reserved_objects + (old["objects"] if old else 0) + len((pending or {}).get("resources", {})),
        }
        # 完整的历史数据只在保存期间临时读取；保存失败后仅保留本次更改和清理规则。
        with self._buffer:
            self._inflight[media_key] = batch
        desired, stats = transform(self._merge(base, delta))
        batch.update(
            kind="cleanup",
            transform=transform,
            empty=not desired["resources"],
            stats=stats,
        )
        return batch

    def _cleanup_locked(self, transform, media_key=""):
        """
        逐个处理媒体并报告失败项；只移除已成功保存的更改，失败部分仍保留以便重试。
        """
        rows = self._query_data() if not media_key else []
        bases = {row["key"][len(self.storage_prefix) :]: self._validate(row["value"]) for row in rows}
        with self._buffer:
            keys = {media_key} if media_key else bases.keys() | self._pending.keys() | self._inflight.keys()
            buffered_keys = self._pending.keys() | self._inflight.keys()
        result = {
            "deleted": 0,
            "deleted_results": 0,
            "deleted_calls": 0,
            "failed_media": [],
            "success": True,
        }
        for key in sorted(keys, key=lambda key: (key not in buffered_keys, key)):
            try:
                base = bases[key] if key in bases else self._read(key)
                with self._buffer:
                    previous = self._inflight.get(key)
                if previous and previous["kind"] == "cleanup":
                    self._write_batch(key, previous)
                    for name in ("deleted", "deleted_results", "deleted_calls"):
                        result[name] += previous["stats"][name]
                    base = self._read(key)
                batch = self._prepare_cleanup(key, base, transform)
                self._write_batch(key, batch)
                for name in ("deleted", "deleted_results", "deleted_calls"):
                    result[name] += batch["stats"][name]
            except BufferError as error:
                with self._buffer:
                    self._last_error = str(error)
                result["failed_media"].append(key)
                result["success"] = False
            except Exception as error:
                self._error(error)
                result["failed_media"].append(key)
                result["success"] = False
        self._count = None
        if result["success"]:
            with self._buffer:
                self._last_error = ""
                self._failures = 0
                self._retry_at = 0
        return result

    def cleanup(self, request) -> dict:
        """
        清除匹配资源或单一分类，不删除剩余分类，不复活清理前增量。

        :param request: all、expired 或 filtered 范围及可选关键词、分类、媒体
        :return: 受影响对象数、分类数、调用次数和部分失败媒体
        :raises ValueError: 当前历史记录对象已关闭，请刷新后重试清理
        """
        cutoff = self.clock() - self.retention_days * 86400
        scope, query, selected_result, media_key = (
            request.scope,
            request.query,
            request.result,
            request.media_key,
        )

        def transform(value):
            stats = {"deleted": 0, "deleted_results": 0, "deleted_calls": 0}
            for resource_id, resource in list(value["resources"].items()):
                row = {
                    **resource,
                    "title": value.get("title", ""),
                    "media_key": value["media_key"],
                }
                matched = (
                    scope == "all"
                    or (scope == "expired" and self._latest(resource)["latest_at"] < cutoff)
                    or (scope == "filtered" and self.matches(row, query, selected_result, media_key))
                )
                if not matched:
                    continue
                names = [selected_result] if scope == "filtered" and selected_result else list(resource["results"])
                stats["deleted"] += 1
                for name in names:
                    stats["deleted_results"] += 1
                    stats["deleted_calls"] += resource["results"].pop(name)["count"]
                if not resource["results"]:
                    del value["resources"][resource_id]
            return value, stats

        with self.lock:
            with self._buffer:
                if self._closed:
                    raise ValueError("当前历史记录对象已关闭，请刷新后重试清理")
            return self._cleanup_locked(transform, media_key if scope == "filtered" else "")

    def _prune_locked(self):
        """
        统计已保存和缓冲中的资源，清理超出保留期限或数量上限的旧记录。
        """
        try:
            with self._buffer:
                calibration_order = self._order
            views = self._load_view()
            candidates = [(row["latest"], key, row["id"]) for key, media in views.items() for row in self._rows(media)]
            candidates.sort(key=lambda row: (-row[0], row[1], row[2]))
            kept = candidates[: self.max_records]
            threshold = (-kept[-1][0], kept[-1][1], kept[-1][2]) if kept else None
            cutoff = self.clock() - self.retention_days * 86400

            def retained(key, resource_id, resource):
                latest = self._latest(resource)["latest_at"]
                return latest >= cutoff and threshold is not None and (-latest, key, resource_id) <= threshold

            affected = {
                key
                for key, media in views.items()
                if any(not retained(key, resource_id, resource) for resource_id, resource in media["resources"].items())
            }

            def transform(value):
                stats = {"deleted": 0, "deleted_results": 0, "deleted_calls": 0}
                for resource_id, resource in list(value["resources"].items()):
                    if not retained(value["media_key"], resource_id, resource):
                        stats["deleted"] += 1
                        stats["deleted_results"] += len(resource["results"])
                        stats["deleted_calls"] += sum(row["count"] for row in resource["results"].values())
                        del value["resources"][resource_id]
                return value, stats

            for key in sorted(affected):
                if not self._cleanup_locked(transform, key)["success"]:
                    return False
            with self._buffer:
                # 统计期间新增记录可能随同一媒体的清理一起保存；有新增时重新统计，确保资源数量准确。
                self._count = len(kept) if self._order == calibration_order else None
            return True
        except Exception as error:
            self._error(error)
            return False

    def prune(self) -> bool:
        """
        定时或初始化清理过期、超量资源；停止后的旧服务不再写入。

        :return: 已确认清理返回 True，停止或持久化失败返回 False
        """
        with self.lock:
            with self._buffer:
                if self._closed:
                    return False
            return self._prune_locked()
