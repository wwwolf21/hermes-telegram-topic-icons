# hermes-telegram-topic-icons

Semantic icons for auto-titled Telegram DM topics in [Hermes Agent](https://github.com/NousResearch/hermes-agent).

By default a topic in the Telegram sidebar shows the first letter of its name. This plugin picks the
catalog icon that best matches the generated title — 🛒 for a shopping assistant, 🪪 for an
auth.log investigation, ⛅ for Cloudflare DNS — in the same `editForumTopic` call the gateway
already makes. Names stay clean; only the bubble changes.

## Requirements

- Hermes Agent with the `pre_topic_rename` gateway hook (PR NousResearch/hermes-agent#119907).
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
