"""HomeStock's window — the pantry view, and the capture page for phones.

The MCP server answers an agent; this answers a person. Same SQLite file, its
own process (WAL already makes concurrent writers queue — see
test_two_process_concurrent_writes), so the app can show a live kitchen while
an agent ingests receipts underneath it.

    uv run homestock-ui              laptop only, on 127.0.0.1
    uv run homestock-ui --lan        also reachable from your phone

Two surfaces, two threat models:

  Loopback (default) is reachable only from this machine, so the filesystem is
  the permission model, exactly as the rest of HomeStock assumes. Writes still
  carry a per-launch token, because a page on the open web can otherwise POST
  to localhost.

  LAN mode is opt-in and never the default, because binding 0.0.0.0 on a cafe
  network would hand a stranger your shopping history. Every request — reads
  included — needs a device that has paired with a 6-digit code shown on the
  laptop. Codes expire, and wrong guesses burn the code rather than the clock.
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from homestock import chat, server

# Two secrets, because they answer two different questions and one of them
# travels somewhere the other must not. TOKEN is embedded in the pages we serve
# and proves *which page* is asking; DEVICE_SECRET lives in a cookie and proves
# *which device*. Cookies ride along with requests the page did not author, so
# a cookie may establish identity but must never confer write capability — and
# that separation is only real if the two values differ. When they were the
# same string, anything that leaked the "read-only" cookie leaked full write
# access, and reading a page handed you the write secret.
TOKEN = secrets.token_urlsafe(24)
DEVICE_SECRET = secrets.token_urlsafe(24)
LAN_MODE = False
PAIR_TTL = 600           # a pairing code is good for ten minutes
PAIR_MAX_ATTEMPTS = 5    # then it is burned, not merely delayed
PAIRING_DAYS = 30        # how long a paired phone stays paired
MAX_UPLOAD = 12 * 1024 * 1024
MAX_PAIR_BODY = 256      # a pairing code is six digits; nothing else belongs here
LOOPBACK = ("127.0.0.1", "::1")

_pair = {"code": None, "expires": 0.0, "attempts": 0}
_lan_ip_cache: list[str | None] = [None]
# ThreadingHTTPServer runs every request on its own thread, so the guess
# counter is shared mutable state. Without this lock `attempts += 1` is a
# read-modify-write that concurrent guesses lose, and the five-attempt burn —
# the only brute-force control on a six-digit code — stops being a limit.
_pair_lock = threading.Lock()


def _secret_eq(given: str, expected: str) -> bool:
    """Constant-time compare that survives hostile input.

    secrets.compare_digest raises TypeError on non-ASCII str, and every caller
    here is fed raw header or body text, so one high byte on the wire would
    otherwise crash the handler pre-auth on every route."""
    if not isinstance(given, str) or not given.isascii():
        return False
    return secrets.compare_digest(given, expected)


def _fn(tool):
    """FastMCP wraps each tool; the plain function is under .fn."""
    return tool.fn if hasattr(tool, "fn") else tool


get_stock = _fn(server.get_stock)
what_should_i_order = _fn(server.what_should_i_order)
get_expiring_soon = _fn(server.get_expiring_soon)
correct_stock = _fn(server.correct_stock)
discard_item = _fn(server.discard_item)
get_health = _fn(server.get_health)
add_capture = _fn(server.add_capture)
list_captures = _fn(server.list_captures)


def new_pair_code() -> str:
    with _pair_lock:
        _pair.update(code=f"{secrets.randbelow(1000000):06d}",
                     expires=time.time() + PAIR_TTL, attempts=0)
        return _pair["code"]


def check_pair_code(given: str) -> bool:
    """A wrong guess costs an attempt; PAIR_MAX_ATTEMPTS wrong guesses burn it.

    Only *failures* count. A household has more than one device, and a code
    that died on its fifth correct use would lock out the phone it was printed
    for. The whole check is under the lock so that concurrent guesses cannot
    lose increments and buy themselves extra tries.
    """
    with _pair_lock:
        code = _pair["code"]
        if not code or time.time() > _pair["expires"]:
            return False
        if _secret_eq(given.strip().replace("-", "").replace(" ", ""), code):
            return True
        _pair["attempts"] += 1
        if _pair["attempts"] >= PAIR_MAX_ATTEMPTS:
            _pair["code"] = None
        return False


def lan_ip() -> str:
    """This machine's address on the local network. No packets are sent.

    Cached: _host_ok() consults it on every request."""
    if _lan_ip_cache[0] is None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 1))  # TEST-NET-1: routable nowhere
            _lan_ip_cache[0] = s.getsockname()[0]
        except OSError:
            _lan_ip_cache[0] = "127.0.0.1"
        finally:
            s.close()
    return _lan_ip_cache[0]


def build_state() -> dict:
    """Everything the page renders, in one round trip."""
    health = get_health()
    order = what_should_i_order()
    # An item cannot both need buying and need eating today. The shopping list
    # wins: it is the one the user acts on before leaving the house.
    on_list = {o["name"] for o in order}
    expiring = [e for e in get_expiring_soon(within_days=3) if e["name"] not in on_list]
    names = {i["name"] for i in get_stock()}
    flagged = on_list | {e["name"] for e in expiring}

    shelf = []
    for name in sorted(names - flagged):
        s = get_stock(name)
        if isinstance(s, dict) and "error" not in s:
            shelf.append(s)
    shelf.sort(key=lambda s: (s["estimated_state"] != "likely_in_stock", s["name"]))
    return {"health": health, "order": order, "expiring": expiring, "shelf": shelf,
            "pending_captures": len(list_captures("pending"))}


def save_photo(data_url: str) -> tuple[str | None, str | None]:
    """Decode a browser data: URL to a file beside the database.

    Photos are files, not blobs: the database stays small enough to copy as a
    backup, and an agent can read the path directly."""
    try:
        header, _, b64 = data_url.partition(",")
        if not b64 or not header.startswith("data:image/"):
            return None, None
        mime = header[5:].split(";")[0]
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
               "image/heic": "heic"}.get(mime)
        if not ext:
            return None, None
        raw = base64.b64decode(b64, validate=True)
    except (ValueError, binascii.Error):
        return None, None
    if not raw or len(raw) > MAX_UPLOAD:
        return None, None
    d = server._captures_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}.{ext}"
    path.write_bytes(raw)
    return str(path), mime

# A local-first app cannot pull a webfont: a request to fonts.googleapis.com
# would leak that this household is running HomeStock, from a product whose
# whole claim is that nothing leaves the machine. So the type personality has
# to come from treatment rather than a downloaded face — which is no loss,
# because this subject already has a vernacular. A pantry is a ledger: every
# figure is monospaced and tabular, sections are ruled rather than boxed, and
# quantities line up in a column the way they do on a receipt.
#
# Colour is spent only on what needs attention. Something that is simply fine
# gets no marker at all — no dot, no tint — so the eye lands on the two rows
# that actually want acting on rather than sweeping twenty identical chips.
# A jar with a fill line, not a shelf of tiny objects: at 26px anything with
# six strokes turns to mush, and "how much is left" is the whole product.
MARK = """<svg class="mark" viewBox="0 0 24 24" aria-hidden="true" fill="none"
 stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
 <path d="M8.5 2.2h7"/>
 <path d="M7 5.8h10a3 3 0 0 1 3 3v9a3.4 3.4 0 0 1-3.4 3.4H7.4A3.4 3.4 0 0 1 4 17.8v-9a3 3 0 0 1 3-3Z"/>
 <path d="M4.2 13.4h15.6"/></svg>"""

# Shared by all three pages. esc() is in here rather than in PAGE alone because
# the capture page interpolates user text too, and a page that forgot it would
# fail silently rather than loudly.
HELPERS = """
const esc = s => String(s).replace(/[&<>"']/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const api = (path, body) => fetch(path, {
  method: "POST",
  headers: {"content-type": "application/json", "x-homestock-token": window.HS_TOKEN || ""},
  body: JSON.stringify(body),
});
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
"""

# No webfont, no remote asset of any kind: a request to a font CDN would leak
# that this household runs HomeStock, from a product whose headline claim is
# that nothing leaves the machine. The character comes from the treatment.
#
# A pantry is still a ledger — figures are monospaced and tabular, and colour is
# spent only where something wants acting on. What changed is that the ledger
# now sits on real surfaces instead of on bare paper: each list is a card with a
# hairline edge, the masthead carries a mark, and the gauge showing where an
# item sits in its repurchase cycle is legible at a glance rather than a 3px
# hairline. Quiet, but built rather than defaulted.
CSS = """
:root {
  --bg:#f4f6f3; --surface:#fff; --sunk:#eef1ec;
  --ink:#12100e; --muted:#5d6862; --faint:#8b958e;
  --line:#e2e7de; --hair:#edf0ea; --track:#dee4d8;
  --accent:#2c6a50; --accent-ink:#fff; --accent-wash:#eaf3ee;
  --alert:#a4341c; --alert-wash:#fbeeea; --warn:#8a5a06; --warn-wash:#fbf2e2;
  --shadow:0 1px 2px rgba(18,30,22,.05), 0 8px 24px -12px rgba(18,30,22,.14);
  --radius:14px;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#0d100e; --surface:#171b18; --sunk:#121614;
  --ink:#e9ece7; --muted:#98a29a; --faint:#78827b;
  --line:#252b27; --hair:#1e231f; --track:#2b322c;
  --accent:#6cbf96; --accent-ink:#0d100e; --accent-wash:#17251d;
  --alert:#ef9377; --alert-wash:#2a1a15; --warn:#dcae57; --warn-wash:#261f10;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 10px 28px -14px rgba(0,0,0,.7);
} }
:root[data-theme="dark"] {
  --bg:#0d100e; --surface:#171b18; --sunk:#121614;
  --ink:#e9ece7; --muted:#98a29a; --faint:#78827b;
  --line:#252b27; --hair:#1e231f; --track:#2b322c;
  --accent:#6cbf96; --accent-ink:#0d100e; --accent-wash:#17251d;
  --alert:#ef9377; --alert-wash:#2a1a15; --warn:#dcae57; --warn-wash:#261f10;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 10px 28px -14px rgba(0,0,0,.7);
}
* { box-sizing:border-box; }
body {
  background:var(--bg); color:var(--ink); font:16px/1.55 var(--sans);
  margin:0 auto; max-width:780px; padding-inline:20px; padding-block:28px 64px;
  font-variant-numeric:tabular-nums; -webkit-font-smoothing:antialiased;
}
.vh { position:absolute; width:1px; height:1px; overflow:hidden;
      clip-path:inset(50%); white-space:nowrap; }

