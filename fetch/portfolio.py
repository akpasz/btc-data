"""fetch/portfolio.py - what different bitcoin weights did, and how much that depended on when you started.

Writes data/portfolio.json.

WHAT THIS IS FOR

Every other page on this site asks whether a signal tells you when to buy. The
answer, across fifteen tested claims, is mostly no. This asks the question that
remains: how much to hold.

WHAT IT DELIBERATELY DOES NOT DO

No optimiser, no recommended weight, no efficient frontier point. Mean-variance
optimisation on about 134 monthly returns produces weights that swing wildly on
tiny changes in assumed return, and a single "optimal" number would be the
asset-allocation equivalent of a flashing buy signal.

Instead: a ladder of fixed weights, each measured, each shown across several
start dates and every rolling window. The spread between start dates is the
finding.

THE BASE (schema 2)

Version 1 measured bitcoin inside 60% equities and 40% gold, with the equity
leg a price index (no dividends) and a flat 2% risk-free rate. An outside
review asked for a conventional benchmark and a total-return equity leg, and
both are now possible from data already in the pipeline:

  equities_tr  S&P 500 total return: Shiller's monthly price plus his
               annualised dividend / 12 each month. Shiller's dividend column
               stops before his price column; the tail is extended at the
               dividend's own trailing ten-year growth rate and the first
               estimated month is published.
  bonds        a constant-maturity 10-year US Treasury, priced each month from
               FRED DGS10: last month's yield as coupon, repriced at this
               month's yield with ten years less a month to run. A standard
               proxy for a bond index, not a fund you could buy.
  cash         effective fed funds rate, monthly average, used as the
               risk-free rate in Sharpe and Sortino month by month.

Three bases are published: 60/40 stocks and bonds (the default), 60/40 stocks
and gold, and the version-1 base (price-only stocks and gold) for continuity.

THE SAMPLE

Monthly averages from July 2015, about one and a half bitcoin cycles. The
equity and gold series exist only as monthly averages, so everything is
averaged to match; averaging smooths, so volatility and drawdown are
understated. daily_check measures by how much, on daily closes, for the legs
that have a daily series.
"""
import io, json, math, os, sys, urllib.request, datetime

BASES_URL = ['https://akpasz.github.io/btc-data/data/',
             'https://raw.githubusercontent.com/akpasz/btc-data/main/data/']
BASES = BASES_URL  # kept for anything importing the old name
LOCAL = os.environ.get('SNAP_DIR')
OUT = os.environ.get('DATA_DIR', 'data')
SCHEMA_VERSION = '2.0'

WEIGHTS = [0, 1, 2, 5, 10, 20]          # per cent in bitcoin
BASE_MIXES = {
    'stocks_bonds': {'label': '60% stocks, 40% bonds',
                     'weights': {'equities_tr': 0.60, 'bonds': 0.40}},
    'stocks_gold': {'label': '60% stocks, 40% gold',
                    'weights': {'equities_tr': 0.60, 'gold': 0.40}},
    'stocks_gold_price': {'label': '60% stocks without dividends, 40% gold (the original base)',
                          'weights': {'equities': 0.60, 'gold': 0.40}},
}
DEFAULT_BASE = 'stocks_bonds'
BASE = BASE_MIXES[DEFAULT_BASE]['weights']
RF_ANNUAL = 0.02            # fallback only, when no cash series is supplied
BOND_YEARS = 10.0
ROLL_WINDOWS = (36, 60, 120)
ROLL_WEIGHT = 10
MIN_ENTRY_MONTHS = 36       # an entry year needs three years of returns to be shown


def load(name, remote_only=False):
    """This run's file first (fetch_all writes relative.json and fred.json
    before this layer runs), then a local snapshot, then the published copy.
    Reading the published copy first would use yesterday's inputs."""
    if not remote_only:
        for d in (OUT, LOCAL):
            if d and os.path.exists(f'{d}/{name}.json'):
                return json.load(io.open(f'{d}/{name}.json', encoding='utf-8'))
    for b in BASES_URL:
        try:
            with urllib.request.urlopen(b + name + '.json', timeout=40) as r:
                return json.loads(r.read().decode())
        except Exception:
            pass
    return None


