"""
Spot Bitcoin ETF flows from issuer disclosures, no key, no paid source.
Each issuer publishes its daily holdings. Net flow for a day = change in BTC held x that day's price.
Issuer file formats change without notice, so each parser is isolated and reports its own status; the manifest
shows which issuers parsed. Holdings are merged into data/etf_holdings.json (issuer -> [[date, btc]]) so the
history accumulates from the first successful run; flows are derived from that history.
"""
import re, io, csv, json, os, time, datetime as dt
import requests

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
      'Accept': 'text/html,application/xhtml+xml,text/csv,application/json;q=0.9,*/*;q=0.8', 'Accept-Language': 'en-US,en;q=0.9'}
TODAY = dt.date.today().isoformat()

def _get(url, tries=3, **kw):
    last = None
    for k in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=60, **kw)
            if r.status_code == 429 or r.status_code >= 500: raise requests.HTTPError(f'{r.status_code} for {url}', response=r)
            r.raise_for_status(); return r
        except Exception as e:
            last = e; time.sleep(8 * (k + 1))
    raise last

def _first_ok(urls):
    last = None
    for u in urls:
        try: return _get(u)
        except Exception as e: last = e
    raise last

def _num(s):
    s = re.sub(r'[^0-9.\-]', '', str(s))
    try: return float(s) if s not in ('', '-', '.') else None
    except ValueError: return None

def _csv_text(r):
    """Return the body as text, or raise if the endpoint answered with an HTML page instead of a data file."""
    t = r.text
    if '<html' in t[:2000].lower() or '<!doctype' in t[:200].lower(): raise RuntimeError('endpoint returned an HTML page, not a data file; source needs a new address')
    return t

# --- parsers: each returns (date_iso, btc_held) for the latest disclosure ------------------------------------------
def ibit():
    # iShares publishes a holdings CSV; the BTC row carries the quantity. Endpoint pattern used by their product pages.
    url = 'https://www.ishares.com/us/products/333011/ishares-bitcoin-trust-etf/1467271812596.ajax?fileType=csv&fileName=IBIT_holdings&dataType=fund'
    txt = _csv_text(_get(url)); date = None; btc = None
    m = re.search(r'Fund Holdings as of,"?([A-Za-z]{3} \d{1,2}, \d{4})', txt)
    if m: date = dt.datetime.strptime(m.group(1), '%b %d, %Y').date().isoformat()
    for row in csv.reader(io.StringIO(txt)):
        head = ' '.join(row[:2]).upper()   # ticker and name cells only; the asset-class cell says "Cash and/or Derivatives" for the coin itself
        if row and (row[0].strip().upper() == 'BTC' or 'BITCOIN' in head) and 'USD CASH' not in head and 'TREASURY' not in head:
            # the coin quantity is the largest number in the row that is a plausible BTC count (dollar notional is far larger)
            cands = [v for v in (_num(c) for c in row) if v and 1e4 <= v <= 5e6]
            if cands: btc = max(cands); break
    return date or TODAY, btc

def grayscale(ticker):
    # Grayscale product pages expose "Bitcoin per Share" and "Shares Outstanding"; holdings = product of the two
    url = {'GBTC': 'https://etfs.grayscale.com/gbtc', 'BTC': 'https://etfs.grayscale.com/btc'}[ticker]
    time.sleep(6); txt = _get(url).text
    per = re.search(r'Bitcoin per Share[^0-9]*([0-9.]+)', txt, re.I); sh = re.search(r'Shares Outstanding[^0-9]*([0-9,]+)', txt, re.I)
    if not (per and sh): raise RuntimeError('fields not found on page')
    date = re.search(r'as of\s*([0-9/]+)', txt, re.I); d = dt.datetime.strptime(date.group(1), '%m/%d/%Y').date().isoformat() if date else TODAY
    return d, float(per.group(1)) * float(sh.group(1).replace(',', ''))