/* Masthead ---------------------------------------------------------------- */
.top { display:flex; align-items:center; gap:10px; margin-bottom:30px; }
.mark { width:26px; height:26px; color:var(--accent); flex:none; }
.brand { font-size:.95rem; font-weight:600; letter-spacing:-.01em; margin:0; }
.top .spacer { flex:1; }
.chip {
  font:.72rem/1 var(--mono); color:var(--muted); background:var(--surface);
  border:1px solid var(--line); border-radius:999px; padding:6px 11px;
  letter-spacing:.01em; white-space:nowrap;
}
h1 { font-size:2rem; font-weight:640; letter-spacing:-.03em; margin:0 0 5px; line-height:1.15; }
.meta { font:.8rem/1.5 var(--mono); color:var(--muted); margin:0 0 26px; letter-spacing:.01em; }

/* Cards ------------------------------------------------------------------- */
.card {
  background:var(--surface); border:1px solid var(--line); border-radius:var(--radius);
  box-shadow:var(--shadow); margin-bottom:18px; overflow:hidden;
}
.card > h2 {
  display:flex; align-items:center; gap:10px; margin:0;
  padding:14px 18px 12px; border-bottom:1px solid var(--hair);
  font:600 .74rem/1 var(--mono); letter-spacing:.13em; text-transform:uppercase;
  color:var(--muted);
}
.card > h2 .n {
  margin-left:auto; font-size:.72rem; letter-spacing:.04em; color:var(--faint);
  background:var(--sunk); border-radius:999px; padding:4px 9px; text-transform:none;
}
.card.attn { border-color:color-mix(in srgb, var(--alert) 34%, var(--line)); }
.card.attn > h2 { color:var(--alert); background:var(--alert-wash); border-bottom-color:transparent; }
.card.soon > h2 { color:var(--warn); background:var(--warn-wash); border-bottom-color:transparent; }