def to_monthly(points):
    """Mean of the observations in each calendar month.

    This has to be an average, not a month-end close, because the equity and
    gold series ARE monthly averages and cannot be anything else; there is no
    daily gold series in the data. Checked rather than assumed: sp500_monthly
    returns correlate 0.933 with the Nasdaq monthly average and only 0.594 with
    its month-end close."""
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


def bond_price(coupon, yld, years):
    """Price per 1 of face of a semi-annual bond. coupon and yld are decimals."""
    n = 2.0 * years
    if abs(yld) < 1e-12:
        return coupon / 2.0 * n + 1.0
    v = (1 + yld / 2.0) ** (-n)
    return coupon / yld * (1 - v) + v


def bond_month_return(y0, y1, years=BOND_YEARS):
    """One month's total return on a constant-maturity bond bought at par.

    Bought last month at par with coupon y0; a month later it has years - 1/12
    to run and is repriced at y1. Return = price change + a month of coupon."""
    return bond_price(y0, y1, years - 1.0 / 12.0) - 1.0 + y0 / 12.0


def bond_levels(yield_pct_by_month, months):
    """A level series (start 1.0) from monthly yields in per cent."""
    lv = {months[0]: 1.0}
    for i in range(1, len(months)):
        r = bond_month_return(yield_pct_by_month[months[i - 1]] / 100.0,
                              yield_pct_by_month[months[i]] / 100.0)
        lv[months[i]] = lv[months[i - 1]] * (1 + r)
    return lv


def extend_dividends(div_by_month, months):
    """Fill months after Shiller's last dividend at its trailing 10-year growth.

    Returns (filled map, first estimated month or None, growth used)."""
    known = sorted(m for m in div_by_month if div_by_month[m] and div_by_month[m] > 0)
    if not known:
        raise ValueError('no dividend data')
    last = known[-1]
    y, mo = int(last[:4]), int(last[5:7])
    ten_back = f'{y - 10}-{mo:02d}'
    g = 0.0
    if ten_back in div_by_month and div_by_month[ten_back] > 0:
        g = (div_by_month[last] / div_by_month[ten_back]) ** 0.1 - 1
    out = dict(div_by_month)
    first_est = None
    for m in months:
        if m > last:
            k = (int(m[:4]) - y) * 12 + int(m[5:7]) - mo
            out[m] = div_by_month[last] * (1 + g) ** (k / 12.0)
            first_est = first_est or m
    return out, first_est, g


def total_return_levels(price_by_month, div_by_month, months):
    """Shiller's convention: each month earns price change plus dividend / 12,
    the dividend being an annualised rate per share."""
    lv = {months[0]: 1.0}
    for i in range(1, len(months)):
        p0, p1 = price_by_month[months[i - 1]], price_by_month[months[i]]
        lv[months[i]] = lv[months[i - 1]] * (p1 + div_by_month[months[i]] / 12.0) / p0
    return lv


def cash_monthly(rate_pct_by_month, months):
    """Monthly risk-free return for each period i (months[i] -> months[i+1]),
    taken from the average rate in the month the period starts."""
    return [(1 + rate_pct_by_month[months[i]] / 100.0) ** (1 / 12.0) - 1
            for i in range(len(months) - 1)]


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


def stats(series, months, rf=None):
    """Annualised return, volatility, Sharpe, Sortino, max drawdown, and the
    longest time spent below a previous peak. rf is a list of monthly
    risk-free returns, one per period; without it a flat RF_ANNUAL is used."""
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
    if rf is None:
        rf = [(1 + RF_ANNUAL) ** (1 / 12) - 1] * n
    excess = [r - rf[i] for i, r in enumerate(rets)]
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
    trough = 0
    for i, v in enumerate(series):
        if v >= peak:
            peak = v
            under = 0
        else:
            under += 1
            longest = max(longest, under)
        if v / peak - 1 < mdd:
            mdd = v / peak - 1
            trough = i
    return {
        'months': n, 'total_return_x': round(total, 4),
        'cagr_pct': round(cagr * 100, 2), 'vol_pct': round(vol * 100, 2),
        'sharpe': None if sharpe is None else round(sharpe, 3),
        'sortino': None if sortino is None else round(sortino, 3),
        'max_drawdown_pct': round(mdd * 100, 1),
        'longest_underwater_months': longest,
        'trough': months[trough] if trough < len(months) else months[-1],
        'start': months[0], 'end': months[-1],
    }


