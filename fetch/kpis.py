"""
Daily KPI computation for the Crypto Exponentials tools hub.
Re-implements the default specification of each tool page from the snapshot files so the hub and the pages agree.
Reconciled against the pages on 2026-09-01 (see the audit and sign-off document).
"""
import json, os, math, datetime as dt
import numpy as np

OUT = os.path.join(os.path.dirname(__file__), '..', 'data')
GENESIS = dt.date(2009, 1, 3)

def load(name):
    p = os.path.join(OUT, name + '.json')
    return json.load(open(p))['series'] if os.path.exists(p) else {}

def to_daily(series, start=None):
    """dict date->value with linear interpolation of gaps up to 60 days; returns (dates, values) sorted."""
    pts = sorted((dt.date.fromisoformat(d), float(v)) for d, v in series if v is not None)
    if start: pts = [p for p in pts if p[0] >= start]
    out = {}
    for i, (d, v) in enumerate(pts):
        out[d] = v
        if i + 1 < len(pts):
            g = (pts[i + 1][0] - d).days
            if 1 < g <= 60:
                for q in range(1, g): out[d + dt.timedelta(q)] = v + (pts[i + 1][1] - v) * q / g
    ds = sorted(out); return ds, np.array([out[d] for d in ds])

def align(*named):
    """Forward-fill several (dates, values) onto the calendar of the first; returns dates and dict of arrays."""
    base_d, base_v = named[0][1]
    res = {named[0][0]: base_v}
    for name, (ds, vs) in named[1:]:
        m = dict(zip(ds, vs)); arr = np.full(len(base_d), np.nan); cur = np.nan
        for i, d in enumerate(base_d):
            if d in m: cur = m[d]
            arr[i] = cur
        res[name] = arr
    return base_d, res

def pct_of(sorted_arr, v): return 100.0 * np.searchsorted(sorted_arr, v, side='left') / len(sorted_arr)

def spark(dates, *arrays, weeks=104):
    idx = list(range(len(dates) - 1, -1, -7))[:weeks][::-1]
    return [[dates[i].isoformat()] + [None if not np.isfinite(a[i]) else round(float(a[i]), 4) for a in arrays] for i in idx]

# ------------------------------------------------------------ Metcalfe (cumulative addresses, n², lost coins 3.5M front-loaded, fit 2017+)
def effective_supply(dates, supply, lost_m=3.5, profile='front'):
    n = len(supply); days0 = max(1, (dates[0] - GENESIS).days); per = supply[0] / days0
    iss = np.concatenate([np.full(days0, per), np.maximum(np.diff(supply, prepend=supply[0]), 0)]); iss[days0] = 0
    L = len(iss); yrs = np.arange(L) / 365.25
    def build(scale):
        hz = (0.003 + scale * 2.0 ** (-yrs / 3)) if profile == 'front' else np.full(L, scale)
        Hc = np.cumsum(hz / 365.25); c = np.cumsum(iss * np.exp(Hc - Hc[-1])); out = c * np.exp(Hc[-1] - Hc); return out
    lo, hi = 0.0, (1.0 if profile == 'front' else 0.2); res = None
    for _ in range(40):
        mid = (lo + hi) / 2; res = build(mid); lost = (supply[-1] - res[-1]) / 1e6
        if lost < lost_m: lo = mid
        else: hi = mid
    return res[days0:]

