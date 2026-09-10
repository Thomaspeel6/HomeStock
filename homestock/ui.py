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
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from homestock import server

TOKEN = secrets.token_urlsafe(24)
LAN_MODE = False
PAIR_TTL = 600           # a pairing code is good for ten minutes
PAIR_MAX_ATTEMPTS = 5    # then it is burned, not merely delayed
MAX_UPLOAD = 12 * 1024 * 1024

_pair = {"code": None, "expires": 0.0, "attempts": 0}

_fn = lambda t: t.fn if hasattr(t, "fn") else t  # noqa: E731
get_stock = _fn(server.get_stock)
what_should_i_order = _fn(server.what_should_i_order)
get_expiring_soon = _fn(server.get_expiring_soon)
correct_stock = _fn(server.correct_stock)
discard_item = _fn(server.discard_item)
get_health = _fn(server.get_health)
add_capture = _fn(server.add_capture)
list_captures = _fn(server.list_captures)


def new_pair_code() -> str:
    _pair.update(code=f"{secrets.randbelow(1000000):06d}",
                 expires=time.time() + PAIR_TTL, attempts=0)
    return _pair["code"]


def check_pair_code(given: str) -> bool:
    """One-shot check. A wrong guess costs an attempt; five burns the code."""
    code = _pair["code"]
    if not code or time.time() > _pair["expires"]:
        return False
    _pair["attempts"] += 1
    if _pair["attempts"] > PAIR_MAX_ATTEMPTS:
        _pair["code"] = None
        return False
    return secrets.compare_digest(given.strip().replace("-", "").replace(" ", ""), code)


def lan_ip() -> str:
    """This machine's address on the local network. No packets are sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 1))  # TEST-NET-1: routable nowhere
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


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


CSS = """
:root {
  --bg:#faf9f7; --card:#fff; --ink:#1c1a17; --muted:#6b6560; --line:#e6e1da;
  --ok:#2f7d4f; --low:#b8720c; --out:#b2402c; --accent:#1c1a17;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#171614; --card:#211f1c; --ink:#f2efea; --muted:#a09a92; --line:#332f2b;
  --ok:#6cc48d; --low:#e0a44a; --out:#e3806a; --accent:#f2efea;
} }
:root[data-theme="dark"] {
  --bg:#171614; --card:#211f1c; --ink:#f2efea; --muted:#a09a92; --line:#332f2b;
  --ok:#6cc48d; --low:#e0a44a; --out:#e3806a; --accent:#f2efea;
}
* { box-sizing:border-box; }
body { background:var(--bg); color:var(--ink); font:16px/1.5 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif;
       margin:0 auto; max-width:760px; padding:16px; padding-block:32px; }
h1 { font-size:1.6rem; margin:0 0 4px; letter-spacing:-.02em; }
h2 { font-size:.8rem; text-transform:uppercase; letter-spacing:.08em; color:var(--muted);
     margin:32px 0 10px; font-weight:600; }
