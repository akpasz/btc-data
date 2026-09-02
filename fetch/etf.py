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
def _ibit_parse_xls(txt):
    """iShares SpreadsheetML fund download; the XML is invalid (unescaped & in disclaimer links), so regex parse.
    Verified against a real downloaded file (2026-08-31: 779,839.6606 BTC). Quantity is the last numeric column of the BTC row."""
    date = None
    m = re.search(r'Fund Holdings as of</ss:Data>.*?<ss:Data[^>]*>([A-Za-z]{3} \d{1,2}, \d{4})</ss:Data>', txt, re.S)
    if m: date = dt.datetime.strptime(m.group(1), '%b %d, %Y').date().isoformat()
    btc = None
    for rm in re.finditer(r'<ss:Row[^>]*>\s*<ss:Cell[^>]*>\s*<ss:Data[^>]*>BTC</ss:Data>\s*</ss:Cell>(.*?)</ss:Row>', txt, re.S):
        nums = []
        for c in re.findall(r'<ss:Data[^>]*>(.*?)</ss:Data>', rm.group(1), re.S):
            try: nums.append(float(c.replace(',', '').strip()))
            except ValueError: pass
        if nums and 1e4 <= nums[-1] <= 5e6: btc = nums[-1]; break
    return date, btc

def _ibit_parse_csv(txt):
    """The product page's direct latest-holdings.csv: header block, then a quoted CSV table whose BTC row carries Quantity."""
    date = None; btc = None
    m = re.search(r'Fund Holdings as of,"?([A-Za-z]{3} \d{1,2}, \d{4})', txt)
    if m: date = dt.datetime.strptime(m.group(1), '%b %d, %Y').date().isoformat()
    for row in csv.reader(io.StringIO(txt)):
        if row and row[0].strip().upper() == 'BTC':
            cands = [v for v in (_num(c) for c in row) if v and 1e4 <= v <= 5e6]
            if cands: btc = max(cands); break
    return date, btc

def ibit():
    # Primary: the static CSV the product page links directly ("Download Holdings CSV").
    try:
        txt = _get('https://www.ishares.com/us/products/333011/ishares-bitcoin-trust-etf/latest-holdings.csv').text
        if '<html' not in txt[:2000].lower():
            date, btc = _ibit_parse_csv(txt)
            if date and btc: return date, btc
    except Exception: pass
    # Secondary: the "Data Download" Excel endpoint behind the product page's button (SpreadsheetML).
    txt = _get('https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document'
               '?appType=PRODUCT_PAGE&appSubType=ISHARES&targetSite=us-ishares&locale=en_US&portfolioId=333011'
               '&component=fundDownload&userType=individual').text
    if '<ss:Workbook' not in txt[:2000]:
        raise RuntimeError('both IBIT endpoints returned pages, not data files; source needs a new address')
    date, btc = _ibit_parse_xls(txt)
    if not (date and btc): raise RuntimeError('IBIT file fetched but the BTC row was not found')
    return date, btc

def grayscale(ticker):
    # Grayscale product pages expose "Bitcoin per Share" and "Shares Outstanding"; holdings = product of the two
    url = {'GBTC': 'https://etfs.grayscale.com/gbtc', 'BTC': 'https://etfs.grayscale.com/btc'}[ticker]
    time.sleep(20); txt = _get(url, tries=4).text
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
    bad = [k for k, v in status.items() if v['status'] != 'ok']
    # 'partial' when some issuers fail, so the hub does not show a green 'ok' over a mostly-failing source
    return {'status': ('ok' if not bad else 'partial') if ok else 'error', 'issuers_ok': ok, 'issuers_error': bad, 'pending': doc['pending'], 'note': 'flows derive from daily differences and start accumulating from the first successful run'}