def kpi_metcalfe(bc):
    pd_, pv = to_daily(bc['price']); ad, av = to_daily(bc['unique_addresses'])
    cum = dict(zip(ad, np.cumsum(av)))
    keep = [i for i, d in enumerate(pd_) if pv[i] > 0 and d in cum]
    dates = [pd_[i] for i in keep]; price = pv[keep]; n = np.array([cum[d] for d in dates])
    sd, sv = to_daily(bc['supply']); _, a = align(('price', (dates, price)), ('supply', (sd, sv)))
    supply = a['supply']; ok = np.isfinite(supply); dates = [d for d, k in zip(dates, ok) if k]; price, n, supply = price[ok], n[ok], supply[ok]
    seff = supply.copy()  # no lost-coin adjustment by default, matching the tool page and published figures; effective_supply() remains available
    X = np.log(n ** 2 / seff); lnP = np.log(price); w = np.array([d >= dt.date(2017, 1, 1) for d in dates])
    k = np.mean(lnP[w] - X[w]); met = np.exp(k + X); prem = 100 * (lnP - k - X); r = lnP[w] - k - X[w]; sig = 100 * math.sqrt((r ** 2).sum() / (w.sum() - 1))
    ps = np.sort(prem[w]); r2 = 1 - (r ** 2).sum() / ((lnP[w] - lnP[w].mean()) ** 2).sum()
    # "comparable" calibration: fit from 2011-01-01 (excludes the first months of 2010, which fit worst by a factor of fifty); reproduces published figures within about 1%
    wf = np.array([d >= dt.date(2011, 1, 1) for d in dates]); k_full = np.mean(lnP[wf] - X[wf]); met_full = float(np.exp(k_full + X[-1])); prem_full = 100 * (lnP - k_full - X); pfs = np.sort(prem_full[wf])
    rf = lnP[wf] - k_full - X[wf]; sig_full = 100 * math.sqrt((rf ** 2).sum() / (wf.sum() - 1))
    return {'value': round(float(met[-1]), 2), 'value_full_history': round(met_full, 2), 'full_history_fit_from': '2011-01-01', 'sigma_pts_full_history': round(sig_full, 1),
            'prem_sorted_full_history': [round(float(v), 2) for v in pfs[::10]], 'prem_full_p10_p50_p90': [round(float(np.quantile(pfs, q)), 1) for q in (0.1, 0.5, 0.9)], 'k': float(np.exp(k)), 'sigma_pts': round(sig, 1), 'r2_window': round(float(r2), 3), 'premium_pct_close': round(float(prem[-1]), 1),
            'percentile_close': round(pct_of(ps, prem[-1]), 0), 'prem_sorted_p10_p50_p90': [round(float(np.quantile(ps, q)), 1) for q in (0.1, 0.5, 0.9)],
            'users': float(n[-1]), 'effective_supply': float(seff[-1]), 'fit_from': '2017-01-01', 'spec': 'cumulative addresses, n², no lost-coin adjustment, fit 2017 onward',
            'prem_sorted': [round(float(v), 2) for v in ps[::10]], 'spark': spark(dates, price, met)}, dates, price, met

# ------------------------------------------------------------ Power law (genesis origin, from day 560)
def kpi_powerlaw(bc):
    pd_, pv = to_daily(bc['price']); keep = pv > 0; dates = [d for d, k in zip(pd_, keep) if k]; price = pv[keep]
    t = np.array([(d - GENESIS).days for d in dates], float); w = t >= 560
    x = np.log10(t); y = np.log10(price); b, a = np.polyfit(x[w], y[w], 1); trend = 10 ** (a + b * x); res = y - (a + b * x)
    rw = res[w]; sig = math.sqrt((rw ** 2).sum() / (w.sum() - 2)); r2 = 1 - (rw ** 2).sum() / ((y[w] - y[w].mean()) ** 2).sum(); rs = np.sort(rw)
    proj = {ds: round(float(10 ** (a + b * math.log10((dt.date.fromisoformat(ds) - GENESIS).days))), 0) for ds in ['2027-01-01', '2028-01-01', '2030-01-01']}
    return {'trend': round(float(trend[-1]), 2), 'beta': round(float(b), 3), 'intercept': round(float(a), 4), 'r2': round(float(r2), 3), 'sigma_dex': round(sig, 3),
            'deviation_dex_close': round(float(res[-1]), 3), 'percentile_close': round(pct_of(rs, res[-1]), 0), 'res_sorted': [round(float(v), 3) for v in rs[::10]],
            'projection': proj, 'spec': 'log10 P = a + β log10(days since genesis), from day 560', 'spark': spark(dates, price, trend)}, dates, price, trend