/* Rows -------------------------------------------------------------------- */
.item {
  display:grid; padding:13px 18px; border-top:1px solid var(--hair);
  grid-template-columns:minmax(0,1fr) auto;
  grid-template-areas:"nm fig" "sub fig" "gauge gauge" "acts acts";
  column-gap:16px; align-items:center;
}
.item:first-child { border-top:0; }
@media (min-width:600px) {
  .item {
    grid-template-columns:minmax(0,1fr) 96px auto auto;
    grid-template-areas:"nm gauge fig acts" "sub gauge fig acts";
    column-gap:18px;
  }
}
.nm { grid-area:nm; font-size:1rem; font-weight:560; min-width:0;
      overflow-wrap:anywhere; align-self:end; letter-spacing:-.006em; }
.sub { grid-area:sub; font:.76rem/1.45 var(--mono); color:var(--muted);
       letter-spacing:.01em; align-self:start; }

/* Where this item sits in its own repurchase cycle. The track runs to one and
   a half cycles, so the notch at two thirds is "due" and overshoot is visible
   past it rather than merely asserted. --p and the notch share one variable,
   so the mark cannot drift away from the scale it labels. */
.gauge {
  grid-area:gauge; --cycles:1.5; width:96px; max-width:100%; height:6px;
  background:var(--track); border-radius:999px; position:relative;
  margin:10px 0 3px; overflow:hidden;
}
@media (min-width:600px) { .gauge { margin:0; } }
.gauge b {
  position:absolute; inset:0 auto 0 0; border-radius:999px; background:var(--muted);
  width:calc(min(var(--p), var(--cycles)) / var(--cycles) * 100%);
  transition:width .25s ease;
}
.gauge.over b { background:var(--alert); }
.gauge::after {
  content:""; position:absolute; top:0; bottom:0; width:2px; border-radius:1px;
  left:calc(100% / var(--cycles)); background:var(--surface); opacity:.9;
}
.fig {
  grid-area:fig; font:600 .78rem/1 var(--mono); color:var(--muted);
  letter-spacing:.02em; white-space:nowrap; text-align:right; min-width:3.8em;
}
.fig.over { color:var(--alert); }
.fig.soon { color:var(--warn); }

.acts { grid-area:acts; display:flex; gap:7px; margin-top:9px; }
@media (min-width:600px) { .acts { margin:0; justify-content:flex-end; } }
.acts button {
  font:500 .8rem/1 var(--sans); color:var(--muted); background:var(--surface);
  border:1px solid var(--line); border-radius:999px; padding:7px 12px; cursor:pointer;
  white-space:nowrap; transition:color .12s, border-color .12s, background .12s;
}
.acts button:hover { color:var(--accent); border-color:var(--accent); background:var(--accent-wash); }
.acts button:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.acts button:disabled { opacity:.45; cursor:default; }
.unsure .nm { color:var(--muted); }

