"""fetch/scorecard.py — Indicator Scorecard.

Writes data/scorecard.json. Runs after the raw sources, since it reads
blockchain.json, coinmetrics.json and lppls.json off disk.

Add to fetch_all.py alongside the other derived layers:

    from scorecard import main as scorecard_main
    ...
    try:
        scorecard_main()
    except Exception as e:
        errors.append(f'scorecard: {e}')

Isolated by design: a failure records a reason and leaves the previous
scorecard.json in place.

Does each popular signal beat a matched baseline?

Writes scorecard.json. Every rule is scored the same way:

  eligible days   the rule COULD have fired (its inputs existed) and the
                  outcome window is complete
  triggers        eligible days on which it did fire, grouped into episodes
  baseline        eligible days that did NOT fire, excluding each trigger and
                  the 90 days after it (transition-matched, so the baseline is
                  not contaminated by the aftermath of the signal itself)
  outcome         top rules: a 40% fall from the close within 365 days
                  bottom rules: a doubling of the close within 365 days
  verdict         difference in points, with no verdict below MIN_EPISODES

Right-censored episodes (window not complete) are excluded from denominators
and counted separately. No rule is scored on data it could not have seen.
"""
import math
import io,json,os,sys,math,urllib.request,datetime

DATA=os.environ.get('DATA_DIR','data')
BASES=['https://akpasz.github.io/btc-data/data/',
       'https://raw.githubusercontent.com/akpasz/btc-data/main/data/']
LOCAL=os.environ.get('SNAP_DIR')
MIN_EPISODES=20
HORIZON=365
CLUSTER=90
# CLUSTER: days after a trigger treated as the same episode

def load(n):
    """In the pipeline the files are already on disk beside us; fall back to
    the published copies so the module can be run standalone for testing."""
    for p in (f'{DATA}/{n}.json', f'{LOCAL}/{n}.json' if LOCAL else None):
        if p and os.path.exists(p):
            return json.load(io.open(p,encoding='utf-8'))
    for b in BASES:
        try:
            with urllib.request.urlopen(b+n+'.json',timeout=40) as r:
                return json.loads(r.read().decode())
        except Exception: pass
    return None

def series(d,k):
    s=(d or {}).get('series',{}).get(k) or []
    return [(x[0],float(x[1])) for x in s if x[1] is not None]

def sma(v,n):
    out=[None]*len(v); s=0.0
    for i,x in enumerate(v):
        s+=x
        if i>=n: s-=v[i-n]
        if i>=n-1: out[i]=s/n
    return out

def rsi(v,n=14):
    out=[None]*len(v)
    if len(v)<n+1: return out
    g=l=0.0
    for i in range(1,n+1):
        d=v[i]-v[i-1]; g+=max(d,0); l+=max(-d,0)
    g/=n; l/=n
    out[n]=100-100/(1+(g/l if l else 999))
    for i in range(n+1,len(v)):
        d=v[i]-v[i-1]
        g=(g*(n-1)+max(d,0))/n; l=(l*(n-1)+max(-d,0))/n
        out[i]=100-100/(1+(g/l if l else 999))
    return out

