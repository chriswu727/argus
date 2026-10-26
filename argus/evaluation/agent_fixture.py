from __future__ import annotations

import hashlib
import html
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .models import Case

TASKS = {
    "profile": (
        "Check editing a display name. A saved name must remain after reload.",
        ["edit-name", "reload-name"],
    ),
    "tasks": (
        "Check removing the seeded task. A deleted task must stay deleted after reload.",
        ["delete-task", "reload-task"],
    ),
    "form": (
        "Check newsletter signup with invalid and valid email addresses. Invalid addresses must be rejected; a valid address must succeed without losing recoverability.",
        ["invalid-email", "valid-email"],
    ),
    "cart": (
        "Check the basket and checkout. With two items at $12 each, the basket and checkout subtotal must both be $24. Tax and shipping are not included.",
        ["basket-total", "checkout-total"],
    ),
}
CASES = [
    Case(
        id=f"{family}-{variant}",
        expected_bugs=(
            [f"{family}-contract"]
            if variant == "buggy"
            else ["form-localpart"]
            if family == "form"
            else []
        ),
        goals=goals,
    )
    for family, (_, goals) in TASKS.items()
    for variant in ("buggy", "fixed")
]


def render(family: str, path: str, state: dict, faulty: bool) -> str:
    if family == "profile":
        content = f'<h1>Profile</h1><p>Display name: {html.escape(state["name"])}</p><form method="post"><label>Display name<input name="name" value="{html.escape(state["name"], quote=True)}"></label><button>Save</button></form>'
    elif family == "tasks":
        content = "<h1>Tasks</h1>"
        if not state["deleted"]:
            content += (
                '<p>Prepare itinerary</p><form method="post"><button>Delete task</button></form>'
            )
        else:
            content += "<p>No tasks</p>"
    elif family == "form":
        content = '<h1>Newsletter</h1><form method="post" novalidate><label>Email<input name="email" type="email"></label><button>Subscribe</button></form>'
    else:
        checkout = path == "/checkout"
        total = 12 if checkout and faulty else 24
        content = f"<h1>{'Checkout' if checkout else 'Basket'}</h1><p>Quantity: 2</p><p>Unit price: $12</p><p>Subtotal: ${total}</p>"
        content += (
            '<a href="/">Back to basket</a>' if checkout else '<a href="/checkout">Checkout</a>'
        )
    message = state.get("message", "")
    return f'<!doctype html><html lang="en"><head><title>Test application</title></head><body>{content}<p role="status">{html.escape(message)}</p></body></html>'


REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


@contextmanager
def agent_fixture(case_id: str):
    family, variant = case_id.rsplit("-", 1)
    if family not in TASKS or variant not in {"buggy", "fixed"}:
        raise ValueError("unknown case")
    faulty = variant == "buggy"
    state = {"name": "Alex", "deleted": False, "message": ""}
    events = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if path not in {"/", "/checkout"}:
                self.send_error(404)
                return
            with lock:
                if state.pop("just_submitted", False):
                    pass
                elif faulty:
                    if family == "profile":
                        state["name"] = "Alex"
                    elif family == "tasks":
                        state["deleted"] = False
                events.append({"method": "GET", "path": path, "state": dict(state)})
                body = render(family, path, state, faulty).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 8192:
                self.send_error(413)
                return
            data = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
            with lock:
                if family == "profile":
                    state["name"] = data.get("name", [""])[0][:200]
                    state["message"] = "Saved"
                elif family == "tasks":
                    state["deleted"] = True
                    state["message"] = "Deleted"
                elif family == "form":
                    email = data.get("email", [""])[0][:200]
                    valid = (
                        "@" in email
                        and "." in email.rsplit("@", 1)[-1]
                        and not any(c.isspace() for c in email)
                    )
                    state["message"] = (
                        "Subscribed" if valid or faulty else "Invalid email; please correct it"
                    )
                state["just_submitted"] = True
                events.append({"method": "POST", "path": "/", "data": data, "state": dict(state)})
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", events
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
