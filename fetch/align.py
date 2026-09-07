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

    if problems:
        for m in problems:
            print(f'  align: FUTURE-DATED {m}', file=sys.stderr)
        raise ValueError('; '.join(problems))

    print(f'  align: {CANONICAL} canonical, shift at ingest {dict(SHIFT)}, '
          f'no future dates ({", ".join(f"{k} to {v}" for k, v in report["checked"].items())})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
