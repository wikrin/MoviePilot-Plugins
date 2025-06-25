from pathlib import Path

from app.core.metainfo import MetaInfoPath
from app.log import logger
from app.schemas.mediaserver import WebhookEventInfo

from ..handlers import BaseFrameHandler, registry
from ..utils import TimeUtils


class MediaServerMsgHandler(BaseFrameHandler):
    """插件: 媒体库服务器通知"""
    category = "媒体服务器"
    skip = 2

    @staticmethod
    def get_run_time(channel: str, data: dict):
        if channel == "jellyfin":
            return data.get("RunTime")
        elif channel == "emby":
            return TimeUtils.runtime_format(data.get("RunTimeTicks") // 10000)
        else:
            return None

    @staticmethod
    def convert_chinese(data_list: list[str]) -> dict:
        """
            将包含中文键值对的列表转换为英文键的字典。

            :param data_list: 包含中文键值对的列表。
            :return: 转换后的字典，键为英文，值为对应的值。
            """
        key_mapping = {
                "用户": "user",
                "设备": "device",
                "进度": "progress",
                "时间": "time",
                "IP地址": "ip_address",
            }

        return {
                key_mapping[key.strip()]: value.strip()
                for item in data_list
                if "：" in item
                for key, value in [item.split("：", 1)]
                if key.strip() in key_mapping
            }

    def send(cls, events: set) -> dict:
        """
        目标方法为媒体库服务器通知中的send方法
        """
        if not (result := cls.extract(method_name="send")):
            return {}
        # 移除 event 字段
        result.pop("event")
        # 获取 webhook 报文
        _event_info: WebhookEventInfo = result.get("event_info")

        logger.debug(f"{cls.__qualname__}：{_event_info}")   # 媒体库负载差异

        # 判断事件
        if _event_info.event not in events:
            return {}
        channel = _event_info.channel
        result["action"] = str(result["message_title"]).replace(_event_info.item_name, "").strip()
        result["run_time"] = cls.get_run_time(channel=channel, data=_event_info.json_object)
        result["meta"] = MetaInfoPath(Path(_event_info.item_path))
        # 转换中文键
        return {**cls.convert_chinese(result.get("message_texts")), **result}

    @registry.register
    def library_new(cls) -> dict:
        """
        :label 新入库
        """
        events = {'library.new'}

        return cls.send(valid_events=events)

    @registry.register
    def playback_start(cls) -> dict:
        """
        :label 开始播放
        """
        events = {"playback.start", "PlaybackStart", "media.play"}

        return cls.send(events=events)

    @registry.register
    def playback_stop(cls) -> dict:
        """
        :label 停止播放
        """
        events = {"playback.stop", "PlaybackStop", "media.stop"}

        return cls.send(events=events)
