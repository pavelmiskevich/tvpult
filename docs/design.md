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

- only private networks, /24, at most 4: the laptop's own (default route), those of the last known
  addresses, and explicitly configured ones. Never public, loopback, link-local or CGNAT (Tailscale) ranges;
- ports 8001 and 8009, 0.5 s timeout, ~2 s for a /24; the last known address is retried with 1.5 s in parallel
  (a Wi-Fi device idle for a while answers its first connection slowly);
- at most once a minute (shared between processes through the address book).

SSDP/mDNS discovery was tried first; multicast replies didn't reach the test machine (firewall), while a
/24 port scan works everywhere.

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
