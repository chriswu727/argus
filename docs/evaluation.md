# Paired evaluation

Available in 0.8.0 and newer.

## Response overhead

Version 0.9 introduced an offline, deterministic encoding benchmark; the figures
below are reproduced with 0.10.0:

```bash
python -m argus.evaluation.overhead
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

The checked-in result includes current discovery sizes for each profile.
Discovery includes the typed finding evidence introduced in 0.10.0; it is not
directly interchangeable with earlier source snapshots. Counts exclude server
instructions; delta mode adds decoding instructions at initialization.

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
evidence was added. Run `python -m argus.evaluation.overhead` for current discovery
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
python -m argus.evaluation --smoke --output /tmp/argus-evaluation.json
python -m argus.evaluation --compare /tmp/argus-evaluation.json
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

Use `Dataset.model_json_schema()` from `argus.evaluation.models` for the exact
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
python -m argus.evaluation.codex_runner \
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

## Independent adjudication

The collector saves an unscored model answer, tool trace and server-side oracle
log for each attempt. A reviewer must inspect the observed behavior against the
stated requirement, not match keywords or trust a reproduction badge. Every model
claim receives a match or null; unsupported and duplicate claims cannot be omitted.
Coverage needs successful tool references showing the actual journey, not merely
the model's assertion that it was covered.

Create a review keyed by run directory, using `Review.model_json_schema()` from
`argus.evaluation.adjudication`. Each finding decision includes its zero-based
claim index, matched bug ID or null, reviewer-checked evidence call numbers, and
rationale. Keep the original answer unchanged; explicitly explain any correction
to model-generated call numbering. Each goal decision includes
the goal ID, evidence call numbers and rationale. Then validate and score:

```bash
python -m argus.evaluation.adjudication \
  --collection /private/path/collection --reviews /private/path/reviews.json \
  --output /private/path/paired.json
python -m argus.evaluation --compare /private/path/paired.json
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
