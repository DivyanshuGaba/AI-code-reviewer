# Evals

Measures the review pipeline against bugs we planted ourselves, so the ground truth is exact.

```bash
reviewer eval retrieval                 # free: retrieval quality per strategy, no model calls
reviewer eval run --dry-run             # rough cost estimate, no model calls
reviewer eval run --max-cost 3          # the real thing: calls the API and costs money
reviewer eval run --include-clean       # also review the unmodified code to count false positives
reviewer eval compare before/results.json after/results.json
```

## How a case works

`cases/<id>/` holds a small, correct Python repo (`repo/`, with its own tests) and a `case.yaml`:

```yaml
id: pricing
description: ...
bugs:
  - id: sign
    file: pricing.py
    old: "price * (1 - pct / 100)"      # must occur exactly once in the clean file
    new: "price * (1 + pct / 100)"      # what the buggy variant contains
    category: bug                       # bug | security | performance | readability | architecture
    severity: high
    title: Discount increases the price instead of reducing it
    tests_detect: true                  # the case's tests fail when this bug is present
    relevant: ["pricing.py::bulk_unit_price"]   # context needed to see the bug (retrieval eval)
acceptable:                             # deliberate patterns; findings here are not errors
  - file: retry.py
    text: "except Exception as err:"
```

The **buggy variant** is the clean repo with the bugs substituted in; line ranges and enclosing
functions are computed from the result. The **clean variant** is the repo untouched: every finding
on it is a false positive. The `control` case has no bugs at all.

## What is measured

| metric | meaning |
|---|---|
| precision, recall, F1 | findings matched to planted bugs (same file and nearby lines, or same function and category); one finding per bug |
| triage / deep-review split | where missed bugs were lost: the function never reached deep review, or it did and the bug was not reported |
| category accuracy, recall by category and severity | whether the finding is labelled correctly; which kinds of bug are missed |
| confidence of true vs false positives | whether the model's confidence means anything |
| false positives per 100 functions | on the clean variant |
| fix quality | produced; **exact** (the patched function equals the original, by AST); **verified** (sandbox status); **resolves** (the case's tests go from failing to passing, sandbox only) |
| retrieval | recall@k and MRR of the planted bug's `relevant` functions, for graph, lexical, vector and fused retrieval, with no model involved |

Fixes are attempted for every found bug in an eval, regardless of the production severity cut-off,
so fix quality is measured on all of them.

## Limits

- The cases are small and synthetic. Planted bugs are easier to find than real ones, so absolute
  numbers will flatter the tool; use them to compare changes (a prompt, a threshold, a model), not
  to predict real-world performance.
- The repos are tiny, so retrieval recall at high k saturates. Look at recall@1, recall@3 and MRR.
- `exact` is strict: a correct fix written differently from the original counts as not exact.
  `resolves` is the fairer check, but it needs Docker and only covers bugs the tests can see.
- Responses are cached by content, so re-running an unchanged eval is free, and a changed prompt,
  model or code re-runs only what changed. Use a fresh `REVIEWER_HOME` to force a full re-run.
- To use historical bug-fix commits as ground truth, check out the fix's parent as a case's `repo/`
  and express the fix as the `new` to `old` swap. Not automated yet.
