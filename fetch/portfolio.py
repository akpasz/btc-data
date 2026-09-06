"""fetch/portfolio.py - what different bitcoin weights did, and how much that depended on when you started.

Writes data/portfolio.json.

WHAT THIS IS FOR

Every other page on this site asks whether a signal tells you when to buy. The
answer, across fifteen tested claims, is mostly no. This asks the question that
remains: how much to hold.

WHAT IT DELIBERATELY DOES NOT DO

No optimiser, no recommended weight, no efficient frontier point. Mean-variance
optimisation on 134 monthly observations produces weights that swing wildly on
tiny changes in assumed return, and a single "optimal" number would be the
asset-allocation equivalent of a flashing buy signal.

Instead: a ladder of fixed weights, each measured, each shown across several
start dates. The spread between start dates is the finding. It is usually
larger than the spread between weights, which is the honest answer to "how
much should I hold": over this history the outcome depended more on when you
started than on the size you chose.

THE SAMPLE

134 monthly observations from July 2015, which is about one and a half bitcoin
cycles. Every figure here is descriptive. Bitcoin's history is also the history
of an asset going from $300 to $80,000, so any weight looks good in aggregate;
the start-date table is there to show how unevenly that was distributed.

There are no bonds in the data, so the traditional base is 60% equities and
40% gold rather than the usual 60/40 stocks and bonds. That is a real
limitation and it is stated on the page.
"""
import io, json, math, os, sys, urllib.request, datetime

BASES = ['https://akpasz.github.io/btc-data/data/',
         'https://raw.githubusercontent.com/akpasz/btc-data/main/data/']
LOCAL = os.environ.get('SNAP_DIR')
OUT = os.environ.get('DATA_DIR', 'data')
SCHEMA_VERSION = '1.0'

WEIGHTS = [0, 1, 2, 5, 10, 20]          # per cent in bitcoin
BASE = {'equities': 0.60, 'gold': 0.40}  # no bond series available
RF_ANNUAL = 0.02                         # a flat, stated assumption


def load(name):
    if LOCAL and os.path.exists(f'{LOCAL}/{name}.json'):
        return json.load(io.open(f'{LOCAL}/{name}.json', encoding='utf-8'))
    for b in BASES:
        try:
            with urllib.request.urlopen(b + name + '.json', timeout=40) as r:
                return json.loads(r.read().decode())
        except Exception:
            pass
    return None


def to_monthly(points):
    """Mean of the observations in each calendar month.

    This has to be an average, not a month-end close, because the equity and
    gold series in relative.json ARE monthly averages and cannot be anything
    else - there is no daily gold series in the data. Checked rather than
    assumed: sp500_monthly returns correlate 0.933 with the Nasdaq monthly
    average and only 0.594 with its month-end close, and gold_usd_monthly
    correlates 0.995 with a daily gold proxy's monthly average against 0.801
    with its close, matching on levels to within 0.3%.

    Taking bitcoin's last daily price while equities and gold were averages
    mixed two conventions: it overstated bitcoin's volatility relative to the
    others and put the observations half a month apart. Averaging everything
    is consistent. It also smooths, so volatility and drawdown are understated
    for every asset alike - which the page states.
    """
    tot, cnt = {}, {}
    for d, v in points:
        if v is None:
            continue
        k = str(d)[:7]
        tot[k] = tot.get(k, 0.0) + float(v)
        cnt[k] = cnt.get(k, 0) + 1
    return {k: tot[k] / cnt[k] for k in tot}


def returns(levels, months):
    """Simple monthly returns, aligned to a month list."""
    out = []
    for i in range(1, len(months)):
        a, b = levels[months[i - 1]], levels[months[i]]
        out.append(b / a - 1 if a else 0.0)
    return out