/* "In the house" is reference, not a to-do list: dense, quiet, two columns
   when there is room, controls out of the way until reached for. */
.house { display:grid; grid-template-columns:minmax(0,1fr); }
@media (min-width:620px) {
  .house { grid-template-columns:repeat(2,minmax(0,1fr)); }
  .house .item:nth-child(2) { border-top:0; }
  .house .item:nth-child(odd) { border-right:1px solid var(--hair); }
}
.house .item { padding:9px 18px 10px; grid-template-columns:minmax(0,1fr) auto;
               grid-template-areas:"nm fig" "acts acts"; }
.house .nm { font-size:.95rem; font-weight:500; align-self:center; }
.house .sub { display:none; }
.house .acts { margin-top:4px; justify-content:flex-start; }
.house .acts button { padding:5px 10px; font-size:.76rem; }
@media (hover:hover) and (min-width:620px) {
  .house .acts { opacity:0; transition:opacity .13s; }
  .house .item:hover .acts, .house .item:focus-within .acts { opacity:1; }
}
.empty { color:var(--muted); font-size:.93rem; padding:20px 18px; margin:0; }

/* Louder than any estimate on the page, because a silent ingestion failure
   looks exactly like a quiet week. */
.stale {
  display:flex; gap:12px; align-items:flex-start; background:var(--alert-wash);
  border:1px solid color-mix(in srgb, var(--alert) 30%, transparent);
  border-radius:var(--radius); padding:14px 16px; margin:0 0 18px;
}
.stale svg { width:18px; height:18px; color:var(--alert); flex:none; margin-top:1px; }
.stale strong { display:block; font-size:.95rem; }
.stale span { font:.79rem/1.5 var(--mono); color:var(--muted); }

/* Add + pairing ----------------------------------------------------------- */
.add { padding:16px 18px 18px; }
.add input {
  font:1rem/1.5 var(--sans); width:100%; padding:11px 14px; color:var(--ink);
  background:var(--sunk); border:1px solid transparent; border-radius:10px;
}
.add input:focus { outline:0; border-color:var(--accent); background:var(--surface); }
.hint { font:.78rem/1.5 var(--mono); color:var(--muted); margin:9px 0 0; letter-spacing:.01em; }
.lanhint { font:.83rem/1.5 var(--sans); color:var(--faint); letter-spacing:0;
           margin:0; padding:0 18px 18px; }
.lanhint code { font-size:.78rem; background:var(--sunk); border-radius:5px; padding:2px 6px; }
.pair {
  margin:14px 18px 18px; background:var(--accent-wash); border-radius:12px;
  padding:14px 16px; border:1px solid color-mix(in srgb, var(--accent) 22%, transparent);
}
.pair strong { font-size:.92rem; }
.pair .code {
  font:640 2rem/1.2 var(--mono); letter-spacing:.16em; margin:8px 0 4px; color:var(--ink);
}
footer {
  margin-top:30px; font:.78rem/1.6 var(--mono); color:var(--faint); letter-spacing:.01em;
}
footer p { margin:0 0 6px; }
footer .prose { font-family:var(--sans); font-size:.83rem; letter-spacing:0; }
code { font-family:var(--mono); word-break:break-all; }

/* Capture / pair sheets --------------------------------------------------- */
.sheet { background:var(--surface); border:1px solid var(--line); border-radius:var(--radius);
         box-shadow:var(--shadow); padding:20px; }
.sheet > * + * { margin-top:20px; }
.field label { display:block; font:600 .72rem/1 var(--mono); letter-spacing:.13em;
               text-transform:uppercase; color:var(--muted); margin:0 0 9px; }
.field input {
  font:1rem/1.5 var(--sans); width:100%; padding:12px 14px; color:var(--ink);
  background:var(--sunk); border:1px solid transparent; border-radius:10px;
}
.field input:focus { outline:0; border-color:var(--accent); background:var(--surface); }
.primary {
  display:block; width:100%; padding:18px; border-radius:12px; cursor:pointer;
  font:600 1.05rem/1.2 var(--sans); color:var(--accent-ink); background:var(--accent); border:0;
}
.primary:hover { filter:brightness(1.07); }
.secondary {
  display:block; width:100%; padding:12px; border-radius:10px; cursor:pointer; margin-top:9px;
  font:500 .92rem/1.2 var(--sans); color:var(--ink); background:none;
  border:1px solid var(--line);
}
.secondary:hover { border-color:var(--accent); color:var(--accent); }
.primary:focus-visible, .secondary:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.flash { min-height:1.4em; }
@media (prefers-reduced-motion:reduce) { * { transition:none !important; } }
"""

_HEAD = """<style>__CSS__</style></head><body>
<header class="top">__MARK__<p class="brand">HomeStock</p><div class="spacer"></div>__CHIP__</header>
"""

PAGE = """<title>HomeStock</title>""" + _HEAD.replace(
    "__CHIP__", '<span class="chip" id="chip">reading&hellip;</span>') + """
