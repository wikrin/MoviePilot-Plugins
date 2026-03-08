import contextvars
import select
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional, Dict, List

import psutil

from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta.metabase import MetaBase
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import MediaType
from app.utils.http import RequestUtils
from app.utils.system import SystemUtils

from .patch import MonkeyPatchManager


class CureTMDbAnime(_PluginBase):
    # 插件名称
    plugin_name = "CTMDbA"
    # 插件描述
    plugin_desc = "对 TMDb 上被合并为一季的番剧进行季信息分离。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/wikrin/MoviePilot-Plugins/main/icons/ctmdbanime.png"
    # 插件版本
    plugin_version = "2.0.1"
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
    # 二进制文件
    binary_name = "curetmdbanime"
    # 二进制文件版本
    binary_version = "1.0.0"

    # 私有属性
    _contextvars = contextvars.ContextVar("recursion_flag", default=False)
    _process = None
    _thread: Optional[threading.Thread] = None

    # 配置属性
    _enabled: bool = False
    _source: Optional[str] = (
        "https://raw.githubusercontent.com/wikrin/CureTMDb/main/tv.json"
    )
    _port: int = 8632

    CONFIG_KEYS = (
        "enabled",
        "source",
        "port",
    )

    @contextmanager
    def no_recursion(self):
        """防递归上下文管理器"""
        token = self._contextvars.set(True)
        try:
            yield
        finally:
            self._contextvars.reset(token)

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()
        # 加载插件配置
        self.load_config(config)
        self.patch_manager = MonkeyPatchManager()
        if self._enabled:
            # 在单独线程中运行 CureTMDbAnime 服务
            self._thread = threading.Thread(target=self._run_binary_in_thread)
            self._thread.daemon = True
            self._thread.start()

    def load_config(self, config: dict):
        """加载配置"""
        if config:
            # 遍历配置中的键并设置相应的属性
            for key in self.CONFIG_KEYS:
                setattr(self, f"_{key}", config.get(key, getattr(self, f"_{key}")))

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        pass

    def stop_service(self):
        """退出插件"""
        if getattr(self, "patch_manager", None):
            self.patch_manager.unpatch_all()
        if self._process is not None and self._process.is_running():
            self._process.kill()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)  # 等待线程结束，或最多等待5秒

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_command(self):
        pass

    def get_form(self):
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            # {
                            #     'component': 'VCol',
                            #     'props': {'cols': 12, 'md': 4},
                            #     'content': [
                            #         {
                            #             'component': 'VSwitch',
                            #             'props': {
                            #                 'model': 'use_cont_eps',
                            #                 'label': '使用连续集号',
                            #             }
                            #         }
                            #     ]
                            # },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "port",
                                            "label": "监听端口",
                                            "type": "number",
                                            "min": 1024,
                                            "max": 65535,
                                            "step": 1,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 9},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "source",
                                            "label": "来源",
                                            "placeholder": "远程地址",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "source": "https://raw.githubusercontent.com/wikrin/CureTMDb/main/tv.json",
            "port": 8632,
        }

    def get_page(self):
        pass

    def get_state(self):
        return self._enabled

    def _run_binary_in_thread(self):
        """
        在独立线程中运行二进制文件并捕获其输出。
        """
        # 工作目录
        working_dir = settings.PLUGIN_DATA_PATH / self.__class__.__name__.lower()
        # 可执行文件路径
        executable_path = working_dir / self.binary_name

        if not executable_path.exists():
            logger.info("尝试自动下载二级制文件...")
            self.__download(executable_path)
            if not executable_path.exists():
                logger.error("二级制文件不存在，无法启动 CureTMDbAnime 服务。")
                return

        # 构建命令行参数列表
        cmd_args = [
            executable_path.as_posix(),
            "--PORT",
            str(self._port),
            "--DATA_DIR",
            working_dir.as_posix(),
        ]

        if self._source:
            cmd_args.extend(["--CURE_SOURCE", self._source])

        if settings.PROXY_HOST:
            cmd_args.extend(["--PROXY", settings.PROXY_HOST])

        if settings.TMDB_API_DOMAIN:
            cmd_args.extend(
                ["--TMDB_UPSTREAM_URL", f"https://{settings.TMDB_API_DOMAIN}"]
            )

        try:
            # 启动子进程
            self._process = psutil.Popen(
                cmd_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            if self._process.is_running():
                self.patch_manager.patch_build_url(self._port)
                self.patch_manager.patch_torrent_helper(self.correct_meta)

            def log_output_line(line: str):
                if line.strip():
                    parts = line.strip().split(" ", 3)
                    log_level = parts[0][1:-1].lower()
                    if log_level == "gin":
                        log_func = logger.debug
                    else:
                        log_func = getattr(logger, log_level, logger.critical)
                    log_func(f"🔗  {parts[-1].strip()}")

            # 实时读取并记录输出
            while self._process.is_running():
                rlist, _, _ = select.select(
                    [self._process.stdout, self._process.stderr], [], [], 0.1
                )
                if not rlist:
                    time.sleep(0.1)

                for stream in [self._process.stdout, self._process.stderr]:
                    if stream in rlist:
                        line = stream.readline()
                        if line:
                            log_output_line(line)

            # 读取剩余输出
            stdout, stderr = self._process.communicate()
            for line in stdout.splitlines() + stderr.splitlines():
                log_output_line(line)

        except Exception as e:
            logger.error(f"启动 CureTMDbAnime 服务时发生错误: {e}")
        finally:
            if self._process and self._process.is_running():
                logger.warning("CureTMDbAnime 服务异常终止，尝试清理进程。")
                self._process.kill()
            if self._process:
                self._process.wait()
            logger.info("CureTMDbAnime 服务线程已退出。")

    def __download_url(self):
        """
        获取下载链接
        """
        _url = "{author_url}/{name}/releases/download/v{version}/{name}-{os}-{arch}"

        if SystemUtils.is_aarch64():
            arch = "arm64"
        elif SystemUtils.is_x86_64():
            arch = "amd64"
        else:
            raise NotImplementedError("不支持的CPU架构")

        os_name = "darwin" if SystemUtils.is_macos() else "linux"

        return _url.format(
            author_url=self.author_url,
            name=self.binary_name,
            arch=arch,
            version=self.binary_version,
            os=os_name,
        )

    def __download(self, dest_path: Path):
        """
        下载二进制文件
        """
        url = self.__download_url()
        temp_dir = tempfile.mkdtemp()
        temp_file = Path(temp_dir) / f"{self.binary_name}.tmp"

        try:
            # 创建目标目录
            dest_path.parent.mkdir(parents=True, exist_ok=True)

            logger.info(f"正在下载: {url}")
            with RequestUtils(proxies=settings.PROXY).get_stream(url) as r:
                r.raise_for_status()
                with open(temp_file, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)

            # 设置可执行权限
            temp_file.chmod(0o755)

            # 复制到目标位置
            shutil.copy2(temp_file.as_posix(), dest_path.as_posix())

            logger.info(f"下载完成: {dest_path}")

        except Exception as e:
            logger.error(f"下载失败: {e}")
            raise
        finally:
            # 清理临时目录
            try:
                shutil.rmtree(temp_dir)
            except Exception as e:
                logger.warning(f"清理临时目录失败: {e}")

    def get_module(self) -> Dict[str, Any]:
        """
        获取插件模块声明，用于胁持系统模块实现（方法名：方法实现）
        """
        return {
            # 识别媒体信息
            "recognize_media": self.on_recognize_media,
            "async_recognize_media": self.on_recognize_media,
            # 文件整理
            "transfer": self.on_transfer,
        }

    def is_eligible(self, mtype: MediaType = None) -> bool:

        if self._contextvars.get():
            return False

        if settings.RECOGNIZE_SOURCE != "themoviedb":
            return False

        if mtype == MediaType.MOVIE:
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
            matched_word = next(
                (
                    word
                    for word in meta.apply_words
                    if " >> " in word and " <> " in word
                ),
                None,
            )
            if matched_word:
                logger.info(
                    f"存在应用的集数偏移识别词 `{matched_word}`, 跳过调整元数据"
                )
                return False

        corrected = False

        def adjust_episode(is_begin: bool) -> bool:
            """调整单个集数信息"""
            episode_num = meta.begin_episode if is_begin else meta.end_episode
            if not episode_num:
                return False

            season_num = (meta.begin_season if is_begin else meta.end_season) or 1

            # TMDB 使用连续集号时
            if result := RequestUtils().get_json(
                f"http://127.0.0.1:{self._port}/cache/{mediainfo.tmdb_id}/mapping/{season_num}/{episode_num}"
            ):
                logical_season = result["season"]
                logical_episode = result["episode"]
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
                logger.debug(
                    f"{mediainfo.title_year} TMDb集数信息可能未更新, 不调整元数据"
                )
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
                    if season_key == 0: # 排除季0
                        continue
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
            logger.info(
                f"{mediainfo.title_year} 元数据季集已调整：{orig_season_episode} ==> {meta.season_episode}"
            )

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

        if not self.is_eligible(mtype=mtype):
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
                **kwargs,
            )
        # 识别失败，阻止run_module继续执行
        if media_info is None:
            return False
        # 只处理电视剧
        if media_info.type != MediaType.TV:
            return media_info

        self.correct_meta(meta, media_info)
        return media_info

    def on_transfer(self, meta: MetaBase, mediainfo: MediaInfo, **kwargs):
        """
        文件整理

        :param meta: 预识别的元数据
        :param mediainfo:  识别的媒体信息
        """
        if mediainfo.type != MediaType.TV:
            return None

        self.correct_meta(meta, mediainfo)
        return None
