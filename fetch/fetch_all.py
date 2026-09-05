#!/usr/bin/env python3
"""
Crypto Exponentials data snapshot.
Pulls free, keyless public sources once a day, merges with the existing history in data/,
and writes one JSON file per source plus data/manifest.json describing what succeeded.

Every source is isolated: a failure records an error in the manifest and leaves the previous
file untouched. Nothing here needs an API key or a paid plan.
"""
import json, os, sys, time, datetime as dt, io, csv, math
from typing import Dict, List, Tuple
import requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

OUT = os.path.join(os.path.dirname(__file__), '..', 'data')
UA = {'User-Agent': 'CryptoExponentials-DataSnapshot/1.0 (+https://cryptoexponentials.com/tools/)'}
NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
S = requests.Session(); S.headers.update(UA)
manifest: Dict[str, dict] = {}

def get(url, tries=3, **kw):
    """Free endpoints rate-limit unpredictably from shared CI addresses. A 429 or a 5xx is usually gone
    within a minute, so retry a couple of times with a growing pause before treating it as a failure."""
    last = None
    for k in range(tries):
        try:
            r = S.get(url, timeout=60, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f'{r.status_code} Client Error: {r.reason} for url: {url}', response=r)
            r.raise_for_status(); return r
        except Exception as e:
            last = e
            if k < tries - 1: time.sleep(4 * (k + 1))
    raise last

def load_existing(name):
    p = os.path.join(OUT, name + '.json')
    if os.path.exists(p):
        try:
            with open(p) as f: return json.load(f)
        except Exception: return None
    return None

def merge_series(old: Dict[str, List], new: Dict[str, List]) -> Dict[str, List]:
    """Union by date, new values win. Series are lists of [date, value]."""
    out = {}
    for k in set(list(old.keys()) + list(new.keys())):
        m = {}
        for d, v in old.get(k, []): m[d] = v
        # A null in the new payload means "this source had nothing for that date",
        # not "delete what we stored". Letting it through overwrote a good value and
        # the filter below then dropped the date entirely — permanent, silent loss,
        # because save() writes the merged result back over the file.
        for d, v in new.get(k, []):
            if v is not None: m[d] = v
        out[k] = sorted([[d, v] for d, v in m.items() if v is not None])
    return out

def save(name, source_url, series: Dict[str, List], note=''):
    old = load_existing(name)
    merged = merge_series(old['series'] if old else {}, series)
    doc = {'schema_version': SCHEMA_VERSION, 'source': name, 'source_url': source_url, 'fetched_at': NOW, 'note': note, 'series': merged}
    with open(os.path.join(OUT, name + '.json'), 'w') as f: json.dump(doc, f, separators=(',', ':'))
    last = max((s[-1][0] for s in merged.values() if s), default=None)
    manifest[name] = {'status': 'ok', 'fetched_at': NOW, 'metrics': {k: len(v) for k, v in merged.items()}, 'last_date': last, 'source_url': source_url}
    manifest[name].update(freshness(name, last))
    print(f'  ok  {name}: {sum(len(v) for v in merged.values())} points, last {last}')

# HTTP 200 is not freshness: a source can answer with yesterday's data. Each source has an expected maximum age in days;
# beyond it the run is still 'ok' (the fetch worked) but freshness is 'stale', and the dashboard can say so.
# These files are, in effect, a small public API: anyone may read them directly.
# SCHEMA_VERSION is stamped on every published document so a consumer can tell,
# without guessing, whether the shape it was written against still holds.
#   MAJOR  a key was removed or its meaning changed - consumers must be updated
#   MINOR  a key was added; existing readers keep working
# History
#   1.0  first versioned publish. Shape as of September 2026: every source file
#        carries {source, source_url, fetched_at, note, series}; series values are
#        [date, number] pairs sorted ascending with nulls excluded.
SCHEMA_VERSION = '1.0'

EXPECTED_MAX_AGE_DAYS = {'lppls': 3, 'macro': 10, 'coinmetrics': 3, 'fred': 5, 'derivatives': 2, 'relative': 5, 'etf_flows': 4, 'coingecko_global': 2, 'etf_quarterly': 135}
def freshness(name, last_date):
    if not last_date: return {'freshness': 'unknown', 'age_days': None, 'expected_max_age_days': EXPECTED_MAX_AGE_DAYS.get(name, 2)}
    try: age = (dt.datetime.fromisoformat(NOW).date() - dt.date.fromisoformat(last_date[:10])).days
    except Exception: return {'freshness': 'unknown', 'age_days': None, 'expected_max_age_days': EXPECTED_MAX_AGE_DAYS.get(name, 2)}
    thr = EXPECTED_MAX_AGE_DAYS.get(name, 2)
    return {'freshness': 'current' if age <= thr else 'stale', 'age_days': age, 'expected_max_age_days': thr}

def fail(name, e, source_url=''):
    """A source that could not be refreshed is only an ERROR if the site would now show something wrong.
    If the stored file still carries a value inside that source's expected max age, the honest state is
    STALE: the figure on the page is real, just older than an hour ago. Distinguishing the two keeps the
    red notice meaningful — it fires when a number is missing or genuinely out of date, not when a free
    endpoint rate-limited one run out of twenty."""
    old = load_existing(name)
    last = None
    if old:
        try: last = max((v[-1][0] for v in (old.get('series') or {}).values() if v), default=None)
        except Exception: last = None
        if not last: last = (old.get('fetched_at') or '')[:10] or None
    fr = freshness(name, last)
    usable = bool(old) and fr['freshness'] == 'current'
    manifest[name] = {'status': 'stale' if usable else 'error', 'error': str(e)[:300], 'fetched_at': NOW,
                      'source_url': source_url, 'kept_previous': bool(old),
                      'previous_fetched_at': old.get('fetched_at') if old else None,
                      'serving': ('last good value, ' + str(fr['age_days']) + ' day(s) old') if usable else 'nothing',
                      **({**fr, 'freshness': 'stale'} if usable else fr)}
    print(f"  {'stl' if usable else 'ERR'} {name}: {str(e)[:200]}", file=sys.stderr)

