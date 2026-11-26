"""只保存后台定时探测结果；手动探测和页面查询不增加记录，每个半小时时段最多保留一条。"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time

from .runtime import redact


class UpstreamHistoryStore:
    storage_key = "upstream_monitor_history"
    schema_version = 1
    interval_seconds = 1800
    slot_count = 48
    names = ("tmdb", "tvdb", "bangumi")

    def __init__(self, plugin, identity: str, clock=None):
        self.plugin = plugin
        self.identity = identity
        self.clock = clock or time.time
        self.lock = threading.RLock()

    @staticmethod
    def fingerprint(target: dict) -> str:
        """只持久保存指纹，不保存服务地址、管理令牌或代理凭据。"""
        value = json.dumps(target, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(value.encode()).hexdigest()

    @classmethod
    def slot_for(cls, timestamp: float) -> int:
        return math.floor(timestamp / cls.interval_seconds) * cls.interval_seconds

    @classmethod
    def window(cls, now: float) -> tuple[int, int]:
        """返回最近 48 个半小时时段的边界，包含尚未结束的当前时段。"""
        end = cls.slot_for(now) + cls.interval_seconds
        return end - cls.slot_count * cls.interval_seconds, end

    @staticmethod
    def _message(value) -> str:
        return redact(value if isinstance(value, str) else "")[:512]

    @staticmethod
    def _number(value) -> bool:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        try:
            return value >= 0 and math.isfinite(value)
        except OverflowError:
            return False

    @classmethod
    def _unknown(cls, name: str, message: str) -> dict:
        return {
            "name": name,
            "state": "unknown",
            "reachable": None,
            "available": None,
            "status_code": None,
            "latency_ms": None,
            "message": cls._message(message),
        }

    @classmethod
    def _item(cls, name: str, item: dict) -> dict:
        # 持久化的不可测记录不重新解释为实际上游连接失败。
        if item.get("state") == "unknown" and item.get("reachable") is None and item.get("available") is None:
            return cls._unknown(name, item.get("message", "无法探测"))
        reachable, available = item.get("reachable"), item.get("available")
        code = item.get("status_code")
        if (
            not isinstance(reachable, bool)
            or not isinstance(available, bool)
            or not isinstance(code, int)
            or isinstance(code, bool)
            or not (code == 0 or 100 <= code <= 599)
            or reachable != (code != 0)
            or available != (reachable and 200 <= code < 300)
        ):
            return cls._unknown(name, "服务返回的上游状态无效")
        latency = item.get("latency_ms")
        return {
            "name": name,
            "state": "available" if available else "degraded" if reachable else "failed",
            "reachable": reachable,
            "available": available,
            "status_code": code,
            "latency_ms": latency if cls._number(latency) else None,
            "message": cls._message(item.get("message", "")),
        }

    @classmethod
    def _items(cls, items) -> list[dict]:
        found, duplicate = {}, set()
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            name = item["name"].lower()
            if name not in cls.names:
                continue
            if name in found:
                duplicate.add(name)
            found[name] = item
        return [
            cls._unknown(name, "服务返回了重复的上游结果")
            if name in duplicate
            else cls._item(name, found[name])
            if name in found
            else cls._unknown(name, "服务未返回此上游检测结果")
            for name in cls.names
        ]

    def _load(self) -> tuple[object, list]:
        value = self.plugin.get_data(key=self.storage_key)
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != self.schema_version
            or value.get("identity") != self.identity
            or not isinstance(value.get("samples"), list)
        ):
            return value, []
        return value, value["samples"]

    def _retained(self, samples: list, now: float) -> list[dict]:
        start, end = self.window(now)
        by_slot = {}
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            slot, checked_at = sample.get("slot"), sample.get("checked_at")
            if (
                not isinstance(slot, int)
                or isinstance(slot, bool)
                or slot % self.interval_seconds
                or not start <= slot < end
                or not self._number(checked_at)
            ):
                continue
            normalized = {
                "slot": slot,
                "checked_at": checked_at,
                "items": self._items(sample.get("items")),
            }
            # 同一半小时时段只保留首次自动探测结果，配置重载或重复调度不覆盖已有记录。
            previous = by_slot.get(slot)
            if previous is None or checked_at < previous["checked_at"]:
                by_slot[slot] = normalized
        return [by_slot[slot] for slot in sorted(by_slot)][-self.slot_count :]

    def _payload(self, samples: list) -> dict:
        return {
            "schema_version": self.schema_version,
            "identity": self.identity,
            "samples": samples,
        }

    def prune(self):
        """初始化/自动写入时清理；目标改变时开启新的有界历史。"""
        with self.lock:
            previous, samples = self._load()
            value = self._payload(self._retained(samples, self.clock()))
            if previous is not None and previous != value:
                self.plugin.save_data(key=self.storage_key, value=value)

    def needs_sample(self, sampled_at: float | None = None) -> bool:
        with self.lock:
            now = self.clock() if sampled_at is None else sampled_at
            _, samples = self._load()
            return not any(sample["slot"] == self.slot_for(now) for sample in self._retained(samples, now))

    def record(self, data: dict | None = None, *, error: str = "", sampled_at: float | None = None) -> bool:
        """只有后台任务调用；采样起始时间确定格子，完成时间用于详情。"""
        with self.lock:
            now = self.clock()
            slot = self.slot_for(now if sampled_at is None else sampled_at)
            _, previous = self._load()
            samples = self._retained(previous, now)
            start, end = self.window(now)
            if not start <= slot < end or any(row["slot"] == slot for row in samples):
                return False
            items = (
                [self._unknown(name, error) for name in self.names]
                if error
                else self._items(data.get("items") if isinstance(data, dict) else None)
            )
            value = self._payload(self._retained([*samples, {"slot": slot, "checked_at": now, "items": items}], now))
            self.plugin.save_data(key=self.storage_key, value=value)
            return True

    @staticmethod
    def _stats(samples: list[dict]) -> dict:
        """按实际连通结果统计，缺测和不可测不进入分母。"""
        observed = [row for row in samples if isinstance(row["reachable"], bool)]
        reachable = sum(row["reachable"] is True for row in observed)
        latencies = sorted(
            row["latency_ms"] for row in observed if row["reachable"] is True and row["latency_ms"] is not None
        )
        return {
            "sample_count": len(observed),
            "reachable_count": reachable,
            "reachability_percent": round(100 * reachable / len(observed), 2) if observed else None,
            "latency_sample_count": len(latencies),
            "avg_latency_ms": round(sum(value / len(latencies) for value in latencies), 1) if latencies else None,
            "p90_latency_ms": latencies[math.ceil(0.9 * len(latencies)) - 1] if latencies else None,
        }

    def snapshot(self) -> dict:
        """读取已保存的探测记录，不请求上游或修改数据；图表和统计使用同一 24 小时范围。"""
        with self.lock:
            now = self.clock()
            _, previous = self._load()
            retained = self._retained(previous, now)
        start, end = self.window(now)
        batches = {row["slot"]: row for row in retained}
        items = []
        for index, name in enumerate(self.names):
            samples = []
            for slot in range(start, end, self.interval_seconds):
                batch = batches.get(slot)
                samples.append(
                    {
                        **(batch["items"][index] if batch else self._unknown(name, "此时段未采样")),
                        "slot_start": slot,
                        "slot_end": slot + self.interval_seconds,
                        "checked_at": batch["checked_at"] if batch else None,
                    }
                )
            latest = next((row for row in reversed(samples) if row["checked_at"] is not None), None)
            items.append({"name": name, "latest": latest, "samples": samples, "stats": self._stats(samples)})
        return {
            "interval_seconds": self.interval_seconds,
            "window_hours": self.slot_count * self.interval_seconds // 3600,
            "window_start": start,
            "window_end": end,
            "generated_at": now,
            "checked_at": retained[-1]["checked_at"] if retained else None,
            "items": items,
        }
