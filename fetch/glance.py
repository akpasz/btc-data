"""fetch/glance.py - one small file for the front page. Writes data/glance.json.

WHY

At a glance is the page the rail calls "Start here", so it is the first thing
a new reader loads. It was fetching eleven files totalling 2.46 MB - the whole
blockchain source at 1.34 MB for one price line, relative.json at 397 KB for a
single monthly gold series, lppls.json at 329 KB for two numbers, composite at
264 KB for three. Several seconds on a phone before anything appears, for a
page whose job is to be read in ten.

This publishes exactly what that page needs, and nothing else: a few kilobytes
of already-computed summary. The page then fetches two files - this one and
the slim price series - instead of eleven.

The rule for what belongs here: a field the front page displays. Anything a
reader would drill into stays on the page that owns it. This file is a view,
never a source; every value is copied from a layer that computed it, so there
is no second implementation to drift.
"""
import io, json, os, sys, datetime as dt

OUT = os.environ.get('DATA_DIR', 'data')
SCHEMA_VERSION = '1.0'


def _load(name):
    p = os.path.join(OUT, name + '.json')
    return json.load(io.open(p, encoding='utf-8')) if os.path.exists(p) else None


def main():
    K = _load('kpis'); C = _load('composite'); L = _load('lppls'); S = _load('scorecard')
    BR = _load('baserate'); XA = _load('crossasset'); FL = _load('flows')
    EF = _load('etf_flows'); MF = _load('manifest'); RL = _load('relative')
    if not K:
        print('  glance: kpis.json not available'); return 1

    def cut(d, *keys):
        return {k: (d or {}).get(k) for k in keys if (d or {}).get(k) is not None}

    out = {'schema_version': SCHEMA_VERSION,
           'generated_at': dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
           'as_of': K.get('as_of'),
           'note': ('A view for the front page: every value here is copied from the layer that computed it, so this '
                    'file can be regenerated from the others and never disagrees with them. Published so that page '
                    'fetches a few kilobytes instead of two and a half megabytes.'),
           'price_close': K.get('price_close')}

    out['positioning'] = cut(K.get('positioning') or {},
                             'price_30d_change_pct', 'price_vs_200d_pct', 'low_90d', 'high_90d',
                             'position_in_90d_range_pct', 'composite_percentile', 'fear_greed',
                             'fear_greed_percentile', 'drawdown_from_ath_pct', 'ath_close', 'ath_date',
                             'days_since_halving', 'cycle_multiple')
    # value_reference too: the dashboard states the alternative calibration
    # beside the headline premium, so the view must carry both
    # the fit windows too: the gauge names both, and a year typed into the
    # page would go stale the day either fit is re-specified
    out['metcalfe'] = cut(K.get('metcalfe') or {}, 'premium_pct_close', 'percentile_close',
                          'value', 'value_reference', 'fit_from', 'reference_fit_from')
    out['powerlaw'] = cut(K.get('powerlaw') or {}, 'deviation_dex_close', 'percentile_close', 'trend')
    out['realised'] = cut(K.get('realised') or {}, 'mvrv_close', 'percentile_close', 'realised_price')
    out['extended'] = cut(K.get('extended') or {}, 'etf_btc', 'etf_pct_supply')
    _cyc = (K.get('extended') or {}).get('cycle')
    if _cyc: out['extended']['cycle'] = _cyc

    if C: out['composite'] = cut(C.get('today') or {}, 'composite', 'state')
    if L:
        out['lppls'] = {'pos': (L.get('today') or {}).get('pos'),
                        'hit_rate_signal': (L.get('random_baseline') or {}).get('hit_rate_signal'),
                        'hit_rate_all_days': (L.get('random_baseline') or {}).get('hit_rate_all_days')}
    if S:
        rules = S.get('rules') or []
        out['scorecard'] = {
            'count': len(rules), 'own': sum(1 for r in rules if r.get('own')),
            'judged': sum(1 for r in rules if not r.get('own') and r.get('verdict') not in ('Not enough episodes to score', 'Not scored')),
            'beats': sum(1 for r in rules if r.get('verdict') == 'Beats the baseline'),
            'median_episodes': sorted(r.get('episodes') or 0 for r in rules)[len(rules) // 2] if rules else None,
            # the whole grid, small enough to carry: name, direction, episodes,
            # verdict, whether it is firing. This is what the evidence grid draws.
            'rules': [{'key': r.get('key'), 'name': r.get('name'), 'direction': r.get('direction'),
                       'episodes': r.get('episodes'), 'verdict': r.get('verdict'),
                       'firing_today': bool(r.get('firing_today')), 'own': bool(r.get('own')),
                       'difference': r.get('difference'), 'detail': r.get('detail')} for r in rules]}
    if BR and BR.get('outcomes'):
        out['baserate'] = {'fall_40pct': BR['outcomes']['fall_40pct_within_365d']['rate_pct'],
                           'double': BR['outcomes']['double_within_365d']['rate_pct']}
    if XA and XA.get('rules'):
        out['crossasset'] = {k: {'name': v.get('name'), 'verdict': (v.get('pooled') or {}).get('verdict'),
                                 'difference': (v.get('pooled') or {}).get('difference'),
                                 'episodes': (v.get('pooled') or {}).get('episodes')}
                             for k, v in XA['rules'].items()}
    if FL and FL.get('walk_forward'):
        w = FL['walk_forward']; scored = [r for r in (w.get('rows') or []) if not r.get('open')]
        out['flows'] = {'origins': w.get('origins'), 'iqr_coverage': w.get('iqr_coverage'),
                        'recent_scored': len(scored), 'recent_missed': sum(1 for r in scored if not r.get('inside_iqr')),
                        'etf_pct_supply': ((FL.get('holders') or {}).get('tracked_total') or {}).get('pct_supply')}
    if EF and EF.get('issuers'):
        out['etf'] = {'issuers': len(EF['issuers']),
                      'total_btc': sum(float((v or {}).get('btc') or 0) for v in EF['issuers'].values())}
    if MF:
        out['manifest'] = {'generated_at': MF.get('generated_at'), 'errors': MF.get('errors') or [],
                           'sources': len(MF.get('sources') or {}),
                           'degraded': MF.get('degraded_sources') or []}
    # gold: the front page shows one ratio, so carry the latest point rather
    # than the 397 KB file the whole series lives in
    if RL and (RL.get('series') or {}).get('gold_usd_monthly'):
        g = [p for p in RL['series']['gold_usd_monthly'] if p[1]]
        if g:
            out['gold'] = {'usd_per_oz': float(g[-1][1]), 'as_of': g[-1][0]}
            px = out.get('price_close')
            if px:
                out['gold']['bitcoin_in_ounces'] = round(px / float(g[-1][1]), 2)

    os.makedirs(OUT, exist_ok=True)
    tmp = os.path.join(OUT, 'glance.json.tmp')
    io.open(tmp, 'w', encoding='utf-8').write(json.dumps(out, separators=(',', ':')))
    os.replace(tmp, os.path.join(OUT, 'glance.json'))
    kb = os.path.getsize(os.path.join(OUT, 'glance.json')) / 1024
    print(f'  glance: {kb:.0f} KB, replacing ~2,400 KB of source files for the front page')
    return 0


if __name__ == '__main__':
    sys.exit(main())
