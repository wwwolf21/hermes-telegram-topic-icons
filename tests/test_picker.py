"""Picker contracts: catalog-only answers, specific > generic, unseen > recent, failures swallowed."""

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import picker  # noqa: E402
from picker import RecentIcons, TopicIconCatalog, choose_topic_icon, rank_candidates  # noqa: E402

CATALOG = [
    {"emoji": "💻", "custom_emoji_id": "1"},
    {"emoji": "⚡️", "custom_emoji_id": "2"},
    {"emoji": "💬", "custom_emoji_id": "3"},
    {"emoji": "🪪", "custom_emoji_id": "4"},
]


def _catalog():
    c = TopicIconCatalog()
    c.load(CATALOG)
    return c


def _reply(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_lookup_ignores_variation_selectors():
    assert _catalog().lookup("⚡") == _catalog().lookup("⚡️") == "2"


def test_rejects_emoji_outside_catalog_and_tolerates_fences():
    with patch.object(picker, "call_llm", return_value=_reply('{"emoji": ["🐘"]}')):
        assert choose_topic_icon("Postgres", _catalog()) is None
    with patch.object(picker, "call_llm", return_value=_reply('```json\n{"emoji": ["💻"]}\n```')):
        assert choose_topic_icon("Fix driver", _catalog()) == "💻"


def test_aux_failure_is_swallowed():
    with patch.object(picker, "call_llm", side_effect=RuntimeError("down")):
        assert choose_topic_icon("x", _catalog()) is None


def test_rank_prefers_specific_and_unseen():
    c = _catalog()
    assert rank_candidates(["💻", "🪪", "⚡"], c) == "🪪"
    assert rank_candidates(["🪪", "⚡", "💻"], c, recent=["🪪"]) == "⚡"
    assert rank_candidates(["🪪", "💻"], c, recent=["🪪"]) == "🪪"
    assert rank_candidates(["🐘", "⚡", "🪪"], c) == "⚡"
    assert rank_candidates(["🐘"], c) is None


def test_recent_ring_is_per_key_and_bounded():
    r = RecentIcons(window=2)
    for e in ("💻", "⚡️", "🪪"):
        r.push("a", e)
    r.push("b", "💬")
    assert r.get("a") == ["⚡", "🪪"]
    assert r.get("b") == ["💬"]
    assert r.get("c") == []


@pytest.mark.asyncio
async def test_hook_returns_catalog_id_and_feeds_recency():
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("topic_icons_plugin", os.path.join(root, "__init__.py"))
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)

    async def fetch():
        return CATALOG

    with patch.object(picker, "call_llm", return_value=_reply('{"emoji": ["🪪", "⚡"]}')):
        first = await plugin.pre_topic_rename(platform="telegram", chat_id="c", title="SSH", fetch_icon_catalog=fetch)
        second = await plugin.pre_topic_rename(platform="telegram", chat_id="c", title="SSH #2", fetch_icon_catalog=fetch)
    assert first == {"icon_custom_emoji_id": "4"}
    assert second == {"icon_custom_emoji_id": "2"}
    assert await plugin.pre_topic_rename(platform="discord", chat_id="c", title="x", fetch_icon_catalog=fetch) is None