def path(rets_by_asset, weights, months, rebalance='monthly'):
    """Growth of 1 unit. Monthly rebalancing back to the target weights, or
    buy-and-hold where the weights drift with performance."""
    names = list(weights)
    if rebalance == 'monthly':
        v = 1.0
        series = [1.0]
        for i in range(len(months) - 1):
            step = sum(weights[n] * (1 + rets_by_asset[n][i]) for n in names)
            v *= step
            series.append(v)
        return series
    holdings = {n: weights[n] for n in names}
    series = [1.0]
    for i in range(len(months) - 1):
        for n in names:
            holdings[n] *= (1 + rets_by_asset[n][i])
        series.append(sum(holdings.values()))
    return series


def stats(series, months):
    """Annualised return, volatility, Sharpe, Sortino, max drawdown, and the
    longest time spent below a previous peak."""
    n = len(series) - 1
    if n < 12:
        return None
    total = series[-1] / series[0]
    years = n / 12.0
    cagr = total ** (1 / years) - 1
    rets = [series[i + 1] / series[i] - 1 for i in range(n)]
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    vol = math.sqrt(var) * math.sqrt(12)
    rf_m = (1 + RF_ANNUAL) ** (1 / 12) - 1
    excess = [r - rf_m for r in rets]
    ex_mean = sum(excess) / n
    sharpe = (ex_mean * 12) / vol if vol else None
    # Sortino penalises only downside deviation, which flatters a positively
    # skewed asset like bitcoin. Both are reported so the gap is visible.
    down = [min(0.0, e) for e in excess]
    dvar = sum(d * d for d in down) / n
    dvol = math.sqrt(dvar) * math.sqrt(12)
    sortino = (ex_mean * 12) / dvol if dvol else None
    peak = series[0]
    mdd = 0.0
    under = 0
    longest = 0
    for v in series:
        if v >= peak:
            peak = v
            under = 0
        else:
            under += 1
            longest = max(longest, under)
        mdd = min(mdd, v / peak - 1)
    return {
        'months': n, 'total_return_x': round(total, 4),
        'cagr_pct': round(cagr * 100, 2), 'vol_pct': round(vol * 100, 2),
        'sharpe': None if sharpe is None else round(sharpe, 3),
        'sortino': None if sortino is None else round(sortino, 3),
        'max_drawdown_pct': round(mdd * 100, 1),
        'longest_underwater_months': longest,
        'start': months[0], 'end': months[-1],
    }


def build(assets, months, btc_pct, rebalance='monthly'):
    w = {}
    b = btc_pct / 100.0
    for k, share in BASE.items():
        w[k] = share * (1 - b)
    w['btc'] = b
    missing = [k for k in w if w[k] > 0 and k not in assets]
    if missing:
        # Dropping a missing leg used to leave the weights summing to 0.64 while
        # the result was still reported as a whole portfolio. A partial portfolio
        # is not a smaller portfolio, it is a wrong one.
        raise ValueError(f'missing asset(s) {missing}; refusing to build a partial portfolio')
    w = {k: v for k, v in w.items() if v > 0}
    total = sum(w.values())
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f'weights sum to {total}, not 1')
    rets = {n: returns(assets[n], months) for n in w}
    return stats(path(rets, w, months, rebalance), months), w


