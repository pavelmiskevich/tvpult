# Changelog

All notable changes are listed here. Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- The ARP check works on macOS and on Linux without `net-tools` (#28): MACs with one-digit octets
  (`8c:79:f5:a:1b:2c`) are understood, Linux reads `/proc/net/arp` or `ip neigh` first, and when no source of the
  table works the log and the setup wizard say so. A TV that got a new address is found at once even while its
  old ARP entry is still there (#29).
- A mistake in `devices.toml` no longer ends in a traceback — or, from the desktop shortcut, in nothing at all
  (#30). The file is checked when read; the message names the section and the key (`[tv.living] host: …`), in
  English and Russian. The command line prints it with the file path; the shortcut opens the remote, which shows
  the mistake and the file. A device id may only have `a-z`, `0-9`, `-` and `_`; `host` must be an IPv4 address of
  the home network. The setup wizard writes the file in one step, like the settings page.
- Far fewer requests to the TVs while the remote is open (#33): one status per device every 2.5 s at most,
  shared by every open page (callers arriving meanwhile wait for the same answer, so a slow TV can't make
  requests pile up); the app on screen is asked about the last one seen first, the rest every 15 s at once;
  the app list is refreshed in the background; the ARP check runs once a minute; the page waits for one poll
  before sending the next.
- A TV that is simply off no longer makes tvpult scan the networks every minute while the page is open (#34):
  the background search backs off per device (1 → 2 → 5 → 15 minutes), starts over when the device answers, a
  page opens, a button is pressed for it or the laptop moved to another network, and skips a TV switched off
  from the remote.
- No console window flashes while the remote runs from the desktop shortcut (#36): `arp`, `tailscale` and
  PowerShell are started with `CREATE_NO_WINDOW` (`tools.run_tool`). Started from `pythonw`, a console program
  otherwise gets a visible console window of its own — once a minute per TV for the ARP check.
- Rough edges of the command line and shortcut mode (#37): a busy port is one line naming it and suggesting
  `--port` (no traceback, nothing left listening; on Windows a second server can no longer share the port);
  starting faster (no reverse-DNS lookup of the listening address); `app --lan` on a remote already running
  without phone access says so; a TV dropping between two calls is "offline", not a traceback; a key sequence
  interrupted by a reconnect carries on from the failed key instead of sending the first ones again; `vol`
  explains a wrong value and asks which soundbar when there are several; a phone no longer keeps shortcut mode
  running after the window is closed; the computer's page says when the LAN address changed; Wake-on-LAN uses
  the network's real mask.

### Security
- PIN sign-in (`--lan`) has an overall limit that actually stops guessing (#31): 20 wrong PINs within an hour from
  all addresses together pause sign-in for everyone until **New PIN** is pressed on the computer, whose page says
  so and counts the wrong PINs. Before, a device using many addresses could make ~180,000 guesses a day, and
  replacing the PIN after 30 didn't slow it down. Old failures age out, the right PIN clears them, empty
  per-address entries and expired sessions are dropped.
- The HTTP server is harder to misuse (#32): no page may frame the remote (`X-Frame-Options: DENY`,
  `frame-ancestors 'none'` — a site could otherwise make you click it); the page gets a strict
  Content-Security-Policy with a fresh nonce for its one script; a client that stays silent for 10 seconds is
  dropped instead of holding a thread; an unexpected error answers a fixed text (`internal`) and logs the
  details; at most 20 keys per request; `true`/`false` are no longer taken as a volume level.

### Added
- `tvpult shortcut` on Linux and macOS (#35): on Linux a menu entry (`.desktop`, plus one on the desktop if there
  is a desktop folder), on macOS an app in `~/Applications`; both start the remote without a terminal. The remote
  opens as its own window with Chrome, Chromium, Edge or Brave on these systems too. Before, the command failed
  there with a message about `pythonw`.
- Error messages in English and Russian too (#25): every error tvpult raises has a code and parameters
  (`errors.py`), worded in the dictionaries (`err.<code>`); the API answers with the code, the page and the command
  line show it in their language. The setup wizard's reasons for not trusting a TV are translated as well.
- CONTRIBUTING.md (running the tests, adding an app, texts in both languages, what a driver for another brand
  will need, pull request rules) and issue forms: "My TV is not found" (model, year, the TV's `/api/v2/` answer
  without MACs and ids), "Add an app", bug report; vulnerabilities go to private reporting. `tvpult --version`.
  SECURITY.md now says what a signed-in phone may change; the TV menu path for removing tvpult is the same
  everywhere (it differs on 2022+ sets). (#10)
- The app catalog is a data file, `src/tvpult/apps.json` (alias, names in English/Russian, regions, ids with
  the model years they were confirmed on), checked against its schema when loaded and by a test. A TV tries the
  ids seen on its own model year first. `tvpult catalog` lists it; CONTRIBUTING.md explains how to add an app. (#5)
- English and Russian everywhere (#6): the page picks the browser's language, with a switch in **⚙ settings**
  (remembered per browser); the command line, its help and the desktop shortcut's name ("TV Remote" /
  «Пульт ТВ») follow the system language — `TVPULT_LANG=en|ru` overrides. Two JSON dictionaries in the package
  (`locales/`), embedded into the page by the server, so the PIN screen is translated too; a test checks both
  have the same keys and placeholders and that every key the code uses exists. Power results and "cable" are
  now translated on the page instead of shown in English.
- Settings page (the "⚙" tab): rename or remove TVs and soundbars, connect a TV again (the old pairing token comes
  back if the TV says no), forget its token with directions for removing tvpult from the TV's own device list,
  Wake-on-LAN on/off, "search again" for devices that moved, and pick and order the quick-launch apps from those
  the TVs have. `devices.toml` is edited in place with `tomlkit` — comments and layout stay — checked before
  writing and replaced in one step; removing a device also deletes its token and remembered address. (#4)
- Phone access over the home network with a PIN (`serve --lan`, `app --lan`, `shortcut --lan`): the computer shows
  the address, a QR code and a 6-digit PIN; a phone enters it once and stays signed in for 30 days (HttpOnly,
  SameSite=Strict cookie; only hashes stored); sessions listed with readable device names and revocable; wrong
  PINs rate-limited (5 per address / 10 min; 30 in total renew the PIN). `SECURITY.md` with the threat model;
  private vulnerability reporting enabled. (#7)
- First-run setup wizard in the remote page: with nothing configured it finds every Samsung TV, lets you name
  each one and connect it (press Allow on the TV); the TV is added to `devices.toml` only after the TV agreed,
  appending a section and leaving everything else in the file untouched. More TVs later via the "+ ТВ" tab.
  Pairing accepts only candidates from the server's own last scan, and only ARP-confirmed ones. (#3)
- `tvpult find --all` (and `find` with no config) lists every Samsung TV on the network — model, year, MAC,
  whether it is configured; TVs the ARP table does not confirm are shown with the reason. The laptop's network is
  scanned with its real mask (capped at /22); VPN, WSL and Hyper-V adapters are never scanned. (#2)
- Config and data (pairing tokens, addresses, log) live in the user's directories (`platformdirs`), not next
  to the code; `tvpult init` creates the config from the shipped example, `tvpult paths` shows where things are;
  `--config` / `--data-dir` and `TVPULT_CONFIG` / `TVPULT_DATA_DIR` override. (#1)
- The desktop shortcut runs `pythonw -m tvpult app` from the environment tvpult is installed in, so it works
  for source checkouts and installed tools alike. (#1)
- CI on Windows, Linux and macOS with Python 3.12 and 3.13, ruff lint, package check. (#8)
- Remote page and command line for Samsung Tizen TVs: keys, apps, text input, power, status.
- Google Cast soundbar/speaker volume and mute.
- Device discovery by MAC (TV) / Cast UUID, confirmed against the ARP table; address book.
- Desktop shortcut mode: starts without a console, opens the remote as an app window, stops when it closes.
- Safety: key allowlist (no service-menu keys), Host/Origin/custom-header checks, identity check before commands.
