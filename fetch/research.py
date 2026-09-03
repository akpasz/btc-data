"""Research outputs computed once in the pipeline so the pages read them rather than each re-deriving them.

  composite.json   the as-of valuation composite (MVRV, Mayer, expanding power-law deviation ranked against
                   prior history since 2013, equal-weight mean, cuts at 10/30/70/90) — the single source of truth
                   for Where Things Stand and the Indicator Autopsy event study.
  metcalfe.null_test   price ~ time, price ~ network, price ~ both: does the user stock add information beyond time?
  extended.independence   Spearman correlation of the signal levels the site publishes, so agreement between
                   columns is not mistaken for independent confirmation.
  changes.atom     one entry per snapshot describing what moved, for readers who subscribe rather than revisit.

Everything here is point-in-time where the page it feeds is point-in-time, and says so in its note field.
"""
import json, os, math, datetime as dt
import numpy as np
from kpis import to_daily, align

W2013 = dt.date(2013, 1, 1)
GENESIS = dt.date(2009, 1, 3)


def _sma(a, w):
    """Simple moving average tolerant of gaps: window must be full and at least 90% finite (matches the pages)."""
    n = len(a); out = np.full(n, np.nan); s = 0.0; c = 0; q = []
    for i, x in enumerate(a):
        q.append(x)
        if np.isfinite(x): s += x; c += 1
        if len(q) > w:
            y = q.pop(0)
            if np.isfinite(y): s -= y; c -= 1
        if len(q) == w and c >= 0.9 * w: out[i] = s / c
    return out


def _asof_rank(dates, a, start=W2013, min_prior=250):
    """Percentile of each value against prior observations only (>= min_prior of them), window from `start`."""
    n = len(a); out = np.full(n, np.nan); seen = []
    for i in range(n):
        x = a[i]
        if dates[i] >= start and np.isfinite(x):
            if len(seen) >= min_prior:
                arr = np.array(seen); out[i] = 100.0 * np.count_nonzero(arr <= x) / len(arr)
            seen.append(x)
    return out


def _expanding_powerlaw_dev(dates, price, min_obs=500, from_day=560):
    """log10 price minus the power-law trend fitted on data up to each day (expanding window); NaN until min_obs."""
    n = len(price); out = np.full(n, np.nan); sx = sy = sxx = sxy = 0.0; c = 0
    for i in range(n):
        d = (dates[i] - GENESIS).days
        if not (d > from_day and price[i] > 0): continue
        X = math.log10(d); Y = math.log10(price[i]); sx += X; sy += Y; sxx += X * X; sxy += X * Y; c += 1
        if c < min_obs: continue
        den = c * sxx - sx * sx
        if den <= 0: continue
        b = (c * sxy - sx * sy) / den; a0 = (sy - b * sx) / c; out[i] = Y - (a0 + b * X)
    return out


def state_of(x):
    if x is None or not np.isfinite(x): return None
    return 'very cheap' if x < 10 else 'cheap' if x < 30 else 'average' if x < 70 else 'expensive' if x < 90 else 'very expensive'


