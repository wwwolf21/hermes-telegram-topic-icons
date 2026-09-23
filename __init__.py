"""Hermes plugin: semantic icons for auto-titled Telegram DM topics.

Two ways in, picked at load time:

1. **Hook** — a Hermes core with the ``pre_topic_rename`` gateway hook (NousResearch/hermes-agent
   PR #119907) calls us with the title and a catalog fetcher; our answer rides the same
   ``editForumTopic`` as the rename.
2. **Shim** — a stock core has no such hook. We wrap ``TelegramAdapter.rename_dm_topic`` once,
   lazily, with a version that picks the icon and passes ``icon_custom_emoji_id`` to
   ``bot.edit_forum_topic``. The wrapper only ever adds one kwarg to the Bot API call; if the
   adapter's rename signature ever changes, the shim steps aside and the plain rename stays.

Same picker (:mod:`picker`) behind both. One catalog and one per-chat recency ring per process;
the auxiliary LLM call runs off the event loop.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import os
import sys
from typing import Any, Optional

# Plugin directories are loaded as loose modules, not packages: import the sibling by path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from picker import RecentIcons, TopicIconCatalog, choose_topic_decor  # noqa: E402

logger = logging.getLogger(__name__)

HOOK = "pre_topic_rename"
_ADAPTER_MODULES = ("hermes_plugins.platforms__telegram.adapter", "plugins.platforms.telegram.adapter")
_SHIM_MARK = "_topic_icons_shim"

_catalog = TopicIconCatalog()
_recent = RecentIcons()


async def pick_decor(chat_id: str, title: str, fetch_icon_catalog: Any) -> tuple:
    """``(custom_emoji_id, short_name)`` for ``title`` — either None; shared by the hook and the shim."""
    if fetch_icon_catalog is None or not await _catalog.ensure_loaded(fetch_icon_catalog):
        return None, None
    emoji, name = await asyncio.to_thread(choose_topic_decor, title, _catalog, recent=_recent.get(str(chat_id)))
    if emoji:
        _recent.push(str(chat_id), emoji)
    if name and name.casefold() == title.casefold():
        name = None
    logger.info("Topic decor %s '%s' for '%s'", emoji or "∅", name or title, title)
    return (_catalog.lookup(emoji) if emoji else None), name


async def pick_icon_id(chat_id: str, title: str, fetch_icon_catalog: Any) -> Optional[str]:
    return (await pick_decor(chat_id, title, fetch_icon_catalog))[0]


# ── 1. hook path ────────────────────────────────────────────────────────────────────────────

async def pre_topic_rename(
    platform: str, chat_id: str, title: str, fetch_icon_catalog: Any = None, **_: Any,
) -> Optional[dict]:
    if platform != "telegram":
        return None
    icon, name = await pick_decor(chat_id, title, fetch_icon_catalog)
    result = {}
    if icon:
        result["icon_custom_emoji_id"] = icon
    if name:
        result["name"] = name
    return result or None


# ── 2. shim path (stock core) ───────────────────────────────────────────────────────────────

def _shim_rename(original):
    """``rename_dm_topic`` that picks an icon first. Falls back to ``original`` on any miss."""
    @functools.wraps(original)
    async def rename_dm_topic(self, chat_id, thread_id, name, *args, **kwargs):
        bot = getattr(self, "_bot", None)
        if args or kwargs or bot is None:  # a signature we do not know: stay out of the way
            return await original(self, chat_id, thread_id, name, *args, **kwargs)
        icon, short = None, None
        try:
            icon, short = await pick_decor(chat_id, name, lambda: bot.get_forum_topic_icon_stickers())
        except Exception:
            logger.debug("Topic decor pick failed; plain rename", exc_info=True)
        if not icon and not short:
            return await original(self, chat_id, thread_id, name)
        try:
            chat_id_arg = int(chat_id)
        except (TypeError, ValueError):
            chat_id_arg = chat_id
        kwargs = {"icon_custom_emoji_id": icon} if icon else {}
        await bot.edit_forum_topic(
            chat_id=chat_id_arg, message_thread_id=int(thread_id), name=short or name, **kwargs,
        )
        logger.info("[Telegram] Renamed DM topic in chat %s thread_id=%s -> '%s'%s",
                    chat_id, thread_id, short or name, " with icon" if icon else "")
    rename_dm_topic.__dict__[_SHIM_MARK] = True
    return rename_dm_topic


def _adapter_class():
    for mod_name in _ADAPTER_MODULES:
        mod = sys.modules.get(mod_name)
        if mod is None:
            try:
                mod = __import__(mod_name, fromlist=["TelegramAdapter"])
            except Exception:
                continue
        cls = getattr(mod, "TelegramAdapter", None)
        if cls is not None:
            return cls
    return None


def install_shim() -> bool:
    """Wrap ``TelegramAdapter.rename_dm_topic`` once. True when installed (or already present)."""
    cls = _adapter_class()
    original = getattr(cls, "rename_dm_topic", None) if cls else None
    if original is None:
        return False
    if getattr(original, _SHIM_MARK, False):
        return True
    params = list(inspect.signature(original).parameters)
    if params[:4] != ["self", "chat_id", "thread_id", "name"]:
        logger.warning("TelegramAdapter.rename_dm_topic signature changed (%s); topic icons disabled", params)
        return False
    cls.rename_dm_topic = _shim_rename(original)
    logger.info("Topic icons: shimmed TelegramAdapter.rename_dm_topic (core has no %s hook)", HOOK)
    return True


def core_has_hook() -> bool:
    try:
        from hermes_cli.plugins import VALID_HOOKS
    except Exception:
        return False
    return HOOK in VALID_HOOKS


def register(ctx) -> None:
    if core_has_hook():
        ctx.register_hook(HOOK, pre_topic_rename)
        return
    if not install_shim():
        # The adapter module may not be imported yet at plugin-load time; retry on session start.
        ctx.register_hook("on_session_start", lambda **_: install_shim())
