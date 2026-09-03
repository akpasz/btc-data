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

def oos_rmse(dates, lnP, X, start):
    """Expanding-window annual refits of the Metcalfe constant from `start`: fit through each 31 December,
    predict the following year, pool the errors. Returns RMSE in log points, matching the tool page's
    out-of-sample validation table for the fixed-exponent model. The hub renders these figures instead of
    hardcoded numbers."""
    errs = []
    for Y in range(start.year + 3, dates[-1].year + 1):
        cut = dt.date(Y, 1, 1); end = dt.date(Y + 1, 1, 1)
        idx = [i for i, d in enumerate(dates) if start <= d < cut]
        if len(idx) < 365 * 3: continue
        k = float(np.mean([lnP[i] - X[i] for i in idx]))
        errs += [float(lnP[i] - k - X[i]) for i, d in enumerate(dates) if cut <= d < end]
    return round(100 * math.sqrt(sum(e * e for e in errs) / len(errs)), 1) if errs else None

# ------------------------------------------------------------ Extended KPIs (Tier 1: computed from series already in the snapshot;
# Tier 2 blocks activate automatically once their fetchers are deployed). Everything here is additive: the existing
# sections and self-test are untouched, and any failure is caught in main() so it can never break the pipeline.
def technical_state(prices):
    """Compact technical readings for the dashboard: relative strength, the 50/200 relationship,
    price against the 200-day average and 30-day realised volatility. Definitions match the
    technical-signals page exactly so the two surfaces cannot disagree; that page carries the
    historical record of these rules, this publishes only the current state."""
    import math as _m
    p = [float(x) for x in prices if x and float(x) > 0]
    if len(p) < 260:
        return None
    w = 14
    au = sum(max(p[i] - p[i - 1], 0.0) for i in range(1, w + 1)) / w
    ad = sum(max(p[i - 1] - p[i], 0.0) for i in range(1, w + 1)) / w
    for i in range(w + 1, len(p)):
        d = p[i] - p[i - 1]
        au = (au * (w - 1) + max(d, 0.0)) / w
        ad = (ad * (w - 1) + max(-d, 0.0)) / w
    rsi = 100 - 100 / (1 + au / ad) if ad > 0 else 100.0
    ma50 = sum(p[-50:]) / 50.0
    ma200 = sum(p[-200:]) / 200.0
    lr = [_m.log(p[i] / p[i - 1]) for i in range(len(p) - 30, len(p))]
    mu = sum(lr) / len(lr)
    rv = (sum((x - mu) ** 2 for x in lr) / len(lr)) ** 0.5 * (365 ** 0.5) * 100
    return {'rsi_14': round(rsi, 1),
            'rsi_state': 'stretched upward' if rsi > 70 else ('stretched downward' if rsi < 30 else 'neither extreme'),
            'ma50': round(ma50, 2), 'ma200': round(ma200, 2),
            'trend': '50-day above the 200-day' if ma50 > ma200 else '50-day below the 200-day',
            'price_vs_ma200_pct': round(100 * (p[-1] / ma200 - 1), 1),
            'realised_vol_30d': round(rv, 1),
            'note': 'current readings only; the historical record of these rules is on the technical-signals page'}


