"""
在插件内部查询现有 PluginData 键值，不扩展宿主接口或创建数据库表。
"""

from collections.abc import Callable
from typing import Any

from sqlalchemy import JSON, String, case, cast, column, func, literal, or_, select, table
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select


def _escape_like(value: str) -> str:
    """
    将反斜线、百分号和下划线转为 LIKE 的字面字符。

    :param value: 键前缀或完整搜索词
    :return: 使用反斜线转义的文本
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _query_statement(plugin_id: str, key_prefix: str, query: str, dialect: str) -> Select:
    """
    生成限定插件身份、字面前缀与固定 JSON 文本字段的候选查询。

    :param plugin_id: 与现有键值接口一致的插件实例类名
    :param key_prefix: 区分大小写的完整字面键前缀
    :param query: 完整字面搜索词，仅使用 Python lower 规范化
    :param dialect: sqlite 或 postgresql
    :return: 返回普通 key/value 列的 SQLAlchemy Core 查询
    :raises ValueError: 数据库类型不受支持
    """
    if dialect not in ("sqlite", "postgresql"):
        raise ValueError("插件历史查询仅支持 SQLite 和 PostgreSQL")
    data = table("plugindata", column("plugin_id", String), column("key", String), column("value", JSON))
    statement = select(data.c.key, data.c.value).where(
        data.c.plugin_id == plugin_id,
        data.c.key.like(_escape_like(key_prefix) + "%", escape="\\"),
        # SQLite LIKE 默认忽略 ASCII 大小写，补充字面比较保持键身份一致。
        func.substr(data.c.key, 1, len(key_prefix)) == key_prefix,
    )
    if not query:
        return statement

    pattern = "%" + _escape_like(query.lower()) + "%"
    if dialect == "sqlite":
        valid = case((func.json_valid(data.c.value) == 1, data.c.value), else_="{}")
        root = case((func.json_type(valid) == "object", valid), else_="{}")
        title = case(
            (func.json_type(root, "$.search_title") == "text", func.json_extract(root, "$.search_title")),
            else_=None,
        )
        resources = case(
            (func.json_type(root, "$.resources") == "object", func.json_extract(root, "$.resources")),
            else_="{}",
        )
        entries = func.json_each(resources).table_valued(column("key", String), column("value", JSON))
        valid_entry = case((func.json_valid(entries.c.value) == 1, entries.c.value), else_="{}")
        resource = case((func.json_type(valid_entry) == "object", valid_entry), else_="{}")
        search_text = case(
            (func.json_type(resource, "$.search_text") == "text", func.json_extract(resource, "$.search_text")),
            else_=None,
        )
    else:
        empty = cast(literal("{}"), JSON)
        root = case((func.json_typeof(data.c.value) == "object", data.c.value), else_=empty)
        raw_title = func.json_extract_path(root, "search_title")
        title = case(
            (func.json_typeof(raw_title) == "string", func.json_extract_path_text(root, "search_title")),
            else_=None,
        )
        raw_resources = func.json_extract_path(root, "resources")
        resources = case((func.json_typeof(raw_resources) == "object", raw_resources), else_=empty)
        entries = func.json_each(resources).table_valued(column("key", String), column("value", JSON))
        resource = case((func.json_typeof(entries.c.value) == "object", entries.c.value), else_=empty)
        raw_text = func.json_extract_path(resource, "search_text")
        search_text = case(
            (func.json_typeof(raw_text) == "string", func.json_extract_path_text(resource, "search_text")),
            else_=None,
        )

    # 将类型防护放进展开函数参数，不依赖 WHERE 条件的求值顺序。
    resource_match = select(literal(1)).select_from(entries).where(
        search_text.like(pattern, escape="\\"),
    ).correlate(data).exists()
    return statement.where(or_(title.like(pattern, escape="\\"), resource_match))


def query_plugin_data(
    execute_query: Callable[[Callable[[Session], list[dict[str, Any]]]], list[dict[str, Any]]],
    plugin_id: str,
    key_prefix: str,
    query: str = "",
) -> list[dict[str, Any]]:
    """
    通过现有查询执行入口读取候选，不创建会话或提交调用方事务。

    :param execute_query: 现有 plugindata._execute_sync_query 绑定方法
    :param plugin_id: 当前插件实例类名，不接受外部查询覆盖
    :param key_prefix: 字面键前缀
    :param query: 媒体名或资源搜索文本的完整字面关键词
    :return: 会话内转换完成的普通 key/value 字典列表
    """
    def read(session: Session) -> list[dict[str, Any]]:
        """
        在执行入口提供的会话内查询并转换结果。

        :param session: 调用方会话或现有短事务会话
        :return: 不携带 ORM 或会话的普通数据
        """
        statement = _query_statement(plugin_id, key_prefix, query, session.get_bind().dialect.name)
        return [dict(row) for row in session.execute(statement).mappings()]

    return execute_query(read)
