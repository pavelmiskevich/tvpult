# Contributing

Thanks for helping. Small pull requests are the easiest to review — one app, one fix, one TV model.

- [Add an app to the catalog](#add-an-app-to-the-catalog) — the most common contribution, no Python needed
- [Run it and the tests](#run-it-and-the-tests)
- [Texts and languages](#texts-and-languages)
- [Support another TV brand (drivers)](#support-another-tv-brand-drivers)
- [Pull requests](#pull-requests)

Found a security problem? Please report it privately — see [SECURITY.md](SECURITY.md).
TV not found, an app missing, a bug? Use the [issue forms](https://github.com/pavelmiskevich/tvpult/issues/new/choose).

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

## Run it and the tests

Needs [uv](https://docs.astral.sh/uv/) (it brings Python 3.12+ and the dependencies).

```sh
git clone https://github.com/pavelmiskevich/tvpult && cd tvpult
uv run pytest                # all tests; a few seconds
uv run ruff check src tests  # lint (CI runs the same; lines up to 120 characters)
uv run tvpult serve          # the remote page at http://127.0.0.1:8765/
```

- **Tests never touch the network.** `tests/conftest.py` makes every HTTP request, ARP lookup and TV connection
  fail, and gives each test its own config and data folders. A test that reached a real TV could pop up its
  "Allow?" prompt — so fake the device (see `tests/test_core.py`), never loosen the guard.
- **Security checks come with a test that fails without them** (MAC/ARP identity, allowed hosts, the custom
  header, the PIN). Before relying on a new one, break it on purpose and watch the test catch it.
- Code: Python 3.12, standard library first, no web framework; the page is one HTML file with no external
  resources (it must work offline). How things fit together and why: [docs/design.md](docs/design.md).
- CI runs the tests on Windows, Linux and macOS with Python 3.12 and 3.13.

## Texts and languages

Everything a person reads is in English and Russian: [`src/tvpult/locales/en.json`](src/tvpult/locales/en.json)
and `ru.json`. Keys starting with `page.` are for the page (`t("key", {name})` in its script, `data-i18n` in its
HTML); the rest are for the command line (`t("cli.…", name=…)`). Add every new key to **both** files with the same
`{placeholders}` — `tests/test_i18n.py` checks that, and that every key the code uses exists. Don't speak Russian?
Add the English text to both files and say so in the pull request; someone will translate it.

## Support another TV brand (drivers)

Only Samsung Tizen is supported now. A driver interface — so LG webOS, Android TV and others can be added
without touching the rest — is planned in [#12](https://github.com/pavelmiskevich/tvpult/issues/12). Until it
lands, please open an issue first. What a driver will have to provide, judging by `SamsungTv` in
`src/tvpult/devices.py`: status (online, power, what's on screen), keys, text input, apps (list and launch),
power on/off, pairing, and an identity the network can confirm (a MAC checked against the ARP table) — tvpult
never sends commands or tokens to a device it can't identify.

## Pull requests

- One topic per pull request; reference the issue with `Closes #NN` (the English keyword, so GitHub closes it).
- Tests and lint green; new behaviour comes with a test.
- No personal data in code, tests or screenshots: MAC addresses, ids, your network's addresses. Tests use
  `192.168.1.x` and `02:00:00:…` MACs.
- Update `CHANGELOG.md` under *Unreleased* for anything a user would notice.
