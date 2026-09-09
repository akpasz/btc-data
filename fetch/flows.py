"""fetch/flows.py - capital flow monitor. Writes data/flows.json.

WHAT THIS IS

Every visitor arrives with the question "what will it be worth". This site's
answer has been to decline. This layer answers it the honest way: here is what
is measurable, here is what is not, here is what we are watching, and here is
the date by which each assumption will have been tested.

THE DECOMPOSITION

    market value at a horizon = (realised cap now + capital that arrives)
                                x (MVRV at the horizon)

Two separate bets. The first is how much money comes; the second is where the
cycle is when you look. Published price targets collapse both into one number
and call the second a "multiplier". Kept apart, the second turns out to be the
larger lever and the two are anti-correlated over three-year windows.

WHAT IS MEASURED HERE, FROM DATA
  * realised cap, daily, derived as market cap / MVRV (Coin Metrics publishes
    no direct realised-cap series on the community tier; the derivation
    reconciles to the cent and a test asserts it)
  * realised-cap growth over trailing windows, by cycle - the deceleration
  * the distribution of MVRV over every day since 2013
  * the coupling between window growth and endpoint MVRV
  * a walk-forward of the empirical model at EVERY origin, not a chosen few
  * the supply schedule, deterministic from the halving calendar
  * the holder decomposition, from primary sources only

WHAT IS NOT
  * any forecast. The surface is published with every assumption as a labelled
    axis so that no cell can be read without its cycle assumption beside it.
  * any holder category the pipeline cannot read from a primary source.
    Sovereign holdings are listed only where an on-chain address or a filing
    exists, and the list says so.

TRIPWIRES

Each is a dated, checkable statement. The monitor reports whether each has
been crossed. Precision here is not in the prior; it is in how quickly the
data says an assumption was wrong.
"""
import io, json, math, os, sys, bisect, datetime as dt

OUT = os.environ.get('DATA_DIR', 'data')
SCHEMA_VERSION = '1.0'
FROM = dt.date(2013, 1, 1)
HORIZON_YEARS = 3
CYCLES = [('2013-01-01', '2016-12-31', '2013-16'), ('2017-01-01', '2019-12-31', '2017-19'),
          ('2020-01-01', '2023-12-31', '2020-23')]
HALVINGS = [(dt.date(2024, 4, 20), 3.125), (dt.date(2028, 4, 15), 1.5625), (dt.date(2032, 4, 1), 0.78125)]
BLOCKS_PER_DAY = 144

# Published forecasts, located on the surface. Each is a claim someone else
# made, with the source; none is used in any calculation.
PUBLISHED = [
    {'who': 'River (Sam Baker), 2 Sep 2026', 'horizon': '3-5 years',
     'low': 250_000, 'high': 840_000,
     'basis': 'advisor adoption 20-40% of portfolios at 2-4%, $1.3-5.3T inflow, x3 multiplier',
     'url': 'https://river.com/learn/'},
]

# Sovereign and treasury holders the pipeline can read from a PRIMARY source.
# Anything that is only "widely reported" is deliberately absent.
# Holder lists live in treasuries.py; this layer only reads what it produced.


def _q(v, p):
    s = sorted(v)
    return s[min(len(s) - 1, int(p * (len(s) - 1)))] if s else None


def _load(name):
    p = os.path.join(OUT, name + '.json')
    return json.load(io.open(p, encoding='utf-8')) if os.path.exists(p) else None


def supply_on(date, sply_now, date_now):
    """Deterministic: the halving schedule. Test asserts it reproduces the
    current supply from a known earlier point to within 0.1%."""
    s = sply_now
    d = date_now
    while d < date:
        reward = 3.125
        for hd, r in HALVINGS:
            if d >= hd:
                reward = r
        s += reward * BLOCKS_PER_DAY
        d += dt.timedelta(days=1)
    return s


def realised_cap_series(cm):
    S = cm['series']
    mc = dict((str(d)[:10], float(v)) for d, v in S['CapMrktCurUSD'] if v)
    mv = dict((str(d)[:10], float(v)) for d, v in S['CapMVRVCur'] if v)
    sp = dict((str(d)[:10], float(v)) for d, v in S['SplyCur'] if v)
    pr = dict((str(d)[:10], float(v)) for d, v in S['PriceUSD'] if v)
    days = sorted(d for d in set(mc) & set(mv) & set(sp) & set(pr) if d >= FROM.isoformat())
    return days, {d: mc[d] / mv[d] for d in days}, mv, mc, sp, pr


