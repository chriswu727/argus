"""Live LLM bench — a *real* model drives Argus's MCP tools.

The scripted bench (`scenarios_*.py`) measures Argus's **capability ceiling**:
"what is findable through this tool surface?" It says nothing about how often a
given LLM, left to its own judgement, actually *remembers* to map → hypothesise
→ act → verify → record. That variance is what this module measures.

The shape:

1. **Drive** — pin a Playwright session to a fixture, hand the model the exact
   same web-mode tools and role-instruction block the MCP server ships, and let
   it test freely until it calls `end_session` (or runs out of turns).
2. **Judge** — the model roamed free, so its bug titles won't keyword-match the
   seeded list. An LLM-as-judge maps each recorded bug onto at most one seeded
   bug, and recall = matched seeded bugs / total seeded bugs.

Everything here is model-agnostic: a `Model` is anything with a `.complete()`
method. `LiteLLMModel` is the real backend; tests inject a scripted fake, so the
whole loop + judge + scoring is exercised without a network call or an API key.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

import argus.mcp_server as mcp_module


# ── Which tools the live agent gets ─────────────────────────────────
# Web-mode, black-box surface — the same tools a senior tester would reach
# for. Deliberately excludes screen_* (different target), start_session (the
# harness pins the URL), and eval_js (the live agent tests from the user's
# side of the screen, not by running JS in the page — that's the whole point
# of the human-eye benchmark).
DEFAULT_WEB_TOOLS: List[str] = [
    "observe",
    "click_what",
    "type_into",
    "select_into",
    "hover_what",
    "right_click",
    "navigate",
    "go_back",
    "scroll_down",
    "inspect_element",
    "screenshot",
    "get_errors",
    "verify_persistence",
    "check_links",
    "check_performance",
    "wait_for_text",
    "test_action",
    "test_form",
    "record_bug",
    "end_session",
]


# ── Model abstraction ───────────────────────────────────────────────


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class Completion:
    content: Optional[str]
    tool_calls: List[ToolCall] = field(default_factory=list)


class Model(Protocol):
    """Anything that can turn (messages, tools) into a Completion.

    `name` is used only for reporting. `complete` is synchronous — the driver
    awaits tool dispatch around it, but a single model turn is one blocking
    call, which keeps the fake (and the litellm backend) trivial.
    """

    name: str

    def complete(self, messages: List[dict], tools: List[dict]) -> Completion:
        ...


class LiteLLMModel:
    """Real backend. Routes through litellm, so any provider litellm knows
    (anthropic/…, openai/…, etc.) works as long as its key is in the env."""

    def __init__(self, name: str, temperature: float = 0.0, max_tokens: int = 4096):
        self.name = name
        self.temperature = temperature
        self.max_tokens = max_tokens

    def complete(self, messages: List[dict], tools: List[dict]) -> Completion:
        import litellm  # lazy — importing the bench package shouldn't need it

        resp = litellm.completion(
            model=self.name,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        msg = resp.choices[0].message
        calls: List[ToolCall] = []
        for tc in (getattr(msg, "tool_calls", None) or []):
            raw = tc.function.arguments
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw or "{}")
                except json.JSONDecodeError:
                    raw = {}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=raw or {}))
        return Completion(content=getattr(msg, "content", None), tool_calls=calls)


# ── Tool surface plumbing ───────────────────────────────────────────


def _tool_index() -> Dict[str, Any]:
    return {t.name: t for t in mcp_module.mcp._tool_manager.list_tools()}


def build_tool_schemas(allowlist: List[str]) -> List[dict]:
    """Render the allow-listed MCP tools as OpenAI/litellm function schemas,
    straight from FastMCP's own introspected parameter schemas."""
    idx = _tool_index()
    schemas = []
    for name in allowlist:
        t = idx.get(name)
        if t is None:
            continue
        schemas.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": (t.description or "").strip(),
                "parameters": t.parameters,
            },
        })
    return schemas


async def dispatch(name: str, arguments: Dict[str, Any]) -> str:
    """Invoke one MCP tool by name and return a string result the model can
    read. Tool errors are returned as text, not raised — a real tester sees a
    failed action and adapts; a crashed harness teaches nothing."""
    t = _tool_index().get(name)
    if t is None:
        return f"error: unknown tool {name!r}"
    try:
        res = await t.fn(**(arguments or {}))
    except Exception as exc:  # surface to the model, keep the run alive
        return f"error calling {name}: {exc!r}"
    if isinstance(res, str):
        return res
    try:
        return json.dumps(res)[:12000]
    except (TypeError, ValueError):
        return str(res)[:12000]


# ── Driver ──────────────────────────────────────────────────────────