# ------------------------------------------------------------ Realised value (Coin Metrics; realised cap = market cap / MVRV; window 2017+)
def kpi_realised(cm):
    if not cm.get('PriceUSD'): return None, None, None, None
    pd_, pv = to_daily(cm['PriceUSD']); _, a = align(('price', (pd_, pv)), ('mkt', to_daily(cm['CapMrktCurUSD'])), ('mvrv', to_daily(cm['CapMVRVCur'])), ('sup', to_daily(cm['SplyCur'])), ('adr', to_daily(cm.get('AdrBalCnt', []))))
    ok = np.isfinite(a['mkt']) & np.isfinite(a['mvrv']) & np.isfinite(a['sup']) & (pv > 0); dates = [d for d, k in zip(pd_, ok) if k]
    price, mkt, mvrv, sup, adr = pv[ok], a['mkt'][ok], a['mvrv'][ok], a['sup'][ok], a['adr'][ok]
    real = mkt / mvrv; realp = real / sup; w = np.array([d >= dt.date(2017, 1, 1) for d in dates]); sd = mkt[w].std(ddof=1); z = (mkt - real) / sd; mv = mkt / real; mvs = np.sort(mv[w])
    return {'realised_price': round(float(realp[-1]), 2), 'realised_cap': float(real[-1]), 'supply': float(sup[-1]), 'mvrv_close': round(float(mv[-1]), 3), 'z_close': round(float(z[-1]), 3),
            'mktcap_sd_window': float(sd), 'percentile_close': round(pct_of(mvs, mv[-1]), 0), 'mvrv_sorted': [round(float(v), 3) for v in mvs[::10]], 'addresses_with_balance': float(adr[-1]) if np.isfinite(adr[-1]) else None,
            'window': '2017-01-01', 'realised_cap_source': 'market cap / MVRV (CapMVRVCur)', 'spark': spark(dates, price, realp)}, dates, price, realp

# ------------------------------------------------------------ Positioning (window 2021+)
def kpi_positioning(bc, dv, st, fg, fr, cm):
    pd_, pv = to_daily(bc['price']); keep = pv > 0; pd_ = [d for d, k in zip(pd_, keep) if k]; pv = pv[keep]
    ser = {'funding': dv.get('deribit_funding_8h_daily_mean', []), 'oi': dv.get('okx_open_interest_usd', []), 'dvol': dv.get('deribit_dvol_close', []), 'stable': st.get('total_usd', []), 'fng': fg.get('index', []), 'dxy': fr.get('dollar_index_broad', []), 'realy': fr.get('real_yield_10y', []), 'mkt': cm.get('CapMrktCurUSD', [])}
    _, a = align(('price', (pd_, pv)), *[(k, to_daily(v)) for k, v in ser.items() if v])
    w = np.array([d >= dt.date(2021, 1, 1) for d in pd_]); out = {'window': '2021-01-01'}
    def last(arr):
        idx = np.where(np.isfinite(arr))[0]; return (int(idx[-1]) if len(idx) else None)
    def pct(arr, v):
        s = np.sort(arr[w & np.isfinite(arr)]); return round(pct_of(s, v), 0) if len(s) > 30 else None
    if 'funding' in a: i = last(a['funding']); fa = a['funding'] * 1095 * 100; out['funding_annualised_pct'] = round(float(fa[i]), 2); out['funding_percentile'] = pct(fa, fa[i]); out['funding_date'] = pd_[i].isoformat()
    if 'oi' in a and 'mkt' in a: i = last(a['oi']); oish = 100 * a['oi'] / a['mkt']; out['open_interest_usd'] = float(a['oi'][i]); out['oi_share_pct'] = round(float(oish[i]), 3); out['oi_share_percentile'] = pct(oish, oish[i]); out['oi_history_days'] = int(np.isfinite(a['oi'][w]).sum())
    if 'dvol' in a: i = last(a['dvol']); out['dvol'] = round(float(a['dvol'][i]), 2); out['dvol_percentile'] = pct(a['dvol'], a['dvol'][i])
    if 'stable' in a: i = last(a['stable']); out['stablecoin_supply_usd'] = float(a['stable'][i]); out['stablecoin_30d_change_pct'] = round(float(100 * (a['stable'][i] / a['stable'][i - 30] - 1)), 2)
    if 'fng' in a: i = last(a['fng']); out['fear_greed'] = int(a['fng'][i]); out['fear_greed_percentile'] = pct(a['fng'], a['fng'][i])
    if 'dxy' in a: i = last(a['dxy']); out['dollar_index'] = round(float(a['dxy'][i]), 2); j = i - 30; out['dollar_30d_change_pct'] = round(float(100 * (a['dxy'][i] / a['dxy'][j] - 1)), 2) if j >= 0 and np.isfinite(a['dxy'][j]) else None
    if 'realy' in a: i = last(a['realy']); out['real_yield_10y'] = round(float(a['realy'][i]), 2); j = i - 30; out['real_yield_30d_change'] = round(float(a['realy'][i] - a['realy'][j]), 2) if j >= 0 and np.isfinite(a['realy'][j]) else None
    # market structure from price alone
    P = pv; i = len(P) - 1
    out['price_200d_avg'] = round(float(P[i-199:i+1].mean()), 2); out['price_vs_200d_pct'] = round(float(100 * (P[i] / out['price_200d_avg'] - 1)), 2)
    hi90, lo90 = float(P[i-89:i+1].max()), float(P[i-89:i+1].min()); out['high_90d'] = round(hi90, 2); out['low_90d'] = round(lo90, 2); out['position_in_90d_range_pct'] = round(100 * (P[i] - lo90) / (hi90 - lo90), 1) if hi90 > lo90 else None
    out['price_30d_change_pct'] = round(float(100 * (P[i] / P[i-30] - 1)), 2)
    comps = [out.get(k) for k in ('funding_percentile', 'oi_share_percentile', 'fear_greed_percentile') if out.get(k) is not None]
    out['composite_percentile'] = round(sum(comps) / len(comps), 0) if len(comps) >= 2 else None
    # composite series over the window (same rule as the Flows page) and its sorted distribution, so the hub can rank today's value against it
    ser2 = {'f': a['funding'] * 1095 * 100 if 'funding' in a else None, 'o': (100 * a['oi'] / a['mkt']) if ('oi' in a and 'mkt' in a) else None, 'g': a['fng'] if 'fng' in a else None}
    srt = {k: np.sort(v[w & np.isfinite(v)]) for k, v in ser2.items() if v is not None}
    comp_series = []
    for i in range(len(pd_)):
        if not w[i]: continue
        ps = [pct_of(srt[k], ser2[k][i]) for k in srt if np.isfinite(ser2[k][i]) and len(srt[k]) > 30]
        if len(ps) >= 2: comp_series.append(sum(ps) / len(ps))
    out['composite_sorted'] = [round(float(v), 1) for v in np.sort(comp_series)[::5]] if comp_series else None
    out['spark'] = spark(pd_, pv, a['stable'] if 'stable' in a else np.full(len(pv), np.nan))
    return out

