"""fetch/slim.py — publish one file per series.

blockchain.json is 1.3 MB and coinmetrics.json is 1.7 MB because each carries
the full history of every series it collects. But the Cycle Monitor needs only
`price` (153 KB of that 1.3 MB), and the Autopsy needs `price`, `supply` and
MVRV. Every page was downloading megabytes to use a fraction of it.

This writes data/series/<name>.json, one series each, so a page fetches only
what it draws. The fat files stay exactly as they are — they are the documented
public API and nothing is removed from them.

Run after the sources, alongside kpis and scorecard:

    import slim; slim.OUT = OUT; slim.main()

Values are rounded to significant figures, not decimal places. These series
span many orders of magnitude — hash rate is 5e-08 in 2009 and 9e+02 today —
and fixed decimals silently flatten the early years to zero. Nine significant
figures is far more than any page renders and still removes the long float
tails that make the source files large.
"""
import io, json, os, sys

OUT = os.environ.get('DATA_DIR', 'data')
SCHEMA_VERSION = '1.0'

# source file -> {series key in that file: (published name, decimals)}
MAP = {
    'blockchain': {
        'price':            ('price', 9),
        'supply':           ('supply', 12),
        'transactions':     ('transactions', 12),
        'unique_addresses': ('unique_addresses', 12),
        'utxo_count':       ('utxo_count', 12),
        'fees_usd':         ('fees_usd', 9),
        'hash_rate':        ('hash_rate', 9),
        'tx_volume_usd':    ('tx_volume_usd', 9),
    },
    'coinmetrics': {
        'CapMVRVCur':    ('mvrv', 9),
        'CapMrktCurUSD': ('market_cap', 9),
        'IssTotUSD':     ('issuance_usd', 9),
        'AdrBalCnt':     ('addr_balance_count', 12),
        'PriceUSD':      ('price_cm', 9),
        'SplyCur':       ('supply_cm', 12),
    },
}


def _round(v, sig):
    """Round to significant figures, not decimal places.

    These series span many orders of magnitude: hash rate is 5e-08 in 2009 and
    9e+02 today. Fixed decimals silently flattened the early years to zero —
    1,341 points of hash rate destroyed before this was caught. Significant
    figures keep the same relative precision at every scale.
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f == 0:
        return 0
    from math import floor, log10
    d = sig - int(floor(log10(abs(f)))) - 1
    r = round(f, d)
    # a whole number is written as 6454, not 6454.0 — one wasted byte per
    # point across 6,400 points per series adds up
    if abs(r) < 2**53 and r == int(r):
        return int(r)
    return r


def main():
    written, total_in, total_out = [], 0, 0
    os.makedirs(os.path.join(OUT, 'series'), exist_ok=True)
    for src, series_map in MAP.items():
        p = os.path.join(OUT, src + '.json')
        if not os.path.exists(p):
            print(f'  slim: {src}.json not found, skipping')
            continue
        total_in += os.path.getsize(p)
        doc = json.load(io.open(p, encoding='utf-8'))
        series = doc.get('series', {})
        for key, (name, dp) in series_map.items():
            pts = series.get(key)
            if not pts:
                continue
            clean = [[d, _round(v, dp)] for d, v in pts if v is not None]
            clean = [x for x in clean if x[1] is not None]
            if not clean:
                continue
            out = {
                'schema_version': SCHEMA_VERSION,
                'series_name': name,
                'source': src,
                'source_key': key,
                'source_url': doc.get('source_url'),
                'fetched_at': doc.get('fetched_at'),
                'significant_figures': dp,
                'points': len(clean),
                'first_date': clean[0][0],
                'last_date': clean[-1][0],
                'note': f'Single series extracted from {src}.json. '
                        f'The full file remains published and unchanged.',
                'values': clean,
            }
            dest = os.path.join(OUT, 'series', name + '.json')
            tmp = dest + '.tmp'
            io.open(tmp, 'w', encoding='utf-8').write(json.dumps(out, separators=(',', ':')))
            os.replace(tmp, dest)          # atomic: a failed run leaves the last good file
            sz = os.path.getsize(dest)
            total_out += sz
            written.append((name, len(clean), sz))

    # an index so a consumer can discover what exists without guessing
    idx = {
        'schema_version': SCHEMA_VERSION,
        'note': 'One file per series, for pages and consumers that need a single '
                'metric rather than a whole source file. Fetch '
                'series/<series_name>.json; values are [date, number] pairs, '
                'sorted ascending, nulls excluded.',
        'series': [{'name': n, 'points': c, 'bytes': s,
                    'url': f'series/{n}.json'} for n, c, s in sorted(written)],
    }
    tmp = os.path.join(OUT, 'series', 'index.json.tmp')
    io.open(tmp, 'w', encoding='utf-8').write(json.dumps(idx, indent=1))
    os.replace(tmp, os.path.join(OUT, 'series', 'index.json'))

    for n, c, s in sorted(written):
        print(f'  slim: series/{n}.json  {c} points, {s/1024:.0f} KB')
    print(f'  slim: {len(written)} series, {total_out/1024:.0f} KB total '
          f'(source files {total_in/1024:.0f} KB, unchanged)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
