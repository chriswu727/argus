# Paired evaluation

Available in 0.8.0 and newer.

## Response overhead

Version 0.9 introduced an offline, deterministic encoding benchmark; the figures
below are reproduced with 0.13.2:

```bash
python -m argus_lab.evaluation.overhead
```

It encodes the same 24 synthetic observations in compatible, compact and delta
modes, checks lossless reconstruction, and measures tool discovery separately.
The compact encoder retains 0.8 semantics; there are no model calls, browser
actions or timing claims in this benchmark. Repeated catalog content deliberately
exercises the deduplication opportunity, while navigation is a negative control.
The [checked-in result](../examples/response-overhead.json) records the method,
versions and byte-category definitions.

| Workload (6 responses each) | Compact JSON bytes | Delta JSON bytes | Reduction |
|---|---:|---:|---:|
| Repeated catalog observation | 30,897 | 7,809 | 74.73% |
| Catalog with changing feedback | 30,993 | 7,975 | 74.27% |
| Navigation on every step | 30,897 | 31,119 | -0.72% |
| Small repeated page | 4,395 | 3,392 | 22.82% |
| Total | 97,182 | 50,295 | 48.25% |

Encoding sizes are unchanged from 0.10.0. The checked-in result records the
Argus, MCP SDK (1.30.0) and Python versions used, and includes current discovery
sizes for each profile; counts exclude server instructions, and delta mode adds
decoding instructions at initialization.

Since 0.11.0, tool descriptions taken from docstrings are normalized with
`inspect.cleandoc`, so the whole result, apart from the recorded Python version,
is byte-identical on Python 3.11, 3.12 and 3.13. Earlier releases advertised raw
docstrings: Python 3.13 strips their indentation at compile time and older
versions do not, so 0.10 discovery sizes for profiles using docstrings depend on
the Python version.

0.11.0 replaced the `agent` and `core` profiles with `web`. Full discovery
payloads (tool JSON plus the server instructions, 223 bytes up to 0.13.1 and
238 bytes from 0.13.2) compare as follows:

| Profile | Tools | Discovery bytes |
|---|---:|---:|
| 0.10 `agent` | 19 | 24,386 (any Python) |
| 0.10 `core` (former default) | 30 | 51,732 on Python 3.11/3.12; 50,564 on 3.13 |
| 0.11 `web` (default) | 28 | 34,379 (any Python) |
| 0.11 `advanced` | 61 | 79,039 (any Python) |
| 0.12 `web` (default) | 29 | 36,280 |
| 0.12 `advanced` | 62 | 80,940 |
| 0.13.2 `web` (default) | 29 | 36,480 |
| 0.13.2 `advanced` | 62 | 81,140 |

0.11 `web` is 41.0% larger than 0.10 `agent`, and 33.5% (Python 3.11/3.12) or 32.0%
(Python 3.13) smaller than 0.10 `core`. These are bytes for discovery only, not
model tokens or whole-session context.

0.12 adds `export_test` and richer interaction arguments. Its web discovery is
5.5% larger than 0.11 web; response encoding sizes remain unchanged. The 0.12
figures above were measured on Python 3.12.13 with MCP SDK 1.30.0.

0.13.2 changes the server instructions (they now say to look for the app's
address before asking and to report before changing code), so discovery grows
by 15 bytes in every profile; `full` also grows by 87 bytes of tool JSON from
the `navigate` description. Response encoding sizes are unchanged. Measured on
Python 3.12.13 with MCP SDK 1.30.0.

These are **encoding results, not a new agent evaluation**. They do not show
token savings, coverage gains, fewer false positives, lower billed costs or faster
end-to-end work. Snapshot resynchronization may add calls when a host loses context.
The 0.8 model evaluation remains the evidence for that earlier version, including
its higher measured token usage. A future model comparison needs a separately
approved budget and matched controls; do not substitute bytes for unknown tokens.

