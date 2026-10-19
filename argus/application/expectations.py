from __future__ import annotations

from argus.domain.models import (
    PageState,
)
from argus.security import _redact

from .text import _token_present

_EXPECT_KEYS = {"count", "gains", "removes", "text_present", "text_absent", "toast", "url_changed"}


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y")
    return bool(v)


def _evaluate_expectation(before: PageState, after: PageState, expect: dict) -> list:
    """Predict-then-check: evaluate a bounded predicate dict against the
    before/after diff. Returns [(label, ok, detail)] where ok is:
      True  = prediction held, False = SURPRISE (a bug lead),
      None  = could not evaluate (unmeasurable) — NEVER a surprise.

    The vocabulary is deliberately small — only the deltas the page state
    already exposes — and judged HONESTLY so neither a false MATCH nor a false
    SURPRISE can mislead the tester:
      - gains/removes use set membership across whole before/after row sets
        (a token present after but not before = a real add), so an in-place
        row edit (priority/timestamp change) is NOT mistaken for an add+remove.
      - text checks use VISIBLE text only (never an input value) and require
        the text to have APPEARED (text_present), not merely pre-exist.
      - an unmeasurable count (label not on the page) or an unknown key is
        reported as UNCHECKED, not as a failed prediction.
    Token matching is substring-on-word-boundary, so pass a distinctive item
    text to avoid bleed onto a sibling row.
    """
    results = []

    def _as_list(x):
        return [x] if isinstance(x, str) else list(x or [])

    def _in(item, state):
        if _token_present(item, state.page_text):
            return True
        for el in state.elements:
            if el.text and _token_present(item, el.text):
                return True
        for items in state.item_lists.values():
            for row in items:
                if _token_present(item, row):
                    return True
        return False

    c = expect.get("count")
    if c is not None:
        if not (isinstance(c, dict) and c.get("label")):
            results.append(("count (malformed — need {label, delta|value})", None, repr(c)[:60]))
        else:
            lab = c["label"]
            b, a = before.counts.get(lab), after.counts.get(lab)
            if a is None and b is None:
                results.append(
                    (f"count {lab!r}", None, "label not among page counts — could not evaluate")
                )
            elif "delta" in c:
                try:
                    want = int(c["delta"])
                    actual = (a or 0) - (b or 0)
                    results.append(
                        (f"count {lab!r} change {want:+d}", actual == want, f"{b} -> {a}")
                    )
                except (TypeError, ValueError):
                    results.append(
                        (f"count {lab!r} delta", None, f"non-numeric delta {c['delta']!r}")
                    )
            elif "value" in c:
                results.append((f"count {lab!r} == {c['value']}", a == c["value"], f"observed {a}"))
            else:
                results.append((f"count {lab!r} (malformed — need delta or value)", None, ""))

    for item in _as_list(expect.get("gains")):
        after_has, before_has = _in(item, after), _in(item, before)
        ok = after_has and not before_has
        detail = (
            "newly present"
            if ok
            else ("already present before action" if before_has else "not present after")
        )
        results.append((f"list gains {item!r}", ok, detail))
    for item in _as_list(expect.get("removes")):
        after_has, before_has = _in(item, after), _in(item, before)
        ok = before_has and not after_has
        detail = (
            "gone now"
            if ok
            else ("STILL present" if after_has else "was not present before action")
        )
        results.append((f"list removes {item!r}", ok, detail))

    if "text_present" in expect:
        t = expect["text_present"]
        ok = _in(t, after) and not _in(t, before)
        detail = (
            "appeared"
            if ok
            else ("already there before action" if _in(t, before) else "not visible after")
        )
        results.append((f"text appears {t!r}", ok, detail))
    if "text_absent" in expect:
        t = expect["text_absent"]
        ok = not _in(t, after)
        results.append((f"text absent {t!r}", ok, "absent" if ok else "STILL visible"))
    if "toast" in expect:
        t = expect["toast"]
        new_toasts = set(after.toast_messages) - set(before.toast_messages)
        ok = any(_token_present(t, tm) for tm in new_toasts)
        results.append((f"toast {t!r}", ok, "; ".join(list(new_toasts)[:2]) or "no new toast"))
    if "url_changed" in expect:
        want = _as_bool(expect["url_changed"])
        changed = before.url != after.url
        results.append((f"url changed == {want}", changed == want, f"{before.url} -> {after.url}"))

    for k in expect:
        if k not in _EXPECT_KEYS:
            results.append((f"unknown key {k!r}", None, "not a recognised predicate — ignored"))

    return results


_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _reconcile_action(new_reqs: list, before: PageState, after: PageState) -> tuple:
    """Surface the network a click produced as EVIDENCE for the agent to judge —
    deliberately NOT an auto-verdict. Returns (evidence_lines, check_or_None).

    Argus does not assert "deceptive success" from heuristics: request->action
    attribution is fuzzy (background polls / telemetry land in the same window),
    "no network write" is legitimate for optimistic UI and client-side/offline
    persistence, and success/failure copy is open-vocabulary. So it shows what
    fired (methods, statuses, errors) and, when a message appeared, nudges the
    agent to CONFIRM the claim persisted — the reliable oracle is a fresh
    reload, not the wire. This is the seam pure-FE tests (never see persistence)
    and pure-BE tests (never see the UI claim) both miss; the agent makes the
    call.
    """
    mutating = [r for r in new_reqs if (r.get("method") or "").upper() in _MUTATING_METHODS]
    failed = [r for r in new_reqs if (r.get("status") or 0) >= 400]
    new_toasts = set(after.toast_messages) - set(before.toast_messages)

    evidence = [f"Requests fired by this action: {len(new_reqs)} ({len(mutating)} mutating)"]
    for r in new_reqs[:6]:
        st = r.get("status")
        evidence.append(
            f"  {(r.get('method') or '?'):6} "
            f"{(str(st) if st is not None else 'pending'):>7}  {_redact(r.get('url') or '')[:90]}"
        )
    if len(new_reqs) > 6:
        evidence.append(f"  (+{len(new_reqs) - 6} more)")
    for r in failed:
        evidence.append(
            f"  error: {r.get('method')} {_redact(r.get('url') or '')[:70]} -> HTTP {r.get('status')}"
        )

    check = None
    if new_toasts:
        names = "; ".join(list(new_toasts)[:2])[:80]
        if not mutating:
            check = (
                f"a message appeared ({names!r}) but this click fired NO mutating request. "
                "If it claims a saved/changed state, confirm it actually persisted on a fresh "
                "reload (verify_persistence) — optimistic UI / client-side persistence are "
                "legitimate, a lying toast is not."
            )
        elif failed:
            check = (
                f"a message appeared ({names!r}) and a request this click fired returned an "
                "error (above). If the message claims success, that may be success copy over a "
                "failed write — read the response body (network_request) or reload to confirm."
            )
    return evidence, check