def score(dates,px,fires,eligible,direction):
    """fires/eligible: boolean lists. direction 'top' or 'bottom'."""
    n=len(px)
    def outcome(i):
        j=min(n,i+HORIZON+1)
        if i+HORIZON>=n: return None                 # right-censored
        w=px[i+1:j]
        if not w: return None
        return (min(w)<=px[i]*0.60) if direction=='top' else (max(w)>=px[i]*2.0)

    # episodes: a run of triggers, plus anything within CLUSTER days of it
    eps=[];i=0
    while i<n:
        if fires[i] and eligible[i]:
            start=i; last=i; j=i+1
            while j<n and j-last<=CLUSTER:
                if fires[j] and eligible[j]: last=j
                j+=1
            eps.append((start,last)); i=last+1
        else: i+=1
    # `eligible` now means the INPUTS existed on that day. Whether the 365-day
    # outcome window has closed is decided here, so an episode that fired
    # recently is counted as PENDING rather than dropped.
    #
    # It used to be dropped invisibly. Every eligible definition already
    # required i + HORIZON < n, so an episode could never be censored and the
    # counter was unreachable - it reported 0 for every rule while seven rules
    # had fired inside the last year. "Price below the 2-year average" had been
    # firing continuously since January with no hint on the page.
    hit=tot=cens=0
    pending_dates=[]
    for s,_ in eps:
        o=outcome(s)
        if o is None:
            cens+=1
            pending_dates.append(dates[s])
            continue
        tot+=1; hit+=1 if o else 0

    # transition-matched baseline
    excl=[False]*n
    for s,e in eps:
        for k in range(s,min(n,e+CLUSTER+1)): excl[k]=True
    bh=bt=0
    for i in range(n):
        if not eligible[i] or excl[i]: continue
        o=outcome(i)
        if o is None: continue
        bt+=1; bh+=1 if o else 0
    # How wide is the uncertainty on that hit rate?
    #
    # "Five observations cannot be told apart from luck" was an assertion until
    # now. This measures it.
    #
    # A resampling bootstrap was the obvious choice and it is WRONG here. When
    # every episode is a hit, as with Pi Cycle at five for five, every resample
    # returns 100% and the interval collapses to "100% to 100%" - reporting
    # certainty from five observations, which is the precise opposite of the
    # point. The statistic is a binomial proportion, so the Wilson score
    # interval is the right tool: it is well behaved at zero and one, needs no
    # random draws, and gives the same answer every run.
    #
    # Episodes are the unit, not days, because the days inside one episode are
    # the same event.
    hit_n = sum(1 for s0, _ in eps if outcome(s0) is True)
    obs_n = sum(1 for s0, _ in eps if outcome(s0) is not None)
    ci = None
    if obs_n >= 2:
        z = 1.6449                      # 90% two-sided
        p = hit_n / obs_n
        d = 1 + z * z / obs_n
        centre = (p + z * z / (2 * obs_n)) / d
        half = (z / d) * math.sqrt(p * (1 - p) / obs_n + z * z / (4 * obs_n * obs_n))
        ci = [round(max(0.0, centre - half) * 100, 1),
              round(min(1.0, centre + half) * 100, 1)]

    return dict(episodes=tot,censored=cens,
        pending_since=(pending_dates[0] if pending_dates else None),
        hit_rate=(100*hit/tot) if tot else None,
        hit_rate_ci90=ci,
        baseline_rate=(100*bh/bt) if bt else None,
        baseline_days=bt,
        eligible_days=sum(1 for i in range(n) if eligible[i]))

def _lppls_summary(rb):
    """Compact form of a random_baseline result for the scorecard."""
    if not rb or 'error' in rb:
        return None
    if not rb.get('signal_days'):
        return {'signal_days': 0, 'runs': 0, 'note': 'never fires at confidence 0.5'}
    return {'signal_days': rb.get('signal_days'), 'runs': rb.get('runs'),
            'hit_rate': round(100*float(rb['hit_rate_signal']), 1),
            'baseline_rate': round(100*float(rb['hit_rate_all_days']), 1),
            'random_p5': round(100*float(rb['random_p5']), 1), 'random_p95': round(100*float(rb['random_p95']), 1),
            'p': rb.get('p_random_at_least_observed'), 'direction': rb.get('direction', 'top')}


