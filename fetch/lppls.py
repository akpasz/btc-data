"""
Log-Periodic Power Law Singularity (LPPLS) bubble detector, computed point-in-time.

Model (Johansen-Ledoit-Sornette), in the Filimonov & Sornette (2013) linearised form:
    ln p(t) = A + B (tc - t)^m + C1 (tc - t)^m cos(w ln(tc - t)) + C2 (tc - t)^m sin(w ln(tc - t))
A, B, C1, C2 are linear given (tc, m, w), so each candidate (tc, m, w) is scored by least squares
and only three parameters are searched: a coarse grid followed by Nelder-Mead polish.

PRIMARY specification (locked before any historical evaluation): the Gerlach, Demos & Sornette (2019)
qualification filters, Table 2, as published:
    0 < m < 1;  4 <= w <= 25;  B < 0 for a positive bubble (B > 0 for a negative one);
    T < tc <= T + dt, where dt is the window length;  damping = m|B| / (w|C|) >= 0.5;
    number of oscillations >= 2.5, applied only when |C/B| >= 0.05 (when the oscillation is negligible
    the count is not required).
SENSITIVITY specification, reported beside it and never used to select a result: a commonly used
restrictive LPPLS parameter set, not attributed to any single paper: 0.1 <= m <= 0.9; 6 <= w <= 13;
tc within a fifth of the window ahead; damping >= 1; oscillations >= 2.5.
The primary specification is fixed because it is an externally published rule set; alternative filters
are sensitivity analyses. Results are not selected on which set produces the better record.

Conventions: a window of W days is the last W observations, t1 = t[i-W+1] .. t2 = t[i]; dt = t2 - t1 = W - 1 days,
and the critical time is searched and filtered on (t2, t2 + dt]. Readings are computed through the last COMPLETED
daily-average observation in the price series; a partially formed current day is never used, so the series carries a
one-day lag by design.
The confidence indicator is the share of the eight window lengths whose best fit qualifies.
Everything is computed as of each day on data up to that day only; the history is DAILY from 2013.

Caveats recorded here and on the page:
  * The fit is multi-modal. The search is a fixed coarse grid (12 tc x 3 m x 3 w) followed by Nelder-Mead;
    the critical time is searched over the full interval the primary filter permits, (T, T + dt], with
    12 grid points then Nelder-Mead; the result is conditional on that search, and it is not the original
    authors' numerical optimiser.
  * Prices are daily averages, not closes.
  * The published fit diagnostics include C/|B| so the size of the oscillatory component can be read.
"""
import math, json, os, datetime as dt
import numpy as np

WINDOWS = [120, 180, 240, 300, 365, 450, 540, 730]
TC_EPS = 0.01  # days; the critical time may sit arbitrarily close to the window end, but not on or before it
RECOMPUTE_TRAILING_DAYS = 30  # readings inside this trailing span are recomputed each run, so a revised daily average is absorbed
FILTERS = {  # primary: Gerlach, Demos & Sornette (2019), Table 2
    'name': 'Gerlach-Demos-Sornette 2019 (primary, fixed)',
    'm': [0.0, 1.0], 'w': [4.0, 25.0], 'tc_max_frac': 1.0, 'damping_min': 0.5, 'osc_min': 2.5, 'osc_applies_if_C_over_B_at_least': 0.05}
FILTERS_STRICT = {  # sensitivity only
    'name': 'strict sensitivity specification: a commonly used restrictive LPPLS parameter set, not attributed to a single paper',
    'm': [0.1, 0.9], 'w': [6.0, 13.0], 'tc_max_frac': 0.2, 'damping_min': 1.0, 'osc_min': 2.5, 'osc_applies_if_C_over_B_at_least': 0.0}


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
    for tc in np.linspace(T + TC_EPS, T + span, 12):  # the whole permitted interval (T, T + dt], dt = t2 - t1
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
        if tc <= T + TC_EPS or tc > T + span or m < 0.001 or m > 0.999 or w < 2 or w > 25:
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
            'c_over_b': (C / abs(B)) if B != 0 else float('inf'),
            'osc': (w / (2 * math.pi)) * math.log((tc - T + span) / (tc - T)) if tc > T else 0.0,
            'rel_rmse': math.sqrt(sse / n) / abs(float(np.mean(y))) if np.mean(y) != 0 else 99.0,
            'r2': 1 - sse / float(np.sum((y - y.mean()) ** 2))}