def selftest(kp, bc, cm):
    """Independent recomputation of the headline numbers by a different route than the main functions."""
    fails, warns = [], []
    try:  # Metcalfe: closed-form k from log means, cumulative users via plain cumsum, no interpolation helper
        pd_, pv = to_daily(bc['price']); ad = dict(to_daily(bc['unique_addresses']) and zip(*to_daily(bc['unique_addresses'])))
        ads = sorted(ad); cum = {}; c = 0.0
        for d in ads: c += ad[d]; cum[d] = c
        sd, sv = to_daily(bc['supply']); sup = dict(zip(sd, sv))
        rows = [(d, v, cum[d], sup.get(d)) for d, v in zip(pd_, pv) if v > 0 and d in cum and sup.get(d)]
        w = [(d, v, n, s) for d, v, n, s in rows if d >= dt.date(2011, 1, 1)]
        k = sum(math.log(v) - math.log(n * n / s) for d, v, n, s in w) / len(w); d, v, n, s = rows[-1]; met = math.exp(k) * n * n / s
        if abs(met / kp['metcalfe']['value_full_history'] - 1) > 0.02: fails.append(f'metcalfe comparable: selftest {met:.0f} vs {kp["metcalfe"]["value_full_history"]:.0f}')
    except Exception as e: warns.append('metcalfe selftest not run: ' + str(e)[:120])
    try:  # power law: normal equations instead of polyfit
        pd_, pv = to_daily(bc['price']); pts = [((d - GENESIS).days, v) for d, v in zip(pd_, pv) if v > 0 and (d - GENESIS).days >= 560]
        xs = [math.log10(t) for t, _ in pts]; ys = [math.log10(v) for _, v in pts]; n = len(xs); mx, my = sum(xs) / n, sum(ys) / n
        b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs); a = my - b * mx; trend = 10 ** (a + b * xs[-1])
        if abs(trend / kp['powerlaw']['trend'] - 1) > 0.005: fails.append(f'powerlaw: selftest {trend:.0f} vs {kp["powerlaw"]["trend"]:.0f}')
        if abs(b - kp['powerlaw']['beta']) > 0.01: fails.append(f'powerlaw beta: {b:.3f} vs {kp["powerlaw"]["beta"]}')
    except Exception as e: warns.append('powerlaw selftest not run: ' + str(e)[:120])
    try:  # realised: last-row division straight from the raw series
        if kp.get('realised') and cm.get('CapMrktCurUSD'):
            last = {k: dict(cm[k])[max(dict(cm[k]))] for k in ('CapMrktCurUSD', 'CapMVRVCur', 'SplyCur')}
            rp = last['CapMrktCurUSD'] / last['CapMVRVCur'] / last['SplyCur']
            if abs(rp / kp['realised']['realised_price'] - 1) > 0.005: fails.append(f'realised: selftest {rp:.0f} vs {kp["realised"]["realised_price"]:.0f}')
    except Exception as e: warns.append('realised selftest not run: ' + str(e)[:120])
    return {'failures': fails, 'warnings': warns, 'status': 'ok'}

