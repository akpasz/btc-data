"""fetch/align.py — put every source on one date convention.

THE PROBLEM

Blockchain.com and Coin Metrics label the same day's price one day apart.
Measured over 400 overlapping days:

    same label                       1.594% mean difference
    Coin Metrics shifted +1 day      0.089% mean difference

An eighteen-fold improvement. The two series describe the same days; they
disagree only about what to call them.

kpis.json presents both under a single `as_of`, so the site publishes a price
from one convention beside a ratio computed from the other. On 4 September 2026
the dashboard showed price_close $81,272.88 and realised price $53,139.56 —
divide those and you get MVRV 1.53 — beside a published MVRV of 1.499, which
was computed against $79,656, the Coin Metrics value labelled the 4th and
actually describing the 5th.

WHICH ONE IS RIGHT

Not determined here, and it does not need to be. Checked against an
independent daily close (Binance USDT, 304 days), both series appear late:
Blockchain.com by two days, Coin Metrics by one. But that reference is
USDT-denominated and its own labelling is unverified, so it settles the
RELATIVE offset and not the absolute truth.

Absolute correctness needs each provider's documented definition of a daily
close. Internal consistency does not, and internal consistency is what makes
the published numbers reconcile with each other.

THE CHOICE MADE HERE

Blockchain.com is the canonical convention, because its price is what the site
publishes as price_close, what every chart draws, and what the scorecard scores
against. Coin Metrics is shifted forward one day to match.

This changes published history for everything derived from Coin Metrics: MVRV,
the MVRV Z-score, realised price and cap, Puell, thermocap, NVT, and the two
MVRV rules on the scorecard. Episode dates may move by a day. Episode counts
should not, since the clustering window is 90 days.

Everything from Blockchain.com is untouched: price, supply, hash rate, fees,
transactions, addresses, and every price-based rule.

Run after the sources and before kpis:

    import align; align.OUT = OUT; align.main()
"""
import io, json, os, sys, datetime as dt

OUT = os.environ.get('DATA_DIR', 'data')
SCHEMA_VERSION = '1.0'

# source file -> days to add to every date label
# Positive means the source labels a close EARLIER than the canonical
# convention, so its dates move forward to match.
SHIFT = {'coinmetrics': 1}

CANONICAL = 'blockchain'


def shift_series(series, days):
    """Move every date label by `days`. Used at INGEST by fetch_all, on raw rows,
    before anything is merged or stored."""
    return _shift_series(series, days)


def _shift_series(series, days):
    out = {}
    for name, points in series.items():
        moved = []
        for d, v in points:
            try:
                nd = (dt.date.fromisoformat(str(d)[:10]) + dt.timedelta(days)).isoformat()
            except Exception:
                continue
            moved.append([nd, v])
        out[name] = moved
    return out


def _mean_abs_pct(a, b, keys):
    tot = 0.0
    for k in keys:
        if b[k]:
            tot += abs(a[k] - b[k]) / abs(b[k]) * 100.0
    return tot / len(keys) if keys else None


def best_offset(series_a, series_b, span=2, tail=400):
    """Which whole-day shift of series_a best matches series_b?

    align used to verify only that nothing was dated in the future. That would
    not notice the shift being WRONG: set SHIFT to 0 or 2 and no date is in the
    future, the check prints ok, and the series is simply a day out - mean
    difference jumping from 0.09% to 1.6% with nothing to say so.
    """
    import datetime as _dt
    best = None
    for off in range(-span, span + 1):
        moved = {}
        for d, v in series_a.items():
            try:
                nd = (_dt.date.fromisoformat(d) + _dt.timedelta(off)).isoformat()
            except Exception:
                continue
            moved[nd] = v
        keys = sorted(set(moved) & set(series_b))[-tail:]
        if len(keys) < 30:
            continue
        e = _mean_abs_pct(moved, series_b, keys)
        if e is not None and (best is None or e < best[1]):
            best = (off, e)
    return best