def day(ts): return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime('%Y-%m-%d')

# ---------------------------------------------------------------- Blockchain.com
def src_blockchain():
    name = 'blockchain'; base = 'https://api.blockchain.info/charts/'
    charts = {'price': 'market-price', 'unique_addresses': 'n-unique-addresses', 'supply': 'total-bitcoins',
              'transactions': 'n-transactions', 'utxo_count': 'utxo-count', 'hash_rate': 'hash-rate',
              'tx_volume_usd': 'estimated-transaction-volume-usd', 'fees_usd': 'transaction-fees-usd'}
    series = {}; errs = []
    for key, chart in charts.items():
        try:
            j = get(base + chart, params={'timespan': 'all', 'sampled': 'false', 'format': 'json'}).json()
            series[key] = [[day(p['x']), p['y']] for p in j['values']]
        except Exception as e:
            errs.append(f'{key}: {e}')
    if not series: raise RuntimeError('; '.join(errs))
    save(name, base, series, note=('partial: ' + '; '.join(errs)) if errs else '')

# ---------------------------------------------------------------- Coin Metrics community
def src_coinmetrics():
    name = 'coinmetrics'; base = 'https://community-api.coinmetrics.io/v4/timeseries/asset-metrics'
    wanted = ['PriceUSD', 'CapMrktCurUSD', 'CapRealUSD', 'CapMVRVCur', 'AdrActCnt', 'SplyCur', 'TxCnt', 'TxTfrValAdjUSD',
              'FeeTotUSD', 'HashRate', 'IssTotUSD', 'DiffMean', 'ROI30d', 'VtyDayRet30d', 'SplyAct1yr', 'SplyAct180d', 'SplyAct30d',
              'SplyAct2yr', 'SplyAct5yr', 'SplyAct10yr', 'NVTAdj', 'NVTAdj90', 'AdrBalCnt', 'AdrBal1in1MCnt', 'SplyFF']
    def fetch(metrics):
        rows = []; url = base; params = {'assets': 'btc', 'metrics': ','.join(metrics), 'frequency': '1d',
                                         'start_time': '2010-07-17', 'page_size': 10000}
        while url:
            j = get(url, params=params).json(); rows += j.get('data', []); url = j.get('next_page_url'); params = None
        return rows
    try:
        rows = fetch(wanted); ok = wanted
    except requests.HTTPError:
        ok = []; rows = []
        for m in wanted:  # find the subset the community tier supports
            try: fetch([m]); ok.append(m)
            except requests.HTTPError: pass
        if ok: rows = fetch(ok)
    if not rows: raise RuntimeError('no metrics available')
    series = {m: [] for m in ok}
    for r in rows:
        d = r['time'][:10]
        for m in ok:
            v = r.get(m)
            if v not in (None, ''): series[m].append([d, float(v)])
    save(name, base, {k: v for k, v in series.items() if v}, note=f'community tier; {len(ok)} of {len(wanted)} metrics available')

# ---------------------------------------------------------------- Coinbase spot
def src_coinbase():
    name = 'coinbase'; url = 'https://api.coinbase.com/v2/prices/BTC-USD/spot'
    p = float(get(url).json()['data']['amount'])
    save(name, url, {'spot': [[NOW[:10], p]]}, note='daily spot at fetch time; intraday live price is fetched by the pages themselves')

# ---------------------------------------------------------------- mempool.space
def src_mempool():
    name = 'mempool'; base = 'https://mempool.space/api/v1/'
    series = {}
    j = get(base + 'mining/hashrate/3y').json()
    series['hash_rate_ehs'] = [[day(p['timestamp']), p['avgHashrate'] / 1e18] for p in j.get('hashrates', [])]
    series['difficulty'] = [[day(p['time']), p['difficulty']] for p in j.get('difficulty', [])]
    f = get(base + 'fees/recommended').json()
    series['fee_fastest_sat_vb'] = [[NOW[:10], f.get('fastestFee')]]
    save(name, base, series)

# ---------------------------------------------------------------- DefiLlama stablecoins
def src_stablecoins():
    name = 'stablecoins'; url = 'https://stablecoins.llama.fi/stablecoincharts/all'
    j = get(url).json(); series = {'total_usd': []}
    for p in j:
        tot = p.get('totalCirculatingUSD', {}); v = tot.get('peggedUSD') if isinstance(tot, dict) else None
        if v: series['total_usd'].append([day(p['date']), float(v)])
    save(name, url, series)

# ---------------------------------------------------------------- FRED macro (CSV, no key)
def src_fred():
    name = 'fred'; base = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id='
    ids = {'dollar_index_broad': 'DTWEXBGS', 'fed_funds_effective': 'DFF', 'treasury_10y': 'DGS10', 'real_yield_10y': 'DFII10', 'm2': 'M2SL',
           'walcl': 'WALCL', 'tga': 'WTREGEN', 'rrp_on': 'RRPONTSYD'}  # net liquidity trio: WALCL/TGA in $M, RRP in $B (kpis.py handles units)
    series = {}; errs = []
    for key, sid in ids.items():
        try:
            txt = get(base + sid).text; rd = csv.DictReader(io.StringIO(txt)); col = [c for c in rd.fieldnames if c != 'observation_date' and c != 'DATE'][0]
            pts = []
            for row in rd:
                d = row.get('observation_date') or row.get('DATE'); v = row.get(col)
                if v not in (None, '', '.') and d >= '2009-01-01': pts.append([d, float(v)])
            series[key] = pts
        except Exception as e: errs.append(f'{key}: {e}')
    if not series: raise RuntimeError('; '.join(errs))
    save(name, 'https://fred.stlouisfed.org/', series, note=('partial: ' + '; '.join(errs)) if errs else '')