def weights_for(btc_pct, base=DEFAULT_BASE):
    b = btc_pct / 100.0
    w = {k: share * (1 - b) for k, share in BASE_MIXES[base]['weights'].items()}
    w['btc'] = b
    return w


def build(assets, months, btc_pct, rebalance='monthly', base=DEFAULT_BASE, cash=None):
    """cash: monthly risk-free rate map (per cent a year) or None for flat."""
    w = weights_for(btc_pct, base)
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
    rf = cash_monthly(cash, months) if cash else None
    return stats(path(rets, w, months, rebalance), months, rf), w


def rolling(assets, months, base, cash, length, pct=ROLL_WEIGHT):
    """Every window of `length` monthly returns: what the weight added to the
    annual return and to the worst fall, against the same base with none."""
    rows = []
    for i in range(0, len(months) - length):
        win = months[i:i + length + 1]
        a, _ = build(assets, win, pct, base=base, cash=cash)
        z, _ = build(assets, win, 0, base=base, cash=cash)
        rows.append([win[0], round(a['cagr_pct'] - z['cagr_pct'], 2),
                     round(a['max_drawdown_pct'] - z['max_drawdown_pct'], 1)])
    if not rows:
        return None
    g = sorted(r[1] for r in rows)
    d = sorted(r[2] for r in rows)
    lo = min(rows, key=lambda r: r[1])
    hi = max(rows, key=lambda r: r[1])
    med = lambda xs: xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2
    return {'months': length, 'windows': len(rows), 'weight_pct': pct,
            'added_return': {'worst': lo[1], 'worst_start': lo[0], 'median': round(med(g), 2),
                             'best': hi[1], 'best_start': hi[0]},
            'added_return_negative_share': round(sum(1 for x in g if x < 0) / len(g), 3),
            'extra_drawdown': {'worst': d[0], 'median': round(med(d), 1), 'least': d[-1]},
            'rows': rows}


def max_drawdown(values):
    peak, mdd = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return mdd * 100