The evaluation gateway writes numeric `discovery.json` and per-call byte counts
alongside its private trace, plus cumulative runtime component timings for the
Argus arm. Baseline component timing is unknown, not zero. Production monitoring
exposes only counts/timings and does not persist trace payloads.

The evaluation package is separate from the MCP runtime. Its default CLI only
runs local protocol smoke checks and offline scoring. A separate, explicit opt-in
Codex collector consumes the operator's subscription; it is never invoked by the
MCP server, package import or test suite. No API-provider fallback is implemented.

## 0.10 encoding and finalization

A subsequent offline change removes generated input-schema display titles,
omits empty optional success metadata in compact/delta results, and lets agents
submit final coverage while ending a session. Runtime validators, tool display
titles, evidence, actual progress stages and verdicts are retained.

| Deterministic measurement | Before | After |
|---|---:|---:|
| Agent-profile tool JSON bytes | 24,040 | 23,069 |
| Agent-profile input-schema bytes | 4,461 | 3,414 |
| Compact result bytes, 24-response fixture | 99,570 | 97,182 |
| Delta result bytes, same fixture | 52,683 | 50,295 |
| Argus scripted save/reload calls per control | 7 | 5 |

These discovery figures describe the finalization snapshot, before typed finding
evidence was added. Run `python -m argus_lab.evaluation.overhead` for current discovery
counts and the scripted smoke check for the five-call workflow. It starts, declares the
goal in progress, clicks Save and reads its returned observation, checks a
fresh load, then ends with explicit final coverage. It runs buggy, client-storage
fixed and server-backed clean controls over real MCP stdio. Separate tests
verify that invalid final batches leave both the session and coverage unchanged,
and that export failures retain the final ledger with a warning.

These are byte counts and scripted call counts. This follow-up consumed no
model calls and does not establish new token, subscription, latency or agent
quality improvements. The model measurements below describe the earlier source
snapshot, before this finalization change.

## Evidence provenance regressions (0.10)

Local browser regressions cover deferred findings from two pages while the agent
is on a third page, distinct correctly located screenshots, caller-page
restoration, and exactly one original form POST. They also cover screenshot
opt-outs/failures, login/error-page uncertainty, explicit URL mismatches, frozen
coverage windows, revisited URLs, late goal-linked findings, atomic rejection,
expected/actual report redaction and the real MCP stdio evidence schema.

This verification uses deterministic fixtures and no model or subscription calls.
It establishes these regression behaviors, not a new long-workflow model quality
or token comparison. Typed evidence deliberately adds a small discovery schema;
use the offline benchmark to measure bytes, not to infer model-token costs.

## Bounded verification-efficiency check (pre-batching snapshot)