def qualifies(f, sign, F=None):
    F = F or FILTERS
    if f is None:
        return False
    if not (F['m'][0] < f['m'] < F['m'][1] and F['w'][0] <= f['w'] <= F['w'][1]):
        return False
    if sign > 0 and not f['B'] < 0:
        return False
    if sign < 0 and not f['B'] > 0:
        return False
    if not (f['T'] < f['tc'] <= f['T'] + F['tc_max_frac'] * f['span']):
        return False
    if f['damping'] < F['damping_min']:
        return False
    if f['c_over_b'] >= F['osc_applies_if_C_over_B_at_least'] and f['osc'] < F['osc_min']:
        return False
    return True


def reading(logp, i):
    """Point-in-time reading at index i of the log-price array, under the primary and the strict filters."""
    pos = neg = pos_s = tot = 0; tcs = []; best_pos = None
    for W in WINDOWS:
        if i - W + 1 < 0:
            continue
        t = np.arange(i - W + 1, i + 1, dtype=float)
        f = fit_window(t, logp[i - W + 1:i + 1])
        tot += 1
        if qualifies(f, +1):
            pos += 1; tcs.append(f['tc'] - f['T'])
            if best_pos is None or f['r2'] > best_pos['r2']:
                best_pos = f
        elif qualifies(f, -1):
            neg += 1
        if qualifies(f, +1, FILTERS_STRICT):
            pos_s += 1
    if tot < len(WINDOWS):
        return None  # the published reading is a share of eight windows; fewer than eight is not published
    return {'pos': pos / tot, 'neg': neg / tot, 'pos_strict': pos_s / tot, 'windows': tot,
            'tc_days': float(np.median(tcs)) if tcs else None,
            'best': {k: round(best_pos[k], 4) for k in ('m', 'w', 'B', 'C', 'c_over_b', 'damping', 'osc', 'r2', 'rel_rmse')} if best_pos else None}


