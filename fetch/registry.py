"""fetch/registry.py - pre-registered rules, scored daily. Reads
data/registry.json (the register) and writes data/registry_scores.json.

WHAT THIS IS

The rule builder scores a rule and forgets it. This remembers. A rule is
registered with a date, in the builder's vocabulary - indicator, operator,
threshold, direction - and the pipeline scores it every morning through the
scorecard's engine, exactly as it scores the fifteen. Its record accumulates
in public beside them.

WHAT PRE-REGISTRATION MEANS HERE

A rule registered BEFORE its result was seen is a genuine prediction. A rule
registered after is a fit. The register records the date; the page shows how
many episodes have occurred SINCE registration and scores those separately
from the full history. The full-history score answers "would this have
worked"; the since-registration score answers "did it", and only the second
is evidence.

HOW A RULE GETS IN

By GitHub issue, using the template in .github/ISSUE_TEMPLATE/register-rule.yml.
A maintainer checks the spec parses and appends it here; the pipeline does
the rest. There is no approval on merit - a registered rule that fails is a
result, and the register keeps it.

THE SITE'S OWN RULES ARE SEEDED, NOT PRE-REGISTERED. Their thresholds were
chosen after seeing history. They are in the register so their record runs
forward from the register's start date on the same terms as everyone else's;
the entry says so.
"""
import io, json, os, sys, datetime as dt

OUT = os.environ.get('DATA_DIR', 'data')
sys.path.insert(0, os.path.dirname(__file__))

INDICATORS = {  # key -> (label, unit)
    'mayer': ('Mayer multiple (price / 200-day average)', 'x'), 'p200w': ('Price / 200-week average', 'x'),
    'p2y': ('Price / 2-year average', 'x'), 'p50v200': ('50-day average / 200-day average', 'x'),
    'pi': ('111-day average / (2 x 350-day average)', 'x'), 'rsi': ('RSI-14', ''), 'mvrv': ('MVRV', ''),
    'fg': ('Fear and Greed index', ''), 'hashrib': ('Hash rate, 30-day / 60-day average', 'x'),
    'ath': ('Drawdown from all-time high', '%')}
OPERATORS = ('above', 'below', 'crosses_above', 'crosses_below')


def _load(name):
    p = os.path.join(OUT, name + '.json')
    return json.load(io.open(p, encoding='utf-8')) if os.path.exists(p) else None


def series_for(key, dates, px, mvrv, hr, fg):
    import scorecard as sc
    n = len(px)
    if key == 'rsi': return sc.rsi(px, 14)
    if key == 'mvrv': return mvrv
    if key == 'fg': return fg
    if key == 'ath':
        out, m = [], -1e300
        for p in px:
            m = max(m, p); out.append(100 * (p / m - 1))
        return out
    if key == 'hashrib':
        f, last = [], None
        for x in hr:
            if x is not None: last = x
            f.append(last if last is not None else 0.0)
        a, b = sc.sma(f, 30), sc.sma(f, 60)
        return [(a[i] / b[i]) if (a[i] is not None and b[i]) else None for i in range(n)]
    ma = {'mayer': 200, 'p200w': 1400, 'p2y': 730}
    if key in ma:
        m_ = sc.sma(px, ma[key]); return [px[i] / m_[i] if m_[i] else None for i in range(n)]
    if key == 'p50v200':
        a, b = sc.sma(px, 50), sc.sma(px, 200); return [(a[i] / b[i]) if (a[i] is not None and b[i]) else None for i in range(n)]
    if key == 'pi':
        a, b = sc.sma(px, 111), sc.sma(px, 350); return [(a[i] / (2 * b[i])) if (a[i] is not None and b[i]) else None for i in range(n)]
    raise ValueError(key)


def fires_for(v, op, thr):
    n = len(v); fires = [False] * n; elig = [v[i] is not None for i in range(n)]
    for i in range(n):
        if not elig[i]: continue
        if op == 'above': fires[i] = v[i] > thr
        elif op == 'below': fires[i] = v[i] < thr
        elif op == 'crosses_above': fires[i] = i > 0 and v[i - 1] is not None and v[i] > thr and v[i - 1] <= thr
        elif op == 'crosses_below': fires[i] = i > 0 and v[i - 1] is not None and v[i] < thr and v[i - 1] >= thr
    return fires, elig


