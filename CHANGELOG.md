# Changelog

All notable changes are listed here. Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
