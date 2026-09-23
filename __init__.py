"""Hermes plugin: semantic icons for auto-titled Telegram DM topics.

Wires :mod:`picker` to the gateway's ``pre_topic_rename`` hook. One catalog and one per-chat
recency ring live for the plugin's lifetime; the auxiliary LLM call runs off the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Optional

# Plugin directories are loaded as loose modules, not packages: import the sibling by path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from picker import RecentIcons, TopicIconCatalog, choose_topic_icon  # noqa: E402

logger = logging.getLogger(__name__)

_catalog = TopicIconCatalog()
_recent = RecentIcons()


async def pre_topic_rename(
    platform: str, chat_id: str, title: str, fetch_icon_catalog: Any = None, **_: Any,
) -> Optional[dict]:
    if platform != "telegram" or fetch_icon_catalog is None:
        return None
    if not await _catalog.ensure_loaded(fetch_icon_catalog):
        return None
    emoji = await asyncio.to_thread(choose_topic_icon, title, _catalog, recent=_recent.get(chat_id))
    if not emoji:
        return None
    _recent.push(chat_id, emoji)
    logger.info("Topic icon %s for '%s'", emoji, title)
    return {"icon_custom_emoji_id": _catalog.lookup(emoji)}


def register(ctx) -> None:
    ctx.register_hook("pre_topic_rename", pre_topic_rename)