def kpi_extended(bc, cm, dv, fr=None, cb=None, bn=None, cg=None):
    fr = fr or {}; cb = cb or {}; bn = bn or {}; cg = cg or {}
    pd_, pv = to_daily(bc['price']); keep = pv > 0
    dates = [d for d, k in zip(pd_, keep) if k]; price = pv[keep]
    n = len(dates); W = dt.date(2017, 1, 1); w = np.array([d >= W for d in dates])

    def ffill(series):
        if not series: return np.full(n, np.nan)
        ds, vs = to_daily(series); m = dict(zip(ds, vs)); arr = np.full(n, np.nan); cur = np.nan
        for i, d in enumerate(dates):
            if d in m: cur = m[d]
            arr[i] = cur
        return arr
    def sma(a, wdw):
        out = np.full(n, np.nan); c = np.nancumsum(np.where(np.isfinite(a), a, 0)); k = np.cumsum(np.isfinite(a))
        for i in range(wdw - 1, n):
            cnt = k[i] - (k[i - wdw] if i >= wdw else 0)
            if cnt >= wdw * 0.8: out[i] = (c[i] - (c[i - wdw] if i >= wdw else 0)) / cnt
        return out
    def pct(arr, v, win=None):
        win = w if win is None else win
        srt = np.sort(arr[win & np.isfinite(arr)])
        return round(pct_of(srt, v), 0) if len(srt) > 100 and np.isfinite(v) else None
    def fwd365(signal, buckets, cur):
        rows = []
        for lo, hi, lab in buckets:
            rets = [100 * (price[i + 365] / price[i] - 1) for i in range(n - 365)
                    if w[i] and np.isfinite(signal[i]) and lo <= signal[i] < hi]
            med = round(float(np.median(rets)), 0) if rets else None
            rows.append({'bucket': lab, 'median_365d_pct': med, 'days': len(rets), 'current': bool(np.isfinite(cur) and lo <= cur < hi)})
        return rows

    iss = ffill(cm.get('IssTotUSD')); fees = ffill(bc.get('fees_usd')); hr = ffill(bc.get('hash_rate'))
    txv = ffill(bc.get('tx_volume_usd')); mkt = ffill(cm.get('CapMrktCurUSD')); mvrv = ffill(cm.get('CapMVRVCur'))
    adract = ffill(cm.get('AdrActCnt')); adrbal = ffill(cm.get('AdrBalCnt')); dvol = ffill(dv.get('deribit_dvol_close'))
    i = n - 1; out = {'as_of': dates[i].isoformat(), 'window': W.isoformat(),
                      'note': 'Tier 1 metrics from series already in the snapshot; percentiles over the stated window at daily close. Descriptive, not signals; forward-return rows are in-sample with overlapping windows.'}

    ma200 = sma(price, 200); mayer = price / ma200; mv_ = float(mayer[i]) if np.isfinite(mayer[i]) else None
    out['mayer'] = {'value': round(mv_, 3) if mv_ else None, 'pct': pct(mayer, mayer[i]),
                    'ma200': round(float(ma200[i]), 2) if np.isfinite(ma200[i]) else None}
    iss365 = sma(iss, 365); puell = iss / iss365; pu = float(puell[i]) if np.isfinite(puell[i]) else None
    out['puell'] = {'value': round(pu, 3) if pu else None, 'pct': pct(puell, puell[i])}
    nupl = np.where(mvrv > 0, 1 - 1 / mvrv, np.nan); nu = float(nupl[i]) if np.isfinite(nupl[i]) else None
    out['nupl'] = {'value': round(nu, 3) if nu is not None else None, 'pct': pct(nupl, nupl[i]),
                   'zone': None if nu is None else ('capitulation' if nu < 0 else 'hope' if nu < .25 else 'optimism' if nu < .5 else 'belief' if nu < .75 else 'euphoria')}
    thermo = np.nancumsum(np.where(np.isfinite(iss), iss, 0)); tratio = np.where(thermo > 0, mkt / thermo, np.nan)
    out['thermocap'] = {'usd': round(float(thermo[i]), 0), 'multiple': round(float(tratio[i]), 2) if np.isfinite(tratio[i]) else None, 'pct': pct(tratio, tratio[i])}
    txv90 = sma(txv, 90); nvtc = np.where(txv > 0, mkt / txv, np.nan); nvts = np.where(txv90 > 0, mkt / txv90, np.nan)
    out['nvt'] = {'classic': round(float(nvtc[i]), 1) if np.isfinite(nvtc[i]) else None,
                  'signal_90d': round(float(nvts[i]), 1) if np.isfinite(nvts[i]) else None, 'pct_signal': pct(nvts, nvts[i]),
                  'volume_source': 'blockchain.com estimated tx volume, not entity adjusted'}
    hr30 = sma(hr, 30); hr60 = sma(hr, 60); cap = hr30 < hr60
    state = bool(cap[i]) if np.isfinite(hr30[i]) and np.isfinite(hr60[i]) else None
    streak = 0
    for k in range(i, -1, -1):
        if not (np.isfinite(hr30[k]) and np.isfinite(hr60[k])) or bool(cap[k]) != state: break
        streak += 1
    last_recovery = None
    for k in range(i, 0, -1):
        if np.isfinite(hr30[k]) and np.isfinite(hr60[k]) and (not cap[k]) and cap[k - 1]: last_recovery = dates[k].isoformat(); break
    out['hash_ribbons'] = {'state': None if state is None else ('capitulation' if state else 'expansion'), 'days_in_state': streak,
                           'last_recovery_signal': last_recovery, 'hr30_ehs': round(float(hr30[i]) / 1e6, 1) if np.isfinite(hr30[i]) else None,
                           'hr60_ehs': round(float(hr60[i]) / 1e6, 1) if np.isfinite(hr60[i]) else None}
    hp = np.where(hr > 0, (iss + fees) / hr, np.nan); hp30 = sma(hp, 30)
    j = i - 30
    out['hashprice'] = {'usd_per_th_day': round(float(hp[i]), 4) if np.isfinite(hp[i]) else None, 'pct': pct(hp, hp[i]),
                        'change_30d_pct': round(float(100 * (hp30[i] / hp30[j] - 1)), 1) if j >= 0 and np.isfinite(hp30[i]) and np.isfinite(hp30[j]) else None}
    fshare = np.where((fees + iss) > 0, 100 * fees / (fees + iss), np.nan); fs90 = sma(fshare, 90)
    out['fee_share'] = {'pct_90d': round(float(fs90[i]), 2) if np.isfinite(fs90[i]) else None,
                        'note': 'fees as a share of miner revenue; the long-run security-budget question'}
    lr = np.diff(np.log(price)); rv = np.full(n, np.nan)
    for k in range(30, n): rv[k] = float(np.std(lr[k - 30:k], ddof=1)) * math.sqrt(365) * 100
    vrp = dvol - rv; wd = np.isfinite(dvol)
    out['volatility'] = {'realized_30d_pct': round(float(rv[i]), 1) if np.isfinite(rv[i]) else None,
                         'dvol_pct': round(float(dvol[i]), 1) if np.isfinite(dvol[i]) else None,
                         'vrp_pts': round(float(vrp[i]), 1) if np.isfinite(vrp[i]) else None,
                         'vrp_pct': pct(vrp, vrp[i], win=wd),
                         'note': 'VRP = implied (DVOL) minus realized; persistently positive is normal, negative marks stress'}
    halvings = [dt.date(2012, 11, 28), dt.date(2016, 7, 9), dt.date(2020, 5, 11), dt.date(2024, 4, 20)]
    dmap = {d: k for k, d in enumerate(dates)}
    def px_on(d0):
        for back in range(6):
            k = dmap.get(d0 - dt.timedelta(back))
            if k is not None: return float(price[k]), k
        return None, None
    hlast = max(h for h in halvings if h <= dates[i]); days_in = (dates[i] - hlast).days
    p0, _ = px_on(hlast); comp = {'halving': hlast.isoformat(), 'days_since_halving': days_in,
                                  'multiple_this_cycle': round(float(price[i]) / p0, 2) if p0 else None, 'prior_cycles_at_same_day': {}}
    for h in halvings:
        if h >= hlast: continue
        ph, kh = px_on(h); pt_, _ = px_on(h + dt.timedelta(days_in))
        if ph and pt_: comp['prior_cycles_at_same_day'][h.isoformat()[:4]] = round(pt_ / ph, 2)
    out['cycle'] = comp
    a30 = sma(adract, 30); j = i - 365
    out['network'] = {'active_addresses_30d': round(float(a30[i]), 0) if np.isfinite(a30[i]) else None,
                      'active_addresses_yoy_pct': round(float(100 * (a30[i] / a30[j] - 1)), 1) if j >= 0 and np.isfinite(a30[i]) and np.isfinite(a30[j]) else None,
                      'addresses_with_balance_yoy_pct': round(float(100 * (adrbal[i] / adrbal[j] - 1)), 1) if j >= 0 and np.isfinite(adrbal[i]) and np.isfinite(adrbal[j]) else None}
    out['fwd'] = {
        'mayer': fwd365(mayer, [(-9, .8, 'below 0.8'), (.8, 1, '0.8 to 1.0'), (1, 1.5, '1.0 to 1.5'), (1.5, 2.4, '1.5 to 2.4'), (2.4, 99, 'above 2.4')], mayer[i]),
        'puell': fwd365(puell, [(-9, .5, 'below 0.5'), (.5, 1, '0.5 to 1.0'), (1, 2, '1.0 to 2.0'), (2, 4, '2.0 to 4.0'), (4, 99, 'above 4.0')], puell[i]),
        'nupl':  fwd365(nupl, [(-9, 0, 'below 0'), (0, .25, '0 to 0.25'), (.25, .5, '0.25 to 0.50'), (.5, .75, '0.50 to 0.75'), (.75, 9, 'above 0.75')], nupl[i])}
    out['spark_mayer'] = spark(dates, price, ma200)
    out['spark_ribbons'] = spark(dates, hr30 / 1e6, hr60 / 1e6)

    def last_of(series):
        try:
            for d_, v_ in reversed(series):
                fv = float(v_)
                if math.isfinite(fv): return fv
        except Exception: pass
        return None
    extras = {}
    if fr.get('walcl') and fr.get('tga') and fr.get('rrp_on'):
        # FRED units: WALCL and WTREGEN in $ millions, RRPONTSYD in $ billions
        nl = ffill([[d, v * 1e6] for d, v in fr['walcl']]) - ffill([[d, v * 1e6] for d, v in fr['tga']]) - ffill([[d, v * 1e9] for d, v in fr['rrp_on']])
        j = i - 90
        extras['fed_net_liquidity'] = {'usd_t': round(float(nl[i]) / 1e12, 3) if np.isfinite(nl[i]) else None,
                                       'change_90d_pct': round(float(100 * (nl[i] / nl[j] - 1)), 2) if j >= 0 and np.isfinite(nl[j]) else None,
                                       'spec': 'WALCL - TGA (WTREGEN) - ON RRP (RRPONTSYD)'}
    if cg.get('btc_dominance_pct'):
        cur = last_of(cg['btc_dominance_pct']); dom = ffill(cg['btc_dominance_pct']); j = i - 30
        extras['btc_dominance'] = {'pct': round(cur, 2) if cur is not None else None,
                                   'change_30d_pts': round(float(cur - dom[j]), 2) if cur is not None and j >= 0 and np.isfinite(dom[j]) else None}
    if cb.get('spot') and bn.get('close_usdt'):
        cbs = ffill(cb['spot']); bns = ffill(bn['close_usdt']); prem = np.where(bns > 0, 100 * (cbs / bns - 1), np.nan)
        ok = np.isfinite(prem)
        cbm = {d_: float(v_) for d_, v_ in cb['spot']}; cur = None
        for d_, v_ in reversed(bn['close_usdt']):
            if d_ in cbm and float(v_) > 0: cur = 100 * (cbm[d_] / float(v_) - 1); break
        if cur is None and np.isfinite(prem[i]): cur = float(prem[i])
        extras['coinbase_premium'] = {'pct': round(cur, 3) if cur is not None else None,
                                      'pct_rank': pct(prem, cur, win=ok) if ok.sum() > 100 and cur is not None else None,
                                      'note': 'Coinbase USD spot vs offshore USDT close (OKX); USDT peg deviation is inside this number'}
    if dv.get('deribit_basis_90d_ann_pct'):
        cur = last_of(dv['deribit_basis_90d_ann_pct']); bas = ffill(dv['deribit_basis_90d_ann_pct']); ok = np.isfinite(bas)
        extras['futures_basis'] = {'ann_pct': round(cur, 2) if cur is not None else None,
                                   'pct_rank': pct(bas, cur, win=ok) if ok.sum() > 100 and cur is not None else None}
    if dv.get('deribit_putcall_oi_ratio'):
        cur = last_of(dv['deribit_putcall_oi_ratio'])
        extras['putcall_oi'] = {'ratio': round(cur, 3) if cur is not None else None}
    if extras: out['flows_extras'] = extras
    tech = technical_state(list(price[-400:]))
    if tech: out['technical'] = tech
    return out

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
    oos_v = oos_rmse(dates, lnP, X, dt.date(2017, 1, 1)); oos_f = oos_rmse(dates, lnP, X, dt.date(2011, 1, 1))
    return {'value': round(float(met[-1]), 2),
            # reference calibration (fit from 2011-01-01, comparable with published figures). The *_full_history keys are the legacy names for the same
            # numbers and are kept for one release; 'full history' is a misnomer, since the true full-history fit from 2010 is the one the site rejects.
            'value_reference': round(met_full, 2), 'reference_fit_from': '2011-01-01', 'sigma_pts_reference': round(sig_full, 1), 'oos_rmse_pts_reference': oos_f,
            'prem_sorted_reference': [round(float(v), 2) for v in pfs[::10]], 'prem_reference_p10_p50_p90': [round(float(np.quantile(pfs, q)), 1) for q in (0.1, 0.5, 0.9)],
            'value_full_history': round(met_full, 2), 'full_history_fit_from': '2011-01-01', 'sigma_pts_full_history': round(sig_full, 1),
            'prem_sorted_full_history': [round(float(v), 2) for v in pfs[::10]], 'prem_full_p10_p50_p90': [round(float(np.quantile(pfs, q)), 1) for q in (0.1, 0.5, 0.9)], 'k': float(np.exp(k)), 'sigma_pts': round(sig, 1), 'r2_window': round(float(r2), 3), 'premium_pct_close': round(float(prem[-1]), 1),
            'percentile_close': round(pct_of(ps, prem[-1]), 0), 'prem_sorted_p10_p50_p90': [round(float(np.quantile(ps, q)), 1) for q in (0.1, 0.5, 0.9)],
            'users': float(n[-1]), 'effective_supply': float(seff[-1]), 'fit_from': '2017-01-01', 'spec': 'cumulative addresses, n², no lost-coin adjustment, fit 2017 onward',
            'prem_sorted': [round(float(v), 2) for v in ps[::10]], 'oos_rmse_pts': oos_v, 'oos_rmse_pts_full_history': oos_f, 'spark': spark(dates, price, met)}, dates, price, met

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
    out['composite_percentile'] = round(sum(comps) / len(comps), 0) if len(comps) >= 3 else None  # all three components or nothing: a two-component average is a different series
    out['composite_components_present'] = len(comps)
    # composite series over the window (same rule as the Flows page) and its sorted distribution, so the hub can rank today's value against it
    ser2 = {'f': a['funding'] * 1095 * 100 if 'funding' in a else None, 'o': (100 * a['oi'] / a['mkt']) if ('oi' in a and 'mkt' in a) else None, 'g': a['fng'] if 'fng' in a else None}
    srt = {k: np.sort(v[w & np.isfinite(v)]) for k, v in ser2.items() if v is not None}
    comp_series = []
    for i in range(len(pd_)):
        if not w[i]: continue
        ps = [pct_of(srt[k], ser2[k][i]) for k in srt if np.isfinite(ser2[k][i]) and len(srt[k]) > 30]
        if len(ps) >= 3: comp_series.append(sum(ps) / len(ps))
    out['composite_sorted'] = [round(float(v), 1) for v in np.sort(comp_series)[::5]] if comp_series else None
    out['spark'] = spark(pd_, pv, a['stable'] if 'stable' in a else np.full(len(pv), np.nan))
    return out

