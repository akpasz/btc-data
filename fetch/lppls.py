"""
Log-Periodic Power Law Singularity (LPPLS) bubble detector, computed point-in-time.

Model (Johansen-Ledoit-Sornette), in the Filimonov & Sornette (2013) linearised form:
    ln p(t) = A + B (tc - t)^m + C1 (tc - t)^m cos(w ln(tc - t)) + C2 (tc - t)^m sin(w ln(tc - t))
A, B, C1, C2 are linear given (tc, m, w), so each candidate (tc, m, w) is scored by least squares
and only three parameters are searched: a coarse grid followed by Nelder-Mead polish.

A fit on a window qualifies as a POSITIVE bubble when (Gerlach, Demos & Sornette 2019 style):
    0.01 <= m <= 0.99,  2 <= w <= 25,  B < 0,  T < tc <= T + 0.1 * window,
    damping = m|B| / (w |C|) >= 0.5,  at least 2.5 log-periodic oscillations inside the window,
    relative RMSE <= 0.05.
A NEGATIVE bubble uses the same filters with B > 0 (accelerating decline).

The published indicator is the share of window lengths whose best fit qualifies. Windows: 120 to 730
days. Everything is computed as of each day on data up to that day only; no later prices are used.

Honest caveats, recorded once here and again on the page:
  * The fit is multi-modal. Confidence depends on the optimiser finding the minimum; a coarse grid is
    used so that the 2017 textbook fits are found, and the search is fixed rather than tuned per day.
  * The filter set is a choice. The Sornette group's own thresholds vary between papers (damping >= 1.0
    in one, >= 0.5 in another). The looser published set is used because it is the one under which the
    model performs best; a model tested at its most favourable setting that still fails is a fair test.
  * Prices are daily averages, not closes.
"""
import math, json, os, datetime as dt
import numpy as np

WINDOWS = [120, 180, 240, 300, 365, 450, 540, 730]
FILTERS = {'m': [0.01, 0.99], 'w': [2, 25], 'tc_max_frac': 0.1, 'damping_min': 0.5, 'osc_min': 2.5, 'rel_rmse_max': 0.05}


def _lin(t, y, tc, m, w):
    dtc = tc - t
    if np.any(dtc <= 0):
        return None
    f = dtc ** m
    lg = np.log(dtc)
    X = np.column_stack([np.ones_like(t), f, f * np.cos(w * lg), f * np.sin(w * lg)])
    coef = np.linalg.lstsq(X, y, rcond=None)[0]
    r = y - X @ coef
    return coef, float(np.sum(r * r))


def fit_window(t, y, maxiter=400):
    """Best LPPLS fit on one window. t in days (monotone), y = ln price. Returns a dict or None."""
    from scipy.optimize import minimize
    T = t[-1]; span = t[-1] - t[0]; best = None
    for tc in np.linspace(T + 2, T + 0.4 * span, 8):
        for m in (0.2, 0.5, 0.8):
            for w in (6.0, 9.0, 12.0):
                r = _lin(t, y, tc, m, w)
                if r is not None and (best is None or r[1] < best[0]):
                    best = (r[1], tc, m, w)
    if best is None:
        return None
    _, tc0, m0, w0 = best

    def obj(p):
        tc, m, w = p
        if tc <= T + 1 or m < 0.01 or m > 0.99 or w < 2 or w > 25:
            return 1e9
        r = _lin(t, y, tc, m, w)
        return 1e9 if r is None else r[1]

    res = minimize(obj, [tc0, m0, w0], method='Nelder-Mead', options={'xatol': 1e-3, 'fatol': 1e-7, 'maxiter': maxiter})
    tc, m, w = res.x
    r = _lin(t, y, tc, m, w)
    if r is None:
        return None
    (A, B, C1, C2), sse = r
    C = math.hypot(C1, C2)
    n = len(y)
    return {'T': float(T), 'span': float(span), 'tc': float(tc), 'm': float(m), 'w': float(w), 'A': float(A), 'B': float(B), 'C': float(C),
            'damping': (m * abs(B)) / (w * C) if C > 0 else 99.0,
            'osc': (w / (2 * math.pi)) * math.log((tc - T + span) / (tc - T)) if tc > T else 0.0,
            'rel_rmse': math.sqrt(sse / n) / abs(float(np.mean(y))) if np.mean(y) != 0 else 99.0,
            'r2': 1 - sse / float(np.sum((y - y.mean()) ** 2))}


