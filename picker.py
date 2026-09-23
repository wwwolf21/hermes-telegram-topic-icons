"""Semantic Telegram forum-topic icon + short name selection (the picker behind ``pre_topic_rename``).

Telegram forum topics carry an icon drawn from a fixed catalog of ~110 custom-emoji stickers
(``getForumTopicIconStickers``); arbitrary emoji are rejected. When Hermes auto-titles a topic
lane this plugin picks the catalog entry that best matches the title, so the sidebar bubble shows
a meaningful glyph instead of the first letter of the name. One small auxiliary call, thinking
off, constrained to the catalog; any failure degrades to "no icon", never to a broken rename.

Diversity is the hard part: a bare emoji list makes a small model collapse onto 💻/🤖 for every
technical title. Three levers keep the sidebar varied: the model sees each icon with a short
meaning (so 🛃 reads as "access control", not "customs"), it ranks three candidates instead of
one, and generic / recently-used icons only win when nothing more specific was offered.

The same call also returns a SHORT topic name: Hermes titles sessions in 3-7 words for a list
view, but Telegram's collapsed sidebar shows ~12 characters with an ellipsis in the middle, so
"Настроить помощника-покупателя для Авито" reads as "Настроить пом…вито". The picker asks for the
2-4 word core of the title (same language, nouns first) and enforces the length locally.
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
ICON_MAX_TOKENS = 200
ICON_TIMEOUT_SECONDS = 20.0
# Telegram sidebar budget: the collapsed list shows ~12 chars, the expanded one ~28 before "…".
NAME_MAX_CHARS = 28
NAME_MAX_WORDS = 4
# How many recent picks the runner remembers per chat to steer the model away from repeats.
RECENT_WINDOW = 30
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
    "📣": "announcement, marketing, promotion", "🔥": "urgent, hot, incident, outage", "❤": "favourite, love, thanks, user care, retention",
    "❓": "question, unclear", "📈": "growth, metrics, priorities, strategy", "📉": "decline, degradation, regression",
    "💎": "premium, quality, valuable", "💰": "money, pricing, budget", "💸": "spending, costs, expenses",
    "🪙": "crypto, tokens, coins", "💱": "exchange, conversion, currency", "⁉": "confusion, bug report",
    "🎮": "gaming, controller, play, remote control, KVM, input devices", "💻": "coding, computer, generic tech", "📱": "mobile, app, phone, Telegram client",
    "🚗": "car, driving, transport, delivery, logistics, drivers (software)", "🏠": "home, house, smart home, home server, self-hosted, local network", "💘": "dating, romance, matching, recommendations, pairing devices",
    "🎉": "celebration, launch, release", "‼": "critical, double alert", "🏆": "achievement, win, award",
    "🏁": "finish, completion, done, milestone", "🎬": "video, film, recording, screen capture, demo, action/run", "🎵": "music, song, audio files, notifications sound, ringtone",
    "🔞": "adult, restricted, age gate, NSFW filter, content moderation", "📚": "learning, books, reference, study", "👑": "admin, owner, leadership",
    "⚽": "football, sport, team play, competition, league, goal tracking", "🏀": "basketball, sport, bounce, retry, ball-passing, hand-off", "📺": "TV, streaming, display, monitor",
    "👀": "review, watch, monitoring, observe", "🫦": "flirt, tone of voice, persona style, copywriting, wording", "🍓": "fruit, food, Raspberry Pi, small board computer, seasonal",
    "💄": "beauty, cosmetics, polish, visual touch-up, branding", "👠": "fashion, shoes, style, e-commerce apparel, dress code", "✈": "travel, flight, migration, moving",
    "🧳": "trip, packing, relocation, migration of data, moving servers, export/import", "🏖": "vacation, rest, beach, out-of-office, downtime, sleep mode", "⛅": "weather, cloud, cloud hosting",
    "🦄": "startup, unicorn, magic, rare case, edge case, exotic setup", "🛍": "shopping, purchase", "👜": "bag, accessories, inventory, bundle, kit, dependencies",
    "🛒": "e-commerce, cart, orders", "🚂": "train, pipeline, railway, CI/CD, sequential jobs, queue", "🛥": "boat, yacht, ship it, deployment, release cruise, premium",
    "🏔": "mountain, big challenge, roadmap", "🏕": "camping, outdoors, offline mode, minimal setup, field work", "🤖": "bot, AI agent, automation, LLM",
    "🪩": "party, disco, launch event, celebration, community, social", "🎟": "ticket, event, access pass", "🏴‍☠": "pirate, hacking, red team",
    "🗳": "decision, vote, agreement, choice", "🎓": "education, tutorial, course", "🔭": "research, exploration, long-term",
    "🔬": "deep analysis, inspection, science", "🎶": "music, playlist", "🎤": "singing, speaking, podcast",
    "🕺": "dance, fun, animation, motion, choreography of steps, workflow", "💃": "dance, fun, presentation, showcase, performance tuning", "🪖": "military, defense, hardening, security",
    "💼": "business, work, plan, strategy", "🧪": "testing, experiment, lab, QA", "👨‍👩‍👧‍👦": "family, group, team, users, multi-user, roles",
    "👶": "baby, kids, new project, onboarding, beginner, MVP", "🤰": "pregnancy, incubation, in-progress, expected release, waiting", "💅": "nails, self-care, polish, cosmetic fixes, UI details, finishing",
    "🏛": "government, legal, institution, architecture", "🧮": "calculation, math, accounting, sizing", "🖨": "printing, documents, PDF",
    "👮‍♂": "police, compliance, enforcement, moderation", "🩺": "health check, diagnostics, doctor", "💊": "medicine, pills, hotfix, patch, dependency update",
    "💉": "injection, vaccine, medical, dependency injection, immunity, hardening", "🧼": "cleanup, hygiene, refactor, sanitize", "🪪": "identity, auth, credentials, login",
    "🛃": "access control, permissions, firewall, checkpoint", "🍽": "food, restaurant, recipe, menu, serving, API serving, cookbook", "🐟": "fish, fishing, phishing, lure, catch, scraping",
    "🎨": "design, UI, art, theme", "🎭": "roles, personas, theatre", "🎩": "magic trick, elegance, formal, white-hat, etiquette",
    "🔮": "forecast, prediction, future", "🍹": "cocktail, leisure, mix of tools, blend, casual", "🎂": "birthday, anniversary, milestone date, expiry, renewal",
    "☕": "coffee, casual chat, break", "🍣": "sushi, food, Japan, raw data, slices, chunks", "🍔": "burger, food, hamburger menu, stack, layers",
    "🍕": "pizza, food, slices, sharding, partition, delivery", "🦠": "virus, malware, infection", "💬": "chat, generic conversation",
    "🎄": "christmas, holidays, seasonal, tree structure, hierarchy", "🎃": "halloween, spooky, scary bug, haunting issue, ghost process", "✍": "writing, drafting, editing text",
    "⭐": "favourite, starred, rating", "✅": "done, checklist, verification, approval", "🎖": "medal, recognition",
    "🤡": "joke, absurd, clownery, silly mistake, WTF, prank", "🧠": "thinking, model, intelligence, memory", "🦮": "guide, dog, assistance, walkthrough, tutorial, watchdog",
    "🐈": "cat, pet, cat command, logs viewing, curious, independent",
}

_ICON_PROMPT_TEMPLATE = (
    "You label chat topics for a narrow Telegram sidebar. Given a topic title, return (1) a SHORT "
    "name and (2) the THREE catalog emoji that best represent its subject, most specific first.\n\n"
    "Short name rules:\n"
    "- 2 to 4 words, at most 28 characters. The sidebar shows ~12 characters, so put the key noun FIRST.\n"
    "- Same language as the title. Keep exact technical terms, product names, filenames, error codes.\n"
    "- Drop verbs like 'настроить/сделать/проверить/set up/fix' and filler; name the SUBJECT, not the request.\n"
    "- No trailing punctuation, no quotes, no emoji in the name.\n"
    "Examples: 'Настроить помощника-покупателя для Авито' -> 'Авито-помощник'; "
    "'Оценить интеграцию computer-use-linux MCP' -> 'MCP computer-use'; "
    "'Инструменты исследования и поиска информации' -> 'Поиск и research'; "
    "'Hermes gateway degraded after restart' -> 'Gateway degraded'.\n\n"
    "Emoji rules:\n"
    "- Only catalog emoji are valid; anything else is discarded.\n"
    "- Be specific: match the domain of the topic (auth, backup, network, testing, docs, money, "
    "voice, decision...), not the fact that it is technical. Generic icons (💻 🤖 💬 📝 ❗ ❓) are last resorts.\n"
    "- Spread across the whole catalog: a sidebar full of the same three glyphs is useless. Prefer a "
    "less obvious icon whose meaning fits over an obvious one that was used recently.\n"
    "- Three DIFFERENT emoji, each a plausible fit on its own.\n"
    "__RECENT__"
    "\nCatalog (emoji: meaning):\n__CATALOG__\n\n"
    'Reply with JSON only: {"name": "...", "emoji": ["first", "second", "third"]}'
)
_RECENT_RULE = ("- These icons were used for recent topics in this chat; do NOT propose them unless nothing else "
                "in the catalog fits at all: __RECENT_LIST__\n")

_ICON_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "topic_icon", "strict": True, "schema": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "emoji": {"type": "array", "items": {"type": "string"}}},
        "required": ["name", "emoji"], "additionalProperties": False}},
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


def _parse_payload(raw: str) -> Dict[str, Any]:
    text = (raw or "").strip()
    fenced = re.search(r"\{.*\}", text, re.DOTALL)
    if fenced:
        text = fenced.group(0)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_candidates(raw: str) -> List[str]:
    """Ranked emoji list from a ``{"emoji": [...]}`` payload (or a bare string), tolerant of fences."""
    value = _parse_payload(raw).get("emoji")
    if isinstance(value, str):
        return [value]
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


_NAME_TRIM_RE = re.compile(r"^[\s\"'«»„“”‘’.,;:!?\-–—]+|[\s\"'«»„“”‘’.,;:!?\-–—]+$")


def clean_short_name(value: Any, *, max_chars: int = NAME_MAX_CHARS, max_words: int = NAME_MAX_WORDS) -> Optional[str]:
    """Model-proposed short name fitted to the sidebar budget, or None when unusable.

    Trims wrappers/punctuation, strips emoji, then cuts by WORDS (never mid-word) until it fits
    ``max_words`` and ``max_chars``. A single word longer than ``max_chars`` is rejected rather than
    truncated — a chopped identifier is worse than the long title.
    """
    if not isinstance(value, str):
        return None
    text = _EMOJI_NOISE_RE.sub("", value)
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", "", text)
    text = _NAME_TRIM_RE.sub("", " ".join(text.split()))
    words = text.split()
    if not words:
        return None
    words = words[:max_words]
    while words and len(" ".join(words)) > max_chars:
        words.pop()
    name = _NAME_TRIM_RE.sub("", " ".join(words))
    return name or None


def rank_candidates(candidates: Sequence[str], catalog: TopicIconCatalog, recent: Sequence[str] = ()) -> Optional[str]:
    """First catalog-valid candidate, preferring unseen over recent and specific over generic.

    Order of preference: fresh+specific > fresh+generic > recent+specific > recent+generic. Recency
    outranks specificity on purpose — a sidebar where every technical topic wears the same three
    "specific" glyphs is exactly the failure this plugin exists to prevent. The model's own ranking
    breaks ties, so a good first choice is never overridden by a worse one.
    """
    recent_set = {_normalize_emoji(r) for r in recent}
    best: Optional[tuple] = None
    for position, raw in enumerate(candidates):
        emoji = _normalize_emoji(raw)
        if not catalog.lookup(emoji):
            continue
        score = (emoji in recent_set, emoji in GENERIC_ICONS, position)
        if best is None or score < best[0]:
            best = (score, emoji)
    return best[1] if best else None


def choose_topic_decor(
    title: str, catalog: TopicIconCatalog, *, recent: Sequence[str] = (), timeout: float = ICON_TIMEOUT_SECONDS,
) -> tuple:
    """``(emoji, short_name)`` for ``title`` — either may be None; both None on failure.

    One aux call (synchronous — callers run it off-loop via ``asyncio.to_thread``). The emoji is
    normalised and catalog-valid; map it to ``custom_emoji_id`` through ``catalog.lookup``. The
    short name is already fitted to ``NAME_MAX_CHARS``/``NAME_MAX_WORDS``.
    """
    if not title or not catalog.emojis:
        return None, None
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
        raw = response.choices[0].message.content or ""
    except Exception as e:
        logger.debug("Topic decor selection failed: %s", e, exc_info=True)
        return None, None
    payload = _parse_payload(raw)
    candidates = _extract_candidates(raw)
    emoji = rank_candidates(candidates, catalog, recent)
    if emoji is None:
        logger.debug("Topic icon candidates %r not in catalog; leaving icon unset", candidates)
    return emoji, clean_short_name(payload.get("name"))


def choose_topic_icon(
    title: str, catalog: TopicIconCatalog, *, recent: Sequence[str] = (), timeout: float = ICON_TIMEOUT_SECONDS,
) -> Optional[str]:
    """Catalog emoji best matching ``title``, or None (icon half of :func:`choose_topic_decor`)."""
    return choose_topic_decor(title, catalog, recent=recent, timeout=timeout)[0]
