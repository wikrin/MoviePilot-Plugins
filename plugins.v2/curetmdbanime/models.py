from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum, IntEnum

from app.core.meta.metabase import MetaBase


class EvidenceLevel(IntEnum):
    """证据强度等级，数值越大表示信息越强"""

    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class CandidateSourceKind(str, Enum):
    """候选来源类别"""

    KEEP_ORIGINAL = "keep_original"
    EXPLICIT_MAPPING = "explicit_mapping"
    ABSOLUTE_EPISODE = "absolute_episode"
    PRODUCTION_CYCLE = "production_cycle"
    UNKNOWN = "unknown"


class DecisionRank(IntEnum):
    """离散决策等级，数值越大表示候选越强"""

    REJECTED = 0
    FALLBACK = 1
    WEAK = 2
    MEDIUM = 3
    STRONG = 4
    VERY_STRONG = 5


class ContextMatchLevel(IntEnum):
    """通用上下文匹配等级"""

    STRONG_CONFLICT = -2
    CONFLICT = -1
    NEUTRAL = 0
    MATCH = 1
    STRONG_MATCH = 2


class ContradictionLevel(IntEnum):
    """反证强度等级"""

    NONE = 0
    SOFT = 1
    HARD = 2


@dataclass(frozen=True, order=True)
class EpisodePoint:
    """单个季集坐标点"""

    # 季号
    season: int
    # 集号
    episode: int

    def __post_init__(self) -> None:
        """
        校验季集点是否合法

        :raises ValueError: 季号或集号不是正整数时抛出
        """
        if self.season < 1 or self.episode < 1:
            raise ValueError("季号和集号必须为正整数")

    def format(self) -> str:
        """
        将季集点格式化为标准展示字符串

        :return: `SxxEyy` 形式的字符串
        """
        return f"S{self.season:02d}E{self.episode:02d}"


@dataclass(frozen=True)
class EpisodeRange:
    """季集范围，支持单集与跨季区间"""

    # 起始季集点
    begin: EpisodePoint
    # 结束季集点
    end: EpisodePoint

    def __post_init__(self) -> None:
        """
        校验范围边界是否合法

        :raises ValueError: 结束点早于起始点时抛出
        """
        if self.is_reverse:
            raise ValueError("范围结束点不能早于起始点")

    @property
    def is_single(self) -> bool:
        """
        判断是否为单集范围

        :return: 起止点一致时返回 True
        """
        return self.begin == self.end

    @property
    def is_same_season(self) -> bool:
        """
        判断范围是否位于同一季

        :return: 同季时返回 True
        """
        return self.begin_season == self.end_season

    @property
    def is_reverse(self) -> bool:
        """判断范围是否逆序（结束点早于起始点）"""
        return (self.end_season, self.end_episode) < (
            self.begin_season,
            self.begin_episode,
        )

    @property
    def begin_season(self) -> int:
        """返回起始季号"""
        return self.begin.season

    @property
    def end_season(self) -> int:
        """返回结束季号"""
        return self.end.season

    @property
    def begin_episode(self) -> int:
        """返回起始集号"""
        return self.begin.episode

    @property
    def end_episode(self) -> int:
        """返回结束集号"""
        return self.end.episode

    @property
    def season_list(self) -> list[int]:
        """
        输出写回 `MetaBase.set_season()` 所需的季列表
        """
        return list(range(self.begin_season, self.end_season + 1))

    @property
    def episode_list(self) -> list[int]:
        """
        输出写回 `MetaBase.set_episode()` 所需的集列表
        """
        return list(range(self.begin_episode, self.end_episode + 1))

    @property
    def intra_season_length(self) -> int | None:
        """
        推断范围长度
        - 仅对常见同季连续范围做保守支持
        - 跨季范围交给更高层的 absolute 逻辑处理

        :param episode_range: 待判断范围
        :return: 可可靠推断时返回长度, 否则返回 None
        """
        if not self.is_same_season:
            return None
        return self.end_episode - self.begin_episode + 1

    def expand_original_points(self) -> tuple[EpisodePoint, ...]:
        """
        展开原始范围中的逐集点

        :return: 同季连续范围时返回逐集点序列, 否则返回空元组
        """
        if self.is_same_season:
            return tuple(
                EpisodePoint(season=self.begin_season, episode=episode)
                for episode in range(
                    self.begin_episode,
                    self.end_episode + 1,
                )
            )
        return ()

    @classmethod
    def from_meta_fields(
        cls,
        seasons: list[int] | None,
        episodes: list[int] | None,
    ) -> EpisodeRange | None:
        """
        从 `MetaBase` 的 begin/end 字段构建范围对象

        :param seasons: 包含季列表
        :param episodes: 含集列表
        :return: 成功时返回范围对象, 起始信息缺失时返回 None
        """
        if not episodes:
            return None

        begin_season = min(seasons, default=1)
        end_season = max(seasons, default=1)
        begin_episode = min(episodes)
        end_episode = max(episodes)

        return cls(
            begin=EpisodePoint(begin_season, begin_episode),
            end=EpisodePoint(end_season, end_episode),
        )

    @classmethod
    def from_meta(cls, meta: MetaBase) -> EpisodeRange | None:
        """
        从元数据对象提取范围

        :param meta: 元数据对象
        :return: 成功时返回范围对象, 无法构建时返回 None
        """
        return cls.from_meta_fields(
            seasons=meta.season_list,
            episodes=meta.episode_list,
        )

    def format(self) -> str:
        """
        格式化范围字符串

        :return: 单集时返回 `SxxEyy`, 区间时返回 `SxxEyy-SxxEzz`
        """
        if self.is_single:
            return self.begin.format()
        return f"{self.begin.format()}-{self.end.format()}"