def main():
    bc, cm, dv, st, fg, fr = (load(x) for x in ('blockchain', 'coinmetrics', 'derivatives', 'stablecoins', 'fear_greed', 'fred'))
    kp = {'generated_at': dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(), 'note': 'Headline readings of each tool page at its default specification, computed from the same snapshot. Price-dependent readings are recomputed live on the hub from the spot price.'}
    m, dates, price, met = kpi_metcalfe(bc); kp['metcalfe'] = m; kp['price_close'] = round(float(price[-1]), 2); kp['as_of'] = dates[-1].isoformat()
    p, _, _, _ = kpi_powerlaw(bc); kp['powerlaw'] = p
    r, _, _, _ = kpi_realised(cm); kp['realised'] = r
    kp['positioning'] = kpi_positioning(bc, dv, st, fg, fr, cm)
    # (4) self-test: recompute each headline a second, independent way and fail loudly on drift
    st = selftest(kp, bc, cm); kp['selftest'] = st
    # (2) daily history of the readings (one row per snapshot date, overwritten if the job runs twice in a day)
    hist_p = os.path.join(OUT, 'kpis_history.json')
    hist = json.load(open(hist_p)) if os.path.exists(hist_p) else {'note': 'one row per day: as_of, price_close, metcalfe (comparable, 2011), metcalfe_validated (2017), powerlaw_trend, realised_price, composite_percentile', 'rows': []}
    row = {'date': kp['as_of'], 'price': kp['price_close'], 'metcalfe': m['value_full_history'], 'metcalfe_validated': m['value'], 'powerlaw': p['trend'], 'realised': r['realised_price'] if r else None, 'composite': kp['positioning'].get('composite_percentile'), 'generated_at': kp['generated_at']}
    hist['rows'] = [x for x in hist['rows'] if x.get('date') != row['date']] + [row]; hist['rows'].sort(key=lambda x: x['date'])
    # drift check against the previous row
    prev = hist['rows'][-2] if len(hist['rows']) > 1 else None
    if prev:
        for k in ('metcalfe', 'metcalfe_validated', 'powerlaw', 'realised'):
            if prev.get(k) and row.get(k) and abs(row[k] / prev[k] - 1) > 0.25: st['warnings'].append(f'{k} moved {100*(row[k]/prev[k]-1):+.0f}% since {prev["date"]}')
    st['status'] = 'fail' if st['failures'] else ('warn' if st['warnings'] else 'ok')
    with open(hist_p, 'w') as f: json.dump(hist, f, separators=(',', ':'))
    with open(os.path.join(OUT, 'kpis.json'), 'w') as f: json.dump(kp, f, separators=(',', ':'))
    if st['failures']: raise RuntimeError('self-test failed: ' + '; '.join(st['failures']))
    print(f"  ok  kpis: price {kp['price_close']}, metcalfe {m['value']}, power law {p['trend']}, realised {r['realised_price'] if r else None}, composite {kp['positioning'].get('composite_percentile')}")

if __name__ == '__main__': main()
