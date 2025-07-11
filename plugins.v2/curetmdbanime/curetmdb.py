import json
from pathlib import Path
from typing import Optional

from app.core.config import settings
from app.log import logger
from app.utils.http import RequestUtils

from .models import SeriesEntry


SAVE_PATH = settings.TEMP_PATH / "curetmdb.json"


class CureTMDb:

    _url = ""

    def __init__(self, source: str):
        path = source.strip() if source else ""
        self.is_remote = path.startswith("http://") or path.startswith("https://")
        if self.is_remote:
            self.path = SAVE_PATH
            self._url = path
            self.fetch_and_save_remote(False)
        else:
            self.path = Path(path)

    def fetch_and_save_remote(self, force: bool = True) -> bool:
        """
        如果是远程地址，尝试获取远程 JSON 并保存到本地。
        适用于 GitHub Raw 或其他提供 JSON 的 URL。
        """
        if not self.is_remote:
            logger.debug("当前路径不是远程地址，跳过下载")
            return True  # 非远程视为成功

        if SAVE_PATH.exists() and not force:
            return None

        try:
            logger.info(f"正在从远程地址加载季信息：{self._url}")
            resp = RequestUtils(proxies=settings.PROXY).get_res(self._url)
            if not resp:
                return False

            data = resp.json()
            with open(SAVE_PATH, 'w', encoding='utf-8') as f:
                json.dump(data, f)

            logger.info(f"远程数据已保存至本地：{SAVE_PATH}")
            return True
        except Exception as e:
            logger.error(f"获取远程季信息失败: {str(e)}")
            return False

    def season_info(self, tmdbid: int) -> Optional[SeriesEntry]:
        """
        从本地 JSON 加载季信息，用于替代 Bangumi 查询。

        :param mediainfo: 媒体信息对象
        :return: 构建好的 bgm_seasons 字典（格式：{season_num: episode_count}）
        """
        if not self.path.exists():
            logger.warning("信息文件不存在")
            return None

        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                local_data = json.load(f)
            tmdbid = str(tmdbid)
            if tmdbid not in local_data:
                logger.debug(f"未在本地季信息中找到 TMDB ID {tmdbid}")
                return None

            return SeriesEntry(**local_data[tmdbid])

        except Exception as e:
            logger.error(f"读取本地季信息失败: {str(e)}")
            return None

    @property
    def remote_mode(self) -> bool:
        return getattr(self, "is_remote", False)
