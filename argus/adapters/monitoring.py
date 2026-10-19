from __future__ import annotations

import asyncio
import json
import socket
from contextlib import contextmanager
from importlib.resources import files
from urllib.parse import urlsplit

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    JSONResponse,
    PlainTextResponse,
    Response,
)
from starlette.routing import Route

from argus.application.manager import SessionManager


class Monitor:
    def __init__(self, runtime: SessionManager, port: int = 0):
        self.runtime = runtime
        self.port = port
        self.subscribers = 0
        self.server = None
        self.task = None
        self.socket = None
        self.app = Starlette(
            routes=[
                Route("/healthz", self.health),
                Route("/readyz", self.ready),
                Route("/api/status", self.status),
                Route("/api/events", self.events),
                Route("/metrics", self.metrics),
                Route("/", self.asset),
                Route("/dashboard.js", self.asset),
                Route("/dashboard.css", self.asset),
            ]
        )
        self.app = _AccessGuard(self.app, self)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def allowed(self, request: Request) -> bool:
        allowed = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        host = request.headers.get("host", "").lower()
        origin = request.headers.get("origin")
        try:
            parsed = urlsplit(origin) if origin else None
            valid_origin = parsed is None or (
                parsed.scheme == "http"
                and parsed.netloc.lower() in allowed
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            return False
        return (
            host in allowed
            and valid_origin
            and request.headers.get("sec-fetch-site") != "cross-site"
        )

    async def health(self, request: Request) -> Response:
        return JSONResponse({"status": "ok", "schema_version": 1})

    async def ready(self, request: Request) -> Response:
        ready = self.runtime.ready and not self.runtime.closing
        return JSONResponse({"ready": ready}, status_code=200 if ready else 503)

    async def status(self, request: Request) -> Response:
        return JSONResponse(self.runtime.snapshot())

    async def events(self, request: Request) -> Response:
        try:
            cursor = int(
                request.headers.get("last-event-id", request.query_params.get("after", "0"))
            )
            if cursor < 0:
                raise ValueError
        except ValueError:
            return JSONResponse({"error": "invalid event cursor"}, status_code=400)
        if self.subscribers >= 32:
            return JSONResponse({"error": "subscriber limit reached"}, status_code=429)
        self.subscribers += 1

        async def stream():
            nonlocal cursor
            hub = self.runtime.events
            while not hub.closed:
                changed = hub.changed
                gap, batch = hub.read(cursor)
                if gap:
                    yield 'event: gap\ndata: {"reason":"events outside retention window; refresh status"}\n\n'
                for event in batch:
                    cursor = event["sequence"]
                    yield f"id: {cursor}\nevent: runtime\ndata: {json.dumps(event)}\n\n"
                if gap and not batch:
                    cursor = hub.sequence
                try:
                    await asyncio.wait_for(changed.wait(), timeout=10)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"

        def release():
            self.subscribers -= 1

        return _EventResponse(stream(), release)

    async def metrics(self, request: Request) -> Response:
        runtime = self.runtime
        lines = [
            "# TYPE argus_ready gauge",
            f"argus_ready {int(runtime.ready and not runtime.closing)}",
            "# TYPE argus_events_dropped_total counter",
            f"argus_events_dropped_total {runtime.events.dropped}",
            "# TYPE argus_operation_calls_total counter",
        ]
        for name, count in sorted(runtime.calls.items()):
            label = json.dumps(name)
            lines.extend(
                [
                    f"argus_operation_calls_total{{operation={label}}} {count}",
                    f"argus_operation_failures_total{{operation={label}}} {runtime.failures[name]}",
                    f"argus_operation_duration_seconds_sum{{operation={label}}} {runtime.durations[name]}",
                    f"argus_operation_duration_seconds_count{{operation={label}}} {count}",
                ]
            )
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    async def asset(self, request: Request) -> Response:
        name = "dashboard.html" if request.url.path == "/" else request.url.path.lstrip("/")
        media = {
            "dashboard.html": "text/html",
            "dashboard.js": "text/javascript",
            "dashboard.css": "text/css",
        }[name]
        return Response(
            files("argus.adapters.assets").joinpath(name).read_bytes(), media_type=media
        )

    async def start(self) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.socket.bind(("127.0.0.1", self.port))
            self.socket.listen(128)
            self.socket.setblocking(False)
            self.port = self.socket.getsockname()[1]
            self.server = _Server(
                uvicorn.Config(
                    self.app, log_level="warning", lifespan="off", timeout_graceful_shutdown=2
                )
            )
            self.task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
            for _ in range(100):
                if self.server.started:
                    return
                if self.task.done():
                    await self.task
                    raise RuntimeError("monitor exited before startup")
                await asyncio.sleep(0.02)
            raise TimeoutError("monitor startup timed out")
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
        if self.task:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 3)
            except asyncio.TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        if self.socket:
            self.socket.close()


class _Server(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield


class _AccessGuard:
    def __init__(self, app, monitor):
        self.app, self.monitor = app, monitor

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not self.monitor.allowed(Request(scope)):
            await PlainTextResponse("Local same-origin access only", status_code=403)(
                scope, receive, send
            )
            return

        async def secured(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (
                        b"content-security-policy",
                        b"default-src 'self'; frame-ancestors 'none'; base-uri 'none'",
                    ),
                    (b"referrer-policy", b"no-referrer"),
                ]
            await send(message)

        await self.app(scope, receive, secured)


class _EventResponse(Response):
    media_type = "text/event-stream"

    def __init__(self, stream, release):
        super().__init__(headers={"X-Accel-Buffering": "no"})
        del self.headers["content-length"]
        self.stream = stream
        self.release = release

    async def __call__(self, scope, receive, send):
        async def pump():
            async for chunk in self.stream:
                await send(
                    {"type": "http.response.body", "body": chunk.encode(), "more_body": True}
                )
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        async def disconnected():
            while (await receive())["type"] != "http.disconnect":
                pass

        tasks = []
        try:
            await send({"type": "http.response.start", "status": 200, "headers": self.raw_headers})
            tasks = [asyncio.create_task(pump()), asyncio.create_task(disconnected())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except OSError:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.stream.aclose()
            self.release()
