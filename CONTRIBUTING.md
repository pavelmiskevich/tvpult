# Contributing

Thanks for helping. Small pull requests are the easiest to review — one app, one fix, one TV model.

## Add an app to the catalog

tvpult finds apps on a TV by asking for each id in [`src/tvpult/apps.json`](src/tvpult/apps.json): the TV answers
404 for an app it doesn't have. 2022+ TVs never list their apps, so an app that isn't in the catalog can't be
shown — that is why a new entry helps everyone with the same service.

```json
"kinopoisk": {
  "names": {"en": "Kinopoisk", "ru": "Кинопоиск"},
  "regions": ["RU"],
  "ids": [
    {"id": "111399000037", "seen": [2017, 2022]}
  ]
}
```

- **alias** (the key): short, lowercase — letters, digits, `.` `_` `-`. It is what people type
  (`tvpult launch living kinopoisk`) and what `[ui] quick_apps` refers to.
- **names**: `en` is required; add `ru` if the name differs in Russian. Shown on the buttons.
- **regions**: where the service works — `["*"]` for everywhere, otherwise country codes (`["RU"]`, `["DE", "AT"]`).
- **ids**: the Tizen app ids, one per line. The same app often has a different id on different model years; list
  them all. `seen` is the model years you **confirmed** the id on (the first two digits of the TV's platform code,
  `22_PONTUSM_…` → 2022). Ids seen on the TV's year are tried first. Leave `seen` out if you haven't checked.

### Finding the id

- **2017–2021 TVs** list their apps: `tvpult discover <tv> --grep netflix` prints every installed app with its id.
- **Any TV**: check an id you found elsewhere —
  `curl http://<tv-address>:8001/api/v2/applications/<id>` answers with the app's name if it's installed, 404 if not.
- `tvpult catalog` shows what is already there.

Before you send it: `uv run pytest tests/test_catalog.py` — it checks the file against the schema (and that no id
appears under two apps). In the pull request, say which TV model year(s) you checked the id on.

You can also try an id without changing tvpult: put it in your own `devices.toml` under `[apps]`
(`myapp = "3201234567890"`), the remote picks it up after a restart.
