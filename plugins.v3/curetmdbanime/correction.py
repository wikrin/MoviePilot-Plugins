"""
元数据修正编排；核心分季决策继续由 engine 承担。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable

from app.db.models.downloadhistory import DownloadHistory
from app.sdk.cache import cached
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.media import MediaInfo, MetaBase

from .config import CureTMDbAnimeConfig
from .engine import MetaCorrectionUseCase
from .history import HistoryStore
from .patch import MonkeyPatchManager
from .runtime import ServiceController, ServiceError, redact


class CorrectionService:
    """
    隔离修正异常并记录通过目标准入的动画资源。
    """

    def __init__(
        self,
        *,
        runtime: ServiceController,
        patch_manager: MonkeyPatchManager,
        chain,
        config_provider: Callable[[], CureTMDbAnimeConfig],
        history_provider: Callable[[], HistoryStore | None],
    ):
        """
        注入运行时、补丁和历史提供者，不在构造时访问外部服务。
        """
        self.runtime = runtime
        self.patch_manager = patch_manager
        self.chain = chain
        self._config_provider = config_provider
        self._history_provider = history_provider
        self.use_case = MetaCorrectionUseCase()

    @property
    def config(self) -> CureTMDbAnimeConfig:
        """
        获取当前配置，重载后不保留旧配置副本。
        """
        return self._config_provider()

    @property
    def history(self) -> HistoryStore | None:
        """
        获取当前历史记录对象；生产钩子会为单次修正单独捕获对象，以隔离插件重载。
        """
        return self._history_provider()

    def configure(self, config: CureTMDbAnimeConfig):
        """
        根据已验证配置替换纯修正策略。
        """
        self.use_case = MetaCorrectionUseCase(
            grace_episodes=config.grace_episodes,
            assume_season_by_window=config.assume_season_by_window,
            production_cycle_gap_days=config.production_cycle_gap_days,
        )

    # 保留原入口缓存区（包括实例包名），避免搬迁方法时改变已有缓存语义。
    @cached(region=f"{__package__}:CureTMDbAnime:_load_mapping", ttl=2 * 3600)
    def _load_mapping(self, address: str, tmdb_id: int, generation: int):
        """
        按服务地址、媒体和服务启动编号缓存季集映射，响应错误不缓存。
        """
        # 地址和服务启动编号进入缓存键，避免切换模式或重启后复用旧服务映射。
        result = self.runtime.request(f"/cache/mapping/{tmdb_id}", timeout=2, address=address)
        if not isinstance(result, dict):
            raise ServiceError("季集映射接口响应无效")
        mapping = {}
        for season, episodes in result.items():
            if not isinstance(episodes, dict):
                continue
            for episode, item in episodes.items():
                try:
                    mapping[(int(season), int(episode))] = (
                        int(item["season"]),
                        int(item["episode"]),
                    )
                except TypeError, ValueError, KeyError:
                    continue
        return mapping

    def _record(
        self,
        *,
        meta: MetaBase,
        mediainfo: MediaInfo,
        history: HistoryStore,
        before: str,
        result: str,
        reason: str,
        strategy: str,
        source: str,
    ):
        """
        从当前元数据提取资源信息，写入本次修正使用的历史记录对象。
        """
        try:
            if history is None:
                return

            title = mediainfo.title_year or meta.title or meta.name or "未识别媒体"
            key = f"{mediainfo.type.name}:{mediainfo.tmdb_id}" + hashlib.sha256(title.encode()).hexdigest()[:24]
            history.record(
                media_key=key,
                tmdb_id=mediainfo.tmdb_id,
                title=title,
                resource_title=meta.title or "",
                resource_subtitle=meta.subtitle or "",
                result=result,
                original_range=before,
                final_range=meta.season_episode,
                reason=redact(reason),
                strategy=strategy,
                source=source,
            )
        except Exception:  # noqa: BLE001 -- 历史写入不能中断识别
            logger.error("无法保存动画修正历史记录，请检查插件数据存储")

    def correct_meta(
        self,
        meta: MetaBase,
        mediainfo: MediaInfo,
        *,
        downloadhistory: DownloadHistory | None = None,
        source: str = "unknown",
        history: HistoryStore | None = None,
    ) -> MetaBase:
        """
        修正目标动画；符合记录条件后才保存本次处理结果。

        :param downloadhistory: 下载历史或字段兼容的只读快照
        :param history: 生产钩子在本次修正开始时捕获的历史记录对象；未传入时使用当前对象
        :return: 原元数据对象；资格检查、修正和历史保存异常均不向外传播
        """
        admitted = False
        before = ""
        result, reason, strategy = "skipped", "", ""
        try:
            if not self.config.enable_correction:
                reason = "元数据修正已关闭"
                return meta
            if not meta or not mediainfo or getattr(getattr(mediainfo, "type", None), "name", None) != "TV":
                reason = "缺少元数据/媒体信息或非电视剧"
                return meta
            if set(mediainfo.genre_ids or []).isdisjoint(settings.ANIME_GENREIDS):
                reason = "非动画媒体"
                return meta
            admitted = True
            before = meta.season_episode
            history = history if history is not None else self.history
            resource_title = downloadhistory.torrent_name if downloadhistory is not None else meta.title or ""
            resource_subtitle = (
                downloadhistory.torrent_description if downloadhistory is not None else meta.subtitle or ""
            )

            if meta.apply_words and any(" >> " in word and " <> " in word for word in meta.apply_words):
                reason = "已应用季集偏移识别词，跳过重复调整"
                return meta
            if (
                downloadhistory is not None
                and getattr(downloadhistory, "seasons", None)
                and getattr(downloadhistory, "episodes", None)
            ):
                meta.set_season([int(x) for x in re.findall(r"\d+", downloadhistory.seasons)])
                meta.set_episode([int(x) for x in re.findall(r"\d+", downloadhistory.episodes)])
                result = "changed" if before != meta.season_episode else "unchanged"
                reason, strategy = "使用下载历史记录的季集值", "download_history"
                return meta

            if not meta.episode_list:
                reason = "资源未解析出集号，跳过季集修正"
                return meta

            if not mediainfo.seasons and (info := self.chain.tmdb_info(mediainfo.tmdb_id, mtype=mediainfo.type)):
                mediainfo = MediaInfo(tmdb_info=info)
            pubdate = self.patch_manager.get_torrent_pubdate(
                title=resource_title,
                description=resource_subtitle,
            )

            decision = self.use_case.correct(
                meta=meta,
                tmdb_mapping=self._load_mapping(self.runtime.base_url, mediainfo.tmdb_id, self.runtime.generation),
                mediainfo=mediainfo,
                publish_date=pubdate,
                source="torrent_pubdate" if pubdate else None,
            )
            source = "torrent_pubdate" if pubdate else source
            result = "skipped" if decision.skipped else "changed" if decision.changed else "unchanged"
            reason = "；".join(decision.reasons)
            if decision.selected_candidate:
                strategy = decision.selected_candidate.strategy
            if decision.changed:
                meta.set_season(decision.final_range.season_list)
                meta.set_episode(decision.final_range.episode_list)
                logger.info(f"{meta.title}, {reason}")
            return meta
        except Exception as error:  # noqa: BLE001 -- 元数据钩子的错误边界
            result, reason = "failed", f"{type(error).__name__}: {redact(str(error))}"
            logger.warning(f"CTMDbA 元数据修正失败：{reason}")
            return meta
        finally:
            if admitted:
                self._record(
                    meta=meta,
                    mediainfo=mediainfo,
                    history=history,
                    before=before,
                    result=result,
                    reason=reason,
                    strategy=strategy,
                    source=source,
                )