# ---------------------------------------------------------------- Fear and Greed
def src_fng():
    name = 'fear_greed'; url = 'https://api.alternative.me/fng/?limit=0&format=json'
    j = get(url).json(); series = {'index': [[day(p['timestamp']), float(p['value'])] for p in j.get('data', [])]}
    save(name, url, series)

# ---------------------------------------------------------------- Offshore spot via OKX (Binance blocks US runner IPs with HTTP 451)
def src_offshore_spot():
    """BTC-USDT daily closes from OKX; the offshore leg of the Coinbase premium in kpis.py. OKX is already
    a trusted source here (open interest) and serves US-based CI runners. USDT quote, so the USDT peg
    deviation is inside the premium; that caveat is printed with the metric. 300 daily candles per call;
    history accumulates via the merge."""
    name = 'offshore_spot'; url = 'https://www.okx.com/api/v5/market/candles'
    j = get(url, params={'instId': 'BTC-USDT', 'bar': '1D', 'limit': '300'}).json()
    rows = j.get('data') or []
    series = {'close_usdt': sorted([[day(int(r[0]) / 1000), float(r[4])] for r in rows])}
    if not series['close_usdt']: raise RuntimeError('okx candles empty: ' + str(j)[:150])
    save(name, url, series, note='OKX BTC-USDT daily close; offshore leg of the Coinbase premium')

# ---------------------------------------------------------------- CoinGecko global (UNTESTED stub: verify on first run)
def src_coingecko_global():
    name = 'coingecko_global'; url = 'https://api.coingecko.com/api/v3/global'
    j = get(url).json()
    d = float(j['data']['market_cap_percentage']['btc'])
    save(name, url, {'btc_dominance_pct': [[NOW[:10], d]]}, note='one point per day; history accumulates via merge')

# ---------------------------------------------------------------- Derivatives (Deribit and OKX public)
def cot_columns_ok(v, tol=1.0):
    """Self-test for the CFTC Traders in Financial Futures column layout, using the report's own identities:
    each side's category legs plus the spreading columns must equal total reportable, and total reportable
    plus non-reportable must equal open interest. If the file's layout ever shifts, this returns False and
    the caller skips the row instead of publishing a number read from the wrong column."""
    try:
        spreads = v[10] + v[13] + v[16] + v[19]
        long_ok = abs((v[8] + v[11] + v[14] + v[17] + spreads) - v[20]) <= tol
        short_ok = abs((v[9] + v[12] + v[15] + v[18] + spreads) - v[21]) <= tol
        oi_ok = abs((v[20] + v[22]) - v[7]) <= tol
        return long_ok and short_ok and oi_ok
    except Exception:
        return False

# ---------------------------------------------------------------- options skew (helper for src_derivatives)
def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _parse_option(name):
    """BTC-26SEP26-100000-C -> (expiry datetime UTC 08:00, strike, 'C'|'P'); None if it is not a standard option."""
    parts = name.split('-')
    if len(parts) != 4 or parts[3] not in ('C', 'P'): return None
    try:
        exp = dt.datetime.strptime(parts[1], '%d%b%y').replace(hour=8, tzinfo=dt.timezone.utc)
        return exp, float(parts[2]), parts[3]
    except Exception: return None

def options_skew(chain, target_days=30, min_days=7):
    """25-delta risk reversal (put IV - call IV) and butterfly, in volatility points, on the listed expiry nearest
    target_days. Deltas are Black-Scholes with r=0 on the underlying the exchange quotes with each instrument.
    Returns None when the chain is too thin to interpolate both wings."""
    now = dt.datetime.now(dt.timezone.utc); byexp = {}
    for o in chain:
        p = _parse_option(o.get('instrument_name', '') or '')
        iv = o.get('mark_iv'); und = o.get('underlying_price')
        if not p or not iv or not und or iv <= 0 or und <= 0: continue
        exp, strike, cp = p
        days = (exp - now).total_seconds() / 86400.0
        if days < min_days: continue
        byexp.setdefault(exp, []).append((strike, cp, float(iv) / 100.0, float(und), days))
    if not byexp: return None
    exp = min(byexp, key=lambda e: abs((e - now).total_seconds() / 86400.0 - target_days))
    rows = byexp[exp]; days = rows[0][4]; T = days / 365.0
    calls, puts = [], []
    for strike, cp, sig, und, _ in rows:
        if sig <= 0 or T <= 0 or strike <= 0: continue
        d1 = (math.log(und / strike) + 0.5 * sig * sig * T) / (sig * math.sqrt(T))
        delta = _norm_cdf(d1) if cp == 'C' else _norm_cdf(d1) - 1.0
        (calls if cp == 'C' else puts).append((delta, sig * 100.0))
    def interp(points, target):
        """IV at a target delta by linear interpolation on delta; points are (delta, iv_pts)."""
        pts = sorted(points)
        if len(pts) < 2: return None
        lo, hi = pts[0][0], pts[-1][0]
        if not (lo <= target <= hi): return None
        for (d0, v0), (d1_, v1) in zip(pts, pts[1:]):
            if d0 <= target <= d1_:
                if d1_ == d0: return v0
                w = (target - d0) / (d1_ - d0)
                return v0 + w * (v1 - v0)
        return None
    c25 = interp(calls, 0.25); p25 = interp(puts, -0.25)
    atm_c = interp(calls, 0.50); atm_p = interp(puts, -0.50)
    atm = None
    if atm_c is not None and atm_p is not None: atm = (atm_c + atm_p) / 2.0
    elif atm_c is not None: atm = atm_c
    elif atm_p is not None: atm = atm_p
    if c25 is None or p25 is None or atm is None: return None
    return {'expiry': exp.date().isoformat(), 'days_to_expiry': round(days, 1), 'atm_iv': round(atm, 2),
            'skew_25d': round(p25 - c25, 2), 'fly_25d': round((c25 + p25) / 2.0 - atm, 2)}

