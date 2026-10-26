# Paired evaluation

Availability: development on `main`; not included in PyPI 0.7.1.

The evaluation package is separate from the MCP runtime. It has no provider
client, API key requirement or autonomous model invocation. It supports local
protocol smoke checks and offline scoring of externally collected agent trials.

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
