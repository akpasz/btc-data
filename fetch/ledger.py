"""fetch/ledger.py - the site's own forward record. Writes data/ledger.json.

WHAT THIS IS

Every page is a snapshot of now plus a backtest of the past, and a backtest
can be argued with forever. What cannot be argued with is a claim written on
a date, committed to git, and scored a year later. This layer runs LAST in
the pipeline and writes one row a day: what every scorecard rule was saying,
which band the composite was in, where each valuation lens sat, and the
price. Once a row is 365 days old it is scored: did a 40% fall or a doubling
follow, and which instruments were right about it.

The ledger is worth nothing on the day it starts. In 2028 it is the most
valuable thing on the site: a timestamped record of what a deterministic
method said in real time, including where it was wrong, that no one can
edit after the fact because the git history holds every version.

WHAT IS SCORED, ONCE A ROW IS OLD ENOUGH
  * the price outcome over the following 365 days: max, min, and whether a
    40% fall or a doubling occurred (the scorecard's two outcomes)
  * for every rule that was firing that day: whether its predicted outcome
    happened. A top rule firing is "right" if a 40% fall followed; a bottom
    rule if a doubling did.
  * for the composite band: what followed from that band

Rows are never rewritten once scored. A row is written once per date; if the
pipeline runs twice in a day the later run replaces the unscored row.
"""
import io, json, os, sys, datetime as dt

OUT = os.environ.get('DATA_DIR', 'data')
SCHEMA_VERSION = '1.0'
HORIZON = 365


def _load(name):
    p = os.path.join(OUT, name + '.json')
    return json.load(io.open(p, encoding='utf-8')) if os.path.exists(p) else None