def options_term_structure(chain, min_days=2, tenors=(7, 30, 90, 180)):
    """At-the-money implied volatility on every listed expiry, and the values at fixed tenors by linear interpolation
    in days to expiry (only where a tenor is bracketed by listed expiries; nothing is extrapolated). ATM is the mean of
    the 50-delta call and put IV located by delta interpolation, the same construction the skew uses. Returns None if
    fewer than two expiries yield an ATM level."""
    now = dt.datetime.now(dt.timezone.utc); byexp = {}
    for o in chain:
        p = _parse_option(o.get('instrument_name', '') or '')
        iv = o.get('mark_iv'); und = o.get('underlying_price')
        if not p or not iv or not und or iv <= 0 or und <= 0: continue
        exp, strike, cp = p; days = (exp - now).total_seconds() / 86400.0
        if days < min_days: continue
        byexp.setdefault(exp, []).append((strike, cp, float(iv) / 100.0, float(und), days))
    curve = []
    for exp, rows in sorted(byexp.items()):
        days = rows[0][4]; T = days / 365.0; calls, puts = [], []
        for strike, cp, sig, und, _ in rows:
            if sig <= 0 or T <= 0 or strike <= 0: continue
            d1 = (math.log(und / strike) + 0.5 * sig * sig * T) / (sig * math.sqrt(T))
            delta = _norm_cdf(d1) if cp == 'C' else _norm_cdf(d1) - 1.0
            (calls if cp == 'C' else puts).append((delta, sig * 100.0))
        def interp(points, target):
            pts = sorted(points)
            if len(pts) < 2 or not (pts[0][0] <= target <= pts[-1][0]): return None
            for (d0, v0), (d1_, v1) in zip(pts, pts[1:]):
                if d0 <= target <= d1_: return v0 if d1_ == d0 else v0 + (target - d0) / (d1_ - d0) * (v1 - v0)
            return None
        ac, ap = interp(calls, 0.50), interp(puts, -0.50)
        atm = (ac + ap) / 2.0 if (ac is not None and ap is not None) else (ac if ac is not None else ap)
        if atm is not None: curve.append({'expiry': exp.date().isoformat(), 'days': round(days, 1), 'atm_iv': round(atm, 2), 'n_strikes': len(rows)})
    if len(curve) < 2: return None
    xs = [c['days'] for c in curve]; ys = [c['atm_iv'] for c in curve]; at = {}
    for t in tenors:
        if xs[0] <= t <= xs[-1]:
            for x0, y0, x1, y1 in zip(xs, ys, xs[1:], ys[1:]):
                if x0 <= t <= x1: at[t] = round(y0 if x1 == x0 else y0 + (t - x0) / (x1 - x0) * (y1 - y0), 2); break
    slope = round(at[180] - at[30], 2) if (30 in at and 180 in at) else None
    return {'curve': curve, 'at_tenor': at, 'slope_30_180_pts': slope,
            'note': 'ATM IV per listed expiry (50-delta by interpolation, r=0); tenor values interpolated in days, never extrapolated. '
                    'Slope is 180-day minus 30-day ATM IV in vol points: positive is the normal contango of a calm surface, negative (inverted) marks stress.'}

