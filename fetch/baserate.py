"""fetch/baserate.py - what bitcoin does when nothing in particular is true.
Writes data/baserate.json.

Every claim on the scorecard is measured against "what happened anyway". This
is that. Fifteen pages reference the baseline; none showed it. A reader who
sees "47.6% against 59.9%" needs to know what the 59.9% is a rate OF, and
this page is where it comes from.

WHAT IS HERE
  * forward return distributions at 30, 90, 180 and 365 days, on every day
    since 2013 and by starting cycle - percentiles, so the shape is visible
  * the two scorecard outcomes, unconditionally: how often a 40% fall arrives
    within a year, and how often a doubling does. These are the raw base
    rates the scorecard's transition-matched baselines are built from.
  * by cycle position: months since the last halving, in quarters, because
    "does the rule work only after a halving" is the most-asked question and
    the base rate by cycle position is the first thing to check
  * by starting MVRV quartile: the same outcomes conditioned on where the
    cycle was when the window opened

WHAT IS NOT
  * any conditioning on a signal. That is the scorecard's job.
  * any claim about the future. A forward-return distribution is a fact about
    the past, drawn from four cycles of overlapping windows.
"""
import io, json, os, sys, bisect, datetime as dt

OUT = os.environ.get('DATA_DIR', 'data')
SCHEMA_VERSION = '1.0'
FROM = dt.date(2013, 1, 1)
HORIZONS = (30, 90, 180, 365)
HALVINGS = [dt.date(2012, 11, 28), dt.date(2016, 7, 9), dt.date(2020, 5, 11), dt.date(2024, 4, 20), dt.date(2028, 4, 15)]
CYCLES = [('2013-01-01', '2016-12-31', '2013-16'), ('2017-01-01', '2019-12-31', '2017-19'),
          ('2020-01-01', '2023-12-31', '2020-23'), ('2024-01-01', '2029-12-31', '2024-')]


def _q(v, p):
    s = sorted(v)
    return s[min(len(s) - 1, int(p * (len(s) - 1)))] if s else None


def _pct(v):
    return {'p5': _q(v, .05), 'p25': _q(v, .25), 'median': _q(v, .5), 'p75': _q(v, .75), 'p95': _q(v, .95), 'n': len(v)}


def _load(name):
    p = os.path.join(OUT, name + '.json')
    return json.load(io.open(p, encoding='utf-8')) if os.path.exists(p) else None


def months_since_halving(d):
    prev = max((h for h in HALVINGS if h <= d), default=None)
    return None if prev is None else (d - prev).days / 30.4375