<h1>Your kitchen</h1>
<p class="meta" id="meta">Working out what you have&hellip;</p>
<div id="app"></div>

<section class="card">
  <h2>Add something</h2>
  <div class="add">
    <label for="quick" class="vh">Type what you bought</label>
    <input type="text" id="quick" placeholder="2 milk, bread, 6 eggs" autocomplete="off">
    <p class="hint" id="pending">Goes to the inbox for your agent to read.</p>
  </div>
  <div id="lanbox"></div>
</section>

<footer>
  <p id="foot"></p>
  <p class="prose">A database and a captures folder on this computer. Copy them to back
     up, delete them to erase everything. Nothing is sent anywhere.</p>
</footer>

<script>
window.HS_TOKEN = "__TOKEN__";
const LAN = __LAN__;
__HELPERS__
const ACTIONS = {
  buy:   [["have", "Already have it"]],
  soon:  [["out", "Used it"], ["binned", "Binned it"]],
  house: [["have", "Still have it"], ["out", "Out of it"]],
};

/* Every number below is read from the server, never recomputed here. The
   window and an agent asking the same question have to give the same answer,
   and only the server's copy has tests. */
function gauge(it) {
  if (it.cycle_position === null || it.cycle_position === undefined) return "<span></span>";
  const p = it.cycle_position;
  return `<span class="gauge${p > 1 ? " over" : ""}" style="--p:${p}" role="img"
    aria-label="${Math.round(p * 100)}% through its usual cycle"><b></b></span>`;
}

function item(it, kind) {
  let sub, fig = "", cls = "";
  if (kind === "soon") {
    sub = `keeps about ${it.shelf_life_days}d${it.storage ? " in the " + it.storage : ""}`;
    fig = `<span class="fig soon">${it.days_left <= 0 ? "today" : "in " + it.days_left + "d"}</span>`;
  } else if (it.confirmed) {
    sub = it.corrected_quantity === 0 ? "you said you were out" : "you confirmed this";
    if (kind === "buy") fig = `<span class="fig over">now</span>`;
  } else if (it.days_over !== null && it.days_over !== undefined) {
    sub = `every ${Math.round(it.median_interval_days)}d, last ${it.days_since_observation}d ago`;
    fig = `<span class="fig${it.days_over > 0 ? " over" : ""}">${
      it.days_over > 0 ? "+" + it.days_over + "d" : "in " + -it.days_over + "d"}</span>`;
  } else {
    sub = `bought once, ${it.days_since_last_purchase}d ago`;
    cls = " unsure";
  }
  return `<div class="item${cls}">
    <span class="nm">${esc(it.name)}</span>
    <span class="sub">${esc(sub)}</span>
    ${kind === "buy" ? gauge(it) : ""}${fig}
    <span class="acts">${ACTIONS[kind].map(a =>
      `<button type="button" data-item="${esc(it.name)}" data-action="${a[0]}">${a[1]}</button>`).join("")}</span>
  </div>`;
}

function card(title, rows, kind, empty, tone) {
  const body = rows.length ? rows.map(r => item(r, kind)).join("")
                           : `<p class="empty">${empty}</p>`;
  const count = rows.length ? `<span class="n">${rows.length}</span>` : "";
  const cls = rows.length && tone ? ` ${tone}` : "";
  return `<section class="card${cls}"><h2>${title}${count}</h2>
    <div class="${kind === "house" ? "house" : ""}">${body}</div></section>`;
}

const WARN_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
  stroke-linecap="round"><path d="M12 8v5"/><path d="M12 17h.01"/>
  <path d="M10.3 3.9 2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/></svg>`;

async function render() {
  const s = await (await fetch("/api/state")).json();
  const h = s.health;
  let html = "";
  if (h.stale) {
    html += `<div class="stale">${WARN_ICON}<div><strong>Not up to date.</strong><span>` +
      (h.last_ingest_at
        ? `no receipts read for ${h.days_since_ingest} days &mdash; estimates below may be stale`
        : `no receipts read yet &mdash; nothing below is based on much`) + `</span></div></div>`;
  }
  html += card("Buy these", s.order, "buy", "Nothing looks due.", "attn");
  html += card("Use soon", s.expiring, "soon", "Nothing about to go off.", "soon");
  html += card("In the house", s.shelf, "house", "No history yet.");
  document.getElementById("app").innerHTML = html;

  document.getElementById("chip").textContent = plural(h.items, "item", "items");
  document.getElementById("meta").textContent =
    `${plural(h.receipt_lines, "receipt line", "receipt lines")}`
    + (h.last_ingest_at ? ` · last read ${h.last_ingest_at.slice(0, 16)}` : " · nothing read yet");
  document.getElementById("pending").textContent = s.pending_captures
    ? `${plural(s.pending_captures, "capture", "captures")} waiting to be read.`
    : "Goes to the inbox for your agent to read.";
  document.getElementById("foot").innerHTML = `Database: <code>${esc(h.db_path)}</code>`;

  document.querySelectorAll("button[data-item]").forEach(b => b.onclick = async () => {
    document.querySelectorAll("button[data-item]").forEach(x => x.disabled = true);
    await api("/api/action", {item: b.dataset.item, action: b.dataset.action});
    render();
  });
}