def src_derivatives():
    name = 'derivatives'; series = {}; errs = []
    try:  # Deribit DVOL (BTC implied volatility index), last 3 years daily
        end = int(time.time() * 1000); start = end - 3 * 365 * 86400 * 1000
        j = get('https://www.deribit.com/api/v2/public/get_volatility_index_data', params={'currency': 'BTC', 'resolution': '1D', 'start_timestamp': start, 'end_timestamp': end}).json()
        series['deribit_dvol_close'] = [[day(p[0] / 1000), p[4]] for p in j['result']['data']]
    except Exception as e: errs.append(f'dvol: {e}')
    try:  # Deribit perpetual funding history (8h), aggregated to daily mean; the endpoint returns about a month per call, so page monthly over a year
        byd = {}; end = int(time.time() * 1000)
        for k in range(12):
            e_ = end - k * 30 * 86400 * 1000; s_ = e_ - 30 * 86400 * 1000
            try:
                j = get('https://www.deribit.com/api/v2/public/get_funding_rate_history', params={'instrument_name': 'BTC-PERPETUAL', 'start_timestamp': s_, 'end_timestamp': e_}).json()
                for p in j['result']: byd.setdefault(day(p['timestamp'] / 1000), []).append(p['interest_8h'])
            except Exception: break
        if not byd: raise RuntimeError('no funding rows')
        series['deribit_funding_8h_daily_mean'] = [[d, sum(v) / len(v)] for d, v in sorted(byd.items())]
    except Exception as e: errs.append(f'funding: {e}')
    try:  # OKX open interest history for BTC swaps, daily
        j = get('https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume', params={'ccy': 'BTC', 'period': '1D'}).json()
        series['okx_open_interest_usd'] = sorted([[day(int(p[0]) / 1000), float(p[1])] for p in j.get('data', [])])
    except Exception as e: errs.append(f'okx_oi: {e}')
    try:  # Deribit dated-futures basis, annualised, instrument nearest 90 days to expiry (UNTESTED stub: verify on first run)
        idx = float(get('https://www.deribit.com/api/v2/public/get_index_price', params={'index_name': 'btc_usd'}).json()['result']['index_price'])
        js = get('https://www.deribit.com/api/v2/public/get_book_summary_by_currency', params={'currency': 'BTC', 'kind': 'future'}).json()['result']
        best = None
        for f in js:
            nm = f.get('instrument_name', '')
            if 'PERPETUAL' in nm or not f.get('mark_price'): continue
            try: exp = dt.datetime.strptime(nm.split('-')[1], '%d%b%y').replace(tzinfo=dt.timezone.utc)
            except Exception: continue
            days = (exp - dt.datetime.now(dt.timezone.utc)).days
            if days < 30: continue
            if best is None or abs(days - 90) < abs(best[0] - 90): best = (days, float(f['mark_price']))
        if best and idx > 0:
            basis = ((best[1] / idx) - 1) * 365 / best[0] * 100
            series['deribit_basis_90d_ann_pct'] = [[NOW[:10], round(basis, 3)]]
    except Exception as e: errs.append(f'basis: {e}')
    jo = None
    try:  # Deribit options put/call open-interest ratio (OI is in BTC, ratio is unitless)
        jo = get('https://www.deribit.com/api/v2/public/get_book_summary_by_currency', params={'currency': 'BTC', 'kind': 'option'}).json()['result']
        oc = sum(o.get('open_interest', 0) or 0 for o in jo if o.get('instrument_name', '').endswith('-C'))
        op = sum(o.get('open_interest', 0) or 0 for o in jo if o.get('instrument_name', '').endswith('-P'))
        if oc > 0: series['deribit_putcall_oi_ratio'] = [[NOW[:10], round(op / oc, 4)]]
    except Exception as e: errs.append(f'putcall: {e}')
    try:  # 25-delta risk reversal and butterfly on the listed expiry nearest 30 days, from the same option chain (no extra call).
        # Deribit publishes mark_iv per instrument but no delta, so delta is computed Black-Scholes on the forward with r=0,
        # which is the market convention for crypto options and is exact enough to locate the 25-delta wings by interpolation.
        if jo:
            sk = options_skew(jo)
            if sk:
                series['deribit_atm_iv_30d'] = [[NOW[:10], sk['atm_iv']]]
                series['deribit_skew_25d_pts'] = [[NOW[:10], sk['skew_25d']]]
                series['deribit_butterfly_25d_pts'] = [[NOW[:10], sk['fly_25d']]]
    except Exception as e: errs.append(f'skew: {e}')
    try:  # term structure from the same chain: ATM IV at 7/30/90/180 days and the 30-180 slope; the full curve goes to the manifest
        if jo:
            ts = options_term_structure(jo)
            if ts:
                for t, v in ts['at_tenor'].items():
                    if t != 30: series[f'deribit_atm_iv_{t}d'] = [[NOW[:10], v]]
                if ts['slope_30_180_pts'] is not None: series['deribit_term_slope_30_180_pts'] = [[NOW[:10], ts['slope_30_180_pts']]]
                manifest.setdefault('_derivatives_extra', {})['term_structure'] = {'as_of': NOW, 'curve': ts['curve'], 'at_tenor': ts['at_tenor'], 'note': ts['note']}
    except Exception as e: errs.append(f'term: {e}')
    try:  # CME bitcoin futures, CFTC Traders in Financial Futures (weekly, public comma-delimited file).
        # Column map (verified against the file's own identities, see cot_columns_ok): 7 open interest,
        # 8/9/10 dealer L/S/spread, 11/12/13 asset manager, 14/15/16 leveraged funds, 17/18/19 other,
        # 20/21 total reportable L/S, 22/23 non-reportable L/S. Leveraged funds are hedge funds and CTAs.
        txt = get('https://www.cftc.gov/dea/newcot/FinFutWk.txt').text; rd = csv.reader(io.StringIO(txt))
        oi, lev_l, lev_s, lev_n, checked, bad = [], [], [], [], 0, 0
        for row in rd:
            if len(row) > 23 and 'BITCOIN' in row[0].upper() and 'MICRO' not in row[0].upper() and 'CHICAGO MERCANTILE' in row[0].upper():
                try:
                    d = dt.datetime.strptime(row[2].strip(), '%Y-%m-%d').strftime('%Y-%m-%d')
                    # fields 0-6 are text (contract name, report date, code, exchange); only 7 onward are numeric
                    v = [0.0] * 7 + [float(row[i].strip() or 0) for i in range(7, 24)]
                except Exception: continue
                checked += 1
                if not cot_columns_ok(v): bad += 1; continue      # layout changed: skip rather than publish a wrong column
                oi.append([d, v[7]]); lev_l.append([d, v[14]]); lev_s.append([d, v[15]]); lev_n.append([d, v[14] - v[15]])
        if checked and bad == checked: raise RuntimeError('CFTC column layout no longer reconciles; leaving the series unchanged')
        if oi:
            series['cme_btc_open_interest_contracts'] = sorted(oi)
            series['cme_btc_leveraged_long'] = sorted(lev_l); series['cme_btc_leveraged_short'] = sorted(lev_s)
            series['cme_btc_leveraged_net'] = sorted(lev_n)
    except Exception as e: errs.append(f'cftc: {e}')
    if not series: raise RuntimeError('; '.join(errs))
    save(name, 'Deribit, OKX, CFTC public endpoints', series, note=('partial: ' + '; '.join(errs)) if errs else '')
    extra = manifest.pop('_derivatives_extra', None)
    if extra: manifest[name].update(extra)