def main():
    bc = _load('blockchain')
    if not bc:
        print('  baserate: blockchain.json not available'); return 1
    px = {str(d)[:10]: float(v) for d, v in bc['series']['price'] if v and float(v) > 0}
    days = [d for d in sorted(px) if d >= FROM.isoformat()]
    D = [dt.date.fromisoformat(d) for d in days]
    P = [px[d] for d in days]
    n = len(days)
    if n < 1500:
        print(f'  baserate: only {n} days, refusing'); return 1

    mv = None
    cm = _load('coinmetrics')
    if cm and cm['series'].get('CapMVRVCur'):
        mv = {str(d)[:10]: float(v) for d, v in cm['series']['CapMVRVCur'] if v}

    # forward returns, forward min and max, per horizon
    fwd = {h: [] for h in HORIZONS}
    fall40 = []; double = []
    cyc = []; msh = []; mvq = []
    mv_all = sorted(mv[d] for d in days if mv and d in mv) if mv else []
    q1, q2, q3 = (_q(mv_all, .25), _q(mv_all, .5), _q(mv_all, .75)) if mv_all else (None, None, None)
    for i in range(n):
        row = {}
        for h in HORIZONS:
            if i + h < n:
                fwd[h].append((i, P[i + h] / P[i] - 1))
        if i + 365 < n:
            w = P[i + 1:i + 366]
            fall40.append((i, min(w) <= P[i] * 0.60))
            double.append((i, max(w) >= P[i] * 2.0))
    cycle_of = {}
    for a, b, lab in CYCLES:
        for i, d in enumerate(days):
            if a <= d <= b:
                cycle_of[i] = lab

    def by_group(pairs, keyfn):
        g = {}
        for i, v in pairs:
            k = keyfn(i)
            if k is None: continue
            g.setdefault(k, []).append(v)
        return g

    out = {'schema_version': SCHEMA_VERSION,
           'generated_at': dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
           'as_of': days[-1], 'from': days[0], 'days': n,
           'note': ('Unconditional: every day since 2013 is a window start. Windows overlap heavily, so the '
                    'effective sample is the number of independent regimes, not the number of days. These are '
                    'the base rates the scorecard measures each rule against; the scorecard then restricts them '
                    'to the days a rule could have fired, which is why its baselines differ from the figures here.'),
           'forward_return_pct': {}, 'outcomes': {}, 'by_cycle': {}, 'by_months_since_halving': {},
           'by_starting_mvrv_quartile': {}}

    # 1. forward-return distributions
    for h in HORIZONS:
        vals = [100 * v for _, v in fwd[h]]
        out['forward_return_pct'][str(h)] = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in _pct(vals).items()}
        out['forward_return_pct'][str(h)]['share_positive'] = round(100 * sum(1 for v in vals if v > 0) / len(vals), 1)

    # 2. the two scorecard outcomes, unconditionally
    out['outcomes']['fall_40pct_within_365d'] = {'rate_pct': round(100 * sum(v for _, v in fall40) / len(fall40), 1), 'n': len(fall40)}
    out['outcomes']['double_within_365d'] = {'rate_pct': round(100 * sum(v for _, v in double) / len(double), 1), 'n': len(double)}

    # 3. by starting cycle
    for lab in [c[2] for c in CYCLES]:
        g365 = [100 * v for i, v in fwd[365] if cycle_of.get(i) == lab]
        f = [v for i, v in fall40 if cycle_of.get(i) == lab]; d2 = [v for i, v in double if cycle_of.get(i) == lab]
        if len(g365) > 60:
            out['by_cycle'][lab] = {'fwd365_median_pct': round(_q(g365, .5), 1), 'fwd365_p25_pct': round(_q(g365, .25), 1),
                                    'fwd365_p75_pct': round(_q(g365, .75), 1), 'n': len(g365),
                                    'fall40_pct': round(100 * sum(f) / len(f), 1) if f else None,
                                    'double_pct': round(100 * sum(d2) / len(d2), 1) if d2 else None}

    # 4. by months since halving, in six-month bins
    def bin_msh(i):
        m = months_since_halving(D[i])
        if m is None or m >= 48: return None
        return f'{int(m // 6) * 6:02d}-{int(m // 6) * 6 + 6:02d}'
    g = by_group([(i, 100 * v) for i, v in fwd[365]], bin_msh)
    gf = by_group(fall40, bin_msh); gd = by_group(double, bin_msh)
    for k in sorted(g):
        if len(g[k]) > 60:
            out['by_months_since_halving'][k] = {'fwd365_median_pct': round(_q(g[k], .5), 1), 'n': len(g[k]),
                                                 'fall40_pct': round(100 * sum(gf.get(k, [])) / max(1, len(gf.get(k, []))), 1),
                                                 'double_pct': round(100 * sum(gd.get(k, [])) / max(1, len(gd.get(k, []))), 1)}

    # 5. by starting MVRV quartile
    if mv_all:
        def quart(i):
            d = days[i]
            if d not in mv: return None
            x = mv[d]
            return 'Q1 lowest' if x <= q1 else 'Q2' if x <= q2 else 'Q3' if x <= q3 else 'Q4 highest'
        g = by_group([(i, 100 * v) for i, v in fwd[365]], quart)
        gf = by_group(fall40, quart); gd = by_group(double, quart)
        for k in ('Q1 lowest', 'Q2', 'Q3', 'Q4 highest'):
            if k in g and len(g[k]) > 60:
                out['by_starting_mvrv_quartile'][k] = {
                    'fwd365_median_pct': round(_q(g[k], .5), 1), 'n': len(g[k]),
                    'fall40_pct': round(100 * sum(gf.get(k, [])) / max(1, len(gf.get(k, []))), 1),
                    'double_pct': round(100 * sum(gd.get(k, [])) / max(1, len(gd.get(k, []))), 1)}
        out['mvrv_quartile_bounds'] = {'q1': round(q1, 3), 'median': round(q2, 3), 'q3': round(q3, 3)}

    os.makedirs(OUT, exist_ok=True)
    tmp = os.path.join(OUT, 'baserate.json.tmp')
    io.open(tmp, 'w', encoding='utf-8').write(json.dumps(out, separators=(',', ':')))
    os.replace(tmp, os.path.join(OUT, 'baserate.json'))
    o = out['outcomes']
    print(f"  baserate: {n} days; 40% fall within a year {o['fall_40pct_within_365d']['rate_pct']}%, "
          f"doubling {o['double_within_365d']['rate_pct']}%")
    return 0


if __name__ == '__main__':
    sys.exit(main())