def main():
    import scorecard as sc
    R = _load('registry'); bc = _load('blockchain'); cm = _load('coinmetrics'); fgd = _load('fear_greed')
    if not (R and bc):
        print('  registry: register or price data absent'); return 1
    # the FULL price series, exactly as scorecard.py reads it. A first version
    # started at 2013 and lost ten of thirteen rules' early episodes to the
    # moving-average warm-up; the register must see what the scorecard sees.
    pr = [(str(d)[:10], float(v)) for d, v in bc['series']['price'] if v and float(v) > 0]
    dates = [d for d, _ in pr]; px = [v for _, v in pr]; n = len(px)
    mv = {str(d)[:10]: float(v) for d, v in (cm or {}).get('series', {}).get('CapMVRVCur', []) if v}
    hr = {str(d)[:10]: float(v) for d, v in bc['series'].get('hash_rate', []) if v}
    fg = {str(d)[:10]: float(v) for d, v in (fgd or {}).get('series', {}).get('index', []) if v}
    mvrv = [mv.get(d) for d in dates]; hrs = [hr.get(d) for d in dates]; fgs = [fg.get(d) for d in dates]
    out = {'schema_version': '1.0', 'generated_at': dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
           'as_of': dates[-1], 'rules': [], 'note': __doc__.strip().split('\n\n')[2]}
    for e in R.get('rules', []):
        try:
            v = series_for(e['ind'], dates, px, mvrv, hrs, fgs)
            fires, elig = fires_for(v, e['op'], float(e['thr']))
            full = sc.score(dates, px, fires, elig, e['dir'])
            # since registration only: episodes whose first day is on or after the registration date
            reg = e.get('registered', dates[-1])
            elig_since = [elig[i] and dates[i] >= reg for i in range(n)]
            since = sc.score(dates, px, fires, elig_since, e['dir'])
            lastf = next((dates[i] for i in range(n - 1, -1, -1) if fires[i] and elig[i]), None)
            out['rules'].append({'id': e['id'], 'registered': reg, 'by': e.get('by', 'anonymous'), 'seeded': bool(e.get('seeded')),
                                 'ind': e['ind'], 'op': e['op'], 'thr': e['thr'], 'dir': e['dir'],
                                 'label': f"{INDICATORS[e['ind']][0]} {e['op'].replace('_', ' ')} {e['thr']}{INDICATORS[e['ind']][1]} \u2192 "
                                          + ('a 40% fall' if e['dir'] == 'top' else 'a doubling') + ' within a year',
                                 'claim': e.get('claim', ''),
                                 'full_history': {k: full.get(k) for k in ('episodes', 'censored', 'hit_rate', 'hit_rate_ci90', 'baseline_rate', 'baseline_days')},
                                 'since_registration': {k: since.get(k) for k in ('episodes', 'censored', 'hit_rate', 'hit_rate_ci90', 'baseline_rate', 'baseline_days', 'pending_since')},
                                 'firing_today': bool(fires[-1] and elig[-1]), 'last_fired': lastf})
        except Exception as ex:
            out['rules'].append({'id': e.get('id'), 'error': str(ex)[:160]})
    out['count'] = len(out['rules']); out['seeded'] = sum(1 for r in out['rules'] if r.get('seeded'))
    out['expected_false_positives_at_90pct'] = round(0.10 * (out['count'] - out['seeded']), 1)
    p = os.path.join(OUT, 'registry_scores.json'); tmp = p + '.tmp'
    io.open(tmp, 'w', encoding='utf-8').write(json.dumps(out, separators=(',', ':')))
    os.replace(tmp, p)
    print(f'  registry: {out["count"]} rules scored ({out["seeded"]} seeded), {sum(1 for r in out["rules"] if r.get("firing_today"))} firing today')
    return 0


if __name__ == '__main__':
    sys.exit(main())