# ---------------------------------------------------------------- ETF flows (phase 2)
def src_etf():
    """Issuer holdings -> net flows. Each issuer parser is isolated; see fetch/etf.py."""
    import etf
    price = {}
    try:
        bc = load_existing('blockchain'); 
        if bc: price = {d: v for d, v in bc['series'].get('price', [])}
    except Exception: pass
    res = etf.run(OUT, price)
    manifest['etf_flows'] = {**res, 'fetched_at': NOW, 'source_url': 'issuer disclosures', **freshness('etf_flows', res.get('last_date'))}
    print(f"  {'ok ' if res['status']=='ok' else 'prt' if res['status']=='partial' else 'ERR'} etf_flows: parsed {res['issuers_ok']}, failed {res['issuers_error']}, pending {res['pending']}")

# ---------------------------------------------------------------- Macro: the series people correlate with bitcoin cycles
def src_macro():
    """Fed policy, money, liquidity and risk-appetite series from FRED (keyless CSV). Each leg isolated. Net liquidity
    (Fed balance sheet less the Treasury account less reverse repo) is assembled in the page from the three legs."""
    name = 'macro'
    legs = [('fed_funds_daily', 'DFF', 'effective federal funds rate, % (daily)'),
            ('treasury_2y', 'DGS2', '2-year Treasury yield, % (daily)'),
            ('m2_monthly', 'M2SL', 'US M2 money stock, $bn, monthly, seasonally adjusted'),
            ('fed_assets_weekly', 'WALCL', 'Fed total assets, $mn, weekly (Wednesday)'),
            ('tga_weekly', 'WTREGEN', 'Treasury General Account, $bn, weekly (Wednesday)'),
            ('rrp_daily', 'RRPONTSYD', 'overnight reverse repo, $bn, daily'),
            ('hy_spread_daily', 'BAMLH0A0HYM2', 'ICE BofA US high-yield option-adjusted spread, % (daily)'),
            ('vix_daily', 'VIXCLS', 'CBOE VIX close (daily)'),
            ('breakeven_10y_daily', 'T10YIE', '10-year breakeven inflation, % (daily)')]
    series = {}; prov = {}; errs = []
    for key, sid, desc in legs:
        try:
            s = _fred_daily(sid, since='2010-01-01')
            if len(s) < 50: raise RuntimeError(f'{sid}: only {len(s)} rows')
            series[key] = s; prov[key] = f'FRED {sid}: {desc}'
        except Exception as e:
            errs.append(f'{key}: {e}')
    if not series: raise RuntimeError('every macro leg failed: ' + '; '.join(errs))
    note = ('Series that are widely correlated with bitcoin cycles, published raw so the page can test the claim rather than draw the overlay. '
            'Not carried and why: ISM manufacturing PMI is proprietary (ISM / S&P Global) with no free primary feed since FRED dropped it in 2016; '
            "'global M2' is an author-specific blend of central-bank aggregates in dollars with no single public definition, so US M2 is published and the blend is not. "
            'Provenance: ' + '; '.join(f'{k} = {v}' for k, v in prov.items()))
    save(name, 'https://fred.stlouisfed.org/', series, note=note)
    manifest[name]['status'] = 'ok' if not errs else 'partial'; manifest[name]['legs_ok'] = sorted(series); manifest[name]['legs_failed'] = errs
    print(f"  {'ok ' if not errs else 'prt'} macro: {len(series)} of {len(legs)} legs" + (f", failed {errs}" if errs else ''))

# ---------------------------------------------------------------- LPPLS bubble indicator, point-in-time
def src_lppls():
    """Log-periodic power law singularity confidence, computed as of each day on the stored price series.
    The deep history ships with the repository (computed once); each run extends it by the days not yet stored."""
    import lppls, datetime as _dt
    bc = load_existing('blockchain')
    if not bc: raise RuntimeError('blockchain price series not available')
    m = {d: float(v) for d, v in bc['series'].get('price', []) if float(v) > 0}
    ds = sorted(m); dates = [_dt.date.fromisoformat(d) for d in ds]; prices = [m[d] for d in ds]
    existing = load_existing('lppls')
    prior = existing.get('random_baseline') if existing else None
    doc = lppls.build_history(dates, prices, os.path.join(OUT, 'lppls.json'), existing=existing, log=lambda s: print(s), baseline=prior)
    # the baseline is recomputed weekly (Mondays) or when absent; it is deterministic (seeded) and takes ~30s
    if prior is None or dt.datetime.now(dt.timezone.utc).weekday() == 0:
        base = lppls.random_baseline(dates, prices, doc['series']['lppls_pos'])
        doc['random_baseline'] = base
        with open(os.path.join(OUT, 'lppls.json'), 'w') as fh: json.dump(doc, fh, separators=(',', ':'))
    n_pos = len(doc['series']['lppls_pos'])
    manifest['lppls'] = {'status': 'ok', 'fetched_at': NOW, 'source_url': 'computed from the blockchain.com daily price series',
                         'last_date': doc['series']['lppls_pos'][-1][0] if n_pos else None, 'points': n_pos,
                         'today': doc['today'], 'note': 'positive-bubble confidence = share of 8 window lengths whose best LPPLS fit passes the stated filters',
                         **freshness('lppls', doc['series']['lppls_pos'][-1][0] if n_pos else None)}
    print(f"  ok  lppls: {n_pos} points, today pos {doc['today']['pos'] if doc['today'] else None}")