@dataclass(frozen=True)
class EvidenceItem:
    """
    策略判定过程中可复用的证据项

    `summary` 用于面向人阅读的简短结论, `detail` 则保留必要上下文,
    方便日志与最终决策理由复用, 而无需每层重复拼接文本。
    """

    # 证据编码
    code: str
    # 面向人阅读的简短结论
    summary: str
    # 证据强度等级
    level: EvidenceLevel
    # 证据权重，仅保留给兼容展示，不再作为核心裁决参数
    weight: float = 1.0
    # 证据详情上下文
    detail: str | None = None
    # 当前观察到的范围
    observed_range: EpisodeRange | None = None
    # 证据支持的目标范围
    expected_range: EpisodeRange | None = None


@dataclass(frozen=True)
class ProductionCycle:
    """按播出规律聚合的一段连续内容"""

    # 档期编号
    cycle_id: int
    # 档期起始累计集序
    start_absolute: int
    # 档期结束累计集序
    end_absolute: int
    # 档期包含的季集点集合
    points: tuple[EpisodePoint, ...]
    # 档期划分原因
    reason: str
    # 档期起始播出日期
    start_date: date | None = None
    # 档期结束播出日期
    end_date: date | None = None

    @property
    def is_empty(self) -> bool:
        """
        判断周期是否为空

        :return: 没有任何季集点时返回 True
        """
        return not self.points

    def contains_date(self, target: date | None) -> bool:
        """判断指定日期是否落在周期的播出窗口内"""
        if target is None or not self.has_schedule_window:
            return False
        return bool(
            self.start_date
            and self.end_date
            and self.start_date <= target <= self.end_date
        )

    @property
    def has_schedule_window(self) -> bool:
        """
        判断是否具备时间窗口信息
        """
        return self.start_date is not None and self.end_date is not None


@dataclass
class ReleaseInfo:
    """发布条目基础信息，承载原始范围、发布时间和外部映射输入"""

    # 发布条目标题
    title: str
    # 发布条目年份
    year: int | None = None
    # 从标题等信息解析出的当前有效范围
    parsed_range: EpisodeRange | None = None
    # 发布日期
    publish_date: date | None = None
    # 发布来源标识
    source: str | None = None
    # TMDB 季集映射表
    tmdb_mapping: dict[EpisodePoint, EpisodePoint] = field(default_factory=dict)

    @property
    def release_date(self) -> date | None:
        """
        暴露统一的发布时间语义

        :return: 归一化后的发布日期
        """
        return self.publish_date


