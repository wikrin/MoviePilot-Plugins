from dataclasses import dataclass
from typing import Callable

from app.chain.tmdb import TmdbChain
from app.core.context import MediaInfo
from app.log import logger


@dataclass
class Candidate:
    season_num: int
    episode_num: int
    source: str


@dataclass
class ScoreDetail:
    candidate: Candidate
    priority: int
    seq_distance: int
    latest_grace: bool
    prefer_original: bool
    reasons: list[str]
    sort_key: tuple[int, int, int, int, int, int, int]


@dataclass
class AdjustDecision:
    season_num: int
    episode_num: int
    changed: bool
    source: str
    reasons: list[str]
    scored: list[ScoreDetail]


@dataclass
class SelectDecision:
    best_candidate: Candidate
    reasons: list[str]
    source: str
    scored: list[ScoreDetail]


@dataclass
class PairAdjustDecision:
    begin_season: int | None
    begin_episode: int | None
    end_season: int | None
    end_episode: int | None
    changed: bool
    begin_decision: AdjustDecision | None
    end_decision: AdjustDecision | None
    end_source: str
    reasons: list[str]


class CandidatePool:
    def __init__(
        self, build_continuous: Callable[[int, int, MediaInfo], tuple[int, int] | None]
    ):
        """
        候选收集器

        :param build_continuous: 连续集号候选构造函数
        """
        self._build_continuous = build_continuous

    @staticmethod
    def _dedupe_add(candidates: list[Candidate], candidate: Candidate) -> None:
        if all(
            c.season_num != candidate.season_num
            or c.episode_num != candidate.episode_num
            for c in candidates
        ):
            candidates.append(candidate)

    def collect(
        self,
        season_num: int,
        episode_num: int,
        tmdb_mapping: dict[tuple[int, int], tuple[int, int]],
        mediainfo: MediaInfo,
    ) -> tuple[Candidate, list[Candidate]]:
        """
        收集原始、映射、连续候选

        :param season_num: 原始季号
        :param episode_num: 原始集号
        :param tmdb_mapping: TMDB 逻辑季集映射
        :param mediainfo: 媒体信息对象

        :return: (原始候选, 去重后候选列表)
        """
        original = Candidate(
            season_num=season_num,
            episode_num=episode_num,
            source="original",
        )
        candidates: list[Candidate] = [original]

        if mapped := tmdb_mapping.get((season_num, episode_num)):
            self._dedupe_add(
                candidates,
                Candidate(
                    season_num=mapped[0],
                    episode_num=mapped[1],
                    source="mapping",
                ),
            )

        if continuous := self._build_continuous(
            season_num,
            episode_num,
            mediainfo,
        ):
            self._dedupe_add(
                candidates,
                Candidate(
                    season_num=continuous[0],
                    episode_num=continuous[1],
                    source="continuous",
                ),
            )

        return original, candidates


class CandidateSpec:
    def filter(
        self, candidates: list[Candidate], mediainfo: MediaInfo
    ) -> tuple[list[Candidate], list[str]]:
        """
        按规范过滤候选

        :param candidates: 待过滤候选列表
        :param mediainfo: 媒体信息对象

        :return: (过滤后候选, 丢弃原因)
        """
        filtered: list[Candidate] = []
        dropped: list[str] = []

        for candidate in candidates:
            if candidate.season_num < 1 or candidate.episode_num < 1:
                dropped.append(
                    f"{candidate.source}:{candidate.season_num}x{candidate.episode_num}(非法季集号)"
                )
                continue
            if (
                mediainfo.number_of_episodes
                and candidate.episode_num > mediainfo.number_of_episodes
            ):
                dropped.append(
                    f"{candidate.source}:{candidate.season_num}x{candidate.episode_num}(超过总集数{mediainfo.number_of_episodes})"
                )
                continue

            season_episodes = mediainfo.seasons.get(candidate.season_num, [])
            known_count = len(season_episodes)
            if (
                known_count
                and candidate.episode_num > known_count
                and candidate.season_num != mediainfo.number_of_seasons
            ):
                dropped.append(
                    f"{candidate.source}:{candidate.season_num}x{candidate.episode_num}(非最新季越界>{known_count})"
                )
                continue
            filtered.append(candidate)

        return filtered, dropped

    def is_valid(self, season_num: int, episode_num: int, mediainfo: MediaInfo) -> bool:
        """
        检查候选是否满足硬约束

        :param season_num: 季号
        :param episode_num: 集号
        :param mediainfo: 媒体信息对象

        :return bool: 合法返回 True，否则返回 False
        """
        if season_num < 1 or episode_num < 1:
            return False
        if mediainfo.number_of_episodes and episode_num > mediainfo.number_of_episodes:
            return False

        season_episodes = mediainfo.seasons.get(season_num, [])
        known_count = len(season_episodes)
        if (
            known_count
            and episode_num > known_count
            and season_num != mediainfo.number_of_seasons
        ):
            return False
        return True