def valuation_composite(bc, cm):
    """Returns (doc, arrays). Price is the Blockchain.com daily average, positive days only, as on the pages."""
    pd_, pv = to_daily(bc['price'])
    keep = pv > 0; dates = [d for d, k in zip(pd_, keep) if k]; price = pv[keep]
    md, mv = to_daily(cm.get('CapMVRVCur', [])) if cm.get('CapMVRVCur') else ([], np.array([]))
    _, a = align(('price', (dates, price)), ('mvrv', (md, mv)))
    mvrv = a['mvrv']
    # forward-fill is capped at 7 days: beyond that the reading is unknown, not stale
    if len(md):
        mset = set(md); since = None
        for i, d in enumerate(dates):
            if d in mset: since = d
            if since is None or (d - since).days > 7: mvrv[i] = np.nan
    ma200 = _sma(price, 200); mayer = np.where(np.isfinite(ma200), price / ma200, np.nan)
    pld = _expanding_powerlaw_dev(dates, price)
    rM, rY, rP = _asof_rank(dates, mvrv), _asof_rank(dates, mayer), _asof_rank(dates, pld)
    comp = np.where(np.isfinite(rM) & np.isfinite(rY) & np.isfinite(rP), (rM + rY + rP) / 3, np.nan)
    first = next((i for i in range(len(dates)) if np.isfinite(comp[i])), None)
    rows = []
    for i in range(len(dates)):
        if first is None or i < first: continue
        rows.append([dates[i].isoformat(),
                     None if not np.isfinite(comp[i]) else round(float(comp[i]), 2),
                     None if not np.isfinite(rM[i]) else round(float(rM[i]), 1),
                     None if not np.isfinite(rY[i]) else round(float(rY[i]), 1),
                     None if not np.isfinite(rP[i]) else round(float(rP[i]), 1),
                     None if not np.isfinite(mvrv[i]) else round(float(mvrv[i]), 4),
                     None if not np.isfinite(mayer[i]) else round(float(mayer[i]), 4),
                     None if not np.isfinite(pld[i]) else round(float(pld[i]), 4)])
    today = rows[-1] if rows else None
    doc = {'source': 'derived', 'generated_at': dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
           'note': ('Equal-weight descriptive composite of three as-of percentiles (MVRV, Mayer multiple, deviation from an '
                    'expanding-window power law), each ranked against prior observations only from 2013 with at least 250 prior days; '
                    'cuts at 10/30/70/90 into very cheap / cheap / average / expensive / very expensive. Point-in-time: no row uses later data. '
                    'Price is the Blockchain.com daily average. This file is the single source for the Where Things Stand strip and the '
                    'Indicator Autopsy event study.'),
           'columns': ['date', 'composite', 'mvrv_pct', 'mayer_pct', 'powerlaw_pct', 'mvrv', 'mayer', 'powerlaw_dev_dex'],
           'cuts': [10, 30, 70, 90],
           'today': None if not today else {'date': today[0], 'composite': today[1], 'state': state_of(today[1]),
                                            'mvrv_pct': today[2], 'mayer_pct': today[3], 'powerlaw_pct': today[4]},
           'rows': rows}
    return doc, {'dates': dates, 'price': price, 'comp': comp, 'mvrv': mvrv, 'mayer': mayer, 'pld': pld}


# ---------------------------------------------------------------- Metcalfe: does the cumulative address-activity proxy add information beyond time?
def _ols(Y, X):
    """OLS with intercept; X is (n,k). Returns coef (k+1,), fitted, r2."""
    A = np.column_stack([np.ones(len(Y)), X]); coef, *_ = np.linalg.lstsq(A, Y, rcond=None)
    fit = A @ coef; ss = ((Y - Y.mean()) ** 2).sum(); r2 = 1 - ((Y - fit) ** 2).sum() / ss if ss > 0 else float('nan')
    return coef, fit, r2


