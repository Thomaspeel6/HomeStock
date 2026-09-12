# Security

HomeStock holds a record of everything a household buys. That data reveals
health conditions, religion, household composition and addiction — so a bug
here is not a cosmetic problem. Please report it.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: go to the **Security** tab of
this repository and choose **Report a vulnerability**. That opens a private
thread with the maintainer — please don't open a public issue for anything
exploitable.

Include what you did, what happened, and what you expected. A proof of concept
helps. There is no bounty; this is a spare-time project, and you will be
credited in the changelog unless you'd rather not be.

## What HomeStock is designed to guarantee

- **No outbound network calls.** The server and the pantry window make none.
  No telemetry, no accounts, no analytics, no webfonts, no CDN assets. If you
  see HomeStock make a request to anything, that is a bug worth reporting.
- **Your data is one file.** `homestock.db` on your disk, plus `captures/`
  beside it for photographed receipts. Deleting them deletes everything.
- **Diagnostics leak nothing.** `get_health()` returns counts and dates only,
  never item names or email content, so its output is safe to paste into an
  issue.
- **Email access is read-only and sender-filtered**, to retailers you approve.

## Known limitations, stated plainly

These are accepted trade-offs, not undiscovered bugs. Reporting them is still
welcome if you think a trade-off is wrong.

- **LAN mode is plain HTTP.** `homestock-ui --lan` serves your phone over your
  own wifi without TLS, so anyone already on that network who can capture
  traffic can read a capture in flight, and the pairing cookie cannot be
  `Secure`. It is off by default for exactly this reason. Don't use it on
  shared, workplace or public wifi. TLS is tracked in
  [TODOS.md](TODOS.md).
- **LAN mode has no user accounts.** Any device that enters the six-digit
  pairing code within its ten-minute window can read your kitchen and add to
  it. Codes burn after five wrong guesses, but a person standing next to your
  laptop can read the code off the screen.
- **Loopback mode has no authentication at all**, by design: the filesystem is
  the permission model. Anything that can run as your user can already read
  `homestock.db` directly. Writes still require a per-launch token so that a
  web page you happen to visit cannot POST to `localhost`, and every request
  must arrive under a `Host` we recognise — without that check, a site whose
  DNS name resolves to `127.0.0.1` would be same-origin with the window and
  could both read your kitchen and lift the write token out of the page.
- **Your AI client is outside this boundary.** When an agent reads your stock
  to answer a question, that conversation goes wherever your AI client sends
  it, under its own privacy policy. HomeStock cannot change that, and does not
  pretend to.

## Supported versions

Alpha. Fixes land on `main`; there is no backport branch yet.
