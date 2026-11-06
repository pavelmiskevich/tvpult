# Security

tvpult controls TVs on your home network. This page says what it protects against, what it doesn't,
and how to report a problem.

## Reporting a vulnerability

Please **don't open a public issue**. Use GitHub's private reporting:
*Security → Report a vulnerability* on https://github.com/pavelmiskevich/tvpult. You'll get an answer within a week.

## What tvpult protects against

| Threat | Protection |
|---|---|
| A website you visit drives your TVs through your browser | The server answers only its own host names (blocks DNS rebinding); every action needs a custom header that cross-site pages can't send; a foreign `Origin` is refused |
| Someone on the internet reaches the remote | It never listens on public addresses. By default only on this computer (`127.0.0.1`); `--tailnet` adds your own Tailscale network; `--lan` adds the home network with a PIN. **Never forward its port on your router** |
| Someone on your Wi-Fi uses the remote (`--lan`) | Every API call needs a session. A phone gets one by entering a 6-digit PIN shown **only on the computer itself**. 5 wrong PINs lock that address for 10 minutes; 30 wrong PINs in total replace the PIN. Sessions are HttpOnly, SameSite=Strict cookies, last 30 days and can be revoked from the computer; only their SHA-256 hashes are stored |
| A device on the LAN poses as your TV to get the pairing token or what you type | A TV is used only if the MAC it reports matches the computer's ARP table for that address — during discovery and before every command |
| The setup wizard is tricked into connecting somewhere | Pairing accepts only a candidate id from the server's own last scan, never an address from the page, and only ARP-confirmed TVs |
| Irreversible changes on the TV | Only ordinary remote keys can be sent; service-menu keys (`KEY_FACTORY`, …) are blocked |

## What it doesn't protect against

- **Traffic on your home network is plain HTTP.** With `--lan`, someone who can sniff your Wi-Fi traffic could
  copy a session cookie. Home Wi-Fi with WPA2/WPA3 encrypts traffic against outsiders; if you don't trust other
  devices on your network, don't use `--lan` — use Tailscale (`--tailnet`), which is encrypted end to end.
- **ARP spoofing** on your home network defeats the identity check — but an attacker able to do that can
  intercept much more than tvpult.
- **The TV's own TLS certificate is not pinned** yet (issue #14).
- Anyone with access to your user account on the computer can read the pairing tokens and use the remote.
- A signed-in phone (`--lan`) or a Tailscale device (`--tailnet`) has the same rights as the computer, except for
  the PIN and the list of signed-in phones: it can also change the settings (rename, remove, re-pair devices).
  Give the PIN only to people you'd hand the TV remote to.

## Undo everything

In the remote, **⚙ → Remove** each device. On each TV remove `tvpult` from its device list
(Settings → General (on 2022+ sets: Connection) → External Device Manager → Device Connection Manager → Device List; names vary a little by year). Delete the data folder (`tvpult paths` shows it), uninstall tvpult.