def spec_hash():
    """A digest of everything that determines a fit: the windows, the filters,
    the grid constants. The stored history is only reusable while this is
    unchanged; a changed specification forces a full recomputation rather
    than extending an incrementally-built series that no longer matches."""
    import hashlib, json as _j
    spec = {'windows': WINDOWS, 'tc_eps': TC_EPS, 'filters': FILTERS, 'filters_strict': FILTERS_STRICT,
            'recompute_trailing_days': RECOMPUTE_TRAILING_DAYS}
    return hashlib.sha256(_j.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()[:16]


def input_hash(dates, prices):
    """A digest of the price history the fits were made on. If the provider
    revises history, the stored fits were made on data that no longer exists."""
    import hashlib
    h = hashlib.sha256()
    for d, p in zip(dates, prices):
        h.update(f'{d}:{float(p):.6f};'.encode())
    return h.hexdigest()[:16]


def build_history(dates, prices, out_path, existing=None, log=print, baseline=None):
    """Compute the DAILY confidence series from 2013, reusing anything already stored. Returns the document."""
    logp = np.log(np.asarray(prices, dtype=float))
    n = len(logp)
    have = {}
    _spec = spec_hash()
    # The input hash excludes the trailing span that is recomputed every run,
    # so a revised recent daily average does not force a full rebuild.
    _inp = input_hash(dates[:-RECOMPUTE_TRAILING_DAYS], prices[:-RECOMPUTE_TRAILING_DAYS])
    if existing and (existing.get('spec_hash') != _spec or existing.get('input_hash') != _inp):
        log(f'  lppls: stored history was built under spec {existing.get("spec_hash")} / input {existing.get("input_hash")}; '
            f'now {_spec} / {_inp} - discarding it and recomputing in full')
        existing = None; baseline = None
    if existing:
        for d, v in existing.get('series', {}).get('lppls_pos', []):
            have[d] = True
    rows = {}
    if existing:
        for key in ('lppls_pos', 'lppls_neg', 'lppls_pos_strict', 'lppls_tc_days'):
            for d, v in existing.get('series', {}).get(key, []):
                rows.setdefault(d, {})[key] = v
    start = next((k for k in range(n) if dates[k] >= dt.date(2013, 1, 1)), 0)
    todo = [i for i in range(start, n) if dates[i].isoformat() not in have or i >= n - RECOMPUTE_TRAILING_DAYS]
    log(f'  lppls: {len(todo)} days to compute ({len(have)} already stored)')
    for k, i in enumerate(todo):
        r = reading(logp, i)
        if r is None:
            continue
        d = dates[i].isoformat()
        rows[d] = {'lppls_pos': round(r['pos'], 3), 'lppls_neg': round(r['neg'], 3), 'lppls_pos_strict': round(r['pos_strict'], 3), 'lppls_tc_days': (round(r['tc_days'], 1) if r['tc_days'] is not None else None)}
        if k % 100 == 0 and k:
            log(f'  lppls: {k}/{len(todo)}')
    series = {key: sorted([[d, v.get(key)] for d, v in rows.items() if v.get(key) is not None]) for key in ('lppls_pos', 'lppls_neg', 'lppls_pos_strict', 'lppls_tc_days')}
    today = reading(logp, n - 1)
    doc = {'source': 'lppls', 'computed_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
           'method': 'Filimonov-Sornette (2013) linearised LPPLS; fixed grid (12 tc over the full permitted interval x 3 m x 3 w) then Nelder-Mead; confidence = share of the 8 window lengths whose best fit passes the filters',
           'windows_days': WINDOWS, 'window_convention': 'a W-day window is the last W observations; dt = t2 - t1 = W - 1; tc searched and filtered on (t2, t2 + dt]',
           'timing_convention': 'computed through the last completed daily-average observation; a partial current day is never used (one-day lag by design)',
           'filters_primary': FILTERS, 'filters_sensitivity': FILTERS_STRICT,
           'specification_note': 'the primary specification is the published Gerlach-Demos-Sornette (2019) rule set, fixed before evaluation; the strict set is a sensitivity analysis and is never used to select a result',
           'history_note': 'daily from 2013-01-01, computed point-in-time on daily-average prices',
           'random_baseline': baseline,
           'spec_hash': _spec, 'input_hash': _inp,
           'today': today, 'series': series}
    tmp = out_path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(doc, f, separators=(',', ':'))
    os.replace(tmp, out_path)  # atomic on POSIX: the previous file survives any failure before this line
    return doc


def random_baseline(dates, prices, series_pos, threshold=0.5, draws=10000, seed=2026, direction='top'):
    """Signal-day evaluation against a block-matched random baseline. Deterministic seed so the published
    figure is reproducible; recomputed on each run as the history grows."""
    import random as _r
    _r.seed(seed)
    p = np.asarray(prices, dtype=float); n = len(p)
    idx = {d.isoformat(): i for i, d in enumerate(dates)}
    i0 = next((k for k in range(n) if dates[k] >= dt.date(2013, 1, 1)), 0)
    elig = list(range(i0, n - 366))
    if len(elig) < 400:
        return None
    dec = np.zeros(n, bool); up = np.zeros(n, bool)
    for i in elig:
        seg = p[i + 1:i + 366]; dec[i] = seg.min() / p[i] - 1 <= -0.40; up[i] = seg.max() / p[i] - 1 >= 1.0
    es = set(elig)
    sig = sorted(idx[d] for d, v in series_pos if v >= threshold and idx.get(d) in es)
    if not sig:
        return {'signal_days': 0, 'eligible_days': len(elig), 'hit_rate_all_days': float(dec[elig].mean())}
    runs = []
    for i in sig:
        if runs and i - runs[-1][-1] <= 7: runs[-1].append(i)
        else: runs.append([i])
    lens = [len(r) for r in runs]
    # a NEGATIVE bubble is an accelerating decline; its claim is that a bottom is
    # near, so its hit is the mirror of the top rule's: a doubling within a year
    target = dec if direction == 'top' else up
    hit = float(target[sig].mean()); base = float(target[elig].mean())
    def draw():
        # exact non-overlap: each placed block is an inclusive interval [s, s+L-1]; a candidate is
        # rejected if it intersects any placed interval, whatever the two lengths are
        tot = 0.0; placed = []
        for L in sorted(lens, reverse=True):  # longest first so the long blocks always find room
            for _ in range(10000):
                s = _r.choice(elig); e = s + L - 1
                if e <= elig[-1] and all(e < a or s > b for a, b in placed): break
            else:
                raise RuntimeError('could not place a block without overlap')
            placed.append((s, e)); tot += target[s:s + L].mean() * L
        return tot / sum(lens)
    dist = np.array([draw() for _ in range(draws)])
    _rule = ('signal days = daily confidence >= 0.5 with a completed 365-day window; hit = '
             + ('lowest close within 365 days at least 40% below' if direction == 'top'
                else 'highest close within 365 days at least 100% above')
             + '; baseline = random day-blocks matching the number and lengths of the signal runs')
    return {'rule': _rule, 'direction': direction,
            'signal_days': len(sig), 'eligible_days': len(elig), 'runs': len(runs), 'median_run_days': int(np.median(lens)), 'longest_run_days': int(max(lens)),
            'hit_rate_signal': hit, 'hit_rate_all_days': base, 'doubling_rate_signal': float(up[sig].mean()), 'doubling_rate_all_days': float(up[elig].mean()),
            'random_mean': float(dist.mean()), 'random_p5': float(np.percentile(dist, 5)), 'random_p95': float(np.percentile(dist, 95)),
            'p_random_at_least_observed': float((dist >= hit).mean()), 'draws': draws, 'seed': seed}


def critical_time_test(dates, prices, series_pos, series_tc, threshold=0.5, tolerance=30, seed=2026, draws=10000):
    """Test what LPPLS actually claims: a critical TIME, not a magnitude.

    random_baseline scores "a 40% fall within a year" - a reasonable reading of
    a bubble signal, but not the model's own assertion. LPPLS fits t_c, the
    date the regime ends. This asks: at the START of each signal run, did the
    highest close of the following year fall within +-tolerance days of the
    fitted t_c?

    Scored per RUN, not per day. Days inside a run share nearly the same t_c
    and the same forward year, so counting each as a trial would inflate the
    sample roughly thirty-fold and manufacture significance. This matches how
    every other rule on the site counts: episodes, not days.

    Baseline: the same question for random start days, with t_c offsets drawn
    from the runs' own offsets, so the comparison is like for like. Two
    tolerances are reported so a reader can see the result is not an artefact
    of one window.
    """
    import random as _r
    _r.seed(seed)
    p = np.asarray(prices, dtype=float); n = len(p)
    idx = {d.isoformat(): i for i, d in enumerate(dates)}
    tc = dict((d, float(v)) for d, v in series_tc if v is not None)
    i0 = next((k for k in range(n) if dates[k] >= dt.date(2013, 1, 1)), 0)
    elig = [i for i in range(i0, n - 366)]
    es = set(elig)
    sig = sorted(idx[d] for d, v in series_pos if v >= threshold and idx.get(d) in es)
    runs = []
    for i in sig:
        if runs and i - runs[-1][-1] <= 7: runs[-1].append(i)
        else: runs.append([i])
    # one trial per run: its first day and that day's fitted t_c
    trials = []
    for r in runs:
        d0 = dates[r[0]].isoformat()
        if d0 in tc and 0 < tc[d0] <= 365:
            trials.append((r[0], tc[d0]))
    if len(trials) < 5:
        return {'runs': len(runs), 'trials': len(trials), 'note': 'too few runs with a fitted t_c inside the year'}
    def near_top(i, offset, tol):
        seg = p[i + 1:i + 366]
        peak = i + 1 + int(np.argmax(seg))
        return abs(peak - (i + offset)) <= tol
    offsets = [off for _, off in trials]
    out = {'rule': ('one trial per signal run: at its first day, did the highest close of the following '
                    '365 days fall within the tolerance of the fitted t_c; baseline = random start days '
                    'with t_c offsets resampled from the runs'),
           'runs': len(runs), 'trials': len(trials), 'median_tc_days_ahead': float(np.median(offsets)),
           'draws': draws, 'seed': seed, 'by_tolerance': {}}
    for tol in (15, 30, 60):
        hits = [near_top(i, off, tol) for i, off in trials]
        hit = float(np.mean(hits))
        dist = []
        for _ in range(draws):
            ok = 0
            for _k in range(len(trials)):
                ok += near_top(_r.choice(elig), _r.choice(offsets), tol)
            dist.append(ok / len(trials))
        dist = np.array(dist)
        out['by_tolerance'][str(tol)] = {
            'hit_rate_signal': hit, 'hits': int(sum(hits)),
            'random_mean': float(dist.mean()),
            'random_p5': float(np.percentile(dist, 5)), 'random_p95': float(np.percentile(dist, 95)),
            'p_random_at_least_observed': float((dist >= hit).mean())}
    return out
