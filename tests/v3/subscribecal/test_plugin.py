"""订阅日历 V3 媒体身份与宿主查询合同回归测试。"""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest
from app.plugins.subscribecal import CalendarInfo, SubscribeCal
from app.schemas.subscribe import Subscribe
from app.schemas.types import MediaSource, MediaType

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_SOURCE = REPO_ROOT / "plugins.v3" / "subscribecal" / "__init__.py"


def make_subscribe(**kwargs) -> Subscribe:
    """构造 V3 订阅测试对象。"""
    values = {
        "name": "示例剧集",
        "year": "2026",
        "type": MediaType.TV.value,
        "season": 1,
        "date": "2026-08-27 00:00:00",
        "media_source": MediaSource.TMDB,
        "media_id": "12345",
    }
    values.update(kwargs)
    return Subscribe(**values)


def test_tmdb_identity_builds_legacy_compatible_calendar_key() -> None:
    """TMDB 订阅应从 media_id 取得原生 ID，并保持既有日历缓存键形态。"""
    subscribe = make_subscribe()

    assert SubscribeCal.get_tmdb_id(subscribe) == 12345
    assert SubscribeCal.get_sub_key(subscribe) == "__key_12345_2026_1__"


def test_non_tmdb_identity_is_skipped() -> None:
    """非 TMDB 订阅不得误调用 TMDB，也不得生成可混淆的缓存键。"""
    subscribe = make_subscribe(media_source=MediaSource.Douban, media_id="12345")

    assert SubscribeCal.get_tmdb_id(subscribe) is None
    assert SubscribeCal.get_sub_key(subscribe) is None


@pytest.mark.parametrize("media_id", (None, "", " ", "0", "not-a-number"))
def test_invalid_tmdb_identity_is_skipped(media_id: Optional[str]) -> None:
    """空值、零值和非数字 TMDB ID 不得进入日历查询。"""
    subscribe = SimpleNamespace(
        media_source=MediaSource.TMDB,
        media_id=media_id,
    )

    assert SubscribeCal.get_tmdb_id(subscribe) is None
    assert SubscribeCal.get_sub_key(subscribe) is None


def test_search_uses_media_id_for_tmdb_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """日历查询应把 V3 media_id 转换为 TMDB 链所需的整数参数。"""
    calls = []

    class ChainStub:
        """记录 TMDB 查询参数的链替身。"""

        def tmdb_info(self, **kwargs):
            calls.append(kwargs)
            return {"episodes": [{"id": 1, "air_date": "2026-08-27", "episode_number": 1}]}

    class CacheStub:
        """避免测试触碰宿主缓存后端的查询替身。"""

        def exists(self, **kwargs):
            return False

        def set(self, **kwargs):
            return None

    plugin = object.__new__(SubscribeCal)
    plugin.chain = ChainStub()
    plugin._search_sub_region = "test"
    module = __import__("app.plugins.subscribecal", fromlist=["result_cache", "fresh"])
    monkeypatch.setattr(module, "result_cache", CacheStub())
    monkeypatch.setattr(module, "fresh", lambda _value: nullcontext())

    result = plugin.search_sub(make_subscribe(), cache=False)

    assert result is not None
    assert calls == [{
        "tmdbid": 12345,
        "mtype": MediaType.TV,
        "season": 1,
    }]


def test_average_time_queries_download_history_by_media_identity(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    """下载历史查询必须同时传递 TMDB 来源和 media_id。"""
    calls = []

    class DownloadHistoryStub:
        """记录下载历史查询参数的替身。"""

        def get_last_by(self, **kwargs):
            calls.append(kwargs)
            return []

    module = __import__("app.plugins.subscribecal", fromlist=["DownloadHistoryOper"])
    monkeypatch.setattr(module, "DownloadHistoryOper", DownloadHistoryStub)
    plugin = object.__new__(SubscribeCal)
    plugin._interval_minutes = 15

    result = plugin.generate_average_time(
        make_subscribe(),
        [CalendarInfo(id=1, air_date="2026-08-27", episode_number=1)],
    )

    assert result is None
    assert calls == [{
        "mtype": MediaType.TV.value,
        "title": "示例剧集",
        "year": "2026",
        "season": "S01",
        "media_source": MediaSource.TMDB,
        "media_id": "12345",
    }]


def test_v3_source_has_no_legacy_subscribe_identity_access() -> None:
    """V3 实现不得恢复对已删除 Subscribe.tmdbid 的业务访问。"""
    source = PLUGIN_SOURCE.read_text(encoding="utf-8")

    assert "sub.tmdbid" not in source
    assert "tmdbid=sub" not in source
    assert "app.db.downloadhistory_oper" not in source
    assert "app.db.subscribe_oper" not in source


def test_package_metadata_matches_plugin_version() -> None:
    """V3 索引、插件类版本与旧索引禁用标记必须一致。"""
    v3_package = json.loads((REPO_ROOT / "package.v3.json").read_text(encoding="utf-8"))
    v2_package = json.loads((REPO_ROOT / "package.v2.json").read_text(encoding="utf-8"))

    assert v3_package["SubscribeCal"]["version"] == "2.0.1"
    assert v3_package["SubscribeCal"]["name"] == "订阅日历（dibin）"
    assert v3_package["SubscribeCal"]["author"] == "dibin666"
    assert v3_package["SubscribeCal"]["system_version"] == ">=3.0.0"
    assert v2_package["SubscribeCal"]["v3"] is False
    assert SubscribeCal.plugin_version == v3_package["SubscribeCal"]["version"]


def test_dashboard_metadata_declares_default_entry() -> None:
    """V3 仪表盘聚合器应能发现插件的默认仪表盘入口。"""
    plugin = object.__new__(SubscribeCal)
    assert plugin.get_dashboard_meta() == [{
        "key": "",
        "name": "订阅日历（dibin）",
    }]
