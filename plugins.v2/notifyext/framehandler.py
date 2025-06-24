import importlib
import inspect
from abc import ABC, ABCMeta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Type, Any, Callable

from app.core.metainfo import MetaInfoPath
from app.core.meta.metabase import MetaBase
from app.core.context import MediaInfo, TorrentInfo
from app.log import logger
from app.schemas.message import Notification
from app.schemas.transfer import TransferInfo

from .models import FrameHandlerItem, FrameResult


class FrameInspector:
    @staticmethod
    def inspect(config: dict = None) -> dict:

        def _contains_notification(obj, seen=None):
            """
            递归检查对象中是否包含 Notification 实例
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
                    if _contains_notification(k, seen) or _contains_notification(
                        v, seen
                    ):
                        return True
            elif isinstance(obj, (list, tuple, set)):
                for item in obj:
                    if _contains_notification(item, seen):
                        return True
            elif hasattr(obj, "__dict__"):
                # 处理类实例，检查其属性
                for k, v in vars(obj).items():
                    if _contains_notification(k, seen) or _contains_notification(
                        v, seen
                    ):
                        return True

            return False

        def has_notification(_locals: dict):
            """
            判断否拥有 Notification 实例
            """
            for val in _locals.values():
                if _contains_notification(val):
                    return True
            return False

        if not config:
            return {}

        target_cls = config.get("cls_name")
        target_method = config.get("method_name")
        depth = config.get("depth", 15)
        skip = config.get("skip", 8)

        frame = inspect.currentframe()

        for i in range(depth):
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
                logger.debug(f"depth: {i}, Extracting context from {cls_name}.{method_name}")
                return FrameInspector._extract_from_locals(locals_)

        return {}

    @staticmethod
    def _extract_from_locals(local_vars: dict):

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


class HandlerMetaclass(ABCMeta):
    """处理器元类"""
    def __new__(mcs, name, bases, attrs):
        # 先创建类
        cls = super().__new__(mcs, name, bases, attrs)

        # 处理所有被装饰的方法
        for attr_name, attr_value in attrs.items():
            if hasattr(attr_value, "__is_handler__"):
                # 获取装饰器存储的category
                category = getattr(attr_value, "__category__", None)
                # 使用类的category作为默认值
                effective_category = category or getattr(cls, "category", "")

                qualified_name = f"{name}.{attr_name}"
                _doc = attr_value.__doc__ or ""
                registry._handlers[qualified_name] = {
                    "func_path": f"{attr_value.__module__}.{qualified_name}",
                    "label": " - ".join(
                        filter(
                            None,
                            [
                                cls.__doc__,
                                HandlerMetaclass._extract_tag(_doc, ":label"),
                            ],
                        )
                    )
                    or None,
                    "category": effective_category,
                    "description": HandlerMetaclass._extract_tag(_doc, ":description"),
                }
                logger.debug(f"Registering frame handler: {qualified_name} - {effective_category}")

        return cls

    def _extract_tag(doc: str, tag: str) -> Optional[str]:
        """
        从 docstring 中提取指定标签后的内容。

        :param doc: 原始 docstring
        :param tag: 要提取的标签（如 ":label"）
        :return: 提取到的内容 或 None
        """
        if not doc:
            return None

        lines = [line.strip() for line in doc.strip().splitlines()]

        for line in lines:
            if line.startswith(tag):
                content = line[len(tag):].split("\n", 1)[0].strip()
                return content or None

        return None


class BaseHandler(ABC, metaclass=HandlerMetaclass):
    """
    抽象处理器基类，所有通知处理器都应继承此类
    """
    # 配置字段
    depth: int = 15
    skip: int = 8
    category: str = ""

    @classmethod
    def get_config(cls) -> dict:
        return {
            "cls_name": cls.__name__.removesuffix("Handler"),
            "depth": cls.depth,
            "skip": cls.skip,
        }

    @classmethod
    def extract(cls, **kwargs) -> dict:
        """
        提取上下文逻辑
        """
        config = cls.get_config()
        config.update(kwargs)
        return FrameInspector.inspect(config)


class HandlerRegistry:
    def __init__(self):
        self._handlers: Dict[str, Dict] = {}

    def register(self, f=None, *, category: str = None):

        if f is None:
            return lambda func: self.register(func, category=category)

        def decorator(func):
            func.__is_handler__ = True
            func.__category__ = category
            return func

        return decorator(f)

    def get_handler(self, name: str) -> Optional[Callable]:
        if name not in self._handlers:
            return None

        def load_handler(func_path: str) -> Optional[Tuple[Type[BaseHandler], Callable]]:
            """
            从 func_path 加载 handler 方法。
            func_path 格式应为 "module.class_name.method_name"
            """
            try:
                # 分割模块、类、方法
                parts = func_path.rsplit(".", 2)  # 最多分割两部分：模块.类.方法
                if len(parts) != 3:
                    logger.error(f"无效的函数路径：{func_path}")
                    return None

                module_path, class_name, method_name = parts

                # 导入模块
                module = importlib.import_module(module_path)

                # 获取类
                cls = getattr(module, class_name, None)
                if not isinstance(cls, type):
                    logger.error(f"找不到类 {class_name} 在模块 {module_path}")
                    return None

                # 获取类方法
                func = getattr(cls, method_name, None)
                if not callable(func):
                    logger.error(f"{class_name}.{method_name} 不是一个可调用的方法")
                    return None

                # 验证是否是注册过的 handler
                if not getattr(func, "__is_handler__", False):
                    logger.warning(f"{class_name}.{method_name} 不是一个有效的 handler 方法")
                    return None

                # 如果不是类方法，手动绑定类
                if not isinstance(func, classmethod):
                    return lambda: func(cls)

                return func

            except (ImportError, ValueError, AttributeError) as e:
                logger.error(f"无法加载 handler 函数：{func_path} {e}")
                return None

        if func_path := self._handlers[name].get("func_path"):
            return load_handler(func_path)


    def list_all(self) -> List[FrameHandlerItem]:
        return [
            FrameHandlerItem(name=name, **handler)
            for name, handler in self._handlers.items()
        ]

# 全局注册中心实例
registry = HandlerRegistry()


class AutoSignInHandler(BaseHandler):
    """站点签到"""
    category = "站点"

    # @registry.register
    def do_notify(cls) -> dict:
        """
        :label 通知
        :description 定时通知
        """
        if context := cls.extract(method_name="__do", skip=10):
            return context


class MediaServerMsgHandler(BaseHandler):
    """媒体库服务器通知"""
    category = "媒体服务器"

    @registry.register
    def send(cls) -> dict:
        """
        :description 插件
        """

        if result := cls.extract(method_name="send", skip=10):
            return cls._send_post(result)

    def _send_post(data: dict[str, Any]) -> dict:
        """
        send方法的后处理方法
        """
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

        if not data:
            return {}

        event_info = data.get("event_info")
        meta = MetaInfoPath(Path(event_info.item_path))
        data.pop("event")
        context = {
            **convert_chinese(data.get("message_texts")),
            **data
        }
        return FrameResult(
            need_media_info=True,
            meta=meta,
            context=context
        )
