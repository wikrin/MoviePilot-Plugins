import re
import cn2an
import requests
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

from app.core.cache import cached
from app.log import logger
from app.utils.http import RequestUtils

from .models import SeasonEntry, SeriesEntry


class BangumiAPIClient:
    """
    https://bangumi.github.io/api/
    """

    _urls = {
        "discover": "v0/subjects",
        "search": "v0/search/subjects",
        "detail": "v0/subjects/%s",
        "subjects": "v0/subjects/%s/subjects",
        "episodes": "v0/episodes?subject_id=%s"
    }
    _base_url = "https://api.bgm.tv/"

    def __init__(self):
        self._session = requests.Session()
        self._req = RequestUtils(session=self._session)

    @cached(maxsize=1024, ttl=60 * 60 * 6)
    def __invoke(self, method, url, key: Optional[str] = None, data: Any = None, json: dict = None, **kwargs):
        req_url = self._base_url + url
        req_method = {
            "get": self._req.get_res,
            "post": self._req.post_res,
            "put": self._req.put_res,
            "request": self._req.request
        }
        params = {}
        if kwargs:
            params.update(kwargs)
        resp = req_method[method](url=req_url, params=params, data=data, json=json)
        try:
            if not resp:
                return None
            result = resp.json()
            return result.get(key) if key else result
        except Exception as e:
            print(e)
            return None

    def search(self, title: str, air_date: str):
        """
        搜索媒体信息
        """
        if not title or not air_date:
            return []

        _air_date = datetime.strptime(air_date, "%Y-%m-%d").date()
        start_date = _air_date - timedelta(days=10)
        end_date = _air_date + timedelta(days=10)
        json = {
                "keyword": re.sub(r"[\u2000-\u206f\u3000-\u303f\uff00-\uffef\W_]", "", title),
                "sort": "match",
                "filter": {
                    "type": [2],
                    "air_date": [f">={start_date}", f"<={end_date}"]
                },
            }
        if result := self.__invoke("post", self._urls["search"], json=json):
            return result.get("data") or []
        return []

    def detail(self, bid: int) -> Optional[dict]:
        """
        获取番剧详情
        """
        return self.__invoke("get", self._urls["detail"] % bid, _ts=datetime.strftime(datetime.now(), '%Y%m%d'))

    def subjects(self, bid: int):
        """
        获取关联条目信息
        """
        return self.__invoke("get", self._urls["subjects"] % bid, _ts=datetime.strftime(datetime.now(), '%Y%m%d'))

    def episodes(self, bid: int, type: int = 0, limit: int = 1, offset: int = 0):
        """
        获取所有集信息
        """
        kwargs = {k: v for k, v in locals().items() if k not in ("self", "bid")}
        return self.__invoke("get", self._urls["episodes"] % bid, _ts=datetime.strftime(datetime.now(), '%Y%m%d'), **kwargs)

    def get_all_sequels(self, bid: int) -> List[int]:
        """
        递归获取指定 Bangumi 条目及其所有续集条目
        :param bid: 初始 Bangumi ID
        :return: 包含所有续集关系的嵌套结构
        """
        result = []

        def _recursive_fetch(current_bid: int) -> Dict[str, Any]:
            result.append(current_bid)
            # 获取关联条目并查找续集
            related_subjects = self.subjects(current_bid)
            if not related_subjects:
                return

            for item in related_subjects:
                if item.get("relation") == "续集":
                    _recursive_fetch(item["id"])

        _recursive_fetch(bid)

        logger.debug(f"{bid} 获取到关联条目数 {len(result)} : {result}")
        return result

    def season_info(self, item: dict) -> Optional[SeriesEntry]:
        if not item:
            return None
        bgm_seasons: dict[int, SeasonEntry] = {}
        try:
            sids = self.get_all_sequels(item["id"])
            for sid in sids:
                detail = self.detail(sid)
                if not detail or detail.get("platform") == "剧场版":
                    continue
                name = detail.get("name")
                name_cn = detail.get("name_cn")
                num = self.extract_season_number(name, name_cn)
                eps = detail.get("eps", 0)
                if num not in bgm_seasons:
                    bgm_seasons[num] = SeasonEntry(
                        episode_count=bgm_seasons.get(num, 0) + eps, name=name_cn,
                        season_number=num
                    )
                elif result := self.get_sort_and_ep(sid):
                    if result[0] == result[1]:
                        num = max(bgm_seasons.keys(), default=0) + 1
                        bgm_seasons[num] = SeasonEntry(
                            episode_count=bgm_seasons.get(num, 0) + eps, name=name_cn,
                            season_number=num
                        )
                    else:
                        bgm_seasons[num].episode_count += eps

            return SeriesEntry(seasons=list(bgm_seasons.values())) if len(bgm_seasons) >= 2 else None

        except Exception as e:
            logger.error(f"构建季信息失败: {str(e)}")
            return None

    def get_sort_and_ep(self, sid: int) -> Optional[Tuple[int, int]]:
        """
        获取 Bangumi 条目中的 sort 和 ep 值。

        :param sid: Bangumi 条目 ID
        :return: (sort, ep) 元组，若失败则返回 None
        """
        if not (result := self.episodes(sid)):
            return None

        try:
            episode = result["data"][0]
            sort = episode.get("sort")
            ep = episode.get("ep")

            logger.debug(f"获取 {sid} 的 sort 和 ep 值: {sort}, {ep}")

            if sort is None or ep is None:
                return None

            return sort, ep
        except (IndexError, KeyError, TypeError):
            return None

    @staticmethod
    def extract_season_number(name: str, name_cn: str) -> int:
        """
        提取季号，优先从 name_cn 获取，否则从 name 获取。
        如果两者都有结果，且一致则使用；否则以 name_cn 为准。
        都没有则默认为 1。
        """

        def _parse(text: str):
            if not text:
                return None
            # 第x季、第x期、x季全
            match = re.search(r"[第\s]*([一二三四五六七八九十ⅠⅡⅢⅣ0-9]+)\s*(?:季|期)", text, re.IGNORECASE)
            if match:
                season_str = match.group(1).strip()
                try:
                    return int(cn2an.cn2an(season_str, mode='smart'))
                except Exception:
                    pass

            # Season x
            match = re.search(r"Season\s*([0-9ⅠⅡⅢⅣ]+)", text, re.IGNORECASE)
            if match:
                season_str = match.group(1).strip()
                try:
                    return int(cn2an.cn2an(season_str, mode='smart'))
                except Exception:
                    pass

            # 2nd season
            match = re.search(r"([0-9]{1,2})(?:st|nd|rd|th)\s+season", text, re.IGNORECASE)
            if match:
                season_str = match.group(1).strip()
                try:
                    return int(season_str)
                except Exception:
                    pass

            return None

        cn_result = _parse(name_cn) if name_cn else None
        en_result = _parse(name) if name else None

        if cn_result and en_result:
            # 两者都匹配到，若一致则使用，否则优先用中文
            return cn_result
        elif cn_result:
            return cn_result
        elif en_result:
            return en_result
        else:
            # 都没找到，默认第一季
            return 1

    def clear(self):
        """
        清除cached缓存
        """
        self.__invoke.cache_clear()

    def close(self):
        if self._session:
            self._session.close()