class CandidateSelector:
    def __init__(self, grace_episodes: int):
        """
        候选排序器

        :param grace_episodes: 最新季越界宽限集数
        """
        self._grace_episodes = grace_episodes

    @staticmethod
    def _episode_exists_in_season(
        season_num: int,
        episode_num: int,
        mediainfo: MediaInfo,
    ) -> bool:
        """
        判断指定季中是否存在目标剧集

        :param season_num: 季号
        :param episode_num: 集号
        :param mediainfo: 媒体信息对象

        :return bool: 存在返回 True，否则返回 False
        """
        season_episodes = mediainfo.seasons.get(season_num, [])
        return episode_num in season_episodes

    def _is_latest_season_grace(
        self, candidate: Candidate, mediainfo: MediaInfo
    ) -> bool:
        """
        判断候选是否处于最新季宽限区间

        :param candidate: 当前候选
        :param mediainfo: 媒体信息对象

        :return bool: 命中宽限返回 True，否则返回 False
        """
        season_episodes = mediainfo.seasons.get(candidate.season_num, [])
        known_count = len(season_episodes)
        return bool(
            mediainfo.number_of_episodes
            and candidate.season_num == mediainfo.number_of_seasons
            and known_count
            < candidate.episode_num
            <= known_count + self._grace_episodes
        )

    @staticmethod
    def _is_count_finalized(candidate: Candidate, mediainfo: MediaInfo) -> bool:
        """
        判断候选季的总集数是否已最终确定（即已完结或当前季集数建立完整）

        :param candidate: 当前候选
        :param mediainfo: 媒体信息对象

        :return bool:
            - True: 总集数已确定（剧集已完结 Ended/Canceled，或最后播出的集是季终 finale）
            - False: 总集数未确定（仍在连载中，或数据缺失保守视为未完结）
        """
        # 全局状态已完结，总集数必然已确定
        if mediainfo.status in ("Ended", "Canceled"):
            return True

        last_episode = mediainfo.tmdb_info.get("last_episode_to_air") or {}

        # 最后播出的季数大于当前候选季，说明当前季早已结束，总集数已确定
        if last_episode.get("season_number", float("-inf")) > candidate.season_num:
            return True

        episodes = TmdbChain().tmdb_episodes(
            mediainfo.tmdb_id,
            season=candidate.season_num,
            episode_group=mediainfo.episode_group,
        )

        # 没有数据，保守假设总集数尚未确定（可能还在更，也可能数据缺），返回 False
        if not last_episode and not episodes:
            return False

        # 检查最后播出的集或获取到的剧集列表最后一集是否为“季终”
        is_last_finale = last_episode.get("episode_type") in ("finale", "mid_season")
        is_ep_list_finale = (
            episodes[-1].episode_type in ("finale", "mid_season") if episodes else False
        )

        # 有一个迹象表明已完结（是季终），就认为总集数已确定
        return is_last_finale or is_ep_list_finale

    def _evaluate_priority(
        self,
        candidate: Candidate,
        original: Candidate,
        mediainfo: MediaInfo,
        has_mapping_override: bool,
    ) -> tuple[int, list[str]]:
        """
        评估优先级分值

        :param candidate: 当前候选
        :param original: 原始候选
        :param mediainfo: 媒体信息对象
        :param has_mapping_override: 是否存在映射覆盖候选

        :return: (优先级分值, 命中理由)
        """
        priority = 0
        reasons: list[str] = []

        if (
            has_mapping_override
            and candidate.source == "mapping"
            and (
                candidate.season_num != original.season_num
                or candidate.episode_num != original.episode_num
            )
        ):
            priority = max(priority, 300)
            reasons.append("优先规则: 映射覆盖命中")

        if candidate.source == "continuous" and candidate.episode_num == 1:
            priority = max(priority, 200)
            reasons.append("优先规则: 连续集号还原为E01")

        if candidate.source == "original" and self._episode_exists_in_season(
            original.season_num,
            original.episode_num,
            mediainfo,
        ):
            priority = max(priority, 150)
            reasons.append("优先规则: 原始季集存在于TMDB")

        next_ep = (
            mediainfo.next_episode_to_air
            if isinstance(mediainfo.next_episode_to_air, dict)
            else {}
        )
        next_season_num = next_ep.get("season_number")
        next_episode_num = next_ep.get("episode_number")
        if (
            next_season_num
            and next_episode_num
            and (candidate.season_num, candidate.episode_num)
            == (next_season_num, next_episode_num)
        ):
            priority = max(priority, 100)
            reasons.append("优先规则: 命中 next_episode_to_air")

        return priority, reasons

    def select(
        self,
        candidates: list[Candidate],
        original: Candidate,
        mediainfo: MediaInfo,
    ) -> SelectDecision:
        """
        对候选执行确定性排序并返回最优结果

        :param candidates: 候选列表
        :param original: 原始候选
        :param mediainfo: 媒体信息对象

        :return: 最优候选结果
        """
        has_mapping_override = any(c.source == "mapping" for c in candidates)

        next_ep = (
            mediainfo.next_episode_to_air
            if isinstance(mediainfo.next_episode_to_air, dict)
            else {}
        )
        next_season_num = next_ep.get("season_number")
        next_episode_num = next_ep.get("episode_number")

        source_rank = {
            "mapping": 0,
            "continuous": 1,
            "original": 2,
        }

        tmdb_info = mediainfo.tmdb_info if isinstance(mediainfo.tmdb_info, dict) else {}
        last_ep = tmdb_info.get("last_episode_to_air")
        last_season_num = (
            last_ep.get("season_number") if isinstance(last_ep, dict) else None
        )
        last_episode_num = (
            last_ep.get("episode_number") if isinstance(last_ep, dict) else None
        )

        scored: list[ScoreDetail] = []
        for candidate in candidates:
            reasons: list[str] = []
            priority, priority_reasons = self._evaluate_priority(
                candidate,
                original,
                mediainfo,
                has_mapping_override,
            )
            reasons.extend(priority_reasons)

            seq_distance = 999
            latest_grace = self._is_latest_season_grace(candidate, mediainfo)
            if self._is_count_finalized(candidate, mediainfo):
                latest_grace = False
                seq_distance = (
                    0
                    if self._episode_exists_in_season(
                        candidate.season_num,
                        candidate.episode_num,
                        mediainfo,
                    )
                    else 999
                )
                reasons.append(
                    f"确定性规则: 历史季存在性={'命中' if seq_distance == 0 else '未命中'}"
                )
            else:
                if (
                    last_season_num
                    and last_episode_num
                    and candidate.season_num == last_season_num
                ):
                    seq_distance = abs(candidate.episode_num - last_episode_num)
                    reasons.append(f"确定性规则: 活跃季序位距离(last)={seq_distance}")
                elif (
                    next_season_num
                    and next_episode_num
                    and candidate.season_num == next_season_num
                ):
                    seq_distance = abs(candidate.episode_num - next_episode_num)
                    reasons.append(f"确定性规则: 活跃季序位距离(next)={seq_distance}")

            if latest_grace:
                reasons.append("确定性规则: 最新季宽限内")

            prefer_original = int(
                candidate.season_num == original.season_num
                and candidate.episode_num == original.episode_num
            )
            if prefer_original:
                reasons.append("确定性规则: 平票保留原值")

            # 排序键顺序固定：先比较优先级，再比较序位距离与稳定平票项
            sort_key = (
                priority,
                -seq_distance,
                int(latest_grace),
                prefer_original,
                -candidate.season_num,
                -candidate.episode_num,
                -source_rank.get(candidate.source, 99),
            )

            scored.append(
                ScoreDetail(
                    candidate=candidate,
                    priority=priority,
                    seq_distance=seq_distance,
                    latest_grace=latest_grace,
                    prefer_original=bool(prefer_original),
                    reasons=reasons,
                    sort_key=sort_key,
                )
            )

            logger.debug(
                "%s 排序项: source=%s target=S%02dE%02d priority=%s seq_distance=%s latest_grace=%s prefer_original=%s sort_key=%s",
                mediainfo.title_year,
                candidate.source,
                candidate.season_num,
                candidate.episode_num,
                priority,
                seq_distance,
                int(latest_grace),
                prefer_original,
                sort_key,
            )

        scored.sort(key=lambda item: item.sort_key, reverse=True)
        best = scored[0]

        return SelectDecision(
            best_candidate=best.candidate,
            reasons=best.reasons,
            source=best.candidate.source,
            scored=scored,
        )