def metcalfe_null_test(bc, start=dt.date(2017, 1, 1)):
    """Three nested regressions of ln price on the 2017 window, with the same expanding annual-refit
    out-of-sample protocol the Metcalfe page uses. The question is whether the user stock carries information
    beyond calendar time, which a price-time power law already captures; the answer is the incremental R² and,
    more importantly, the incremental out-of-sample error."""
    pd_, pv = to_daily(bc['price']); ad, av = to_daily(bc['unique_addresses']); cum = dict(zip(ad, np.cumsum(av)))
    keep = [i for i, d in enumerate(pd_) if pv[i] > 0 and d in cum and (d - GENESIS).days > 560]
    dates = [pd_[i] for i in keep]; price = pv[keep]; nusers = np.array([cum[d] for d in dates])
    sd, sv = to_daily(bc['supply']); _, a = align(('price', (dates, price)), ('supply', (sd, sv))); supply = a['supply']
    ok = np.isfinite(supply) & (nusers > 0); dates = [d for d, k in zip(dates, ok) if k]; price, nusers, supply = price[ok], nusers[ok], supply[ok]
    lnP = np.log(price); lt = np.log(np.array([(d - GENESIS).days for d in dates], float)); ln_net = np.log(nusers ** 2 / supply)
    models = {'time': lt[:, None], 'network': ln_net[:, None], 'both': np.column_stack([lt, ln_net])}
    w = np.array([d >= start for d in dates]); out = {}
    for name, X in models.items():
        coef, fit, r2 = _ols(lnP[w], X[w])
        errs = []; by_year = {}
        for Y in range(start.year + 3, dates[-1].year + 1):
            cut = dt.date(Y, 1, 1); end = dt.date(Y + 1, 1, 1)
            tr = np.array([start <= d < cut for d in dates]); te = np.array([cut <= d < end for d in dates])
            if tr.sum() < 365 * 3 or te.sum() == 0: continue
            c, _, _ = _ols(lnP[tr], X[tr]); pred = np.column_stack([np.ones(te.sum()), X[te]]) @ c
            e = list(lnP[te] - pred); errs += e; by_year[Y] = round(100 * math.sqrt(np.mean(np.square(e))), 1)
        rmse = round(100 * math.sqrt(np.mean(np.square(errs))), 1) if errs else None
        out[name] = {'r2_in_sample': round(float(r2), 4), 'oos_rmse_pts': rmse, 'oos_rmse_by_refit_year': by_year, 'coef': [round(float(x), 4) for x in coef[1:]]}
    out['incremental_r2_network_beyond_time'] = round(out['both']['r2_in_sample'] - out['time']['r2_in_sample'], 4)
    out['incremental_oos_pts_network_beyond_time'] = (None if out['both']['oos_rmse_pts'] is None or out['time']['oos_rmse_pts'] is None
                                                      else round(out['both']['oos_rmse_pts'] - out['time']['oos_rmse_pts'], 1))
    out['network_vs_time_oos_pts'] = (None if out['network']['oos_rmse_pts'] is None or out['time']['oos_rmse_pts'] is None
                                      else round(out['network']['oos_rmse_pts'] - out['time']['oos_rmse_pts'], 1))
    # the pooled difference is one number over a handful of refit years; show how it varies year by year, which is the only
    # uncertainty statement the sample supports (seven refits is not enough for a bootstrap interval to mean much)
    yrs = sorted(set(out['time']['oos_rmse_by_refit_year']) & set(out['network']['oos_rmse_by_refit_year']))
    diffs = [round(out['network']['oos_rmse_by_refit_year'][y] - out['time']['oos_rmse_by_refit_year'][y], 1) for y in yrs]
    out['network_vs_time_by_refit_year'] = dict(zip(yrs, diffs))
    out['network_better_in_years'] = f"{sum(1 for d in diffs if d < 0)} of {len(diffs)}"
    out['network_vs_time_range_pts'] = [min(diffs), max(diffs)] if diffs else None
    out['window'] = start.isoformat(); out['n_days'] = int(w.sum())
    out['note'] = ('ln price regressed on ln days-since-genesis (time), on ln(users²/supply) (network), and on both, 2017 window; '
                   'out-of-sample by expanding annual refits. network_vs_time compares the two single-regressor models; negative means the '
                   'user stock predicted next year better than calendar time; the year-by-year range says how stable that is. Within this specification the '
                   'user stock did not clearly outperform the simpler time model, and nothing here says the Metcalfe model fails. The joint model is reported because a reviewer asked for it, '
                   'and its coefficients are not separately identified: the two regressors are near-collinear on a series that grows six '
                   'orders of magnitude, which is why its out-of-sample error is worse than either alone. Descriptive, not causal: address '
                   'counts respond to price as well as drive it.')
    return out


