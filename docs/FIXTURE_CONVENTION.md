# Argus fixture convention

Argus benchmarks against **fixtures**: small web apps seeded with known,
intentional bugs. A fixture is just a normal web app with two extra HTTP
endpoints that let a test driver read and reset the app's state out-of-band —
i.e. without having to navigate the (deliberately buggy) UI to get there.

This document is the spec. If your app implements it, `python -m argus.bench`
can drive it, and the bench runner's health-check will recognise it.

> The two checked-in fixtures both implement this convention:
> [`test-site/app.py`](../test-site/app.py) (BuggyTasks) and
> [`human-eye-fixture/app.py`](../human-eye-fixture/app.py) (DarkShop).

---

## Why these endpoints exist

Some seeded bugs only manifest in states the buggy UI flow can't reliably
reach. Example: BuggyTasks BUG #14 ("empty list says *Loading…* forever")
needs **zero** tasks, and BUG #22 ("0-remaining shown in alarming red") needs
**all** tasks done. Driving the broken UI to drain or complete every task is
slow and itself bug-prone, so the fixture exposes a reset endpoint to jump
straight to the state under test.

These endpoints are **out of scope for the seeded-bug list** — they are test
scaffolding, not part of the product surface the agent is grading.

---

## The two endpoints

### `GET /api/test/state`

Return the fixture's full in-memory state as JSON. No required schema — return
whatever a test needs to assert against. The bench runner only requires that
the response is **HTTP 200** and **contains JSON** (a `{`); the shape is yours.

BuggyTasks returns, for example:

```json
{
  "tasks": [ ... ],
  "task_id_seq": 8,
  "users": ["alex@example.com"],
  "current_user": null,
  "settings": {"theme": "dark", "notifications": true, "language": "en"},
  "task_counts": {"total": 8, "pending": 5, "done": 3}
}
```

DarkShop returns `products`, `cart`, `account`, `orders`, and a derived
`cart_total_items`.

### `POST /api/test/reset?mode=<mode>`

Restore the fixture to a known starting state, then optionally apply a named
`mode` to reach a specific extreme. Return a JSON body that includes a truthy
**`ok`** marker — the bench's `reset()` helper checks that the string `"ok"`
appears (case-insensitive) in the response:

```json
{"ok": true, "mode": "empty", "tasks": 0, "pending": 0, "done": 0}
```

`mode` defaults to `seeded` when the query param is absent.

---

## Defined modes

`mode` is **fixture-defined** — there is no global registry. Each fixture
documents its own modes in the docstring of its `api_test_reset` handler.

### BuggyTasks (`test-site/app.py`)

| Mode          | State                                                        |
|---------------|--------------------------------------------------------------|
| `seeded`      | Original 8 seeded tasks, no users, no login (default).       |
| `empty`       | Zero tasks — triggers BUG #14 (empty list stuck on *Loading…*). |
| `all_done`    | All seeded tasks done — triggers BUG #22 (0-remaining red).  |
| `one_pending` | Exactly one undone task (smoke).                             |

### DarkShop (`human-eye-fixture/app.py`)

| Mode         | State                                                          |
|--------------|----------------------------------------------------------------|
| `seeded`     | Fresh products, empty cart, account = "Alex" (default).        |
| `with_items` | Cart pre-loaded with two products.                             |
| `renamed`    | Account name set to "Alex-Renamed" while `nav_display_name` stays "Alex" — exposes BUG #10 (stale nav greeting). |

An unrecognised `mode` should fall back to the seeded baseline rather than
erroring, so a typo degrades to a safe reset.

---

## How the bench reaches these endpoints

The bench driver does **not** call `/api/test/*` over a raw HTTP client. It is
already inside a Playwright page on the fixture's origin, so it reaches the
endpoints through `eval_js` + `fetch`, keeping every call same-origin:

```python
# argus/bench/runner.py — reset() helper
await call(
    mcp_module.eval_js,
    code="() => fetch('/api/test/reset?mode=empty', {method:'POST'})"
         ".then(r => r.json())",
)
```

That is why the bench runs with `ARGUS_UNSAFE_EVAL=1` (set automatically by
`argus/bench/__main__.py`): `eval_js` is off by default and the reset path
needs it.

---

## Writing your own fixture

To make an app Argus-bench-compatible:

1. Keep all mutable state in process memory (or a store you can wipe).
2. Add `GET /api/test/state` → return that state as JSON.
3. Add `POST /api/test/reset` → reset to baseline, branch on `?mode=`, and
   return `{"ok": true, ...}`.
4. Seed your intentional bugs deterministically on reset, so every run starts
   identical.
5. Add a scenarios module shaped like
   [`argus/bench/scenarios_buggytasks.py`](../argus/bench/scenarios_buggytasks.py):
   expose a `BASE_URL` and a `SCENARIOS` list of `(bug_id, name, fn)` tuples,
   where each `fn(session) -> (caught: bool, method: str)` drives the MCP
   tools and decides whether the bug was caught.

The bench is intentionally fixture-agnostic — the runner
([`argus/bench/runner.py`](../argus/bench/runner.py)) owns timing, the
health-check, and report-building; the scenarios own the per-bug judgement.

---

## Health-check semantics

Before running scenarios, the runner calls `fixture_healthy(base_url)`, which
`GET`s `/api/test/state` with a 3-second timeout and treats the fixture as
ready only when it returns HTTP 200 with a JSON body. A clear error is raised
otherwise, including the command to start the fixture — so a forgotten
`python test-site/app.py` fails fast with a fix, not a cryptic timeout.