@dataclass
class AdjustmentCandidate:
    """范围调整候选模型"""

    # 原始范围
    original_range: EpisodeRange
    # 候选目标范围
    target_range: EpisodeRange
    # 调整策略标识
    strategy: str = "unknown"
    # 候选来源类别
    source_kind: CandidateSourceKind = CandidateSourceKind.UNKNOWN
    # 候选来源/摘要列表（不承载逐层评估细节）
    reasons: tuple[str, ...] = ()
    # 候选证据列表
    evidences: tuple[EvidenceItem, ...] = ()
    # 最终决策等级
    decision_rank: DecisionRank = DecisionRank.REJECTED

    @property
    def changed(self) -> bool:
        """
        判断候选是否产生实质调整

        :return: 目标范围与原始范围不一致时返回 True
        """
        return self.original_range != self.target_range


@dataclass(frozen=True)
class RangeAdjustmentDecision:
    """范围调整决策结果，同时承载应用层处理语义"""

    # 原始范围
    original_range: EpisodeRange
    # 最终确定的范围
    final_range: EpisodeRange
    # 最终选中的候选
    selected_candidate: AdjustmentCandidate | None
    # 所有候选列表
    candidates: tuple[AdjustmentCandidate, ...] = ()
    # 被硬约束拒绝的候选列表
    rejected_candidates: tuple[AdjustmentCandidate, ...] = ()
    # 最终决策理由列表
    reasons: tuple[str, ...] = ()
    # 是否因上游冲突等原因跳过调整
    skipped: bool = False

    @property
    def changed(self) -> bool:
        """
        判断最终决策是否修改范围

        :return: 最终范围与原始范围不一致时返回 True
        """
        return self.original_range != self.final_range


