"""Hermes plugin: semantic icons and sidebar-length names for auto-titled Telegram DM topics.

One auxiliary call per topic: the core titler's request is widened (``merged_titler``) so the same
reply carries ``title``, ``name`` and ``emoji``; the rename hook then answers from a stash without
a second model round-trip. The stash misses when the icon catalog was not loaded yet (first topic after a restart); that
topic takes the two-call path and warms the catalog. This plugin is loaded before the runtime
Telegram adapter in many gateway configurations: the stock-core shim rechecks the runtime class
at the first session boundary instead of trusting the source-tree class patched at discovery.

Two ways into the rename, picked at load time:

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
import merged_titler  # noqa: E402
from picker import RecentIcons, TopicIconCatalog, choose_topic_decor, rank_candidates  # noqa: E402

logger = logging.getLogger(__name__)

HOOK = "pre_topic_rename"
_ADAPTER_MODULES = ("hermes_plugins.platforms__telegram.adapter", "plugins.platforms.telegram.adapter")
_SHIM_MARK = "_topic_icons_shim"

_catalog = TopicIconCatalog()
_recent = RecentIcons()
_stash = merged_titler.DecorStash()
_last_chat_for_titler = {"key": ""}


async def pick_decor(chat_id: str, title: str, fetch_icon_catalog: Any) -> tuple:
    """``(custom_emoji_id, short_name)`` for ``title`` — either None; shared by the hook and the shim.

    Stash first (filled by the merged titler call), model second (catalog not loaded yet, or the
    titler ran without the merge). Either way the recency ring and the catalog lookup are the same.
    """
    if fetch_icon_catalog is None or not await _catalog.ensure_loaded(fetch_icon_catalog):
        return None, None
    _last_chat_for_titler["key"] = str(chat_id)
    stashed = _stash.pop(title)
    if stashed is not None:
        candidates, name = stashed
        emoji = rank_candidates(candidates, _catalog, _recent.get(str(chat_id)))
        how = "merged"
    else:
        emoji, name = await asyncio.to_thread(choose_topic_decor, title, _catalog, recent=_recent.get(str(chat_id)))
        how = "second call"
    if emoji:
        _recent.push(str(chat_id), emoji)
    if name and name.casefold() == title.casefold():
        name = None
    logger.info("Topic decor %s '%s' for '%s' (%s)", emoji or "∅", name or title, title, how)
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
    """Wrap every loaded Telegram adapter class, including late-loaded runtime namespaces."""
    installed = False
    classes = [_adapter_class()]
    classes.extend(getattr(sys.modules.get(mod_name), "TelegramAdapter", None)
                   for mod_name in _ADAPTER_MODULES)
    for cls in classes:
        original = getattr(cls, "rename_dm_topic", None) if cls else None
        if original is None:
            continue
        if getattr(original, _SHIM_MARK, False):
            installed = True
            continue
        params = list(inspect.signature(original).parameters)
        if params[:4] != ["self", "chat_id", "thread_id", "name"]:
            logger.warning("TelegramAdapter.rename_dm_topic signature changed (%s); topic icons disabled", params)
            continue
        cls.rename_dm_topic = _shim_rename(original)
        logger.info("Topic icons: shimmed %s.TelegramAdapter.rename_dm_topic", cls.__module__)
        installed = True
    return installed


def core_has_hook() -> bool:
    try:
        from hermes_cli.plugins import VALID_HOOKS
    except Exception:
        return False
    return HOOK in VALID_HOOKS


def register(ctx) -> None:
    # Recent icons for the merged prompt: the titler call carries no chat id, so we use the ring of
    # the chat that last renamed a topic — in a single-user gateway that is the right one.
    merged_titler.install(_catalog, _stash, lambda: _recent.get(_last_chat_for_titler["key"]))
    if core_has_hook():
        ctx.register_hook(HOOK, pre_topic_rename)
        return
    # Platform plugins load under a separate runtime namespace, often AFTER this plugin. Even if
    # the source-tree class was patched during discovery, re-check at the first session boundary.
    install_shim()
    ctx.register_hook("on_session_start", lambda **_: install_shim())
