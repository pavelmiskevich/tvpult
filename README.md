# tvpult

[![CI](https://github.com/pavelmiskevich/tvpult/actions/workflows/ci.yml/badge.svg)](https://github.com/pavelmiskevich/tvpult/actions/workflows/ci.yml)

**A local remote for Samsung Smart TVs.** Finds your TVs on the home network and controls them from a
browser window — no cloud, no account, no app store. Double-click a desktop shortcut to open it; close the
window and it stops.

*pult* is Russian for "remote control". [Русская версия](README.ru.md)

> **Status: pre-alpha.** It works daily on two Samsung TVs (2017 Tizen 3.0 and 2022 Tizen 6.5).
> A setup wizard, PyPI packaging and a Windows installer are on the way — see [Roadmap](#roadmap).

## What it does

- **Remote keys with no lag** (~10 ms from click to TV), D-pad, volume, channels, playback.
- **Quick launch** of apps — YouTube, Netflix, the browser, and more; regional apps included.
- **What's on screen** and which Wi-Fi access point the TV uses.
- **Type text** into the field open on the TV (search, passwords) from your phone or laptop.
- **Power on** over the network (Wake-on-LAN, where the TV supports it) and off.
- **Soundbar volume** for Google Cast speakers and soundbars.
- **Finds devices by themselves**: if the router hands out new addresses or you move to another network,
  devices are found again by their MAC (TV) or Cast UUID — and checked against the laptop's ARP table, so
  another device on the network can't pose as your TV.
- **Desktop shortcut**: starts everything without a console window, opens the remote as its own window,
  stops ~10 s after you close it.

## Quick start (from source, for now)

Needs [uv](https://docs.astral.sh/uv/) — it installs Python 3.12 and the dependencies by itself.

```powershell
git clone https://github.com/pavelmiskevich/tvpult
cd tvpult
uv run tvpult init        # creates devices.toml in your user folder and prints where it is
                          # put your TVs' MAC addresses there (Settings > About on the TV)
uv run tvpult find --all  # lists every Samsung TV on your network (model, year, MAC)
uv run tvpult pair living # press "Allow" on the TV
uv run tvpult shortcut    # creates the desktop shortcut
```

Your files live in your user folder, not next to the code: `tvpult paths` shows where
(Windows: `%LOCALAPPDATA%\tvpult`; Linux: `~/.config/tvpult` and `~/.local/share/tvpult`).
`--config` / `--data-dir` or `TVPULT_CONFIG` / `TVPULT_DATA_DIR` override that.

Then double-click the shortcut. Or run `uv run tvpult serve` and open http://127.0.0.1:8765/.

Command line too: `tvpult status`, `tvpult key living home right ok`, `tvpult launch living youtube`,
`tvpult vol +5`, `tvpult text living "search words"` — `tvpult --help` for all.

## Supported

| Brand | Models | Status |
|---|---|---|
| Samsung | Tizen, 2016 and newer | tested on a 2017 (Tizen 3.0) and a 2022 (Tizen 6.5) TV |
| Google Cast | soundbars, speakers | volume and mute |

Other brands (LG webOS, Android/Google TV, Roku) are planned as separate drivers.
Please report your model in an issue — whether it works or not.

## Safety

- Talks only to devices on your local network. Nothing goes to any cloud.
- The page listens on `127.0.0.1` only (phone access over the home network with a PIN is planned;
  remote access via your own Tailscale tailnet is supported). **Never forward its port on your router.**
- Only ordinary remote keys can be sent — service-menu keys are blocked, so nothing irreversible can be done.
- Before any command the device's identity is checked (MAC reported by the TV must match the ARP table).
- Requests from other websites in your browser are rejected (Host check, custom header, Origin check).
- Undo everything: remove `tvpult` from the TV's connected-devices list and delete the folder.

Details: [docs/design.md](docs/design.md).

## Roadmap

1. Setup wizard: find all Samsung TVs, pair, name them — no config file to edit.
2. Settings page; app catalog with regions; English and Russian UI.
3. Phone access over home Wi-Fi with a PIN.
4. PyPI package (`uv tool install tvpult`), CI, first release.
5. Windows installer with a desktop shortcut and a tray icon.
6. Drivers for other brands.

## Credits

Built on [samsungtvws](https://github.com/xchwarze/samsung-tv-ws-api) and
[pychromecast](https://github.com/home-assistant-libs/pychromecast).

Not affiliated with Samsung, Google or any streaming service. Product names belong to their owners.

## License

[MIT](LICENSE)