def _after(D, i, years):
    d0 = D[i]
    try:
        t = d0.replace(year=d0.year + years)
    except ValueError:
        t = d0.replace(year=d0.year + years, day=28)
    j = bisect.bisect_left(D, t)
    return j if j < len(D) else None


def main():
    cm = _load('coinmetrics')
    if not cm:
        print('  flows: coinmetrics.json not available'); return 1
    days, RC, MV, MC, SP, PR = realised_cap_series(cm)
    if len(days) < 1500:
        print(f'  flows: only {len(days)} days, refusing'); return 1
    D = [dt.date.fromisoformat(d) for d in days]
    today = days[-1]
    rc_now, mvrv_now, sply_now, price_now = RC[today], MV[today], SP[today], PR[today]

    # 1. reconciliation, published so a reader can check it
    recon = {'date': today,
             'mktcap_over_supply': round(MC[today] / SP[today], 2), 'price': round(price_now, 2),
             'price_diff_pct': round(100 * (MC[today] / SP[today] / price_now - 1), 4),
             'realised_cap_usd': round(rc_now, 0), 'mvrv': round(mvrv_now, 4),
             'realised_cap_times_mvrv': round(rc_now * mvrv_now, 0), 'mktcap': round(MC[today], 0)}

    # 2. realised-cap growth over trailing windows, and by starting cycle
    windows = {}
    for Y in (1, 3):
        g, mend, starts = [], [], []
        for i in range(len(days)):
            j = _after(D, i, Y)
            if j is None:
                break
            g.append(RC[days[j]] / RC[days[i]]); mend.append(MV[days[j]]); starts.append(days[i])
        by_cycle = {}
        for a, b, lab in CYCLES:
            sub = [x for x, s in zip(g, starts) if a <= s <= b]
            if len(sub) > 30:
                by_cycle[lab] = {'n': len(sub), 'p25': round(_q(sub, .25), 2), 'median': round(_q(sub, .5), 2),
                                 'p75': round(_q(sub, .75), 2)}
        n = len(g); mg = sum(g) / n; mm = sum(mend) / n
        cov = sum((a - mg) * (b - mm) for a, b in zip(g, mend)) / n
        sg = (sum((a - mg) ** 2 for a in g) / n) ** .5; sm = (sum((b - mm) ** 2 for b in mend) / n) ** .5
        windows[str(Y)] = {'n_windows': n, 'all': {'p5': round(_q(g, .05), 2), 'p25': round(_q(g, .25), 2),
                                                    'median': round(_q(g, .5), 2), 'p75': round(_q(g, .75), 2),
                                                    'p95': round(_q(g, .95), 2)},
                           'by_starting_cycle': by_cycle,
                           'corr_growth_vs_endpoint_mvrv': round(cov / (sg * sm), 3) if sg and sm else None}
        # trailing growth as of today
        i = bisect.bisect_left(D, D[-1].replace(year=D[-1].year - Y))
        windows[str(Y)]['trailing_now'] = round(rc_now / RC[days[i]], 3)

    # 3. MVRV distribution
    mv_all = [MV[d] for d in days]
    mvrv_dist = {'p5': round(_q(mv_all, .05), 3), 'p25': round(_q(mv_all, .25), 3), 'median': round(_q(mv_all, .5), 3),
                 'p75': round(_q(mv_all, .75), 3), 'p95': round(_q(mv_all, .95), 3), 'today': round(mvrv_now, 3),
                 'today_percentile': round(100 * sum(1 for x in mv_all if x <= mvrv_now) / len(mv_all), 1)}

    # 4. walk-forward at EVERY origin: forecast 3y ahead using only windows that
    #    had completed by the origin and that started in the preceding cycle
    #    (a fixed 4-year block ending at the origin minus the horizon). Report
    #    how often the actual landed inside the forecast IQR. This is what the
    #    model is worth, stated before any number from it.
    wf = []
    Y = HORIZON_YEARS
    # Origins whose three-year target has NOT yet arrived are computed too and
    # kept as open forecasts. The table used to stop at the last scoreable
    # origin with no sign the method was still running, and readers concluded
    # the page was stale. An open row shows what the method says now and the
    # date its answer is due - which makes the reason the scored rows end
    # self-evident rather than something the page has to explain.
    for oi in range(0, len(days), 30):                       # monthly origins
        od = D[oi]
        j = _after(D, oi, Y)
        try:
            target_date = od.replace(year=od.year + Y)
        except ValueError:
            target_date = od.replace(year=od.year + Y, day=28)
        train_lo = od.replace(year=od.year - Y - 4); train_hi = od.replace(year=od.year - Y)
        draws = []
        for i in range(len(days)):
            if not (train_lo <= D[i] <= train_hi):
                continue
            k = _after(D, i, Y)
            if k is None or D[k] > od:
                continue
            # supply at the target date: known from the schedule even when the
            # target has not arrived, so an open forecast can still be made
            _sup = SP[days[j]] if j is not None else supply_on(target_date, SP[days[-1]], D[-1])
            draws.append(RC[days[oi]] * (RC[days[k]] / RC[days[i]]) * MV[days[k]] / _sup)
        if len(draws) < 100:
            continue
        lo, med, hi = _q(draws, .25), _q(draws, .5), _q(draws, .75)
        row = {'origin': days[oi], 'target': target_date.isoformat(),
               'p25': round(lo), 'median': round(med), 'p75': round(hi)}
        if j is None:
            # still running: the forecast exists, its answer does not
            row.update({'open': True, 'actual': None, 'inside_iqr': None, 'log_error': None,
                        'price_at_origin': round(PR[days[oi]])})
        else:
            actual = PR[days[j]]
            row.update({'open': False, 'target': days[j], 'actual': round(actual),
                        'inside_iqr': bool(lo <= actual <= hi), 'log_error': round(math.log10(actual / med), 3)})
        wf.append(row)
    scored = [w for w in wf if not w.get('open')]
    open_rows = [w for w in wf if w.get('open')]
    wf_cov = sum(1 for w in scored if w['inside_iqr']) / len(scored) if scored else None
    wf_bias = sum(w['log_error'] for w in scored) / len(scored) if scored else None

    # 5. the surface: inflow across, endpoint MVRV down, at the horizon
    horizon_date = D[-1].replace(year=D[-1].year + Y)
    sply_h = supply_on(horizon_date, sply_now, D[-1])
    inflows = [0.5e12, 1.0e12, 1.3e12, 2.0e12, 3.0e12, 5.3e12]
    mvrvs = [1.0, 1.3, 1.7, 2.2, 3.0]
    surface = {'horizon': horizon_date.isoformat(), 'supply_at_horizon': round(sply_h),
               'realised_cap_now': round(rc_now), 'inflow_usd': inflows, 'endpoint_mvrv': mvrvs,
               'price': [[round((rc_now + f) * m / sply_h) for f in inflows] for m in mvrvs]}
    # where each published forecast sits: the endpoint MVRV it implies at its own stated inflow
    for pub in PUBLISHED:
        pub['implied'] = {}
        for lab, price, inf in (('low', pub['low'], 1.3e12), ('high', pub['high'], 5.3e12)):
            pub['implied'][lab] = {'inflow_usd': inf, 'endpoint_mvrv': round(price * sply_h / (rc_now + inf), 2)}

    # 6. holder decomposition, primary sources only
    holders = {'as_of': today, 'supply': round(sply_now), 'categories': {}, 'note': (
        'Only holders the pipeline reads from a primary source: SEC XBRL filings for US spot ETFs and '
        'listed treasury companies, and a published on-chain address for a sovereign. Categories that are '
        'only reported second-hand are absent by design, and the residual is everyone else.')}
    eq = _load('etf_quarterly') or {}
    etf_btc = 0.0; etf_n = 0; etf_detail = {}
    for t, v in (eq.get('trusts') or {}).items():
        qs = v.get('quarters') or []
        if qs:
            last = max(qs, key=lambda x: x['end']); etf_btc += float(last['btc']); etf_n += 1
            etf_detail[t] = {'btc': last['btc'], 'as_of': last['end']}
    holders['categories']['us_spot_etfs'] = {'btc': round(etf_btc), 'pct_supply': round(100 * etf_btc / sply_now, 2),
                                             'n_resolved': etf_n, 'detail': etf_detail,
                                             'source': 'SEC 10-Q/10-K XBRL, us-gaap:CryptoAssetNumberOfUnits'}
    tr = _load('treasuries') or {}
    tr_btc = sum(float(v.get('btc') or 0) for v in (tr.get('companies') or {}).values())
    holders['categories']['listed_treasuries'] = {'btc': round(tr_btc), 'pct_supply': round(100 * tr_btc / sply_now, 2),
                                                  'detail': tr.get('companies') or {},
                                                  'source': 'SEC XBRL where filed; see fetch/treasuries.py'}
    sv = _load('sovereigns') or {}
    sv_btc = sum(float(v.get('btc') or 0) for v in (sv.get('holders') or {}).values())
    holders['categories']['sovereign_on_chain'] = {'btc': round(sv_btc), 'pct_supply': round(100 * sv_btc / sply_now, 2),
                                                   'detail': sv.get('holders') or {},
                                                   'source': 'published reserve addresses, read on chain'}
    known = etf_btc + tr_btc + sv_btc
    holders['tracked_total'] = {'btc': round(known), 'pct_supply': round(100 * known / sply_now, 2)}

    # 7. tripwires: dated, checkable, with live status
    def by(date_iso):
        return dt.date.fromisoformat(date_iso)
    tw = []
    d6 = (D[-1] + dt.timedelta(days=182)).isoformat()
    rc_recent_med = windows['3']['by_starting_cycle'].get('2020-23', {}).get('median')
    tw.append({'name': 'Inflow above the recent cycle', 'check_by': d6,
               'condition': 'realised cap above $1.6T', 'threshold_usd': 1.6e12,
               'reads_now': round(rc_now), 'status': 'crossed' if rc_now > 1.6e12 else 'not yet',
               'means': "capital arriving faster than any window of the 2020-23 cycle; River's high case in play"})
    tw.append({'name': 'Inflow below the low case', 'check_by': d6,
               'condition': 'realised cap below $1.3T on the check date', 'threshold_usd': 1.3e12,
               'reads_now': round(rc_now), 'status': 'pending until check date',
               'means': "even River's low case is not arriving"})
    tw.append({'name': 'Buyer mix has changed', 'check_by': (D[-1] + dt.timedelta(days=365)).isoformat(),
               'condition': 'tracked ETF + treasury + sovereign share above 12% of supply',
               'reads_now': f"{holders['tracked_total']['pct_supply']}%",
               'status': 'crossed' if holders['tracked_total']['pct_supply'] > 12 else 'not yet',
               'means': 'structurally new holders at scale; the historical deceleration may not apply'})
    tw.append({'name': 'Endpoint timing assumption', 'check_by': horizon_date.isoformat(),
               'condition': 'MVRV outside 1.3-2.2 at the horizon', 'reads_now': round(mvrv_now, 3),
               'status': 'pending until horizon',
               'means': 'every published range that held MVRV near constant was wrong about timing'})

    out = {'schema_version': SCHEMA_VERSION,
           'generated_at': dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
           'as_of': today, 'reconciliation': recon,
           'now': {'price': round(price_now, 2), 'realised_cap': round(rc_now), 'mvrv': round(mvrv_now, 3),
                   'supply': round(sply_now), 'market_cap': round(MC[today])},
           'windows': windows, 'mvrv_distribution': mvrv_dist,
           'walk_forward': {'horizon_years': Y, 'origins': len(scored), 'open_forecasts': len(open_rows),
                            'iqr_coverage': round(wf_cov, 3) if wf_cov is not None else None,
                            'mean_log_error': round(wf_bias, 3) if wf_bias is not None else None,
                            'note': ('Forecast at every monthly origin using only windows from the preceding four '
                                     'years that had completed by then. A calibrated model puts the actual inside '
                                     'its interquartile range half the time. Mean log error below zero means the '
                                     'model forecast too high.'),
                            # the 18 most recent scored rows, then every open
                            # forecast, so the table runs to the present day
                            'rows': scored[-18:] + open_rows},
           'surface': surface, 'published_forecasts': PUBLISHED, 'holders': holders, 'tripwires': tw,
           'realised_cap_series': [[d, round(RC[d])] for d in days[::7]],
           'note': ('Realised cap is derived as market cap / MVRV; the reconciliation block shows it. '
                    'Nothing here is a forecast. The surface is published with every assumption as a '
                    'labelled axis so no cell can be read without its cycle assumption beside it.')}
    os.makedirs(OUT, exist_ok=True)
    tmp = os.path.join(OUT, 'flows.json.tmp')
    io.open(tmp, 'w', encoding='utf-8').write(json.dumps(out, separators=(',', ':')))
    os.replace(tmp, os.path.join(OUT, 'flows.json'))
    print(f'  flows: RC ${rc_now/1e12:.2f}T, walk-forward {len(wf)} origins, IQR coverage '
          f'{100*wf_cov:.0f}%, tracked holders {holders["tracked_total"]["pct_supply"]}% of supply')
    return 0


if __name__ == '__main__':
    sys.exit(main())
