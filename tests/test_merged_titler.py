"""Merged titler contracts: only the titler call is widened, the core still reads ``title`` from
the same reply, decor reaches the rename hook by title, and anything unexpected passes through."""

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import merged_titler  # noqa: E402
import picker  # noqa: E402
from picker import TopicIconCatalog  # noqa: E402

CATALOG = [{"emoji": "🪪", "custom_emoji_id": "4"}, {"emoji": "💻", "custom_emoji_id": "1"}]
TITLER_KWARGS = {
    "task": "title_generation",
    "messages": [{"role": "system", "content": "You name chat sessions.\n\nRules:\n- 3 to 7 words\n\nReply with JSON only: {\"title\": \"...\"}"},
                 {"role": "user", "content": "Посмотри auth.log, кто ломится по SSH"}],
    "max_tokens": 64, "temperature": None, "timeout": 30,
    "extra_body": {"response_format": {"type": "json_schema", "json_schema": {"name": "session_title", "strict": True, "schema": {
        "type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"], "additionalProperties": False}}}},
    "reasoning_config": {"enabled": False},
}


def _catalog():
    c = TopicIconCatalog()
    c.load(CATALOG)
    return c


def _reply(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_augment_widens_schema_and_prompt_without_touching_the_user_turn():
    out = merged_titler.augment_titler_kwargs(TITLER_KWARGS, _catalog(), recent=["💻"])
    schema = out["extra_body"]["response_format"]["json_schema"]["schema"]
    assert set(schema["properties"]) == {"title", "name", "emoji"} and set(schema["required"]) == {"title", "name", "emoji"}
    system = out["messages"][0]["content"]
    assert system.count("Reply with JSON only") == 1 and "🪪:" in system and "used for recent topics" in system
    assert out["messages"][1] == TITLER_KWARGS["messages"][1]
    assert out["max_tokens"] >= 256
    assert TITLER_KWARGS["max_tokens"] == 64  # deep copy: the core's dict is untouched


def test_wrapper_only_touches_titler_calls_and_stashes_decor():
    seen = []

    def original(**kwargs):
        seen.append(kwargs)
        return _reply('{"title": "Анализ SSH-входов в auth.log", "name": "SSH auth.log", "emoji": ["🪪", "💻"]}')

    stash = merged_titler.DecorStash()
    wrapped = merged_titler.wrap_call_llm(original, _catalog(), stash)
    other = {"task": "compression", "messages": [{"role": "system", "content": "x"}], "extra_body": {}}
    wrapped(**other)
    assert seen[-1] == other
    resp = wrapped(**TITLER_KWARGS)
    assert "emoji" in seen[-1]["extra_body"]["response_format"]["json_schema"]["schema"]["properties"]
    assert '"title": "Анализ SSH-входов в auth.log"' in resp.choices[0].message.content  # core still parses title
    assert stash.pop("Анализ SSH-входов в auth.log") == (["🪪", "💻"], "SSH auth.log")
    assert stash.pop("Анализ SSH-входов в auth.log") is None  # one-shot


def test_stash_key_is_tolerant_of_sanitizer_changes():
    stash = merged_titler.DecorStash()
    stash.put("Fix  driver: config!", ["🪪"], "Driver config")
    assert stash.pop("fix driver config") == (["🪪"], "Driver config")


def test_core_titler_reads_title_through_the_wrapper():
    """Real ``agent.title_generator.generate_title`` with the wrapper installed and a fake model."""
    from agent import title_generator

    catalog = _catalog()
    stash = merged_titler.DecorStash()
    fake = lambda **kw: _reply('{"title": "Анализ SSH-входов", "name": "SSH auth.log", "emoji": ["🪪"]}')
    with patch.object(title_generator, "call_llm", merged_titler.wrap_call_llm(fake, catalog, stash)), \
         patch.object(title_generator, "_auto_title_enabled", return_value=True):
        title = title_generator.generate_title("Посмотри auth.log, кто ломится по SSH")
    assert title == "Анализ SSH-входов"
    assert stash.pop("Анализ SSH-входов") == (["🪪"], "SSH auth.log")


def test_install_is_idempotent_and_survives_missing_titler():
    from agent import title_generator

    catalog, stash = _catalog(), merged_titler.DecorStash()
    original = title_generator.call_llm
    try:
        assert merged_titler.install(catalog, stash) is True
        first = title_generator.call_llm
        assert merged_titler.install(catalog, stash) is True and title_generator.call_llm is first
    finally:
        title_generator.call_llm = original
    with patch.object(merged_titler, "TITLER_MODULE", "agent.no_such_titler"):
        assert merged_titler.install(catalog, stash) is False