# ---------------------------------------------------------------- how independent are the columns?
def _spearman(x, y):
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 60: return None, int(m.sum())
    def midrank(v):
        order = np.argsort(v, kind='mergesort'); r = np.empty(len(v)); sv = v[order]; i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and sv[j + 1] == sv[i]: j += 1
            r[order[i:j + 1]] = (i + j) / 2 + 1; i = j + 1
        return r
    rx, ry = midrank(x[m]), midrank(y[m]); c = np.corrcoef(rx, ry)[0, 1]
    return (None if not np.isfinite(c) else round(float(c), 2)), int(m.sum())


def independence(arrs, dv, st, fg, start=dt.date(2019, 1, 1)):
    """Spearman correlation of daily LEVELS since 2019 (when every derivatives series exists). Levels, because the
    question is whether two columns reading 'expensive' on the same day are two observations or one."""
    dates, price = arrs['dates'], arrs['price']
    # RSI 14 (Wilder) on the same daily-average price
    n = len(price); rsi = np.full(n, np.nan); au = ad = 0.0
    for i in range(1, n):
        d = price[i] - price[i - 1]
        if i <= 14: au += max(d, 0) / 14; ad += max(-d, 0) / 14
        else: au = (au * 13 + max(d, 0)) / 14; ad = (ad * 13 + max(-d, 0)) / 14
        if i >= 14: rsi[i] = 100 - 100 / (1 + au / ad) if ad > 0 else 100.0
    series = {'MVRV': arrs['mvrv'], 'Mayer multiple': arrs['mayer'], 'Power-law deviation': arrs['pld'],
              'Valuation composite': arrs['comp'], 'RSI 14': rsi}
    ext = {}
    for name, key, src in [('Funding', 'deribit_funding_8h_daily_mean', dv), ('Open interest, USD', 'okx_open_interest_usd', dv),
                           ('DVOL', 'deribit_dvol_close', dv), ('Fear and Greed', 'index', fg), ('Stablecoin supply', 'total_usd', st)]:
        if src and src.get(key):
            ds, vs = to_daily(src[key]); _, a = align(('price', (dates, price)), ('x', (ds, vs))); ext[name] = a['x']
    series.update(ext)
    w = np.array([d >= start for d in dates]); names = list(series); k = len(names); M = [[None] * k for _ in range(k)]; N = [[0] * k for _ in range(k)]
    for i in range(k):
        for j in range(k):
            if j < i: M[i][j], N[i][j] = M[j][i], N[j][i]; continue
            c, cnt = (1.0, int(w.sum())) if i == j else _spearman(series[names[i]][w], series[names[j]][w]); M[i][j] = c; N[i][j] = cnt
    # evidence overlap among the valuation-type columns (a correlation summary, not a latent-factor count): pairs above 0.8 overlap heavily
    strong = [(names[i], names[j], M[i][j]) for i in range(k) for j in range(i + 1, k) if M[i][j] is not None and abs(M[i][j]) >= 0.8]
    return {'window_from': start.isoformat(), 'names': names, 'spearman': M, 'n_days': N,
            'pairs_above_0_8': [{'a': a, 'b': b, 'rho': r} for a, b, r in strong],
            'note': ('Spearman rank correlation of daily levels since 2019 with midranks for ties. Pairs at or above 0.8 in absolute '
                     'value are, for the purpose of counting evidence, one observation: several columns agreeing is not several '
                     'confirmations. Levels are used rather than changes because the site\'s tables read states, not moves.')}