@dataclass(frozen=True)
class ShowContext:
    """
    剧集上下文聚合对象，供策略、候选评估和最终排序共享

    `absolute` 映射负责跨季连续性判断, `production_cycles` 负责按制作周期
    解释标题年份与发布时间等通用上下文证据。
    """

    # 已存在的季集点集合
    existing_points: frozenset[EpisodePoint]
    # 各季对应的已知集号列表
    season_episodes: dict[int, list[int]]
    # 季集点到累计集序的映射
    point_to_absolute: dict[EpisodePoint, int]
    # 累计集序到季集点的映射
    absolute_to_point: dict[int, EpisodePoint]
    # 播出档期列表
    production_cycles: tuple[ProductionCycle, ...]
    # 当前已知的上一集
    last_episode: EpisodePoint | None = None
    # 当前已知的下一集
    next_episode: EpisodePoint | None = None
    # 当前已知的上一集播出日期
    last_air_date: date | None = None
    # 当前已知的下一集播出日期
    next_air_date: date | None = None
    # 集数是否已最终确定
    count_finalized: bool = False

    def __post_init__(self) -> None:
        """
        归一化上下文字段为只读结构
        """
        object.__setattr__(self, "existing_points", frozenset(self.existing_points))
        object.__setattr__(self, "production_cycles", tuple(self.production_cycles))

    @property
    def latest_season_number(self) -> int | None:
        """
        返回已知最新季号
        """
        return max(self.season_episodes) if self.season_episodes else None

    @property
    def latest_season_max_episode(self) -> int | None:
        """
        返回已知最新季的最大集号

        :return: 最大集号, 不存在时返回 None
        """
        if (latest_season := self.latest_season_number) is None:
            return None
        episodes = self.season_episodes.get(latest_season, [])
        return max(episodes) if episodes else None

    def contains_point(self, point: EpisodePoint) -> bool:
        """
        判断指定季集点是否存在于上下文
        """
        return point in self.existing_points

    def absolute_by_point(self, point: EpisodePoint) -> int | None:
        """
        返回指定点的累计集序

        :param point: 待查询的季集点
        :return: 命中时返回累计集序, 否则返回 None
        """
        return self.point_to_absolute.get(point)

    def point_by_absolute(self, absolute: int) -> EpisodePoint | None:
        """
        根据累计集序返回季集点

        :param absolute: 累计集序
        :return: 命中时返回季集点, 否则返回 None
        """
        return self.absolute_to_point.get(absolute)

    def known_max_episode_for_original(self, season: int) -> int | None:
        """
        查询原始逻辑季已知最大集号
        """
        if not (season_episodes := self.season_episodes.get(season)):
            return None
        return max(season_episodes)

    def is_latest_season_grace_point(
        self, point: EpisodePoint, grace_episodes: int
    ) -> bool:
        """
        判断点是否落在最新季的宽限区间（容忍连载滞后）
        """
        if self.count_finalized:
            return False

        latest_season = self.latest_season_number
        latest_max_episode = self.latest_season_max_episode
        if latest_season is None or latest_max_episode is None:
            return False

        return (
            point.season == latest_season
            and latest_max_episode
            < point.episode
            <= latest_max_episode + grace_episodes
        )

    def range_length(self, episode_range: EpisodeRange) -> int | None:
        """
        计算范围长度 - 优先使用累计映射支持跨季场景
        """
        begin_absolute = self.absolute_by_point(episode_range.begin)
        end_absolute = self.absolute_by_point(episode_range.end)
        if begin_absolute is not None and end_absolute is not None:
            return end_absolute - begin_absolute + 1
        # 同季场景的保守回退
        if episode_range.is_same_season:
            return episode_range.end_episode - episode_range.begin_episode + 1
        return None

    def is_contiguous_range(self, episode_range: EpisodeRange) -> bool:
        """
        判断范围在累计集序上是否连续存在
        """
        begin_absolute = self.absolute_by_point(episode_range.begin)
        end_absolute = self.absolute_by_point(episode_range.end)
        if begin_absolute is None or end_absolute is None:
            return False
        return all(
            self.point_by_absolute(abs_num) is not None
            for abs_num in range(begin_absolute, end_absolute + 1)
        )

    def production_cycle_for_range(
        self, episode_range: EpisodeRange
    ) -> ProductionCycle | None:
        """
        返回完整覆盖指定范围的播出档期
        """
        begin_absolute = self.absolute_by_point(episode_range.begin)
        end_absolute = self.absolute_by_point(episode_range.end)
        if begin_absolute is None or end_absolute is None:
            return None
        for cycle in self.production_cycles:
            if (
                cycle.start_absolute
                <= begin_absolute
                <= end_absolute
                <= cycle.end_absolute
            ):
                return cycle
        return None

    def latest_available_cycle(
        self, release_date: date | None
    ) -> ProductionCycle | None:
        """
        返回指定发布时间下最新可用的播出周期
        """
        if release_date is None:
            return None

        latest: ProductionCycle | None = None
        for cycle in self.production_cycles:
            if cycle.start_date is None:
                continue
            # 找到起始日期不晚于发布时间的最后一个周期
            if cycle.start_date <= release_date:
                latest = cycle
            else:
                break
        return latest

    def expand_target_points(
        self, episode_range: EpisodeRange
    ) -> tuple[EpisodePoint, ...]:
        """
        展开目标范围中的逐集点 - 优先使用累计集序支持跨季展开
        """
        # 尝试通过累计集序展开（支持跨季）
        begin_absolute = self.absolute_by_point(episode_range.begin)
        end_absolute = self.absolute_by_point(episode_range.end)
        if begin_absolute is not None and end_absolute is not None:
            points = [
                self.point_by_absolute(abs_num)
                for abs_num in range(begin_absolute, end_absolute + 1)
            ]
            return tuple(point for point in points if point is not None)

        # 同季直接展开，跨季无映射则返回起止点
        if not episode_range.is_same_season:
            return (episode_range.begin, episode_range.end)

        return tuple(
            EpisodePoint(season=episode_range.begin_season, episode=episode)
            for episode in range(
                episode_range.begin_episode, episode_range.end_episode + 1
            )
        )