def daily_check(rel, fred, months, weights=(0, 10, 20)):
    """How much monthly averaging understates the worst fall.

    Gold has no daily series, so the stocks-and-gold bases cannot be checked.
    The stocks-and-bonds base can: S&P 500 daily closes (FRED, price only, from
    when FRED's ten-year window starts), the 10-year Treasury repriced daily
    from DGS10, and bitcoin's daily close. Rebalanced on the first common day
    of each month, like the monthly engine. Compared with the monthly-average
    engine over the same months, on the same price-only stock leg, so the only
    difference is the sampling."""
    s = rel.get('series', {}) if rel else {}
    f = fred.get('series', {}) if fred else {}
    sp = {d: v for d, v in s.get('sp500_daily', []) if v}
    bt = {d: v for d, v in s.get('btc_usd', []) if v}
    ty = {d: v for d, v in f.get('treasury_10y', []) if v is not None}
    days = sorted(set(sp) & set(bt) & set(ty))
    if len(days) < 500:
        return None
    # start on the first full month inside the sample
    first_month = days[0][:7]
    days = [d for d in days if d[:7] > first_month and d[:7] <= months[-1]]
    if not days:
        return None
    out = {'from': days[0], 'to': days[-1], 'base': '60% stocks (price only), 40% bonds', 'by_weight': {}}
    # asset-level worst falls, daily close against monthly average, same span
    msp = to_monthly([[d, sp[d]] for d in days])
    mbt = to_monthly([[d, bt[d]] for d in days])
    mm = sorted(msp)
    out['assets'] = {
        'btc': {'daily_close_pct': round(max_drawdown([bt[d] for d in days]), 1),
                'monthly_average_pct': round(max_drawdown([mbt[m] for m in mm]), 1)},
        'stocks': {'daily_close_pct': round(max_drawdown([sp[d] for d in days]), 1),
                   'monthly_average_pct': round(max_drawdown([msp[m] for m in mm]), 1)},
    }
    # daily bond returns: carry for the calendar days elapsed plus repricing
    br = [0.0]
    for i in range(1, len(days)):
        d0 = datetime.date.fromisoformat(days[i - 1]); d1 = datetime.date.fromisoformat(days[i])
        t = (d1 - d0).days / 365.0
        y0, y1 = ty[days[i - 1]] / 100.0, ty[days[i]] / 100.0
        br.append(bond_price(y0, y1, BOND_YEARS - t) - 1.0 + y0 * t)
    mty = to_monthly([[d, ty[d]] for d in days])
    mbond = bond_levels(mty, mm)
    for pct in weights:
        b = pct / 100.0
        w = {'stocks': 0.6 * (1 - b), 'bonds': 0.4 * (1 - b), 'btc': b}
        h = dict(w); v = 1.0; vals = [1.0]
        for i in range(1, len(days)):
            if days[i][:7] != days[i - 1][:7]:
                h = {k: v * w[k] for k in w}          # rebalance at the month's first day
            h['stocks'] *= sp[days[i]] / sp[days[i - 1]]
            h['bonds'] *= 1 + br[i]
            h['btc'] *= bt[days[i]] / bt[days[i - 1]]
            v = sum(h.values()); vals.append(v)
        rets = {'stocks': returns(msp, mm), 'bonds': returns(mbond, mm), 'btc': returns(mbt, mm)}
        ww = {k: x for k, x in w.items() if x > 0}
        mser = path(rets, ww, mm)
        out['by_weight'][str(pct)] = {'daily_close_pct': round(max_drawdown(vals), 1),
                                      'monthly_average_pct': round(max_drawdown(mser), 1)}
    return out


