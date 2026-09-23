"""Semantic Telegram forum-topic icon selection (the picker behind the ``pre_topic_rename`` hook).

Telegram forum topics carry an icon drawn from a fixed catalog of ~110 custom-emoji stickers
(``getForumTopicIconStickers``); arbitrary emoji are rejected. When Hermes auto-titles a topic
lane this plugin picks the catalog entry that best matches the title, so the sidebar bubble shows
a meaningful glyph instead of the first letter of the name. One small auxiliary call, thinking
off, constrained to the catalog; any failure degrades to "no icon", never to a broken rename.

Diversity is the hard part: a bare emoji list makes a small model collapse onto 💻/🤖 for every
technical title. Three levers keep the sidebar varied: the model sees each icon with a short
meaning (so 🛃 reads as "access control", not "customs"), it ranks three candidates instead of
one, and generic / recently-used icons only win when nothing more specific was offered.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Iterable, List, Optional, Sequence

from agent.auxiliary_client import call_llm

logger = logging.getLogger(__name__)

# The catalog only changes when Telegram ships new stickers; one refresh per gateway day is plenty.
CATALOG_TTL_SECONDS = 24 * 3600
ICON_MAX_TOKENS = 160
ICON_TIMEOUT_SECONDS = 20.0
# How many recent picks the runner remembers per chat to steer the model away from repeats.
RECENT_WINDOW = 12
# Variation selectors / ZWJ sequences: the model echoes "⚡" for the catalog's "⚡️"; normalise both sides.
_EMOJI_NOISE_RE = re.compile("[\ufe0e\ufe0f\u200d]")

# Catch-all icons: valid, but they win only when no more specific candidate was offered.
GENERIC_ICONS = frozenset({"💻", "🤖", "💬", "📝", "❗", "❓"})

# Short meanings for the catalog, keyed by normalised emoji. Unknown entries fall back to the bare
# emoji. Written for a technical/business operator's topics; the model still sees the full
# catalog, so unmatched icons stay reachable.
ICON_MEANINGS: Dict[str, str] = {
    "📰": "news, digest, announcements", "💡": "idea, proposal, brainstorm", "⚡": "performance, speed, quick fix, power",
    "🎙": "voice, audio, speech, podcast", "🔝": "top, best, ranking", "🗣": "conversation, meeting, interview",
    "🆒": "cool, fun, nice-to-have", "❗": "important, alert", "📝": "notes, writing, documentation",
    "📆": "schedule, calendar, deadline, cron", "📁": "files, storage, backup, archive", "🔎": "search, investigate, logs, analysis",
    "📣": "announcement, marketing, promotion", "🔥": "urgent, hot, incident, outage", "❤": "favourite, love, thanks",
    "❓": "question, unclear", "📈": "growth, metrics, priorities, strategy", "📉": "decline, degradation, regression",
    "💎": "premium, quality, valuable", "💰": "money, pricing, budget", "💸": "spending, costs, expenses",
    "🪙": "crypto, tokens, coins", "💱": "exchange, conversion, currency", "⁉": "confusion, bug report",
    "🎮": "gaming, controller, play", "💻": "coding, computer, generic tech", "📱": "mobile, app, phone, Telegram client",
    "🚗": "car, driving, transport", "🏠": "home, house, smart home", "💘": "dating, romance",
    "🎉": "celebration, launch, release", "‼": "critical, double alert", "🏆": "achievement, win, award",
    "🏁": "finish, completion, done, milestone", "🎬": "video, film, recording", "🎵": "music, song",
    "🔞": "adult, restricted", "📚": "learning, books, reference, study", "👑": "admin, owner, leadership",
    "⚽": "football, sport", "🏀": "basketball, sport", "📺": "TV, streaming, display, monitor",
    "👀": "review, watch, monitoring, observe", "🫦": "flirt", "🍓": "fruit, food",
    "💄": "beauty, cosmetics", "👠": "fashion, shoes", "✈": "travel, flight, migration, moving",
    "🧳": "trip, packing, relocation", "🏖": "vacation, rest, beach", "⛅": "weather, cloud, cloud hosting",
    "🦄": "startup, unicorn, magic", "🛍": "shopping, purchase", "👜": "bag, accessories",
    "🛒": "e-commerce, cart, orders", "🚂": "train, pipeline, railway", "🛥": "boat, yacht",
    "🏔": "mountain, big challenge, roadmap", "🏕": "camping, outdoors", "🤖": "bot, AI agent, automation, LLM",
    "🪩": "party, disco", "🎟": "ticket, event, access pass", "🏴‍☠": "pirate, hacking, red team",
    "🗳": "decision, vote, agreement, choice", "🎓": "education, tutorial, course", "🔭": "research, exploration, long-term",
    "🔬": "deep analysis, inspection, science", "🎶": "music, playlist", "🎤": "singing, speaking, podcast",
    "🕺": "dance, fun", "💃": "dance, fun", "🪖": "military, defense, hardening, security",
    "💼": "business, work, plan, strategy", "🧪": "testing, experiment, lab, QA", "👨‍👩‍👧‍👦": "family",
    "👶": "baby, kids", "🤰": "pregnancy", "💅": "nails, self-care",
    "🏛": "government, legal, institution, architecture", "🧮": "calculation, math, accounting, sizing", "🖨": "printing, documents, PDF",
    "👮‍♂": "police, compliance, enforcement, moderation", "🩺": "health check, diagnostics, doctor", "💊": "medicine, pills",
    "💉": "injection, vaccine, medical", "🧼": "cleanup, hygiene, refactor, sanitize", "🪪": "identity, auth, credentials, login",
    "🛃": "access control, permissions, firewall, checkpoint", "🍽": "food, restaurant, recipe", "🐟": "fish, fishing",
    "🎨": "design, UI, art, theme", "🎭": "roles, personas, theatre", "🎩": "magic trick, elegance",
    "🔮": "forecast, prediction, future", "🍹": "cocktail, leisure", "🎂": "birthday, anniversary",
    "☕": "coffee, casual chat, break", "🍣": "sushi, food", "🍔": "burger, food",
    "🍕": "pizza, food", "🦠": "virus, malware, infection", "💬": "chat, generic conversation",
    "🎄": "christmas, holidays", "🎃": "halloween, spooky", "✍": "writing, drafting, editing text",
    "⭐": "favourite, starred, rating", "✅": "done, checklist, verification, approval", "🎖": "medal, recognition",
    "🤡": "joke, absurd, clownery", "🧠": "thinking, model, intelligence, memory", "🦮": "guide, dog, assistance",
    "🐈": "cat, pet",
}

_ICON_PROMPT_TEMPLATE = (
    "You pick an icon for a chat topic. Given the topic title, rank the THREE emoji from the "
    "catalog that best represent its subject, most specific first.\n\n"
    "Rules:\n"
    "- Only catalog emoji are valid; anything else is discarded.\n"
    "- Be specific: match the domain of the topic (auth, backup, network, testing, docs, money, "
    "voice, decision...), not the fact that it is technical. Generic icons (💻 🤖 💬 📝) are last resorts.\n"
    "- Three DIFFERENT emoji, each a plausible fit on its own.\n"
    "__RECENT__"
    "\nCatalog (emoji: meaning):\n__CATALOG__\n\n"
    'Reply with JSON only: {"emoji": ["first", "second", "third"]}'
)
_RECENT_RULE = "- Recently used in this chat (avoid unless clearly the best fit): __RECENT_LIST__\n"

_ICON_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "topic_icon", "strict": True, "schema": {
        "type": "object",
        "properties": {"emoji": {"type": "array", "items": {"type": "string"}}},
        "required": ["emoji"], "additionalProperties": False}},
}


def _normalize_emoji(value: str) -> str:
    return _EMOJI_NOISE_RE.sub("", str(value or "")).strip()


class TopicIconCatalog:
    """Emoji -> ``custom_emoji_id`` map from ``getForumTopicIconStickers``, refreshed lazily.

    ``fetch`` is any awaitable returning the sticker list (adapter-injected so the catalog stays
    transport-agnostic and testable without a bot). Entries are keyed by normalised emoji.
    """

    def __init__(self, ttl_seconds: float = CATALOG_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._loaded_at: float = 0.0
        self._by_emoji: Dict[str, str] = {}
        self._lock = threading.Lock()

    @property
    def emojis(self) -> List[str]:
        return list(self._by_emoji)

    def lookup(self, emoji: str) -> Optional[str]:
        return self._by_emoji.get(_normalize_emoji(emoji))

    def is_fresh(self) -> bool:
        return bool(self._by_emoji) and (time.monotonic() - self._loaded_at) < self._ttl

    def load(self, stickers: Iterable[Any]) -> None:
        """Replace the catalog from sticker objects (PTB ``Sticker`` or raw dicts)."""
        mapping: Dict[str, str] = {}
        for sticker in stickers or ():
            emoji = getattr(sticker, "emoji", None) if not isinstance(sticker, dict) else sticker.get("emoji")
            custom_id = (getattr(sticker, "custom_emoji_id", None) if not isinstance(sticker, dict)
                         else sticker.get("custom_emoji_id"))
            if emoji and custom_id:
                mapping.setdefault(_normalize_emoji(emoji), str(custom_id))
        with self._lock:
            self._by_emoji = mapping
            self._loaded_at = time.monotonic()

    async def ensure_loaded(self, fetch) -> bool:
        """Refresh through ``fetch()`` when stale; True when the catalog has entries afterwards."""
        if self.is_fresh():
            return True
        try:
            self.load(await fetch())
        except Exception:
            logger.debug("Forum topic icon catalog refresh failed", exc_info=True)
        return bool(self._by_emoji)

    def legend(self) -> str:
        return "\n".join(f"{e}: {ICON_MEANINGS.get(e, '')}".rstrip(": ") for e in self._by_emoji)


class RecentIcons:
    """Per-key ring of recently chosen emoji (one ring per chat) so consecutive topics differ."""

    def __init__(self, window: int = RECENT_WINDOW) -> None:
        self._window = window
        self._rings: Dict[str, Deque[str]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> List[str]:
        with self._lock:
            return list(self._rings.get(key, ()))

    def push(self, key: str, emoji: str) -> None:
        with self._lock:
            self._rings.setdefault(key, deque(maxlen=self._window)).append(_normalize_emoji(emoji))


def _extract_candidates(raw: str) -> List[str]:
    """Ranked emoji list from a ``{"emoji": [...]}`` payload (or a bare string), tolerant of fences."""
    text = (raw or "").strip()
    fenced = re.search(r"\{.*\}", text, re.DOTALL)
    if fenced:
        text = fenced.group(0)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return []
    value = parsed.get("emoji") if isinstance(parsed, dict) else None
    if isinstance(value, str):
        return [value]
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def rank_candidates(candidates: Sequence[str], catalog: TopicIconCatalog, recent: Sequence[str] = ()) -> Optional[str]:
    """First catalog-valid candidate, preferring specific over generic and unseen over recent.

    Order of preference: specific+fresh > specific+recent > generic+fresh > generic+recent. The
    model's own ranking breaks ties, so a good first choice is never overridden by a worse one.
    """
    recent_set = {_normalize_emoji(r) for r in recent}
    best: Optional[tuple] = None
    for position, raw in enumerate(candidates):
        emoji = _normalize_emoji(raw)
        if not catalog.lookup(emoji):
            continue
        score = (emoji in GENERIC_ICONS, emoji in recent_set, position)
        if best is None or score < best[0]:
            best = (score, emoji)
    return best[1] if best else None


def choose_topic_icon(
    title: str, catalog: TopicIconCatalog, *, recent: Sequence[str] = (), timeout: float = ICON_TIMEOUT_SECONDS,
) -> Optional[str]:
    """Catalog emoji best matching ``title`` (normalised), or None on any miss or failure.

    Synchronous (one aux call) — callers run it off-loop via ``asyncio.to_thread`` and map the
    emoji to its ``custom_emoji_id`` through ``catalog.lookup``.
    """
    if not title or not catalog.emojis:
        return None
    recent_rule = _RECENT_RULE.replace("__RECENT_LIST__", " ".join(recent)) if recent else ""
    prompt = (_ICON_PROMPT_TEMPLATE
              .replace("__RECENT__", recent_rule)
              .replace("__CATALOG__", catalog.legend()))
    try:
        response = call_llm(
            task="title_generation",  # same cheap tier and pinning as the title call it rides behind
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": title}],
            max_tokens=ICON_MAX_TOKENS, temperature=None, timeout=timeout,
            extra_body={"response_format": _ICON_RESPONSE_FORMAT},
            reasoning_config={"enabled": False},
        )
        candidates = _extract_candidates(response.choices[0].message.content or "")
    except Exception as e:
        logger.debug("Topic icon selection failed: %s", e, exc_info=True)
        return None
    emoji = rank_candidates(candidates, catalog, recent)
    if emoji is None:
        logger.debug("Topic icon candidates %r not in catalog; leaving icon unset", candidates)
    return emoji
