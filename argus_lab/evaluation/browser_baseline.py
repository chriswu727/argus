from __future__ import annotations

from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from playwright.async_api import async_playwright


class BrowserBaseline:
    def __init__(self, origin: str):
        self.origin = origin
        self.playwright = None
        self.browser = None
        self.page = None

    async def start(self):
        if self.page is not None:
            return
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch()
        self.page = await self.browser.new_page()
        self.page.set_default_timeout(5000)
        await self.page.route("**/*", self.guard)

    async def guard(self, route):
        parsed = urlsplit(route.request.url)
        if f"{parsed.scheme}://{parsed.netloc}" != self.origin:
            await route.abort()
        else:
            await route.continue_()

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    def server(self) -> FastMCP:
        server = FastMCP(
            "browser",
            instructions="Use the browser tools to inspect and interact with the authorized application.",
        )

        @server.tool()
        async def browser_navigate(url: str) -> str:
            """Navigate to an authorized URL and return its accessible page snapshot."""
            parsed = urlsplit(url)
            if f"{parsed.scheme}://{parsed.netloc}" != self.origin:
                raise ValueError("URL outside the authorized fixture")
            await self.start()
            await self.page.goto(url)
            return await self.page.locator("body").aria_snapshot()

        @server.tool()
        async def browser_snapshot() -> str:
            """Read the visible accessible page structure, labels, text and input values."""
            return await self.page.locator("body").aria_snapshot()

        @server.tool()
        async def browser_click(role: str, name: str) -> str:
            """Click an element by its exact accessible role and name; return the new snapshot."""
            await self.page.get_by_role(role, name=name, exact=True).click()
            return await self.page.locator("body").aria_snapshot()

        @server.tool()
        async def browser_fill(label: str, value: str) -> str:
            """Fill an input by its exact accessible label; return the new snapshot."""
            await self.page.get_by_label(label, exact=True).fill(value)
            return await self.page.locator("body").aria_snapshot()

        @server.tool()
        async def browser_reload() -> str:
            """Reload the current page and return its accessible snapshot."""
            await self.page.reload()
            return await self.page.locator("body").aria_snapshot()

        @server.tool()
        async def browser_press(key: str) -> str:
            """Press a keyboard key, such as Enter, then return the accessible snapshot."""
            await self.page.keyboard.press(key)
            return await self.page.locator("body").aria_snapshot()

        @server.tool()
        async def browser_screenshot() -> Image:
            """Capture the current viewport as an image."""
            return Image(data=await self.page.screenshot(), format="png")

        return server