@dataclass
class LiveRun:
    bugs: list  # List[argus.models.Bug] recorded during the run
    turns: int
    tool_calls: int
    stop_reason: str  # "end_session" | "no_tool_calls" | "max_turns" | "error"
    transcript: List[dict] = field(default_factory=list)


_BENCH_PREAMBLE = (
    "A web app is already open in your testing session at {url}. You do not "
    "need to start a session — begin with observe(). Test the app like a "
    "senior human QA tester: walk real user journeys, hypothesise failures, "
    "verify persistence, and call record_bug for each distinct, real bug you "
    "confirm. When you have covered the app's main flows, call end_session. "
    "Find as many real, distinct bugs as you can — quality over quantity."
)


async def drive(
    base_url: str,
    model: Model,
    max_turns: int = 40,
    tools: Optional[List[str]] = None,
) -> LiveRun:
    """Pin a session to `base_url`, then let `model` test freely.

    Returns the bugs the model recorded, regardless of whether it ended the
    session itself — `end_session` resets the global session but finalises the
    held session's `.bugs` first, so the reference we grab after start_session
    stays valid as the source of truth.
    """
    allow = tools or DEFAULT_WEB_TOOLS
    schemas = build_tool_schemas(allow)

    start = mcp_module.start_session
    start_fn = getattr(start, "fn", start)
    launch = await start_fn(base_url)
    if not mcp_module._session.active:
        raise RuntimeError(f"could not start session on {base_url}: {launch}")
    s = mcp_module._session  # held reference — survives end_session's reset

    system = (mcp_module.mcp.instructions or "") + "\n\n" + _BENCH_PREAMBLE.format(url=base_url)
    messages: List[dict] = [{"role": "system", "content": system}]

    turns = 0
    tool_calls = 0
    stop_reason = "max_turns"

    try:
        while turns < max_turns:
            turns += 1
            comp = model.complete(messages, schemas)

            assistant: dict = {"role": "assistant", "content": comp.content or ""}
            if comp.tool_calls:
                assistant["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in comp.tool_calls
                ]
            messages.append(assistant)

            if not comp.tool_calls:
                stop_reason = "no_tool_calls"
                break

            ended = False
            for tc in comp.tool_calls:
                tool_calls += 1
                if tc.name == "end_session":
                    # Finalise the report ourselves so bugs are flushed, then
                    # stop — further calls would hit a dead session.
                    result = await dispatch("end_session", {})
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                    ended = True
                    stop_reason = "end_session"
                    break
                result = await dispatch(tc.name, tc.arguments)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            if ended:
                break
    finally:
        # If the model never ended the session, close it so the browser frees
        # and the HTML report still gets written.
        if mcp_module._session.active:
            end = mcp_module.end_session
            await getattr(end, "fn", end)()

    return LiveRun(
        bugs=list(s.bugs),
        turns=turns,
        tool_calls=tool_calls,
        stop_reason=stop_reason,
        transcript=messages,
    )


# ── Seeded-bug specs (reused from the scripted scenarios) ───────────


def seeded_specs(scenarios: List[Tuple[int, str, Callable]]) -> List[dict]:
    """Derive the canonical seeded-bug list from the scripted scenarios — no
    duplicated source of truth. Each scenario's docstring leads with a
    `BUG #N: <one-liner>`, which is the description we hand the judge."""
    specs = []
    for bug_id, name, fn in scenarios:
        doc = (fn.__doc__ or "").strip()
        # Strip the leading "BUG #N:" marker; keep the human description.
        desc = doc
        if ":" in doc.split("\n", 1)[0]:
            desc = doc.split(":", 1)[1].strip()
        specs.append({"id": bug_id, "name": name, "description": desc})
    return specs


# ── LLM-as-judge ────────────────────────────────────────────────────


_JUDGE_SYSTEM = (
    "You are a strict, fair benchmark judge for a QA-testing agent. You are "
    "given a list of SEEDED bugs (the ground truth intentionally planted in a "
    "test app) and a list of bugs the agent REPORTED. Decide which seeded bugs "
    "the agent genuinely found.\n\n"
    "Rules:\n"
    "- A reported bug matches a seeded bug only if it describes the SAME "
    "underlying defect. Paraphrase is fine; a different defect is not.\n"
    "- Each reported bug matches AT MOST ONE seeded bug. Each seeded bug is "
    "matched AT MOST ONCE (by its best reported bug).\n"
    "- Do not give credit for vague or speculative reports that merely "
    "overlap in topic.\n"
    "Respond with ONLY a JSON object, no prose, of the form:\n"
    '{"matches": [{"seeded_id": <int>, "reported_index": <int>}]}'
)


@dataclass
class JudgeResult:
    matched_seeded_ids: List[int]
    matches: List[dict]  # [{"seeded_id", "reported_index"}]
    raw: str = ""


