# hermes-telegram-topic-icons

Semantic icons and short names for auto-titled Telegram DM topics in [Hermes Agent](https://github.com/NousResearch/hermes-agent).

By default a topic in the Telegram sidebar shows the first letter of its name. This plugin picks the
catalog icon that best matches the generated title — 🛒 for a shopping assistant, 🪪 for an
auth.log investigation, ⛅ for Cloudflare DNS — in the same `editForumTopic` call the gateway
already makes. Names stay clean; only the bubble changes.

## One call per topic

Hermes already makes one cheap auxiliary call per new session — the titler asks the `title_generation` model for `{"title": ...}` from your opening message. This plugin rides that call instead of adding a second one: at load it wraps the titler's `call_llm`, appends the icon catalog and the short-name rules to the system prompt and widens the JSON schema to `{"title", "name", "emoji"}`. The core keeps reading `title` from the same reply; the other two fields wait in a small stash until the gateway renames the topic a moment later. No extra model round-trip, no change to the core.

- **Short name** — 2-4 words, max 28 chars, key noun first, verbs and filler dropped, same language, technical terms exact. Becomes the **topic name only**; the session title Hermes stores (`hermes sessions`, TUI) is untouched. A collapsed Telegram sidebar shows ~12 characters, so "Настроить помощника-покупателя для Авито" reads as "Настроить пом…вито" without it and "Авито-помощник" with it.
- **Icon** — Telegram allows only its own ~112 forum-topic stickers, so variety comes from the picker, not the catalog: every sticker carries a broad legend (🍓 Raspberry Pi, 🚂 CI/CD pipeline, 🐟 phishing, 🎃 haunting bug…), the model ranks three candidates, and an icon used in the last 30 topics of that chat loses to any unseen one — even a generic one. 40 real titles → 31 unique icons, no icon above 8%.
- **Fallback** — the first topic after a gateway restart (catalog not loaded yet), or a core whose titler shape changed, takes a second call for the decor; the title is never at risk.

Point `auxiliary.title_generation` at a fast model (`hermes config set auxiliary.title_generation.provider anthropic` / `.model claude-sonnet-5`); by default it is your main model, which is overkill for seven words. Measured on 40 real titles: claude-sonnet-5 2.2 s, clean Russian, 31 unique icons; gpt-6-sol 3 s; claude-haiku-4-5 2 s but transliterates technical terms.

Live sample (gpt-6-sol, one call each, 2.5-4.6 s):

| opening message | title | name | icon |
|---|---|---|---|
| бэкап state.db в S3 через restic падает по таймеру | Починить сбой бэкапа state.db в S3 | Бэкап state.db в S3 | 📁 |
| помощник-покупатель для Авито, мониторить объявления | Настроить помощника-покупателя для Авито | Авито-помощник | 🛒 |
| auth.log, кто ломится по SSH, забань топ-10 через ufw | Найти топ-10 IP в auth.log и забанить через ufw | IP SSH-атак | 🪖 |
| ценообразование на подписку, три тарифа, unit-экономика | Рассчитать unit-экономику трёх тарифов | Unit-экономика тарифов | 💰 |

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
