from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = """<!doctype html><html><body><h1>Persistence fixture</h1>
<p id="status">{value}</p><button onclick="save()">Save</button>
<script>
const mode = '{mode}';
if (mode === 'fixed') document.getElementById('status').textContent = localStorage.getItem('value') || 'Not saved';
async function save() {{
  if (mode === 'fixed') localStorage.setItem('value', 'Saved value');
  if (mode === 'clean') await fetch(location.pathname, {{method: 'POST'}});
  document.getElementById('status').textContent = 'Saved value';
}}
</script></body></html>"""
REVISION = hashlib.sha256(PAGE.encode()).hexdigest()


@contextmanager
def fixture_server():
    values = set()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            mode = self.path.strip("/").split("/")[0]
            if mode not in {"buggy", "fixed", "clean"}:
                self.send_error(404)
                return
            value = "Saved value" if self.path in values else "Not saved"
            body = PAGE.format(mode=mode, value=value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not self.path.startswith("/clean/"):
                self.send_error(404)
                return
            values.add(self.path)
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
