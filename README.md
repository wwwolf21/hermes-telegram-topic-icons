# hermes-telegram-topic-icons

Semantic icons and short names for auto-titled Telegram DM topics in [Hermes Agent](https://github.com/NousResearch/hermes-agent).

By default a topic in the Telegram sidebar shows the first letter of its name. This plugin picks the
catalog icon that best matches the generated title — 🛒 for a shopping assistant, 🪪 for an
auth.log investigation, ⛅ for Cloudflare DNS — in the same `editForumTopic` call the gateway
already makes. Names stay clean; only the bubble changes.

## Short names

Hermes titles a session in 3-7 words for list views; a collapsed Telegram sidebar shows ~12 characters with an ellipsis in the middle, so "Настроить помощника-покупателя для Авито" reads as "Настроить пом…вито". The same auxiliary call that picks the icon also returns a 2-4 word name (max 28 chars, key noun first, verbs and filler dropped, same language, technical terms exact) which becomes the **topic name only** — the session title Hermes stores (`hermes sessions`, TUI) is untouched.

On 40 real titles: 40/40 shortened, average 20 characters, e.g. `Оценить интеграцию computer-use-linux MCP` → `MCP computer-use`, `Hermes gateway degraded after restart` → `Gateway degraded`, `Анализ auth.log: топ IP по неудачным SSH-входам` → `auth.log SSH-атаки`.

## Requirements

- Any current Hermes Agent. The plugin has two ways in and picks one at load time:
  - **Hook** — cores with the `pre_topic_rename` gateway hook (NousResearch/hermes-agent#119907)
    call the plugin with the title; its answer rides the same `editForumTopic` as the rename.
  - **Shim** — stock cores get `TelegramAdapter.rename_dm_topic` wrapped once, at load, with a
    version that passes `icon_custom_emoji_id` to the same Bot API call. The wrapper adds one
    kwarg and nothing else; if upstream ever changes that method's signature the shim refuses to
    install (logged) and topics rename as before, just without icons.
- Telegram [Threaded Mode](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram#threaded-mode-dm-topics) (DM topics) enabled for your bot.
- An auxiliary model configured (the plugin rides the `title_generation` tier — same model that
  titles the topic).

## Install

```bash
git clone https://github.com/wwwolf21/hermes-telegram-topic-icons ~/.hermes/plugins/hermes_telegram_topic_icons
hermes plugins enable hermes_telegram_topic_icons
hermes gateway restart
```

No configuration. The catalog (`getForumTopicIconStickers`) is fetched once per day; every
titled topic costs one small auxiliary call (~3 s, off the event loop, after the rename).

## How it picks

Telegram allows only ~110 fixed icons and a bare emoji list makes small models collapse onto 💻
for everything technical. Three levers keep the sidebar varied:

- **Legend.** The model sees each icon with a short meaning (`🛃: access control, permissions,
  firewall`), so obscure glyphs become reachable.
- **Three ranked candidates.** `rank_candidates` prefers specific over generic (`💻 🤖 💬 📝 ❗ ❓`)
  and unseen over recently used, with model order breaking ties.
- **Per-chat recency.** The last 12 picks in a chat are fed back into the prompt.

Measured on 40 real titles: 24 unique icons, no icon above 7% share.

Any failure — catalog fetch, aux error, candidate outside the catalog — returns no icon and the
gateway renames the topic as usual.

## Tune

`ICON_MEANINGS` in `picker.py` is the legend; edit it to bias picks toward your domain.
`GENERIC_ICONS` lists the last-resort glyphs. `RECENT_WINDOW` sets the anti-repeat memory.

## Tests

```bash
PYTHONPATH=~/.hermes/hermes-agent ~/.hermes/hermes-agent/venv/bin/python -m pytest tests
```

## License

MIT
