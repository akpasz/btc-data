#!/usr/bin/env python3
"""
Crypto Exponentials data snapshot.
Pulls free, keyless public sources once a day, merges with the existing history in data/,
and writes one JSON file per source plus data/manifest.json describing what succeeded.

Every source is isolated: a failure records an error in the manifest and leaves the previous
file untouched. Nothing here needs an API key or a paid plan.
"""
import json, os, sys, time, datetime as dt, io, csv
from typing import Dict, List, Tuple
import requests

OUT = os.path.join(os.path.dirname(__file__), '..', 'data')
UA = {'User-Agent': 'CryptoExponentials-DataSnapshot/1.0 (+https://cryptoexponentials.com/tools/)'}
NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
S = requests.Session(); S.headers.update(UA)
manifest: Dict[str, dict] = {}

def get(url, **kw):
    r = S.get(url, timeout=60, **kw); r.raise_for_status(); return r

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
        for d, v in new.get(k, []): m[d] = v
        out[k] = sorted([[d, v] for d, v in m.items() if v is not None])
    return out

def save(name, source_url, series: Dict[str, List], note=''):
    old = load_existing(name)
    merged = merge_series(old['series'] if old else {}, series)
    doc = {'source': name, 'source_url': source_url, 'fetched_at': NOW, 'note': note, 'series': merged}
    with open(os.path.join(OUT, name + '.json'), 'w') as f: json.dump(doc, f, separators=(',', ':'))
    last = max((s[-1][0] for s in merged.values() if s), default=None)
    manifest[name] = {'status': 'ok', 'fetched_at': NOW, 'metrics': {k: len(v) for k, v in merged.items()}, 'last_date': last, 'source_url': source_url}
    print(f'  ok  {name}: {sum(len(v) for v in merged.values())} points, last {last}')

def fail(name, e, source_url=''):
    old = load_existing(name)
    manifest[name] = {'status': 'error', 'error': str(e)[:300], 'fetched_at': NOW, 'source_url': source_url,
                      'kept_previous': bool(old), 'previous_fetched_at': old.get('fetched_at') if old else None}
    print(f'  ERR {name}: {str(e)[:200]}', file=sys.stderr)

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
    ids = {'dollar_index_broad': 'DTWEXBGS', 'fed_funds_effective': 'DFF', 'treasury_10y': 'DGS10', 'real_yield_10y': 'DFII10', 'm2': 'M2SL'}
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

# ---------------------------------------------------------------- Derivatives (Deribit and OKX public)
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
    try:  # CME bitcoin futures open interest via CFTC commitments of traders (weekly, public CSV)
        txt = get('https://www.cftc.gov/dea/newcot/FinFutWk.txt').text; rd = csv.reader(io.StringIO(txt)); pts = []
        for row in rd:
            if len(row) > 8 and 'BITCOIN' in row[0].upper() and 'CHICAGO MERCANTILE' in row[0].upper():
                try: pts.append([dt.datetime.strptime(row[2].strip(), '%Y-%m-%d').strftime('%Y-%m-%d'), float(row[7])])
                except Exception: pass
        if pts: series['cme_btc_open_interest_contracts'] = sorted(pts)
    except Exception as e: errs.append(f'cftc: {e}')
    if not series: raise RuntimeError('; '.join(errs))
    save(name, 'Deribit, OKX, CFTC public endpoints', series, note=('partial: ' + '; '.join(errs)) if errs else '')

# ---------------------------------------------------------------- ETF flows (phase 2)
def src_etf():
    """Placeholder. Issuer daily holdings files differ in format and change URLs; implement per issuer after the
    first run confirms the rest of the pipeline. Until then the manifest records this source as not implemented."""
    manifest['etf_flows'] = {'status': 'not_implemented', 'fetched_at': NOW,
                             'plan': 'derive net flows from issuer daily holdings (IBIT, FBTC, GBTC, BTC, ARKB, BITB, HODL, BTCO, EZBC, BRRR, BTCW)'}
    print('  --  etf_flows: not implemented yet')

SOURCES = [('blockchain', src_blockchain), ('coinmetrics', src_coinmetrics), ('coinbase', src_coinbase), ('mempool', src_mempool),
           ('stablecoins', src_stablecoins), ('fred', src_fred), ('fear_greed', src_fng), ('derivatives', src_derivatives), ('etf_flows', src_etf)]

def main():
    os.makedirs(OUT, exist_ok=True); print('Snapshot at', NOW)
    for name, fn in SOURCES:
        try: fn()
        except Exception as e: fail(name, e)
    manifest_doc = {'generated_at': NOW, 'sources': manifest,
                    'ok': sorted(k for k, v in manifest.items() if v['status'] == 'ok'),
                    'errors': sorted(k for k, v in manifest.items() if v['status'] == 'error')}
    with open(os.path.join(OUT, 'manifest.json'), 'w') as f: json.dump(manifest_doc, f, indent=1)
    print('Done. ok:', manifest_doc['ok'], 'errors:', manifest_doc['errors'])
    return 0

if __name__ == '__main__': sys.exit(main())