def main():
    rel = load('relative')
    if not rel:
        print('  portfolio: relative.json not available, skipping')
        return 1
    s = rel['series']
    assets = {}
    if 'sp500_monthly' in s:
        assets['equities'] = to_monthly(s['sp500_monthly'])
    if 'gold_usd_monthly' in s:
        assets['gold'] = to_monthly(s['gold_usd_monthly'])
    if 'btc_usd' in s:
        assets['btc'] = to_monthly(s['btc_usd'])
    if 'eth_usd' in s:
        assets['eth'] = to_monthly(s['eth_usd'])
    need = ['equities', 'gold', 'btc']
    if any(k not in assets for k in need):
        print('  portfolio: missing one of equities, gold, btc')
        return 1
    months = sorted(set.intersection(*[set(assets[k]) for k in need]))
    if len(months) < 60:
        print(f'  portfolio: only {len(months)} overlapping months, refusing to publish')
        return 1

    ladder = []
    for pct in WEIGHTS:
        st, w = build(assets, months, pct)
        if st:
            ladder.append({'btc_pct': pct, 'weights': {k: round(v, 4) for k, v in w.items()},
                           **st})

    # Start-date sensitivity. The same weight entered in different years, held
    # to the end. This is the point of the page.
    starts = []
    for y in range(int(months[0][:4]), int(months[-1][:4]) - 2):
        window = [m for m in months if m >= f'{y}-01']
        if len(window) < 36:
            continue
        row = {'start_year': y, 'months': len(window) - 1, 'by_weight': {}}
        for pct in WEIGHTS:
            st, _ = build(assets, window, pct)
            if st:
                row['by_weight'][str(pct)] = {
                    'cagr_pct': st['cagr_pct'], 'max_drawdown_pct': st['max_drawdown_pct'],
                    'sharpe': st['sharpe'], 'sortino': st['sortino']}
        starts.append(row)

    # Rebalancing: does putting the weight back matter?
    rebal = []
    for pct in WEIGHTS:
        a, _ = build(assets, months, pct, 'monthly')
        b, _ = build(assets, months, pct, 'hold')
        if a and b:
            rebal.append({'btc_pct': pct, 'rebalanced': a, 'buy_and_hold': b})

    # Rolling 36-month correlation of bitcoin to each of the others.
    roll = {}
    for other in ('equities', 'gold'):
        pairs = []
        rb = returns(assets['btc'], months)
        ro = returns(assets[other], months)
        for i in range(35, len(rb)):
            x = rb[i - 35:i + 1]
            y = ro[i - 35:i + 1]
            mx, my = sum(x) / len(x), sum(y) / len(y)
            num = sum((a - mx) * (b - my) for a, b in zip(x, y))
            dx = math.sqrt(sum((a - mx) ** 2 for a in x))
            dy = math.sqrt(sum((b - my) ** 2 for b in y))
            if dx and dy:
                pairs.append([months[i + 1], round(num / (dx * dy), 3)])
        roll[other] = pairs

    # Publish the aligned monthly series so the page can recompute any weight,
    # base mix, start date or rebalancing rule in the browser from EXACTLY the
    # numbers used here. Without this the page would re-derive them from
    # relative.json and could silently drift from the published figures.
    series_out = {k: [[m, round(assets[k][m], 6)] for m in months]
                  for k in ('equities', 'gold', 'btc') if k in assets}
    if 'eth' in assets:
        eth_months = [m for m in months if m in assets['eth']]
        if len(eth_months) > 60:
            series_out['eth'] = [[m, round(assets['eth'][m], 6)] for m in eth_months]

    out = {
        'schema_version': SCHEMA_VERSION,
        'monthly': series_out,
        'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
        'frequency': 'monthly-average',
        'months': len(months), 'first_month': months[0], 'last_month': months[-1],
        'base': BASE, 'risk_free_annual_pct': RF_ANNUAL * 100,
        'note': ('Descriptive, in sample, and short. 134 monthly observations is about '
                 'one and a half bitcoin cycles. There are no bonds in the data, so the '
                 'traditional base is 60% equities and 40% gold. Sortino penalises only '
                 'downside deviation, which flatters a positively skewed asset; both it '
                 'and Sharpe are reported so the gap is visible. Every series is a '
                 'monthly average, because the equity and gold series can only be that; '
                 'averaging smooths, so volatility and drawdown are understated for all '
                 'assets alike. Nothing here is a recommendation, and past distributions '
                 'are not forecasts.'),
        'ladder': ladder, 'start_dates': starts, 'rebalancing': rebal,
        'rolling_corr_36m': roll,
    }
    os.makedirs(OUT, exist_ok=True)
    tmp = os.path.join(OUT, 'portfolio.json.tmp')
    io.open(tmp, 'w', encoding='utf-8').write(json.dumps(out, separators=(',', ':')))
    os.replace(tmp, os.path.join(OUT, 'portfolio.json'))
    print(f'  portfolio: {len(months)} months, {len(ladder)} weights, '
          f'{len(starts)} start years')
    return 0


if __name__ == '__main__':
    sys.exit(main())
