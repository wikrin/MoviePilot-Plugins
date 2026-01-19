# 基础库
import contextvars
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional, Dict, List, Tuple

# 项目库
from app import schemas
from app.core.cache import Cache
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta.metabase import MetaBase
from app.log import logger
from app.modules.themoviedb import TmdbApi
from app.modules.themoviedb.scraper import TmdbScraper
from app.plugins import _PluginBase
from app.schemas.types import MediaType

from .bangumi import BangumiAPIClient
from .curetmdb import CureTMDb
from .models import EpisodeMap, SeriesEntry, LogicSeason, LogicSeries


class SeasonCache:
    region = "plugin.curetmdbanime"

    def __init__(self, use_cont_eps: bool = False):
        self._cache = Cache(cache_type="lru", maxsize=100)
        self.use_cont_eps = use_cont_eps

    def put(self, tmdbid: int, series: LogicSeries):
        self._cache.set(str(tmdbid), series, region=self.region)

    def get(self, tmdbid: int) -> Optional[LogicSeries]:
        return self._cache.get(str(tmdbid), region=self.region)

    def _get_logic_season(self, tmdbid: int, season: int) -> Optional[LogicSeason]:
        """获取指定季的信息，若不存在则返回 None"""
        if series := self.get(tmdbid):
            return next((s for s in series.seasons if s.season_number == season), None)

    def season_info(self, tmdbid: int, season: int) -> dict:
        if (series := self.get(tmdbid)) and series.has_season(season):
            return series.season_info(season).to_dict()
        return {}

    def org_season_episode(self, tmdbid: int, season: int, episode: int = None) -> Tuple[int, int, dict]:
        """
        根据逻辑季信息解析出原始季号和集号。
        如果没有逻辑季，则尝试获取第一个 tv 类型的原始季号。

        :param tmdbid: 媒体 TMDB ID
        :param season: 用户提供的季号
        :param episode: 用户提供的集号（可选）
        :return tuple: (original_season, original_episode, seasoninfo)
        """
        if season is None:
            return (season, episode, {})
        seasoninfo = {}
        if logic := self._get_logic_season(tmdbid, season):
            seasoninfo.update(logic.to_dict())
            if episode is not None:
                mapping = logic.org_map(self.use_cont_eps)
                for f, l in mapping.items():
                    if (season, episode) == f or (season, episode) == l:
                        season, episode = f
                        break
            else:
                for _map in logic.episodes_map.values():
                    if _map.type == "tv" and _map.season_number is not None:
                        season = _map.season_number
                        break
        return (season, episode, seasoninfo)

    def org_to_logic(self, tmdbid: int, season: int) -> Dict[tuple, tuple[int, int]]:
        logic = self._get_logic_season(tmdbid, season)
        return logic.org_map(self.use_cont_eps) if logic else None

    def org_map(self, tmdbid: int) -> Dict[tuple, tuple[int, int]]:
        """
        获取原始季映射关系

        :param tmdbid: 媒体 TMDB ID
        """
        return logic.org_map(self.use_cont_eps) if (logic := self.get(tmdbid)) else {}

    def unique_seasons(self, tmdbid: int, season: int) -> list[EpisodeMap]:
        logic = self._get_logic_season(tmdbid, season)
        if not logic:
            return []
        return logic.unique_entry

    def clear(self):
        self._cache.clear(self.region)


