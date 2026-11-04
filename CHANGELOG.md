# Changelog

All notable changes are listed here. Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