def qualifies(f, sign):
    if f is None:
        return False
    F = FILTERS
    if not (F['m'][0] <= f['m'] <= F['m'][1] and F['w'][0] <= f['w'] <= F['w'][1]):
        return False
    if sign > 0 and not f['B'] < 0:
        return False
    if sign < 0 and not f['B'] > 0:
        return False
    if not (f['T'] < f['tc'] <= f['T'] + F['tc_max_frac'] * f['span']):
        return False
    return f['damping'] >= F['damping_min'] and f['osc'] >= F['osc_min'] and f['rel_rmse'] <= F['rel_rmse_max']


def reading(logp, i):
    """Point-in-time reading at index i of the log-price array."""
    pos = neg = tot = 0; tcs = []; best_pos = None
    for W in WINDOWS:
        if i - W < 0:
            continue
        t = np.arange(i - W, i + 1, dtype=float)
        f = fit_window(t, logp[i - W:i + 1])
        tot += 1
        if qualifies(f, +1):
            pos += 1; tcs.append(f['tc'] - f['T'])
            if best_pos is None or f['r2'] > best_pos['r2']:
                best_pos = f
        elif qualifies(f, -1):
            neg += 1
    if tot == 0:
        return None
    return {'pos': pos / tot, 'neg': neg / tot, 'windows': tot,
            'tc_days': float(np.median(tcs)) if tcs else None,
            'best': {k: round(best_pos[k], 4) for k in ('m', 'w', 'B', 'C', 'damping', 'osc', 'r2')} if best_pos else None}


def build_history(dates, prices, out_path, stride_days=7, daily_last=400, existing=None, log=print):
    """Compute the confidence series: weekly stride for the deep history, daily for the recent past,
    reusing anything already stored. Returns the document written to out_path."""
    logp = np.log(np.asarray(prices, dtype=float))
    n = len(logp)
    have = {}
    if existing:
        for d, v in existing.get('series', {}).get('lppls_pos', []):
            have[d] = True
    rows = {}
    if existing:
        for key in ('lppls_pos', 'lppls_neg', 'lppls_tc_days'):
            for d, v in existing.get('series', {}).get(key, []):
                rows.setdefault(d, {})[key] = v
    start = next((k for k in range(n) if dates[k] >= dt.date(2013, 1, 1)), 0)
    todo = [i for i in range(start, n) if (i >= n - daily_last or (i - start) % stride_days == 0) and dates[i].isoformat() not in have]
    log(f'  lppls: {len(todo)} days to compute ({len(have)} already stored)')
    for k, i in enumerate(todo):
        r = reading(logp, i)
        if r is None:
            continue
        d = dates[i].isoformat()
        rows[d] = {'lppls_pos': round(r['pos'], 3), 'lppls_neg': round(r['neg'], 3), 'lppls_tc_days': (round(r['tc_days'], 1) if r['tc_days'] is not None else None)}
        if k % 100 == 0 and k:
            log(f'  lppls: {k}/{len(todo)}')
    series = {key: sorted([[d, v.get(key)] for d, v in rows.items() if v.get(key) is not None]) for key in ('lppls_pos', 'lppls_neg', 'lppls_tc_days')}
    today = reading(logp, n - 1)
    doc = {'source': 'lppls', 'computed_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
           'method': 'Filimonov-Sornette linearised LPPLS; grid + Nelder-Mead; share of qualifying windows',
           'windows_days': WINDOWS, 'filters': FILTERS,
           'history_note': f'weekly stride before the last {daily_last} days, daily after; computed point-in-time on daily-average prices',
           'today': today, 'series': series}
    with open(out_path, 'w') as f:
        json.dump(doc, f, separators=(',', ':'))
    return doc