class SeasonSplitter:

    def __init__(self, ctmdb: "CureTMDbAnime"):
        self.ctmdb = ctmdb
        self.curetmdb = CureTMDb(self.ctmdb._source)
        self.bgm = BangumiAPIClient(ua=settings.USER_AGENT)

    def from_tmdb(self, mediainfo: MediaInfo) -> Optional[LogicSeries]:
        """
        根据 TMDB 和 Bangumi 数据构建逻辑季信息。
        """
        season_count = mediainfo.number_of_seasons

        def air_date(season: Optional[int] = None) -> Optional[str]:
            if season is None:
                air_date = mediainfo.release_date
            else:
                air_date = next(
                        (
                            info.get("air_date")
                            for info in mediainfo.season_info
                            if info.get("season_number") == season
                        ),
                        mediainfo.release_date,
                    )
            return air_date

        def ctmdb_derive() -> Optional[SeriesEntry]:
            if result := self.curetmdb.season_info(mediainfo.tmdb_id):
                return SeriesEntry(**result)

        def bangumi_derive() -> Optional[SeriesEntry]:
            if not (set(mediainfo.genre_ids) & {16} and set(mediainfo.origin_country) & {"JP"}):
                return None

            if season_count and season_count < 3:
                # 如果未找到季信息且媒体信息显示有2季
                if season_count == 2:
                    # 查找Bangumi条目并验证是否符合合并条件
                    item = self._search_subjects(mediainfo.original_title, air_date(season_count))
                    result = item and self.bgm.get_sort_and_ep(item["id"])
                    if result and result[0] == result[1]:
                        return None
                # 若仍未找到季信息，则从Bangumi获取
                item = self._search_subjects(mediainfo.original_title, air_date())
                return self.bgm.season_info(item)

        seasons = ctmdb_derive() or bangumi_derive()
        if seasons and seasons.has_inconsistent(mediainfo.seasons):
            return self._logic_seasons(mediainfo, seasons)

    def _search_subjects(self, title: str, air_date: Optional[int] = None) -> Optional[dict]:
        try:
            result = self.bgm.search(title, air_date)
            return next((item for item in result if item.get("platform") == "TV"), None)
        except Exception as e:
            logger.error(f"Bangumi search error: {e}")

    def _logic_seasons(self, mediainfo: MediaInfo, series: SeriesEntry) -> LogicSeries:
        logic_series = LogicSeries(name=series.name)

        def append(name: str, mapping: dict[int, EpisodeMap]):
            name = name or tmdb_season.get("name")

            logger.info(f"{mediainfo.tmdb_id} {mediainfo.title_year}: {name}, 集数: {len(mapping)}")
            logic_series.add_season(
                    name=name,
                    air_date=air_date,
                    season_number=current_season,
                    episodes_map=mapping,
                    **{k: v for k, v in tmdb_season.items() if k not in {"name", "air_date", "season_number"}},
                )

        mapping = series.episode_mapping
        missing_eps = series.missing_episodes_by_season
        current_season = series.min_season

        tmdb_seasons = []
        for season in range(current_season, mediainfo.number_of_seasons + 1):
            with self.ctmdb.no_recursion():
                if tmdb_season := self.ctmdb.chain.tmdb_info(mediainfo.tmdb_id, mediainfo.type, season):
                    tmdb_seasons.append(tmdb_season)
        if not tmdb_seasons:
            return None
        if mediainfo.number_of_seasons >= 2:
            # 存在多季时, 检查并删除重复的集
            tmdb_seasons = self.remove_duplicate_episodes(tmdb_seasons)
        try:
            last_episode = tmdb_seasons[-1]["episodes"][-1]["id"]
        except(IndexError, KeyError):
            last_episode = None

        for tmdb_season in tmdb_seasons:
            episodes: list[dict] = tmdb_season.get("episodes", [])
            air_date = tmdb_season.get("air_date")

            for ep in episodes:
                uniqueid = ep.get("id")
                if air_date is None:
                    air_date = ep.get("air_date")

                logic_ep = (
                    missing_eps[current_season].pop(0)
                    if missing_eps.get(current_season)
                    else logic_ep + 1
                )

                if current_season not in mapping:
                    mapping[current_season] = {}

                if logic_ep:
                    mapping[current_season][logic_ep] = EpisodeMap(
                        order=logic_ep, **ep
                    )

                while any((
                    missing_eps.get(current_season) == [],
                    # ep.get("episode_type") == "finale",
                    is_last := (uniqueid == last_episode),
                )):
                    append(series.season_names.get(current_season), mapping=mapping.get(current_season, {}))
                    current_season += 1
                    air_date = None
                    logic_ep = 0
                    if is_last:
                        break

        return logic_series

    @staticmethod
    def remove_duplicate_episodes(seasons: List[dict]):
        """
        根据播出日期去重，保留数据最完整的集，并保持原有顺序

        :param seasons: 包含多个季度信息的列表，每个季度包含episodes列表
        :return List[dict]: 去重后的季度列表
        """

        def completeness_score(episode: dict) -> int:
            """计算集的完整性评分"""
            score = 0
            if episode.get("name"):
                score += 10
            if episode.get("overview"):
                score += 8
            if episode.get("still_path"):
                score += 5
            if episode.get("vote_average"):
                score += 3
            score += sum(1 for v in episode.values() if v is not None)
            return score

        # 按播出日期分组：{air_date: {season_idx: [ep_idx, ...]}}
        episodes_by_date: dict[str, dict[int, list[int]]] = {}
        episodes_to_remove = set()

        # 构建索引
        for season_idx, season in enumerate(seasons):
            episodes_list = season.get("episodes", [])
            for ep_idx, episode in enumerate(episodes_list):
                air_date = episode.get("air_date")
                if not air_date:
                    continue

                if air_date not in episodes_by_date:
                    episodes_by_date[air_date] = {}

                if season_idx not in episodes_by_date[air_date]:
                    # 发现相同播出日期的不同季
                    if episodes_by_date[air_date]:
                        # 比较并保留完整性最高的集
                        for existing_season_idx, existing_ep_indices in episodes_by_date[air_date].items():
                            existing_ep_idx = existing_ep_indices.pop(0)
                            existing_episode = seasons[existing_season_idx]["episodes"][existing_ep_idx]

                            if completeness_score(episode) > completeness_score(existing_episode):
                                # 新集更完整，替换旧集
                                seasons[existing_season_idx]["episodes"][existing_ep_idx] = episode
                            # 标记新集移除
                            episodes_to_remove.add(ep_idx)

                    episodes_by_date[air_date][season_idx] = []

                episodes_by_date[air_date][season_idx].append(ep_idx)

            # 删除重复的集（从后向前删除以保持索引有效性）
            for ep_idx in sorted(list(episodes_to_remove), reverse=True):
                season["episodes"].pop(ep_idx)

        # 过滤掉没有episodes的季
        return [s for s in seasons if s and s.get("episodes")]

    def clear(self):
        """清理缓存"""
        self.curetmdb.clear()
        self.bgm.clear()

    def close(self):
        self.bgm.close()

    @property
    def services(self) -> bool:
        if getattr(self, "curetmdb", None):
            return self.curetmdb.remote_mode
        return False


