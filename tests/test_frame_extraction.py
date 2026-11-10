"""Elements inside iframes are listed once, with a chain that reaches them."""

from __future__ import annotations

import http.server
import threading

import pytest

from argus.application.actions import click_what
from argus.application.lifecycle import start_session

LOG = "<pre id='log'></pre><script>function log(t){document.getElementById('log').textContent+=t+';'}</script>"


def _pages(main: str, other: str) -> dict:
    return {
        main: {
            "/srcdoc": LOG
            + "<iframe id='editor' srcdoc=\"<button onclick='parent.log(&quot;BOLD&quot;)'>Bold</button>\"></iframe>"
            + "<iframe id='blank'></iframe><script>"
            + "const d=document.getElementById('blank').contentDocument;"
            + "const b=d.createElement('button');b.textContent='Italic';"
            + "b.onclick=()=>log('ITALIC');d.body.appendChild(b);</script>",
            "/nested": LOG + f"<iframe id='outer' src='{other}/outer'></iframe>",
            "/leaf": "<button onclick=\"top.log('LEAF')\">Leaf</button>",
        },
        other: {
            "/outer": "<button>Outer</button><iframe id='same' src='/same'></iframe>"
            + f"<iframe id='inner' src='{main}/leaf'></iframe>",
            "/same": "<button onclick=\"this.textContent='Same done'\">Same</button>",
        },
    }


@pytest.fixture
def origins():
    servers, threads, pages = [], [], {}

    def handler_for(origin):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(pages[origin].get(self.path, "<p>home</p>").encode())

        return Handler

    for _ in range(2):
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), http.server.BaseHTTPRequestHandler
        )
        origin = f"http://127.0.0.1:{server.server_port}"
        server.RequestHandlerClass = handler_for(origin)
        servers.append(server)
        threads.append(threading.Thread(target=server.serve_forever, daemon=True))
        threads[-1].start()
    main, other = (f"http://127.0.0.1:{s.server_port}" for s in servers)
    pages.update(_pages(main, other))
    try:
        yield main, other
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()


async def _elements(runtime):
    return (await runtime.session.browser.get_state()).elements


async def _log(runtime) -> str:
    return await runtime.session.browser._page.inner_text("#log")


async def test_srcdoc_and_blank_frames_are_listed_once(isolated_runtime, origins):
    main, _ = origins
    try:
        await start_session(main + "/srcdoc")
        elements = await _elements(isolated_runtime)
        assert [el.frame for el in elements if el.text == "Bold"] == ['iframe[id="editor"]']
        assert [el.frame for el in elements if el.text == "Italic"] == ['iframe[id="blank"]']
        await click_what("Bold")
        await click_what("Italic")
        assert await _log(isolated_runtime) == "BOLD;ITALIC;"
    finally:
        await isolated_runtime.shutdown()


async def test_nested_cross_origin_frames_keep_the_whole_chain(isolated_runtime, origins):
    main, _ = origins
    try:
        await start_session(main + "/nested")
        elements = await _elements(isolated_runtime)
        frames = {
            text: [el.frame for el in elements if el.text == text]
            for text in ("Outer", "Same", "Leaf")
        }
        assert frames == {
            "Outer": ['iframe[id="outer"]'],
            "Same": ['iframe[id="outer"] >>> iframe[id="same"]'],
            "Leaf": ['iframe[id="outer"] >>> iframe[id="inner"]'],
        }
        assert not [el for el in elements if el.tag == "iframe"]
        await click_what("Leaf")
        assert await _log(isolated_runtime) == "LEAF;"
        await click_what("Same")
        assert "Same done" in [el.text for el in await _elements(isolated_runtime)]
    finally:
        await isolated_runtime.shutdown()
