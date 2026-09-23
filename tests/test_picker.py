"""Picker contracts: catalog-only answers, specific > generic, unseen > recent, sidebar-length
names cut by words never mid-word, failures swallowed."""

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import picker  # noqa: E402
from picker import (  # noqa: E402
    NAME_MAX_CHARS, RecentIcons, TopicIconCatalog, choose_topic_decor, choose_topic_icon, clean_short_name,
    rank_candidates,
)

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


def test_short_name_fits_sidebar_by_words_never_mid_word():
    assert clean_short_name('  "Авито-помощник". ') == "Авито-помощник"
    assert clean_short_name("🛒 Авито помощник ✅") == "Авито помощник"
    assert clean_short_name("one two three four five six") == "one two three four"
    long = "Настройка маршрутизации split-tunnel через sing-box"
    cut = clean_short_name(long)
    assert len(cut) <= NAME_MAX_CHARS and long.startswith(cut) and not long[len(cut)].isalnum()
    assert clean_short_name("x" * (NAME_MAX_CHARS + 1)) is None  # never truncate an identifier
    assert clean_short_name("") is None and clean_short_name(None) is None and clean_short_name("...") is None


def test_decor_returns_name_and_icon_together_and_degrades_per_field():
    with patch.object(picker, "call_llm", return_value=_reply('{"name": "SSH audit", "emoji": ["🪪"]}')):
        assert choose_topic_decor("Analyse SSH login failures in auth.log", _catalog()) == ("🪪", "SSH audit")
    with patch.object(picker, "call_llm", return_value=_reply('{"name": "SSH audit", "emoji": ["🐉"]}')):
        assert choose_topic_decor("x", _catalog()) == (None, "SSH audit")
    with patch.object(picker, "call_llm", return_value=_reply('{"emoji": ["🪪"]}')):
        assert choose_topic_decor("x", _catalog()) == ("🪪", None)
    with patch.object(picker, "call_llm", side_effect=RuntimeError("aux down")):
        assert choose_topic_decor("x", _catalog()) == (None, None)


def _load_plugin():
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("topic_icons_plugin", os.path.join(root, "__init__.py"))
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    return plugin


class _Ctx:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, cb):
        self.hooks[name] = cb


@pytest.mark.asyncio
async def test_hook_returns_catalog_id_and_feeds_recency():
    plugin = _load_plugin()

    async def fetch():
        return CATALOG

    with patch.object(picker, "call_llm", return_value=_reply('{"name": "SSH", "emoji": ["🪪", "⚡"]}')):
        first = await plugin.pre_topic_rename(platform="telegram", chat_id="c", title="SSH", fetch_icon_catalog=fetch)
        second = await plugin.pre_topic_rename(platform="telegram", chat_id="c", title="SSH #2", fetch_icon_catalog=fetch)
    assert first == {"icon_custom_emoji_id": "4"}  # name identical to the title is not an override
    assert second == {"icon_custom_emoji_id": "2", "name": "SSH"}
    assert await plugin.pre_topic_rename(platform="discord", chat_id="c", title="x", fetch_icon_catalog=fetch) is None


def test_register_prefers_hook_when_core_has_it():
    plugin = _load_plugin()
    with patch.object(plugin, "core_has_hook", return_value=True), patch.object(plugin, "install_shim") as shim:
        ctx = _Ctx()
        plugin.register(ctx)
    assert list(ctx.hooks) == ["pre_topic_rename"]
    shim.assert_not_called()


@pytest.mark.asyncio
async def test_shim_adds_icon_to_stock_rename_and_steps_aside_on_unknown_signature():
    plugin = _load_plugin()
    calls = []

    class Bot:
        async def get_forum_topic_icon_stickers(self):
            return CATALOG

        async def edit_forum_topic(self, **kw):
            calls.append(kw)

    class Adapter:
        _bot = Bot()

        async def rename_dm_topic(self, chat_id, thread_id, name):
            await self._bot.edit_forum_topic(chat_id=int(chat_id), message_thread_id=int(thread_id), name=name)

    with patch.object(plugin, "_adapter_class", return_value=Adapter):
        assert plugin.install_shim() is True
        assert plugin.install_shim() is True  # idempotent
    adapter = Adapter()
    with patch.object(picker, "call_llm", return_value=_reply('{"name": "SSH audit", "emoji": ["🪪"]}')):
        await adapter.rename_dm_topic("7", "42", "Analyse SSH login failures")
    with patch.object(picker, "call_llm", side_effect=RuntimeError("aux down")):
        await adapter.rename_dm_topic("7", "43", "Anything")
    assert calls[0] == {"chat_id": 7, "message_thread_id": 42, "name": "SSH audit", "icon_custom_emoji_id": "4"}
    assert calls[1] == {"chat_id": 7, "message_thread_id": 43, "name": "Anything"}

    class Changed:
        async def rename_dm_topic(self, chat_id, topic, name):  # renamed positional: refuse to wrap
            pass

    with patch.object(plugin, "_adapter_class", return_value=Changed):
        assert plugin.install_shim() is False