# ---------------------------------------------------------------- Relative value: what one bitcoin buys of other assets
def _coinbase_daily(product, start='2016-01-01'):
    """Daily closes from Coinbase Exchange candles, paginated in 300-day windows. Keyless, primary exchange data."""
    base = f'https://api.exchange.coinbase.com/products/{product}/candles'
    end = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    cur = dt.datetime.fromisoformat(start).replace(tzinfo=dt.timezone.utc); pts = {}
    while cur < end:
        nxt = min(cur + dt.timedelta(days=300), end)
        j = get(base, params={'granularity': 86400, 'start': cur.isoformat(), 'end': nxt.isoformat()}).json()
        for row in j:                       # [time, low, high, open, close, volume]
            pts[day(row[0])] = float(row[4])
        cur = nxt; time.sleep(0.35)         # stay well inside the public rate limit
    return sorted([[d, v] for d, v in pts.items()])

def _fred_daily(sid, since='2009-01-01'):
    txt = get('https://fred.stlouisfed.org/graph/fredgraph.csv?id=' + sid).text
    rd = csv.DictReader(io.StringIO(txt)); col = [c for c in rd.fieldnames if c not in ('observation_date', 'DATE')][0]
    out = []
    for row in rd:
        d = row.get('observation_date') or row.get('DATE'); v = row.get(col)
        if v not in (None, '', '.') and d >= since: out.append([d, float(v)])
    return out

def _worldbank_pinksheet():
    """World Bank Commodity Markets 'Pink Sheet', monthly, public domain. Returns {'gold':[[YYYY-MM-01,usd]], 'silver':[...]}."""
    # The document URL changes with every release, so the link is resolved from the commodity-markets
    # page each run, exactly as the World Bank's own mirrors do; a fixed URL served a file that was
    # twenty months stale on first deployment.
    import re as _re, openpyxl
    page = get('https://www.worldbank.org/en/research/commodity-markets').text
    m = _re.search(r'href="([^"]+)"[^>]*>[^<]*[Mm]onthly [Pp]rices', page) or _re.search(r'href="([^"]*CMO-Historical-Data-Monthly[^"]*)"', page)
    if not m: raise RuntimeError('monthly prices link not found on the World Bank page')
    url = m.group(1); url = url if url.startswith('http') else 'https://www.worldbank.org' + url
    wb = openpyxl.load_workbook(io.BytesIO(get(url).content), read_only=True, data_only=True)
    ws = wb['Monthly Prices']; rows = list(ws.iter_rows(values_only=True))
    hdr_i = next(i for i, r in enumerate(rows) if r and any(isinstance(c, str) and 'Gold' in c for c in r))
    hdr = [str(c or '').strip() for c in rows[hdr_i]]
    gi = next(i for i, c in enumerate(hdr) if c.startswith('Gold')); si = next(i for i, c in enumerate(hdr) if c.startswith('Silver'))
    gold, silver = [], []
    for r in rows[hdr_i + 1:]:
        if not r or not r[0]: continue
        lab = str(r[0]).strip()                                  # e.g. 2010M01
        if len(lab) == 7 and lab[4] == 'M':
            d = f'{lab[:4]}-{lab[5:]}-01'
            if d < '2009-01-01': continue
            try:
                if r[gi] is not None: gold.append([d, float(r[gi])])
                if r[si] is not None: silver.append([d, float(r[si])])
            except (TypeError, ValueError): pass
    return {'gold': sorted(gold), 'silver': sorted(silver)}

def _datahub_sp500_monthly():
    """Monthly average S&P 500 level from Robert Shiller's long series, mirrored on GitHub by datahub.io.
    FRED's free daily series carries only ten years; this gives the ratio its full history from 2010."""
    txt = get('https://raw.githubusercontent.com/datasets/s-and-p-500/main/data/data.csv').text
    out = []
    for row in csv.DictReader(io.StringIO(txt)):
        d = row.get('Date', ''); v = row.get('SP500')
        if d >= '2009-01-01' and v not in (None, '', '0', '0.0'):
            try: out.append([d[:7] + '-01', float(v)])
            except ValueError: pass
    return sorted(out)

def _datahub_gold_monthly():
    """Fallback for gold only: datahub's mirror of the same World Bank series, hosted on GitHub."""
    txt = get('https://raw.githubusercontent.com/datasets/gold-prices/main/data/monthly.csv').text
    out = []
    for row in csv.DictReader(io.StringIO(txt)):
        if row['Date'] >= '2009-01': out.append([row['Date'] + '-01', float(row['Price'])])
    return sorted(out)