document.getElementById("quick").addEventListener("keydown", async e => {
  if (e.key !== "Enter" || !e.target.value.trim()) return;
  await api("/api/capture", {kind: "note", text: e.target.value, device: "laptop"});
  e.target.value = "";
  render();
});

async function showPairing() {
  const box = document.getElementById("lanbox");
  if (!LAN) {
    box.innerHTML = `<p class="lanhint">Want to photograph paper receipts?
      Restart with <code>homestock-ui --lan</code> to pair your phone.</p>`;
    return;
  }
  const p = await (await fetch("/api/pairing")).json();
  box.innerHTML = `<div class="pair"><strong>On your phone</strong>, on this wifi, open
    <code>${esc(p.url)}</code>
    <div class="code">${esc(p.code.slice(0, 3))} ${esc(p.code.slice(3))}</div>
    <span class="hint">expires in ${Math.round(p.expires_in / 60)} min · photos go
    straight to this computer</span></div>`;
}

render();
showPairing();
</script></body>
"""

PAIR_PAGE = """<title>Pair with HomeStock</title>""" + _HEAD.replace("__CHIP__", "") + """
<h1>Pair this phone</h1>
<p class="meta">Enter the code shown on your computer.</p>
<div class="sheet">
  <div class="field">
    <label for="code">Pairing code</label>
    <input type="tel" id="code" inputmode="numeric" autocomplete="off" placeholder="000 000">
  </div>
  <p class="hint flash" id="err"></p>
  <button type="button" class="primary" id="go">Pair this phone</button>
</div>
<script>
const go = document.getElementById("go");
const field = document.getElementById("code");

async function attempt(code) {
  go.disabled = true;
  const r = await fetch("/api/pair", {
    method: "POST", headers: {"content-type": "application/json"},
    body: JSON.stringify({code}),
  });
  if (r.ok) { location.href = "/capture"; return; }
  const b = await r.json().catch(() => ({}));
  document.getElementById("err").textContent = b.error || "That code did not work.";
  go.disabled = false;
}

go.onclick = () => attempt(field.value);
field.addEventListener("keydown", e => { if (e.key === "Enter") attempt(field.value); });

// Arrived by scanning the QR code on the laptop: the code is already in the
// URL, so pair without making anyone read six digits off a screen.
const fromQR = new URLSearchParams(location.search).get("code");
if (fromQR) { field.value = fromQR; attempt(fromQR); }
</script></body>
"""

CAPTURE_PAGE = """<title>HomeStock - Add</title>""" + _HEAD.replace("__CHIP__", "") + """
<h1>Add to your kitchen</h1>
<p class="meta">Read on your computer. Nothing goes to the internet.</p>

<div class="sheet">
  <div>
    <button type="button" class="primary" id="shoot">Photograph a receipt</button>
    <input type="file" id="shot" accept="image/*" capture="environment" hidden>
    <p class="hint">The one that covers shopping in an actual shop.</p>
  </div>

  <div class="field">
    <label for="code">Barcode</label>
    <input type="tel" id="code" inputmode="numeric" placeholder="scan or type the number">
    <button type="button" class="secondary" id="scan">Scan with camera</button>
  </div>

  <div class="field">
    <label for="note">Or just type it</label>
    <input type="text" id="note" placeholder="2 milk, bread, 6 eggs" autocomplete="off">
    <button type="button" class="secondary" id="send">Add</button>
  </div>

  <p class="hint flash" id="msg"></p>
</div>
<footer><p id="recent"></p></footer>

<script>
window.HS_TOKEN = "__TOKEN__";
__HELPERS__
const msg = document.getElementById("msg");
async function send(body) {
  msg.textContent = "Sending…";
  const r = await api("/api/capture", body);
  const b = await r.json().catch(() => ({}));
  msg.textContent = r.ok ? "Added. Your computer will read it shortly."
                         : (b.error || "That did not work.");
  if (r.ok) recent();
}

document.getElementById("shoot").onclick = () => document.getElementById("shot").click();
document.getElementById("shot").addEventListener("change", e => {
  const f = e.target.files[0];
  if (!f) return;
  const reader = new FileReader();
  reader.onload = () => send({kind: "receipt_photo", data_url: reader.result, device: "phone"});
  reader.readAsDataURL(f);
  e.target.value = "";
});

document.getElementById("send").onclick = () => {
  const note = document.getElementById("note"), code = document.getElementById("code");
  if (code.value.trim()) { send({kind: "barcode", text: code.value, device: "phone"}); code.value = ""; }
  else if (note.value.trim()) { send({kind: "note", text: note.value, device: "phone"}); note.value = ""; }
  else msg.textContent = "Type what you bought first.";
};