def arkb():
    txt = _csv_text(_first_ok(['https://assets.ark-funds.com/fund-documents/funds-etf-csv/ARK_21SHARES_BITCOIN_ETF_ARKB_HOLDINGS.csv',
                     'https://ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_21SHARES_BITCOIN_ETF_ARKB_HOLDINGS.csv'])); date = None; btc = None
    for row in csv.reader(io.StringIO(txt)):
        if not row: continue
        if not date and re.match(r'\d{1,2}/\d{1,2}/\d{4}', row[0].strip()): date = dt.datetime.strptime(row[0].strip(), '%m/%d/%Y').date().isoformat()
        if any('BITCOIN' in c.upper() for c in row) and not any('CASH' in c.upper() for c in row):
            cands = [v for v in (_num(c) for c in row) if v and 1e3 <= v <= 5e6]
            if cands: btc = max(cands)
    return date or TODAY, btc

def bitwise():
    txt = _get('https://bitbetf.com/').text
    m = re.search(r'Bitcoin in Trust[^0-9]*([0-9,]+\.?[0-9]*)', txt, re.I) or re.search(r'BTC in Trust[^0-9]*([0-9,]+\.?[0-9]*)', txt, re.I)
    if not m: raise RuntimeError('holdings not found')
    return TODAY, float(m.group(1).replace(',', ''))

ISSUERS = [('IBIT', 'iShares Bitcoin Trust', ibit), ('GBTC', 'Grayscale Bitcoin Trust', lambda: grayscale('GBTC')), ('BTC', 'Grayscale Bitcoin Mini Trust', lambda: grayscale('BTC')),
           ('ARKB', 'ARK 21Shares Bitcoin ETF', arkb), ('BITB', 'Bitwise Bitcoin ETF', bitwise)]
# FBTC, HODL, BTCO, EZBC, BRRR, BTCW: pending parsers; their pages are JS-rendered or PDF-only and need per-issuer work after the first run.

def run(out_dir, price_by_date):
    hold_p = os.path.join(out_dir, 'etf_holdings.json'); hold = json.load(open(hold_p)) if os.path.exists(hold_p) else {}
    status = {}
    for tk, name, fn in ISSUERS:
        try:
            d, btc = fn()
            if not btc: raise RuntimeError('no quantity parsed')
            ser = {x[0]: x[1] for x in hold.get(tk, [])}; ser[d] = btc; hold[tk] = sorted([[k, v] for k, v in ser.items()])
            status[tk] = {'status': 'ok', 'name': name, 'date': d, 'btc': btc}
        except Exception as e:
            status[tk] = {'status': 'error', 'name': name, 'error': str(e)[:200], 'kept_previous': tk in hold}
    with open(hold_p, 'w') as f: json.dump(hold, f, separators=(',', ':'))
    # flows: per issuer, daily change in BTC held x price; total across issuers by date
    flows = {}
    for tk, ser in hold.items():
        for (d0, b0), (d1, b1) in zip(ser, ser[1:]):
            px = price_by_date.get(d1); 
            if px: flows.setdefault(d1, 0.0); flows[d1] += (b1 - b0) * px
    total_btc = {}
    for tk, ser in hold.items():
        if ser: total_btc[ser[-1][0]] = total_btc.get(ser[-1][0], 0) + ser[-1][1]
    doc = {'source': 'etf_flows', 'source_url': 'issuer daily holdings (see status)', 'fetched_at': dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
           'note': 'net flow = change in BTC held x price; accumulates from first successful run; issuers without a parser are listed as pending',
           'issuers': status, 'pending': ['FBTC', 'HODL', 'BTCO', 'EZBC', 'BRRR', 'BTCW'],
           'series': {'net_flow_usd': sorted([[d, v] for d, v in flows.items()]), 'btc_held_by_issuer': {tk: ser[-1] for tk, ser in hold.items() if ser}}}
    with open(os.path.join(out_dir, 'etf_flows.json'), 'w') as f: json.dump(doc, f, separators=(',', ':'))
    ok = [k for k, v in status.items() if v['status'] == 'ok']
    return {'status': 'ok' if ok else 'error', 'issuers_ok': ok, 'issuers_error': [k for k, v in status.items() if v['status'] != 'ok'], 'pending': doc['pending'], 'note': 'flows derive from daily differences and start accumulating from the first successful run'}
