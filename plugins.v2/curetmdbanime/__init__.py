# 基础库
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Dict, List, Tuple

# 项目库
from app import schemas
from app.core.cache import cached
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta.metabase import MetaBase
from app.log import logger
from app.modules.themoviedb.scraper import TmdbScraper
from app.plugins import _PluginBase
from app.schemas.types import MediaType

from .bangumi import BangumiAPIClient
from .curetmdb import CureTMDb
from .models import EpisodeMap, SeriesEntry, LogicSeason, LogicSeries


class SeasonCache:
    def __init__(self, use_cont_eps: bool = False):
        self._cache: Dict[int, LogicSeries] = {}
        self.use_cont_eps = use_cont_eps

    def put(self, tmdbid: int, series: LogicSeries):
        self._cache[tmdbid] = series

    def get(self, tmdbid: int) -> Optional[LogicSeries]:
        return self._cache.get(tmdbid)

    def _get_logic_season(self, tmdbid: int, season: int) -> Optional[LogicSeason]:
        """获取指定季的信息，若不存在则返回 None"""
        if series := self.get(tmdbid):
            return next((s for s in series.seasons if s.season_number == season), None)

    def org_season_episode(self, tmdbid: int, season: int, episode: int = None) -> Tuple[int, int, dict]:
        """
        根据逻辑季信息解析出原始季号和集号。
        如果没有逻辑季，则尝试获取第一个 tv 类型的原始季号。
        :param tmdbid: 媒体 TMDB ID
        :param season: 用户提供的季号
        :param episode: 用户提供的集号（可选）
        :return: (original_season, original_episode, seasoninfo)
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

    def org_map(self, tmdbid: int) -> Optional[Dict[tuple, tuple[int, int]]]:
        """
        获取原始季映射关系
        :param tmdbid: 媒体 TMDB ID
        :param use_cont_eps: 是否使用连续集号
        """
        if logic := self.get(tmdbid):
            return logic.org_map(self.use_cont_eps)

    def unique_seasons(self, tmdbid: int, season: int) -> list[EpisodeMap]:
        logic = self._get_logic_season(tmdbid, season)
        if not logic:
            return []
        return logic.unique_entry

    def clear(self):
        self._cache.clear()


class SeasonSplitter:

    def __init__(self, ctmdb: "CureTMDbAnime"):
        self.ctmdb = ctmdb
        self.curetmdb = CureTMDb(self.ctmdb._source)
        self.bgm = BangumiAPIClient()

    @cached(maxsize=500, ttl=60 * 60 * 8, skip_empty=True)
    def seasons(self, mediainfo: MediaInfo) -> Optional[SeriesEntry]:
        # 优先使用本地
        seasons = self.curetmdb.season_info(mediainfo.tmdb_id)

        if all(
            [
                not seasons,
                mediainfo.category == self.ctmdb._category,
                mediainfo.number_of_seasons and mediainfo.number_of_seasons < 3,
            ]
        ):
            # 如果未找到季信息且媒体信息显示有2季
            if mediainfo.number_of_seasons == 2:
                # 查找Bangumi条目并验证是否符合合并条件
                item = self._search_by_mediainfo(mediainfo, season=2)
                result = item and self.bgm.get_sort_and_ep(item["id"])
                if result and result[0] == result[1]:
                    return

            # 若仍未找到季信息，则从Bangumi获取
            item = self._search_by_mediainfo(mediainfo)
            seasons = self.bgm.season_info(item)

        return seasons

    @cached(maxsize=100, ttl=500, skip_empty=True)
    def from_tmdb(self, mediainfo: MediaInfo):
        """
        根据 TMDB 和 Bangumi 数据构建逻辑季信息。
        """
        if seasons := self.seasons(mediainfo):
            # 拆分季
            logic_series = self._logic_seasons(mediainfo, seasons)
            return logic_series

    def _search_by_mediainfo(self, mediainfo: MediaInfo, season: Optional[int] = None) -> Optional[dict]:
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
        try:
            result = self.bgm.search(mediainfo.original_title, air_date)
            return next((item for item in result if item.get("platform") == "TV"), None)
        except Exception as e:
            logger.error(f"Bangumi search error: {e}")

    def _logic_seasons(self, mediainfo: MediaInfo, series: SeriesEntry) -> LogicSeries:
        seasons: list[LogicSeason] = []
        logic_series = LogicSeries(name=series.name)

        def append(name: str, mapping: dict[int, EpisodeMap]):
            if logic_series.has_season(current_season) and self.is_date_diff_within(
                air_date, logic_series.season_info(current_season).air_date
            ):
                logger.info(f"忽略重复的季: {current_season}: 已存在总集数: {seasons[current_season].episode_count}, 新的总集数: {len(mapping)}")
                return  # 保留集数多的

            logger.info(f"添加季: {current_season}, 总集数: {len(mapping)}")
            logic_series.add_season(
                    name=name,
                    air_date=air_date,
                    season_number=current_season,
                    episodes_map=mapping,
                    **mediainfo.to_dict()
                )

        mapping = series.episode_mapping
        missing_eps = series.missing_episodes_by_season
        current_season = series.min_season
        max_season = series.max_season

        tmdb_seasons = []
        for season in range(current_season, mediainfo.number_of_seasons + 1):
            try:
                if tmdb_season := self.ctmdb.chain.tmdb_info(mediainfo.tmdb_id, mediainfo.type, season):
                    tmdb_seasons.append(tmdb_season)
            except Exception as e:
                logger.error(f"获取第 {season} 季信息失败: {str(e)}")
        if not tmdb_seasons:
            return None
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
                    if missing_eps[current_season]
                    else None
                )

                if current_season not in mapping:
                    mapping[current_season] = {}

                if logic_ep:
                    mapping[current_season][logic_ep] = EpisodeMap(
                        order=logic_ep, **ep
                    )

                if any([
                    not missing_eps[current_season],
                    # ep.get("episode_type") == "finale",
                    uniqueid == last_episode
                ]):
                    append(series.season_names[current_season], mapping=mapping[current_season])
                    current_season += 1
                    air_date = None

                if current_season > max_season:
                    return logic_series

        return logic_series

    @staticmethod
    def is_date_diff_within(date_str1: str, date_str2: str, days_range: int = 3) -> bool:
        """
        判断两个日期字符串之间的差是否在指定天数范围内。

        :param date_str1: 第一个日期字符串，格式应为 "YYYY-MM-DD"
        :param date_str2: 第二个日期字符串，格式应为 "YYYY-MM-DD"
        :param days_range: 指定的天数范围（绝对值比较）
        :return: 如果时间差小于等于 days_range 天，则返回 True，否则返回 False
        """
        try:
            date1 = datetime.strptime(date_str1, "%Y-%m-%d")
            date2 = datetime.strptime(date_str2, "%Y-%m-%d")
            diff = abs((date2 - date1).days)
            return diff <= days_range
        except (ValueError, TypeError) as e:
            # 日期格式错误或无法解析
            logger.error(f"日期格式错误: {e}")
            return False

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
    plugin_version = "1.2.0"
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
    _local = threading.local()

    # 配置属性
    _enabled: bool = False
    _category: Optional[str] = "日番"
    _source: Optional[str] = ""
    _use_cont_eps: bool = False

    @property
    def flag(self) -> bool:
        """获取 flag 属性是否存在"""
        return getattr(type(self)._local, "flag", False)

    @flag.setter
    def flag(self, value: bool):
        """设置 flag 属性"""
        setattr(type(self)._local, "flag", value)

    @flag.deleter
    def flag(self):
        """删除 flag 属性"""
        if hasattr(type(self)._local, "flag"):
            delattr(type(self)._local, "flag")

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

    def load_config(self, config: dict):
        """加载配置"""
        if config:
            # 遍历配置中的键并设置相应的属性
            for key in (
                "enabled",
                "category",
                "source",
                "use_cont_eps",
            ):
                setattr(self, f"_{key}", config.get(key, getattr(self, f"_{key}")))

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
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_command(self):
        pass

    def get_form(self):
        from app.modules.themoviedb.category import CategoryHelper
        tv_categories = list(map(lambda cat: {"title": cat, "value": cat}, CategoryHelper().tv_categorys))
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
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
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
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
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'category',
                                            'label': 'Bangumi兜底类别',
                                            'items': tv_categories,
                                        },
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 12},
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
            "category" : "日番",
            "source": "https://raw.githubusercontent.com/wikrin/CureTMDb/main/tv.json",
            "use_cont_eps": False,
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
            # 媒体集信息
            "tmdb_episodes": self.on_tmdb_episodes,
            # 刮削元数据
            "metadata_nfo": self.on_metadata_nfo,
            # 刮削图片
            "metadata_img": self.on_metadata_img,
            # 季信息
            "tmdb_seasons": self.on_tmdb_seasons,
        }

    def is_eligible(self, tmdbid: int = None, mtype: MediaType = None, episode_group: Optional[str] = None) -> bool:
        if self.flag:
            return False

        if settings.RECOGNIZE_SOURCE != "themoviedb":
            return False

        if episode_group:
            return False

        if mtype == MediaType.MOVIE:
            return False

        return True

    def correct_meta(self, meta: MetaBase, mediainfo: MediaInfo):
        """
        根据逻辑季信息矫正元数据对象中的季号和集号。

        :param meta: 原始元数据对象
        :param mediainfo: 媒体信息对象
        """
        if not meta or not mediainfo:
            return

        if _map := self.cache.org_map(mediainfo.tmdb_id):

            # 尝试基于指定的 begin_season 查找匹配
            self._correct_by_specified(meta, _map)

    def _correct_by_specified(
        self,
        meta: MetaBase,
        episodes_map: dict[tuple, tuple[int, int]]
    ) -> bool:
        """
        尝试基于用户提供的 begin_season 来校正元数据。
        :return: 是否成功进行了校正
        """
        if not episodes_map:
            return False

        begin_sea = meta.begin_season or 1
        corrected = False

        # 校正 begin_episode 的季号与集号
        if meta.begin_episode and (begin_sea, meta.begin_episode) in episodes_map:
            sea, ep = episodes_map[begin_sea, meta.begin_episode]
            meta.begin_season = sea
            meta.begin_episode = ep
            corrected = True

        # 处理 end_episode（若存在）
        end_sea = meta.end_season or begin_sea
        if meta.end_episode and (end_sea, meta.end_episode) in episodes_map:
            sea, ep = episodes_map[end_sea, meta.end_episode]
            meta.end_season = sea if meta.end_season else None
            meta.end_episode = ep
            corrected = True

        if corrected:
            logger.info(f"元数据季集已调整：{meta.season_episode}")

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

        try:
            self.flag = True
            media_info = self.chain.recognize_media(
                meta=meta,
                tmdbid=tmdbid,
                mtype=mtype,
                episode_group=episode_group,
                cache=cache,
                **kwargs
            )
        except Exception as e:
            logger.error(f"识别媒体信息出错：{e}")
            media_info = None
        finally:
            del self.flag
        # 识别失败不处理
        if media_info is None:
            return None

        if logic := self.splitter.from_tmdb(media_info):
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
            media_info.season_info = seasons_info
            self.correct_meta(meta, media_info)

        return media_info

    def on_tmdb_episodes(self, tmdbid: int, season: int, episode_group: Optional[str] = None) -> Tuple[schemas.TmdbEpisode]:
        """
        根据TMDBID查询某季的所有集信息
        :param tmdbid:  TMDBID
        :param season:  季
        :param episode_group:  剧集组
        """
        if not self.is_eligible(episode_group=episode_group):
            return None

        unique_seasons = self.cache.unique_seasons(tmdbid, season)
        tmdb_info = [
            info
            for entry in unique_seasons
            if (
                info := self.chain.tmdb_info(
                    tmdbid=entry.tmdbid or tmdbid,
                    mtype=MediaType.MOVIE if entry.type == "movie" else MediaType.TV,
                    season=entry.season_number,
                )
            )
        ]
        if not tmdb_info:
            return None
        eps = self.cache.org_to_logic(tmdbid, season)
        if not eps:
            return None

        return (
            schemas.TmdbEpisode(
                **{
                    k: v
                    for k, v in ep.items()
                    if k not in {"season_number", "episode_number"}
                },
                season_number=season,
                episode_number=eps[_k][1],
            )
            for info in tmdb_info
            for ep in info.get("episodes") or [info]
            if (
                _k := (
                    ep.get("season_number") or "movie",
                    ep.get("episode_number") or ep["id"],
                )
            )
            in eps
        )

    def on_metadata_nfo(self, meta: MetaBase, mediainfo: MediaInfo,
                     season: Optional[int] = None, episode: Optional[int] = None) -> Optional[str]:
        """
        获取NFO文件内容文本
        :param meta: 元数据
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        if not self.is_eligible(mtype=mediainfo.type):
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
                         episode: Optional[int] = None) -> dict:
        """
        获取图片名称和url
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        if not self.is_eligible(mtype=mediainfo.type):
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
        if not self.is_eligible():
            return None

        if logic := self.cache.get(tmdbid):
            return ( # 返回Tuple 终止run_module执行
                schemas.TmdbSeason(**sea.to_dict())
                for sea in logic.seasons
                if sea.season_number
            )
