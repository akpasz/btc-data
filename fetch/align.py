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
    applied = {}
    for src, days in SHIFT.items():
        p = os.path.join(OUT, src + '.json')
        if not os.path.exists(p):
            print(f'  align: {src}.json not found, skipping')
            continue
        doc = json.load(io.open(p, encoding='utf-8'))
        if doc.get('date_alignment_applied'):
            print(f'  align: {src} already aligned, skipping')
            applied[src] = doc['date_alignment_applied']
            continue
        before = {k: len(v) for k, v in doc.get('series', {}).items()}
        doc['series'] = _shift_series(doc.get('series', {}), days)
        after = {k: len(v) for k, v in doc['series'].items()}
        lost = {k: before[k] - after.get(k, 0) for k in before if before[k] != after.get(k, 0)}
        if lost:
            print(f'  align: ABORTED on {src} — shifting dropped points {lost}')
            return 1
        doc['date_alignment_applied'] = days
        doc['date_alignment_note'] = (
            f'Every date label moved {days:+d} day to match {CANONICAL}.json, which is '
            f'the convention this site publishes as price_close. The two sources '
            f'described the same days under labels one day apart; measured over 400 '
            f'overlapping days the mean difference falls from 1.594% to 0.089% when '
            f'this shift is applied. See fetch/align.py.')
        tmp = p + '.tmp'
        io.open(tmp, 'w', encoding='utf-8').write(json.dumps(doc, separators=(',', ':')))
        os.replace(tmp, p)          # atomic: a failed run leaves the last good file
        applied[src] = days
        print(f'  align: {src} shifted {days:+d} day, {sum(after.values())} points')

    if applied:
        # record it where a consumer will see it
        mp = os.path.join(OUT, 'manifest.json')
        if os.path.exists(mp):
            m = json.load(io.open(mp, encoding='utf-8'))
            m['date_alignment'] = {'canonical': CANONICAL, 'shifted': applied}
            tmp = mp + '.tmp'
            io.open(tmp, 'w', encoding='utf-8').write(json.dumps(m, indent=1))
            os.replace(tmp, mp)
    return 0


if __name__ == '__main__':
    sys.exit(main())
