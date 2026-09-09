"""fetch/crossasset.py - does the rule work anywhere, or only in bitcoin?
Writes data/crossasset.json.

THE QUESTION

Two of fifteen scorecard rules have enough history to judge, and both are RSI.
Four cycles cannot accumulate a sample for once-per-cycle signals. The only
honest route to more episodes is more assets: if RSI-oversold fails in
bitcoin, ether, Solana, the Nasdaq and the S&P alike, it is falsified as a
rule rather than as a bitcoin curiosity. If it holds in all five, that is the
strongest claim on the internet about it.

THE METHODOLOGY DECISION, stated

The scorecard's outcomes - a doubling or a 40% fall within a year - are
bitcoin-scale. Equities never double in a year, so applying them across assets
scores 0% for every episode AND 0% for the baseline, which means nothing.

Here the outcome is defined relative to EACH ASSET'S OWN DISTRIBUTION of
one-year forward returns: for a bottom rule, a hit is a forward return above
that asset's 80th percentile; for a top rule, below its 20th. The percentile is
computed over the asset's own eligible days. A rule therefore has to beat the
asset's own base rate, which is what "does it work anywhere" means.

The 80/20 cut is a choice. It was fixed before any result was computed and it
is the same for every asset and every rule. Its consequence: the unconditional
base rate is 20% by construction for every asset, and every result is read
against that.

EVERYTHING ELSE IS THE SCORECARD'S ENGINE: episode clustering at 90 days, the
transition-matched baseline, Wilson at 90%, twenty episodes for a verdict.
Pooling across assets sums episodes and hits; the pooled interval is Wilson on
the pooled counts, which treats episodes in different assets as independent -
a defensible assumption for equities against crypto, a weaker one for BTC
against ETH, and the page says so.
"""
import io, json, math, os, sys, bisect, datetime as dt

OUT = os.environ.get('DATA_DIR', 'data')
SCHEMA_VERSION = '1.0'
HORIZON = 365
CLUSTER = 90
MIN_EPISODES = 20
PCT_HI, PCT_LO = 0.80, 0.20
ASSETS = [('btc', 'btc_usd', 'Bitcoin'), ('eth', 'eth_usd', 'Ether'), ('sol', 'sol_usd', 'Solana'),
          ('nasdaq', 'nasdaq_daily', 'Nasdaq'), ('sp500', 'sp500_daily', 'S&P 500')]


def sma(v, n):
    out = [None] * len(v); s = 0.0
    for i, x in enumerate(v):
        s += x
        if i >= n: s -= v[i - n]
        if i >= n - 1: out[i] = s / n
    return out