// Chrome and recent Safari read a barcode natively; everything else types it.
const SCAN_TRIES = 200, SCAN_POLL_MS = 100;   // ~20 seconds of looking
document.getElementById("scan").onclick = async () => {
  if (!("BarcodeDetector" in window)) {
    msg.textContent = "This browser cannot scan — type the number, or photograph the label.";
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({video: {facingMode: "environment"}});
    const video = document.createElement("video");
    video.srcObject = stream; video.setAttribute("playsinline", ""); await video.play();
    const det = new BarcodeDetector();
    msg.textContent = "Point at the barcode…";
    for (let i = 0; i < SCAN_TRIES; i++) {
      const found = await det.detect(video).catch(() => []);
      if (found.length) {
        stream.getTracks().forEach(t => t.stop());
        return send({kind: "barcode", text: found[0].rawValue, device: "phone"});
      }
      await new Promise(r => setTimeout(r, SCAN_POLL_MS));
    }
    stream.getTracks().forEach(t => t.stop());
    msg.textContent = "Did not find a barcode. Type the number instead.";
  } catch (err) {
    msg.textContent = "No camera access — type the number instead.";
  }
};

async function recent() {
  const r = await fetch("/api/captures");
  if (!r.ok) return;
  const n = (await r.json()).length;
  document.getElementById("recent").textContent =
    n ? `${plural(n, "capture", "captures")} waiting to be read.` : "";
}
recent();
</script></body>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "HomeStock"

    # --- plumbing ---------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str, cookie: str | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _page(self, template: str) -> None:
        # A real document, not a fragment. Without the viewport meta a phone
        # lays the page out at 980px and zooms out, which is precisely the
        # device this page exists for.
        html = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                "<meta name=\"color-scheme\" content=\"light dark\">"
                + template.replace("__CSS__", CSS).replace("__MARK__", MARK)
                          .replace("__HELPERS__", HELPERS).replace("__TOKEN__", TOKEN)
                          .replace("__LAN__", "true" if LAN_MODE else "false")
                + "</html>")
        self._send(200, html.encode(), "text/html; charset=utf-8")

    def _body(self, cap: int | None = None) -> dict | None:
        cap = cap if cap is not None else MAX_UPLOAD * 2  # base64 inflates by ~4/3
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if n <= 0 or n > cap:
            return None
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, UnicodeDecodeError):
            return None
        return body if isinstance(body, dict) else None

    def _header_token(self) -> bool:
        """Proof the request came from a page we served. A browser will not
        attach a custom header cross-site without a preflight we never answer,
        so this — and not the cookie — is what stops another page on the
        network from driving this one."""
        return _secret_eq(self.headers.get("x-homestock-token", ""), TOKEN)

    def _cookie_token(self) -> bool:
        """Proof this device has paired. Rides along with any request the
        browser makes to us, cross-site ones included — so it establishes who,
        never what may be done. Deliberately a different secret from TOKEN:
        presenting this value as the write header must not work."""
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "hs" and _secret_eq(v, DEVICE_SECRET):
                return True
        return False

    def _loopback(self) -> bool:
        return self.client_address[0] in LOOPBACK

    def _host_ok(self) -> bool:
        """Reject a request that reached us under someone else's name.

        Without this the loopback server is open to DNS rebinding: a page on
        the public web whose hostname resolves to 127.0.0.1 becomes same-origin
        with us, and same-origin means it can read the whole grocery history
        and lift TOKEN straight out of the page it is allowed to fetch. The
        custom-header rule only ever defended against *cross*-origin callers.
        """
        host = self.headers.get("Host", "").strip()
        name = host.rsplit(":", 1)[0].strip("[]") if ":" in host else host
        allowed = {*LOOPBACK, "localhost"}
        if LAN_MODE:
            allowed.add(lan_ip())
        return name in allowed

    def _paired(self) -> bool:
        """In LAN mode nothing is readable until a device has paired. On
        loopback the filesystem is already the permission model — and the
        laptop has to be able to reach its own pairing code, which is the
        page it would otherwise be locked out of."""
        return True if (not LAN_MODE or self._loopback()) else (
            self._header_token() or self._cookie_token())

    # --- routes -----------------------------------------------------------

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "unrecognised Host"})
        path = self.path.split("?")[0]
        if not self._paired():
            return self._page(PAIR_PAGE) if path in ("/", "/capture") else self._json(403, {"error": "pair first"})
        if path == "/":
            self._page(PAGE)
        elif path == "/capture":
            self._page(CAPTURE_PAGE)
        elif path == "/api/state":
            self._json(200, build_state())
        elif path == "/api/providers":
            # What the settings screen offers, including whether each one sends
            # anything off this machine. The UI states that per provider.
            self._json(200, {"providers": chat.available_providers()})
        elif path == "/api/captures":
            self._json(200, list_captures("pending"))
        elif path == "/api/pairing":
            if not LAN_MODE:
                return self._json(404, {"error": "not in LAN mode"})
            if not _pair["code"] or time.time() > _pair["expires"]:
                new_pair_code()
            self._json(200, {"code": _pair["code"], "url": f"http://{lan_ip()}:{self.server.server_port}/",
                             "expires_in": int(_pair["expires"] - time.time())})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "unrecognised Host"})
        path = self.path.split("?")[0]

        if path == "/api/pair":
            # Unauthenticated, so it gets its own tiny body cap rather than the
            # photo-sized one: nothing here is bigger than a six-digit code.
            body = self._body(MAX_PAIR_BODY)
            if not body or not isinstance(body.get("code"), str):
                return self._json(400, {"error": "expected {code}"})
            if not check_pair_code(body["code"]):
                return self._json(403, {"error": "That code is wrong or expired. "
                                                 "Check your computer for a new one."})
            return self._send(200, b'{"paired":true}', "application/json",
                              cookie=f"hs={DEVICE_SECRET}; Path=/; HttpOnly; SameSite=Strict; "
                                     f"Max-Age={PAIRING_DAYS * 86400}")

        if not self._paired():
            return self._json(403, {"error": "pair first"})
        # Reads accept the pairing cookie; writes never do. A cookie says
        # which device is asking, not which page asked, and only the header
        # can say that.
        if not self._header_token():
            return self._json(403, {"error": "bad token"})

        body = self._body()
        if body is None:
            return self._json(400, {"error": "bad request body"})

        if path == "/api/action":
            item, action = body.get("item"), body.get("action")
            if not isinstance(item, str) or not isinstance(action, str):
                return self._json(400, {"error": "expected {item, action}"})
            if action == "have":
                result = correct_stock(item, 1)
            elif action == "out":
                result = correct_stock(item, 0)
            elif action == "binned":
                # Binning something means it is gone, not merely wasted. Record
                # the waste (the signal that a shelf-life estimate was too
                # generous) and the fact that there is none left, or the row
                # sits in "Use soon" nagging about food already in the bin.
                result = discard_item(item)
                if "error" not in result:
                    correct_stock(item, 0)
            else:
                return self._json(400, {"error": f"unknown action {action!r}"})
            return self._json(400 if "error" in result else 200, result)

        if path == "/api/chat":
            msgs, provider = body.get("messages"), body.get("provider")
            if not isinstance(msgs, list) or not isinstance(provider, str):
                return self._json(400, {"error": "expected {messages, provider}"})
            try:
                return self._json(200, chat.chat(
                    messages=msgs, provider=provider, model=body.get("model"),
                    api_key=body.get("api_key"), base_url=body.get("base_url")))
            except chat.ChatError as e:
                # Intelligible to a person: it is rendered straight into the
                # conversation, not into a log.
                return self._json(502, {"error": str(e)})

        if path == "/api/capture":
            kind = body.get("kind")
            device = body.get("device") if isinstance(body.get("device"), str) else None
            if kind == "receipt_photo":
                data_url = body.get("data_url")
                if not isinstance(data_url, str):
                    return self._json(400, {"error": "expected {data_url}"})
                stored, mime = save_photo(data_url)
                if not stored:
                    return self._json(400, {"error": "that image could not be read"})
                result = add_capture("receipt_photo", path=stored, device=device, mime=mime)
            elif kind in ("barcode", "note"):
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    return self._json(400, {"error": "expected {text}"})
                result = add_capture(kind, text=text.strip()[:2000], device=device)
            else:
                return self._json(400, {"error": f"unknown capture kind {kind!r}"})
            return self._json(400 if "error" in result else 200, result)

        self._json(404, {"error": "not found"})

    def log_message(self, *args) -> None:
        pass  # a kitchen app should not spew a request log


