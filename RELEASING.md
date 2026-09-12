# Releasing HomeStock

Two products come out of this repo and they release on different rails:

| | What it is | How it ships |
|---|---|---|
| **HomeStock.app** | The Mac app | A DMG on GitHub Releases |
| **homestock-mcp** | The MCP server | PyPI, on a `v*` tag (`release.yml`) |

Neither needs the other. Installing the app does not install the MCP server, and
an agent using the MCP server does not need the app.

## Cutting an app release

```bash
./scripts/release.sh 0.2.0 2        # <version> <build-number>
```

The build number **must increase every release** — it is what an updater
compares to decide whether an installed copy is out of date. Keep it simple: 1,
2, 3.

That builds `HomeStock.app` at the requested version with the Python runtime
inside it, ad-hoc signs it, and packages `dist/HomeStock-<version>.dmg`
(around 50MB). Then:

```bash
gh release create v0.2.0 dist/HomeStock-0.2.0.dmg \
  --title "HomeStock 0.2.0" --notes "What changed…"
```

The tag must be `v<version>` and the asset filename must match what the script
printed, because that is the URL an appcast will eventually point at.

## Why the app is 50MB

It carries its own Python 3.12. macOS still ships 3.9, the engine needs 3.11 or
newer, and `pydantic_core` is a compiled extension whose wheel is built per
interpreter version — so "use whatever python3 the user has" cannot work for
something people download. Bringing the runtime is what makes the app need
nothing installed.

## First launch, for every user, once

HomeStock is ad-hoc signed, not notarised by Apple — that needs a paid Developer
account. Gatekeeper therefore blocks the **first** open. This is ordinary for
indie Mac apps, and the README says so plainly rather than letting people
discover it:

1. Drag **HomeStock** into Applications from the DMG.
2. **Right-click** HomeStock, then **Open**, then **Open** again in the dialog.
   A plain double-click will not offer the Open button — it has to be
   right-click then Open.
3. Still refused? **System Settings → Privacy & Security**, scroll to the
   bottom, **Open Anyway**.

Once done, it opens normally from then on.

## Auto-update is not wired up yet

Sparkle is the intended answer — it verifies each update against our own EdDSA
key, so no Apple Developer account is needed, and the appcast and DMGs sit on
GitHub for free. It is deliberately not in the build yet: pulling Sparkle in
through SwiftPM compiles the whole framework from source, which adds more than
ten minutes to *every* build for everyone, and the framework then has to be
embedded by hand into a bundle this repo assembles itself.

`scripts/release.sh` already looks for the Sparkle tools in `.sparkle-tools/`
and writes `appcast.xml` when it finds them, so the release side is ready for
it. See TODOS.md for what is left.

## Cutting an MCP server release

Tag `v<version>`. `.github/workflows/release.yml` builds the wheel and publishes
it to PyPI through trusted publishing — no tokens stored. That workflow has
never actually run; the first tag will be its first test.