def main():
    bc=load('blockchain'); cm=load('coinmetrics'); lp=load('lppls'); fg=load('fear_greed')
    if not bc: print('no blockchain.json'); return 1
    pr=[(d,v) for d,v in series(bc,'price') if v>0]
    dates=[d for d,_ in pr]; px=[v for _,v in pr]; n=len(px)
    ma50,ma111,ma200,ma350=sma(px,50),sma(px,111),sma(px,200),sma(px,350)
    ma200w,ma2y=sma(px,1400),sma(px,730)
    r=rsi(px,14)
    mv={d:v for d,v in series(cm,'CapMVRVCur')}
    mvrv=[mv.get(d) for d in dates]
    # series behind the wider bull-bear indicators, aligned to the price
    # calendar. Each may be absent; every rule below guards on its own inputs.
    st=load('stablecoins'); at=load('attention')
    _mc={d:v for d,v in series(cm,'CapMrktCurUSD')}
    _iss={d:v for d,v in series(cm,'IssTotUSD')}
    _sply={d:v for d,v in series(cm,'SplyCur')}
    _fees={d:v for d,v in series(bc,'fees_usd')}
    _stbl={d:v for d,v in series(st,'total_usd')} if st else {}
    mc=[_mc.get(d) for d in dates]
    iss=[_iss.get(d) for d in dates]
    sply=[_sply.get(d) for d in dates]
    fees=[_fees.get(d) for d in dates]
    # realised cap = market cap / MVRV, the identity the flow monitor publishes
    rc=[(mc[i]/mvrv[i]) if (mc[i] and mvrv[i]) else None for i in range(n)]
    # stablecoin supply and monthly attention are carried forward to daily
    def _ffill(m):
        out=[]; last=None
        for d in dates:
            if d in m and m[d]: last=m[d]
            out.append(last)
        return out
    stbl=_ffill(_stbl) if _stbl else None
    _att={}
    if at:
        for d,v in (at.get('series',{}).get('bitcoin') or []): _att[d]=v
    att=_ffill(_att) if _att else None

    def el(*arrs):
        return [all(a[i] is not None for a in arrs) for i in range(n)]

    RULES=[]
    def add(key,name,claim,direction,fires,eligible,detail):
        s=score(dates,px,fires,eligible,direction)
        # Is the rule firing on the latest day, and when did it last fire? The
        # scorecard showed every rule's track record and not its current state,
        # so a visitor asking "is RSI oversold today, and does that matter?"
        # got the second half on a page that withheld the first.
        _last=None
        for _i in range(len(fires)-1,-1,-1):
            if fires[_i] and eligible[_i]:
                _last=dates[_i]; break
        s.update(key=key,name=name,claim=claim,direction=direction,detail=detail,
                 firing_today=bool(fires[-1] and eligible[-1]) if len(fires) else None,
                 last_fired=_last, as_of_day=dates[-1] if len(dates) else None)
        RULES.append(s)

    e=el(ma111,ma350)
    add('pi_cycle','Pi Cycle Top','The 111-day average crossing above twice the 350-day average marks a cycle top.',
        'top',[bool(e[i] and ma111[i]>2*ma350[i]) for i in range(n)],e,'/tools/bitcoin-indicator-autopsy')
    e=el(ma200)
    add('mayer_high','Mayer multiple above 2.4','Price at 2.4x its 200-day average is an overheated market.',
        'top',[bool(e[i] and px[i]/ma200[i]>2.4) for i in range(n)],e,'/tools/bitcoin-technical-signals')
    e=el(ma200w)
    add('below_200w','Price below the 200-week average','Price under its 200-week average is a generational buying zone.',
        'bottom',[bool(e[i] and px[i]<ma200w[i]) for i in range(n)],e,'/tools/bitcoin-indicator-autopsy')
    e=el(ma2y)
    add('two_year_ma','Price below the 2-year average','The 2-year moving average multiplier marks the accumulation band.',
        'bottom',[bool(e[i] and px[i]<ma2y[i]) for i in range(n)],e,'/tools/bitcoin-indicator-autopsy')
    e=el(ma50,ma200)
    add('golden_cross','Golden cross','The 50-day crossing above the 200-day average starts a bull phase.',
        'bottom',[bool(e[i] and i>0 and ma50[i-1] is not None and ma200[i-1] is not None and ma50[i]>ma200[i] and ma50[i-1]<=ma200[i-1]) for i in range(n)],e,'/tools/bitcoin-technical-signals')
    add('death_cross','Death cross','The 50-day crossing below the 200-day average starts a bear phase.',
        'top',[bool(e[i] and i>0 and ma50[i-1] is not None and ma200[i-1] is not None and ma50[i]<ma200[i] and ma50[i-1]>=ma200[i-1]) for i in range(n)],e,'/tools/bitcoin-technical-signals')
    e=[r[i] is not None for i in range(n)]
    add('rsi_hot','RSI-14 above 70','An overbought RSI precedes a fall.',
        'top',[bool(e[i] and r[i]>70) for i in range(n)],e,'/tools/bitcoin-technical-signals')
    add('rsi_cold','RSI-14 below 30','An oversold RSI precedes a rally.',
        'bottom',[bool(e[i] and r[i]<30) for i in range(n)],e,'/tools/bitcoin-technical-signals')
    e=[mvrv[i] is not None for i in range(n)]
    add('mvrv_high','MVRV above 3.7','An MVRV above 3.7 marks the top of the cycle.',
        'top',[bool(e[i] and mvrv[i]>3.7) for i in range(n)],e,'/tools/bitcoin-realised-value-monitor')
    add('mvrv_low','MVRV below 1','An MVRV below 1 means the average holder is under water: a bottom.',
        'bottom',[bool(e[i] and mvrv[i]<1.0) for i in range(n)],e,'/tools/bitcoin-realised-value-monitor')


    # ---- indicators from the wider bull-bear tally -----------------------
    # Fourteen indicators people quote when arguing the cycle, each computed
    # from series this pipeline already publishes and scored the same way as
    # every other rule. Adding them is not endorsement: it is the only way to
    # say whether "the Puell multiple marks cycle lows" is a claim with a
    # record or a claim with an anecdote. The thresholds are the ones the
    # people making the claims use, not thresholds fitted here.
    cum = []
    _t = 0.0
    for v in (iss or []):
        _t += (v or 0.0); cum.append(_t)
    thermo = cum                                     # cumulative miner revenue
    iss365 = sma(iss, 365) if iss else [None]*n
    puell = [(iss[i]/iss365[i]) if (iss365 and iss365[i]) else None for i in range(n)]
    e = [puell[i] is not None for i in range(n)]
    add('puell_low', 'Puell multiple below 0.4',
        'Miner revenue far below its yearly average marks a cycle low.',
        'bottom', [bool(e[i] and puell[i] < 0.4) for i in range(n)], e, '/tools/bitcoin-miners-monitor')
    add('puell_high', 'Puell multiple above 4',
        'Miner revenue far above its yearly average marks a cycle top.',
        'top', [bool(e[i] and puell[i] > 4.0) for i in range(n)], e, '/tools/bitcoin-miners-monitor')

    # MVRV Z: (market cap - realised cap) / standard deviation of market cap
    if mc and rc:
        zs = []
        for i in range(n):
            if mc[i] is None or rc[i] is None or i < 365: zs.append(None); continue
            w = [x for x in mc[max(0,i-364):i+1] if x is not None]
            if len(w) < 100: zs.append(None); continue
            mu = sum(w)/len(w); sd = (sum((x-mu)**2 for x in w)/len(w))**0.5
            zs.append((mc[i]-rc[i])/sd if sd else None)
        e = [zs[i] is not None for i in range(n)]
        add('mvrvz_low', 'MVRV Z-score below zero',
            'Market value below realised value, in standard deviations, marks the bottom.',
            'bottom', [bool(e[i] and zs[i] < 0) for i in range(n)], e, '/tools/bitcoin-realised-value-monitor')
        add('mvrvz_high', 'MVRV Z-score above 5',
            'An extreme Z-score marks the top of the cycle.',
            'top', [bool(e[i] and zs[i] > 5) for i in range(n)], e, '/tools/bitcoin-realised-value-monitor')
        # market cap / thermocap
        mt = [(mc[i]/thermo[i]) if (i < len(thermo) and thermo[i] and mc[i]) else None for i in range(n)]
        e = [mt[i] is not None for i in range(n)]
        add('mcap_thermo_low', 'Market cap under 10x thermocap',
            'Market value close to everything ever paid to miners marks a cycle low.',
            'bottom', [bool(e[i] and mt[i] < 10) for i in range(n)], e, '/tools/bitcoin-miners-monitor')
        # price / realised price, as its own rule
        e = [rc[i] is not None and mc[i] is not None for i in range(n)]
        add('below_realised', 'Price below realised price',
            'When price falls under what the average holder paid, the bottom is in.',
            'bottom', [bool(e[i] and mc[i] < rc[i]) for i in range(n)], e, '/tools/bitcoin-realised-value-monitor')
        # balanced price = realised price - transferred price, approximated as
        # realised price x (1 - realised/market): published as an approximation
        bal = [(rc[i]/sply[i]) * (rc[i]/mc[i]) if (rc[i] and mc[i] and sply and sply[i]) else None for i in range(n)]
        e = [bal[i] is not None for i in range(n)]
        add('below_balanced', 'Price below the balanced price',
            'A deeper floor than realised price, reached only in the worst bear markets.',
            'bottom', [bool(e[i] and px[i] < bal[i]) for i in range(n)], e, '/tools/bitcoin-realised-value-monitor')

    # stablecoin supply ratio: market cap / stablecoin supply
    if mc and stbl:
        ssr = [(mc[i]/stbl[i]) if (stbl[i] and mc[i]) else None for i in range(n)]
        w = [x for x in ssr if x is not None]
        if len(w) > 400:
            lo = sorted(w)[int(0.10*(len(w)-1))]
            e = [ssr[i] is not None for i in range(n)]
            add('ssr_low', 'Stablecoin supply ratio in its lowest tenth',
                'Plenty of dry powder relative to market value precedes a rally.',
                'bottom', [bool(e[i] and ssr[i] <= lo) for i in range(n)], e, '/tools/bitcoin-flows-positioning-monitor')

    # weekly and monthly RSI, on resampled closes carried back to daily
    def _resample_rsi(step_days):
        idx = list(range(0, n, step_days))
        closes = [px[i] for i in idx]
        rr = rsi(closes, 14)
        out = [None]*n
        for k, i in enumerate(idx):
            j = idx[k+1] if k+1 < len(idx) else n
            for t in range(i, j): out[t] = rr[k]
        return out
    wrsi = _resample_rsi(7); mrsi = _resample_rsi(30)
    e = [wrsi[i] is not None for i in range(n)]
    add('rsi_weekly_low', 'Weekly RSI below 30',
        'An oversold weekly RSI marks the bottom of a bear market.',
        'bottom', [bool(e[i] and wrsi[i] < 30) for i in range(n)], e, '/tools/bitcoin-technical-signals')
    e = [mrsi[i] is not None for i in range(n)]
    add('rsi_monthly_low', 'Monthly RSI below 40',
        'A monthly RSI at bear-market levels marks the bottom.',
        'bottom', [bool(e[i] and mrsi[i] < 40) for i in range(n)], e, '/tools/bitcoin-technical-signals')

    # one-year trailing return
    roi = [(px[i]/px[i-365]-1) if i >= 365 and px[i-365] else None for i in range(n)]
    e = [roi[i] is not None for i in range(n)]
    add('roi1y_low', 'One-year return below -50%',
        'A full reset of the trailing year marks the capitulation low.',
        'bottom', [bool(e[i] and roi[i] < -0.5) for i in range(n)], e, '/tools/bitcoin-cycle-monitor')

    # 50-week (350-day) moving average recapture
    ma350 = sma(px, 350)
    e = [ma350[i] is not None for i in range(n)]
    add('reclaim_50w', 'Price reclaims the 50-week average',
        'A convincing close back above the 50-week average confirms the low is in.',
        'bottom', [bool(e[i] and i > 0 and ma350[i-1] is not None and px[i] > ma350[i]*1.02 and px[i-1] <= ma350[i-1]*1.02) for i in range(n)],
        e, '/tools/bitcoin-technical-signals')

    # transaction fees at a multi-year low
    if fees:
        fw = [x for x in fees if x is not None]
        if len(fw) > 400:
            flo = sorted(fw)[int(0.05*(len(fw)-1))]
            e = [fees[i] is not None for i in range(n)]
            add('fees_low', 'Transaction fees in their lowest twentieth',
                'Fees as low as at previous bottoms mean the chain is as quiet as it gets.',
                'bottom', [bool(e[i] and fees[i] <= flo) for i in range(n)], e, '/tools/bitcoin-miners-monitor')

    # public attention at a multi-year low, and at an extreme
    if att:
        aw = [x for x in att if x is not None]
        if len(aw) > 300:
            alo = sorted(aw)[int(0.15*(len(aw)-1))]; ahi = sorted(aw)[int(0.90*(len(aw)-1))]
            e = [att[i] is not None for i in range(n)]
            add('attention_low', 'Public attention in its lowest sixth',
                'Nobody is looking: the condition that precedes a recovery.',
                'bottom', [bool(e[i] and att[i] <= alo) for i in range(n)], e, '/tools/bitcoin-market-context')
            add('attention_high', 'Public attention in its highest tenth',
                'Everybody is looking: the condition that marks a top.',
                'top', [bool(e[i] and att[i] >= ahi) for i in range(n)], e, '/tools/bitcoin-market-context')

    # ---- the site's own instruments ------------------------------------
    # The scorecard held fifteen external claims to a standard the site had
    # not applied to itself. These score the site's own composite and its
    # power-law percentile as rules, through the same engine, with the same
    # floor and the same interval. The cuts are the composite's own published
    # bands (10/30/70/90), fixed on the Where Things Stand page before this
    # test existed - but they were still chosen by us, after seeing history,
    # and the page says so.
    co = load('composite')
    if co and co.get('rows'):
        cols = co['columns']; ci_ = cols.index('composite'); pi_ = cols.index('powerlaw_pct')
        comp = {r[0]: r[ci_] for r in co['rows']}; plp = {r[0]: r[pi_] for r in co['rows']}
        cs = [comp.get(d) for d in dates]; ps = [plp.get(d) for d in dates]
        e = [cs[i] is not None for i in range(n)]
        add('site_composite_cheap', 'Our composite reads cheap (below 30)',
            'When our own valuation composite is in its cheap band, a doubling follows.',
            'bottom', [bool(e[i] and cs[i] < 30) for i in range(n)], e, '/tools/bitcoin-market-context')
        add('site_composite_dear', 'Our composite reads dear (above 70)',
            'When our own valuation composite is in its dear band, a 40% fall follows.',
            'top', [bool(e[i] and cs[i] > 70) for i in range(n)], e, '/tools/bitcoin-market-context')
        add('site_composite_floor', 'Our composite reads very cheap (below 10)',
            'When our composite is in its lowest band, a doubling follows.',
            'bottom', [bool(e[i] and cs[i] < 10) for i in range(n)], e, '/tools/bitcoin-market-context')
        add('site_composite_ceiling', 'Our composite reads very dear (above 90)',
            'When our composite is in its highest band, a 40% fall follows.',
            'top', [bool(e[i] and cs[i] > 90) for i in range(n)], e, '/tools/bitcoin-market-context')
        e = [ps[i] is not None for i in range(n)]
        add('site_powerlaw_low', 'Price in the power-law model\'s bottom fifth',
            'When price sits in the lowest fifth of its power-law deviation history, a doubling follows.',
            'bottom', [bool(e[i] and ps[i] < 20) for i in range(n)], e, '/tools/bitcoin-power-law-monitor')
        add('site_powerlaw_high', 'Price in the power-law model\'s top fifth',
            'When price sits in the highest fifth of its power-law deviation history, a 40% fall follows.',
            'top', [bool(e[i] and ps[i] > 80) for i in range(n)], e, '/tools/bitcoin-power-law-monitor')
        for x in RULES:
            if x['key'].startswith('site_'): x['own'] = True

    # ---- sentiment ----------------------------------------------------
    fgv = {d: v for d, v in series(fg, 'index')} if fg else {}
    fgs = [fgv.get(d) for d in dates]
    e = [fgs[i] is not None for i in range(n)]
    add('fear_extreme', 'Fear and Greed at 20 or below',
        'Extreme fear means the market has capitulated and it is time to buy.',
        'bottom', [bool(e[i] and fgs[i] <= 20) for i in range(n)], e,
        '/tools/bitcoin-flows-positioning-monitor')
    add('greed_extreme', 'Fear and Greed at 80 or above',
        'Extreme greed means the market is euphoric and due to fall.',
        'top', [bool(e[i] and fgs[i] >= 80) for i in range(n)], e,
        '/tools/bitcoin-flows-positioning-monitor')

    # ---- miner capitulation --------------------------------------------
    hr = {d: v for d, v in series(bc, 'hash_rate')}
    hrs = [hr.get(d) for d in dates]
    # Forward-fill, never zero-fill. A zero costs the 30-day mean 1/30 of its
    # level but the 60-day only 1/60, so a gap MANUFACTURES hr30 < hr60 - the
    # capitulation condition. Blockchain.com has an 11-day gap including one
    # 7-day stretch; zero-filling turned 4 genuine capitulation days into 21.
    # Both gaps currently sit in the ineligible tail, so today's scores are
    # unaffected, but the 2025 gap ages into the scoreable window next year.
    _f, _last = [], None
    for x in hrs:
        if x is not None:
            _last = x
        _f.append(_last if _last is not None else 0.0)
    hr30, hr60 = sma(_f, 30), sma(_f, 60)
    e = [hr30[i] is not None and hr60[i] is not None and hrs[i] is not None
         for i in range(n)]
    add('hash_ribbon', 'Hash ribbon capitulation',
        'When the 30-day hash rate falls below the 60-day, miners are '
        'capitulating and a bottom is near.',
        'bottom', [bool(e[i] and hr30[i] < hr60[i]) for i in range(n)], e,
        '/tools/bitcoin-miners-monitor')

    # ---- stock to flow --------------------------------------------------
    sup = {d: v for d, v in series(bc, 'supply')}
    sups = [sup.get(d) for d in dates]
    s2f = [None] * n
    for i in range(365, n):
        a, b = sups[i - 365], sups[i]
        if a and b and b > a:
            flow = b - a
            if flow > 0:
                sf_ratio = b / flow      # NOT `r`: that name holds the RSI series
                    # PlanB's published regression is on MARKET VALUE:
                # ln(mktcap) = 3.3*ln(SF) + 14.6. This uses his exponent with a
                # price-scale coefficient of 0.4, which is neither of his
                # published forms and sits well above both. The rule fires as
                # one long episode either way, so the verdict is unaffected -
                # but the model line drawn on the claim page is too high and
                # the coefficient should not be described as his.
                s2f[i] = 0.4 * (sf_ratio ** 3.3)
    e = [s2f[i] is not None for i in range(n)]
    add('below_s2f', 'Price below the Stock-to-Flow model',
        'Bitcoin trades below its scarcity-implied value and will revert to it.',
        'bottom', [bool(e[i] and px[i] < s2f[i]) for i in range(n)], e,
        '/tools/bitcoin-indicator-autopsy')

    # LPPL comes precomputed with its own pre-registered specification
    rb=(lp or {}).get('random_baseline') or {}
    if rb:
        RULES.append(dict(key='lppls',name='LPPLS bubble model',
            claim='A log-periodic power-law fit identifies a bubble approaching its critical time.',
            direction='top',episodes=rb.get('runs'),censored=0,
            hit_rate=100*float(rb.get('hit_rate_signal',0)),
            # This rule's interval is the range a RANDOM signal firing on the
            # same number of days produces, from 10,000 seeded draws - a
            # permutation test, which is the right instrument here and a
            # stronger one than Wilson. The claim page used to say "not
            # computed" for this row while the pipeline had already computed
            # something better.
            hit_rate_ci90=[round(100*float(rb['random_p5']),1), round(100*float(rb['random_p95']),1)]
                          if rb.get('random_p5') is not None else None,
            interval_kind='random-signal 90% range, 10,000 draws',
            baseline_rate=100*float(rb.get('hit_rate_all_days',0)),
            baseline_days=rb.get('eligible_days'),
            eligible_days=rb.get('eligible_days'),
            # what happened AFTER a top signal, measured against the bottom
            # rule's outcome: doubling within a year. A bubble signal should
            # be followed by doubling LESS often than baseline. It was not.
            doubling_after_signal=round(100*float(rb['doubling_rate_signal']),1) if rb.get('doubling_rate_signal') is not None else None,
            doubling_baseline=round(100*float(rb['doubling_rate_all_days']),1) if rb.get('doubling_rate_all_days') is not None else None,
            median_run_days=rb.get('median_run_days'), longest_run_days=rb.get('longest_run_days'),
            signal_days=rb.get('signal_days'),
            confidence_threshold=0.5,
            firing_today=(float((lp or {}).get('today',{}).get('pos') or 0) >= 0.5) if lp else None,
            last_fired=next((d for d,v in reversed((lp or {}).get('series',{}).get('lppls_pos',[])) if v is not None and float(v)>=0.5), None),
            as_of_day=((lp or {}).get('series',{}).get('lppls_pos') or [[None]])[-1][0],
            # the three evaluations the page could not previously report
            strict=_lppls_summary((lp or {}).get('random_baseline_strict')),
            negative=_lppls_summary((lp or {}).get('random_baseline_negative')),
            critical_time=(lp or {}).get('critical_time_test'),
            strict_spec_scored=bool((lp or {}).get('random_baseline_strict')) and 'error' not in ((lp or {}).get('random_baseline_strict') or {}),
            evaluation_type='random_block_signal_days',
            detail='/tools/bitcoin-indicator-autopsy',
            note=f"random-block p = {rb.get('p_random_at_least_observed')}"))

    for x in RULES:
        h,b=x.get('hit_rate'),x.get('baseline_rate')
        x['difference']=None if (h is None or b is None) else round(h-b,1)
        # Every rule names the statistical design it was scored under, so the
        # renderer never applies one design's wording to another's result.
        x.setdefault('evaluation_type', 'episode_binomial')
        # The verdict is now an inferential statement, not a point-estimate
        # gap. "Beats the baseline" requires the 90% interval on the hit rate
        # to sit entirely above the baseline rate; "Worse" entirely below. A
        # +5-point gap whose interval straddles the baseline is
        # "Indistinguishable" - which is what it is. This makes the page's
        # multiple-testing sentence ("one in ten clears its interval by
        # chance") literally true of the verdict it describes. The baseline
        # rate is from thousands of days and treated as known; the interval
        # is on the rule's own proportion, so this is a one-sided comparison,
        # conservative in the direction that protects against false claims.
        ci=x.get('hit_rate_ci90')
        x['interval_clears_baseline']=None if not (ci and b is not None) else ('above' if ci[0]>b else 'below' if ci[1]<b else 'no')
        if x.get('episodes',0) is None or x.get('episodes',0)<MIN_EPISODES:
            x['verdict']='Not enough episodes to score'
        elif x['difference'] is None: x['verdict']='Not scored'
        elif x['difference']>=5 and x['interval_clears_baseline']=='above': x['verdict']='Beats the baseline'
        elif x['difference']<=-5 and x['interval_clears_baseline']=='below': x['verdict']='Worse than the baseline'
        else: x['verdict']='Indistinguishable'
        for k in ('hit_rate','baseline_rate'):
            if x.get(k) is not None: x[k]=round(x[k],1)

    out=dict(schema_version='1.0',source='derived',generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
        as_of=dates[-1],price_points=n,horizon_days=HORIZON,cluster_days=CLUSTER,
        min_episodes=MIN_EPISODES,
        method=('Each rule is scored against the days on which it could have fired. The baseline '
                'excludes trigger days and the 90 days after each trigger, so it is not contaminated '
                'by the aftermath of the signal. Top rules are scored on a 40% fall within 365 days; '
                'bottom rules on a doubling within 365 days. Episodes whose window is incomplete are '
                'excluded and counted as censored. In-sample and descriptive: this measures what '
                'followed, not what will follow.'),
        rules=RULES)
    os.makedirs(DATA,exist_ok=True)
    tmp=f'{DATA}/scorecard.json.tmp'
    io.open(tmp,'w',encoding='utf-8').write(json.dumps(out,indent=1))
    os.replace(tmp,f'{DATA}/scorecard.json')   # atomic: a failed run cannot damage the last good file
    print(f"{'rule':34s} {'eps':>4} {'hit%':>6} {'base%':>6} {'diff':>6}  verdict")
    for x in RULES:
        print(f"{x['name'][:34]:34s} {str(x.get('episodes')):>4} {str(x.get('hit_rate')):>6} "
              f"{str(x.get('baseline_rate')):>6} {str(x.get('difference')):>6}  {x['verdict']}")
    return 0

if __name__=='__main__': sys.exit(main())