.sub { color:var(--muted); margin:0 0 8px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
.row { display:flex; align-items:center; gap:12px; padding:12px 14px; border-top:1px solid var(--line); flex-wrap:wrap; }
.row:first-child { border-top:none; }
.name { font-weight:550; flex:1 1 180px; min-width:0; }
.why { color:var(--muted); font-size:.85rem; flex:1 1 100%; order:3; }
@media (min-width:560px) { .why { flex:0 1 auto; order:0; text-align:right; } }
.dot { width:8px; height:8px; border-radius:50%; flex:none; }
.s-likely_in_stock .dot { background:var(--ok); }
.s-likely_low .dot { background:var(--low); }
.s-likely_out .dot, .s-expiring .dot { background:var(--out); }
.s-unknown .dot { background:var(--line); border:1px solid var(--muted); }
.acts { display:flex; gap:6px; flex:none; }
button { font:inherit; font-size:.85rem; padding:5px 11px; border-radius:999px; cursor:pointer;
         border:1px solid var(--line); background:transparent; color:var(--ink); }
button:hover { border-color:var(--accent); }
button:disabled { opacity:.45; cursor:default; }
.conf-low { opacity:.6; }
footer { margin-top:40px; padding-top:16px; border-top:1px solid var(--line);
         color:var(--muted); font-size:.82rem; }
footer code { font-size:.95em; word-break:break-all; }
.warn { background:color-mix(in srgb, var(--out) 12%, transparent); border:1px solid var(--out);
        border-radius:10px; padding:12px 14px; margin:16px 0; }
.note { background:color-mix(in srgb, var(--ok) 10%, transparent); border:1px solid var(--ok);
        border-radius:10px; padding:12px 14px; margin:16px 0; }
.empty { color:var(--muted); padding:14px; }
input[type=text], input[type=tel], textarea {
  font:inherit; width:100%; padding:11px 13px; border-radius:10px; color:var(--ink);
  border:1px solid var(--line); background:var(--bg); }
label { display:block; font-weight:550; margin:0 0 6px; }
.big { display:block; width:100%; padding:18px; font-size:1.05rem; text-align:center;
       border-radius:12px; margin-bottom:10px; }
.code { font:600 2.2rem/1.2 ui-monospace,SFMono-Regular,Menlo,monospace;
        letter-spacing:.15em; text-align:center; margin:10px 0; }
.stack > * + * { margin-top:14px; }
.hint { color:var(--muted); font-size:.85rem; margin:6px 0 0; }
"""

PAGE = """<title>HomeStock</title>
<style>__CSS__</style></head><body>

<h1>Your kitchen</h1>
<p class="sub">Built from your receipts. Nothing typed in, nothing uploaded.</p>
<div id="app">Loading…</div>

<h2>Add something</h2>
<div class="card stack" style="padding:14px">
  <label for="quick">Type what you bought</label>
  <input type="text" id="quick" placeholder="2 milk, bread, 6 eggs" autocomplete="off">
  <p class="hint">Goes to the inbox for your agent to read — it works out the
     names and quantities. <span id="pending"></span></p>
  <p id="lanbox"></p>
</div>

<footer>
  <p id="foot"></p>
  <p>Everything lives in one file on this computer. Copy it to back it up, delete it to erase
     everything. Nothing is sent anywhere.</p>
</footer>

<script>
const TOKEN = "__TOKEN__";
const LAN = __LAN__;
// What is useful to say about an item depends on why it is listed.
const ACTIONS = {
  order:    [["have", "Already have it"]],
  expiring: [["out", "Used it"], ["binned", "Binned it"]],
  shelf:    [["have", "Still have it"], ["out", "Out of it"]],
};
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function api(path, body) {
  return fetch(path, {
    method: "POST",
    headers: {"content-type": "application/json", "x-homestock-token": TOKEN},
    body: JSON.stringify(body),
  });
}

function row(it, kind) {
  const state = kind === "expiring" ? "expiring" : it.estimated_state;
  let why;
  if (kind === "expiring") {
    why = it.days_left < 0 ? `use-by was ${-it.days_left}d ago`
        : it.days_left === 0 ? "use by today" : `use within ${it.days_left}d`;
  } else if (kind === "order") {
    why = it.reason === "you said you were out" ? "you said you were out"
        : `${it.days_since_observation}d since last, usually every ${Math.round(it.median_interval_days)}d`;
  } else if (it.confirmed) {
    why = "you confirmed this";
  } else if (it.median_interval_days) {
    why = `bought ${it.days_since_observation}d ago, usually every ${Math.round(it.median_interval_days)}d`;
  } else {
    why = `bought once, ${it.days_since_last_purchase}d ago`;
  }
  const dim = it.confidence === "low" && !it.confirmed ? " conf-low" : "";
  return `<div class="row s-${state}${dim}">
    <span class="dot"></span>
    <span class="name">${esc(it.name)}</span>
    <span class="why">${esc(why)}</span>
    <span class="acts">${ACTIONS[kind].map(a =>
      `<button data-item="${esc(it.name)}" data-action="${a[0]}">${a[1]}</button>`).join("")}
    </span>
  </div>`;
}

function section(title, items, kind, empty) {
  if (!items.length) return `<h2>${title}</h2><div class="card"><p class="empty">${empty}</p></div>`;
  return `<h2>${title}</h2><div class="card">${items.map(i => row(i, kind)).join("")}</div>`;
}

async function render() {
  const s = await (await fetch("/api/state")).json();
  const h = s.health;
  let html = "";
  if (h.stale) {
    html += `<div class="warn"><strong>Not up to date.</strong> ` + (h.last_ingest_at
      ? `No receipts have been read for ${h.days_since_ingest} days.`
      : `No receipts have been read yet.`) +
      ` Estimates below may be out of date.</div>`;
  }
  html += section("Buy these", s.order, "order", "Nothing looks due. Enjoy it.");
  html += section("Use soon", s.expiring, "expiring", "Nothing about to go off.");
  html += section("Probably in the house", s.shelf, "shelf", "No history yet — read some receipts to start.");
  document.getElementById("app").innerHTML = html;

  document.getElementById("pending").textContent =
    s.pending_captures ? `${s.pending_captures} waiting to be read.` : "";
  document.getElementById("foot").innerHTML =
    `${h.items} items - ${h.receipt_lines} receipt lines` +
    (h.last_ingest_at ? ` - last read ${esc(h.last_ingest_at)}` : ``) +
    `<br>Database: <code>${esc(h.db_path)}</code>`;

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
  if (!LAN) {
    document.getElementById("lanbox").innerHTML =
      `<span class="hint">To send photos from your phone, restart with
       <code>homestock-ui --lan</code>.</span>`;
    return;
  }
  const p = await (await fetch("/api/pairing")).json();
  document.getElementById("lanbox").innerHTML =
    `<div class="note"><strong>On your phone</strong>, on the same wifi, open
     <code>${esc(p.url)}</code> and enter this code:
     <div class="code">${esc(p.code.slice(0,3))}-${esc(p.code.slice(3))}</div>
     <span class="hint">Expires in ${Math.round(p.expires_in/60)} minutes. Photos go
     straight to this computer — never to the internet.</span></div>`;
}

render();
showPairing();
</script></body>
"""

PAIR_PAGE = """<title>Pair with HomeStock</title>
<style>__CSS__</style></head><body>
<h1>HomeStock</h1>
<p class="sub">Enter the code shown on your computer to use this phone for capture.</p>
<div class="card" style="padding:16px">
  <label for="code">Pairing code</label>
  <input type="tel" id="code" inputmode="numeric" autocomplete="off" placeholder="000000">
  <p id="err" class="hint"></p>
  <button class="big" id="go" style="margin-top:12px">Pair this phone</button>
</div>
<script>
const go = document.getElementById("go");
go.onclick = async () => {
  go.disabled = true;
  const r = await fetch("/api/pair", {
    method: "POST", headers: {"content-type": "application/json"},
    body: JSON.stringify({code: document.getElementById("code").value}),
  });
  if (r.ok) { location.href = "/capture"; return; }
  const b = await r.json().catch(() => ({}));
  document.getElementById("err").textContent = b.error || "That code did not work.";
  go.disabled = false;
};
</script></body>
"""

CAPTURE_PAGE = """<title>HomeStock - Add</title>
<style>__CSS__</style></head><body>
<h1>Add to your kitchen</h1>
<p class="sub">Photos are read on your computer. Nothing goes to the internet.</p>

<div class="card" style="padding:16px">
  <label class="big" for="shot" style="border:1px solid var(--line); cursor:pointer">
    Photograph a receipt
  </label>
  <input type="file" id="shot" accept="image/*" capture="environment" hidden>

  <label for="code" style="margin-top:18px">Barcode</label>
  <input type="tel" id="code" inputmode="numeric" placeholder="scan or type the number">
  <button class="big" id="scan" style="margin-top:8px">Scan with camera</button>

  <label for="note" style="margin-top:18px">Or just type it</label>
  <input type="text" id="note" placeholder="2 milk, bread, 6 eggs" autocomplete="off">
  <button class="big" id="send" style="margin-top:8px">Add</button>
  <p id="msg" class="hint"></p>
</div>
<footer><p id="recent"></p></footer>

<script>
const msg = document.getElementById("msg");
async function send(body) {
  msg.textContent = "Sending…";
  const r = await fetch("/api/capture", {
    method: "POST", headers: {"content-type": "application/json", "x-homestock-token": "__TOKEN__"},
    body: JSON.stringify(body),
  });
  const b = await r.json().catch(() => ({}));
  msg.textContent = r.ok ? "Added. Your computer will read it shortly." : (b.error || "That did not work.");
  if (r.ok) recent();
}

document.getElementById("shot").addEventListener("change", async e => {
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
};

// Chrome and recent Safari can read a barcode natively; everything else types it.
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
    for (let i = 0; i < 200; i++) {
      const found = await det.detect(video).catch(() => []);
      if (found.length) {
        stream.getTracks().forEach(t => t.stop());
        return send({kind: "barcode", text: found[0].rawValue, device: "phone"});
      }
      await new Promise(r => setTimeout(r, 100));
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
  document.getElementById("recent").textContent = n ? `${n} waiting to be read.` : "";
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
                + template.replace("__CSS__", CSS).replace("__TOKEN__", TOKEN)
                          .replace("__LAN__", "true" if LAN_MODE else "false")
                + "</html>")
        self._send(200, html.encode(), "text/html; charset=utf-8")

    def _body(self) -> dict | None:
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if n <= 0 or n > MAX_UPLOAD * 2:  # base64 inflates by ~4/3
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
        return secrets.compare_digest(self.headers.get("x-homestock-token", ""), TOKEN)

    def _cookie_token(self) -> bool:
        """Proof this device has paired. Rides along with any request the
        browser makes to us, cross-site ones included — so it establishes who,
        never what may be done."""
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "hs" and secrets.compare_digest(v, TOKEN):
                return True
        return False

    def _paired(self) -> bool:
        """In LAN mode nothing is readable until a device has paired. On
        loopback the filesystem is already the permission model."""
        return (self._header_token() or self._cookie_token()) if LAN_MODE else True

    # --- routes -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if not self._paired():
            return self._page(PAIR_PAGE) if path in ("/", "/capture") else self._json(403, {"error": "pair first"})
        if path == "/":
            self._page(PAGE)
        elif path == "/capture":
            self._page(CAPTURE_PAGE)
        elif path == "/api/state":
            self._json(200, build_state())
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

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]

        if path == "/api/pair":
            body = self._body()
            if not body or not isinstance(body.get("code"), str):
                return self._json(400, {"error": "expected {code}"})
            if not check_pair_code(body["code"]):
                return self._json(403, {"error": "That code is wrong or expired. "
                                                 "Check your computer for a new one."})
            return self._send(200, b'{"paired":true}', "application/json",
                              cookie=f"hs={TOKEN}; Path=/; SameSite=Strict; Max-Age=2592000")

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
                result = discard_item(item)
            else:
                return self._json(400, {"error": f"unknown action {action!r}"})
            return self._json(400 if "error" in result else 200, result)

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


def main(port: int = 7777, open_browser: bool = True, lan: bool | None = None) -> None:
    global LAN_MODE
    if lan is None:
        lan = "--lan" in sys.argv
    LAN_MODE = lan

    server.init_db()
    host = "0.0.0.0" if LAN_MODE else "127.0.0.1"  # noqa: S104 — opt-in, see module docstring
    httpd = ThreadingHTTPServer((host, port), Handler)
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
