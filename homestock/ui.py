"""HomeStock's window — the local pantry view.

The MCP server answers an agent; this answers a person. Same SQLite file, its
own process (WAL already makes concurrent writers queue — see
test_two_process_concurrent_writes), so the app can show a live kitchen while
an agent ingests receipts underneath it.

Run:  uv run python -m homestock.ui        (then open the printed URL)
Bind: 127.0.0.1 only. Never expose this; there is no authentication because
      the filesystem is the permission model.

Writes require a per-launch token sent as a header. A page on the open web
cannot read that token and cannot send a custom header without a CORS
preflight we refuse — so a random site cannot quietly empty your pantry.
"""

from __future__ import annotations

import json
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from homestock import server

TOKEN = secrets.token_urlsafe(24)

_fn = lambda t: t.fn if hasattr(t, "fn") else t  # noqa: E731
get_stock = _fn(server.get_stock)
discard_item = _fn(server.discard_item)
what_should_i_order = _fn(server.what_should_i_order)
get_expiring_soon = _fn(server.get_expiring_soon)
correct_stock = _fn(server.correct_stock)
get_health = _fn(server.get_health)

STATE_LABEL = {
    "likely_in_stock": "In stock",
    "likely_low": "Running low",
    "likely_out": "Probably out",
    "unknown": "Not enough history",
}


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
    return {"health": health, "order": order, "expiring": expiring, "shelf": shelf}


PAGE = """<title>HomeStock</title>
<style>
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
.pill { font-size:.75rem; color:var(--muted); border:1px solid var(--line); border-radius:999px; padding:2px 8px; }
.conf-low { opacity:.6; }
footer { margin-top:40px; padding-top:16px; border-top:1px solid var(--line);
         color:var(--muted); font-size:.82rem; }
footer code { font-size:.95em; word-break:break-all; }
.warn { background:color-mix(in srgb, var(--out) 12%, transparent); border:1px solid var(--out);
        border-radius:10px; padding:12px 14px; margin:16px 0; }
.empty { color:var(--muted); padding:14px; }
</style>

<h1>Your kitchen</h1>
<p class="sub" id="tagline">Built from your receipts. Nothing typed in, nothing uploaded.</p>
<div id="app">Loading…</div>

<footer>
  <p id="foot"></p>
  <p>Everything lives in one file on this computer. Copy it to back it up, delete it to erase
     everything. Nothing is sent anywhere.</p>
</footer>

<script>
const TOKEN = "__TOKEN__";
const LABEL = __LABELS__;
// What is actually useful to say about an item depends on why it is listed.
const ACTIONS = {
  order:    [["have", "Already have it"]],
  expiring: [["out", "Used it"], ["binned", "Binned it"]],
  shelf:    [["have", "Still have it"], ["out", "Out of it"]],
};
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

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
  document.getElementById("foot").innerHTML =
    `${h.items} items - ${h.receipt_lines} receipt lines` +
    (h.last_ingest_at ? ` - last read ${esc(h.last_ingest_at)}` : ``) +
    `<br>Database: <code>${esc(h.db_path)}</code>`;

  document.querySelectorAll("button[data-item]").forEach(b => b.onclick = async () => {
    document.querySelectorAll("button[data-item]").forEach(x => x.disabled = true);
    await fetch("/api/action", {
      method: "POST",
      headers: {"content-type": "application/json", "x-homestock-token": TOKEN},
      body: JSON.stringify({item: b.dataset.item, action: b.dataset.action}),
    });
    render();
  });
}
render();
</script>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "HomeStock"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # No page on the open web should be able to read a kitchen.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            page = PAGE.replace("__TOKEN__", TOKEN).replace("__LABELS__", json.dumps(STATE_LABEL))
            self._send(200, page.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._json(200, build_state())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/action":
            return self._json(404, {"error": "not found"})
        if not secrets.compare_digest(self.headers.get("x-homestock-token", ""), TOKEN):
            return self._json(403, {"error": "bad token"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            item, action = str(body["item"]), str(body["action"])
        except (ValueError, KeyError, TypeError):
            return self._json(400, {"error": "expected {item, action}"})
        if action == "have":
            result = correct_stock(item, 1)
        elif action == "out":
            result = correct_stock(item, 0)
        elif action == "binned":
            result = discard_item(item)
        else:
            return self._json(400, {"error": f"unknown action {action!r}"})
        self._json(400 if "error" in result else 200, result)

    def log_message(self, *args) -> None:
        pass  # a kitchen app should not spew a request log


def main(port: int = 7777, open_browser: bool = True) -> None:
    server.init_db()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"HomeStock is at {url}")
    print(f"Database: {Path(server.DB_PATH)}")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
