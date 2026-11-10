"""crawl_site stays on the origin it started from."""

from __future__ import annotations

import http.server
import threading

import pytest

import argus.mcp_server as m
from argus.application.lifecycle import start_session
from argus.application.probes import crawl_site


@pytest.fixture
def two_sites():
    routes: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            port = self.server.server_address[1]
            route = routes.get((port, self.path))
            if route is None:
                self.send_response(404)
                self.end_headers()
                return
            if route.startswith("redirect:"):
                self.send_response(302)
                self.send_header("Location", route[len("redirect:") :])
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(route.encode())

    servers = [http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for thread in threads:
        thread.start()
    a, b = (s.server_address[1] for s in servers)
    site_a, site_b = f"http://127.0.0.1:{a}", f"http://127.0.0.1:{b}"
    routes.update(
        {
            (a, "/"): "<h1>A home</h1><a href='/out'>Partner</a> <a href='/about'>About</a>",
            (a, "/about"): "<h1>A about</h1><script>console.error('about broke')</script>",
            (a, "/out"): f"redirect:{site_b}/",
            (b, "/"): "<h1>B home</h1><a href='/b-only'>B only</a>",
            (b, "/b-only"): "<h1>B only</h1>",
        }
    )
    try:
        yield site_a, site_b
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()


async def test_a_redirect_to_another_origin_is_not_followed(isolated_runtime, two_sites):
    site_a, site_b = two_sites
    record_bug = getattr(m.record_bug, "fn", m.record_bug)
    try:
        await start_session(site_a + "/")
        await record_bug("Earlier finding", "low", {"expected": "x", "actual": "y"})
        s = isolated_runtime.session
        assert len(s.bugs) == 1

        report = str(await crawl_site(max_pages=10))

        assert not [url for url in s.pages_visited if url.startswith(site_b)]
        assert "b-only" not in report
        assert f"{site_a}/out  (redirected to another origin; not crawled)" in report
        assert f"{site_a}/about" in report
        assert "event-bugs captured" in report
        assert len(s.bugs) == 2
        assert ", 1 event-bugs captured" in report.splitlines()[0]
        assert s.browser.current_url == site_a + "/"
    finally:
        await isolated_runtime.shutdown()
