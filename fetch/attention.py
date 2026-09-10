"""fetch/attention.py - public attention to bitcoin, from Wikimedia.
Writes data/attention.json.

WHY THIS SOURCE AND NOT GOOGLE TRENDS

Search interest is the series everyone quotes and nobody can reproduce.
Google publishes no official API; the unofficial ones rate-limit, and the
numbers they return are RELATIVE to the query window, so the same request
made twice with different date ranges gives different values. A figure that
changes when you ask for it differently cannot be published here.

Wikimedia's pageviews API is public, keyless, documented, and absolute: the
number of times the English Bitcoin article was read in a month. It goes
back to July 2015, which covers three cycle tops and two bottoms. It is a
narrower measure than search - people who already hold bitcoin do not look
it up on Wikipedia - and the page says so.

WHAT IT IS FOR

The claim, made by many, is that attention at a multi-year low precedes a
recovery and attention at an extreme marks a top. Both directions are
scored on the scorecard against a transition-matched baseline, like every
other rule.
"""
import io, json, os, sys, datetime as dt
import urllib.request

OUT = os.environ.get('DATA_DIR', 'data')
ARTICLES = ['Bitcoin', 'Cryptocurrency']
START = '2015070100'
UA = {'User-Agent': os.environ.get('SEC_CONTACT') or 'CryptoExponentials/1.0 (tools@cryptoexponentials.com)'}


def _get(url, timeout=45):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main():
    end = dt.datetime.now(dt.timezone.utc).strftime('%Y%m0100')
    out = {'schema_version': '1.0', 'source': 'wikimedia pageviews API',
           'source_url': 'https://wikimedia.org/api/rest_v1/metrics/pageviews/',
           'fetched_at': dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
           'note': ('Monthly views of the English Wikipedia article, all access, human agents only. Absolute counts, '
                    'unlike search-interest indices, so the same month always reports the same number. A narrower '
                    'measure of attention than search: people who already hold bitcoin do not look it up here.'),
           'series': {}}
    ok = 0
    for art in ARTICLES:
        url = (f'https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/'
               f'{art}/monthly/{START}/{end}')
        try:
            items = _get(url).get('items', [])
            pts = [[f"{i['timestamp'][:4]}-{i['timestamp'][4:6]}-01", int(i['views'])] for i in items]
            pts = [p for p in pts if p[1] > 0]
            if len(pts) < 24:
                out['series'][art.lower()] = []
                print(f'  attention: {art} returned only {len(pts)} months, ignored', file=sys.stderr)
                continue
            out['series'][art.lower()] = pts
            ok += 1
        except Exception as e:
            out['series'][art.lower()] = []
            print(f'  attention: {art} failed: {str(e)[:120]}', file=sys.stderr)
    if not ok:
        print('  attention: no series retrieved'); return 1
    b = out['series'].get('bitcoin') or []
    out['last_date'] = b[-1][0] if b else None
    os.makedirs(OUT, exist_ok=True)
    tmp = os.path.join(OUT, 'attention.json.tmp')
    io.open(tmp, 'w', encoding='utf-8').write(json.dumps(out, separators=(',', ':')))
    os.replace(tmp, os.path.join(OUT, 'attention.json'))
    print(f'  attention: {len(b)} months to {out["last_date"]}, latest {b[-1][1]:,} views' if b else '  attention: written')
    return 0


if __name__ == '__main__':
    sys.exit(main())