def main():
    """Verify the convention holds. This no longer SHIFTS anything.

    It used to shift the stored file, and that was wrong in a way only
    production showed: fetch_all.save() rebuilds each document from scratch and
    drops the date_alignment_applied flag, so the idempotency guard never fired,
    and merge_series unions dates rather than replacing them. Every run pushed
    Coin Metrics one more day into the future, permanently - five phantom days,
    each a copy of the last real value, served publicly on the API.

    The shift now happens in src_coinmetrics() on the raw rows, where a second
    application is impossible because stored data is never re-read and re-written.
    What is left here is the definition of the convention and a check that it held.
    """
    today = dt.date.today().isoformat()
    report = {'canonical': CANONICAL, 'shift_at_ingest': dict(SHIFT), 'checked': {}}
    problems = []
    for src in set(list(SHIFT) + [CANONICAL]):
        p = os.path.join(OUT, src + '.json')
        if not os.path.exists(p):
            continue
        doc = json.load(io.open(p, encoding='utf-8'))
        last = None
        for name, pts in (doc.get('series') or {}).items():
            if not pts:
                continue
            d = str(pts[-1][0])[:10]
            if last is None or d > last:
                last = d
            if d > today:
                problems.append(f'{src}.{name} ends {d}, which is after {today}')
        report['checked'][src] = last
        if doc.get('date_alignment_applied'):
            # left over from when this module mutated stored files
            doc.pop('date_alignment_applied', None)
            doc.pop('date_alignment_note', None)
            tmp = p + '.tmp'
            io.open(tmp, 'w', encoding='utf-8').write(json.dumps(doc, separators=(',', ':')))
            os.replace(tmp, p)
            print(f'  align: removed the stale alignment flag from {src}.json')

    # Is the convention still the right one? Compare the canonical price series
    # against the shifted one at several offsets; zero should win.
    try:
        _c = json.load(io.open(os.path.join(OUT, CANONICAL + '.json'), encoding='utf-8'))
        _s = json.load(io.open(os.path.join(OUT, 'coinmetrics.json'), encoding='utf-8'))
        _a = {d: v for d, v in _s['series'].get('PriceUSD', []) if v}
        _b = {d: v for d, v in _c['series'].get('price', []) if v}
        _best = best_offset(_a, _b)
        if _best:
            report['best_extra_offset'] = _best[0]
            report['mean_abs_pct_at_zero'] = round(
                _mean_abs_pct(_a, _b, sorted(set(_a) & set(_b))[-400:]) or 0, 4)
            if _best[0] != 0:
                problems.append(
                    f'coinmetrics fits {CANONICAL} better shifted a further {_best[0]:+d} day '
                    f'({_best[1]:.4f}% vs {report["mean_abs_pct_at_zero"]:.4f}% as stored) - '
                    f'the convention in SHIFT is wrong')
    except Exception as _e:
        print(f'  align: convention check skipped ({_e})')

    if problems:
        for m in problems:
            print(f'  align: FUTURE-DATED {m}', file=sys.stderr)
        raise ValueError('; '.join(problems))

    print(f'  align: {CANONICAL} canonical, shift at ingest {dict(SHIFT)}, '
          f'fit {report.get("mean_abs_pct_at_zero","?")}% at offset 0, '
          f'no future dates ({", ".join(f"{k} to {v}" for k, v in report["checked"].items())})')
    # Publish the fit, not just print it. fetch_all records align as the bare
    # string "ok" and rewrites manifest.json at the end of the run, so this
    # number survived only in a log. It is the single most diagnostic figure
    # here: if the convention ever drifts, this moves before anything breaks.
    try:
        mp = os.path.join(OUT, 'align_report.json')
        tmp = mp + '.tmp'
        io.open(tmp, 'w', encoding='utf-8').write(json.dumps({
            'schema_version': SCHEMA_VERSION,
            'checked_at': dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            **report}, indent=1))
        os.replace(tmp, mp)
    except Exception as e:
        print(f'  align: could not write align_report.json ({e})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
