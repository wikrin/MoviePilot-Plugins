from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta

from app.sdk.media import MediaInfo, MetaBase
from app.sdk.logging import logger

from .models import (
    AdjustmentCandidate,
    EpisodePoint,
    EpisodeRange,
    EvidenceItem,
    ProductionCycle,
    RangeAdjustmentDecision,
    ReleaseInfo,
    ShowContext,
)


def _range_is_absolute_contiguous(
    context: ShowContext,
    episode_range: EpisodeRange,
    grace_episodes: int,
) -> bool:
    """
    检查范围在累计集序上是否连续 - 允许最新季宽限区缺失

    :param context: 剧集上下文
    :param episode_range: 待检查范围
    :param grace_episodes: 宽限集数
    :return: 连续时返回 True
    """
    begin_absolute = context.absolute_by_point(episode_range.begin)
    end_absolute = context.absolute_by_point(episode_range.end)

    if begin_absolute is not None and end_absolute is not None:
        return context.is_contiguous_range(episode_range)

    points = context.expand_target_points(episode_range)
    existing_points = tuple(point for point in points if context.contains_point(point))
    if not existing_points:
        return False

    missing_points = tuple(
        point for point in points if not context.contains_point(point)
    )
    if any(
        not context.is_latest_season_grace_point(point, grace_episodes)
        for point in missing_points
    ):
        return False

    return context.is_contiguous_range(
        EpisodeRange(begin=existing_points[0], end=existing_points[-1])
    )


