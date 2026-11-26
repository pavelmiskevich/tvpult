# tvpult

[![CI](https://github.com/pavelmiskevich/tvpult/actions/workflows/ci.yml/badge.svg)](https://github.com/pavelmiskevich/tvpult/actions/workflows/ci.yml)

**A local remote for Samsung Smart TVs.** Finds your TVs on the home network and controls them from a
browser window — no cloud, no account, no app store. Double-click a desktop shortcut to open it; close the
window and it stops.

*pult* is Russian for "remote control". [Русская версия](README.ru.md)

> **Status: pre-alpha.** It works daily on two Samsung TVs (2017 Tizen 3.0 and 2022 Tizen 6.5).
> The first release on PyPI (0.1.0) and a Windows installer are on the way — see [Roadmap](#roadmap).

## What it does

- **Remote keys with no lag** (~10 ms from click to TV), D-pad, volume, channels, playback.
- **Quick launch** of apps — YouTube, Netflix, the browser, and more; regional apps included. Missing one? Adding it is
  a one-entry change to the catalog — see [CONTRIBUTING.md](CONTRIBUTING.md).
- **What's on screen** and which Wi-Fi access point the TV uses.
- **Type text** into the field open on the TV (search, passwords) from your phone or laptop.
- **Power on** over the network (Wake-on-LAN, where the TV supports it) and off.
- **Soundbar volume** for Google Cast speakers and soundbars.
- **Finds devices by themselves**: if the router hands out new addresses or you move to another network,
  devices are found again by their MAC (TV) or Cast UUID — and checked against the laptop's ARP table, so
  another device on the network can't pose as your TV.
- **Desktop shortcut** (Windows, Linux, macOS): starts everything without a console window, opens the remote
  as its own window (Edge, Chrome or Chromium; otherwise a browser tab), stops ~10 s after you close it.

## Quick start

Needs [uv](https://docs.astral.sh/uv/) — it installs Python 3.12 and the dependencies by itself.

```powershell
uv tool install tvpult    # from PyPI, starting with the first release (0.1.0)
tvpult shortcut           # creates the shortcut "TV Remote" («Пульт ТВ» on a Russian system)
```

The shortcut goes on the desktop on Windows; on Linux into the applications menu and onto the desktop; on macOS
it is an app in `~/Applications` (Launchpad, Spotlight). `uv tool upgrade tvpult` updates it later — the shortcut
keeps working; `uv tool uninstall tvpult` removes it (the shortcut and your settings stay until you delete them).

Before 0.1.0 is out, or to run the newest code, from source:

```powershell
git clone https://github.com/pavelmiskevich/tvpult
cd tvpult
uv run tvpult shortcut
```

Switch the TVs on and double-click the shortcut. The first time, a **setup screen** finds every Samsung TV
on your network: give each a name, click **Connect**, press **Allow** on the TV — done, that's your remote.
More TVs later: the **+ TV** tab. Everyday changes are in **⚙ settings**: rename or remove devices, connect a TV
again or forget it, Wake-on-LAN on/off, "search again", and which apps sit in the quick-launch row and in what
order. No files to edit. (Without the shortcut: `tvpult serve` and open http://127.0.0.1:8765/.)

Your files live in your user folder, not next to the code: `tvpult paths` shows where
(Windows: `%LOCALAPPDATA%\tvpult`; Linux: `~/.config/tvpult` and `~/.local/share/tvpult`).
`--config` / `--data-dir` or `TVPULT_CONFIG` / `TVPULT_DATA_DIR` override that.
The command line can do the same: `tvpult find --all` lists every TV, `tvpult init` starts a config by hand.

**English and Russian.** The page follows the browser's language; **⚙ → Language** switches it (remembered
per browser). The command line and the shortcut's name follow the system language; `TVPULT_LANG=en` or `ru`
overrides it.

Command line too: `tvpult status`, `tvpult key living home right ok`, `tvpult launch living youtube`,
`tvpult vol +5`, `tvpult text living "search words"` — `tvpult --help` for all.

## Supported

| Brand | Models | Status |
|---|---|---|
| Samsung | Tizen, 2016 and newer | tested on a 2017 (Tizen 3.0) and a 2022 (Tizen 6.5) TV |
| Google Cast | soundbars, speakers | volume and mute |

Other brands (LG webOS, Android/Google TV, Roku) are planned as separate drivers.
Please report your model in an issue — whether it works or not.

## On your phone

Start the remote with `--lan` (`tvpult serve --lan`, or a shortcut made with `tvpult shortcut --lan`).
On the computer, open **📱 Remote on the phone**: point the phone's camera at the QR code (or type the address)
and enter the 6-digit PIN shown there — once; the phone stays signed in for 30 days. Phones that signed in are
listed there and can be signed out with one click. Away from home: `--tailnet` with your own Tailscale network.

## Safety

- Talks only to devices on your local network. Nothing goes to any cloud.
- The page listens on `127.0.0.1` only by default; `--lan` opens it to the home network **behind a PIN**,
  `--tailnet` to your own Tailscale network. **Never forward its port on your router.**
- Only ordinary remote keys can be sent — service-menu keys are blocked, so nothing irreversible can be done.
- Before any command the device's identity is checked (MAC reported by the TV must match the ARP table).
- Requests from other websites in your browser are rejected (Host check, custom header, Origin check).
- Undo everything: **⚙ → Remove** each device (or delete the folder), and remove `tvpult` from the TV's own
  device list (Settings → General (on 2022+ sets: Connection) → External Device Manager → Device Connection Manager → Device List).

Details: [SECURITY.md](SECURITY.md) (what is and isn't protected, how to report a vulnerability) and
[docs/design.md](docs/design.md).

## Roadmap

Done: setup wizard, settings page, English and Russian UI and error messages, app catalog, finding every Samsung
TV, phone access with a PIN, the shortcut on Windows, Linux and macOS, CI, the release workflow. Next:

1. First release on PyPI (`uv tool install tvpult`), 0.1.0.
2. Windows installer with a desktop shortcut and a tray icon.
3. Finding Cast soundbars by themselves; drivers for other brands.

## Credits

Built on [samsungtvws](https://github.com/xchwarze/samsung-tv-ws-api) and
[pychromecast](https://github.com/home-assistant-libs/pychromecast).

Not affiliated with Samsung, Google or any streaming service. Product names belong to their owners.

## License

[MIT](LICENSE)