def main():
    K = _load('kpis'); S = _load('scorecard'); C = _load('composite'); BC = _load('blockchain')
    if not (K and S and BC):
        print('  ledger: inputs not available'); return 1
    p = os.path.join(OUT, 'ledger.json')
    L = json.load(io.open(p, encoding='utf-8')) if os.path.exists(p) else {
        'schema_version': SCHEMA_VERSION, 'began': K['as_of'], 'horizon_days': HORIZON, 'rows': [],
        'note': ('One row per day: what every instrument on the site was saying. Rows older than the horizon are '
                 'scored against what price then did, and never rewritten. The git history of this file is the audit trail.')}
    # The row records what the SCORECARD said, so it is stamped with the
    # scorecard's day. The first version stamped it with kpis' as_of while the
    # firing list came from a scorecard run on a later partial day - a row
    # that said a golden cross was firing on a day no cross occurred. With
    # intraday points dropped at ingest the two dates now agree; this makes
    # the row honest even if they ever diverge again.
    today = S.get('as_of') or K['as_of']
    # Each firing rule is stored WITH its direction and name, frozen at write
    # time. An earlier version looked both up from today's scorecard when
    # scoring a year-old row, so a renamed key, a flipped direction or a
    # retired rule would have changed or silently erased a historical verdict.
    # For a layer whose entire value is immunity to later edits, the row must
    # carry everything its scoring will need.
    firing = sorted(({'key': r['key'], 'direction': r.get('direction'), 'name': r.get('name')}
                     for r in S['rules'] if r.get('firing_today')), key=lambda x: x['key'])
    directions = {r['key']: r.get('direction') for r in S['rules']}   # fallback for pre-freeze rows only
    comp_state = (C or {}).get('today', {}).get('state')
    row = {'date': today, 'price': K.get('price_close'),
           'firing': firing,
           'composite': (C or {}).get('today', {}).get('composite'), 'composite_state': comp_state,
           'powerlaw_pct': (K.get('powerlaw') or {}).get('percentile_close'),
           'mvrv': (K.get('realised') or {}).get('mvrv_close'),
           'scored': None}
    # One-time correction. The row dated 2026-09-08 was written with a firing
    # list from a scorecard that had read a partial 2026-09-09 price; it said a
    # golden cross was firing on 09-08 when, on the complete day, the 50-day
    # average was $124 BELOW the 200-day. The row is unscored, so it may be
    # corrected - but not silently. The original list is kept on the row with
    # the reason, and the git history holds every version.
    for r in L['rows']:
        if r['date'] == '2026-09-08' and r.get('scored') is None and not r.get('corrected'):
            orig = r.get('firing', []); keys = [x['key'] if isinstance(x, dict) else x for x in orig]
            if 'golden_cross' in keys:
                r['corrected'] = {'on': dt.datetime.now(dt.timezone.utc).date().isoformat(),
                                  'original_firing': keys,
                                  'reason': ('firing list came from a scorecard run that had ingested an incomplete '
                                             '2026-09-09 price; on the complete 2026-09-08 day no golden cross had occurred')}
                r['firing'] = [x for x in orig if (x['key'] if isinstance(x, dict) else x) != 'golden_cross']
    # replace today's unscored row if present; never touch a scored one
    L['rows'] = [r for r in L['rows'] if not (r['date'] == today and r.get('scored') is None)]
    if not any(r['date'] == today for r in L['rows']):
        L['rows'].append(row)
    L['rows'].sort(key=lambda r: r['date'])

    # score rows that have aged past the horizon
    px = {str(d)[:10]: float(v) for d, v in BC['series']['price'] if v and float(v) > 0}
    days = sorted(px)
    import bisect
    scored_now = 0
    for r in L['rows']:
        if r.get('scored') is not None: continue
        d0 = dt.date.fromisoformat(r['date']); end = (d0 + dt.timedelta(days=HORIZON)).isoformat()
        if end > days[-1]: continue
        i = bisect.bisect_right(days, r['date']); j = bisect.bisect_right(days, end)
        w = [px[d] for d in days[i:j]]
        if not w or not r.get('price'): continue
        mx, mn = max(w), min(w)
        fell40 = mn <= r['price'] * 0.60; doubled = mx >= r['price'] * 2.0
        verdicts = {}
        for item in r.get('firing', []):
            k = item['key'] if isinstance(item, dict) else item
            dr = item.get('direction') if isinstance(item, dict) else directions.get(k)
            if dr == 'top': verdicts[k] = bool(fell40)
            elif dr == 'bottom': verdicts[k] = bool(doubled)
        r['scored'] = {'on': days[-1], 'max_365': round(mx, 2), 'min_365': round(mn, 2),
                       'fell_40pct': fell40, 'doubled': doubled, 'rules_right': verdicts,
                       'end_price': round(px[days[j - 1]], 2), 'return_pct': round(100 * (px[days[j - 1]] / r['price'] - 1), 1)}
        scored_now += 1

    # running tally for the page
    sc = [r for r in L['rows'] if r.get('scored')]
    tally = {'rows': len(L['rows']), 'scored': len(sc), 'unscored': len(L['rows']) - len(sc),
             'first_scorable': (dt.date.fromisoformat(L['began']) + dt.timedelta(days=HORIZON)).isoformat(),
             'fell_40pct': sum(1 for r in sc if r['scored']['fell_40pct']),
             'doubled': sum(1 for r in sc if r['scored']['doubled'])}
    per_rule = {}
    for r in sc:
        for k, ok in r['scored']['rules_right'].items():
            per_rule.setdefault(k, {'days_firing': 0, 'right': 0})
            per_rule[k]['days_firing'] += 1; per_rule[k]['right'] += 1 if ok else 0
    frozen = {}
    for r in L['rows']:
        for item in r.get('firing', []):
            if isinstance(item, dict): frozen[item['key']] = item
    for k, v in per_rule.items():
        v['name'] = frozen.get(k, {}).get('name') or next((x['name'] for x in S['rules'] if x['key'] == k), k)
        v['direction'] = frozen.get(k, {}).get('direction') or directions.get(k)
    by_state = {}
    for r in sc:
        st = r.get('composite_state') or 'unknown'
        by_state.setdefault(st, {'days': 0, 'fell_40pct': 0, 'doubled': 0})
        by_state[st]['days'] += 1; by_state[st]['fell_40pct'] += 1 if r['scored']['fell_40pct'] else 0
        by_state[st]['doubled'] += 1 if r['scored']['doubled'] else 0
    L['tally'] = tally; L['per_rule'] = per_rule; L['by_composite_state'] = by_state
    L['generated_at'] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    L['as_of'] = today
    tmp = p + '.tmp'
    io.open(tmp, 'w', encoding='utf-8').write(json.dumps(L, separators=(',', ':')))
    os.replace(tmp, p)
    print(f'  ledger: {len(L["rows"])} rows, {len(sc)} scored ({scored_now} today), first scorable {tally["first_scorable"]}, '
          f'{len(firing)} rules firing today')
    return 0


if __name__ == '__main__':
    sys.exit(main())