def selftest(kp, bc, cm):
    """Independent recomputation of the headline numbers by a different route than the main functions."""
    fails, warns = [], []
    try:  # Metcalfe: closed-form k from log means, cumulative users via plain cumsum (shares to_daily for calendar fill)
        pd_, pv = to_daily(bc['price']); _ad_d, _ad_v = to_daily(bc['unique_addresses']); ad = dict(zip(_ad_d, _ad_v))
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
    try:
        kp['extended'] = kpi_extended(bc, cm, dv, fr=fr, cb=load('coinbase'), bn=load('offshore_spot') or load('binance'), cg=load('coingecko_global'))
    except Exception as e:
        kp['extended'] = {'error': str(e)[:200]}
    # (4) self-test: recompute each headline a second, independent way and fail loudly on drift
    sf = selftest(kp, bc, cm); kp['selftest'] = sf
    # (2) daily history of the readings (one row per snapshot date, overwritten if the job runs twice in a day)
    hist_p = os.path.join(OUT, 'kpis_history.json')
    hist = json.load(open(hist_p)) if os.path.exists(hist_p) else {'note': 'one row per day: as_of, price_close, metcalfe (comparable, 2011), metcalfe_validated (2017), powerlaw_trend, realised_price, composite_percentile', 'rows': []}
    row = {'date': kp['as_of'], 'price': kp['price_close'], 'metcalfe': m['value_full_history'], 'metcalfe_validated': m['value'], 'powerlaw': p['trend'], 'realised': r['realised_price'] if r else None, 'composite': kp['positioning'].get('composite_percentile'), 'generated_at': kp['generated_at']}
    hist['rows'] = [x for x in hist['rows'] if x.get('date') != row['date']] + [row]; hist['rows'].sort(key=lambda x: x['date'])
    # drift check against the previous row
    prev = hist['rows'][-2] if len(hist['rows']) > 1 else None
    if prev:
        for k in ('metcalfe', 'metcalfe_validated', 'powerlaw', 'realised'):
            if prev.get(k) and row.get(k) and abs(row[k] / prev[k] - 1) > 0.25: sf['warnings'].append(f'{k} moved {100*(row[k]/prev[k]-1):+.0f}% since {prev["date"]}')
    sf['status'] = 'fail' if sf['failures'] else ('warn' if sf['warnings'] else 'ok')
    with open(hist_p, 'w') as f: json.dump(hist, f, separators=(',', ':'))
    # (5) research outputs computed once here so the pages read them rather than re-deriving them (composite.json,
    # Metcalfe null test, independence matrix, changes.atom). Isolated: a failure is recorded in kp['research'], not fatal.
    try:
        import research; kp['research'] = research.run(OUT, bc, cm, dv, st, fg, kp, hist['rows'])
        if kp['research'].get('metcalfe_null_test'): kp['metcalfe']['null_test'] = kp['research']['metcalfe_null_test']
        if kp['research'].get('independence'): kp.setdefault('extended', {})['independence'] = kp['research']['independence']
    except Exception as e: kp['research'] = {'error': str(e)[:200]}
    with open(os.path.join(OUT, 'kpis.json'), 'w') as f: json.dump(kp, f, separators=(',', ':'))
    if sf['failures']: raise RuntimeError('self-test failed: ' + '; '.join(sf['failures']))
    print(f"  ok  kpis: price {kp['price_close']}, metcalfe {m['value']}, power law {p['trend']}, realised {r['realised_price'] if r else None}, composite {kp['positioning'].get('composite_percentile')}")

if __name__ == '__main__': main()