# ---------------------------------------------------------------- what changed: an Atom feed of snapshot-to-snapshot moves
def write_changes_feed(out_dir, hist_rows, kp, composite_doc, site='https://cryptoexponentials.com/tools/', keep=60):
    """One entry per snapshot date. Content is computed from the history rows, so an entry never says more than the data."""
    def esc(s): return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    rows = hist_rows[-keep:]; entries = []
    comp_by_date = {r[0]: r[1] for r in composite_doc['rows']} if composite_doc else {}
    for idx in range(len(rows)):
        r = rows[idx]; prev = rows[idx - 1] if idx > 0 else None; lines = []
        def pc(k, label):
            if prev and prev.get(k) and r.get(k): lines.append(f'{label} {r[k]:,.0f} ({100 * (r[k] / prev[k] - 1):+.1f}% vs {prev["date"]})')
            elif r.get(k): lines.append(f'{label} {r[k]:,.0f}')
        pc('price', 'Price'); pc('metcalfe_validated', 'Metcalfe, validated 2017'); pc('metcalfe', 'Metcalfe, reference 2011'); pc('powerlaw', 'Power-law level'); pc('realised', 'Realised price')
        c_now, c_prev = comp_by_date.get(r['date']), (comp_by_date.get(prev['date']) if prev else None)
        if c_now is not None:
            s_now, s_prev = state_of(c_now), state_of(c_prev)
            lines.append(f'Valuation state {s_now} (composite {c_now:.0f})' + (f', was {s_prev}' if s_prev and s_prev != s_now else ''))
        if r.get('composite') is not None: lines.append(f'Crowding composite {r["composite"]:.0f}')
        if idx == len(rows) - 1 and kp:
            sf = kp.get('selftest', {}); 
            if sf.get('status') and sf['status'] != 'ok': lines.append('Self-test: ' + sf['status'] + ' — ' + '; '.join(sf.get('warnings', []) + sf.get('failures', [])))
        upd = (r.get('generated_at') or (r['date'] + 'T06:15:00+00:00')).replace('+00:00', 'Z')
        entries.append(f'''  <entry>
    <title>Snapshot {esc(r['date'])}</title>
    <id>tag:cryptoexponentials.com,2026:snapshot:{esc(r['date'])}</id>
    <updated>{esc(upd)}</updated>
    <link href="{site}"/>
    <content type="text">{esc(chr(10).join(lines))}</content>
  </entry>''')
    latest = rows[-1] if rows else None
    xml = f'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Crypto Exponentials — daily snapshot changes</title>
  <subtitle>What moved between snapshots, computed from the published history. No signals, no recommendations.</subtitle>
  <id>tag:cryptoexponentials.com,2026:snapshot-feed</id>
  <link href="{site}"/>
  <link rel="self" href="https://akpasz.github.io/btc-data/data/changes.atom"/>
  <updated>{esc((latest.get('generated_at') if latest else dt.datetime.now(dt.timezone.utc).isoformat()).replace('+00:00', 'Z'))}</updated>
{chr(10).join(reversed(entries))}
</feed>
'''
    with open(os.path.join(out_dir, 'changes.atom'), 'w', encoding='utf-8') as f: f.write(xml)


def run(out_dir, bc, cm, dv, st, fg, kp, hist_rows):
    """Called from kpis.main after the headline block. Each part is isolated; a failure is recorded, not fatal."""
    res = {}
    comp_doc = None; arrs = None
    try:
        comp_doc, arrs = valuation_composite(bc, cm)
        with open(os.path.join(out_dir, 'composite.json'), 'w') as f: json.dump(comp_doc, f, separators=(',', ':'))
        res['composite'] = comp_doc['today']
    except Exception as e: res['composite_error'] = str(e)[:200]
    try: res['metcalfe_null_test'] = metcalfe_null_test(bc)
    except Exception as e: res['metcalfe_null_test_error'] = str(e)[:200]
    try:
        if arrs is not None: res['independence'] = independence(arrs, dv, st, fg)
    except Exception as e: res['independence_error'] = str(e)[:200]
    try: write_changes_feed(out_dir, hist_rows, kp, comp_doc)
    except Exception as e: res['feed_error'] = str(e)[:200]
    return res
