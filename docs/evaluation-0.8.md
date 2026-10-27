# Argus 0.8 evaluation — 2026-10-25

> Historical result for Argus 0.8 under the conditions below (19-tool `agent`
> profile, compact responses). It does not measure the current release. Commands
> use the current repository layout (`argus_lab`).

The focused workflow reduced Argus's own overhead in this sample and found one
email-validation boundary defect the conventional-browser agent missed. It did
**not** beat the baseline on latency or token usage. Declared journey coverage
was equal. These are small, synthetic experiments, not a general performance claim.

## Final paired sample

Ten pairs used `gpt-6-astra`, high reasoning, ChatGPT subscription authentication,
and `codex-cli 0.155.0-alpha.16.4`. Each attempt had a five-minute model deadline
and a 60-call gateway cap. The baseline was the repository's seven-tool
conventional Playwright MCP adapter, **not Microsoft's Playwright MCP**. Argus
used the 19-tool `agent` profile with compact responses. Both arms returned
post-action observations. Argus also recorded coverage, rechecked symptoms and
exported reports; resource totals include this additional work.

| Metric | Browser baseline | Argus |
| --- | ---: | ---: |
| Trials | 10 | 10 |
| Found defect opportunities | 5 / 6 | 6 / 6 |
| False-positive / duplicate findings | 0 / 0 | 0 / 0 |
| Evidence-backed declared goals | 20 / 20 | 20 / 20 |
| Failed / over-budget trials | 0 / 0 | 0 / 0 |
| Median tool calls | 4 | 7 |
| Median elapsed seconds | 22.8 | 39.9 |
| Median input + output tokens | 68,926.5 | 145,975 |

There were eight cases across profile persistence, deletion, email
validation/recovery and basket/checkout consistency. Profile cases ran a second
repetition with reversed arm order. Thus six opportunities represent five unique
defect cases, not six distinct bug classes. Tokens include cached input reported
by the host and are **not** a billing estimate. There were no missing token counts
in the 28 completed application trials.

## Before/after the targeted correction

An eight-run pilot exposed redundant `observe` calls and per-goal coverage calls.
The correction added post-verification observations, atomic related-goal batches,
concise discovery descriptions and instructions to reuse returned evidence.
The first four pairs of the final sample repeat exactly the pilot's case set and
repetition. Compare that common subset, not differently weighted cohort totals:

| Argus on the same four cases | Pilot | Optimized |
| --- | ---: | ---: |
| Found defect opportunities | 3 / 3 | 3 / 3 |
| Median calls | 17 | 9 |
| Median input + output tokens | 198,867 | 163,367 |
| Median elapsed seconds | 60.0 | 54.7 |

That is 47.1% fewer median calls and 17.9% fewer median tokens than the pilot.
Elapsed time fell 9.0%, but timing and model sampling are noisy; this is not a
statistically established speedup. Baseline median calls were seven in both
common subsets. No individual best run was substituted for the full subset.

## Ground-truth correction and review

The historical case ID `form-fixed` was initially mislabeled as clean. Argus
submitted `@example.com`, established an invalid-email error first, then observed
that this malformed address changed the result to `Subscribed`. The server oracle
confirmed the transition. Both pilot and optimized Argus found it; neither paired
baseline did. This is a real residual fixture defect, not an Argus false positive.

The corrected oracle assigns `form-localpart` to that case in **both** arms.
The case's browser behavior and historical ID are preserved as a partial-fix
regression fixture; its ground-truth declaration and browser test now reflect
the residual defect. Other fixed profile, deletion and cart cases remain clean
controls. The extra defect is a **post-hoc discovery**, not a predeclared endpoint.
On predeclared defects alone, both arms found all five final-sample opportunities.

Review was performed by the implementation assistant against tool snapshots,
transitions, the stated requirements and server-side events—not by keyword
matching, a VERIFIED badge, or a blinded third party. Model call-number citations
were sometimes offset by one and goal labels sometimes included prose. The
original reports are preserved; reviewed references and correction rationales
are published separately. An inconclusive replay remains inconclusive even when
separate directly observed transitions support the semantic defect. Exercised
goals do not imply exhaustive input-space coverage or successful backend delivery.

## All authorized attempts

Exactly 32 subscription attempts were used, with no paid API fallback:

- Three approval/setup attempts executed no testing operations. Two early
  collector records said completed because the host produced an answer despite
  denied tools; they are **protocol-invalid, not successful tests**. The third
  was interrupted. The collector now stops on denied tools or no execution.
- One fixture preflight made four browser calls but reload resubmitted POST.
  It was invalid for the intended fresh-GET persistence comparison. The fixture
  was changed to POST/303/GET and all variants were browser-checked before the pilot.
- Eight pilot and twenty optimized trials completed: 28 application trials,
  225 executed tool calls, no timeouts or budget violations. Both cohorts used
  the same post-redirect fixture behavior and identical task prompts.

The four invalid preflights are excluded from application quality comparisons,
not from budget accounting. They took 84.4 seconds total; three reported a combined
173,419 input/output tokens, while the interrupted attempt's token count is unknown.
Separate cohort manifests retain implementation, fixture and prompt hashes.
Ground-truth/reviewer tooling was corrected after collection without changing
the tested agent workflow. No additional model runs were used to select results.

## Recheck the published data

```bash
python -m argus_lab.evaluation --compare examples/agent-evaluation-pilot.json
python -m argus_lab.evaluation --compare examples/agent-evaluation-optimized.json
```

The companion `*-evidence.json` files contain original model reports, review
rationales, selected tool observations, synthetic server events and raw-trace
hashes. Loopback origins are normalized to `http://fixture.test`; local paths,
screenshots and host event logs are not published. The datasets can be rescored
without subscription usage. Raw collection directories remain private.

Real stdio tests confirmed MCP progress delivery with the Python SDK. This Codex
CLI's JSON stream did not expose separate progress events, so it does not prove
visible progress in every host UI. The live dashboard was separately checked
through a real browser, HTTP status/SSE and actual test operations.

Use these results to understand the trade-off: Argus adds a reusable testing and
evidence workflow; it is not yet demonstrated to improve every model, every app,
overall coverage, or cost efficiency compared with ordinary browser tools.
