# tests

Pure-function regression tests for the pipeline. No network, no snapshot files,
no fixtures beyond a few lines. They run in under a second so they can gate
every push.

    pip install pytest
    python -m pytest tests/ -q

## Why these exist

The changelog cited fixture tests for OBTC and EDGAR, a synthetic-chain test
for the options term structure, and unit tests for the freshness helper. None
of them were in the repo — they were run once and discarded. Nothing stopped a
refactor from silently changing a published number.

## What is covered

`test_pipeline.py`
- `merge_series` — the union that protects stored history. Most important
  function in the pipeline: it decides whether a short refetch can truncate
  three thousand days.
- `freshness` — "a fetch can succeed and still be stale". Per-source
  thresholds, boundaries, unknown handling.
- `to_daily` — gap interpolation, including the 60-day cutoff beyond which a
  gap must be left open rather than invented.
- `pct_of`, `_sma`, `state_of` — percentile, moving average with gap
  tolerance, and the exact band boundaries of the composite.

`test_scorecard.py`
- Episode clustering: a run of triggers is one occasion, not one per day.
- Right-censoring: episodes without a complete window are excluded *and
  counted*, never silently dropped.
- Transition-matching: the baseline excludes the aftermath of a trigger.
- Outcome definitions: 40% fall for a top rule, doubling for a bottom rule,
  measured strictly forward — a crash *before* the signal is not credited to it.

## The rule

Every expected value is hand-computed or reasoned from the specification. A
test that asserts whatever the code currently returns will pass forever and
catch nothing.

## What these found

`merge_series` destroyed stored values. If a source returned `null` for a date
that already had a good value, the null overwrote it and the null-filter then
dropped the date entirely — and `save()` writes the merged result back over the
file, so the loss was permanent and silent. Fixed: a null in the new payload
now means "this source had nothing for that date", not "delete what we stored".