def main():
    rel = load('relative')
    fred = load('fred')
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
    fs = (fred or {}).get('series', {})
    ty = to_monthly(fs['treasury_10y']) if fs.get('treasury_10y') else None
    ff = to_monthly(fs['fed_funds_effective']) if fs.get('fed_funds_effective') else None
    div = to_monthly(s['sp500_dividend_monthly']) if s.get('sp500_dividend_monthly') else None
    if not div:
        # Shiller's file is fetched daily but changes a few times a year; if
        # today's fetch failed, yesterday's published copy is the same data
        prev = load('relative', remote_only=True) or {}
        pd = (prev.get('series') or {}).get('sp500_dividend_monthly')
        if pd:
            div = to_monthly(pd)
            print('  portfolio: dividends from the previously published relative.json')
    if not (ty and ff and div):
        # Raised, not returned: the manifest records it and the workflow
        # refuses to publish, rather than leaving the old-format file in place
        # with every layer reported ok.
        raise RuntimeError('bonds, cash or dividends missing; refusing to publish a v2 file '
                           'that silently falls back to the old base')
    sets = [set(assets[k]) for k in need] + [set(ty), set(ff)]
    months = sorted(set.intersection(*sets))
    if len(months) < 60:
        print(f'  portfolio: only {len(months)} overlapping months, refusing to publish')
        return 1
    div_full, div_est_from, div_g = extend_dividends(div, months)
    assets['equities_tr'] = total_return_levels(assets['equities'], div_full, months)
    assets['bonds'] = bond_levels(ty, months)
    cash = {m: round(ff[m], 4) for m in months}   # exactly what is published

    def ladder_for(base):
        out = []
        for pct in WEIGHTS:
            st, w = build(assets, months, pct, base=base, cash=cash)
            if st:
                out.append({'btc_pct': pct, 'weights': {k: round(v, 4) for k, v in w.items()}, **st})
        return out

    ladders = {b: ladder_for(b) for b in BASE_MIXES}

    # Start-date sensitivity on the default base. The same weight entered in
    # different years, held to the end. This is the point of the page.
    starts = []
    for y in range(int(months[0][:4]), int(months[-1][:4]) + 1):
        window = [m for m in months if m >= f'{y}-01']
        if len(window) - 1 < MIN_ENTRY_MONTHS:
            continue
        row = {'start_year': y, 'months': len(window) - 1, 'by_weight': {}}
        for pct in WEIGHTS:
            st, _ = build(assets, window, pct, cash=cash)
            if st:
                row['by_weight'][str(pct)] = {
                    'cagr_pct': st['cagr_pct'], 'max_drawdown_pct': st['max_drawdown_pct'],
                    'sharpe': st['sharpe'], 'sortino': st['sortino'], 'trough': st['trough']}
        starts.append(row)

    rebal = []
    for pct in WEIGHTS:
        a, _ = build(assets, months, pct, 'monthly', cash=cash)
        b, _ = build(assets, months, pct, 'hold', cash=cash)
        if a and b:
            rebal.append({'btc_pct': pct, 'rebalanced': a, 'buy_and_hold': b})

    roll_windows = {}
    for L in ROLL_WINDOWS:
        r = rolling(assets, months, DEFAULT_BASE, cash, L)
        if r:
            roll_windows[str(L)] = r

    # Rolling 36-month correlation of bitcoin to each of the others.
    roll = {}
    rb = returns(assets['btc'], months)
    for other in ('equities', 'gold', 'bonds'):
        pairs = []
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

    try:
        dchk = daily_check(rel, fred, months)
    except Exception as e:
        dchk = {'error': str(e)[:200]}

    # Publish the aligned monthly series so the page recomputes any weight,
    # base, start date or rebalancing rule from EXACTLY the numbers used here.
    series_out = {k: [[m, round(assets[k][m], 6)] for m in months]
                  for k in ('equities', 'equities_tr', 'bonds', 'gold', 'btc')}
    series_out['cash'] = [[m, round(cash[m], 4)] for m in months]
    if 'eth' in assets:
        eth_months = [m for m in months if m in assets['eth']]
        if len(eth_months) > 60:
            series_out['eth'] = [[m, round(assets['eth'][m], 6)] for m in eth_months]

    rf_mean = sum(cash[m] for m in months[:-1]) / (len(months) - 1)
    out = {
        'schema_version': SCHEMA_VERSION,
        'monthly': series_out,
        'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
        'frequency': 'monthly-average',
        'months': len(months), 'returns': len(months) - 1,
        'first_month': months[0], 'last_month': months[-1],
        'default_base': DEFAULT_BASE,
        'bases': {k: {'label': v['label'], 'weights': v['weights']} for k, v in BASE_MIXES.items()},
        'base': BASE_MIXES[DEFAULT_BASE]['weights'],
        'risk_free': 'effective fed funds rate, monthly average, month by month',
        'risk_free_annual_pct': round(rf_mean, 2),
        'dividends': {'source': "Shiller S&P 500 dividend via datahub.io",
                      'estimated_from': div_est_from,
                      'growth_used_pct': round(div_g * 100, 2)},
        'bonds_method': ('constant-maturity 10-year US Treasury repriced monthly from FRED DGS10 '
                         'monthly-average yields; a proxy for a bond index, not a fund'),
        'note': ('Descriptive, in sample, and short: about one and a half bitcoin cycles. '
                 'Every series is a monthly average, because the equity and gold series can '
                 'only be that; averaging smooths, so volatility and drawdown are understated, '
                 'and daily_check measures by how much. Nothing here is a recommendation, '
                 'and past distributions are not forecasts.'),
        'ladder': ladders[DEFAULT_BASE], 'ladders': ladders,
        'start_dates': starts, 'rebalancing': rebal,
        'rolling_windows': roll_windows,
        'rolling_corr_36m': roll,
        'daily_check': dchk,
    }
    os.makedirs(OUT, exist_ok=True)
    tmp = os.path.join(OUT, 'portfolio.json.tmp')
    io.open(tmp, 'w', encoding='utf-8').write(json.dumps(out, separators=(',', ':')))
    os.replace(tmp, os.path.join(OUT, 'portfolio.json'))
    print(f'  portfolio: {len(months)} months, {len(ladders)} bases, '
          f'{len(starts)} start years, rolling {sorted(roll_windows)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