def _parse_json_object(text: str) -> dict:
    """Pull the first JSON object out of a model reply, tolerating code fences
    and surrounding prose."""
    if not text:
        return {}
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.strip("`")
        if t.lstrip().startswith("json"):
            t = t.lstrip()[4:]
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return {}


def judge_recall(model: Model, seeded: List[dict], reported: list) -> JudgeResult:
    """Ask the model which seeded bugs the agent's reported bugs cover.

    `reported` is a list of Bug objects (title / description / severity)."""
    seeded_block = "\n".join(
        f"  [{spec['id']}] {spec['name']} — {spec['description']}" for spec in seeded
    )
    reported_block = "\n".join(
        f"  ({i}) [{b.severity}] {b.title} — {(b.description or '').strip()[:300]}"
        for i, b in enumerate(reported)
    ) or "  (none reported)"

    user = (
        f"SEEDED bugs (ground truth):\n{seeded_block}\n\n"
        f"REPORTED bugs (by the agent):\n{reported_block}\n\n"
        "Return the JSON object of matches now."
    )
    comp = model.complete(
        [{"role": "system", "content": _JUDGE_SYSTEM}, {"role": "user", "content": user}],
        tools=[],
    )
    obj = _parse_json_object(comp.content or "")

    seeded_ids = {spec["id"] for spec in seeded}
    n_reported = len(reported)
    seen_seeded: set = set()
    seen_reported: set = set()
    clean: List[dict] = []
    for m in obj.get("matches", []):
        try:
            sid = int(m["seeded_id"])
            ridx = int(m["reported_index"])
        except (KeyError, TypeError, ValueError):
            continue
        # Enforce the one-to-one contract defensively, in case the judge slips.
        if sid not in seeded_ids or not (0 <= ridx < n_reported):
            continue
        if sid in seen_seeded or ridx in seen_reported:
            continue
        seen_seeded.add(sid)
        seen_reported.add(ridx)
        clean.append({"seeded_id": sid, "reported_index": ridx})

    return JudgeResult(
        matched_seeded_ids=sorted(seen_seeded),
        matches=clean,
        raw=comp.content or "",
    )


# ── Report ──────────────────────────────────────────────────────────


@dataclass
class LiveReport:
    target: str
    model: str
    fixture_url: str
    seeded: List[dict]
    run: LiveRun
    judge: JudgeResult
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def caught(self) -> int:
        return len(self.judge.matched_seeded_ids)

    @property
    def total(self) -> int:
        return len(self.seeded)

    @property
    def recall(self) -> float:
        return (self.caught / self.total) if self.total else 0.0

    @property
    def extra_reported(self) -> int:
        """Reported bugs the judge did not map to any seeded bug — the agent's
        own finds (could be true extra bugs or false positives)."""
        return max(0, len(self.run.bugs) - len(self.judge.matches))

    def to_json(self) -> dict:
        matched = set(self.judge.matched_seeded_ids)
        return {
            "mode": "live",
            "target": self.target,
            "model": self.model,
            "fixture": self.fixture_url,
            "duration_s": round(self.finished_at - self.started_at, 2),
            "turns": self.run.turns,
            "tool_calls": self.run.tool_calls,
            "stop_reason": self.run.stop_reason,
            "caught": self.caught,
            "total": self.total,
            "recall_pct": round(self.recall * 100, 1),
            "reported_total": len(self.run.bugs),
            "extra_reported": self.extra_reported,
            "seeded": [
                {"id": spec["id"], "name": spec["name"], "caught": spec["id"] in matched}
                for spec in self.seeded
            ],
            "reported_bugs": [
                {"title": b.title, "severity": b.severity} for b in self.run.bugs
            ],
        }

    def to_markdown(self) -> str:
        matched = set(self.judge.matched_seeded_ids)
        lines = [
            f"# Argus live bench — {self.target}",
            "",
            f"- Model: `{self.model}`",
            f"- Fixture: `{self.fixture_url}`",
            f"- Duration: {self.finished_at - self.started_at:.1f} s "
            f"({self.run.turns} turns, {self.run.tool_calls} tool calls, "
            f"stop: {self.run.stop_reason})",
            f"- **Recall: {self.caught} / {self.total} = {self.recall * 100:.0f} %**",
            f"- Reported {len(self.run.bugs)} bug(s); "
            f"{self.extra_reported} not mapped to a seeded bug",
            "",
            "| #  | Seeded bug                                              | Found |",
            "|----|---------------------------------------------------------|-------|",
        ]
        for spec in self.seeded:
            mark = "yes" if spec["id"] in matched else "no"
            lines.append(f"| {spec['id']:>2} | {spec['name'][:55]:<55} | {mark:<5} |")
        return "\n".join(lines)
