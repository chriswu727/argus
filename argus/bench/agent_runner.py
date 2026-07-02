"""Real-LLM bench — an ACTUAL model drives the Argus tools against a seeded
fixture, so we measure the recall / precision of a real agent, not the scripted
tool ceiling. The scripted bench (`python -m argus.bench`) proves a bug is
*findable* with these tools (its 22/22 is a capability ceiling); this proves how
much a given model *actually* finds when it drives them itself.

Usage:
    # BuggyTasks fixture up: cd test-site && python app.py            (:5555)
    # or DarkShop:           cd human-eye-fixture && python app.py     (:5556)
    export DEEPSEEK_API_KEY=...            # or any LiteLLM-supported provider key
    python -m argus.bench.agent_runner
    # env: BENCH_MODEL, BENCH_TRIALS, BENCH_MAX_STEPS, BENCH_COST_CAP_USD,
    #      BENCH_BASE_URL (any target), BENCH_CATALOG (buggytasks|darkshop)

Honest caveats printed with the result: recall is a fuzzy keyword estimate
against the selected catalog's seeded signatures; a hard USD cost cap bounds
the run and ALL trials are reported (no cherry-picking).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request

import argus.mcp_server as mcp

BASE = os.environ.get("BENCH_BASE_URL", "http://127.0.0.1:5555")


def _t(name, desc, props=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {}, "required": required or []}}}


TOOLS = [
    _t("start_session", "Start a browser session at a URL.", {"url": {"type": "string"}}, ["url"]),
    _t("observe", "Observe the current page (elements, text, feedback). Read after every action."),
    _t("click_what", "Click the element matching a natural-language description.",
       {"description": {"type": "string"}}, ["description"]),
    _t("type_into", "Type text into the field matching a description.",
       {"description": {"type": "string"}, "text": {"type": "string"}}, ["description", "text"]),
    _t("select_into", "Select a value in the dropdown matching a description.",
       {"description": {"type": "string"}, "value": {"type": "string"}}, ["description", "value"]),
    _t("navigate", "Navigate to a URL.", {"url": {"type": "string"}}, ["url"]),
    _t("scroll_down", "Scroll the page down."),
    _t("verify_persistence", "Force a fresh GET; report whether target_text is present/absent.",
       {"expect": {"type": "string", "enum": ["present", "absent"]},
        "target_text": {"type": "string"}, "after_url": {"type": "string"}}, ["expect", "target_text"]),
    _t("get_errors", "Drain captured console + network events."),
    _t("check_links", "Check internal links for dead ones."),
    _t("record_bug", "Record a CONFIRMED bug. Pass a verify clause "
       "({expect, target_text, at_url}) to attach a reproduction receipt.",
       {"title": {"type": "string"}, "severity": {"type": "string"},
        "evidence": {"type": "object"},
        "verify": {"type": "object", "properties": {
            "expect": {"type": "string"}, "target_text": {"type": "string"}, "at_url": {"type": "string"}}}},
       ["title", "severity"]),
    _t("end_session", "End the session and write the report."),
]

# Fuzzy keyword signatures for the 22 seeded BuggyTasks bugs (recall estimate).
_BUGGYTASKS_CATALOG = {
    1: ["appconfig", "referenceerror"], 2: ["/help", "dead link", "404"], 3: ["newsletter", "500"],
    4: ["any cred", "auth bypass", "accepts any", "wrong password"], 5: ["mismatch", "password"],
    6: ["form", "cleared", "data lost"], 7: ["xss", "script", "reflect"], 8: ["double", "duplicate"],
    9: ["count", "off-by-one", "off by one"], 10: ["delete", "still present", "fake delete"],
    11: ["edit", "not updated", "silent"], 12: ["toggle", "race"], 13: ["load more", "pagination"],
    14: ["loading", "forever", "spinner"], 15: ["case", "search"], 16: ["date", "1.0 days", "decimal"],
    17: ["saved", "false success", "not persist", "toast lie"], 18: ["truncat", "long title"],
    19: ["priority", "arbitrary", "unbounded"],
    20: ["navbar", "still shows login", "after auth"], 21: ["whitespace", "empty task"],
    22: ["0 tasks", "alarming", "remaining"],
}

# The 12 human-eye DarkShop bugs (visual / dark-pattern / deceptive feedback).
_DARKSHOP_CATALOG = {
    1: ["only 3 left", "scarcity", "hardcoded", "stock badge", "3 left"],
    2: ["50%", "sale badge", "fake sale", "discount is a lie", "same price", "original price"],
    3: ["free shipping", "shipping fee", "$5", "flat shipping"],
    4: ["add to cart", "faded", "grey", "gray", "looks disabled", "low contrast"],
    5: ["rating", "star", "4.8", "fake review"],
    6: ["cvv", "card number", "field order", "before card"],
    7: ["subtotal", "pre-tax", "tax", "order summary"],
    8: ["place order", "grey link", "small", "hard to find", "corner"],
    9: ["legal", "asterisk", "consent", "required checkbox"],
    10: ["display name", "profile", "not saved", "silent", "account"],
    11: ["cart count", "badge", "navbar", "stale", "cart"],
    12: ["save10", "discount code", "promo", "clears", "coupon"],
}

_CATALOGS = {"buggytasks": _BUGGYTASKS_CATALOG, "darkshop": _DARKSHOP_CATALOG}
CATALOG = _CATALOGS.get(os.environ.get("BENCH_CATALOG", "buggytasks").lower(), _BUGGYTASKS_CATALOG)
_N = len(CATALOG)

_SYSTEM = mcp.mcp.instructions
_USER = (f"The app under test is at {BASE}. Call start_session with that URL, then USE the app like a "
         "real person — sign up / log in, add and manage tasks, search, change settings — and find bugs. "
         "When you confirm a real bug, call record_bug (attach a verify clause when the symptom is "
         "text-checkable). Call end_session when you've done a thorough pass.")


async def _dispatch(name, args):
    fn = getattr(mcp, name, None)
    if fn is None:
        return f"(no such tool {name})"
    fn = getattr(fn, "fn", fn)
    try:
        return str(await fn(**(args or {})))
    except Exception as e:
        return f"ERROR {name}: {type(e).__name__}: {e}"


def _reset():
    try:
        urllib.request.urlopen(urllib.request.Request(
            BASE + "/api/test/reset?mode=seeded", method="POST"), timeout=5).read()
    except Exception as e:
        print("reset warn:", e, flush=True)


async def run_session(model, max_steps, cost_cap, cost_so_far):
    import litellm
    litellm.suppress_debug_info = True
    msgs = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": _USER}]
    cost = 0.0
    for step in range(max_steps):
        if cost_so_far + cost > cost_cap:
            print(f"  [budget stop at step {step}]", flush=True)
            break
        try:
            resp = litellm.completion(model=model, messages=msgs, tools=TOOLS,
                                      tool_choice="auto", temperature=0.4, max_tokens=900)
        except Exception as e:
            print("  completion err:", str(e)[:200], flush=True)
            break
        try:
            cost += litellm.completion_cost(completion_response=resp) or 0.0
        except Exception:
            pass
        msg = resp.choices[0].message
        msgs.append(msg.model_dump(exclude_none=True) if hasattr(msg, "model_dump") else dict(msg))
        tcs = msg.tool_calls or []
        if not tcs:
            msgs.append({"role": "user", "content": "Call a tool (observe / click_what / record_bug / end_session)."})
            continue
        ended = False
        for tc in tcs:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            result = await _dispatch(name, args)
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": result[:3000]})
            print(f"  step{step}: {name}({str(args)[:55]}) -> {result[:60].strip()}", flush=True)
            if name == "end_session":
                ended = True
        if ended:
            break
    return cost


def score(bugs):
    hay = [((b.title or "") + " " + (b.description or "")).lower() for b in bugs]
    def _matched(h):
        return any(any(k in h for k in kws) for kws in CATALOG.values())
    caught = {bid for bid, kws in CATALOG.items() if any(any(k in h for k in kws) for h in hay)}
    verified = sum(1 for b in bugs if (b.reproduction_receipt or {}).get("reproduced") is True)
    unmatched = [b for b, h in zip(bugs, hay) if not _matched(h)]
    # A VERIFIED finding passed an independent clean-load re-check: it's a real
    # bug (possibly just off our fuzzy catalog), NOT a false positive. Only an
    # UNVERIFIED unmatched finding is a genuine FP candidate.
    fp_candidates = sum(1 for b in unmatched if (b.reproduction_receipt or {}).get("reproduced") is not True)
    return {"recorded": len(bugs), "recall": len(caught), "caught_ids": sorted(caught),
            "unmatched": len(unmatched), "fp_candidates": fp_candidates, "verified": verified}


async def main():
    if not any(os.environ.get(k) for k in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")):
        print("Set a provider key (e.g. DEEPSEEK_API_KEY).", flush=True)
        return 2
    model = os.environ.get("BENCH_MODEL", "deepseek/deepseek-chat")
    trials = int(os.environ.get("BENCH_TRIALS", "3"))
    max_steps = int(os.environ.get("BENCH_MAX_STEPS", "40"))
    cost_cap = float(os.environ.get("BENCH_COST_CAP_USD", "3.0"))
    # Prefer the seeded fixture's control endpoint (enables per-trial reset and
    # meaningful recall scoring), but fall back to the plain root so the bench
    # can drive ANY app — DarkShop, or a real target — for footgun-mining.
    seeded = True
    try:
        urllib.request.urlopen(BASE + "/api/test/state", timeout=3).read()
    except Exception:
        seeded = False
        try:
            urllib.request.urlopen(BASE, timeout=3).read()
        except Exception as e:
            print(f"Target not reachable at {BASE} ({e}). Start the fixture/app first.", flush=True)
            return 2
    if not seeded:
        cat_name = os.environ.get("BENCH_CATALOG", "buggytasks").lower()
        print(f"[{BASE}: no reset endpoint — state carries across trials. Recall scored "
              f"against the '{cat_name}' catalog ({_N} bugs); set BENCH_CATALOG to match "
              "the target, or read the run for footguns.]", flush=True)

    results, cost_total = [], 0.0
    for i in range(trials):
        if cost_total > cost_cap:
            print(f"[cost cap ${cost_cap} reached before trial {i+1}]", flush=True)
            break
        if seeded:
            _reset()
        print(f"\n=== trial {i+1}/{trials} ({model}) — spent ${cost_total:.4f} ===", flush=True)
        cost_total += await run_session(model, max_steps, cost_cap, cost_total)
        s = score(list(getattr(mcp._session, "bugs", [])))
        results.append(s)
        print(f"  -> recall {s['recall']}/{_N}, recorded {s['recorded']} "
              f"(verified {s['verified']}, off-catalog {s['unmatched']}, "
              f"FP-candidates {s['fp_candidates']})", flush=True)
        try:
            await (mcp.end_session.fn if hasattr(mcp.end_session, "fn") else mcp.end_session)()
        except Exception:
            pass

    if results:
        recalls = [r["recall"] for r in results]
        mean = sum(recalls) / len(recalls)
        var = sum((x - mean) ** 2 for x in recalls) / len(recalls)
        print(f"\n===== REAL-LLM BENCH — {model} =====", flush=True)
        for i, r in enumerate(results, 1):
            print(f"  trial {i}: recall {r['recall']}/{_N}  recorded {r['recorded']}  "
                  f"verified {r['verified']}  off-catalog {r['unmatched']}  "
                  f"FP-candidates {r['fp_candidates']}", flush=True)
        fp_total = sum(r["fp_candidates"] for r in results)
        print(f"  mean recall {mean:.1f}/{_N}  variance {var:.2f}  FP-candidates {fp_total}  "
              f"total cost ${cost_total:.4f} (~RMB {cost_total * 7.2:.2f})", flush=True)
        print(f"  recall = fuzzy keyword estimate vs the {_N} seeded signatures; verified = findings "
              "that carried a passing reproduction receipt; off-catalog = recorded bugs matching no "
              "seeded signature; FP-candidates = off-catalog AND unverified (a verified off-catalog "
              "finding is a real bug, not a false positive).", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