def _coerce_date(value: object) -> date | None:
    """
    将外部逐集数据中的日期值转换为日期对象。

    :param value: 外部数据中的日期值
    :return: 成功转换时返回日期，否则返回 None
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


class RangeDecisionEngine:
    """
    根据发布信息和剧集上下文决定最终季集范围。

    引擎按固定策略顺序尝试候选，候选通过统一硬约束后立即采用；没有唯一合法目标时保持原始范围。

    :cvar _CASCADE_STRATEGY_ORDER: 规则级联的策略优先级
    """

    _KEEP_ORIGINAL_STRATEGY = "keep_original"
    _NORMALIZE_EPISODE_RANGE_STRATEGY = "normalize_episode_range"
    _EXPLICIT_MAPPING_STRATEGY = "explicit_mapping"
    _ABSOLUTE_EPISODE_STRATEGY = "absolute_episode"
    _PRODUCTION_CYCLE_STRATEGY = "production_cycle"
    _SEASON_BY_WINDOW_STRATEGY = "season_by_window"

    _CASCADE_STRATEGY_ORDER = (
        _EXPLICIT_MAPPING_STRATEGY,
        _NORMALIZE_EPISODE_RANGE_STRATEGY,
        _SEASON_BY_WINDOW_STRATEGY,
        _ABSOLUTE_EPISODE_STRATEGY,
        _PRODUCTION_CYCLE_STRATEGY,
        _KEEP_ORIGINAL_STRATEGY,
    )

    def __init__(
        self,
        grace_episodes: int = 3,
        assume_season_by_window: bool = False,
        episode_loader: Callable[..., list[object] | None] | None = None,
    ) -> None:
        """
        初始化范围决策引擎。

        播出窗口规则只在通过本地前置条件后调用逐集信息加载器；加载器由用例层提供，
        其内部可以复用主程序已有的 TMDB 缓存。

        :param grace_episodes: 最新季允许暂时缺失的集数
        :param assume_season_by_window: 是否允许按发布时间推断默认季号
        :param episode_loader: 按需加载指定季逐集信息的调用对象
        """
        self.grace_episodes = grace_episodes
        self.assume_season_by_window = assume_season_by_window
        self.episode_loader = episode_loader

    def decide(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidates: list[AdjustmentCandidate] | None = None,
    ) -> RangeAdjustmentDecision:
        original_range = release_info.parsed_range
        if original_range is None:
            raise ValueError("release_info.parsed_range 不能为空")

        logger.debug(
            "%s [决策开始] 原始=%s 发布时间=%s",
            release_info.title,
            original_range.format(),
            release_info.release_date.isoformat()
            if release_info.release_date
            else "未知",
        )

        evaluated_candidates: list[AdjustmentCandidate] = []
        rejected_candidates: list[AdjustmentCandidate] = []

        provided_by_strategy: dict[str, list[AdjustmentCandidate]] = {}
        if candidates is not None:
            for candidate in candidates:
                provided_by_strategy.setdefault(candidate.strategy, []).append(
                    candidate
                )

        strategies = self._CASCADE_STRATEGY_ORDER + tuple(
            strategy
            for strategy in provided_by_strategy
            if strategy not in self._CASCADE_STRATEGY_ORDER
        )
        for strategy in strategies:
            strategy_candidates = (
                provided_by_strategy.get(strategy)
                if candidates is not None
                else self._generate_candidates_for_strategy(
                    strategy,
                    release_info,
                    show_context,
                )
            )
            if not strategy_candidates:
                continue

            accepted_candidates: list[AdjustmentCandidate] = []
            for candidate in strategy_candidates:
                evaluated, accepted, rejection_reason = (
                    self._evaluate_cascade_candidate(
                        release_info=release_info,
                        show_context=show_context,
                        candidate=candidate,
                    )
                )
                if accepted:
                    evaluated_candidates.append(evaluated)
                    accepted_candidates.append(evaluated)
                    continue
                rejected_candidates.append(
                    replace(
                        evaluated,
                        evidences=tuple(evaluated.evidences)
                        + (
                            EvidenceItem(
                                summary=rejection_reason or "候选未通过统一校验",
                                observed_range=evaluated.original_range,
                                expected_range=evaluated.target_range,
                            ),
                        ),
                    )
                )

            if not accepted_candidates:
                continue

            target_ranges = {
                candidate.target_range for candidate in accepted_candidates
            }
            if len(target_ranges) > 1:
                target_descriptions = "、".join(
                    sorted(target_range.format() for target_range in target_ranges)
                )
                ambiguity_reason = (
                    f"保持原始范围 {original_range.format()}：相对集号 "
                    f"E{original_range.begin_episode:02d} 同时对应 "
                    f"{target_descriptions}，目标不唯一。"
                )
                ambiguous_candidates = {
                    id(candidate): replace(
                        candidate,
                        reasons=tuple(candidate.reasons) + (ambiguity_reason,),
                        evidences=tuple(candidate.evidences)
                        + (
                            EvidenceItem(
                                summary=ambiguity_reason,
                                observed_range=candidate.original_range,
                                expected_range=candidate.target_range,
                            ),
                        ),
                    )
                    for candidate in accepted_candidates
                }
                evaluated_candidates = [
                    ambiguous_candidates.get(id(candidate), candidate)
                    for candidate in evaluated_candidates
                ]
                logger.debug(
                    "%s [最终保持] 原因=%s", release_info.title, ambiguity_reason
                )
                return RangeAdjustmentDecision(
                    original_range=original_range,
                    final_range=original_range,
                    selected_candidate=None,
                    candidates=tuple(evaluated_candidates),
                    rejected_candidates=tuple(rejected_candidates),
                    reasons=(ambiguity_reason,),
                )

            selected = accepted_candidates[0]
            selected_rewrite = selected if selected.changed else None
            reason = self._build_accept_reason(
                selected,
                release_info=release_info,
                show_context=show_context,
            )
            logger.debug(
                "%s [最终%s] 原始=%s 目标=%s 原因=%s",
                release_info.title,
                "改写" if selected.changed else "保持",
                original_range.format(),
                selected.target_range.format(),
                reason,
            )
            return RangeAdjustmentDecision(
                original_range=original_range,
                final_range=selected.target_range,
                selected_candidate=selected_rewrite,
                candidates=tuple(evaluated_candidates),
                rejected_candidates=tuple(rejected_candidates),
                reasons=(reason,),
            )

        reason = self._build_fallback_reason(
            original_range=original_range,
            rejected_candidates=rejected_candidates,
            release_info=release_info,
            show_context=show_context,
        )
        logger.debug("%s [最终保持] 原因=%s", release_info.title, reason)
        return RangeAdjustmentDecision(
            original_range=original_range,
            final_range=original_range,
            selected_candidate=None,
            candidates=tuple(evaluated_candidates),
            rejected_candidates=tuple(rejected_candidates),
            reasons=(reason,),
        )

    def _evaluate_cascade_candidate(
        self,
        *,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidate: AdjustmentCandidate,
    ) -> tuple[AdjustmentCandidate, bool, str | None]:
        """
        使用统一硬约束评估级联候选。

        候选策略只负责提出目标范围，本方法负责判断目标是否能被当前上下文接受。

        :param release_info: 发布条目信息
        :param show_context: 剧集上下文
        :param candidate: 待评估候选
        :return: 评估后的候选、是否通过以及唯一决定性拒绝原因
        """
        rejection_reason = self._check_hard_constraints(
            candidate=candidate,
            show_context=show_context,
            release_info=release_info,
        )
        if rejection_reason is None:
            rejection_reason = self._check_cascade_conflicts(
                release_info=release_info,
                show_context=show_context,
                candidate=candidate,
            )
        reasons = (
            (rejection_reason,) if rejection_reason is not None else candidate.reasons
        )
        evaluated = replace(
            candidate,
            reasons=reasons,
        )
        return evaluated, rejection_reason is None, rejection_reason

    def _check_cascade_conflicts(
        self,
        *,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidate: AdjustmentCandidate,
    ) -> str | None:
        """
        检查规则级联中的不可接受冲突。

        播出窗口本身就是一种允许低位集号改季的证据，因此不再套用旧的低位集号禁止规则。

        :param release_info: 发布条目信息
        :param show_context: 剧集上下文
        :param candidate: 待评估候选
        :return: 唯一决定性冲突原因；没有冲突时返回 None
        """
        if not candidate.changed:
            return None

        if candidate.strategy == self._SEASON_BY_WINDOW_STRATEGY:
            for evidence in candidate.evidences:
                if (
                    evidence.summary == "目标集播出日期晚于资源发布时间"
                    and evidence.detail
                ):
                    return evidence.detail
            cycle = show_context.production_cycle_for_range(candidate.target_range)
            if (
                self._title_year_signal(release_info, cycle) < 0
                and self._release_date_signal(release_info, show_context, cycle) < 0
            ):
                return (
                    f"目标={candidate.target_range.format()} 的播出窗口与标题年份"
                    f"({release_info.year or '未知'})和发布时间"
                    f"({release_info.release_date.isoformat() if release_info.release_date else '未知'})均冲突"
                )
            return None

        blocked, reasons = self._check_contradictions(
            release_info,
            show_context,
            candidate,
        )
        return reasons[0] if blocked and reasons else None

    def _generate_candidates_for_strategy(
        self,
        strategy: str,
        release_info: ReleaseInfo,
        show_context: ShowContext,
    ) -> tuple[AdjustmentCandidate, ...]:
        """
        按级联顺序惰性生成单条策略的候选。

        播出窗口策略只有在前面的策略没有产生结果时才会执行，因此不会为已经确定的结果
        提前触发逐集信息查询。

        :param strategy: 待执行的策略标识
        :param release_info: 发布条目信息
        :param show_context: 剧集上下文
        :return: 当前策略生成的候选集合
        """
        original_range = release_info.parsed_range
        if original_range is None:
            return ()

        generators = {
            self._EXPLICIT_MAPPING_STRATEGY: self._generate_explicit_mapping_candidates,
            self._NORMALIZE_EPISODE_RANGE_STRATEGY: (
                self._generate_normalize_episode_range_candidates
            ),
            self._SEASON_BY_WINDOW_STRATEGY: self._generate_season_by_window_candidates,
            self._ABSOLUTE_EPISODE_STRATEGY: self._generate_absolute_episode_candidates,
            self._PRODUCTION_CYCLE_STRATEGY: self._generate_production_cycle_candidates,
            self._KEEP_ORIGINAL_STRATEGY: self._generate_keep_original_candidates,
        }
        generator = generators.get(strategy)
        if generator is None:
            return ()
        return generator(release_info, show_context, original_range)

    def _build_candidate(
        self,
        *,
        original_range: EpisodeRange,
        target_range: EpisodeRange,
        strategy: str,
        strategy_name: str,
        reason_summary: str,
        detail: str | None = None,
        allow_length_change: bool = False,
        requires_production_cycle: bool = False,
        degrade_historical_single_update: bool = False,
    ) -> AdjustmentCandidate:
        """按统一说明格式构建候选，避免生成器重复装配元数据。"""
        return AdjustmentCandidate(
            original_range=original_range,
            target_range=target_range,
            strategy=strategy,
            strategy_name=strategy_name,
            allow_length_change=allow_length_change,
            requires_production_cycle=requires_production_cycle,
            degrade_historical_single_update=degrade_historical_single_update,
            reasons=(reason_summary,),
            evidences=(
                EvidenceItem(
                    summary=reason_summary,
                    detail=detail,
                    observed_range=original_range,
                    expected_range=target_range,
                ),
            ),
        )

    def _normalize_episode_range_target(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
    ) -> EpisodeRange | None:
        """复现归一化候选目标，供生成与引擎内在证据评估共用。"""
        if not (episode_range := release_info.parsed_range):
            return None

        if episode_range.is_single:
            return episode_range

        if episode_range.is_same_season:
            begin_absolute_point = show_context.absolute_by_point(episode_range.begin)
            if (
                begin_absolute_point is not None
                and begin_absolute_point == episode_range.end_episode
            ):
                return EpisodeRange(
                    begin=episode_range.begin,
                    end=episode_range.begin,
                )
        return episode_range

    def _generate_keep_original_candidates(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        original_range: EpisodeRange,
    ) -> tuple[AdjustmentCandidate, ...]:
        """保留原始解析范围。"""
        return (
            self._build_candidate(
                original_range=original_range,
                target_range=original_range,
                strategy=self._KEEP_ORIGINAL_STRATEGY,
                strategy_name="保留原始范围",
                reason_summary="保留解析得到的原始范围",
            ),
        )

    def _generate_normalize_episode_range_candidates(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        original_range: EpisodeRange,
    ) -> tuple[AdjustmentCandidate, ...]:
        """根据剧集上下文与发布习惯收敛原始范围。"""
        normalized_range = self._normalize_episode_range_target(
            release_info, show_context
        )
        if normalized_range is None or normalized_range == original_range:
            return ()
        return (
            self._build_candidate(
                original_range=original_range,
                target_range=normalized_range,
                strategy=self._NORMALIZE_EPISODE_RANGE_STRATEGY,
                strategy_name="归一化集数范围",
                reason_summary="根据剧集上下文与发布习惯收敛原始范围",
                detail=f"原始={original_range.format()}, 归一化={normalized_range.format()}",
                allow_length_change=True,
            ),
        )

    def _generate_explicit_mapping_candidates(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        original_range: EpisodeRange,
    ) -> tuple[AdjustmentCandidate, ...]:
        """使用外部 TMDB 逐集映射生成候选。"""
        if not release_info.tmdb_mapping or original_range.intra_season_length is None:
            return ()

        candidates: list[AdjustmentCandidate] = []
        original_points = original_range.expand_original_points()
        if original_points:
            mapped_points = tuple(
                release_info.tmdb_mapping.get(point) for point in original_points
            )
            if all(point is not None for point in mapped_points):
                target_points = tuple(
                    point for point in mapped_points if point is not None
                )
                candidates.append(
                    self._build_candidate(
                        original_range=original_range,
                        target_range=EpisodeRange(
                            begin=target_points[0],
                            end=target_points[-1],
                        ),
                        strategy=self._EXPLICIT_MAPPING_STRATEGY,
                        strategy_name="显式逐集映射",
                        reason_summary="命中完整逐集映射",
                        detail=f"命中映射集数={len(mapped_points)}",
                    )
                )

        mapped_begin = release_info.tmdb_mapping.get(original_range.begin)
        if mapped_begin is not None:
            inferred_end = EpisodePoint(
                season=mapped_begin.season,
                episode=mapped_begin.episode + original_range.intra_season_length - 1,
            )
            candidates.append(
                self._build_candidate(
                    original_range=original_range,
                    target_range=EpisodeRange(begin=mapped_begin, end=inferred_end),
                    strategy=self._EXPLICIT_MAPPING_STRATEGY,
                    strategy_name="显式逐集映射",
                    reason_summary="仅命中起点映射, 按范围长度推导终点",
                    detail=f"范围长度={original_range.intra_season_length}",
                )
            )

        return tuple(candidates)

    def _generate_absolute_episode_candidates(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        original_range: EpisodeRange,
    ) -> tuple[AdjustmentCandidate, ...]:
        """将越过当前逻辑季上限的集号解释为全作累计集序。"""
        if original_range.intra_season_length is None:
            return ()

        season_episodes = show_context.season_episodes.get(
            original_range.begin_season, []
        )
        known_max_episode = max(season_episodes) if season_episodes else None
        if (
            known_max_episode is None
            or original_range.begin_episode <= known_max_episode
        ):
            return ()

        begin_absolute = original_range.begin_episode
        end_absolute = begin_absolute + original_range.intra_season_length - 1
        target_begin = show_context.absolute_to_point.get(begin_absolute)
        target_end = show_context.absolute_to_point.get(end_absolute)
        if target_begin is None or target_end is None:
            return ()

        return (
            self._build_candidate(
                original_range=original_range,
                target_range=EpisodeRange(begin=target_begin, end=target_end),
                strategy=self._ABSOLUTE_EPISODE_STRATEGY,
                strategy_name="累计集数定位",
                reason_summary="原始集号超过当前逻辑季已知范围, 按全作累计集数定位目标范围",
                detail=(
                    f"逻辑季={original_range.begin_season}, "
                    f"已知最大集={known_max_episode}, "
                    f"累计集窗口={begin_absolute}-{end_absolute}"
                ),
            ),
        )

    def _generate_production_cycle_candidates(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        original_range: EpisodeRange,
    ) -> tuple[AdjustmentCandidate, ...]:
        """按制作周期内相对集序生成候选。"""
        if original_range.intra_season_length is None:
            return ()

        original_points = show_context.expand_target_points(original_range)
        if original_points and all(
            show_context.contains_point(point) for point in original_points
        ):
            return ()

        begin_index = original_range.begin_episode
        end_index = begin_index + original_range.intra_season_length - 1
        candidates: list[AdjustmentCandidate] = []
        for cycle in show_context.production_cycles:
            if begin_index < 1 or end_index > len(cycle.points):
                continue
            target_points = cycle.points[begin_index - 1 : end_index]
            candidates.append(
                self._build_candidate(
                    original_range=original_range,
                    target_range=EpisodeRange(
                        begin=target_points[0],
                        end=target_points[-1],
                    ),
                    strategy=self._PRODUCTION_CYCLE_STRATEGY,
                    strategy_name="制作周期定位",
                    reason_summary=f"按制作周期 #{cycle.cycle_id} 的相对集序生成范围",
                    detail=(
                        f"周期={cycle.cycle_id}, reason={cycle.reason}, "
                        f"窗口={begin_index}-{end_index}"
                    ),
                    requires_production_cycle=True,
                    degrade_historical_single_update=True,
                )
            )
        return tuple(candidates)

    def _generate_season_by_window_candidates(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        original_range: EpisodeRange,
    ) -> tuple[AdjustmentCandidate, ...]:
        """当标题默认落入 S01 时，按发布时间反推目标季号。"""
        if (
            not self.assume_season_by_window
            or original_range.intra_season_length is None
            or not original_range.is_same_season
            or original_range.begin_season != 1
            or release_info.release_date is None
            or self.__looks_like_batch_release(original_range, release_info)
        ):
            return ()

        matched_cycle, matched_by_exact_window = self._find_cycle_by_release_date(
            release_info.release_date,
            show_context,
        )
        if matched_cycle is None or not matched_cycle.points:
            return ()

        target_season = matched_cycle.points[0].season
        if target_season == original_range.begin_season:
            return ()

        target_range = EpisodeRange(
            begin=EpisodePoint(target_season, original_range.begin_episode),
            end=EpisodePoint(target_season, original_range.end_episode),
        )
        target_points = show_context.expand_target_points(target_range)
        if not target_points or any(
            not show_context.contains_point(point) for point in target_points
        ):
            return ()

        if self.episode_loader is None:
            return ()

        matched_season_episodes = sorted(
            self.episode_loader(
                tmdbid=show_context.tmdbid,
                season=matched_cycle.cycle_id,
                episode_group=show_context.group_id,
            )
            or [],
            key=lambda episode: episode.episode_number,
        )
        if not matched_season_episodes:
            return ()

        window_conflict = self._find_window_episode_conflict(
            release_info=release_info,
            target_points=target_points,
            matched_cycle=matched_cycle,
            matched_season_episodes=matched_season_episodes,
        )

        match_detail = (
            f"周期={matched_cycle.cycle_id}, "
            f"窗口={matched_cycle.start_date.isoformat() if matched_cycle.start_date else '未知'}"
            f"~{matched_cycle.end_date.isoformat() if matched_cycle.end_date else '未知'}"
        )
        reason_summary = (
            "发布时间命中目标季播出窗口，按同集号推断季号"
            if matched_by_exact_window
            else "发布时间接近最新可用播出窗口，按同集号推断季号"
        )
        candidate = self._build_candidate(
            original_range=original_range,
            target_range=target_range,
            strategy=self._SEASON_BY_WINDOW_STRATEGY,
            strategy_name="按播出窗口推断季号",
            reason_summary=reason_summary,
            detail=match_detail,
            requires_production_cycle=True,
            degrade_historical_single_update=True,
        )
        if window_conflict is None:
            return (candidate,)
        return (
            replace(
                candidate,
                reasons=(window_conflict,),
                evidences=tuple(candidate.evidences)
                + (
                    EvidenceItem(
                        summary="目标集播出日期晚于资源发布时间",
                        detail=window_conflict,
                        observed_range=original_range,
                        expected_range=target_range,
                    ),
                ),
            ),
        )

    def _find_window_episode_conflict(
        self,
        *,
        release_info: ReleaseInfo,
        target_points: tuple[EpisodePoint, ...],
        matched_cycle: ProductionCycle,
        matched_season_episodes: list[object],
    ) -> str | None:
        """
        检查目标季的播出时间是否与资源发布日期发生明显冲突。

        逐集数据按真实集号查找，不使用宽限集数偏移数组索引；缺少播出日期时保留未知状态，
        不把无法验证误判为冲突。

        :param release_info: 发布条目信息
        :param target_points: 待验证的目标季集点
        :param matched_cycle: 已匹配的制作周期
        :param matched_season_episodes: 主程序返回的逐集信息
        :return: 发现目标集晚于资源发布日期时返回第一条冲突说明
        """
        release_date = release_info.release_date
        if release_date is None:
            return None

        episodes_by_number = {
            getattr(episode, "episode_number", None): episode
            for episode in matched_season_episodes
        }
        for point in target_points:
            if point.season != matched_cycle.cycle_id:
                continue

            episode_info = episodes_by_number.get(point.episode)
            if episode_info is None:
                continue

            air_date = _coerce_date(getattr(episode_info, "air_date", None))
            if air_date is None:
                continue

            elif air_date > release_date:
                return (
                    f"推断目标 {point.format()} 的播出日期 {air_date.isoformat()} "
                    f"晚于资源发布时间 {release_date.isoformat()}"
                )

        return None

    def _check_hard_constraints(
        self,
        *,
        candidate: AdjustmentCandidate,
        show_context: ShowContext,
        release_info: ReleaseInfo,
    ) -> str | None:
        """
        执行硬约束门控 - 快速剔除明显无效的候选

        :param candidate: 待评估候选
        :param show_context: 剧集上下文
        :param release_info: 发布信息
        :return: 第一条决定性拒绝原因；没有拒绝原因时返回 None
        """
        target_range = candidate.target_range

        # 基础合法性：季集号为正且范围顺序正确
        if target_range.begin_season < 1 or target_range.begin_episode < 1:
            return f"目标范围起点非法: 目标={target_range.format()}"
        if target_range.end_season < 1 or target_range.end_episode < 1:
            return f"目标范围终点非法: 目标={target_range.format()}"
        if target_range.is_reverse:
            return f"目标范围逆序: 目标={target_range.format()}"

        # 长度一致性：防止错误映射（如单集误映射为多集）
        if not candidate.allow_length_change:
            original_length = show_context.range_length(candidate.original_range)
            target_length = show_context.range_length(target_range)
            if original_length is None:
                original_length = self.__plain_range_length(candidate.original_range)
            if target_length is None:
                target_length = self.__plain_range_length(target_range)
            if original_length is None or target_length is None:
                return (
                    "无法可靠计算输入输出范围长度: "
                    f"原始={candidate.original_range.format()}, "
                    f"目标={target_range.format()}"
                )
            elif original_length != target_length:
                return (
                    "输入输出范围长度不一致: "
                    f"原始={candidate.original_range.format()}({original_length}集), "
                    f"目标={target_range.format()}({target_length}集)"
                )

        for point in show_context.expand_target_points(target_range):
            if show_context.contains_point(point):
                continue
            if show_context.is_latest_season_grace_point(point, self.grace_episodes):
                continue
            return (
                "目标范围包含不存在的季集点: "
                f"目标={target_range.format()}，缺失={point.format()}，"
                f"宽限集数={self.grace_episodes}"
            )

        # 连续性检查：防止跳跃式映射
        if not _range_is_absolute_contiguous(
            show_context,
            target_range,
            self.grace_episodes,
        ):
            begin_absolute = show_context.absolute_by_point(target_range.begin)
            end_absolute = show_context.absolute_by_point(target_range.end)
            if begin_absolute is not None and end_absolute is not None:
                missing_absolute = tuple(
                    absolute
                    for absolute in range(begin_absolute, end_absolute + 1)
                    if show_context.point_by_absolute(absolute) is None
                )
                missing_detail = (
                    f"，缺失累计集序={','.join(map(str, missing_absolute))}"
                    if missing_absolute
                    else ""
                )
                return (
                    "目标范围在累计集序上不连续: "
                    f"目标={target_range.format()}，累计窗口={begin_absolute}-{end_absolute}"
                    f"{missing_detail}"
                )
            return f"目标范围无法建立连续累计集序: 目标={target_range.format()}"

        cycle = show_context.production_cycle_for_range(target_range)

        # 制作周期边界检查
        if candidate.requires_production_cycle and cycle is None:
            return (
                "制作周期候选没有完整落入已知周期: "
                f"策略={candidate.strategy_name}，目标={target_range.format()}"
            )

        # 发布时间下限检查：资源发布日期不应显著早于目标周期开播日
        release_date = release_info.release_date
        if (
            release_date is not None
            and cycle is not None
            and cycle.start_date is not None
            and release_date
            < cycle.start_date - timedelta(days=self.grace_episodes * 7)
        ):
            return (
                "目标周期开播日晚于资源发布日期: "
                f"目标={target_range.format()}，发布日期={release_date.isoformat()}，"
                f"周期开始={cycle.start_date.isoformat()}，宽限天数={self.grace_episodes * 7}"
            )

        return None

    def _build_accept_reason(
        self,
        candidate: AdjustmentCandidate,
        *,
        release_info: ReleaseInfo,
        show_context: ShowContext,
    ) -> str:
        """
        为最终采用或保留原范围的决策生成一条主原因。

        :param candidate: 最终选中的候选
        :param release_info: 发布条目信息
        :param show_context: 剧集上下文
        :return: 包含策略、关键事实和最终范围的原因
        """
        original_range = candidate.original_range
        target_range = candidate.target_range
        if not candidate.changed:
            if show_context.is_contiguous_range(original_range):
                return (
                    f"保持原始范围 {original_range.format()}：原始范围已在当前上下文中"
                    "连续存在，未发现更强的改写依据。"
                )
            return (
                f"保持原始范围 {original_range.format()}：原始范围通过当前上下文校验，"
                "未发现更强的改写依据。"
            )

        if candidate.strategy == self._EXPLICIT_MAPPING_STRATEGY:
            is_complete = any(
                evidence.summary == "命中完整逐集映射"
                for evidence in candidate.evidences
            )
            mapping_fact = (
                "映射覆盖完整且目标范围连续"
                if is_complete
                else "命中起点映射并按原范围长度推导终点，目标范围连续"
            )
            return (
                f"按{'完整' if is_complete else '起点'}外部映射改写："
                f"{original_range.format()} 映射为 "
                f"{target_range.format()}，{mapping_fact}。"
            )

        if candidate.strategy == self._SEASON_BY_WINDOW_STRATEGY:
            cycle = show_context.production_cycle_for_range(target_range)
            season_label = f"S{target_range.begin_season:02d}"
            summary = candidate.reasons[0] if candidate.reasons else ""
            match_description = (
                f"命中 {season_label} 播出窗口"
                if summary.startswith("发布时间命中")
                else f"接近 {season_label} 最新可用播出窗口"
            )
            if cycle is not None and cycle.start_date is not None:
                match_description = (
                    f"发布时间 {release_info.release_date.isoformat()} "
                    f"{match_description}（始于 {cycle.start_date.isoformat()}）"
                )
            elif release_info.release_date is not None:
                match_description = f"发布时间 {release_info.release_date.isoformat()} {match_description}"
            return (
                f"按播出窗口改写：{match_description}，"
                f"{original_range.format()} 调整为 {target_range.format()}。"
            )

        if candidate.strategy == self._ABSOLUTE_EPISODE_STRATEGY:
            known_max_episode = show_context.known_max_episode_for_original(
                original_range.begin_season
            )
            if known_max_episode is not None:
                return (
                    f"按累计集数改写：原始 E{original_range.begin_episode:02d} 超过 "
                    f"S{original_range.begin_season:02d} 已知最大集数 "
                    f"E{known_max_episode:02d}，累计集序唯一定位为 "
                    f"{target_range.format()}。"
                )

        if candidate.strategy == self._PRODUCTION_CYCLE_STRATEGY:
            return (
                f"按制作周期改写：相对集号 E{original_range.begin_episode:02d} "
                f"唯一定位为 {target_range.format()}。"
            )

        if candidate.strategy == self._NORMALIZE_EPISODE_RANGE_STRATEGY:
            return (
                f"按范围归一化改写：根据剧集上下文将 {original_range.format()} "
                f"收敛为 {target_range.format()}。"
            )

        basis = candidate.reasons[0] if candidate.reasons else candidate.strategy_name
        return (
            f"按{candidate.strategy_name}改写：{basis}，"
            f"{original_range.format()} 调整为 {target_range.format()}。"
        )

    def _build_fallback_reason(
        self,
        *,
        original_range: EpisodeRange,
        rejected_candidates: list[AdjustmentCandidate],
        release_info: ReleaseInfo,
        show_context: ShowContext,
    ) -> str:
        """
        为保持原始范围的回退决策生成一条主原因。

        :param original_range: 原始解析范围
        :param rejected_candidates: 按策略顺序评估后被拒绝的候选
        :param release_info: 发布条目信息
        :param show_context: 剧集上下文
        :return: 概括原范围合理性、首个决定性冲突或证据不足的原因
        """
        for candidate in rejected_candidates:
            window_conflict = next(
                (
                    evidence.detail
                    for evidence in candidate.evidences
                    if evidence.summary == "目标集播出日期晚于资源发布时间"
                    and evidence.detail
                ),
                None,
            )
            if window_conflict is not None:
                return f"保持原始范围 {original_range.format()}：{window_conflict}。"

        if show_context.is_contiguous_range(original_range):
            return (
                f"保持原始范围 {original_range.format()}：原始范围已在当前上下文中"
                "连续存在，未发现更强的改写依据。"
            )

        if rejected_candidates:
            rejected = rejected_candidates[0]
            rejection_reason = (
                rejected.reasons[0] if rejected.reasons else "候选未通过统一校验"
            )
            return (
                f"保持原始范围 {original_range.format()}：候选目标 "
                f"{rejected.target_range.format()} 被拒绝，{rejection_reason}。"
            )

        facts: list[str] = []
        facts.append(
            "外部映射为空"
            if not release_info.tmdb_mapping
            else "外部映射未覆盖完整范围"
        )
        known_max_episode = show_context.known_max_episode_for_original(
            original_range.begin_season
        )
        if (
            known_max_episode is not None
            and original_range.begin_episode <= known_max_episode
        ):
            facts.append(
                f"原始集号 E{original_range.begin_episode:02d} 未超过 "
                f"S{original_range.begin_season:02d} 已知上限 E{known_max_episode:02d}"
            )
        elif known_max_episode is not None:
            facts.append(
                f"原始集号超过 S{original_range.begin_season:02d} 已知上限，"
                "但累计集序无法唯一定位"
            )
        elif self.assume_season_by_window and release_info.release_date is None:
            facts.append("缺少发布时间")

        return (
            f"保持原始范围 {original_range.format()}："
            f"{'，且'.join(facts)}，无法形成唯一改写目标。"
        )

    def _check_contradictions(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidate: AdjustmentCandidate,
    ) -> tuple[bool, list[str]]:
        """
        评估是否存在会阻断候选的明确冲突。

        级联决策不再通过轻重等级扣分；只有明确冲突才阻断候选，普通提示不参与裁决。

        :param release_info: 发布信息
        :param show_context: 剧集上下文
        :param candidate: 待评估候选
        :return: 是否阻断候选以及冲突原因
        """
        reasons: list[str] = []

        # 硬反证：标题年份和发布时间均与目标周期冲突
        cycle = show_context.production_cycle_for_range(candidate.target_range)
        title_signal = self._title_year_signal(release_info, cycle)
        release_signal = self._release_date_signal(release_info, show_context, cycle)
        if candidate.changed and title_signal < 0 and release_signal < 0:
            reasons.append("标题年份与发布时间均强冲突, 改写候选被反证封顶")

        # 硬反证：低位集号（≤3）在无显式映射时禁止重映射
        if (
            candidate.changed
            and candidate.strategy
            in {"absolute_episode", "production_cycle", "season_by_window"}
            and not show_context.contains_point(candidate.original_range.begin)
            and candidate.original_range.begin_episode <= 3
        ):
            reasons.append("缺失季低位集号在无显式映射时不允许强行重映射")

        if (
            candidate.changed
            and candidate.strategy == "season_by_window"
            and candidate.original_range.begin_episode <= 3
            and not release_info.tmdb_mapping
        ):
            reasons.append("低位集号在无显式映射时不允许仅凭播出窗口改季")

        exceed_blocked, exceed_reasons = self._evaluate_episode_exceed_contradiction(
            release_info, show_context, candidate
        )
        reasons.extend(exceed_reasons)

        if reasons or exceed_blocked:
            return True, reasons
        return False, []

    def _evaluate_episode_exceed_contradiction(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidate: AdjustmentCandidate,
    ) -> tuple[bool, list[str]]:
        """
        评估候选集数是否触发明确的播出冲突。

        最新季宽限区内的滞后只允许通过，不产生阻断状态。超出宽限区并且已经能够确认
        作品进度时，候选会被阻断。

        :param release_info: 发布信息
        :param show_context: 剧集上下文
        :param candidate: 待评估候选
        :return: 是否阻断候选以及冲突原因
        """
        reasons: list[str] = []

        # 只针对同季范围进行检查
        if not candidate.target_range.is_same_season:
            return False, reasons

        target_season = candidate.target_range.begin_season
        target_end_episode = candidate.target_range.end_episode

        # 获取该季已知最大集号
        known_max_episode = show_context.known_max_episode_for_original(target_season)
        if known_max_episode is None:
            return False, reasons

        # 计算超出幅度
        exceed_amount = target_end_episode - known_max_episode
        if exceed_amount <= 0:
            return False, reasons

        # 检查是否在宽限范围内
        is_in_grace = show_context.is_latest_season_grace_point(
            candidate.target_range.end,
            self.grace_episodes,
        )

        # 宽限范围内不视为反证（容忍连载滞后）
        if is_in_grace:
            return False, reasons

        # 检查作品是否已完结
        is_finalized = show_context.count_finalized

        if is_finalized:
            reasons.append(
                f"作品已完结，但候选结束集号E{target_end_episode}超出已知最大集号E{known_max_episode}，"
                f"超出{exceed_amount}集"
            )
            return True, reasons

        # 连载中的作品，轻微超出仍交给最新季宽限规则处理。
        if exceed_amount <= self.grace_episodes * 2:
            return False, reasons

        reasons.append(
            f"候选结束集号E{target_end_episode}大幅超出已知最大集号E{known_max_episode}，"
            f"超出{exceed_amount}集远超宽限范围(grace={self.grace_episodes})"
        )

        # 结合发布时间进一步判断合理性
        if (
            release_info.release_date is not None
            and show_context.last_air_date is not None
        ):
            days_diff = (release_info.release_date - show_context.last_air_date).days
            if days_diff < 0:
                reasons.append(
                    f"资源发布日期({release_info.release_date.isoformat()})早于最后播出日期"
                    f"({show_context.last_air_date.isoformat()})，但集号却更高，存在时间矛盾"
                )
            elif days_diff < 7 and exceed_amount > 1:
                reasons.append(
                    f"距离最后播出仅{days_diff}天，但候选超出{exceed_amount}集，时间上不合理"
                )

        return True, reasons

    @staticmethod
    def __looks_like_batch_release(
        episode_range: EpisodeRange,
        release_info: ReleaseInfo,
    ) -> bool:
        """
        判断是否像合集/批量发布 - 用于区分单集更新和历史周期映射场景
        """
        title = release_info.title.lower()
        if any(
            marker in title
            for marker in (
                "complete",
                "batch",
                "合集",
                "全集",
                "bluray",
                "bd",
            )
        ):
            return True

        # 范围长度≥6、跨季、或从E01开始且长度≥3
        plain_length = RangeDecisionEngine.__plain_range_length(episode_range) or 1
        return (
            plain_length >= 6 or episode_range.begin_season != episode_range.end_season
        )

    @staticmethod
    def __plain_range_length(episode_range: EpisodeRange) -> int | None:
        """
        在缺少 absolute 上下文时保守计算范围长度

        :param episode_range: 待计算范围
        :return: 可计算时返回长度, 否则返回 None
        """
        if not episode_range.is_same_season:
            return None
        return episode_range.end_episode - episode_range.begin_episode + 1

    @staticmethod
    def _title_year_signal(
        release_info: ReleaseInfo,
        cycle: ProductionCycle | None,
    ) -> int:
        """
        比较标题年份与候选目标周期年份

        :param release_info: 发布信息
        :param cycle: 目标周期
        :return: 匹配返回 1, 冲突返回 -1, 中性返回 0
        """
        if release_info.year is None or cycle is None or cycle.start_date is None:
            return 0
        return 1 if release_info.year == cycle.start_date.year else -1

    @staticmethod
    def _release_date_signal(
        release_info: ReleaseInfo,
        show_context: ShowContext,
        cycle: ProductionCycle | None,
    ) -> int:
        """
        比较发布时间与候选目标周期窗口
        - 优先看是否命中目标周期时间窗口
        - 若已知发布时间对应的最新可用周期, 也允许其与目标周期一致视为匹配

        :param release_info: 发布信息
        :param show_context: 剧集上下文
        :param cycle: 目标周期
        :return: 匹配返回 1, 冲突返回 -1, 中性返回 0
        """
        release_date = release_info.release_date
        if release_date is None or cycle is None:
            return 0
        if cycle.contains_date(release_date):
            return 1

        latest_cycle = show_context.latest_available_cycle(release_date)
        if latest_cycle is None:
            return 0
        return 1 if latest_cycle.cycle_id == cycle.cycle_id else -1

    @staticmethod
    def _find_cycle_by_release_date(
        release_date: date,
        show_context: ShowContext,
    ) -> tuple[ProductionCycle | None, bool]:
        """
        根据发布时间定位最可信的播出周期。

        先尝试精确命中窗口；若没有，再回退到发布时间下的最新可用周期。
        """
        for cycle in show_context.production_cycles:
            if cycle.contains_date(release_date):
                return cycle, True
        return show_context.latest_available_cycle(release_date), False


class MetaCorrectionUseCase:
    """
    组装元数据修正所需事实并调用范围决策引擎。

    本用例按需提供逐集 TMDB 查询，具体缓存仍由主程序的 TMDB 链负责。
    """

    def __init__(
        self,
        *,
        grace_episodes: int = 3,
        assume_season_by_window: bool = False,
        decision_engine: RangeDecisionEngine | None = None,
    ) -> None:
        self.decision_engine = decision_engine or RangeDecisionEngine(
            grace_episodes=grace_episodes,
            assume_season_by_window=assume_season_by_window,
            episode_loader=self._load_tmdb_episodes,
        )
        self.grace_episodes = grace_episodes
        self.assume_season_by_window = assume_season_by_window

    def correct(
        self,
        *,
        meta: MetaBase,
        mediainfo: MediaInfo,
        tmdb_mapping: dict[tuple[int, int], tuple[int, int]],
        publish_date: date | datetime | str | None = None,
        source: str | None = None,
    ) -> RangeAdjustmentDecision:
        """
        执行元数据修正用例并返回范围决策结果
        """
        release_info = self._build_release_info(
            meta=meta,
            tmdb_mapping=tmdb_mapping,
            publish_date=publish_date,
            source=source,
        )
        if release_info is None:
            raise ValueError("缺少可用的季集范围信息")

        season_episodes = {
            season: episodes
            for season, episodes in mediainfo.seasons.items()
            if season > 0
        }
        existing_points = [
            EpisodePoint(season=season, episode=episode)
            for season in sorted(season_episodes)
            for episode in season_episodes[season]
        ]
        point_to_absolute = {
            point: idx for idx, point in enumerate(existing_points, start=1)
        }
        absolute_to_point = {idx: point for point, idx in point_to_absolute.items()}

        def parse_tmdb_episode(raw: object) -> tuple[EpisodePoint | None, date | None]:
            if not isinstance(raw, dict):
                return None, None
            season = raw.get("season_number")
            episode = raw.get("episode_number")
            point = None
            if (
                isinstance(season, int)
                and isinstance(episode, int)
                and season >= 1
                and episode >= 1
            ):
                point = EpisodePoint(season=season, episode=episode)
            air_date = _coerce_date(raw.get("air_date"))
            return point, air_date

        tmdb_info = mediainfo.tmdb_info if isinstance(mediainfo.tmdb_info, dict) else {}
        last_episode, last_air_date = parse_tmdb_episode(
            tmdb_info.get("last_episode_to_air")
        )
        next_episode, next_air_date = parse_tmdb_episode(mediainfo.next_episode_to_air)

        production_cycles: list[ProductionCycle] = []
        season_info = sorted(
            mediainfo.season_info, key=lambda item: item.get("season_number", 0)
        )
        for info_index, info in enumerate(season_info):
            season = info.get("season_number")
            if not isinstance(season, int) or season < 1:
                continue

            absolutes = sorted(
                (
                    (absolute, point)
                    for point, absolute in point_to_absolute.items()
                    if point.season == season
                ),
                key=lambda item: item[0],
            )
            if not absolutes:
                continue

            air_date = _coerce_date(info.get("air_date"))
            next_season_start = (
                _coerce_date(season_info[info_index + 1].get("air_date"))
                if info_index + 1 < len(season_info)
                else None
            )
            if next_season_start is not None and (
                air_date is None or next_season_start > air_date
            ):
                end_air_date = next_season_start - timedelta(days=1)
            else:
                end_air_date = next_air_date or last_air_date or air_date
            production_cycles.append(
                ProductionCycle(
                    cycle_id=season,
                    start_absolute=absolutes[0][0],
                    end_absolute=absolutes[-1][0],
                    points=tuple(point for _, point in absolutes),
                    reason="按 TMDB season 分段",
                    start_date=air_date,
                    end_date=end_air_date,
                )
            )

        show_context = ShowContext(
            tmdbid=mediainfo.tmdb_id,
            group_id=mediainfo.episode_group,
            existing_points=frozenset(existing_points),
            season_episodes=season_episodes,
            point_to_absolute=point_to_absolute,
            absolute_to_point=absolute_to_point,
            production_cycles=tuple(production_cycles),
            last_episode=last_episode,
            next_episode=next_episode,
            last_air_date=last_air_date,
            next_air_date=next_air_date,
            count_finalized=self.count_finalized_resolver(mediainfo),
        )

        return self.decision_engine.decide(
            release_info=release_info,
            show_context=show_context,
        )

    @staticmethod
    def _load_tmdb_episodes(
        *,
        tmdbid: int,
        season: int,
        episode_group: int,
    ) -> list[object] | None:
        """
        按需加载指定季的逐集信息。

        查询由主程序 TMDB 链负责，主程序现有缓存仍是唯一缓存层；本方法不预加载或复制缓存。

        :param tmdbid: TMDB 作品标识
        :param season: 需要查询的季号
        :param episode_group: TMDB 集数分组标识
        :return: 主程序返回的逐集信息；查询失败或无数据时返回 None
        """
        from app.chain.tmdb import TmdbChain

        return TmdbChain().tmdb_episodes(
            tmdbid=tmdbid,
            season=season,
            episode_group=episode_group,
        )

    def _build_release_info(
        self,
        *,
        meta: MetaBase,
        tmdb_mapping: dict[tuple[int, int], tuple[int, int]],
        publish_date: date | datetime | str | None,
        source: str | None,
    ) -> ReleaseInfo | None:
        """构建决策所需的发布信息对象"""
        episode_range = EpisodeRange.from_meta_fields(
            seasons=meta.season_list,
            episodes=meta.episode_list,
        )
        if episode_range is None:
            return None

        return ReleaseInfo(
            title=meta.title,
            year=int(meta.year) if meta.year else None,
            parsed_range=episode_range,
            publish_date=self._normalize_publish_date(publish_date),
            source=source,
            tmdb_mapping=self._normalize_mapping(tmdb_mapping),
        )

    @staticmethod
    def _normalize_mapping(
        tmdb_mapping: dict[tuple[int, int], tuple[int, int]],
    ) -> dict[EpisodePoint, EpisodePoint]:
        """将 tuple 映射转换为范围引擎使用的点映射"""
        normalized: dict[EpisodePoint, EpisodePoint] = {}
        for source_point, target_point in tmdb_mapping.items():
            try:
                normalized[EpisodePoint(*source_point)] = EpisodePoint(*target_point)
            except (TypeError, ValueError):
                continue
        return normalized

    @staticmethod
    def _normalize_publish_date(
        value: date | datetime | str | None,
    ) -> date | None:
        """规范化发布时间"""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if not isinstance(value, str):
            return None

        raw = value.strip()
        if not raw:
            return None

        for fmt in (
            "%Y-%m-%d",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
        ):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue

        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            return None

    @staticmethod
    def count_finalized_resolver(mediainfo: MediaInfo) -> bool:
        """
        判断目标作品总集数是否已最终确定

        :param mediainfo: 媒体信息对象
        :return: 已最终确定时返回 True
        """
        # 如果剧集状态为 Ended 或 Canceled，则认为已完结
        if mediainfo.status in ["Ended", "Canceled"]:
            return True

        finale_type = {"finale", "mid_season"}

        if mediainfo.next_episode_to_air.get("episode_type") in finale_type:
            return True

        from app.chain.tmdb import TmdbChain

        episodes = TmdbChain().tmdb_episodes(
            mediainfo.tmdb_id,
            season=mediainfo.number_of_seasons,
            episode_group=mediainfo.episode_group,
        )
        if not episodes:
            return False

        # 判断是否最终集
        return episodes[-1].episode_type in finale_type
