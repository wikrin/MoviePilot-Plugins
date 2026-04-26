from datetime import date, datetime, timedelta

from app.chain.tmdb import TmdbChain
from app.log import logger
from app.core.context import MediaInfo
from app.core.meta import MetaBase

from .models import (
    AdjustmentCandidate,
    CandidateSourceKind,
    ContextMatchLevel,
    ContradictionLevel,
    DecisionRank,
    EpisodePoint,
    EpisodeRange,
    EvidenceItem,
    EvidenceLevel,
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
    def __init__(
        self,
        grace_episodes: int = 3,
        rewrite_margin: int = 1,
    ) -> None:
        self.grace_episodes = grace_episodes
        self.rewrite_margin = rewrite_margin

    def decide(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidates: list[AdjustmentCandidate] | None = None,
    ) -> RangeAdjustmentDecision:
        original_range = release_info.parsed_range

        parsed_range = self._normalize_episode_range(release_info, show_context)
        if parsed_range is None or original_range is None:
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
                final_range=parsed_range,
                selected_candidate=None,
                candidates=tuple(raw_candidates),
                rejected_candidates=tuple(rejected_candidates),
                reasons=("所有候选均未通过硬约束门控, 回退原始范围",),
            )

        # 定位或构造原样候选作为比较基准
        original_candidate = self._locate_original_candidate(
            evaluated_candidates,
            parsed_range,
        )
        if original_candidate is None:
            original_candidate, original_state = self._build_virtual_original_candidate(
                release_info=release_info,
                show_context=show_context,
                original_range=parsed_range,
            )
            states[id(original_candidate)] = original_state

        # 计算相对原样的胜出边际并排序
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
                    f"上下文={states[id(candidate)]['context_level'].name} "
                    f"反证={states[id(candidate)]['contradiction_level'].name} "
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

        deduped: dict[tuple[str, str], AdjustmentCandidate] = {}

        def add_candidate(
            strategy: str,
            target_range: EpisodeRange,
            reason_summary: str,
            evidence_level: EvidenceLevel,
            detail: str | None = None,
        ) -> None:
            candidate = AdjustmentCandidate(
                original_range=original_range,
                target_range=target_range,
                strategy=strategy,
                source_kind=CandidateSourceKind(strategy),
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
            dedupe_key = (candidate.target_range.format(), strategy)
            if dedupe_key not in deduped:
                deduped[dedupe_key] = candidate

        add_candidate(
            "keep_original",
            original_range,
            "保留解析得到的原始范围",
            EvidenceLevel.LOW,
        )

        if release_info.tmdb_mapping and original_range.intra_season_length is not None:
            original_points = original_range.expand_original_points()
            if original_points:
                mapped_points = [
                    release_info.tmdb_mapping.get(point) for point in original_points
                ]
                if all(mapped_points):
                    add_candidate(
                        "explicit_mapping",
                        EpisodeRange(begin=mapped_points[0], end=mapped_points[-1]),
                        "命中完整逐集映射",
                        EvidenceLevel.CRITICAL,
                        f"命中映射集数={len(mapped_points)}",
                    )

            mapped_begin = release_info.tmdb_mapping.get(original_range.begin)
            if mapped_begin is not None:
                inferred_end = EpisodePoint(
                    season=mapped_begin.season,
                    episode=mapped_begin.episode
                    + original_range.intra_season_length
                    - 1,
                )
                add_candidate(
                    "explicit_mapping",
                    EpisodeRange(begin=mapped_begin, end=inferred_end),
                    "仅命中起点映射, 按范围长度推导终点",
                    EvidenceLevel.HIGH,
                    f"范围长度={original_range.intra_season_length}",
                )

        if original_range.intra_season_length is None:
            return list(deduped.values())

        season_episodes = show_context.season_episodes.get(
            original_range.begin_season, []
        )
        known_max_episode = max(season_episodes) if season_episodes else None
        if (
            known_max_episode is not None
            and original_range.begin_episode > known_max_episode
        ):
            begin_absolute = original_range.begin_episode
            end_absolute = begin_absolute + original_range.intra_season_length - 1
            target_begin = show_context.absolute_to_point.get(begin_absolute)
            target_end = show_context.absolute_to_point.get(end_absolute)
            if target_begin is not None and target_end is not None:
                add_candidate(
                    "absolute_episode",
                    EpisodeRange(begin=target_begin, end=target_end),
                    "原始集号超过当前逻辑季已知范围, 按全作累计集数定位目标范围",
                    EvidenceLevel.HIGH,
                    (
                        f"逻辑季={original_range.begin_season}, "
                        f"已知最大集={known_max_episode}, "
                        f"累计集窗口={begin_absolute}-{end_absolute}"
                    ),
                )

        original_points = show_context.expand_target_points(original_range)
        if original_points and all(
            show_context.contains_point(point) for point in original_points
        ):
            return list(deduped.values())

        begin_index = original_range.begin_episode
        end_index = begin_index + original_range.intra_season_length - 1
        for cycle in show_context.production_cycles:
            if begin_index < 1 or end_index > len(cycle.points):
                continue
            target_points = cycle.points[begin_index - 1 : end_index]
            add_candidate(
                "production_cycle",
                EpisodeRange(begin=target_points[0], end=target_points[-1]),
                f"按制作周期 #{cycle.cycle_id} 的相对集序生成范围",
                EvidenceLevel.MEDIUM,
                f"周期={cycle.cycle_id}, reason={cycle.reason}, 窗口={begin_index}-{end_index}",
            )

        return list(deduped.values())

    def _evaluate_candidate(
        self,
        *,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidate: AdjustmentCandidate,
    ) -> tuple[AdjustmentCandidate, dict[str, object]]:
        """对单个候选执行门控与离散评估。"""
        candidate_label = (
            f"策略={candidate.strategy} 来源={candidate.source_kind.value} "
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
                AdjustmentCandidate(
                    original_range=candidate.original_range,
                    target_range=candidate.target_range,
                    strategy=candidate.strategy,
                    source_kind=candidate.source_kind,
                    reasons=tuple(candidate.reasons),
                    evidences=tuple(candidate.evidences) + rejection_evidences,
                    decision_rank=DecisionRank.REJECTED,
                ),
                {
                    "feasible": False,
                    "context_level": ContextMatchLevel.STRONG_CONFLICT,
                    "contradiction_level": ContradictionLevel.HARD,
                    "blocked": True,
                    "decision_score": 0,
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
        context_level, context_reasons = self._evaluate_common_context(
            release_info,
            show_context,
            candidate,
        )
        logger.debug(
            "%s [候选评估] %s context=%s details=%s",
            release_info.title,
            candidate_label,
            context_level.name,
            "；".join(context_reasons),
        )
        intrinsic_rank, intrinsic_reasons = self._evaluate_intrinsic_evidence(
            release_info,
            show_context,
            candidate,
        )
        logger.debug(
            "%s [候选评估] %s intrinsic=%s details=%s",
            release_info.title,
            candidate_label,
            intrinsic_rank.name,
            "；".join(intrinsic_reasons),
        )
        contradiction_level, contradiction_reasons, blocked = (
            self._evaluate_contradictions(
                release_info,
                show_context,
                candidate,
            )
        )
        logger.debug(
            "%s [候选评估] %s contradiction=%s blocked=%s details=%s",
            release_info.title,
            candidate_label,
            contradiction_level.name,
            blocked,
            "；".join(contradiction_reasons),
        )
        decision_rank, decision_score = self._compose_decision_rank(
            candidate=candidate,
            prior_rank=prior_rank,
            context_level=context_level,
            intrinsic_rank=intrinsic_rank,
            contradiction_level=contradiction_level,
            blocked_by_contradiction=blocked,
        )
        logger.debug(
            "%s [候选评估] %s final rank=%s score=%s blocked=%s feasible=%s",
            release_info.title,
            candidate_label,
            decision_rank.name,
            decision_score,
            blocked,
            True,
        )

        evaluated = AdjustmentCandidate(
            original_range=candidate.original_range,
            target_range=candidate.target_range,
            strategy=candidate.strategy,
            source_kind=candidate.source_kind,
            reasons=tuple(candidate.reasons),
            evidences=tuple(candidate.evidences)
            + self._build_score_card_evidences(
                candidate=candidate,
                context_level=context_level,
                contradiction_level=contradiction_level,
                blocked_by_contradiction=blocked,
                decision_rank=decision_rank,
            ),
            decision_rank=decision_rank,
        )
        return (
            evaluated,
            {
                "feasible": True,
                "context_level": context_level,
                "contradiction_level": contradiction_level,
                "blocked": blocked,
                "decision_score": decision_score,
                "prior_rank": prior_rank,
                "intrinsic_rank": intrinsic_rank,
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
        if (
            candidate.source_kind == CandidateSourceKind.PRODUCTION_CYCLE
            and cycle is None
        ):
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
        prior_by_source = {
            CandidateSourceKind.KEEP_ORIGINAL: DecisionRank.MEDIUM,
            CandidateSourceKind.EXPLICIT_MAPPING: DecisionRank.STRONG,
            CandidateSourceKind.ABSOLUTE_EPISODE: DecisionRank.MEDIUM,
            CandidateSourceKind.PRODUCTION_CYCLE: DecisionRank.WEAK,
            CandidateSourceKind.UNKNOWN: DecisionRank.FALLBACK,
        }
        prior_rank = prior_by_source.get(candidate.source_kind, DecisionRank.FALLBACK)
        return prior_rank, [
            f"候选来源先验={candidate.source_kind.value}:{prior_rank.name}"
        ]

    def _evaluate_common_context(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidate: AdjustmentCandidate,
    ) -> tuple[ContextMatchLevel, list[str]]:
        """
        评估通用上下文 - 标题年份和发布时间与目标周期的匹配度（投票机制）

        :param release_info: 发布信息
        :param show_context: 剧集上下文
        :param candidate: 待评估候选
        :return: `(context_level, reasons)`
        """
        reasons: list[str] = []
        cycle = show_context.production_cycle_for_range(candidate.target_range)
        if cycle is None:
            reasons.append("目标范围无可用制作周期信息, 通用上下文为 NEUTRAL")
            return ContextMatchLevel.NEUTRAL, reasons

        match_votes = 0
        conflict_votes = 0

        # 标题年份匹配
        year_signal = self._title_year_signal(release_info, cycle)
        if year_signal > 0:
            match_votes += 1
            reasons.append("标题年份与目标周期起始年份匹配")
        elif year_signal < 0:
            conflict_votes += 1
            reasons.append("标题年份与目标周期起始年份冲突")
        else:
            reasons.append("标题年份缺失或不足以判断")

        # 发布时间窗口匹配
        release_signal = self._release_date_signal(release_info, show_context, cycle)
        if release_signal > 0:
            match_votes += 1
            reasons.append("发布时间与目标周期窗口匹配")
        elif release_signal < 0:
            conflict_votes += 1
            reasons.append("发布时间与目标周期窗口冲突")
        else:
            reasons.append("发布时间缺失或不足以判断")

        # 根据投票结果确定等级
        if conflict_votes >= 2:
            return ContextMatchLevel.STRONG_CONFLICT, reasons
        # 单冲突无匹配 → CONFLICT（否定信号）
        if conflict_votes == 1 and match_votes == 0:
            return ContextMatchLevel.CONFLICT, reasons
        # 双匹配 → STRONG_MATCH（最强肯定信号）
        if match_votes >= 2:
            return ContextMatchLevel.STRONG_MATCH, reasons
        # 单匹配无冲突 → MATCH（肯定信号）
        if match_votes == 1 and conflict_votes == 0:
            return ContextMatchLevel.MATCH, reasons
        # 其他情况（如一匹配一冲突、或都缺失）→ NEUTRAL（中性）
        return ContextMatchLevel.NEUTRAL, reasons

    def _evaluate_intrinsic_evidence(
        self,
        release_info: ReleaseInfo,
        show_context: ShowContext,
        candidate: AdjustmentCandidate,
    ) -> tuple[DecisionRank, list[str]]:
        """
        评估策略独有证据 - 针对不同策略的特定验证逻辑

        :param release_info: 发布信息
        :param show_context: 剧集上下文
        :param candidate: 待评估候选
        :return: `(intrinsic_rank, reasons)`
        """
        reasons: list[str] = []

        # 原样候选：检查是否在已知合法范围内
        if candidate.source_kind == CandidateSourceKind.KEEP_ORIGINAL:
            if _range_looks_legal_in_context(show_context, candidate.target_range):
                reasons.append("原样候选命中已知合法范围")
                return DecisionRank.MEDIUM, reasons
            reasons.append("原样候选缺少已知合法性支撑")
            return DecisionRank.FALLBACK, reasons

        # 显式映射：根据命中点数分级（≥2 → VERY_STRONG, =1 → MEDIUM）
        if candidate.source_kind == CandidateSourceKind.EXPLICIT_MAPPING:
            mapping_points = self._count_explicit_mapping_points(
                candidate, release_info
            )
            if mapping_points >= 2:
                reasons.append(f"显式映射完整覆盖目标范围, 命中点数={mapping_points}")
                return DecisionRank.VERY_STRONG, reasons
            if mapping_points == 1:
                reasons.append("显式映射仅命中起点, 终点按长度推导")
                return DecisionRank.MEDIUM, reasons
            reasons.append("显式映射证据不足")
            return DecisionRank.WEAK, reasons

        # 累计集数：验证是否真正触发越界条件
        if candidate.source_kind == CandidateSourceKind.ABSOLUTE_EPISODE:
            known_max_episode = show_context.known_max_episode_for_original(
                candidate.original_range.begin_season
            )
            if (
                known_max_episode is not None
                and candidate.original_range.begin_episode > known_max_episode
            ):
                reasons.append(
                    f"原始集号越过当前逻辑季上限, 触发累计集数解释: 上限={known_max_episode}"
                )
                return DecisionRank.STRONG, reasons
            reasons.append("累计集数候选缺少明显越界触发")
            return DecisionRank.WEAK, reasons

        # 制作周期：验证是否命中有效周期窗口
        if candidate.source_kind == CandidateSourceKind.PRODUCTION_CYCLE:
            cycle = show_context.production_cycle_for_range(candidate.target_range)
            if cycle is not None:
                reasons.append(f"目标范围命中制作周期窗口: cycle={cycle.cycle_id}")
                return DecisionRank.MEDIUM, reasons
            reasons.append("制作周期候选未命中有效周期窗口")
            return DecisionRank.WEAK, reasons

        # 【未知策略】缺少策略独有证据，给予最低优先级
        reasons.append("未知候选缺少策略独有证据")
        return DecisionRank.FALLBACK, reasons

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
            and candidate.source_kind
            in {
                CandidateSourceKind.ABSOLUTE_EPISODE,
                CandidateSourceKind.PRODUCTION_CYCLE,
            }
            and not show_context.contains_point(candidate.original_range.begin)
            and candidate.original_range.begin_episode <= 3
        ):
            level = max(level, ContradictionLevel.HARD)
            blocked = True
            reasons.append("缺失季低位集号在无显式映射时不允许强行重映射")

        # 软反证：单集更新不应映射到历史周期（除非是合集）
        if (
            candidate.source_kind == CandidateSourceKind.PRODUCTION_CYCLE
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
        context_level: ContextMatchLevel,
        intrinsic_rank: DecisionRank,
        contradiction_level: ContradictionLevel,
        blocked_by_contradiction: bool,
    ) -> tuple[DecisionRank, int]:
        """
        组合四层评估结果形成离散决策等级

        :param candidate: 候选对象
        :param prior_rank: 来源先验等级
        :param context_level: 通用上下文等级
        :param intrinsic_rank: 独有证据等级
        :param contradiction_level: 反证等级
        :param blocked_by_contradiction: 是否被反证阻止改写
        :return: `(decision_rank, decision_score)`
        """
        # 被强反证阻止的改写候选直接降级
        if blocked_by_contradiction and candidate.changed:
            return DecisionRank.FALLBACK, 10

        # 分层加权：来源先验(×10) > 独有证据(×8) + 上下文调整 + 反证惩罚 + 原样bonus
        base_score = prior_rank * 10 + intrinsic_rank * 8

        # 根据标题年份和发布时间与目标周期的匹配程度进行加减分
        context_delta = {
            ContextMatchLevel.STRONG_CONFLICT: -18,
            ContextMatchLevel.CONFLICT: -8,
            ContextMatchLevel.NEUTRAL: 0,
            ContextMatchLevel.MATCH: 8,
            ContextMatchLevel.STRONG_MATCH: 16,
        }[context_level]

        # 反证惩罚：硬反证 -20（强力阻止），软反证 -8（适度降级）
        contradiction_delta = {
            ContradictionLevel.NONE: 0,
            ContradictionLevel.SOFT: -8,
            ContradictionLevel.HARD: -20,
        }[contradiction_level]

        # 原样候选保守性加分：如果未发生改写，额外 +4 分
        keep_original_bonus = 4 if not candidate.changed else 0
        decision_score = (
            base_score + context_delta + contradiction_delta + keep_original_bonus
        )

        # 根据总分划分离散等级
        if decision_score >= 88:
            rank = DecisionRank.VERY_STRONG
        elif decision_score >= 68:
            rank = DecisionRank.STRONG
        elif decision_score >= 50:
            rank = DecisionRank.MEDIUM
        elif decision_score >= 32:
            rank = DecisionRank.WEAK
        elif decision_score > 0:
            rank = DecisionRank.FALLBACK
        else:
            rank = DecisionRank.REJECTED
        return rank, decision_score

    @staticmethod
    def _normalize_episode_range(
        release_info: ReleaseInfo,
        show_context: ShowContext,
    ) -> EpisodeRange | None:
        """根据剧集上下文与发布习惯归一化输入范围"""
        if not (episode_range := release_info.parsed_range):
            raise ValueError("release_info.parsed_range 不能为空")

        if episode_range.is_single:
            return episode_range

        if episode_range.is_same_season:
            begin_absolute_point = show_context.absolute_by_point(episode_range.begin)
            if (
                begin_absolute_point is not None
                and begin_absolute_point == episode_range.end_episode
            ):
                logger.warn(
                    "[%s] 强收敛: %s(连续集号:%d)=%s，修正为单集",
                    release_info.title,
                    episode_range.begin.format(),
                    begin_absolute_point,
                    episode_range.end.format(),
                )
                release_info.parsed_range = EpisodeRange(
                    begin=episode_range.begin,
                    end=episode_range.begin,
                )
        return release_info.parsed_range

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
        """计算候选强度用于边际比较：decision_rank×10 + context×3 - contradiction×4 + 原样bonus"""
        state = states[id(candidate)]
        return (
            int(candidate.decision_rank) * 10
            + int(state["context_level"]) * 3
            - int(state["contradiction_level"]) * 4
            + (1 if not candidate.changed else 0)
        )

    def _build_score_card_evidences(
        self,
        *,
        candidate: AdjustmentCandidate,
        context_level: ContextMatchLevel,
        contradiction_level: ContradictionLevel,
        blocked_by_contradiction: bool,
        decision_rank: DecisionRank,
    ) -> tuple[EvidenceItem, ...]:
        """将离散评估结果补充为可读证据。"""
        evidences = [
            EvidenceItem(
                code="decision.context",
                summary=f"通用上下文等级={context_level.name}",
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
        if contradiction_level != ContradictionLevel.NONE:
            evidences.append(
                EvidenceItem(
                    code="decision.contradiction",
                    summary=f"反证等级={contradiction_level.name}",
                    level=EvidenceLevel.HIGH,
                    observed_range=candidate.original_range,
                    expected_range=candidate.target_range,
                )
            )
        if blocked_by_contradiction:
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
            candidate=AdjustmentCandidate(
                original_range=original_range,
                target_range=original_range,
                strategy="keep_original",
                source_kind=CandidateSourceKind.KEEP_ORIGINAL,
                reasons=("虚拟原样候选，仅用于边际比较",),
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

        if states[id(best_rewrite)]["margin_against_original"] < self.rewrite_margin:
            reasons.append(
                (
                    "原样范围通过最终采用条件，最佳改写候选未达到改写边际阈值，保持原样；"
                    f"要求边际>={self.rewrite_margin}，"
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
    ) -> tuple[int, int, int, int, int, int, int]:
        """七层排序优先级：反证阻止 > 上下文 > 决策等级 > 来源先验 > 独有证据 > 边际 > 原样优先。"""
        state = states[id(candidate)]
        return (
            0 if state["blocked"] and candidate.changed else 1,
            int(state["context_level"]),
            int(candidate.decision_rank),
            int(state["prior_rank"]),
            int(state["intrinsic_rank"]),
            int(state["margin_against_original"]),
            1 if not candidate.changed else 0,
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
        rewrite_margin: int = 1,
        decision_engine: RangeDecisionEngine | None = None,
    ) -> None:
        self.decision_engine = decision_engine or RangeDecisionEngine(
            grace_episodes=grace_episodes,
            rewrite_margin=rewrite_margin,
        )
        self.grace_episodes = grace_episodes
        self.rewrite_margin = rewrite_margin

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