class CureTMDbAnime(_PluginBase):
    # 插件名称
    plugin_name = "CTMDbA"
    # 插件描述
    plugin_desc = "对 TMDb 上被合并为一季的番剧进行季信息分离。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/wikrin/MoviePilot-Plugins/main/icons/ctmdbanime.png"
    # 插件版本
    plugin_version = "1.4.0"
    # 插件作者
    plugin_author = "Attente"
    # 作者主页
    author_url = "https://github.com/wikrin"
    # 插件配置项ID前缀
    plugin_config_prefix = "curetmdbanime_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _contextvars = contextvars.ContextVar("recursion_flag", default=False)

    # 配置属性
    _enabled: bool = False
    _source: Optional[str] = ""
    _use_cont_eps: bool = False
    _clear_cache: bool = False

    CONFIG_KEYS = (
            "enabled",
            "source",
            "use_cont_eps",
            "clear_cache",
        )

    @contextmanager
    def no_recursion(self):
        """防递归上下文管理器"""
        token = self._contextvars.set(True)
        try:
            yield
        finally:
            self._contextvars.reset(token)

    @property
    def has_background_service(self) -> bool:
        if getattr(self, "splitter", None):
            return self.splitter.services
        return False

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()
        # 加载插件配置
        self.load_config(config)

        self.cache = SeasonCache(self._use_cont_eps)
        self.splitter = SeasonSplitter(self)
        self.scraper = TmdbScraper()
        if self._clear_cache:
            self._clear()
            self.__update_config()

    def load_config(self, config: dict):
        """加载配置"""
        if config:
            # 遍历配置中的键并设置相应的属性
            for key in self.CONFIG_KEYS:
                setattr(self, f"_{key}", config.get(key, getattr(self, f"_{key}")))

    def _clear(self):
        try:
            self.splitter.clear()
            self.cache.clear()
        except Exception as e:
            logger.error(f"缓存清理失败: {e}")
        finally:
            self._clear_cache = False

    def __update_config(self):
        """更新设置"""
        self.update_config({key: getattr(self, f"_{key}") for key in self.CONFIG_KEYS})

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        if self._enabled and self.has_background_service:
            return [
                {
                    "id": "CureTMDbA",
                    "name": "依赖数据更新",
                    "trigger": "interval",
                    "func": self.splitter.curetmdb.fetch_and_save_remote,
                    "kwargs": {"hours": 8},
                }
            ]
        return []

    def stop_service(self):
        """退出插件"""
        try:
            self.splitter.close()
        except Exception:
            pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_command(self):
        pass

    def get_form(self):
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'use_cont_eps',
                                            'label': '使用连续集号',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'clear_cache',
                                            'label': '清理缓存',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 9},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'source',
                                            'label': '来源',
                                            'placeholder': '本地路径或链接',
                                        },
                                    }
                                ],
                            },
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "source": "https://raw.githubusercontent.com/wikrin/CureTMDb/main/tv.json",
            "use_cont_eps": False,
            "clear_cache": False,
        }

    def get_page(self):
        pass

    def get_state(self):
        return self._enabled

    def get_module(self) -> Dict[str, Any]:
        """
        获取插件模块声明，用于胁持系统模块实现（方法名：方法实现）
        """
        return {
            # 识别媒体信息
            "recognize_media": self.on_recognize_media,
            "async_recognize_media": self.on_recognize_media,
            # tmdb信息
            "tmdb_info": self.on_tmdb_info,
            "async_tmdb_info": self.on_tmdb_info,
            # 媒体集信息
            "tmdb_episodes": self.on_tmdb_episodes,
            "async_tmdb_episodes": self.on_tmdb_episodes,
            # 季信息
            "tmdb_seasons": self.on_tmdb_seasons,
            "async_tmdb_seasons": self.on_tmdb_seasons,
            # 刮削元数据
            "metadata_nfo": self.on_metadata_nfo,
            # 刮削图片
            "metadata_img": self.on_metadata_img,
            # 文件整理
            "transfer": self.on_transfer,
        }

    def is_eligible(self, tmdbid: int = None, mtype: MediaType = None, episode_group: Optional[str] = None) -> bool:

        if self._contextvars.get():
            return False

        if settings.RECOGNIZE_SOURCE != "themoviedb":
            return False

        if episode_group:
            return False

        if mtype == MediaType.MOVIE:
            return False

        if tmdbid and not self.cache.get(tmdbid):
            return False

        return True

    def correct_meta(self, meta: MetaBase, mediainfo: MediaInfo) -> bool:
        """
        根据逻辑季信息调整元数据对象中的季号和集号。

        :param meta: 原始元数据对象
        :param mediainfo: 媒体信息对象
        """
        if not meta or not mediainfo:
            return False

        # 检查识别词是否已偏移集数
        if meta.apply_words:
            matched_word = next((word for word in meta.apply_words if " >> " in word and " <> " in word), None)
            if matched_word:
                logger.info(f"存在应用的集数偏移识别词 `{matched_word}`, 跳过调整元数据")
                return False

        episodes_map: dict[tuple, tuple[int, int]] = self.cache.org_map(mediainfo.tmdb_id)
        corrected = False

        def adjust_episode(is_begin: bool) -> bool:
            """调整单个集数信息"""
            episode_num = meta.begin_episode if is_begin else meta.end_episode
            if not episode_num:
                return False

            season_num = (meta.begin_season if is_begin else meta.end_season) or 1

            # TMDB 使用连续集号时
            if (season_num, episode_num) in episodes_map:
                logical_season, logical_episode = episodes_map[season_num, episode_num]
                if season_num == logical_season and episode_num == logical_episode:
                    return False
                if is_begin:
                    meta.begin_season = logical_season
                    meta.begin_episode = logical_episode
                else:
                    meta.end_season = logical_season if meta.end_season else None
                    meta.end_episode = logical_episode
                return True

            # TMDB 信息未更新时
            elif (
                mediainfo.number_of_episodes
                and season_num == mediainfo.number_of_seasons
                and len(mediainfo.seasons.get(season_num, [])) < episode_num
            ):
                logger.debug(f"{mediainfo.title_year} TMDb集数信息可能未更新, 不调整元数据")
                return False

            # 发布组使用连续集号, TMDB分季时
            elif (
                mediainfo.number_of_episodes
                and len(mediainfo.seasons.get(season_num, []))
                < episode_num
                <= mediainfo.number_of_episodes
            ):
                offset = 0
                for season_key, episodes_list in mediainfo.seasons.items():
                    if (found_episode := episode_num - offset) in episodes_list:
                        if is_begin:
                            meta.begin_season = season_key
                            meta.begin_episode = found_episode
                        else:
                            meta.end_season = season_key if meta.end_season else None
                            meta.end_episode = found_episode
                        return True
                    offset += len(episodes_list)

            return False

        # 调整 begin_episode 和 end_episode
        orig_season_episode = meta.season_episode
        corrected = adjust_episode(True) | adjust_episode(False)

        if corrected:
            logger.info(f"{mediainfo.title_year} 元数据季集已调整：{orig_season_episode} ==> {meta.season_episode}")

        return corrected

    def on_recognize_media(
        self,
        meta: MetaBase = None,
        mtype: MediaType = None,
        tmdbid: Optional[int] = None,
        episode_group: Optional[str] = None,
        cache: Optional[bool] = True,
        **kwargs,
    ) -> Optional[MediaInfo]:

        if not self.is_eligible(mtype=mtype, episode_group=episode_group):
            return None

        if not tmdbid and not meta:
            return None

        if meta and not tmdbid and not meta.name:
            return None

        with self.no_recursion():
            media_info = self.chain.recognize_media(
                meta=meta,
                tmdbid=tmdbid,
                mtype=mtype,
                episode_group=episode_group,
                cache=cache,
                **kwargs
            )
        # 识别失败，阻止run_module继续执行
        if media_info is None:
            return False
        # 只处理电视剧
        if media_info.type != MediaType.TV:
            return media_info

        if logic := self.splitter.from_tmdb(media_info) or self.cache.get(media_info.tmdb_id):
            self.cache.put(media_info.tmdb_id, logic)

            seasons_info = logic.seasons_info
            seasons = logic.seasons_eps(self._use_cont_eps)
            season_years = {}

            for info in seasons_info:
                season_number = info.get("season_number")
                season_years[season_number] = str(info.get("air_date")).split("-")[0]
            # 每季集清单
            if seasons:
                media_info.seasons = seasons
                media_info.number_of_seasons = len(seasons)
            # 每季年份
            if season_years:
                media_info.season_years = season_years
            if logic.name and media_info.title != logic.name:
                logger.info(f"{tmdbid} 标题已调整 {media_info.title} -> {logic.name}")
                media_info.title = logic.name

            media_info.season_info = seasons_info

        self.correct_meta(meta, media_info)
        return media_info

    def on_tmdb_info(self, tmdbid: int, mtype: MediaType, season: Optional[int] = None) -> Optional[dict]:
        """
        获取 TMDB 信息（支持电影/电视剧），并整合季/集信息。

        :param tmdbid: TMDB ID
        :param mtype: 媒体类型（MediaType.MOVIE / MediaType.TV）
        :param season: 季号（仅限电视剧）
        :return dict: 合并后的 TMDB 数据字典 或 None
        """

        if not self.is_eligible(tmdbid, mtype):
            return None

        # 获取真实季信息
        unique_seasons = self.cache.unique_seasons(tmdbid=tmdbid, season=season)

        # 收集所有相关季的信息
        episodes: List[Dict] = []
        latest_season_info: Dict = {}

        for logic_season in unique_seasons:
            if not logic_season:
                continue

            # 获取该季详细信息
            if logic_season.type == "movie":
                season_data = TmdbApi().get_info(
                    mtype=MediaType.MOVIE, tmdbid=logic_season.tmdbid
                )
            else:
                season_data = TmdbApi().get_tv_season_detail(
                    tmdbid=logic_season.tmdbid or tmdbid,
                    season=logic_season.season_number,
                )

            if not season_data:
                logger.warning(
                    f"无法获取 TMDB 季信息: {logic_season.tmdbid or tmdbid} - S{logic_season.season_number}"
                )
                continue

            # 提取集列表
            if season_data.get("season_number") is not None:
                episodes.extend(season_data.get("episodes", []))
                # 更新最新季信息
                if (
                    season_data.get("season_number", 0)
                    > latest_season_info.get("season_number", 0)
                ):
                    latest_season_info = season_data
            else:
                episodes.append(season_data)

        # 返回最终结果
        if not latest_season_info and not episodes:
            return None

        # 整合额外的季信息
        season_info = self.cache.season_info(tmdbid=tmdbid, season=season)

        eps = self.cache.org_to_logic(tmdbid, season)

        if not eps:
            return {**latest_season_info, **season_info}

        def convert_episode(ep: dict):
            raw_season = ep.pop("season_number", None)
            raw_episode = ep.pop("episode_number", None) or ep.get("id")
            _k = ("movie" if raw_season is None else raw_season), raw_episode

            if _k not in eps:
                return None

            logic_season, logic_episode = eps[_k]
            air_date = ep.pop("air_date", None) or ep.pop("release_date", None)
            name = ep.pop("name", None) or ep.pop("title", None)

            return {
                "air_date": air_date,
                "name": name,
                "season_number": logic_season,
                "episode_number": logic_episode,
                **ep,
            }

        # 过滤与替换
        filtered = [e for e in (convert_episode(ep) for ep in episodes) if e is not None]
        # 排序
        sorted_episodes = sorted(filtered, key=lambda x: x.get("episode_number", 0))

        return {
            **latest_season_info,
            **season_info,
            "episodes": sorted_episodes,
        }

    def on_tmdb_episodes(self, tmdbid: int, season: int, episode_group: Optional[str] = None) -> Optional[Tuple[schemas.TmdbEpisode]]:
        """
        根据TMDBID查询某季的所有集信息

        :param tmdbid:  TMDBID
        :param season:  季
        :param episode_group:  剧集组
        """
        if not self.is_eligible(tmdbid=tmdbid, episode_group=episode_group):
            return None

        tmdb_info = self.chain.tmdb_info(tmdbid, MediaType.TV, season)
        if not tmdb_info:
            return None
        return tuple(schemas.TmdbEpisode(**ep) for ep in tmdb_info.get("episodes", []))

    def on_metadata_nfo(self, meta: MetaBase, mediainfo: MediaInfo,
                     season: Optional[int] = None, episode: Optional[int] = None) -> Optional[str]:
        """
        获取NFO文件内容文本

        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        if not self.is_eligible(mediainfo.tmdb_id, mediainfo.type, mediainfo.episode_group):
            return None
        org_sea, org_ep, info = self.cache.org_season_episode(mediainfo.tmdb_id, season, episode)

        if season is not None:
            # 查询季信息
            seasoninfo = {
                **self.scraper.default_tmdb.get_tv_season_detail(
                    mediainfo.tmdb_id, org_sea
                ),
                **info,
            }
            if episode:
                # 集元数据文件
                episodeinfo = self.scraper._TmdbScraper__get_episode_detail(seasoninfo, org_ep)
                doc = self.scraper._TmdbScraper__gen_tv_episode_nfo_file(episodeinfo=episodeinfo, tmdbid=mediainfo.tmdb_id,
                                                        season=season, episode=episode)
            else:
                # 季元数据文件
                doc = self.scraper._TmdbScraper__gen_tv_season_nfo_file(seasoninfo=seasoninfo, season=season)
        else:
            # 电视剧元数据文件
            doc = self.scraper._TmdbScraper__gen_tv_nfo_file(mediainfo=mediainfo)
        if doc:
            return doc.toprettyxml(indent="  ", encoding="utf-8")  # noqa

        return None

    def on_metadata_img(self, mediainfo: MediaInfo, season: Optional[int] = None,
                         episode: Optional[int] = None) -> Optional[dict]:
        """
        获取图片名称和url

        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        if not self.is_eligible(mediainfo.tmdb_id, mediainfo.type, mediainfo.episode_group):
            return None

        images = {}
        season, episode, info = self.cache.org_season_episode(mediainfo.tmdb_id, season, episode)

        if season is not None:
            seasoninfo = {
                **self.scraper.original_tmdb(mediainfo).get_tv_season_detail(
                    mediainfo.tmdb_id, season
                ),
                **info,
            }
            # 只需要季集的图片
            if seasoninfo:
                # 集的图片
                if episode:
                    episodeinfo = self.scraper._TmdbScraper__get_episode_detail(seasoninfo, episode)
                    if episodeinfo and episodeinfo.get("still_path"):
                        # TMDB集still图片
                        still_name = f"{episode}"
                        still_url = f"https://{settings.TMDB_IMAGE_DOMAIN}/t/p/original{episodeinfo.get('still_path')}"
                        images[still_name] = still_url
                else:
                    # TMDB季poster图片
                    poster_name, poster_url = self.scraper.get_season_poster(seasoninfo, season)
                    if poster_name and poster_url:
                        images[poster_name] = poster_url
            return images
        else:
            # 获取媒体信息中原有图片（TheMovieDb或Fanart）
            for attr_name, attr_value in vars(mediainfo).items():
                if attr_value \
                        and attr_name.endswith("_path") \
                        and attr_value \
                        and isinstance(attr_value, str) \
                        and attr_value.startswith("http"):
                    image_name = attr_name.replace("_path", "") + Path(attr_value).suffix
                    images[image_name] = attr_value
            # 替换原语言Poster
            if settings.TMDB_SCRAP_ORIGINAL_IMAGE:
                _mediainfo = self.scraper.original_tmdb(mediainfo).get_info(mediainfo.type, mediainfo.tmdb_id)
                if _mediainfo:
                    for attr_name, attr_value in _mediainfo.items():
                        if attr_name.endswith("_path") and attr_value is not None:
                            image_url = f"https://{settings.TMDB_IMAGE_DOMAIN}/t/p/original{attr_value}"
                            image_name = attr_name.replace("_path", "") + Path(image_url).suffix
                            images[image_name] = image_url
            return images

    def on_tmdb_seasons(self, tmdbid: int):
        if not self.is_eligible(tmdbid=tmdbid):
            return None

        if logic := self.cache.get(tmdbid):
            return tuple( # 返回Tuple 终止run_module执行
                schemas.TmdbSeason(**sea.to_dict())
                for sea in logic.seasons
                if sea.season_number
            )

    def on_transfer(self, meta: MetaBase, mediainfo: MediaInfo, **kwargs):
        """
        文件整理

        :param meta: 预识别的元数据
        :param mediainfo:  识别的媒体信息
        """
        if mediainfo.type != MediaType.TV:
            return None

        if self.correct_meta(meta, mediainfo) and self.is_eligible(
            tmdbid=mediainfo.tmdb_id,
            mtype=mediainfo.type,
            episode_group=mediainfo.episode_group,
        ):
            kwargs["episodes_info"] = self.on_tmdb_episodes(
                mediainfo.tmdb_id,
                meta.begin_season,
            )
            with self.no_recursion():
                return self.chain.transfer(meta=meta, mediainfo=mediainfo, **kwargs)