def src_relative():
    """Every leg isolated: a failure in one denominator must not remove the others."""
    name = 'relative'; series = {}; errs = []; prov = {}
    # btc_usd is a true daily CLOSE. The blockchain.com price used elsewhere is a daily average, which is
    # right for valuation but wrong for return correlations: averaging smears each day's move across
    # neighbours and biases correlation toward zero. Correlations must be close against close.
    for key, product, start in [('btc_usd', 'BTC-USD', '2015-01-01'), ('eth_usd', 'ETH-USD', '2016-05-18'), ('sol_usd', 'SOL-USD', '2021-06-01'), ('paxg_usd', 'PAXG-USD', '2020-02-01')]:
        try: series[key] = _coinbase_daily(product, start); prov[key] = 'Coinbase Exchange daily candles'
        except Exception as e: errs.append(f'{key}: {e}')
    try: series['sp500_monthly'] = _datahub_sp500_monthly(); prov['sp500_monthly'] = "Robert Shiller's monthly S&P 500 series via datahub.io mirror on GitHub"
    except Exception as e: errs.append(f'sp500_monthly: {e}')
    for key, sid in [('sp500_daily', 'SP500'), ('nasdaq_daily', 'NASDAQCOM')]:
        try: series[key] = _fred_daily(sid); prov[key] = f'FRED {sid}'
        except Exception as e: errs.append(f'{key}: {e}')
    wbk = None
    try:
        wbk = _worldbank_pinksheet()
        series['gold_usd_monthly'] = wbk['gold']; series['silver_usd_monthly'] = wbk['silver']
        prov['gold_usd_monthly'] = prov['silver_usd_monthly'] = 'World Bank Commodity Markets (Pink Sheet), monthly average, public domain'
    except Exception as e:
        errs.append(f'worldbank: {e}')
    try:
        mirror = _datahub_gold_monthly()
        # the mirror tracks the same series; if it is fresher than what we parsed, the World Bank link resolved to an old file
        if not wbk or (mirror and wbk['gold'] and mirror[-1][0] > wbk['gold'][-1][0]):
            series['gold_usd_monthly'] = mirror; prov['gold_usd_monthly'] = 'World Bank Pink Sheet via datahub.io mirror on GitHub (fresher than the file resolved from worldbank.org)'
            if wbk: errs.append(f"worldbank file stale: gold ends {wbk['gold'][-1][0]}, mirror ends {mirror[-1][0]}; silver kept from the World Bank file and may lag")
    except Exception as e2:
        errs.append(f'gold mirror: {e2}')
    if not series: raise RuntimeError('; '.join(errs))
    save(name, 'Coinbase Exchange, FRED, World Bank', series,
         note=('Denominators for bitcoin priced in other assets. ' + ('; '.join(errs) if errs else 'all legs ok')))
    manifest[name]['provenance'] = prov
    if errs: manifest[name]['note'] = '; '.join(errs)

# ---------------------------------------------------------------- ETF quarterly holdings from 10-Q/10-K XBRL (reconciliation layer)
def src_etf_quarterly():
    import edgar
    hold = {}
    try:
        h = json.load(open(os.path.join(OUT, 'etf_holdings.json')))
        hold = {tk: v for tk, v in h.items() if isinstance(v, list)}  # etf_holdings.json is {ticker: [[date, btc], ...]}
    except Exception: pass
    res = edgar.run(OUT, hold)
    manifest['etf_quarterly'] = {**res, 'fetched_at': NOW, 'source_url': 'https://data.sec.gov/api/xbrl/companyfacts/', **freshness('etf_quarterly', res.get('last_date'))}
    if res['status'] == 'error': raise RuntimeError('; '.join(res['errors']) or 'no trust parsed')
    print(f"  {'ok ' if res['status']=='ok' else 'prt'} etf_quarterly: {len(res['tickers_ok'])} trusts, latest quarter end {res.get('last_date')}")

SOURCES = [('blockchain', src_blockchain), ('coinmetrics', src_coinmetrics), ('coinbase', src_coinbase), ('offshore_spot', src_offshore_spot), ('mempool', src_mempool),
           ('stablecoins', src_stablecoins), ('fred', src_fred), ('fear_greed', src_fng), ('coingecko_global', src_coingecko_global), ('derivatives', src_derivatives), ('relative', src_relative), ('etf_flows', src_etf), ('etf_quarterly', src_etf_quarterly), ('macro', src_macro), ('lppls', src_lppls)]

def main():
    os.makedirs(OUT, exist_ok=True); print('Snapshot at', NOW)
    for name, fn in SOURCES:
        try: fn()
        except Exception as e: fail(name, e)
    ok = sorted(k for k, v in manifest.items() if v['status'] in ('ok', 'partial'))
    errs = sorted(k for k, v in manifest.items() if v['status'] == 'error')
    stale = sorted(k for k, v in manifest.items() if v['status'] == 'stale' or (v['status'] != 'error' and v.get('freshness') == 'stale'))
    total = len(manifest)
    if errs:
        health = f"{len(errs)} of {total} sources unavailable: " + ', '.join(errs) + ('; ' + ', '.join(stale) + ' serving an older value' if stale else '')
    elif stale:
        health = f"{total - len(stale)} of {total} sources refreshed; " + ', '.join(f"{k} is {manifest[k].get('age_days', '?')} day(s) old" for k in stale)
    else:
        health = f'all {total} sources refreshed'
    manifest_doc = {'schema_version': SCHEMA_VERSION, 'generated_at': NOW, 'sources': manifest, 'ok': ok, 'errors': errs, 'stale': stale,
                    'health': health,
                    'health_state': 'error' if errs else ('stale' if stale else 'ok'),
                    'health_note': 'errors are sources with no usable value; stale are sources that did not refresh but still serve a value inside their expected age'}
    try:
        import kpis; kpis.OUT = OUT; kpis.main(); manifest_doc['kpis'] = 'ok'
    except Exception as e:
        manifest_doc['kpis'] = 'error: ' + str(e)[:300]; print('  ERR kpis:', str(e)[:200], file=sys.stderr)
    with open(os.path.join(OUT, 'manifest.json'), 'w') as f: json.dump(manifest_doc, f, indent=1)
    print('Done. ok:', manifest_doc['ok'], 'errors:', manifest_doc['errors'])
    return 0

if __name__ == '__main__': sys.exit(main())
