from __future__ import annotations

import json
from typing import List, Tuple

import litellm

from .config import FocusArea
from .models import Action, ActionType, InteractiveElement, PageState

# Suppress litellm's noisy logging
litellm.suppress_debug_info = True


def _format_elements(elements: List[InteractiveElement]) -> str:
    lines = []
    for el in elements:
        parts = [f"[{el.index}]", f"<{el.tag}"]
        if el.type:
            parts.append(f'type="{el.type}"')
        parts.append(">")
        if el.text:
            parts.append(f'"{el.text}"')
        if el.placeholder:
            parts.append(f'(placeholder: "{el.placeholder}")')
        if el.href:
            parts.append(f"-> {el.href}")
        if el.disabled:
            parts.append("[disabled]")
        if el.value:
            parts.append(f'value="{el.value}"')
        lines.append(" ".join(parts))
    return "\n".join(lines) if lines else "(no interactive elements found)"


def _format_history(history: List[Tuple[str, Action]]) -> str:
    if not history:
        return "(no actions taken yet)"
    lines = []
    for i, (url, action) in enumerate(history[-20:], 1):
        desc = f"{i}. [{url}] {action.type.value}"
        if action.value:
            desc += f' "{action.value}"'
        if action.element_index is not None:
            desc += f" on element [{action.element_index}]"
        lines.append(desc)
    return "\n".join(lines)


SYSTEM_PROMPT = """You are an expert QA tester performing exploratory testing on a web application.
Your goal is to discover bugs, crashes, and edge-case failures that automated scripts would miss.
Think like a real user — but also like a security researcher and a chaos engineer.

TESTING STRATEGIES you must apply systematically:

1. FORM TESTING — For every form, try ALL of these inputs:
   - Empty submission (click submit with nothing filled)
   - Valid data first to understand happy path
   - Invalid email formats: "notanemail", "a@", "@b.com"
   - Password edge cases: single char "a", empty, 200+ chars "a"*200
   - Special characters: < > " ' & ; | \\ / { } and unicode 中文 🔥
   - SQL injection: ' OR 1=1 -- and "; DROP TABLE users; --
   - XSS payloads: <script>alert('xss')</script> and <img onerror=alert(1) src=x>
   - Very long strings: 500+ character inputs
   - Mismatched confirm fields: different passwords in password/confirm
   - Negative/zero/huge numbers for numeric fields
   - Whitespace-only input: "   "

2. NAVIGATION TESTING:
   - Click every link in the navigation
   - Check for dead links (404 pages)
   - Try accessing pages directly by URL manipulation
   - Try accessing authenticated-only pages without login
   - Test browser back/forward behavior

3. BUTTON/INTERACTION TESTING:
   - Click every button on every page
   - Try actions in wrong order (submit before filling form)
   - Try the same action multiple times

4. AUTH TESTING:
   - Try logging in with wrong credentials
   - Try logging in with empty fields
   - Try accessing protected pages without auth
   - Try registering with existing usernames/emails

5. STATE & FLOW TESTING:
   - Complete full user flows end-to-end
   - Interrupt flows mid-way (navigate away during multi-step process)
   - Test what happens after successful/failed operations

6. VERIFICATION TESTING:
   - After deleting an item: refresh the page and check if it's really gone
   - After editing: reload and verify the new value persists
   - After seeing a success toast: be suspicious — check if the change actually happened
   - Compare displayed counts (e.g., "7 Total Tasks") against actual items on the page

7. CONTENT ANALYSIS:
   - Look at displayed counts and compare to actual items visible
   - Check for "Loading..." text that never resolves
   - Check for NaN or broken date formatting (e.g., "1.52 days ago")
   - Check if items with empty/whitespace-only titles exist
   - Check if success/error styling is appropriate for the state"""


class Planner:
    """Uses an LLM to decide the next exploratory action.

    Powered by LiteLLM — supports any provider:
      - OpenAI:    model="gpt-4o"           (needs OPENAI_API_KEY)
      - Anthropic: model="claude-sonnet-4-20250514" (needs ANTHROPIC_API_KEY)
      - DeepSeek:  model="deepseek/deepseek-chat"  (needs DEEPSEEK_API_KEY)
      - Gemini:    model="gemini/gemini-2.5-flash"    (needs GEMINI_API_KEY)
      - Ollama:    model="ollama/llama3"     (needs local Ollama running)
      - etc.
    """

    def __init__(self, model: str = "gpt-4o-mini", api_base: str = None, api_key: str = None):
        self.model = model
        self.api_base = api_base
        self.api_key = api_key

    async def plan_next_action(
        self,
        state: PageState,
        focus_areas: List[FocusArea],
        history: List[Tuple[str, Action]],
        bugs_found: int,
        steps_remaining: int,
    ) -> Action:
        focus_text = "\n".join(
            f"- {fa.name}: {fa.description}"
            + (f" (paths: {', '.join(fa.paths)})" if fa.paths else "")
            + (f" (try: {', '.join(fa.actions)})" if fa.actions else "")
            for fa in focus_areas
        ) if focus_areas else "No specific focus — explore the application broadly and test everything."

        prompt = f"""FOCUS AREAS:
{focus_text}

CURRENT PAGE:
URL: {state.url}
Title: {state.title}

INTERACTIVE ELEMENTS:
{_format_elements(state.elements)}

PAGE TEXT (first 500 chars):
{state.page_text[:500] if state.page_text else "(not extracted)"}

VISIBLE TOASTS: {', '.join(state.toast_messages) if state.toast_messages else "(none)"}

DISPLAYED COUNTS: {', '.join(f'{v} {k}' for k, v in state.counts.items()) if state.counts else "(none)"}

ACTION HISTORY (last 20):
{_format_history(history)}

STATS: {bugs_found} bugs found | {steps_remaining} steps remaining

VISITED PAGES (DO NOT navigate to these again unless testing a form on them):
{chr(10).join(set(url for url, _ in history)) if history else "(none yet)"}

RULES:
- Prioritize the focus areas first, then explore other parts.
- Apply ALL testing strategies from your instructions systematically.
- For forms: try empty submission first, then valid data, then each edge case type.
- Test every link in navigation — check for dead links.
- Click every button — even decorative ones might have broken handlers.
- Try XSS payloads, SQL injection, and special characters in EVERY text input.
- Try very long strings (500+ chars) in inputs to test length limits.
- If you find a login/register form, test with various invalid inputs.
- After submitting forms, check if the page handles errors gracefully.
- Don't repeat the exact same action (same type + same element + same value) more than once.
- Visit ALL pages accessible from navigation before finishing.
- When steps are running low or everything seems thoroughly covered, return "done".

Respond with ONLY a JSON object:
{{"reasoning": "...", "type": "click|type|navigate|select|scroll|back|done", "element_index": null, "value": null, "url": null}}"""

        kwargs = {
            "model": self.model,
            "max_tokens": 300,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key
        response = await litellm.acompletion(**kwargs)

        text = response.choices[0].message.content.strip()
        # Strip markdown code fences
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        # Robust JSON extraction — find first {...} block
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            text = text[start:end + 1]

        data = json.loads(text)
        return Action(
            type=ActionType(data["type"]),
            reasoning=data.get("reasoning", ""),
            element_index=data.get("element_index"),
            value=data.get("value"),
            url=data.get("url"),
        )
