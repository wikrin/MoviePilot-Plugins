import sys
from typing import Optional, Callable
from urllib.parse import urlparse

from httpx import _client, _models
from requests.sessions import Session

from app.core.cache import Cache
from app.core.config import settings
from app.core.context import MediaInfo, TorrentInfo
from app.chain.transfer import JobManager
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.modules.themoviedb.tmdbv3api.tmdb import TMDb
from app.utils.string import StringUtils


class MonkeyPatchManager:
    """
    通用猴子补丁管理器
    负责在插件启用时应用补丁，在插件停用时恢复原方法
    """

    def __init__(self):

        self.CACHE_REGION = "curetmdbanime:torrent_pubdate"
        self.cache_backend = Cache(
            maxsize=2 * settings.CONF.torrents, ttl=settings.CONF.meta
        )
        # 存储已打补丁的方法的原始信息：{(target_class, method_name): original_method}
        self._original_methods = {}
        # 标记是否有任何补丁被应用
        self._is_patched = False
        # 存储不需要代理的 URL 字符串
        self._no_proxy_urls: list[str] = []

    def add_no_proxy_url(self, url: str):
        """
        将 URL 添加到不需要代理的列表中。
        """
        if url not in self._no_proxy_urls:
            self._no_proxy_urls.append(url)
            logger.debug(f"已添加不需要代理的URL: {url}")

    def remove_no_proxy_url(self, url: str):
        """
        从不需要代理的列表中移除 URL。
        """
        if url in self._no_proxy_urls:
            self._no_proxy_urls.remove(url)
            logger.debug(f"已移除不需要代理的URL: {url}")

    def _should_bypass_no_proxy_url(self, url: str) -> bool:
        """
        检查给定 URL 是否在 _no_proxy_urls 列表中，以确定是否绕过代理。
        """
        parsed_url = urlparse(url)
        hostname = parsed_url.hostname
        port = parsed_url.port

        for no_proxy_url in self._no_proxy_urls:
            parsed_no_proxy_url = urlparse(no_proxy_url)
            # 比较主机和端口
            if (
                hostname == parsed_no_proxy_url.hostname
                and port == parsed_no_proxy_url.port
            ):
                logger.debug(
                    f"URL {url} 匹配不需要代理的URL {no_proxy_url}, 将绕过代理。"
                )
                return True
        return False

    @staticmethod
    def _retarget_method_filename(new_method, original_method):
        """
        将补丁方法的 co_filename 对齐到原始方法
        """
        original_func = getattr(original_method, "__func__", original_method)
        target_filename = getattr(
            getattr(original_func, "__code__", None), "co_filename", None
        )
        if not target_filename:
            return new_method

        is_static = isinstance(new_method, staticmethod)
        is_class = isinstance(new_method, classmethod)
        patch_func = getattr(new_method, "__func__", new_method)
        patch_code = getattr(patch_func, "__code__", None)

        if not patch_code or not hasattr(patch_code, "replace"):
            return new_method

        try:
            patch_func.__code__ = patch_code.replace(co_filename=target_filename)
        except Exception as e:
            logger.debug(f"方法文件名对齐失败，保留原补丁实现: {e}")
            return new_method

        if is_static:
            return staticmethod(patch_func)
        if is_class:
            return classmethod(patch_func)
        return patch_func

    def patch(self, target_class, method_name: str, new_method):
        """
        通用猴子补丁。

        Args:
            target_class: 要打补丁的类。
            method_name: 要打补丁的方法名。
            new_method: 用于替换原始方法的新方法。
        """
        patch_key = (target_class, method_name)
        if patch_key in self._original_methods:
            logger.info(f"方法 {target_class.__name__}.{method_name} 跳过重复操作")
            return

        original_method = getattr(target_class, method_name)

        self._original_methods[patch_key] = original_method

        patched_method = self._retarget_method_filename(new_method, original_method)
        setattr(target_class, method_name, patched_method)
        logger.debug(f"方法 {target_class.__name__}.{method_name} 已补丁")
        self._is_patched = True

    def unpatch_all(self):
        """
        恢复所有方法到原始状态。
        """
        if not self._is_patched and not self._original_methods:
            logger.info("没有应用的补丁需要恢复")
            return

        for (target_class, method_name), original_method in list(
            self._original_methods.items()
        ):
            setattr(target_class, method_name, original_method)
            logger.debug(f"方法 {target_class.__name__}.{method_name} 已恢复")

        self._original_methods.clear()
        self._is_patched = False
        self._no_proxy_urls.clear()
        logger.info("所有补丁和 no_proxy_urls 已恢复/清除")

    def patch_requests(self):

        original_merge_environment_settings = Session.merge_environment_settings

        def new_merge_environment_settings(
            instance, url, proxies, stream, verify, cert
        ):
            if self._should_bypass_no_proxy_url(url):
                proxies = None
            return original_merge_environment_settings(
                instance, url, proxies, stream, verify, cert
            )

        self.patch(
            Session, "merge_environment_settings", new_merge_environment_settings
        )

    def patch_httpx(self):

        original_sync_transport_for_url = _client.Client._transport_for_url
        original_async_transport_for_url = _client.AsyncClient._transport_for_url

        def new_sync_transport_for_url(instance: _client.Client, url: _models.URL):
            if self._should_bypass_no_proxy_url(str(url)):
                return instance._transport
            return original_sync_transport_for_url(instance, url)

        def new_async_transport_for_url(
            instance: _client.AsyncClient, url: _models.URL
        ):
            if self._should_bypass_no_proxy_url(str(url)):
                return instance._transport
            return original_async_transport_for_url(instance, url)

        self.patch(_client.Client, "_transport_for_url", new_sync_transport_for_url)
        self.patch(
            _client.AsyncClient, "_transport_for_url", new_async_transport_for_url
        )

    def patch_build_url(self, port: int):

        tmdb_local_url = f"http://127.0.0.1:{port}"
        self.add_no_proxy_url(tmdb_local_url)

        def new_build_url(instance: TMDb, action, params=""):
            return "%s/3%s?api_key=%s&%s&language=%s" % (
                tmdb_local_url,
                action,
                instance.api_key,
                params,
                instance.language,
            )

        self.patch(TMDb, "_build_url", new_build_url)

        self.patch_requests()
        self.patch_httpx()

    def patch_meta_enhancement(self, func: Callable):
        """
        在关键节点注入自定义元数据处理逻辑
        """
        self.patch_torrent_info_cache()
        self.patch_job_manager(func)
        self.patch_torrent_helper(func)
        self.patch_mediainfo(func)

    def patch_torrent_info_cache(self):
        """
        补丁 TorrentInfo 类，自动将 title/description/pubdate 的映射关系存入缓存
        优化点：处理赋值顺序导致的重复记录问题，确保缓存键的唯一性和准确性。
        """
        method_name = "__setattr__"

        def _make_key(title: str, desc: Optional[str]) -> str:
            """生成统一的缓存键"""
            return StringUtils.md5_hash(f"{title}{desc}")

        def _update_cache(instance: TorrentInfo):
            """
            更新缓存：
            1. 如果 title/desc 变了，删除旧的可能存在的键（清理垃圾）
            2. 如果 title/pubdate 齐备，写入新键
            """
            try:
                title = instance.__dict__.get("title")
                desc = instance.__dict__.get("description")
                pubdate = instance.__dict__.get("pubdate")

                # 核心条件：必须有标题和发布时间
                if not title or not pubdate:
                    return

                new_key = _make_key(title, desc)

                # 检查是否已经存在相同的值，避免重复日志/IO
                existing_val = self.cache_backend.get(new_key, region=self.CACHE_REGION)
                if existing_val == pubdate:
                    return

                self.cache_backend.set(new_key, pubdate, region=self.CACHE_REGION)
                logger.debug(
                    f"[TorrentCache] Recorded: Key={new_key[:10]}... PubDate={pubdate}"
                )

            except Exception as e:
                logger.error(f"[TorrentCache] Failed to update cache: {e}")

        # 创建新的 __setattr__
        original_setattr = getattr(TorrentInfo, method_name)

        def new_setattr(instance, name: str, value: any):
            # 执行原始赋值逻辑
            original_setattr(instance, name, value)

            # 如果修改的是关键字段，尝试更新缓存
            if name in ["title", "description", "pubdate"]:
                _update_cache(instance)

        # 应用补丁
        self.patch(TorrentInfo, method_name, new_setattr)

    def get_torrent_pubdate(
        self, title: Optional[str], description: Optional[str] = None
    ) -> Optional[str]:
        """
        外部查询接口：通过标题和描述获取缓存的发布时间
        """
        if not title:
            return None

        def _make_key(title: str, desc: Optional[str]) -> str:
            return StringUtils.md5_hash(f"{title}{desc}")

        # 优先尝试精确匹配（使用传入的描述，如果没有传则为 None/Empty）
        key = _make_key(title, description)
        pubdate = self.cache_backend.get(key, region=self.CACHE_REGION)

        if pubdate:
            return pubdate

        # 降级策略：如果传了描述但没查到，且描述不为空，尝试查“无描述”的版本
        if description:
            empty_desc_key = _make_key(title, None)
            return self.cache_backend.get(empty_desc_key, region=self.CACHE_REGION)

        return None

    def patch_job_manager(self, func: Callable):

        original_add_task = JobManager.add_task

        def new_add_task(instance, task, *args, **kwargs):
            task.meta = func(task.meta, task.mediainfo)
            return original_add_task(instance, task, *args, **kwargs)

        self.patch(JobManager, "add_task", new_add_task)

    def patch_torrent_helper(self, func: Callable):

        original_match_torrent = TorrentHelper.match_torrent
        original_match_season_episodes = TorrentHelper.match_season_episodes

        @staticmethod
        def new_match_torrent(mediainfo, torrent_meta, torrent) -> bool:
            # 补丁此方法用于非订阅搜索

            if result := original_match_torrent(
                mediainfo=mediainfo, torrent_meta=torrent_meta, torrent=torrent
            ):
                func(torrent_meta, mediainfo)
            return result

        @staticmethod
        def new_match_season_episodes(torrent, meta, season_episodes) -> bool:
            frame = sys._getframe(1)
            if mediainfo := frame.f_locals.get("mediainfo"):
                func(meta, mediainfo)
            return original_match_season_episodes(
                torrent=torrent, meta=meta, season_episodes=season_episodes
            )

        self.patch(TorrentHelper, "match_torrent", new_match_torrent)
        self.patch(TorrentHelper, "match_season_episodes", new_match_season_episodes)

    def patch_mediainfo(self, func: Callable):

        original_set_category = MediaInfo.set_category

        def new_set_category(instance, cat: str):
            frame = sys._getframe(1)
            if meta := frame.f_locals.get("meta"):
                func(meta, instance)
            return original_set_category(instance, cat)

        self.patch(MediaInfo, "set_category", new_set_category)

    def is_patched(self):

        return self._is_patched and bool(self._original_methods)
