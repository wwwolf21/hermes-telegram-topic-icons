"""One auxiliary call per new topic: title + short name + icon candidates.

The core titler (``agent.title_generator.generate_title``) already makes exactly one cheap call
per new session — ``{"title": ...}`` from the opening message. Instead of a second call for the
icon and the sidebar name, this module rides that one: it wraps the titler's ``call_llm`` binding,
appends the icon catalog and the short-name rules to the system prompt, widens the JSON schema to
``{"title", "name", "emoji"}``, and stashes the extra two fields keyed by the returned title. The
core keeps reading ``title`` from the same payload (its parser ignores extra keys); when the
gateway fires ``pre_topic_rename`` a moment later the plugin finds the stash by title and answers
without another model round-trip.

Contracts that keep this safe on a moving core:
- Only a call with ``task="title_generation"`` whose response schema declares a ``title`` property
  is touched; anything else passes through byte-identical.
- The catalog must already be loaded (first topic after a restart takes the old two-call path
  and loads it). Without it the call passes through.
- Any failure in augmentation or parsing falls back to the unmodified call/response.
"""

from __future__ import annotations

import copy
import functools
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Sequence, Tuple

from picker import TopicIconCatalog, _parse_payload, clean_short_name

logger = logging.getLogger(__name__)

TITLER_MODULE = "agent.title_generator"
_SHIM_MARK = "_topic_icons_titler_shim"
STASH_LIMIT = 64
STASH_TTL_SECONDS = 15 * 60
_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)

_MERGED_RULES = (
    "\n\nAlso return, in the same JSON object:\n"
    '- "name": a SHORT form of the title for a narrow sidebar — 2 to 4 words, at most 28 characters, '
    "key noun FIRST, same language as the title, exact technical terms kept, verbs like "
    "'настроить/сделать/проверить/set up/fix' and filler dropped, no emoji, no trailing punctuation. "
    "Examples: 'Настроить помощника-покупателя для Авито' -> 'Авито-помощник'; "
    "'Оценить интеграцию computer-use-linux MCP' -> 'MCP computer-use'; "
    "'Hermes gateway degraded after restart' -> 'Gateway degraded'.\n"
    '- "emoji": THREE different catalog emoji that best represent the subject, most specific first. '
    "Only catalog emoji are valid. Generic icons (💻 🤖 💬 📝 ❗ ❓) are last resorts; spread across the whole "
    "catalog and prefer a less obvious icon whose meaning fits over an obvious one used recently.\n"
    "__RECENT__"
    "\nCatalog (emoji: meaning):\n__CATALOG__\n\n"
    'Reply with JSON only: {"title": "...", "name": "...", "emoji": ["first", "second", "third"]}'
)
_RECENT_RULE = ("- These icons were used for recent topics; do NOT propose them unless nothing else in the "
                "catalog fits at all: __RECENT_LIST__\n")


def _title_key(title: str) -> str:
    return _NON_WORD_RE.sub(" ", str(title or "")).casefold().strip()


class DecorStash:
    """``title -> (emoji candidates, short name)`` handed from the titler call to the rename hook.

    Bounded and time-limited: a title the gateway never renames (CLI sessions, disabled auto-rename)
    must not pin memory. Keyed by a whitespace/punctuation-insensitive fold of the title so the
    gateway's sanitizer (which may trim or collapse characters) still finds it.
    """

    def __init__(self, limit: int = STASH_LIMIT, ttl: float = STASH_TTL_SECONDS) -> None:
        self._limit = limit
        self._ttl = ttl
        self._items: "OrderedDict[str, Tuple[float, List[str], Optional[str]]]" = OrderedDict()
        self._lock = threading.Lock()

    def put(self, title: str, candidates: List[str], name: Optional[str]) -> None:
        key = _title_key(title)
        if not key:
            return
        with self._lock:
            self._items[key] = (time.monotonic(), list(candidates), name)
            self._items.move_to_end(key)
            while len(self._items) > self._limit:
                self._items.popitem(last=False)

    def pop(self, title: str) -> Optional[Tuple[List[str], Optional[str]]]:
        key = _title_key(title)
        with self._lock:
            item = self._items.pop(key, None)
        if item is None:
            return None
        stamp, candidates, name = item
        if time.monotonic() - stamp > self._ttl:
            return None
        return candidates, name


