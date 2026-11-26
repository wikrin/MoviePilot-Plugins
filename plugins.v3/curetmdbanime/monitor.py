"""管理上游自动监测；后台定时任务探测并保存结果，页面查询只读取已保存记录。"""

from __future__ import annotations

from collections.abc import Callable

from app.sdk.logging import logger

from .config import CureTMDbAnimeConfig
from .management import ManagementClient
from .runtime import ServiceController, ServiceError, redact
from .upstreams import UpstreamHistoryStore


class UpstreamMonitor:
    def __init__(
        self,
        *,
        runtime: ServiceController,
        management: ManagementClient,
        storage,
        config_provider: Callable[[], CureTMDbAnimeConfig],
    ):
        self.runtime = runtime
        self.management = management
        self._storage = storage
        self._config_provider = config_provider
        self.history: UpstreamHistoryStore | None = None

    @property
    def config(self) -> CureTMDbAnimeConfig:
        return self._config_provider()

    def identity(self) -> str:
        target = {"mode": self.config.service_mode, "address": self.runtime.base_url}
        if self.config.service_mode == "local":
            # 只取实际启动时影响探测的选项；修正规则/分季源/令牌不入指纹。
            target["upstreams"] = self.runtime.upstream_options()
        return UpstreamHistoryStore.fingerprint(target)

    def prepare(self):
        self.history = UpstreamHistoryStore(self._storage, self.identity())
        try:
            self.history.prune()
        except Exception as error:  # noqa: BLE001 -- 无法保存监测历史不能阻断服务启动
            # 无法保存监测历史记录不影响服务启动和元数据修正。
            logger.warning(f"无法保存上游监测历史记录，不影响服务启动和元数据修正：{redact(str(error))}")

    def monitoring(self):
        return (
            self.config.enabled
            and not self.runtime.event.is_set()
            and self.runtime.state not in {"stopped", "downloading"}
        )

    def collect(self):
        """仅由后台定时任务探测并保存结果；手动检测和页面查询不会增加历史记录。"""
        if not self.runtime.command_lock.acquire(blocking=False):
            return
        try:
            history = self.history
            if not history or not self.monitoring():
                return
            sampled_at = history.clock()
            if not history.needs_sample(sampled_at):
                return
            try:
                data = self.management.request("/management/upstreams", timeout=8)
            except ServiceError as error:
                message = redact(str(error))
                history.record(error=message, sampled_at=sampled_at)
                logger.warning(f"上游自动监测无法执行：{message}")
            else:
                history.record(data, sampled_at=sampled_at)
        except Exception as error:  # noqa: BLE001 -- 定时任务失败不能逃逸到调度器
            logger.warning(f"上游自动监测失败：{redact(str(error))}")
        finally:
            self.runtime.command_lock.release()

    def snapshot(self) -> dict:
        """读取已保存的监测记录和状态，不触发探测或修改数据。"""
        if self.history is None:
            raise ServiceError("上游监测历史尚未初始化", 503)
        try:
            result = self.history.snapshot()
        except Exception as error:
            logger.warning(f"上游监测历史读取失败：{redact(str(error))}")
            raise ServiceError("上游监测历史暂时不可读，请稍后重试", 503) from error
        if not self.config.enabled:
            message = "插件未启用，自动监测暂停；已有历史仍可查看。"
        elif not self.monitoring():
            message = "服务已停止或正在下载/更新，自动监测暂停；已有历史仍可查看。"
        elif not (self.runtime.health or {}).get("capabilities", {}).get("upstreams"):
            message = "服务未连接或缺少上游管理能力；请更新服务并配置管理令牌，无法探测的时段显示灰色。"
        elif result["checked_at"] is not None and all(
            row["latest"] and row["latest"]["state"] == "unknown" for row in result["items"]
        ):
            message = f"最近自动探测未能执行：{result['items'][0]['latest']['message']}"
        else:
            message = ""
        return {**result, "monitoring": self.monitoring(), "message": message}
