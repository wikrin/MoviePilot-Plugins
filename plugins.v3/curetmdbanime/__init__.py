from __future__ import annotations

import secrets
import threading
import time
from typing import Any

from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.plugin.base import _PluginBase
from app.sdk.scheduler import add_plugin_once_job, list_scheduler_jobs

from .api import PluginAPI
from .config import CureTMDbAnimeConfig
from .correction import CorrectionService
from .data import query_plugin_data
from .history import HistoryStore
from .management import ManagementClient
from .monitor import UpstreamMonitor
from .patch import MonkeyPatchManager
from .runtime import ServiceController, ServiceError


class CureTMDbAnime(_PluginBase):
    # 插件名称
    plugin_name = "CTMDbA"
    # 插件描述
    plugin_desc = "将 TMDb 上被合并为一季的番剧拆分为多季。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/wikrin/MoviePilot-Plugins/main/icons/ctmdbanime.png"
    # 插件版本
    plugin_version = "3.1.1"
    # 插件作者
    plugin_author = "Attente"
    # 作者主页
    author_url = "https://github.com/wikrin"
    # 插件配置项ID前缀
    plugin_config_prefix = "curetmdbanime_"
    # 加载顺序
    plugin_order = 99
    # 可使用的用户级别
    auth_level = 1
    # 可执行文件
    binary_name = "curetmdbanime"
    # 可执行文件版本
    binary_version = "1.5.1"

    def __init__(self):
        """
        装配稳定组件和生命周期锁，不启动服务或后台任务。
        """
        super().__init__()
        self.config = CureTMDbAnimeConfig()
        self.patch_manager = MonkeyPatchManager()
        self._patch_lock = threading.RLock()
        self._history_close_lock = threading.RLock()
        # 保留既有本地二进制数据目录；实例通过类名隔离。
        self.runtime = ServiceController(
            settings.PLUGIN_DATA_PATH / self.__class__.__name__.lower(),
            self.binary_name,
            self.binary_version,
            self._connected,
            self._disconnected,
        )
        self.history = None
        self._history_generation = 0
        self._history_closed = True
        self._history_drained = True
        self._scheduler_checked_at = float("-inf")
        self._active_address = None
        self.management = ManagementClient(self.runtime, self._management_token)
        self.correction = CorrectionService(
            runtime=self.runtime,
            patch_manager=self.patch_manager,
            chain=self.chain,
            config_provider=lambda: self.config,
            history_provider=lambda: self.history,
        )
        self.monitor = UpstreamMonitor(
            runtime=self.runtime,
            management=self.management,
            storage=self,
            config_provider=lambda: self.config,
        )
        self.api = PluginAPI(
            runtime=self.runtime,
            patch_manager=self.patch_manager,
            management=self.management,
            monitor=self.monitor,
            config_provider=lambda: self.config,
            history_provider=lambda: self.history,
        )

    def init_plugin(self, config: dict | None = None):
        """
        先完成旧历史记录的最终保存，再应用新配置并创建新的历史记录缓冲区。

        :raises ServiceError: 旧历史记录保存失败；保留旧记录，等待后续关闭时重试。
        """
        validated = CureTMDbAnimeConfig.model_validate(config or {})
        with self.runtime.command_lock, self._history_close_lock:
            self.close()
            self.runtime.stop()

            history = HistoryStore(self, validated.history_max_records)
            history.prune()

            self.config = validated
            self.runtime.config = validated.model_copy(deep=True)

            with self._patch_lock:
                self.history = history
                self._history_generation += 1
                self._history_closed = False
                self._history_drained = False

                generation = self._history_generation
                history.set_request_flush(
                    lambda: self._request_history_flush(generation)
                )
                self._scheduler_checked_at = time.monotonic()

                # 稍后会注册定期保存任务，注册前先视为不可用。
                history.set_scheduler_available(False)

            logger.debug("历史记录定期保存等待调度器注册")
            self.monitor.prepare()
            self.correction.configure(validated)

            if validated.enabled:
                self.runtime.start(validated, self._management_token())

    def _management_token(self):
        if self.config.service_mode == "external":
            return self.config.management_token
        token = self.get_data(key="local_management_token")
        if not token:
            token = secrets.token_urlsafe(32)
            self.save_data(key="local_management_token", value=token)
        return token

    def query_data(self, *, key_prefix: str, query: str = "") -> list[dict[str, Any]]:
        """
        在插件内按字面前缀和固定历史搜索字段查询当前实例数据。

        不依赖基类或 PluginDataOper 新增 query_data 方法；复用已有查询
        执行入口，不导入宿主模型、不新建会话或提交调用方事务。

        :param key_prefix: 区分大小写的完整字面键前缀
        :param query: 完整字面关键词，百分号和下划线不是通配符
        :return: 普通 key/value 候选数据，资源精筛由 HistoryStore 完成
        """
        return query_plugin_data(
            self.plugindata._execute_sync_query,
            self.__class__.__name__,
            key_prefix,
            query,
        )

    def stop_service(self):
        """
        停止接收新记录、等待已开始的保存并完成最后保存，然后停止外部服务。

        保存失败时保留原历史记录对象，后续可以重试。
        """
        with self.runtime.command_lock:
            try:
                self.close()
            finally:
                self.runtime.stop()

    def close(self):
        """
        停止接收新记录并解除生产钩子，等待正在保存但尚未确认的数据，再完成最后保存。

        保存失败时仍保持关闭状态；重复调用会对原历史记录对象重试，不会替换其中的数据。

        :raises ServiceError: 最后保存失败；原历史记录对象仍保留，不能视为已完成收尾
        """
        with self._history_close_lock:
            with self._patch_lock:
                history = self.history
                if not self._history_closed:
                    self._history_closed = True
                    self._history_generation += 1
                    if history is not None:
                        history.stop_admission()
                        history.set_scheduler_available(False)
                self._disconnected()
            if history is not None and not self._history_drained:
                try:
                    closed = history.close()
                except Exception as error:
                    raise ServiceError(
                        "历史记录最终保存失败，保留记录等待重试"
                    ) from error
                if not closed:
                    raise ServiceError("历史记录最终保存失败，保留记录等待重试")
                self._history_drained = True

    def _connected(self, address, event):
        """
        仅在当前历史记录对象仍接收新记录时安装生产钩子。
        """
        with self._patch_lock:
            if event.is_set() or not self.config.enabled or self._history_closed:
                return
            if self._active_address == address:
                return
            self.patch_manager.patch_build_url(address)
            if self.config.enable_correction:
                generation, history = self._history_generation, self.history

                def correct(meta, mediainfo, **kwargs):
                    """
                    旧回调不能向插件重载后创建的新历史记录对象写入数据。
                    """
                    if self._history_closed or generation != self._history_generation:
                        return meta
                    self._refresh_history_scheduler()
                    return self.correction.correct_meta(
                        meta, mediainfo, history=history, **kwargs
                    )

                self.patch_manager.patch_meta_enhancement(correct)
            self._active_address = address

    def _disconnected(self):
        """
        幂等解除生产钩子，不丢弃历史记录对象中尚未保存的记录。
        """
        with self._patch_lock:
            if self._active_address or self.patch_manager.is_patched():
                self.patch_manager.unpatch_all()
            self._active_address = None

    @staticmethod
    def get_render_mode():
        return "vue", "dist/assets"

    def get_form(self):
        return [], CureTMDbAnimeConfig().model_dump()

    def get_page(self):
        pass

    def get_state(self):
        return self.config.enabled

    def get_service(self):
        """
        声明服务；业务参数和调度参数分别传递。
        """
        return [
            {
                "id": "CTMDbAHistoryFlush",
                "name": "历史记录定期保存",
                "trigger": "interval",
                "func": self.flush_history,
                "func_kwargs": {"generation": self._history_generation},
                "kwargs": {
                    "seconds": HistoryStore.flush_interval_seconds,
                    "max_instances": 1,
                    "coalesce": True,
                },
            },
            {
                "id": "CTMDbAHistoryCleanup",
                "name": "历史记录清理",
                "trigger": "interval",
                "func": self.cleanup_expired_history,
                "func_kwargs": {"generation": self._history_generation},
                "kwargs": {"hours": 6},
            },
            {
                "id": "CTMDbAUpstreamMonitor",
                "name": "连通性检测",
                "trigger": "interval",
                "func": self.collect_upstream_history,
                "kwargs": {"minutes": 30},
            },
        ]

    def collect_upstream_history(self):
        """
        仅此入口追加自动样本，页面和手动测试不调用。
        """
        self.monitor.collect()

    def _refresh_history_scheduler(self):
        """
        检查定期保存任务是否已注册；每五秒最多检查一次，不自行创建后台循环。

        检查失败也不能影响识别。
        """
        with self._patch_lock:
            history = self.history
            generation = self._history_generation
            now = time.monotonic()
            if (
                history is None
                or self._history_closed
                or now - self._scheduler_checked_at < 5
            ):
                return
            self._scheduler_checked_at = now
        try:
            job_id = f"{self.__class__.__name__}_CTMDbAHistoryFlush"
            available = any(
                getattr(job, "id", None) == job_id for job in list_scheduler_jobs()
            )
        except Exception:  # noqa: BLE001 -- 检查保存任务失败也不能影响识别
            available = False
        with self._patch_lock:
            if (
                self._history_closed
                or generation != self._history_generation
                or history is not self.history
            ):
                return
            history.set_scheduler_available(available)
            if not available:
                logger.warning(
                    "历史记录的定时保存暂不可用，记录先留在内存中；插件停止时仍会尝试保存。"
                )

    def _request_history_flush(self, generation):
        """
        历史记录快到内存缓冲上限时，尽快安排一次保存，避免记录继续积压。

        插件已关闭或请求来自旧的历史记录时，不再处理。
        如果无法安排这次保存，不影响媒体识别；记录留待可用的保存任务处理，
        插件停止时也会尝试做最后一次保存。
        """
        with self._patch_lock:
            if self._history_closed or generation != self._history_generation:
                return
            self._refresh_history_scheduler()
            if not self.history.status()["scheduler_available"]:
                return
            try:
                available = add_plugin_once_job(
                    self.__class__.__name__,
                    "CTMDbAHistoryPressure",
                    self.flush_history,
                    "历史记录阈值保存",
                    func_kwargs={"generation": generation, "force": True},
                )
            except Exception:  # noqa: BLE001 -- 调度失败不能中断识别
                available = False

            if not available:
                self.history.set_scheduler_available(False)
                logger.warning(
                    "历史记录接近缓冲区上限，无法安排立即保存；记录暂留内存，停止时仍会尝试保存。"
                )

    def flush_history(self, generation=None, force=False):
        """
        只保存当前历史记录对象的记录；旧任务或已关闭对象不得继续保存。
        """
        with self._patch_lock:
            if self._history_closed or generation != self._history_generation:
                return
            history = self.history
            history.set_scheduler_available(True)
        history.flush(force=force)

    def cleanup_expired_history(self, generation=None):
        """
        只清理当前历史记录对象中的过期资源；旧任务或已关闭对象不得继续清理。
        """
        with self._patch_lock:
            if self._history_closed or generation != self._history_generation:
                return
            history = self.history
        history.prune()

    def get_api(self):
        return self.api.get_api()
