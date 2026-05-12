from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

from app.chain.tmdb import TmdbChain
from app.core.context import MediaInfo
from app.core.meta import MetaBase
from app.log import logger

from .models import (
    AdjustmentCandidate,
    ContextMatchLevel,
    ContextScoreCard,
    ContradictionLevel,
    DecisionRank,
    EpisodePoint,
    EpisodeRange,
    EvidenceItem,
    EvidenceLevel,
    PenaltyScoreCard,
    ProductionCycle,
    RangeAdjustmentDecision,
    ReleaseInfo,
    ShowContext,
    StrategyScoreCard,
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


def _range_looks_legal_in_context(
    context: ShowContext,
    episode_range: EpisodeRange,
) -> bool:
    """
    判断原样范围在当前上下文中是否看起来合法
    """
    target_points = context.expand_target_points(episode_range)
    return bool(target_points) and all(
        context.contains_point(point) for point in target_points
    )


class RangeDecisionEngine:
    _KEEP_ORIGINAL_STRATEGY = "keep_original"
    _NORMALIZE_EPISODE_RANGE_STRATEGY = "normalize_episode_range"
    _EXPLICIT_MAPPING_STRATEGY = "explicit_mapping"
    _ABSOLUTE_EPISODE_STRATEGY = "absolute_episode"
    _PRODUCTION_CYCLE_STRATEGY = "production_cycle"

    def __init__(
        self,
        grace_episodes: int = 3,
        rewrite_threshold: int = 16,
    ) -> None:
        self.grace_episodes = grace_episodes
        self.rewrite_threshold = rewrite_threshold

    def decide(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidates: list[AdjustmentCandidate] | None = None,
    ) -> RangeAdjustmentDecision:
        original_range = release_info.parsed_range
        if original_range is None:
            raise ValueError("release_info.parsed_range 不能为空")

        raw_candidates = candidates or self._generate_candidates(
            release_info, show_context
        )
        logger.debug(
            "%s [决策开始] 原始候选数=%s",
            release_info.title,
            len(raw_candidates),
        )

        # 评估所有候选（硬约束 + 多层评分）
        evaluated_candidates: list[AdjustmentCandidate] = []
        rejected_candidates: list[AdjustmentCandidate] = []
        states: dict[int, dict[str, object]] = {}

        for candidate in raw_candidates:
            evaluated_candidate, state = self._evaluate_candidate(
                release_info=release_info,
                show_context=show_context,
                candidate=candidate,
            )
            states[id(evaluated_candidate)] = state
            if state["feasible"]:
                evaluated_candidates.append(evaluated_candidate)
            else:
                rejected_candidates.append(evaluated_candidate)

        # 所有候选被拒绝 → 回退原样
        if not evaluated_candidates:
            return RangeAdjustmentDecision(
                original_range=original_range,
                final_range=original_range,
                selected_candidate=None,
                candidates=tuple(raw_candidates),
                rejected_candidates=tuple(rejected_candidates),
                reasons=("所有候选均未通过硬约束门控, 回退原始范围",),
            )

        # 定位或构造原样候选作为比较基准
        original_candidate = self._locate_original_candidate(
            evaluated_candidates,
            original_range,
        )
        if original_candidate is None:
            original_candidate, original_state = self._build_virtual_original_candidate(
                release_info=release_info,
                show_context=show_context,
                original_range=original_range,
            )
            states[id(original_candidate)] = original_state

        # 最终选择、排序和边际都只读取同一个总分，避免多套强弱公式冲突
        scored_candidates = self._apply_margin_against_original(
            candidates=evaluated_candidates,
            original_candidate=original_candidate,
            states=states,
        )
        scored_candidates.sort(
            key=lambda candidate: self._sort_key(candidate, states),
            reverse=True,
        )

        logger.debug(
            "%s [候选排序] %s",
            release_info.title,
            " | ".join(
                (
                    f"#{idx + 1} 策略={candidate.strategy} "
                    f"目标={candidate.target_range.format()} "
                    f"等级={candidate.decision_rank.name} "
                    f"总分={states[id(candidate)]['decision_score']} "
                    f"上下文分={states[id(candidate)]['context_score']} "
                    f"策略分={states[id(candidate)]['strategy_score']} "
                    f"惩罚={states[id(candidate)]['penalty_score']} "
                    f"边际={states[id(candidate)]['margin_against_original']}"
                )
                for idx, candidate in enumerate(scored_candidates[:5])
            ),
        )

        # 根据原样合法性和改写边际做出最终选择
        selected_candidate, reasons = self._select_candidate(
            candidates=scored_candidates,
            original_candidate=original_candidate,
            states=states,
        )

        selected_rewrite = (
            None
            if selected_candidate is None or not selected_candidate.changed
            else selected_candidate
        )
        final_candidate = selected_candidate or original_candidate

        return RangeAdjustmentDecision(
            original_range=original_range,
            final_range=final_candidate.target_range,
            selected_candidate=selected_rewrite,
            candidates=tuple(scored_candidates),
            rejected_candidates=tuple(rejected_candidates),
            reasons=tuple(reasons),
        )

    def _generate_candidates(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
    ) -> list[AdjustmentCandidate]:
        original_range = release_info.parsed_range
        if original_range is None:
            return []

        generators = (
            self._generate_keep_original_candidates,
            self._generate_normalize_episode_range_candidates,
            self._generate_explicit_mapping_candidates,
            self._generate_absolute_episode_candidates,
            self._generate_production_cycle_candidates,
        )
        deduped: dict[tuple[str, str], AdjustmentCandidate] = {}
        for generator in generators:
            for candidate in generator(release_info, show_context, original_range):
                dedupe_key = (candidate.target_range.format(), candidate.strategy)
                if dedupe_key not in deduped:
                    deduped[dedupe_key] = candidate

        return list(deduped.values())

    def _build_candidate(
        self,
        *,
        original_range: EpisodeRange,
        target_range: EpisodeRange,
        strategy: str,
        strategy_name: str,
        prior_rank: DecisionRank,
        evidence_level: EvidenceLevel,
        reason_summary: str,
        detail: str | None = None,
        allow_length_change: bool = False,
        requires_production_cycle: bool = False,
        degrade_historical_single_update: bool = False,
        intrinsic_evidence_kind: str | None = None,
    ) -> AdjustmentCandidate:
        """按统一证据格式构建候选，避免生成器重复装配元数据。"""
        return AdjustmentCandidate(
            original_range=original_range,
            target_range=target_range,
            strategy=strategy,
            strategy_name=strategy_name,
            prior_rank=prior_rank,
            allow_length_change=allow_length_change,
            requires_production_cycle=requires_production_cycle,
            degrade_historical_single_update=degrade_historical_single_update,
            intrinsic_evidence_kind=intrinsic_evidence_kind,
            reasons=(reason_summary,),
            evidences=(
                EvidenceItem(
                    code=f"range.{strategy}",
                    summary=reason_summary,
                    level=evidence_level,
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
                logger.debug(
                    "[%s] 生成归一化候选: %s(连续集号:%d)=%s，目标单集=%s",
                    release_info.title,
                    episode_range.begin.format(),
                    begin_absolute_point,
                    episode_range.end.format(),
                    episode_range.begin.format(),
                )
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
                prior_rank=DecisionRank.MEDIUM,
                evidence_level=EvidenceLevel.LOW,
                reason_summary="保留解析得到的原始范围",
                intrinsic_evidence_kind="keep_original",
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
                prior_rank=DecisionRank.MEDIUM,
                evidence_level=EvidenceLevel.HIGH,
                reason_summary="根据剧集上下文与发布习惯收敛原始范围",
                detail=f"原始={original_range.format()}, 归一化={normalized_range.format()}",
                allow_length_change=True,
                intrinsic_evidence_kind="normalize_episode_range",
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
                        prior_rank=DecisionRank.STRONG,
                        evidence_level=EvidenceLevel.CRITICAL,
                        reason_summary="命中完整逐集映射",
                        detail=f"命中映射集数={len(mapped_points)}",
                        intrinsic_evidence_kind="explicit_mapping",
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
                    prior_rank=DecisionRank.STRONG,
                    evidence_level=EvidenceLevel.HIGH,
                    reason_summary="仅命中起点映射, 按范围长度推导终点",
                    detail=f"范围长度={original_range.intra_season_length}",
                    intrinsic_evidence_kind="explicit_mapping",
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
                prior_rank=DecisionRank.MEDIUM,
                evidence_level=EvidenceLevel.HIGH,
                reason_summary="原始集号超过当前逻辑季已知范围, 按全作累计集数定位目标范围",
                detail=(
                    f"逻辑季={original_range.begin_season}, "
                    f"已知最大集={known_max_episode}, "
                    f"累计集窗口={begin_absolute}-{end_absolute}"
                ),
                intrinsic_evidence_kind="absolute_episode",
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
                    prior_rank=DecisionRank.WEAK,
                    evidence_level=EvidenceLevel.MEDIUM,
                    reason_summary=f"按制作周期 #{cycle.cycle_id} 的相对集序生成范围",
                    detail=(
                        f"周期={cycle.cycle_id}, reason={cycle.reason}, "
                        f"窗口={begin_index}-{end_index}"
                    ),
                    requires_production_cycle=True,
                    degrade_historical_single_update=True,
                    intrinsic_evidence_kind="production_cycle",
                )
            )
        return tuple(candidates)

    def _evaluate_candidate(
        self,
        *,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidate: AdjustmentCandidate,
    ) -> tuple[AdjustmentCandidate, dict[str, object]]:
        """对单个候选执行门控与离散评估。"""
        candidate_label = (
            f"策略={candidate.strategy}({candidate.strategy_display_name}) "
            f"原始={candidate.original_range.format()} 目标={candidate.target_range.format()}"
        )
        rejection_reasons = self._check_hard_constraints(
            candidate=candidate,
            show_context=show_context,
            release_info=release_info,
        )
        if rejection_reasons:
            logger.debug(
                "%s [候选评估] %s 硬约束拒绝: %s",
                release_info.title,
                candidate_label,
                "；".join(rejection_reasons),
            )
            rejection_evidences = tuple(
                EvidenceItem(
                    code="hard_constraint.reject",
                    summary=reason,
                    level=EvidenceLevel.CRITICAL,
                    observed_range=candidate.original_range,
                    expected_range=candidate.target_range,
                )
                for reason in rejection_reasons
            )
            return (
                replace(
                    candidate,
                    evidences=tuple(candidate.evidences) + rejection_evidences,
                    decision_rank=DecisionRank.REJECTED,
                ),
                {
                    "feasible": False,
                    "context_level": ContextMatchLevel.STRONG_CONFLICT,
                    "contradiction_level": ContradictionLevel.HARD,
                    "blocked": True,
                    "decision_score": 0,
                    "context_score": 0,
                    "strategy_score": 0,
                    "prior_score": 0,
                    "penalty_score": 0,
                    "coverage_total": 0,
                    "coverage_hits": 0,
                    "coverage_grace": 0,
                    "coverage_ratio": 0.0,
                    "prior_rank": DecisionRank.REJECTED,
                    "intrinsic_rank": DecisionRank.REJECTED,
                    "margin_against_original": 0,
                },
            )

        prior_rank, prior_reasons = self._evaluate_prior(candidate)
        logger.debug(
            "%s [候选评估] %s prior=%s details=%s",
            release_info.title,
            candidate_label,
            prior_rank.name,
            "；".join(prior_reasons),
        )
        context_card = self._evaluate_common_context(
            release_info,
            show_context,
            candidate,
        )
        logger.debug(
            "%s [候选评估] %s context=%s score=%s coverage=%s/%s(%.1f%%) grace=%s continuous=%s ambiguous=%s details=%s",
            release_info.title,
            candidate_label,
            context_card.level.name,
            context_card.score,
            context_card.coverage_hits,
            context_card.coverage_total,
            context_card.coverage_ratio * 100,
            context_card.coverage_grace,
            context_card.coverage_contiguous,
            context_card.coverage_ambiguous,
            "；".join(context_card.reasons),
        )
        intrinsic_rank, intrinsic_reasons = self._evaluate_intrinsic_evidence(
            release_info,
            show_context,
            candidate,
        )
        strategy_card = self._build_strategy_score_card(
            intrinsic_rank,
            intrinsic_reasons,
        )
        logger.debug(
            "%s [候选评估] %s intrinsic=%s score=%s details=%s",
            release_info.title,
            candidate_label,
            strategy_card.rank.name,
            strategy_card.score,
            "；".join(strategy_card.reasons),
        )
        contradiction_level, contradiction_reasons, blocked = (
            self._evaluate_contradictions(
                release_info,
                show_context,
                candidate,
            )
        )
        penalty_card = self._build_penalty_score_card(
            contradiction_level,
            contradiction_reasons,
            blocked,
        )
        logger.debug(
            "%s [候选评估] %s contradiction=%s penalty=%s blocked=%s details=%s",
            release_info.title,
            candidate_label,
            penalty_card.level.name,
            penalty_card.score,
            penalty_card.blocked,
            "；".join(penalty_card.reasons),
        )
        decision_rank, decision_score = self._compose_decision_rank(
            candidate=candidate,
            prior_rank=prior_rank,
            context_card=context_card,
            strategy_card=strategy_card,
            penalty_card=penalty_card,
        )
        logger.debug(
            "%s [候选评估] %s final rank=%s score=%s context=%s strategy=%s prior=%s penalty=%s blocked=%s feasible=%s",
            release_info.title,
            candidate_label,
            decision_rank.name,
            decision_score,
            context_card.score,
            strategy_card.score,
            self._prior_score(prior_rank),
            penalty_card.score,
            penalty_card.blocked,
            True,
        )

        evaluated = replace(
            candidate,
            evidences=tuple(candidate.evidences)
            + self._build_score_card_evidences(
                candidate=candidate,
                context_card=context_card,
                penalty_card=penalty_card,
                decision_rank=decision_rank,
            ),
            decision_rank=decision_rank,
        )
        return (
            evaluated,
            {
                "feasible": True,
                "context_level": context_card.level,
                "contradiction_level": penalty_card.level,
                "blocked": penalty_card.blocked,
                "decision_score": decision_score,
                "context_score": context_card.score,
                "strategy_score": strategy_card.score,
                "prior_score": self._prior_score(prior_rank),
                "penalty_score": penalty_card.score,
                "coverage_total": context_card.coverage_total,
                "coverage_hits": context_card.coverage_hits,
                "coverage_grace": context_card.coverage_grace,
                "coverage_ratio": context_card.coverage_ratio,
                "prior_rank": prior_rank,
                "intrinsic_rank": strategy_card.rank,
                "margin_against_original": 0,
            },
        )

    def _check_hard_constraints(
        self,
        *,
        candidate: AdjustmentCandidate,
        show_context: ShowContext,
        release_info: ReleaseInfo,
    ) -> list[str]:
        """
        执行硬约束门控 - 快速剔除明显无效的候选

        :param candidate: 待评估候选
        :param show_context: 剧集上下文
        :param release_info: 发布信息
        :return: 拒绝原因列表
        """
        reasons: list[str] = []
        target_range = candidate.target_range

        # 基础合法性：季集号为正且范围顺序正确
        if target_range.begin_season < 1 or target_range.begin_episode < 1:
            reasons.append("目标范围起点非法")
        if target_range.end_season < 1 or target_range.end_episode < 1:
            reasons.append("目标范围终点非法")
        if target_range.is_reverse:
            reasons.append("目标范围逆序")

        # 长度一致性：防止错误映射（如单集误映射为多集）
        if not candidate.allow_length_change:
            original_length = show_context.range_length(candidate.original_range)
            target_length = show_context.range_length(target_range)
            if original_length is None:
                original_length = self.__plain_range_length(candidate.original_range)
            if target_length is None:
                target_length = self.__plain_range_length(target_range)
            if original_length is None or target_length is None:
                reasons.append("无法可靠计算输入输出范围长度")
            elif original_length != target_length:
                reasons.append(
                    f"输入输出范围长度不一致: 原长度={original_length}, 目标长度={target_length}"
                )

        for point in show_context.expand_target_points(target_range):
            if show_context.contains_point(point):
                continue
            if show_context.is_latest_season_grace_point(point, self.grace_episodes):
                continue
            reasons.append(f"目标范围包含不存在的季集点: {point.format()}")
            break

        # 连续性检查：防止跳跃式映射
        if not _range_is_absolute_contiguous(
            show_context,
            target_range,
            self.grace_episodes,
        ):
            reasons.append("目标范围在累计集序上不连续")

        cycle = show_context.production_cycle_for_range(target_range)

        # 发布时间下限检查：资源发布日期不应显著早于目标周期开播日
        release_date = release_info.release_date
        if (
            release_date is not None
            and cycle is not None
            and cycle.start_date is not None
            and release_date
            < cycle.start_date - timedelta(days=self.grace_episodes * 7)
        ):
            reasons.append(
                "目标周期开播日晚于资源发布日期: "
                f"发布日期={release_date.isoformat()}, 周期开始={cycle.start_date.isoformat()}"
            )

        # 制作周期边界检查
        if candidate.requires_production_cycle and cycle is None:
            reasons.append("制作周期候选超出周期边界")

        return reasons

    def _evaluate_prior(
        self,
        candidate: AdjustmentCandidate,
    ) -> tuple[DecisionRank, list[str]]:
        """
        评估候选来源先验

        :param candidate: 待评估候选
        :return: `(prior_rank, reasons)`
        """
        prior_rank = candidate.prior_rank
        return prior_rank, [f"候选来源先验={candidate.strategy}:{prior_rank.name}"]

    def _evaluate_common_context(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidate: AdjustmentCandidate,
    ) -> ContextScoreCard:
        """
        评估上下文事实 - 时间、周期和范围覆盖共同决定候选是否符合当前作品事实

        :param release_info: 发布信息
        :param show_context: 剧集上下文
        :param candidate: 待评估候选
        :return: 上下文事实评分卡
        """
        reasons: list[str] = []
        cycle = show_context.production_cycle_for_range(candidate.target_range)
        score = 0

        if cycle is None:
            reasons.append("目标范围无可用制作周期信息")
            score -= 4
        else:
            # 周期是作品事实的一部分：完整落在同一周期比单纯日期匹配更可靠。
            reasons.append(f"目标范围完整落在制作周期: cycle={cycle.cycle_id}")
            score += 6

        # 标题年份匹配
        year_signal = self._title_year_signal(release_info, cycle)
        if year_signal > 0:
            reasons.append("标题年份与目标周期起始年份匹配")
            score += 8
        elif year_signal < 0:
            reasons.append("标题年份与目标周期起始年份冲突")
            score -= 10
        else:
            reasons.append("标题年份缺失或不足以判断")

        # 发布时间窗口匹配
        release_signal = self._release_date_signal(release_info, show_context, cycle)
        if release_signal > 0:
            reasons.append("发布时间与目标周期窗口匹配")
            score += 8
        elif release_signal < 0:
            reasons.append("发布时间与目标周期窗口冲突")
            score -= 10
        else:
            reasons.append("发布时间缺失或不足以判断")

        coverage_score, coverage_reasons, coverage_stats = (
            self._evaluate_coverage_context(
                release_info,
                show_context,
                candidate,
            )
        )
        score += coverage_score
        reasons.extend(coverage_reasons)

        return ContextScoreCard(
            level=self._context_level_from_score(score),
            score=score,
            reasons=tuple(reasons),
            coverage_total=coverage_stats["total"],
            coverage_hits=coverage_stats["hits"],
            coverage_grace=coverage_stats["grace"],
            coverage_ratio=coverage_stats["ratio"],
            coverage_contiguous=coverage_stats["contiguous"],
            coverage_ambiguous=coverage_stats["ambiguous"],
        )

    def _evaluate_coverage_context(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidate: AdjustmentCandidate,
    ) -> tuple[int, list[str], dict[str, object]]:
        """
        评估范围覆盖质量。

        覆盖分属于上下文事实层，因为它验证的是候选目标能否被 TMDB
        当前作品事实解释，而不是某个策略自身的来源可信度。
        """
        target_points = show_context.expand_target_points(candidate.target_range)
        total = len(target_points)
        hit_points = tuple(
            point for point in target_points if show_context.contains_point(point)
        )
        grace_points = tuple(
            point
            for point in target_points
            if not show_context.contains_point(point)
            and show_context.is_latest_season_grace_point(point, self.grace_episodes)
        )
        missing_count = total - len(hit_points) - len(grace_points)
        ratio = len(hit_points) / total if total else 0.0
        contiguous = _range_is_absolute_contiguous(
            show_context,
            candidate.target_range,
            self.grace_episodes,
        )
        ambiguous = self._has_absolute_episode_range_ambiguity(
            release_info,
            show_context,
            candidate,
        )

        reasons: list[str] = [
            (
                "覆盖统计: "
                f"命中={len(hit_points)}/{total}, "
                f"命中率={ratio:.1%}, 宽限缺失={len(grace_points)}, "
                f"非宽限缺失={missing_count}, 连续={contiguous}"
            )
        ]

        if total == 0:
            return (
                -12,
                reasons + ["目标范围无法展开, 覆盖事实不足"],
                {
                    "total": total,
                    "hits": len(hit_points),
                    "grace": len(grace_points),
                    "ratio": ratio,
                    "contiguous": contiguous,
                    "ambiguous": ambiguous,
                },
            )

        # 覆盖数量只奖励已真实存在的点；宽限点只表示暂不拒绝，不能制造强证据。
        hit_count = len(hit_points)
        if hit_count >= 13:
            count_score = 30
            reasons.append("覆盖数量达到 13 集以上完整命中分段, 应强于单集解释")
        elif hit_count >= 6:
            count_score = 22
            reasons.append("覆盖数量达到 6-12 集命中分段")
        elif hit_count >= 2:
            count_score = 14
            reasons.append("覆盖数量达到 2-5 集命中分段")
        elif hit_count == 1:
            count_score = 8
            reasons.append("单集正确落点, 给予基础正向覆盖分")
        else:
            count_score = -8
            reasons.append("没有真实命中点, 覆盖事实不足")

        if ratio == 1:
            ratio_score = 8
            reasons.append("覆盖比例 100%, 完整命中")
        elif ratio >= 0.9 and missing_count == 0:
            ratio_score = 2
            reasons.append("覆盖比例 >=90% 且缺失都在最新季宽限区")
        elif ratio >= 0.7:
            ratio_score = -4
            reasons.append("覆盖比例 70%-90%, 覆盖事实不完整")
        else:
            ratio_score = -14
            reasons.append("覆盖比例低于 70%, 覆盖事实明显不足")

        contiguous_score = 4 if contiguous else -12
        if contiguous:
            reasons.append("目标范围在累计集序上连续")
        else:
            reasons.append("目标范围在累计集序上不连续")

        coverage_score = count_score + ratio_score + contiguous_score

        if grace_points:
            coverage_score -= min(len(grace_points) * 2, 6)
            reasons.append("宽限区缺失只允许通过, 不计入强覆盖奖励")

        if missing_count > 0:
            coverage_score -= 20
            reasons.append("存在非宽限缺失点, 覆盖分强扣减")

        if (
            ambiguous
            and not candidate.changed
            and candidate.target_range.begin_episode != 1
        ):
            # 05(77) 类结构通常不是从 E01 开始；完整季度包也可能满足同样的
            # 累计序号等式，因此只限制非首集起始的原样范围，避免误伤季度合集。
            coverage_score = min(coverage_score, 10)
            reasons.append("命中逻辑集号+累计编号结构, 原样覆盖奖励封顶")

        return (
            coverage_score,
            reasons,
            {
                "total": total,
                "hits": hit_count,
                "grace": len(grace_points),
                "ratio": ratio,
                "contiguous": contiguous,
                "ambiguous": ambiguous,
            },
        )

    def _evaluate_intrinsic_evidence(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidate: AdjustmentCandidate,
    ) -> tuple[DecisionRank, list[str]]:
        """
        评估策略独有证据 - 针对不同候选类型的特定验证逻辑

        :param release_info: 发布信息
        :param show_context: 剧集上下文
        :param candidate: 待评估候选
        :return: `(intrinsic_rank, reasons)`
        """
        evaluators = {
            "keep_original": self._evaluate_keep_original_intrinsic_evidence,
            "normalize_episode_range": (
                self._evaluate_normalize_episode_range_intrinsic_evidence
            ),
            "explicit_mapping": self._evaluate_explicit_mapping_intrinsic_evidence,
            "absolute_episode": self._evaluate_absolute_episode_intrinsic_evidence,
            "production_cycle": self._evaluate_production_cycle_intrinsic_evidence,
        }
        evaluator = evaluators.get(candidate.intrinsic_evidence_kind or "")
        if evaluator is None:
            return DecisionRank.FALLBACK, ["未知候选缺少策略独有证据"]
        return evaluator(release_info, show_context, candidate)

    def _evaluate_keep_original_intrinsic_evidence(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidate: AdjustmentCandidate,
    ) -> tuple[DecisionRank, list[str]]:
        """评估原样候选的内在证据。"""
        if _range_looks_legal_in_context(show_context, candidate.target_range):
            return DecisionRank.MEDIUM, ["原样候选命中已知合法范围"]
        return DecisionRank.FALLBACK, ["原样候选缺少已知合法性支撑"]

    def _evaluate_normalize_episode_range_intrinsic_evidence(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidate: AdjustmentCandidate,
    ) -> tuple[DecisionRank, list[str]]:
        """评估归一化候选的内在证据。"""
        normalized_range = self._normalize_episode_range_target(
            release_info, show_context
        )
        if normalized_range == candidate.target_range:
            return DecisionRank.STRONG, ["归一化规则可复现, 原始范围可收敛为目标单集"]
        return DecisionRank.WEAK, ["归一化候选未能被当前上下文规则复现"]

    def _evaluate_explicit_mapping_intrinsic_evidence(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidate: AdjustmentCandidate,
    ) -> tuple[DecisionRank, list[str]]:
        """评估显式映射候选的内在证据。"""
        mapping_points = self._count_explicit_mapping_points(candidate, release_info)
        if mapping_points >= 2:
            return DecisionRank.VERY_STRONG, [
                f"显式映射完整覆盖目标范围, 命中点数={mapping_points}"
            ]
        if mapping_points == 1:
            return DecisionRank.MEDIUM, ["显式映射仅命中起点, 终点按长度推导"]
        return DecisionRank.WEAK, ["显式映射证据不足"]

    def _evaluate_absolute_episode_intrinsic_evidence(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidate: AdjustmentCandidate,
    ) -> tuple[DecisionRank, list[str]]:
        """评估累计集数候选的内在证据。"""
        known_max_episode = show_context.known_max_episode_for_original(
            candidate.original_range.begin_season
        )
        if (
            known_max_episode is not None
            and candidate.original_range.begin_episode > known_max_episode
        ):
            return DecisionRank.STRONG, [
                f"原始集号越过当前逻辑季上限, 触发累计集数解释: 上限={known_max_episode}"
            ]
        return DecisionRank.WEAK, ["累计集数候选缺少明显越界触发"]

    def _evaluate_production_cycle_intrinsic_evidence(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidate: AdjustmentCandidate,
    ) -> tuple[DecisionRank, list[str]]:
        """评估制作周期候选的内在证据。"""
        cycle = show_context.production_cycle_for_range(candidate.target_range)
        if cycle is not None:
            return DecisionRank.MEDIUM, [
                f"目标范围命中制作周期窗口: cycle={cycle.cycle_id}"
            ]
        return DecisionRank.WEAK, ["制作周期候选未命中有效周期窗口"]

    def _evaluate_contradictions(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidate: AdjustmentCandidate,
    ) -> tuple[ContradictionLevel, list[str], bool]:
        """
        评估反证 - 识别与已知上下文的严重冲突

        :param release_info: 发布信息
        :param show_context: 剧集上下文
        :param candidate: 待评估候选
        :return: `(contradiction_level, reasons, blocked)`
        """
        reasons: list[str] = []
        blocked = False
        level = ContradictionLevel.NONE

        # 硬反证：标题年份和发布时间均与目标周期冲突
        cycle = show_context.production_cycle_for_range(candidate.target_range)
        title_signal = self._title_year_signal(release_info, cycle)
        release_signal = self._release_date_signal(release_info, show_context, cycle)
        if candidate.changed and title_signal < 0 and release_signal < 0:
            level = ContradictionLevel.HARD
            blocked = True
            reasons.append("标题年份与发布时间均强冲突, 改写候选被反证封顶")

        # 硬反证：低位集号（≤3）在无显式映射时禁止重映射
        if (
            candidate.changed
            and candidate.intrinsic_evidence_kind
            in {"absolute_episode", "production_cycle"}
            and not show_context.contains_point(candidate.original_range.begin)
            and candidate.original_range.begin_episode <= 3
        ):
            level = max(level, ContradictionLevel.HARD)
            blocked = True
            reasons.append("缺失季低位集号在无显式映射时不允许强行重映射")

        # 软反证：单集更新不应映射到历史周期（除非是合集）
        if (
            candidate.degrade_historical_single_update
            and cycle is not None
            and release_info.release_date is not None
        ):
            latest_cycle = show_context.latest_available_cycle(
                release_info.release_date
            )
            if (
                latest_cycle is not None
                and cycle.cycle_id < latest_cycle.cycle_id
                and not self.__looks_like_batch_release(
                    candidate.original_range, release_info
                )
            ):
                level = max(level, ContradictionLevel.SOFT)
                reasons.append("资源更像当前更新而非历史合集, 历史周期候选被降级")

        if not reasons:
            reasons.append("未发现额外反证")
        return level, reasons, blocked

    def _compose_decision_rank(
        self,
        *,
        candidate: AdjustmentCandidate,
        prior_rank: DecisionRank,
        context_card: ContextScoreCard,
        strategy_card: StrategyScoreCard,
        penalty_card: PenaltyScoreCard,
    ) -> tuple[DecisionRank, int]:
        """
        组合四层评估结果形成统一总分

        :param candidate: 候选对象
        :param prior_rank: 来源先验等级
        :param context_card: 上下文事实评分卡
        :param strategy_card: 策略解释评分卡
        :param penalty_card: 反证惩罚评分卡
        :return: `(decision_rank, decision_score)`
        """
        # 来源先验只做小权重微调；真正主导胜负的是事实分和策略解释分
        keep_original_bonus = 6 if not candidate.changed else 0
        decision_score = (
            context_card.score
            + strategy_card.score
            + self._prior_score(prior_rank)
            + keep_original_bonus
            + penalty_card.score
        )

        # 强反证不再另起一套排序公式，而是通过统一总分降级并由 blocked 控制改写
        if penalty_card.blocked and candidate.changed:
            decision_score = min(decision_score, 20)

        # 根据总分划分离散等级
        if decision_score >= 95:
            rank = DecisionRank.VERY_STRONG
        elif decision_score >= 72:
            rank = DecisionRank.STRONG
        elif decision_score >= 48:
            rank = DecisionRank.MEDIUM
        elif decision_score >= 26:
            rank = DecisionRank.WEAK
        elif decision_score > 0:
            rank = DecisionRank.FALLBACK
        else:
            rank = DecisionRank.REJECTED
        return rank, decision_score

    @staticmethod
    def _prior_score(prior_rank: DecisionRank) -> int:
        """
        先验只表示策略来源的默认可信度，不能压过上下文事实中的覆盖质量
        """
        return int(prior_rank) * 2

    @staticmethod
    def _build_strategy_score_card(
        intrinsic_rank: DecisionRank,
        intrinsic_reasons: list[str],
    ) -> StrategyScoreCard:
        """把策略解释等级转换为统一总分中的策略分。"""
        return StrategyScoreCard(
            rank=intrinsic_rank,
            score=int(intrinsic_rank) * 7,
            reasons=tuple(intrinsic_reasons),
        )

    @staticmethod
    def _build_penalty_score_card(
        contradiction_level: ContradictionLevel,
        contradiction_reasons: list[str],
        blocked: bool,
    ) -> PenaltyScoreCard:
        """把软硬反证转换为统一总分惩罚。"""
        penalty_score = {
            ContradictionLevel.NONE: 0,
            ContradictionLevel.SOFT: -12,
            ContradictionLevel.HARD: -34,
        }[contradiction_level]
        return PenaltyScoreCard(
            level=contradiction_level,
            score=penalty_score,
            reasons=tuple(contradiction_reasons),
            blocked=blocked,
        )

    @staticmethod
    def _context_level_from_score(score: int) -> ContextMatchLevel:
        """将上下文事实细分分兼容映射回旧的离散等级。"""
        if score >= 40:
            return ContextMatchLevel.STRONG_MATCH
        if score >= 18:
            return ContextMatchLevel.MATCH
        if score <= -24:
            return ContextMatchLevel.STRONG_CONFLICT
        if score <= -8:
            return ContextMatchLevel.CONFLICT
        return ContextMatchLevel.NEUTRAL

    def _has_absolute_episode_range_ambiguity(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidate: AdjustmentCandidate,
    ) -> bool:
        """
        判断是否命中“逻辑集号 + 累计编号”的结构歧义

        该判断只依赖季集结构，不读取标题关键词
        误当成长范围合集来保护。
        """
        original_range = release_info.parsed_range
        if original_range is None or not original_range.is_same_season:
            return False
        begin_absolute = show_context.absolute_by_point(original_range.begin)
        if begin_absolute is None or begin_absolute != original_range.end_episode:
            return False
        normalized_range = self._normalize_episode_range_target(
            release_info,
            show_context,
        )
        return bool(
            normalized_range is not None
            and normalized_range.is_single
            and normalized_range.begin == original_range.begin
            and candidate.target_range == original_range
        )

    @staticmethod
    def _count_explicit_mapping_points(
        candidate: AdjustmentCandidate,
        release_info: ReleaseInfo,
    ) -> int:
        """
        统计显式映射命中点数 - 用于评估映射策略的证据强度
        """
        mapping = release_info.tmdb_mapping
        if (
            not mapping
            or not candidate.original_range.is_same_season
            or not candidate.target_range.is_same_season
        ):
            return 0

        # 收集原始范围中每一集的映射点
        mapped_points = []
        for episode in range(
            candidate.original_range.begin_episode,
            candidate.original_range.end_episode + 1,
        ):
            original_point = EpisodePoint(
                candidate.original_range.begin_season, episode
            )
            mapped_point = mapping.get(original_point)
            if mapped_point is not None:
                mapped_points.append(mapped_point)

        if not mapped_points:
            return 0

        # 统计落在目标范围内的映射点数量
        target_points = {
            EpisodePoint(candidate.target_range.begin_season, episode)
            for episode in range(
                candidate.target_range.begin_episode,
                candidate.target_range.end_episode + 1,
            )
        }
        return sum(1 for point in mapped_points if point in target_points)

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
                "fin",
                "final",
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
    def _rank_strength(
        candidate: AdjustmentCandidate,
        states: dict[int, dict[str, object]],
    ) -> int:
        """计算候选强度用于边际比较：直接使用统一总分。"""
        return int(states[id(candidate)]["decision_score"])

    def _build_score_card_evidences(
        self,
        *,
        candidate: AdjustmentCandidate,
        context_card: ContextScoreCard,
        penalty_card: PenaltyScoreCard,
        decision_rank: DecisionRank,
    ) -> tuple[EvidenceItem, ...]:
        """将离散评估结果补充为可读证据。"""
        evidences = [
            EvidenceItem(
                code="decision.context",
                summary=(
                    f"上下文事实等级={context_card.level.name}, "
                    f"分数={context_card.score}, "
                    f"覆盖={context_card.coverage_hits}/{context_card.coverage_total}, "
                    f"命中率={context_card.coverage_ratio:.1%}"
                ),
                level=EvidenceLevel.MEDIUM,
                observed_range=candidate.original_range,
                expected_range=candidate.target_range,
            ),
            EvidenceItem(
                code="decision.rank",
                summary=f"最终离散决策等级={decision_rank.name}",
                level=EvidenceLevel.MEDIUM,
                observed_range=candidate.original_range,
                expected_range=candidate.target_range,
            ),
        ]
        if penalty_card.level != ContradictionLevel.NONE:
            evidences.append(
                EvidenceItem(
                    code="decision.contradiction",
                    summary=f"反证等级={penalty_card.level.name}, 惩罚={penalty_card.score}",
                    level=EvidenceLevel.HIGH,
                    observed_range=candidate.original_range,
                    expected_range=candidate.target_range,
                )
            )
        if penalty_card.blocked:
            evidences.append(
                EvidenceItem(
                    code="decision.blocked",
                    summary="候选被强反证阻止改写",
                    level=EvidenceLevel.CRITICAL,
                    observed_range=candidate.original_range,
                    expected_range=candidate.target_range,
                )
            )
        return tuple(evidences)

    def _locate_original_candidate(
        self,
        candidates: list[AdjustmentCandidate],
        original_range: EpisodeRange,
    ) -> AdjustmentCandidate | None:
        """查找通过门控的原样候选。"""
        for candidate in candidates:
            if candidate.target_range == original_range:
                return candidate
        return None

    def _build_virtual_original_candidate(
        self,
        *,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        original_range: EpisodeRange,
    ) -> tuple[AdjustmentCandidate, dict[str, object]]:
        """当原样候选未显式生成时，构造一个仅用于比较边际的原样候选。"""
        return self._evaluate_candidate(
            release_info=release_info,
            show_context=show_context,
            candidate=self._build_candidate(
                original_range=original_range,
                target_range=original_range,
                strategy=self._KEEP_ORIGINAL_STRATEGY,
                strategy_name="保留原始范围",
                prior_rank=DecisionRank.MEDIUM,
                evidence_level=EvidenceLevel.LOW,
                reason_summary="虚拟原样候选，仅用于边际比较",
                intrinsic_evidence_kind="keep_original",
            ),
        )

    def _apply_margin_against_original(
        self,
        *,
        candidates: list[AdjustmentCandidate],
        original_candidate: AdjustmentCandidate,
        states: dict[int, dict[str, object]],
    ) -> list[AdjustmentCandidate]:
        """计算每个候选相对原样的胜出边际。"""
        original_strength = self._rank_strength(original_candidate, states)
        for candidate in candidates:
            states[id(candidate)]["margin_against_original"] = (
                self._rank_strength(candidate, states) - original_strength
            )
        return candidates

    def _select_candidate(
        self,
        *,
        candidates: list[AdjustmentCandidate],
        original_candidate: AdjustmentCandidate,
        states: dict[int, dict[str, object]],
    ) -> tuple[AdjustmentCandidate | None, list[str]]:
        """最终决策：根据原样合法性和改写边际选择候选。"""
        reasons = []

        original_is_legal = bool(
            states[id(original_candidate)]["feasible"]
            and not states[id(original_candidate)]["blocked"]
        )
        best_candidate = candidates[0]

        if not original_is_legal:
            reasons.append(
                (
                    "原样范围未通过最终采用条件，按排序采用最佳可行候选；"
                    f"策略={best_candidate.strategy}，"
                    f"目标={best_candidate.target_range.format()}，"
                    f"等级={best_candidate.decision_rank.name}"
                )
            )
            return best_candidate, reasons

        best_rewrite = next(
            (
                candidate
                for candidate in candidates
                if candidate.changed and not states[id(candidate)]["blocked"]
            ),
            None,
        )
        if best_rewrite is None:
            reasons.append(
                "原样范围通过最终采用条件，且不存在可采用的改写候选，保持原样"
            )
            return original_candidate, reasons

        if states[id(best_rewrite)]["margin_against_original"] < self.rewrite_threshold:
            reasons.append(
                (
                    "原样范围通过最终采用条件，最佳改写候选未达到改写边际阈值，保持原样；"
                    f"要求总分优势>={self.rewrite_threshold}，"
                    f"实际={states[id(best_rewrite)]['margin_against_original']}"
                )
            )
            return original_candidate, reasons

        reasons.append(
            (
                "原样范围通过最终采用条件，但最佳改写候选形成明确胜出边际，采用改写结果；"
                f"策略={best_rewrite.strategy}，"
                f"目标={best_rewrite.target_range.format()}，"
                f"等级={best_rewrite.decision_rank.name}，"
                f"边际={states[id(best_rewrite)]['margin_against_original']}"
            )
        )
        return best_rewrite, reasons

    def _sort_key(
        self,
        candidate: AdjustmentCandidate,
        states: dict[int, dict[str, object]],
    ) -> tuple[int, int, int, int, int, int]:
        """排序优先级：可采用性优先，其余统一围绕总分和少量平局项。"""
        state = states[id(candidate)]
        return (
            0 if state["blocked"] and candidate.changed else 1,
            int(state["decision_score"]),
            int(candidate.decision_rank),
            1 if candidate.strategy == self._EXPLICIT_MAPPING_STRATEGY else 0,
            1 if not candidate.changed else 0,
            int(state["coverage_hits"]),
        )

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


class MetaCorrectionUseCase:
    """元数据修正应用层用例"""

    def __init__(
        self,
        *,
        grace_episodes: int = 3,
        rewrite_threshold: int = 16,
        decision_engine: RangeDecisionEngine | None = None,
    ) -> None:
        self.decision_engine = decision_engine or RangeDecisionEngine(
            grace_episodes=grace_episodes,
            rewrite_threshold=rewrite_threshold,
        )
        self.grace_episodes = grace_episodes
        self.rewrite_threshold = rewrite_threshold

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

        logger.debug(
            "%s 输入范围=%s 发布时间=%s 来源=%s 映射数=%s",
            release_info.title,
            release_info.parsed_range.format(),
            release_info.release_date,
            release_info.source or "未知",
            len(release_info.tmdb_mapping),
        )

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
            air_date = (
                datetime.strptime(d, "%Y-%m-%d").date()
                if (d := raw.get("air_date"))
                else None
            )
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
        for idx, info in enumerate(season_info, start=1):
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

            air_date = (
                datetime.strptime(d, "%Y-%m-%d").date()
                if (d := info.get("air_date"))
                else None
            )
            end_air_date = (
                (
                    datetime.strptime(d, "%Y-%m-%d").date()
                    if (d := season_info[idx].get("air_date"))
                    else next_air_date or last_air_date
                )
                if idx < len(season_info)
                else next_air_date or last_air_date
            )
            production_cycles.append(
                ProductionCycle(
                    cycle_id=idx,
                    start_absolute=absolutes[0][0],
                    end_absolute=absolutes[-1][0],
                    points=tuple(point for _, point in absolutes),
                    reason="按 TMDB season 分段",
                    start_date=air_date,
                    end_date=end_air_date,
                )
            )

        show_context = ShowContext(
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
        if mediainfo.status in ("Ended", "Canceled"):
            return True

        tmdb_info = mediainfo.tmdb_info if isinstance(mediainfo.tmdb_info, dict) else {}
        episodes = TmdbChain().tmdb_episodes(
            mediainfo.tmdb_id,
            season=mediainfo.number_of_seasons,
            episode_group=mediainfo.episode_group,
        )
        last_episode = tmdb_info.get("last_episode_to_air") or {}

        if not last_episode and not episodes:
            return False

        is_last_finale = last_episode.get("episode_type") in ("finale", "mid_season")
        is_ep_list_finale = (
            episodes[-1].episode_type in ("finale", "mid_season") if episodes else False
        )
        return is_last_finale or is_ep_list_finale