def _arg(name: str, default: str | None = None) -> str | None:
    """--name value, from argv. Enough of an argument parser for two flags."""
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def main(port: int = 7777, open_browser: bool = True, lan: bool | None = None) -> None:
    global LAN_MODE
    if lan is None:
        lan = "--lan" in sys.argv
    LAN_MODE = lan
    port = int(_arg("--port", str(port)))
    # The Mac app spawns this process and needs to know where it landed and
    # what the write token is. Port 0 asks the OS for a free one, and the
    # handshake line below is the only contract between the two halves.
    handshake = "--handshake" in sys.argv
    if handshake:
        open_browser = False

    server.init_db()
    # Binding every interface is the point of LAN mode, and the reason it is
    # opt-in: see this module's docstring for what guards it.
    host = "0.0.0.0" if LAN_MODE else "127.0.0.1"
    httpd = ThreadingHTTPServer((host, port), Handler)
    port = httpd.server_address[1]
    if handshake:
        print(json.dumps({"homestock": "ready", "port": port, "token": TOKEN,
                          "db": str(Path(server.DB_PATH))}), flush=True)
    print(f"HomeStock is at http://127.0.0.1:{port}/")
    print(f"Database: {Path(server.DB_PATH)}")
    if LAN_MODE:
        print(f"\nPhone capture: http://{lan_ip()}:{port}/")
        print(f"Pairing code:  {new_pair_code()}   (expires in {PAIR_TTL // 60} minutes)")
        print("Anyone on this network who has the code can add to your kitchen.")
    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{port}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