def rsi(v, n=14):
    out = [None] * len(v)
    if len(v) < n + 1: return out
    g = l = 0.0
    for i in range(1, n + 1):
        d = v[i] - v[i - 1]; g += max(d, 0); l += max(-d, 0)
    g /= n; l /= n
    out[n] = 100 - 100 / (1 + (g / l if l else 999))
    for i in range(n + 1, len(v)):
        d = v[i] - v[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n; l = (l * (n - 1) + max(-d, 0)) / n
        out[i] = 100 - 100 / (1 + (g / l if l else 999))
    return out


def wilson(hit, n, z=1.6449):
    if n < 2: return None
    p = hit / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = (z / d) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [round(max(0.0, c - h) * 100, 1), round(min(1.0, c + h) * 100, 1)]


def forward_return(px, i, dates):
    """One-year forward return by CALENDAR days, not index steps - equities
    trade about 252 days a year and crypto 365, and an index horizon would
    silently give equities a 17-month window."""
    target = dates[i] + dt.timedelta(days=HORIZON)
    j = bisect.bisect_left(dates, target)
    if j >= len(px): return None
    return px[j] / px[i] - 1


def score_relative(dates, px, fires, eligible, direction, fwd, cut):
    """The scorecard's score(), with the outcome replaced by a percentile cut
    of the asset's own forward-return distribution."""
    n = len(px)
    def outcome(i):
        r = fwd[i]
        if r is None: return None
        return (r <= cut) if direction == 'top' else (r >= cut)
    eps = []; i = 0
    while i < n:
        if fires[i] and eligible[i]:
            start = i; last = i; j = i + 1
            while j < n and (dates[j] - dates[last]).days <= CLUSTER:
                if fires[j] and eligible[j]: last = j
                j += 1
            eps.append((start, last)); i = last + 1
        else: i += 1
    hit = tot = cens = 0
    for s, _ in eps:
        o = outcome(s)
        if o is None: cens += 1; continue
        tot += 1; hit += 1 if o else 0
    excl = [False] * n
    for s, e in eps:
        k = s
        while k < n and (dates[k] - dates[e]).days <= CLUSTER:
            excl[k] = True; k += 1
        for k in range(s, e + 1): excl[k] = True
    bh = bt = 0
    for i in range(n):
        if not eligible[i] or excl[i]: continue
        o = outcome(i)
        if o is None: continue
        bt += 1; bh += 1 if o else 0
    return {'episodes': tot, 'censored': cens, 'hits': hit,
            'hit_rate': round(100 * hit / tot, 1) if tot else None,
            'hit_rate_ci90': wilson(hit, tot),
            'baseline_rate': round(100 * bh / bt, 1) if bt else None, 'baseline_days': bt, 'baseline_hits': bh}


def rules_for(px, dates):
    n = len(px)
    ma50, ma200 = sma(px, 50), sma(px, 200)
    r = rsi(px, 14)
    e_r = [r[i] is not None for i in range(n)]
    e_m = [ma200[i] is not None for i in range(n)]
    e_x = [ma50[i] is not None and ma200[i] is not None for i in range(n)]
    return {
        'rsi_cold':   ('RSI-14 below 30', 'bottom', [bool(e_r[i] and r[i] < 30) for i in range(n)], e_r),
        'rsi_hot':    ('RSI-14 above 70', 'top',    [bool(e_r[i] and r[i] > 70) for i in range(n)], e_r),
        'mayer_low':  ('Price 20% below its 200-day average', 'bottom', [bool(e_m[i] and px[i] / ma200[i] < 0.8) for i in range(n)], e_m),
        'mayer_high': ('Price 40% above its 200-day average', 'top', [bool(e_m[i] and px[i] / ma200[i] > 1.4) for i in range(n)], e_m),
        'golden':     ('Golden cross', 'bottom', [bool(e_x[i] and i > 0 and ma50[i-1] is not None and ma200[i-1] is not None and ma50[i] > ma200[i] and ma50[i-1] <= ma200[i-1]) for i in range(n)], e_x),
        'death':      ('Death cross', 'top', [bool(e_x[i] and i > 0 and ma50[i-1] is not None and ma200[i-1] is not None and ma50[i] < ma200[i] and ma50[i-1] >= ma200[i-1]) for i in range(n)], e_x),
    }


def main():
    p = os.path.join(OUT, 'relative.json')
    if not os.path.exists(p):
        print('  crossasset: relative.json not available'); return 1
    rel = json.load(io.open(p, encoding='utf-8'))['series']
    out = {'schema_version': SCHEMA_VERSION,
           'generated_at': dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
           'outcome': {'bottom': f'one-year forward return above the asset\'s own {int(PCT_HI*100)}th percentile',
                       'top': f'one-year forward return below the asset\'s own {int(PCT_LO*100)}th percentile',
                       'base_rate_by_construction_pct': 20.0},
           'assets': {}, 'rules': {},
           'rules_tested': 6, 'expected_false_positives_at_90pct': 0.6,
           'bitcoin_note': ('The bitcoin row here is NOT the scorecard\'s test. The outcome is the 80th/20th '
                            'percentile of bitcoin\'s own forward returns rather than a doubling or a 40% fall, and the '
                            'series starts in 2015 rather than 2009. It answers "does the rule beat bitcoin\'s own base '
                            'rate" on the same footing as the other four assets; the scorecard answers a different question.'),
           'note': __doc__.strip().split('\n\n')[2]}
    per = {}
    for key, src, name in ASSETS:
        pts = [(dt.date.fromisoformat(str(d)[:10]), float(v)) for d, v in rel.get(src, []) if v and float(v) > 0]
        if len(pts) < 800: continue
        pts.sort(); dates = [d for d, _ in pts]; px = [v for _, v in pts]
        fwd = [forward_return(px, i, dates) for i in range(len(px))]
        valid = sorted(x for x in fwd if x is not None)
        cut_hi = valid[int(PCT_HI * (len(valid) - 1))]; cut_lo = valid[int(PCT_LO * (len(valid) - 1))]
        out['assets'][key] = {'name': name, 'source': src, 'days': len(px), 'from': dates[0].isoformat(), 'to': dates[-1].isoformat(),
                              'fwd365_p80_pct': round(100 * cut_hi, 1), 'fwd365_p20_pct': round(100 * cut_lo, 1),
                              'fwd365_median_pct': round(100 * valid[len(valid) // 2], 1)}
        per[key] = (dates, px, fwd, cut_hi, cut_lo)

    for rk, spec_fn in [(k, None) for k in ('rsi_cold', 'rsi_hot', 'mayer_low', 'mayer_high', 'golden', 'death')]:
        by_asset = {}; pooled_hit = pooled_tot = pooled_bh = pooled_bt = 0; name = direction = None
        for key in per:
            dates, px, fwd, cut_hi, cut_lo = per[key]
            rules = rules_for(px, dates)
            name, direction, fires, elig = rules[rk]
            cut = cut_lo if direction == 'top' else cut_hi
            s = score_relative(dates, px, fires, elig, direction, fwd, cut)
            by_asset[key] = s
            pooled_hit += s['hits']; pooled_tot += s['episodes']; pooled_bh += s['baseline_hits']; pooled_bt += s['baseline_days']
        pooled = {'episodes': pooled_tot, 'hits': pooled_hit,
                  'hit_rate': round(100 * pooled_hit / pooled_tot, 1) if pooled_tot else None,
                  'hit_rate_ci90': wilson(pooled_hit, pooled_tot),
                  'baseline_rate': round(100 * pooled_bh / pooled_bt, 1) if pooled_bt else None, 'baseline_days': pooled_bt}
        diff = (pooled['hit_rate'] - pooled['baseline_rate']) if (pooled['hit_rate'] is not None and pooled['baseline_rate'] is not None) else None
        pooled['difference'] = round(diff, 1) if diff is not None else None
        pooled['verdict'] = ('Not enough episodes to score' if pooled_tot < MIN_EPISODES else 'Not scored' if diff is None
                             else 'Beats the baseline' if diff >= 5 else 'Worse than the baseline' if diff <= -5 else 'Indistinguishable')
        # Does the effect run the same way in every asset, or does the pooled
        # figure average a win in one market against a loss in another? This
        # is a SIGN check on assets with at least five episodes - not a
        # significance test - and a pooled "beats" is downgraded to "mixed" if
        # any judged asset runs the other way by more than five points. The
        # death cross beats its pooled baseline by eight points and REVERSES
        # in the Nasdaq by eleven; without this check the page would call it
        # a rule that works.
        judged = {k: v for k, v in by_asset.items() if v['episodes'] >= 5 and v['hit_rate'] is not None and v['baseline_rate'] is not None}
        signs = {k: (v['hit_rate'] - v['baseline_rate']) for k, v in judged.items()}
        pooled['assets_judged'] = len(judged)
        pooled['by_asset_difference'] = {k: round(x, 1) for k, x in signs.items()}
        if len(signs) >= 2 and diff is not None:
            pooled_sign = 1 if diff > 0 else -1
            contrary = [k for k, x in signs.items() if x * pooled_sign < -5]
            pooled['consistent_direction'] = not contrary
            pooled['contrary_assets'] = contrary
            if contrary and pooled['verdict'] in ('Beats the baseline', 'Worse than the baseline'):
                pooled['verdict'] = 'Mixed: pooled result reverses in ' + ', '.join(contrary)
        else:
            pooled['consistent_direction'] = None; pooled['contrary_assets'] = []
        out['rules'][rk] = {'name': name, 'direction': direction, 'by_asset': by_asset, 'pooled': pooled}

    os.makedirs(OUT, exist_ok=True)
    tmp = os.path.join(OUT, 'crossasset.json.tmp')
    io.open(tmp, 'w', encoding='utf-8').write(json.dumps(out, separators=(',', ':')))
    os.replace(tmp, os.path.join(OUT, 'crossasset.json'))
    print(f'  crossasset: {len(out["assets"])} assets, {len(out["rules"])} rules; RSI<30 pooled '
          f'{out["rules"]["rsi_cold"]["pooled"]["episodes"]} episodes, {out["rules"]["rsi_cold"]["pooled"]["verdict"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