class PairLinker:
    def __init__(self, valid_checker: Callable[[int, int, MediaInfo], bool]):
        """
        区间终点联动器

        :param valid_checker: 候选合法性检查函数
        """
        self._valid_checker = valid_checker

    def link_end(
        self,
        *,
        begin_result: AdjustDecision | None,
        begin_episode: int | None,
        end_input_season: int,
        end_episode: int,
        end_result: AdjustDecision,
        tmdb_mapping: dict[tuple[int, int], tuple[int, int]],
        mediainfo: MediaInfo,
        reasons: list[str],
    ) -> tuple[int | None, int | None, str]:
        """
        计算 end 的最终输出

        :param begin_result: begin 调整结果
        :param begin_episode: 原始 begin 集号
        :param end_input_season: end 调整时输入季号
        :param end_episode: 原始 end 集号
        :param end_result: end 调整结果
        :param tmdb_mapping: 映射表
        :param mediainfo: 媒体信息对象
        :param reasons: 额外理由列表

        :return: (end 输出季号, end 输出集号, 采用来源)
        """
        if end_result.changed:
            source = "mapping_link" if end_result.source == "mapping" else "adjust"
            logger.debug(
                "%s end联动轨迹: route=direct_adjust source=%s target=S%02dE%02d",
                mediainfo.title_year,
                source,
                end_result.season_num,
                end_result.episode_num,
            )
            return end_result.season_num, end_result.episode_num, source

        mapped_end = tmdb_mapping.get((end_input_season, end_episode))
        if (
            mapped_end
            and (mapped_end[0], mapped_end[1]) != (end_input_season, end_episode)
            and self._valid_checker(mapped_end[0], mapped_end[1], mediainfo)
        ):
            reasons.append("end采用mapping联动")
            logger.debug(
                "%s end联动轨迹: route=mapping_link from=S%02dE%02d to=S%02dE%02d",
                mediainfo.title_year,
                end_input_season,
                end_episode,
                mapped_end[0],
                mapped_end[1],
            )
            return mapped_end[0], mapped_end[1], "mapping_link"

        if begin_result and begin_result.changed and begin_episode:
            # 在 mapping 不可用时再走 delta，优先级固定可避免区间端点漂移
            delta = begin_episode - begin_result.episode_num
            linked_end_episode = end_episode - delta
            linked_end_season = begin_result.season_num
            if self._valid_checker(linked_end_season, linked_end_episode, mediainfo):
                reasons.append("end采用delta联动")
                logger.debug(
                    "%s end联动轨迹: route=delta_link delta=%s target=S%02dE%02d",
                    mediainfo.title_year,
                    delta,
                    linked_end_season,
                    linked_end_episode,
                )
                return linked_end_season, linked_end_episode, "delta_link"

        logger.debug(
            "%s end联动轨迹: route=original_keep input=S%02dE%02d",
            mediainfo.title_year,
            end_input_season,
            end_episode,
        )
        return end_input_season, end_episode, "original"


