"""Regression: the stock Telegram adapter is loaded after our plugin under its runtime namespace."""
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"


def _plugin():
    spec = importlib.util.spec_from_file_location("topic_icons_late_probe", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_adapter_loaded_after_register_is_shimmed(monkeypatch):
    plugin = _plugin()
    initial = ModuleType("plugins.platforms.telegram.adapter")
    initial.TelegramAdapter = type("TelegramAdapter", (), {
        "rename_dm_topic": lambda self, chat_id, thread_id, name: None,
    })
    monkeypatch.setitem(sys.modules, initial.__name__, initial)
    monkeypatch.delitem(sys.modules, "hermes_plugins.platforms__telegram.adapter", raising=False)
    hooks = {}
    ctx = SimpleNamespace(register_hook=lambda name, callback: hooks.setdefault(name, callback))
    plugin.register(ctx)
    assert getattr(initial.TelegramAdapter.rename_dm_topic, plugin._SHIM_MARK, False)

    runtime = ModuleType("hermes_plugins.platforms__telegram.adapter")
    runtime.TelegramAdapter = type("TelegramAdapter", (), {
        "rename_dm_topic": lambda self, chat_id, thread_id, name: None,
    })
    monkeypatch.setitem(sys.modules, runtime.__name__, runtime)
    assert "on_session_start" in hooks
    hooks["on_session_start"](platform="telegram", session_id="new-topic")
    assert getattr(runtime.TelegramAdapter.rename_dm_topic, plugin._SHIM_MARK, False)
