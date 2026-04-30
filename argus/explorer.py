from __future__ import annotations

import asyncio
import time
from pathlib import Path

from rich.console import Console

from .browser import BrowserDriver
from .config import Config
from .detector import Detector
from .models import Action, ActionType, ExplorationResult, Screenshot

console = Console()


class Explorer:
    """Drives the exploration loop: observe -> plan -> act -> detect."""

    def __init__(self, config: Config):
        self.config = config
        self.browser = BrowserDriver(
            headless=config.headless,
            viewport_width=config.viewport_width,
            viewport_height=config.viewport_height,
        )
        self.planner = Planner(model=config.model, api_base=config.api_base, api_key=config.api_key)
        self.detector = Detector()
        self.bugs = []
        self.screenshots: list[Screenshot] = []
        self.history: list[tuple[str, Action]] = []
        self.pages_visited: set[str] = set()
        self.steps: list[str] = []
        self._screenshot_counter = 0

    async def run(self) -> ExplorationResult:
        start = time.time()

        console.print(
            f"\n[bold blue]Argus[/] starting exploration of [cyan]{self.config.url}[/]"
        )
        if self.config.focus_areas:
            console.print("[bold]Focus areas:[/]")
            for fa in self.config.focus_areas:
                console.print(f"  * {fa.name}: {fa.description}")
        console.print()

        await self.browser.start()

        try:
            await self.browser.goto(self.config.url)
            self.pages_visited.add(self.config.url)

            # Initial screenshot
            await self._take_screenshot("initial", "Initial page load")

            for step_num in range(self.config.max_steps):
                state = await self.browser.get_state()
                prev_url = list(self.pages_visited)[-1] if self.pages_visited else ""
                self.pages_visited.add(state.url)

                # Screenshot on new page
                if state.url != prev_url:
                    page_name = state.url.split("/")[-1] or "index"
                    await self._take_screenshot(
                        f"page_{page_name}", f"Visited {state.url}"
                    )

                # Drain and process errors from the browser
                console_errs, network_errs = self.browser.drain_errors()
                new_bugs = self.detector.process_console_errors(
                    console_errs, state.url, self.steps
                )
                new_bugs.extend(
                    self.detector.process_network_errors(
                        network_errs, state.url, self.steps
                    )
                )
                # Argus only auto-captures browser-side events that the agent
                # cannot see in page state (console + network). Everything
                # else — content quality, count consistency, CSS feedback,
                # toast/network mismatch, a11y, SEO, perf — is the LLM
                # planner's job to interpret from `state` and decide on.

                # Screenshot and attach to new bugs
                if new_bugs:
                    ss_path = await self._take_screenshot(
                        f"error_{len(self.bugs) + 1}",
                        f"Error detected on {state.url}",
                    )
                    for bug in new_bugs:
                        bug.screenshot_path = ss_path

                self.bugs.extend(new_bugs)

                # Ask the planner for the next action
                try:
                    action = await self.planner.plan_next_action(
                        state=state,
                        focus_areas=self.config.focus_areas,
                        history=self.history,
                        bugs_found=len(self.bugs),
                        steps_remaining=self.config.max_steps - step_num,
                    )
                except Exception as e:
                    console.print(f"  [yellow]Planner error: {e}[/]")
                    msg = str(e).lower()
                    # Auth failures aren't going to fix themselves on
                    # the next iteration — abort immediately and surface
                    # the cause.
                    if any(s in msg for s in (
                        "api key", "api_key", "authentication", "unauthorized",
                        "401", "did not find",
                    )):
                        raise RuntimeError(
                            "LLM auth failed. Set the API key for your "
                            "chosen model (e.g. OPENAI_API_KEY, "
                            "ANTHROPIC_API_KEY, DEEPSEEK_API_KEY) or pass "
                            "--api-key explicitly."
                        ) from e
                    self._consecutive_planner_failures = (
                        getattr(self, "_consecutive_planner_failures", 0) + 1
                    )
                    if self._consecutive_planner_failures >= 3:
                        raise RuntimeError(
                            "Planner failed 3 steps in a row — aborting. "
                            "Last error: " + str(e)[:200]
                        ) from e
                    continue
                else:
                    self._consecutive_planner_failures = 0

                if action.type == ActionType.DONE:
                    console.print(
                        f"  [dim]Step {step_num + 1}:[/] Agent finished exploration"
                    )
                    break

                # Execute the action
                step_desc = self._describe_action(action, state)
                self.steps.append(step_desc)
                self.history.append((state.url, action))

                success = await self._execute_action(action, state)
                status = "[green]+[/]" if success else "[red]x[/]"
                bug_count = (
                    f" [red]({len(self.bugs)} bugs)[/]" if self.bugs else ""
                )
                console.print(
                    f"  [dim]Step {step_num + 1}:[/] {status} {step_desc}{bug_count}"
                )

                # Persistence-verification of delete/edit clicks is the
                # planner's job in CLI mode; the planner is expected to
                # request a refresh-and-compare action when warranted.
                # The MCP-mode `verify_action` tool is the canonical surface;
                # the CLI explorer is intentionally less smart here.

                # Screenshot on failed interaction
                if not success:
                    await self._take_screenshot(
                        f"failed_step_{step_num + 1}",
                        f"Failed: {step_desc}",
                    )

                await asyncio.sleep(0.5)
        finally:
            await self.browser.stop()

        duration = time.time() - start
        result = ExplorationResult(
            url=self.config.url,
            bugs=self.bugs,
            pages_visited=list(self.pages_visited),
            actions_taken=len(self.history),
            duration_seconds=duration,
            focus_areas=[fa.name for fa in self.config.focus_areas],
            screenshots=self.screenshots,
        )

        bug_color = "red" if self.bugs else "green"
        console.print(
            f"\n[bold blue]Done:[/] {len(self.history)} actions, "
            f"{len(self.pages_visited)} pages, "
            f"[{bug_color}]{len(self.bugs)} bugs[/], "
            f"{len(self.screenshots)} screenshots "
            f"in {duration:.1f}s"
        )
        return result

    # -- screenshots --

    async def _take_screenshot(self, name: str, step: str) -> str:
        self._screenshot_counter += 1
        safe_name = f"{self._screenshot_counter:03d}_{name}"
        ss_dir = Path(self.config.output_dir) / "screenshots"
        path = str(ss_dir / f"{safe_name}.png")
        await self.browser.screenshot(path)
        url = self.browser._page.url if self.browser._page else ""
        self.screenshots.append(Screenshot(
            path=path, name=safe_name, step=step, url=url,
        ))
        return path

    # -- action execution --

    async def _execute_action(self, action: Action, state) -> bool:
        try:
            t = action.type
            if t == ActionType.CLICK:
                if (
                    action.element_index is not None
                    and action.element_index < len(state.elements)
                ):
                    return await self.browser.click(
                        action.element_index, state.elements
                    )
            elif t == ActionType.TYPE:
                if (
                    action.element_index is not None
                    and action.value
                    and action.element_index < len(state.elements)
                ):
                    return await self.browser.type_text(
                        action.element_index, action.value, state.elements
                    )
            elif t == ActionType.NAVIGATE:
                if action.url:
                    await self.browser.goto(action.url)
                    return True
            elif t == ActionType.SELECT:
                if (
                    action.element_index is not None
                    and action.value
                    and action.element_index < len(state.elements)
                ):
                    return await self.browser.select_option(
                        action.element_index, action.value, state.elements
                    )
            elif t == ActionType.SCROLL:
                await self.browser.scroll_down()
                return True
            elif t == ActionType.BACK:
                return await self.browser.go_back()
            elif t == ActionType.WAIT:
                await asyncio.sleep(2)
                return True
            return False
        except Exception:
            return False

    # -- human-readable action description --

    @staticmethod
    def _describe_action(action: Action, state) -> str:
        t = action.type
        if t == ActionType.CLICK:
            if (
                action.element_index is not None
                and action.element_index < len(state.elements)
            ):
                el = state.elements[action.element_index]
                label = (
                    el.text
                    or el.aria_label
                    or el.placeholder
                    or f"{el.tag}#{el.id or el.name or '?'}"
                )
                return f'Click "{label}"'
            return "Click (invalid element)"
        elif t == ActionType.TYPE:
            return f'Type "{action.value}" in element [{action.element_index}]'
        elif t == ActionType.NAVIGATE:
            return f"Navigate to {action.url}"
        elif t == ActionType.SELECT:
            return f'Select "{action.value}" in [{action.element_index}]'
        elif t == ActionType.SCROLL:
            return "Scroll down"
        elif t == ActionType.BACK:
            return "Go back"
        elif t == ActionType.WAIT:
            return "Wait"
        return action.type.value


# Import at bottom to avoid circular import
from .planner import Planner  # noqa: E402
