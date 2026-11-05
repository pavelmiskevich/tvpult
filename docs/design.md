# Design notes

How tvpult works and why it is built this way.

## Shape

One Python process: a command line (`tvpult …`) and a small HTTP server with one HTML page (`tvpult serve` /
`tvpult app`). No frameworks, no CDN: the page is a single file that works offline.

| Module | Role |
|---|---|
| `cli.py` | commands, server start, shortcut mode, desktop shortcut creation |
| `server.py` | the page + JSON API, request guards, presence of open pages |
| `devices.py` | Samsung TV (REST `:8001`, WebSocket `:8002`), Cast soundbar (`:8009`), identity checks, moving devices |
| `discovery.py` | which networks to scan, port scan, identification by MAC/UUID, ARP, address book |
| `catalog.py` | allowed keys, app catalog |
| `wol.py` | Wake-on-LAN |
| `icon.py` | the shortcut icon, drawn in code |

## Setup wizard

With nothing configured the page opens a setup screen instead of an empty remote (later: the "+ ТВ" tab).
`POST /api/setup/scan` runs the full scan (#2) and gives each TV a short candidate id; `POST /api/setup/pair`
takes **only such an id** — the page can never make the server connect to an address of its choosing — and only
for ARP-confirmed TVs. One pairing at a time. The TV is written to `devices.toml` only after it said yes: a new
`[tv.<id>]` section is appended (id from the name, transliterated: «Гостиная» → `gostinaya`), the rest of the
file stays byte-for-byte as it was, and the result is re-read — if it doesn't parse, the old file comes back.
Names reach the page only as text (`textContent`), never as HTML. Wake-on-LAN defaults to on for model year
2018+ — an assumption (a 2017 set didn't wake over Wi-Fi), editable later.

## Settings page

The "⚙" tab changes what used to need editing `devices.toml`: names, Wake-on-LAN, removing a device, the
quick-launch row (`[ui] quick_apps`). The file is edited with `tomlkit`, which keeps comments, order and layout;
only the touched values change. The new text is parsed by the same loader before anything is written, then
replaces the file in one step (`os.replace`) — a failed or interrupted edit leaves the old file. One writer at a
time (a lock shared with the wizard). A comment line *above* a removed section stays: TOML can't tell whose it was.

- **Remove** deletes the section, the pairing token and the remembered address. Two taps (the first only asks)
  instead of a browser dialog.
- **Forget the token** ("Отключить") deletes only the token; the TV still lists tvpult in its own device list
  until it's removed there (the page says where). **Connect again** moves the token aside first — otherwise the
  TV would just accept the old one — and puts it back if the TV declines or the prompt times out.
- **Search again** is `relocate(force=True)`, at most once per 10 s.
- Quick-launch apps are chosen from the apps the TVs report (a TV that is off shows none), up to 12; aliases
  are checked against `[A-Za-z0-9_.-]{1,40}`.

Like the wizard, settings are open to every caller the server lets in (this computer, the tailnet, a phone
signed in with the PIN): a signed-in phone already controls the TVs fully.

## Languages

Two dictionaries, `locales/en.json` and `locales/ru.json`, flat `key → text` with `{name}` placeholders. Keys
starting with `page.` belong to the page: the server embeds both languages into it when serving it (the
`/*TVPULT_I18N*/null` slot, `<` escaped), so there is no extra request and the PIN screen — which can't call the
API yet — is translated too. The page picks `localStorage["tvpult.lang"]` (the switch in ⚙), else the first
`en`/`ru` in `navigator.languages`, else English. Static text carries `data-i18n` / `data-i18n-aria` /
`-ph` / `-alt` attributes; per-device messages in the settings are kept as keys, so switching the language
applies to them too. The rest is for the command line, in the system language: `TVPULT_LANG`, then on Windows
the display language (`GetUserDefaultUILanguage`, not the regional format), elsewhere `LC_ALL` / `LC_MESSAGES` /
`LANG`; anything that isn't Russian is English. The shortcut gets its name at creation time.

The server's status is language-neutral (`"wired": true` instead of a word for "cable"; power results are
fixed English phrases the page maps). Error messages from the devices are still English in both languages.

## Where files live

Never next to the code (installed, it sits in site-packages). `platformdirs` user directories:
config `devices.toml` in the config dir, data (pairing tokens, `addresses.json`, `tvpult.log`, the shortcut icon)
in the data dir — both `%LOCALAPPDATA%\tvpult` on Windows. A missing config is a first run, not an error.
Tests run with their own temporary directories (`tests/conftest.py`) and never touch the real ones.

The desktop shortcut runs `pythonw -m tvpult app` next to the interpreter tvpult runs in — windowless, and the
same for a source checkout and for `uv tool install`.

## Samsung specifics learnt the hard way

- **samsungtvws sleeps 1 s after every key by default** (`key_press_delay`). That made a remote feel broken.
  tvpult sets it to 0 and spaces only multi-key sequences (0.15 s). Result: ~10 ms per press.
- **2022+ TVs never answer the installed-apps request** over WebSocket. The REST endpoint
  `GET :8001/api/v2/applications/<id>` works on all tested years: 404 = not installed, `running`, `visible`.
  So apps come from a catalog of candidate ids, probed per TV; the name shown is the one the TV reports.
  2017-era sets do answer the request — `tvpult discover <tv>` lists every app with its id.
- **2017-era REST can't see built-in `org.tizen.*` apps** (e.g. the browser) but can launch them: `assume_apps`.
- **App ids differ between model years** — the catalog keeps several per app; the first one the TV knows wins.
- **Wake-on-LAN** works on sets with "Power On with Mobile"; some older sets turn Wi-Fi off entirely when off.
- **"Allow?" prompts**: the TV may ask on *every new connection* if its "Access Notification" setting is
  "Always". tvpult keeps one connection open in the server and one-shot commands don't open one at all.
- The TV reports the access point's MAC as `ssid`, not the network name.

## Finding devices

Configured addresses are only a first guess. A device is recognised by what doesn't change: a TV by the
Wi-Fi MAC it reports (`/api/v2/` → `wifiMac`), a Cast device by its UUID (`eureka_info` → `ssdp_udn`).
If a device doesn't answer at its address, or something else answers there, tvpult scans:

- only private networks, at most 4: the laptop's own — the adapter holding the default route, with its
  **real mask** (from `ifaddr`), never wider than /22 (~1k addresses); the /24 of each last known address unless
  already covered; and explicitly configured ones (/22 or smaller). VPN, WSL and Hyper-V adapters are never
  scanned; never public, loopback, link-local or CGNAT (Tailscale) ranges;
- ports 8001 and 8009, 0.5 s timeout, ~2 s for a /24; the last known address is retried with 1.5 s in parallel
  (a Wi-Fi device idle for a while answers its first connection slowly);
- at most once a minute (shared between processes through the address book).

SSDP/mDNS discovery was tried first; multicast replies didn't reach the test machine (firewall), while a
/24 port scan works everywhere.

**Every TV, not only configured ones** (`tvpult find --all`, and `find` with no config): the same networks,
port 8001 only, each answer identified (model, name, model year from the platform code `22_…` → 2022).
The ARP rule still holds: a TV whose reported MAC the network doesn't confirm is *listed* with the reason
(cabled → add `lan_mac`; no ARP entry; different MAC → not trusted) so the user isn't left wondering — but it
is never paired or controlled that way.

## Don't trust what a device says about itself

A TV's MAC in its own API answer is just a claim — any device on the LAN could send it and receive the
pairing token and everything typed into the remote (including passwords). So the claim must match the MAC
in the laptop's ARP table for that address (network layer), both during discovery and before every command.
No match or no entry → the device is not used; the token is never sent there. A cabled TV shows its Ethernet
MAC in ARP: configure it as `lan_mac`. Residual risk: ARP spoofing on the home network — an attack on the
whole network, which intercepts other traffic too. TLS pinning of the TV's `:8002` certificate would need
changes in samsungtvws and is not done yet.

## The page and its guards

- Listens on `127.0.0.1` (plus the Tailscale address with `--tailnet`). Never forward the port on a router.
- Each listener has a role: **local** (this computer — everything, including the PIN), **tailnet** (Tailscale
  vouches for the caller), **lan** (`--lan`, home network — every API call needs a session; the page itself is
  served because it holds no secrets). A phone signs in with a 6-digit PIN shown only on the computer
  (`/api/lan` answers only the local listener); it gets a random 256-bit token in an HttpOnly, SameSite=Strict
  cookie for 30 days; only SHA-256 hashes are stored (`sessions.json` in the data dir); sessions are revocable.
  5 wrong PINs lock an address for 10 minutes; 30 in total replace the PIN. Comparisons are constant-time.
  The LAN is plain HTTP — see SECURITY.md for what that means.
- `Host` must be one of ours (DNS rebinding), every POST needs `X-Tvpult: 1` (a cross-site page can't add it
  without a CORS preflight, which is never approved), a foreign `Origin` is refused.
- Only ordinary remote keys are allowed; service-menu keys (`KEY_FACTORY`, …) are not.

## Shortcut mode

`tvpult app` serves while at least one remote page is open. Pages say hello every 20 s and bye on close
(`sendBeacon`). Last page closed → stop after 8 s (room for a reload). A tab gone without bye counts as closed
after 3 min (browsers slow hidden-tab timers to ~1/min). Nobody opened the page in 2 min → stop.
`bye` can't carry the custom header, so it is accepted only for a random 128-bit client id that said hello.

The shortcut runs `uvw` (no console window). `WScript.Shell` saves shortcuts through the ANSI code page, which
may lack the characters of the shortcut's name, so it is saved under an ASCII name and renamed in PowerShell.

## Tests

`uv run pytest`. Every network call is forbidden in tests (`tests/conftest.py`): no ARP, no HTTP, no TV
connection — a test can never reach a real TV (and, say, pop up its "Allow?" prompt). The key protections
were checked by injecting bugs on purpose (MAC check, ARP check, public-network scan, rate limit, foreign
client id) — the tests catch each one.