On 2026-10-26, four authorized Codex attempts tested one synthetic profile-save
task in buggy/fixed variants, each with Argus and the repository's seven-tool
conventional-browser MCP baseline (not Microsoft's Playwright MCP). Controls:
`gpt-6-astra`, high effort, Codex CLI `0.155.0-alpha.16.4`, the existing collector
prompt/fixture, fresh state, 60 calls and five minutes per attempt. Argus used
the 19-tool agent profile with compact responses, not delta. Arm order was
reversed for the fixed variant. All four attempts completed without tool errors.

| Variant / arm | Calls | Seconds | Input tokens | Cached input subset | Output tokens |
|---|---:|---:|---:|---:|---:|
| Buggy / baseline | 4 | 31.7 | 68,508 | 55,808 | 432 |
| Buggy / Argus | 7 | 40.6 | 145,223 | 124,544 | 665 |
| Fixed / Argus | 6 | 38.0 | 126,477 | 107,264 | 583 |
| Fixed / baseline | 4 | 29.7 | 68,528 | 64,256 | 322 |

Trace and server-oracle review found the one seeded defect in both arms, no
false-positive claims on the fixed control, and evidence for all four goal
opportunities per arm. Argus submitted once per variant. On the buggy variant
it used the suggested fresh-load predicate and confirmed the failure in two
loads, without replaying writes or attempting a duplicate finding. Both save
summaries correctly reported visible text and field-value changes. Shared goal
evidence was returned once in the compact coverage map. Incorrect model call
citations were corrected in a separate review; raw answers were not rewritten.

A preceding 0.9.1 diagnostic using the same model/host/task/profile settings
took 10 Argus calls on the buggy variant and 6 on the fixed variant, with
334,627 combined input/output tokens. This follow-up used 13 calls and 272,948
tokens in total (18.4% fewer); non-cached input fell from 42,733 to 39,892 (6.6%).
This is one before/after sample per variant, not a statistical performance claim.
Argus still used 1.98 times the baseline's combined tokens and more calls; its
coverage bookkeeping, independent checks and exported reports are extra work.
Caching and host latency vary, and token totals are not subscription-usage
percentages. No general coverage, speed or cost advantage is established.

The tested runtime source hash was
`66de6f7ec73c93258d9dfd0c6f1c57c7f5fcf32b23d926788bd3aeeacca57539`
(collector SHA-256 over sorted `argus/**/*.py` contents). Raw traces, screenshots,
account usage and local paths remain private; only reviewed synthetic aggregates
are published. No further model attempts were used to improve these results.

## Local smoke check

```bash
python -m argus_lab.evaluation --smoke --output /tmp/argus-evaluation.json
python -m argus_lab.evaluation --compare /tmp/argus-evaluation.json
```

Install Chromium first. The smoke runner starts an ephemeral loopback fixture
and isolated browsers. It compares direct Playwright operations with real Argus
stdio MCP calls for three controls: a save that loses its value on reload, its
client-storage fix, and a clean server-backed save. A deterministic script
drives both arms; neither arm calls an LLM. Temporary reports and logs are
removed when the run exits.

These results validate orchestration, negative controls and scoring. They do
not measure agent reasoning, model recall, or real-world gains. The Argus arm
also records coverage and writes reports, so its scripted time/call count is
not an equal-work efficiency claim. Token usage remains null, not zero.

Exit codes: `0` for a completed comparison, `1` if any trial failed (or a smoke
control was misclassified/over budget), `2` for invalid input. Quality scores
from external agent runs are reported, not automatically declared acceptable.

## Collecting agent trials

Use `Dataset.model_json_schema()` from `argus_lab.evaluation.models` for the exact
schema; `--output` from a smoke run provides a complete example. For real runs,
set `mode` to `agent`, use versioned model/host identities, and collect both arms:

1. Pin the task prompt, fixture revision, initial state, seed, model settings,
   wall-clock/tool/token budgets and host version. Counterbalance arm order
   across repetitions; isolate browser/storage state for every trial.
2. Let the baseline agent use its ordinary browser tools and the Argus arm use
   Argus. Keep task instructions and permissions equivalent. Do not expose
   seeded bug IDs or the answer key to the agent.
3. Record actual elapsed time, tool calls and provider usage. Missing token
   telemetry is null. Keep timeouts, crashes, partial results and budget overruns.
4. Have a separate oracle or reviewer map each finding to an exact ground-truth
   bug ID, or null for a false positive. Supply evidence references and a
   versioned `adjudicator`. Do not use keyword matches or the agent's own
   VERIFIED badge as ground truth. Ground-truth bugs need distinct IDs.
5. Mark a goal covered only when the stored trace/evidence demonstrates its
   journey was exercised. Merely visiting a page does not count. Include fixed
   and clean cases to measure false positives.
6. Score the complete dataset. Report all repetitions and the distribution,
   scope, failures and costs, not a selected best run. Authorize any paid or
   subscription-consuming collection separately; this CLI never launches it.

Every `(case_id, repetition)` must contain baseline and Argus records. The
validator rejects missing/duplicate pairs, mismatched controls, unknown case,
bug or goal IDs, empty evidence, non-finite measurements, and silent failed runs.
It cannot independently verify externally supplied evidence, usage or reviewer
judgments; retain source traces privately for audit. A valid file is not proof
that the experimental methodology was followed.

## Metrics

Recall is unique matched bugs divided by all expected bugs across trials.
Precision is unique matched bugs divided by all findings, so duplicate claims
do not inflate it. False positives and duplicate findings are reported separately.
Clean cases have no recall denominator; false positives on them are counted.
No claims produces null precision, not perfect precision.

Goal coverage counts only supplied evidence-backed goals. Failed trials remain
in all relevant denominators; partial evidence can still count. Time and tool
calls report medians, recall also reports its trial range. Token medians are
null if any trial lacks complete usage. Budget violations remain visible and
do not remove a run. A missing token count cannot establish token-budget
compliance. Deltas are Argus minus baseline; lower resource usage and higher
quality are desirable, but small samples are not statistically conclusive.

Do not publish private targets, credentials, customer data or raw transcripts.
The scorer does not sanitize externally supplied evidence when saving datasets.
Review artifacts before sharing. Only synthetic fixtures belong in this repository.

## Opt-in Codex collection (POSIX hosts)

Use an installed Codex CLI logged in with ChatGPT. Obtain explicit authorization
for the model, number of attempts and budget first. The collector sets
`forced_login_method=chatgpt`, removes API-key environment variables, disables
shell/web/plugin tools, and uses an empty working directory and ephemeral runs.
The synthetic testing gateway alone is pre-approved for this invocation; the
user's persistent Codex settings are not changed. See the official
[non-interactive interface](https://learn.chatgpt.com/docs/non-interactive-mode)
and [MCP tool policies](https://learn.chatgpt.com/docs/extend/mcp).

```bash
# Only after explicit authorization; MODEL must be an available, fixed model.
python -m argus_lab.evaluation.codex_runner \
  --allow-subscription-usage --model MODEL --effort high \
  --output /private/path/collection --runs 8 --max-runs 32
```

The same directory can continue to a larger `--runs` value without repeating
existing attempts. Its immutable manifest pins implementation, fixture, prompt,
host and model settings. `--max-runs` persists the approved collection cap, at
most 32. Changed controls, incomplete attempts or host/tool-startup failures stop
collection. Track authorization across separate collections as well; creating a
new directory is not permission to reset a user's overall budget.

Each run has a five-minute model deadline and a gateway that executes at most
60 tool calls. Process cleanup is bounded separately. Attempts, interruptions
and failures remain on disk. The runner rejects disallowed host tool execution,
caps event output at 16 MB and stderr at 64 KB, and does not retain model reasoning.
Directories are private; only synthetic data should enter these fixtures.

The baseline is a small conventional Playwright MCP adapter, not Microsoft's
official Playwright MCP. Both arms can navigate, read visible state, click, fill,
reload, use the keyboard and take screenshots. Baseline click/fill/navigation
already return post-action snapshots. Argus adds its testing protocol, evidence,
verification, coverage and the 19-tool `agent` profile with compact structured results.
Compare these configurations,
not entire third-party products. Neither model receives the fixture source or
bug/fixed variant. Each trial uses a fresh server and browser state.

Eight cases cover four task families. The historical `form-fixed` case is a
partial fix with a known missing-local-part validation defect, not a clean control;
its oracle includes `form-localpart`. Profile, deletion and cart fixed cases are
clean controls. Browser regression tests verify these declarations. The normal schedule
has two repetitions, alternating arm order. `seed` records the fixture/order seed;
Codex model sampling does not expose a deterministic seed in this runner. Token
usage is the host's reported total input (including cached input) and output, not
a dollar-cost estimate. Keep model, host and reasoning effort fixed.

## Autonomy acceptance

Whether a host agent finishes a testing pass without a person is a separate
question from whether the tools work. Two layers answer it.

**Deterministic layer (runs in CI).** `tests/test_autonomous_acceptance.py`
drives the tools through the MCP server against an application whose defect
can be switched on and off: the defective build, the fixed build and a build
that never had the defect; content that arrives late; a row that is gone; a
timeout after a write; a failed session and a failed restart; a page behind a
login. `tests/test_first_run_browser.py` covers the first-use browser download.
These calls are scripted. They show that a host has what it needs at each
step, not that a model takes the step.

**Host layer (needs separate authorization because it uses model
subscriptions; two small runs are recorded below).** Use the real plugin in a
real host, a fresh conversation per
trial, and only these three requests, with no testing instructions added:

1. "Test the app." (the app is running, or startable from the project)
2. "The feature is done, check it." (after the agent or a person changed code)
3. "The problem from before is fixed, test it again."

Run each request on three builds: with the seeded defect, with it fixed, and
one that never had it. The last two are the controls: a finding there is a
false positive, and a pass that reports "nothing found" on the defective build
is a miss.

Record per trial, from the host transcript and Argus's own artifacts:

| Measure | Source |
|---|---|
| Human interventions (any message the person had to send after the request), and why | transcript |
| Completed (a final report exists and `end_session` ran) | transcript, report file |
| Where a seeded defect was lost: not exercised, exercised but not noticed, noticed but not recorded, recorded but not confirmed, confirmed but missing from the final answer | action trace, findings, receipts, final answer |
| False positives, on every build | adjudicated final answer |
| Inconclusive results and whether they were reported as inconclusive | receipts, final answer |
| For request 3: whether `regression_check` was used, and its result per finding | tool trace |
| Wall-clock time, tool calls, and tokens where the host reports them | transcript |

Rules, so the result means something:

- Fix the prompts, builds, pass criteria and the adjudication key before the
  first trial, and keep them outside what the agent under test can edit.
- Count an intervention as avoidable only if the information was already in
  the project, the conversation or a running process.
- A trial that runs out of budget is reported as unfinished with what it
  covered. Do not rerun it until it passes, and do not drop it.
- Report every trial. Small samples show problems; they do not prove rates.

### Host runs before 0.13.2

Two small runs, each authorized by the maintainer. Both used the Claude plugin
in Claude Code with a small model, against one synthetic to-do app with one
seeded defect: the defective build shows a deleted task as gone but never
saves the delete. They exercised a bounded set of requests on that app. They
found real defects in Argus, all fixed before 0.13.2. They do not measure
reliability, coverage or a completion or detection rate, and nothing here
generalizes to other apps, models or hosts. Transcripts and harnesses are
kept outside this repository.

Common setup: the plugin loaded from a source snapshot (the work was not
released), the user's own settings, hooks and plugins left out, one request
per trial, nobody answering the agent, requests sent in Chinese. Three builds
of the app: defective, fixed, and the fixed build in a fresh conversation as
a project that never had the defect.

#### First run (Claude Haiku 4.5, author-judged)

Requests: "test this app", "the delete feature is done, check it", and "the
problem from before is fixed, test it again", with no testing instructions.

| Request | Build | Outcome on the run's final code |
|---|---|---|
| Test this app | defective | Found the delete defect, confirmed it on fresh loads, kept it in the report and journal |
| Test this app | never defective | Walked the main flows with reload checks; nothing found, no false positive |
| Feature is done, check it | defective | Did not use Argus; read the source, named the defect, then tried to edit the app |
| Feature is done, check it | never defective | Did not use Argus; checked by hand; correct "no problem" |
| It is fixed, test again (it was not) | defective | Walked the journey again, answered "not fixed"; did not call `regression_check` |
| It is fixed, test again | fixed | Walked the journey again, answered "fixed"; did not call `regression_check` |

Defects it found in Argus, fixed before 0.13.2:

- A progress notification read by the host together with its response made
  Claude Code close the connection and restart the server, losing the session
  and its finding. Reproduced without a model with the TypeScript MCP SDK;
  progress notifications are now off unless enabled (see the
  [protocol](agent-protocol.md#progress-transport)).
- "Test this app" was first answered with questions and no tool call.
- A real defect stayed unconfirmed when `verify` was left out or stated the
  expected state instead of the symptom. An automatic correction added for
  this was removed after an independent review showed it confirmed findings
  that claimed something else; `record_bug` now suggests the check and the
  agent confirms it.
- The agent began editing the app before reporting.

#### Second run (Claude Haiku 5.5, independently run and judged)

Run and judged by a reviewer other than the author of the changes. The
requests named Argus and said to test and report only, without changing code.

| Trial | Build | Code | Outcome |
|---|---|---|---|
| Test the main flows | defective | main before the fixes below | Found the delete defect, confirmed it, journaled it, reported impact and evidence |
| Test the main flows | never defective | same | No verified defect; two subjective UI notes recorded as observations, not bugs |
| Re-check the earlier problem (still broken) | defective | same | `regression_check` reached the host with empty structured data; it re-tested by hand and answered "not fixed"; the report had no recheck |
| Re-check the earlier problem (fixed) | fixed | same | The same empty result; re-tested by hand, answered "fixed" |
| Re-check the earlier problem (still broken) | defective | with the fixes below | `regression_check` returned STILL-PRESENT and the report kept it; the host also recorded the known bug again as a new finding |
| Re-check the earlier problem (fixed) | fixed | with the fixes below | `regression_check` returned NO-LONGER-REPRODUCES and the report kept it |

Defects it found in Argus, fixed before 0.13.2:

- Claude Code shows the model the structured result, not the text, and tools
  that answered only in text, `regression_check` among them, reached it as
  `data: {}`. Their results and error reasons are now in `data`.
- `regression_check` results did not appear in the end-of-session summary
  or any report. They now do, apart from new findings.
- To find the app's address the agent read a whole `.env` (the app had
  none). The plugin skills now say not to and to look up only the host or
  port variable if nothing else gives the address.

#### What these runs do not show

- That a host uses Argus when asked, unprompted, to check a finished
  feature. In the first run the model read the code instead.
- That a host re-tests from the journal when the request does not mention
  Argus. It did when the second run's requests named Argus.
- That a host avoids side effects outside the UI. In the never-defective
  trial of the second run the agent changed test data with a direct HTTP
  request while probing an error page.
- Anything about other models, hosts, apps with a login, larger apps, long
  runs, or rates. Each cell ran once, and the same request behaved
  differently between runs.

## Independent adjudication

The collector saves an unscored model answer, tool trace and server-side oracle
log for each attempt. A reviewer must inspect the observed behavior against the
stated requirement, not match keywords or trust a reproduction badge. Every model
claim receives a match or null; unsupported and duplicate claims cannot be omitted.
Coverage needs successful tool references showing the actual journey, not merely
the model's assertion that it was covered.

Create a review keyed by run directory, using `Review.model_json_schema()` from
`argus_lab.evaluation.adjudication`. Each finding decision includes its zero-based
claim index, matched bug ID or null, reviewer-checked evidence call numbers, and
rationale. Keep the original answer unchanged; explicitly explain any correction
to model-generated call numbering. Each goal decision includes
the goal ID, evidence call numbers and rationale. Then validate and score:

```bash
python -m argus_lab.evaluation.adjudication \
  --collection /private/path/collection --reviews /private/path/reviews.json \
  --output /private/path/paired.json
python -m argus_lab.evaluation --compare /private/path/paired.json
```

The assembler requires a review for every finalized attempt, every finding,
valid trace references and complete pairs. It still trusts the reviewer's semantic
judgment. Audit that judgment with server logs and trace excerpts. Protocol-invalid
preflight attempts must be reported separately with their reason and cost; do not
quietly erase them or describe them as successful application tests.

For historical collections, `--cases reviewed-cases.json` supplies an explicit
list of `Case` records instead of the current fixture oracle. Document any
ground-truth correction and apply it to both arms. See the
[0.8 evaluation report](evaluation-0.8.md) for results, preflight dispositions,
the post-hoc fixture correction and downloadable evidence.
