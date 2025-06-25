import inspect
import sys

from app.core.meta.metabase import MetaBase
from app.core.context import MediaInfo, TorrentInfo
from app.log import logger
from app.schemas.message import Notification
from app.schemas.transfer import TransferInfo


class FrameInspector:

    @staticmethod
    def inspect(config: dict = None) -> dict:
        if not config:
            return {}

        target_cls = config.get("cls_name")
        target_method = config.get("method_name")
        depth = config.get("depth", 15)
        skip = config.get("skip", 2)
        frame = inspect.currentframe()

        # 检查是否已经缓存过调用栈深度
        if not hasattr(FrameInspector.inspect, "call_depth"):
            call_depth = FrameInspector._find_plugin_call_depth(frame)
            if call_depth is None:
                logger.warning("未找到插件调用栈起点，使用默认深度")
                call_depth = 10  # 默认回退
            setattr(FrameInspector.inspect, "call_depth",call_depth)

        call_depth = getattr(FrameInspector.inspect, "call_depth")


        frame = sys._getframe(call_depth)
        # 从插件调用栈起点往后查找
        for i in range(1, depth):
            frame = frame.f_back
            if not frame:
                break

            if i <= skip:
                continue

            locals_ = frame.f_locals
            self_obj = locals_.get("self")

            if not self_obj or not hasattr(self_obj, "__class__"):
                continue

            cls_name = self_obj.__class__.__name__
            method_name = frame.f_code.co_name

            if cls_name == target_cls and method_name == target_method:
                logger.debug(f"Found target method at depth {i + call_depth}: {cls_name}.{method_name}")
                return FrameInspector._extract_from_locals(locals_)

        return {}

    @staticmethod
    def _find_plugin_call_depth(frame, max_depth=20):
        """
        查找调用栈中属于插件调用的帧位置（仅首次调用）
        :param frame: 当前帧
        :param max_depth: 最大查找深度
        :return: 插件调用的栈深度，或 None
        """
        for i in range(max_depth):
            frame = frame.f_back
            if not frame:
                break

            cls_name = ""
            self_obj = frame.f_locals.get("self")
            if self_obj and hasattr(self_obj, "__class__"):
                cls_name = self_obj.__class__.__name__
                method_name = frame.f_code.co_name

            # 判断是否是插件基类或封装层
            if cls_name == "MessageQueueManager" and method_name == "send_message":
                logger.debug(f"Found plugin base class at depth {i}: {cls_name}.{method_name}")
                return i + 1  # 返回该帧之后的位置作为起点

        return None

    @staticmethod
    def _extract_from_locals(local_vars: dict) -> dict:

        type_to_key = {
            MetaBase: "meta",
            MediaInfo: "mediainfo",
            TorrentInfo: "torrentinfo",
            TransferInfo: "transferinfo",
        }

        result = {}
        for k, v in local_vars.items():
            if k == "self":
                continue

            mapped_key = type_to_key.get(type(v))
            if mapped_key:
                result[mapped_key] = v
            else:
                result[k] = v

        return result

    @staticmethod
    def has_notification(_locals: dict) -> bool:
        """
        判断否拥有 Notification 实例
        :param _locals: 包含本地变量的字典
        :return: 如果包含 Notification 实例则返回 True，否则返回 False
        """
        for val in _locals.values():
            if FrameInspector._contains_notification(val):
                return True
        return False

    @staticmethod
    def _contains_notification(obj, seen=None):
        """
        递归检查对象中是否包含 Notification 实例

        :param obj: 要检查的对象
        :param seen: 已检查过的对象集合（用于避免循环引用）
        :return: 如果包含 Notification 实例则返回 True，否则返回 False
        """
        if seen is None:
            seen = set()

        obj_id = id(obj)
        if obj_id in seen:
            # 避免循环引用
            return False
        seen.add(obj_id)

        if isinstance(obj, Notification):
            return True

        # 如果是容器类型，递归检查
        if isinstance(obj, dict):
            for k, v in obj.items():
                if FrameInspector._contains_notification(
                    k, seen
                ) or FrameInspector._contains_notification(v, seen):
                    return True
        elif isinstance(obj, (list, tuple, set)):
            for item in obj:
                if FrameInspector._contains_notification(item, seen):
                    return True
        elif hasattr(obj, "__dict__"):
            # 处理类实例，检查其属性
            for k, v in vars(obj).items():
                if FrameInspector._contains_notification(
                    k, seen
                ) or FrameInspector._contains_notification(v, seen):
                    return True

        return False

