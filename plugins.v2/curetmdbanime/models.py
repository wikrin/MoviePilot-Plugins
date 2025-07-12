from typing import Any, Optional, Dict, List, Union

from pydantic import BaseModel, Field


class IncludeEntry(BaseModel):
    class Config:
        extra = "ignore"

    # 集
    order: int
    # 原集号
    episode_number: Optional[int] = None
    # 原季号
    season_number: Optional[int] = None
    # 集数
    count : int = 1
    # 类型
    type: str = "tv"
    # TMDBID
    tmdbid: Optional[int] = None


class EpisodeMap(BaseModel):
    class Config:
        extra = "ignore"

    # 集
    order: int
    # 原集号
    episode_number: Optional[int] = None
    # 原季号
    season_number: Optional[int] = None
    # 类型
    type: str = "tv"
    # TMDBID
    tmdbid: Optional[int] = None

    @property
    def key_part1(self) -> Union[int, str]:
        return self.season_number if self.type == "tv" else self.type

    @property
    def key_part2(self) -> int:
        return self.episode_number if self.type == "tv" else self.tmdbid

    @property
    def mapping_key(self) -> tuple[Union[int, str], int]:
        """
        返回用于 org_map 的 key：
        - TV: (season_number, episode_number)
        - MOVIE: ("movie", tmdbid)
        """
        return (self.key_part1, self.key_part2)


class SeasonEntry(BaseModel):
    # 名称
    name: Optional[str] = None
    # 集数
    episode_count: int = 0
    # 季号
    season_number: int = 0
    # 包含
    include: Optional[List[IncludeEntry]] = None

    @property
    def _included_orders(self) -> set:
        """返回包含的集号集合（order）"""
        if not self.include:
            return set()
        return {item.order for item in self.include}

    @property
    def included_episodes(self) -> dict[int, EpisodeMap]:
        """返回 order -> episode 数据的映射表"""
        if not self.include:
            return {}

        return {
            e + item.order: EpisodeMap(
                order=e + item.order,
                episode_number=e + (item.episode_number or 0),
                season_number=item.season_number,
                type=item.type,
                tmdbid=item.tmdbid
            )
            for item in self.include
            for e in range(item.count)
        }

    @property
    def missing_episodes(self) -> list:
        """计算缺失的集数列表"""
        current = self._included_orders
        all_expected = set(range(1, self.episode_count + 1))

        extra = current - all_expected
        missing = all_expected - current
        sorted_missing = sorted(missing)

        # 移除与多余集数数量相等的末尾缺失项
        if extra:
            return sorted_missing[: -len(extra)]
        return sorted_missing


class SeriesEntry(BaseModel):
    # 名称
    name: Optional[str] = None
    # 季信息
    seasons: List[SeasonEntry] = Field(default_factory=list)

    def season(self, num: int) -> Optional[SeasonEntry]:
        """根据季号查找对应的 SeasonEntry 对象"""
        return next((s for s in self.seasons if s.season_number == num), None)

    @property
    def season_names(self) -> Dict[int, str]:
        """返回各季号对应的名称"""
        return {season.season_number: season.name or f"第 {season.season_number} 季" for season in self.seasons}

    @property
    def missing_episodes_by_season(self) -> Dict[int, list]:
        """返回每季中缺失的集号列表"""
        return {season.season_number: season.missing_episodes for season in self.seasons}

    @property
    def episode_mapping(self) -> Dict[int, Dict[int, EpisodeMap]]:
        """返回每季的集号映射表"""
        return {
            season.season_number: season.included_episodes
            for season in self.seasons
            if season.included_episodes
        }

    @property
    def min_season(self) -> int:
        """获取最小季号"""
        return min((s.season_number for s in self.seasons), default=1)

    @property
    def max_season(self) -> int:
        """获取最大季号"""
        return max((s.season_number for s in self.seasons), default=1)


class LogicSeason(BaseModel):
    class Config:
        extra = "ignore"
        arbitrary_types_allowed = True

    # 季名
    name: Optional[str] = None
    # 播出时间
    air_date: Optional[str] = None
    # 季号
    season_number: int = 1
    # 映射
    episodes_map: Optional[Dict[int, EpisodeMap]] = None
    # 评分
    vote_average: Optional[float] = None
    # 海报
    poster_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name or f"第 {self.season_number} 季",
            "air_date": self.air_date,
            "episode_count": self.episode_count,
            "season_number": self.season_number,
            "vote_average": self.vote_average,
            "poster_path": self.poster_path,
        }

    def eps(self, use_cont_eps) -> list[int]:
        return [logic[1] for logic in self.org_map(use_cont_eps).values()]

    def org_map(self, use_cont_eps: bool = False) -> dict[tuple, tuple[int, int]]:
        """
        返回原始季、集到新的集的映射关系
        - TV: (tmdb_s, tmdb_e) -> s, e
        - MOVIE: ("movie", tmdbid) -> s, e
        """
        offset = 0
        if use_cont_eps:
            for ep, org in self.episodes_map.items():
                if org.season_number and org.episode_number is not None:
                    offset = org.episode_number - ep
                    break
        return {
            org.mapping_key: (self.season_number, ep + offset)
            for ep, org in self.episodes_map.items()
        }

    @property
    def episode_count(self) -> int:
        return len(self.episodes_map) if self.episodes_map else 0

    @property
    def org_seasons(self) -> list[int]:
        return sorted({s.season_number for s in self.episodes_map.values()})

    @property
    def unique_entry(self) -> List[EpisodeMap]:
        if not self.episodes_map:
            return []

        seen = set()
        result = []

        for mapping in self.episodes_map.values():
            # 提取关键字段用于判断唯一性
            key = (
                mapping.season_number,
                mapping.type,
                mapping.tmdbid,
            )

            if key not in seen:
                seen.add(key)
                result.append(mapping)

        return result


class LogicSeries(BaseModel):
    # 剧名
    name: Optional[str] = None
    # 季
    seasons: list[LogicSeason] = Field(default_factory=list)
    # 评分
    vote_average: float = 0
    # 海报
    poster_path: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True

    def org_map(self, use_cont_eps: bool = False) -> dict[tuple, tuple[int, int]]:
        """返回 (org_s, org_e) -> s, e 的映射表（缓存）"""
        return {
            k: v
            for s in self.seasons
            for k, v in s.org_map(use_cont_eps).items()
        }

    def add_season(self, name, air_date, season_number, episodes_map, **kwargs):
        self.seasons.append(
            LogicSeason(
                name=name,
                air_date=air_date,
                season_number=season_number,
                episodes_map=episodes_map,
                **kwargs,
            )
        )

    def has_season(self, season_number):
        for season in self.seasons:
            if season.season_number == season_number:
                return True
        return False

    def season_info(self, season_number) -> Optional[LogicSeason]:
        return next((season for season in self.seasons if season.season_number == season_number), None)

    def seasons_eps(self, use_cont_eps: bool = False) -> Dict[int, List[int]]:
        return {s.season_number: s.eps(use_cont_eps) for s in self.seasons}

    @property
    def seasons_info(self) -> List[dict]:
        return [season.to_dict() for season in self.seasons]
