import os
import stat
import threading
from pathlib import Path
from typing import Any, Optional, Dict, List

import docker
from pydantic import BaseModel, Field

from app.core.cache import cached
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.meta.metabase import MetaBase
from app.helper.system import SystemHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import MediaType
from app.utils.http import RequestUtils
from app.utils.system import SystemUtils

from .engine import MetaCorrectionUseCase
from .patch import MonkeyPatchManager


class CureTMDbAnimeConfig(BaseModel):
    # 启用插件
    enabled: bool = Field(default=False)
    # 启用元数据修正
    enable_correction: bool = Field(default=True)
    # 最新季允许的越界宽限集数
    grace_episodes: int = Field(default=2, ge=0, le=5)
    # 离散等级差
    rewrite_margin: int = Field(default=2, ge=0, le=4)
    # 远程数据源地址
    source: Optional[str] = Field(
        default="https://raw.githubusercontent.com/wikrin/CureTMDb/main/tv.json",
    )
    # 运行端口
    port: int = Field(default=8632, ge=1024, le=65535)


class CureTMDbAnime(_PluginBase):
    # 插件名称
    plugin_name = "CTMDbA"
    # 插件描述
    plugin_desc = "对 TMDb 上被合并为一季的番剧进行季信息分离。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/wikrin/MoviePilot-Plugins/main/icons/ctmdbanime.png"
    # 插件版本
    plugin_version = "2.1.4"
    # 插件作者
    plugin_author = "Attente"
    # 作者主页
    author_url = "https://github.com/wikrin"
    # 插件配置项ID前缀
    plugin_config_prefix = "curetmdbanime_"
    # 加载顺序
    plugin_order = 99
    # 可使用的用户级别
    auth_level = 1
    # 二进制文件
    binary_name = "curetmdbanime"
    # 二进制文件版本
    binary_version = "1.2.1"

    def __init__(self):
        super().__init__()
        self.config = CureTMDbAnimeConfig()
        self.patch_manager = MonkeyPatchManager()
        self._thread: Optional[threading.Thread] = None
        self._event: threading.Event = threading.Event()

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()
        # 加载插件配置
        self.load_config(config)

        if not self.config.enabled:
            return

        # 初始化
        self.meta_correction_use_case = MetaCorrectionUseCase(
            grace_episodes=self.config.grace_episodes,
            rewrite_margin=self.config.rewrite_margin,
        )

        # 在单独线程中运行 CureTMDbAnime 服务
        self._thread = threading.Thread(target=self._run_binary_in_thread, daemon=True)
        self._thread.start()

    def load_config(self, config: dict):
        """加载配置"""
        if config:
            self.config = CureTMDbAnimeConfig(**config)

    def stop_service(self):
        """退出插件"""
        self.patch_manager.unpatch_all()
        if self._thread:
            # 设置停止事件
            self._event.set()
            # 等待线程结束
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                logger.warning("CureTMDbAnime 服务线程未能及时停止。")
            # 重置停止事件
            self._event.clear()
            self._thread = None

    def get_api(self) -> List[Dict[str, Any]]:
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
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "port",
                                            "label": "端口",
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enable_correction",
                                            "label": "启用元数据修正",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "grace_episodes",
                                            "label": "集数越界宽限",
                                            "type": "number",
                                            "min": 0,
                                            "max": 5,
                                            "step": 1,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "rewrite_margin",
                                            "label": "离散等级差",
                                            "type": "number",
                                            "min": 0,
                                            "max": 4,
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
        ], CureTMDbAnimeConfig().model_dump()

    def get_page(self):
        pass

    def get_state(self):
        return self.patch_manager.is_patched()

    def _run_binary_in_thread(self):
        """
        在独立线程中运行二进制文件并捕获其输出。
        """
        # 工作目录
        working_dir = settings.PLUGIN_DATA_PATH / self.__class__.__name__.lower()
        # 可执行文件路径
        executable_path = working_dir / self.binary_name

        if not executable_path.exists() or not self._check_version(executable_path):
            logger.info("尝试下载二级制文件...")
            self.__download(executable_path)
            if not executable_path.exists():
                logger.error("二级制文件不存在，无法启动 CureTMDbAnime 服务。")
                return

        # 确保文件有可执行权限
        if not os.access(executable_path, os.X_OK) and not self.__fix_exec_permission(
            executable_path
        ):
            return

        # 构建命令行参数列表
        cmd_args = [
            executable_path.as_posix(),
            "--debug",
            "--port",
            str(self.config.port),
            "--data-dir",
            working_dir.as_posix(),
        ]

        if self.config.source:
            cmd_args.extend(["--cure-source", self.config.source])

        if settings.PROXY_HOST:
            cmd_args.extend(["--proxy", settings.PROXY_HOST])

        if settings.TMDB_API_DOMAIN:
            cmd_args.extend(["--tmdb-api-url", f"https://{settings.TMDB_API_DOMAIN}"])

        try:
            from subprocess import PIPE

            import psutil

            process = psutil.Popen(
                cmd_args, stdout=PIPE, stderr=PIPE, text=True, bufsize=1
            )
            if process.is_running():
                self.patch_manager.patch_build_url(self.config.port)
                if self.config.enable_correction:
                    self.patch_manager.patch_meta_enhancement(self.correct_meta)
                # 输出服务日志
                self._read_process_output(process)

        finally:
            if process and process.is_running():
                logger.warning("CureTMDbAnime 服务终止，尝试清理进程。")
                process.kill()
            if process:
                process.wait()
            logger.info("CureTMDbAnime 服务线程已退出。")

    def _read_process_output(self, process):
        import selectors

        def log_output_line(line: str):
            if line.strip():
                parts = line.strip().split(" ", 3)
                log_level = parts[0][1:-1].lower()
                log_func = (
                    logger.debug
                    if log_level == "gin"
                    else getattr(logger, log_level, logger.critical)
                )
                log_func(f"🔗  {parts[-1].strip()}")

        with selectors.DefaultSelector() as sel:
            sel.register(process.stdout, selectors.EVENT_READ, data="stdout")
            sel.register(process.stderr, selectors.EVENT_READ, data="stderr")

            while not self._event.is_set():
                events = sel.select(timeout=1)
                for key, _ in events:
                    line = key.fileobj.readline()
                    if line:
                        log_output_line(line)

    def _check_version(self, executable: Path) -> bool:
        """检查版本"""
        from app.utils.string import StringUtils

        version = SystemUtils.execute(f"{executable.as_posix()} -v")
        if version == "dev":
            return True

        result, msg = StringUtils.compare_version(
            version, ">=", self.binary_version, True
        )
        if result is None:
            logger.error(f"比较版本出错：{msg}")
            return False

        logger.info(msg)
        return result

    @staticmethod
    def __fix_exec_permission(file_path: Path) -> bool:
        """修复文件可执行权限"""
        try:
            current_uid = os.getuid()
            file_stat = file_path.stat()

            # 文件所有者或 root 可直接修改
            if current_uid == file_stat.st_uid or current_uid == 0:
                file_path.chmod(file_stat.st_mode | stat.S_IXUSR)
                success = os.access(file_path, os.X_OK)
                if success:
                    logger.info(
                        f"权限修复成功：{oct(file_path.stat().st_mode & 0o777)}"
                    )
                else:
                    logger.error("权限设置后仍无法执行")
                return success

            # Docker 环境通过容器修改
            logger.info("当前用户无权限，尝试通过 Docker 修改")
            return CureTMDbAnime.__fix_permission_via_docker(file_path)

        except Exception as e:
            logger.error(f"修复可执行权限失败：{e}")
            return False

    @staticmethod
    def __fix_permission_via_docker(file_path: Path) -> bool:
        """
        通过 Docker 守护进程修改文件权限

        :return bool: 修复成功返回 True，否则返回 False
        """
        try:
            # 检查是否为 Docker 环境
            if not SystemUtils.is_docker():
                logger.error("非 Docker 环境，无法通过 Docker 守护进程修改权限")
                return False

            # 获取容器 ID
            container_id = SystemHelper._get_container_id()
            if not container_id:
                logger.error("无法获取容器 ID")
                return False

            # 创建 Docker 客户端
            client = docker.DockerClient(base_url=settings.DOCKER_CLIENT_API)
            container = client.containers.get(container_id)

            logger.info("通过 Docker 容器执行权限修改")

            # 执行命令
            exit_code, output = container.exec_run(
                cmd=["chmod", "+x", file_path.as_posix()],
                stdout=False,
                detach=True,
            )

            if exit_code == 0:
                logger.info("通过 Docker 守护进程修改权限成功")
                return os.access(file_path, os.X_OK)
            else:
                logger.error(
                    f"通过 Docker 修改权限失败：{output.decode() if output else '无输出'}"
                )
                return False

        except Exception as e:
            logger.error(f"通过 Docker 修改权限失败：{e}")
            return False

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
        import shutil
        import tempfile

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

    @cached(ttl=2 * 3600)
    def _get_logical_mapping(self, tmdb_id: int):
        """
        获取 TMDB 逻辑季集映射信息
        """
        mapping: Dict[tuple[int, int], tuple[int, int]] = {}

        result = RequestUtils(timeout=2).get_json(
            f"http://127.0.0.1:{self.config.port}/cache/mapping/{tmdb_id}"
        )
        if not isinstance(result, dict):
            return mapping

        for season_key, episodes in result.items():
            try:
                season_num = int(season_key)
            except (TypeError, ValueError):
                continue

            if not isinstance(episodes, dict):
                continue

            for episode_key, item in episodes.items():
                try:
                    episode_num = int(episode_key)
                except (TypeError, ValueError):
                    continue

                if not isinstance(item, dict):
                    continue

                logical_season = item.get("season")
                logical_episode = item.get("episode")
                try:
                    logical_season = int(logical_season)
                    logical_episode = int(logical_episode)
                except (TypeError, ValueError):
                    continue

                mapping[(season_num, episode_num)] = (logical_season, logical_episode)

        return mapping

    def correct_meta(self, meta: MetaBase, mediainfo: MediaInfo) -> MetaBase:
        """
        根据逻辑季信息调整元数据对象中的季号和集号。

        :param meta: 原始元数据对象
        :param mediainfo: 媒体信息对象
        """
        if not meta or not mediainfo or mediainfo.type != MediaType.TV:
            return meta

        # 检查识别词是否已偏移集数
        if meta.apply_words and (
            matched_word := next(
                (
                    word
                    for word in meta.apply_words
                    if " >> " in word and " <> " in word
                ),
                None,
            )
        ):
            logger.info(f"存在应用的集数偏移识别词 `{matched_word}`, 跳过调整元数据")
            return meta

        tmdb_mapping = self._get_logical_mapping(mediainfo.tmdb_id)
        pubdate = self.patch_manager.get_torrent_pubdate(
            title=meta.title,
            description=meta.subtitle,
        )
        try:
            decision = self.meta_correction_use_case.correct(
                meta=meta,
                tmdb_mapping=tmdb_mapping,
                mediainfo=mediainfo,
                publish_date=pubdate,
                source="torrent_pubdate" if pubdate else None,
            )
        except ValueError:
            return meta

        if not decision.changed:
            return meta

        meta.set_season(decision.final_range.season_list)
        meta.set_episode(decision.final_range.episode_list)

        logger.info(
            "%s 调整结论: %s => %s, %s",
            meta.title,
            decision.original_range.format(),
            decision.final_range.format(),
            "；".join(decision.reasons) if decision.reasons else "",
        )

        return meta