class MetaAdjustService:
    def __init__(
        self,
        grace_episodes: int = 3,
    ):
        """
        初始化元数据纠正服务

        :param grace_episodes: 最新季允许的越界宽限集数
        """
        self._grace_episodes = grace_episodes

        self._pool = CandidatePool(self._build_continuous_candidate)
        self._spec = CandidateSpec()
        self._selector = CandidateSelector(grace_episodes=self._grace_episodes)
        self._pair = PairLinker(valid_checker=self._is_valid_candidate)

    def _build_continuous_candidate(
        self, season: int, episode: int, mediainfo: MediaInfo
    ) -> tuple[int, int] | None:
        """
        根据连续集号在各季中反查候选季集

        :param season: 原始季号
        :param episode: 原始集号
        :param mediainfo: 媒体信息对象

        :return: 命中时返回候选季集 (season_num, episode_num)，否则返回 None
        """
        if not mediainfo.number_of_episodes:
            logger.debug(
                "%s 连续候选跳过: 缺少 number_of_episodes", mediainfo.title_year
            )
            return None
        if len(mediainfo.seasons.get(season, [])) >= episode:
            logger.debug(
                "%s 连续候选跳过: 原季已覆盖 S%02dE%02d",
                mediainfo.title_year,
                season,
                episode,
            )
            return None
        if episode > mediainfo.number_of_episodes:
            logger.debug(
                "%s 连续候选跳过: E%s 超过总集数 %s",
                mediainfo.title_year,
                episode,
                mediainfo.number_of_episodes,
            )
            return None

        offset = 0
        for season_key in sorted(mediainfo.seasons.keys()):
            if season_key == 0:
                continue
            episodes_list = mediainfo.seasons.get(season_key, [])
            if (found_episode := episode - offset) in episodes_list:
                logger.debug(
                    "%s 连续候选命中: S%02dE%02d -> S%02dE%02d",
                    mediainfo.title_year,
                    season,
                    episode,
                    season_key,
                    found_episode,
                )
                return season_key, found_episode
            offset += len(episodes_list)

        logger.debug("%s 连续候选未命中: S%02dE%02d", mediainfo.title_year, season, episode)
        return None

    def _is_valid_candidate(
        self,
        season_num: int,
        episode_num: int,
        mediainfo: MediaInfo,
    ) -> bool:
        """
        检查候选合法性

        :param season_num: 季号
        :param episode_num: 集号
        :param mediainfo: 媒体信息对象

        :return bool: 合法返回 True，否则返回 False
        """
        return self._spec.is_valid(season_num, episode_num, mediainfo)

    def adjust(
        self,
        season_num: int,
        episode_num: int,
        tmdb_mapping: dict[tuple[int, int], tuple[int, int]],
        mediainfo: MediaInfo,
    ) -> AdjustDecision:
        """
        对输入季集执行候选生成、规则评估与最终纠正决策

        :param season_num: 原始季号
        :param episode_num: 原始集号
        :param tmdb_mapping: TMDB 逻辑季集映射
        :param mediainfo: 媒体信息对象

        :return: 调整决策结果
        """
        original, candidates = self._pool.collect(
            season_num=season_num,
            episode_num=episode_num,
            tmdb_mapping=tmdb_mapping,
            mediainfo=mediainfo,
        )

        logger.debug(
            "%s 初始候选: %s",
            mediainfo.title_year,
            ", ".join(
                [f"{c.source}:{c.season_num}x{c.episode_num}" for c in candidates]
            ),
        )

        candidates, dropped = self._spec.filter(candidates, mediainfo)
        if dropped:
            logger.debug(
                "%s Specification过滤: dropped=%s",
                mediainfo.title_year,
                "; ".join(dropped),
            )

        if not candidates:
            logger.warning(
                "%s Specification 后无可用候选，保留原值 S%02dE%02d",
                mediainfo.title_year,
                season_num,
                episode_num,
            )
            return AdjustDecision(
                season_num=season_num,
                episode_num=episode_num,
                changed=False,
                source="original",
                reasons=["Specification 后无可用候选"],
                scored=[],
            )

        select = self._selector.select(candidates, original, mediainfo)

        logger.debug(
            "%s 候选优先级明细: %s",
            mediainfo.title_year,
            ", ".join(
                [
                    (
                        f"{item.candidate.source}={item.candidate.season_num}x"
                        f"{item.candidate.episode_num}:{item.priority}:k={item.sort_key}"
                    )
                    for item in select.scored
                ]
            ),
        )
        logger.info(
            "%s 选优决策: source=%s target=S%02dE%02d reasons=%s",
            mediainfo.title_year,
            select.source,
            select.best_candidate.season_num,
            select.best_candidate.episode_num,
            "；".join(select.reasons),
        )

        should_apply = (
            select.best_candidate.season_num,
            select.best_candidate.episode_num,
        ) != (original.season_num, original.episode_num)

        return AdjustDecision(
            season_num=(
                select.best_candidate.season_num
                if should_apply
                else original.season_num
            ),
            episode_num=(
                select.best_candidate.episode_num
                if should_apply
                else original.episode_num
            ),
            changed=should_apply,
            source=select.source,
            reasons=select.reasons,
            scored=select.scored,
        )

    def adjust_pair(
        self,
        *,
        begin_season: int | None,
        begin_episode: int | None,
        end_season: int | None,
        end_episode: int | None,
        tmdb_mapping: dict[tuple[int, int], tuple[int, int]],
        mediainfo: MediaInfo,
    ) -> PairAdjustDecision:
        """
        调整区间季集并进行 begin/end 联动

        :param begin_season: begin 季号
        :param begin_episode: begin 集号
        :param end_season: end 季号
        :param end_episode: end 集号
        :param tmdb_mapping: TMDB 逻辑季集映射
        :param mediainfo: 媒体信息对象

        :return: 区间调整结果
        """
        begin_result = None
        begin_output_season = begin_season
        begin_output_episode = begin_episode
        begin_chain_season = begin_season or 1
        reasons: list[str] = []

        if begin_episode:
            begin_result = self.adjust(
                season_num=begin_chain_season,
                episode_num=begin_episode,
                tmdb_mapping=tmdb_mapping,
                mediainfo=mediainfo,
            )
            if begin_result.changed:
                begin_chain_season = begin_result.season_num
                begin_output_season = begin_result.season_num
                begin_output_episode = begin_result.episode_num

        end_result = None
        end_output_season = end_season
        end_output_episode = end_episode
        end_source = "original"

        if end_episode:
            end_input_season = end_season or begin_chain_season or begin_season or 1
            end_result = self.adjust(
                season_num=end_input_season,
                episode_num=end_episode,
                tmdb_mapping=tmdb_mapping,
                mediainfo=mediainfo,
            )

            end_output_season, end_output_episode, end_source = self._pair.link_end(
                begin_result=begin_result,
                begin_episode=begin_episode,
                end_input_season=end_input_season,
                end_episode=end_episode,
                end_result=end_result,
                tmdb_mapping=tmdb_mapping,
                mediainfo=mediainfo,
                reasons=reasons,
            )

            effective_begin_season = begin_output_season or begin_chain_season or 1
            effective_end_season = (
                end_output_season or end_input_season or effective_begin_season
            )

            if (
                begin_output_episode
                and end_output_episode
                and effective_end_season == effective_begin_season
                and end_output_episode < begin_output_episode
            ):
                end_output_episode = begin_output_episode
                reasons.append("同季逆序保护")
                logger.debug(
                    "%s end联动轨迹: route=reverse_guard keep=S%02dE%02d",
                    mediainfo.title_year,
                    effective_end_season,
                    end_output_episode,
                )

            if begin_output_episode and end_output_episode:
                end_output_season = (
                    None
                    if effective_end_season == effective_begin_season
                    else effective_end_season
                )

        changed = (
            begin_output_season != begin_season
            or begin_output_episode != begin_episode
            or end_output_season != end_season
            or end_output_episode != end_episode
        )

        return PairAdjustDecision(
            begin_season=begin_output_season,
            begin_episode=begin_output_episode,
            end_season=end_output_season,
            end_episode=end_output_episode,
            changed=changed,
            begin_decision=begin_result,
            end_decision=end_result,
            end_source=end_source,
            reasons=reasons,
        )