def _schema_properties(extra_body: Any) -> Optional[Dict[str, Any]]:
    try:
        return extra_body["response_format"]["json_schema"]["schema"]["properties"]
    except (KeyError, TypeError):
        return None


def _is_titler_call(kwargs: Dict[str, Any]) -> bool:
    if kwargs.get("task") != "title_generation":
        return False
    props = _schema_properties(kwargs.get("extra_body"))
    messages = kwargs.get("messages")
    return (isinstance(props, dict) and "title" in props and isinstance(messages, list) and bool(messages)
            and messages[0].get("role") == "system" and isinstance(messages[0].get("content"), str))


def augment_titler_kwargs(kwargs: Dict[str, Any], catalog: TopicIconCatalog, recent: Sequence[str] = ()) -> Dict[str, Any]:
    """Titler ``call_llm`` kwargs widened to ask for ``name`` and ``emoji`` too (deep-copied)."""
    out = copy.deepcopy(kwargs)
    recent_rule = _RECENT_RULE.replace("__RECENT_LIST__", " ".join(recent)) if recent else ""
    addition = _MERGED_RULES.replace("__RECENT__", recent_rule).replace("__CATALOG__", catalog.legend())
    system = out["messages"][0]
    # The core's prompt ends with its own "Reply with JSON only" line; ours supersedes it.
    system["content"] = re.sub(r"\n*Reply with JSON only:.*$", "", system["content"], flags=re.DOTALL) + addition
    schema = out["extra_body"]["response_format"]["json_schema"]["schema"]
    props = schema["properties"]
    props["name"] = {"type": "string"}
    props["emoji"] = {"type": "array", "items": {"type": "string"}}
    required = list(schema.get("required") or [])
    schema["required"] = required + [k for k in ("name", "emoji") if k not in required]
    out["max_tokens"] = max(int(out.get("max_tokens") or 0), 256)
    return out


def _response_text(response: Any) -> Optional[str]:
    try:
        return response.choices[0].message.content
    except (AttributeError, IndexError, KeyError, TypeError):
        return None


def harvest(response: Any, stash: DecorStash) -> Optional[str]:
    """Stash ``(emoji, name)`` from a merged reply; returns the title it was stashed under."""
    payload = _parse_payload(_response_text(response) or "")
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    emoji = payload.get("emoji")
    candidates = [e for e in emoji if isinstance(e, str)] if isinstance(emoji, list) else []
    name = clean_short_name(payload.get("name"))
    if not candidates and not name:
        return None
    stash.put(title, candidates, name)
    return title.strip()


def wrap_call_llm(original, catalog: TopicIconCatalog, stash: DecorStash, recent_for_titler=lambda: ()):
    @functools.wraps(original)
    def call_llm(*args, **kwargs):
        if args or not _is_titler_call(kwargs) or not catalog.emojis:
            return original(*args, **kwargs)
        try:
            merged = augment_titler_kwargs(kwargs, catalog, list(recent_for_titler()))
        except Exception:
            logger.debug("Titler augmentation failed; plain title call", exc_info=True)
            return original(**kwargs)
        response = original(**merged)
        try:
            title = harvest(response, stash)
            if title:
                logger.debug("Merged titler call stashed decor for '%s'", title)
        except Exception:
            logger.debug("Merged titler reply not harvestable; title still flows", exc_info=True)
        return response
    call_llm.__dict__[_SHIM_MARK] = True
    return call_llm


def install(catalog: TopicIconCatalog, stash: DecorStash, recent_for_titler=lambda: ()) -> bool:
    """Wrap ``agent.title_generator.call_llm`` once. True when installed (or already present)."""
    try:
        import importlib
        titler = importlib.import_module(TITLER_MODULE)
    except Exception:
        logger.debug("Titler module unavailable; merged call disabled", exc_info=True)
        return False
    original = getattr(titler, "call_llm", None)
    if not callable(original):
        logger.warning("%s.call_llm not found; topic decor falls back to a second call", TITLER_MODULE)
        return False
    if getattr(original, _SHIM_MARK, False):
        return True
    setattr(titler, "call_llm", wrap_call_llm(original, catalog, stash, recent_for_titler))
    logger.info("Topic icons: merged into the titler call (one auxiliary request per topic)")
    return True
