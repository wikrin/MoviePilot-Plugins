# 基础库
import os
import re
from typing import Any, Dict, List, Optional

# 项目库
from app.core.config import settings
from app.db.models.mediaserver import MediaServerItem
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.modules.jellyfin import Jellyfin
from app.plugins import _PluginBase
from app.schemas.mediaserver import WebhookEventInfo
from app.utils.http import RequestUtils


class JellyfinExtension(Jellyfin):

    def __init__(self, instance: Jellyfin):
        for key, value in vars(instance).items():
            setattr(self, key, value)

    def get_log_files(self) -> Optional[List[Dict]]:
        """
        获取媒体库日志文件
        """
        if not self._host or not self._apikey or not self.user:
            return None

        url = f"{self._host}System/Logs"
        params = {"api_key": self._apikey}

        try:
            res = RequestUtils().get_res(url, params=params)
            if not res or res.status_code != 200:
                logger.error(f"获取日志列表失败，状态码：{res.status_code if res else '无响应'}，URL={url}")
                return None

            logs = res.json()
            return logs

        except Exception as e:
            logger.error(f"未知错误：{e}，URL={url}")
            return None

    def get_log(self, index: int = 0) -> dict[str, str]:
        """
        获取日志内容
        return: 从新到旧的日志行列表
        """
        if not self._host or not self._apikey or not self.user:
            return []

        logs = self.get_log_files() or [{}]
        file_name = logs[index].get("Name")
        if not file_name:
            return []

        url = f"{self._host}System/Logs/Log"
        params = {"name": file_name, "api_key": self._apikey}

        try:
            res = RequestUtils().get_res(url, params=params)
            if not res or res.status_code != 200:
                logger.error(f"获取日志失败，状态码：{res.status_code if res else '无响应'}，URL={url}")
                return []

            return res.text.splitlines()

        except Exception as e:
            logger.error(f"未知错误：{str(e)}，URL={url}")
            return []

    def find_item_path(self, item_id: str) -> Optional[str]:
        """
        使用正则表达式查找匹配项
        :param item_id: 要查找的 Item ID（不带连字符）
        :return: path
        """

        # 获取日志内容
        log_lines = self.get_log()
        if not log_lines:
            return None

        # 构建目标 ID 的正则（兼容无连字符和有连字符的情况）
        normalized_id = re.sub(r'[^a-fA-F0-9]', '', item_id)
        pattern = re.compile(
            r'Type:\s*"([^"]+)",\s*Name:\s*"([^"]+)",\s*Path:\s*"([^"]+)",\s*Id:\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})',
            re.IGNORECASE
        )

        for line in log_lines:
            match = pattern.search(line)
            if not match:
                continue

            log_id = match.group(4).replace('-', '')
            if log_id != normalized_id:
                continue

            path = match.group(3)
            ext = os.path.splitext(path)[-1].lower()

            if ext != '' and ext not in settings.RMT_MEDIAEXT:
                logger.debug(f"跳过非媒体文件：{path}")
                continue

            return path

        return None


class EnrichWebhook(_PluginBase):
    # 插件名称
    plugin_name = "Jellyfin报文补充"
    # 插件描述
    plugin_desc = "补充webhook报文, 为 [媒体文件同步删除] 提供必要的数据支持"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/wikrin/MoviePilot-Plugins/main/icons/path_a.png"
    # 插件版本
    plugin_version = "1.0.3"
    # 插件作者
    plugin_author = "Attente"
    # 作者主页
    author_url = "https://github.com/wikrin"
    # 插件配置项ID前缀
    plugin_config_prefix = "enrichwebhook_"
    # 加载顺序
    plugin_order = 12
    # 可使用的用户级别
    auth_level = 1

    # 配置属性
    _enabled: bool = False

    def init_plugin(self, config: dict = None):
        self.mediaserver_helper = MediaServerHelper()
        # 停止现有任务
        self.stop_service()
        self.load_config(config)

    def load_config(self, config: dict):
        """加载配置"""
        if config:
            # 遍历配置中的键并设置相应的属性
            for key in (
                "enabled",
            ):
                setattr(self, f"_{key}", config.get(key, getattr(self, f"_{key}")))

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
                                'props': {'cols': 6, 'md': 3},
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
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
        }

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        pass

    def stop_service(self):
        """退出插件"""
        pass

    def get_api(self):
        pass

    def get_command(self):
        pass

    def get_page(self):
        pass

    def get_state(self):
        return self._enabled

    def get_module(self) -> Dict[str, Any]:
        """
        获取插件模块声明，用于胁持系统模块实现（方法名：方法实现）
        """
        return {"webhook_parser": self.enrich_webhook}

    def enrich_webhook(self, body: Any, form: Any, args: Any):
        source = args.get("source")

        # 获取 webhookinfo
        webhookinfo, serverinfo = self._get_webhook_info(source, body)
        if not webhookinfo:
            logger.warning("未能获取到有效的 webhook 消息")
            return None

        if webhookinfo.item_path is None:
            logger.debug("item_path 为空，开始补充路径信息")
            jellyfin_ext = JellyfinExtension(serverinfo.instance)

            # 补充 item_path
            if path := jellyfin_ext.find_item_path(webhookinfo.item_id):
                webhookinfo.item_path = path
                logger.info(f"成功获取 item_path：{path}")

            # 修正 tmdb_id
            series_id = webhookinfo.json_object.get("SeriesId")
            if series_id:
                webhookinfo.tmdb_id = self._resolve_tmdb_id(jellyfin_ext, series_id)
            logger.info(f"完成 webhook 处理：tmdbid: {webhookinfo.tmdb_id}, item_path: {webhookinfo.item_path}")

        return webhookinfo


    def _get_webhook_info(self, source: str, body: Any):
        """获取 webhook 消息主体及对应的服务器信息"""
        if source:
            serverinfo = self.mediaserver_helper.get_service(source, "jellyfin")
            if not serverinfo:
                return None, None
            logger.info(f"获取到 Jellyfin 服务器信息：{serverinfo.name}")
            webhookinfo: WebhookEventInfo = serverinfo.instance.get_webhook_message(body)
            if webhookinfo:
                webhookinfo.server_name = serverinfo.name
                logger.debug(f"从服务器 {serverinfo.name} 获取到 webhook 消息")
            return webhookinfo, serverinfo

        jellyfin_servers = self.mediaserver_helper.get_services("jellyfin")
        logger.info(f"发现 {len(jellyfin_servers or {})} 个 Jellyfin 服务器")
        for serverinfo in (jellyfin_servers or {}).values():
            webhookinfo: WebhookEventInfo = serverinfo.instance.get_webhook_message(body)
            if webhookinfo:
                logger.debug(f"从服务器 {serverinfo.name} 获取到 webhook 消息")
                return webhookinfo, serverinfo
        return None, None

    def _resolve_tmdb_id(self, jellyfin_ext: JellyfinExtension, series_id: str) -> Optional[int]:
        """解析 SeriesId 对应的 TMDB ID"""

        iteminfo = jellyfin_ext.get_iteminfo(series_id)
        if iteminfo and iteminfo.tmdbid:
            logger.info(f"通过 Jellyfin API 获取到 TMDB ID：{iteminfo.tmdbid}")
            return iteminfo.tmdbid

        # 尝试通过本地数据库获取
        item = MediaServerItem.get_by_itemid(db=None, item_id=series_id)
        if item and item.tmdbid:
            logger.info(f"通过本地数据库获取到 TMDB ID：{item.tmdbid}")
            return item.tmdbid

        return None