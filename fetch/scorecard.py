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
import io,json,os,sys,math,urllib.request,datetime

DATA=os.environ.get('DATA_DIR','data')
BASES=['https://akpasz.github.io/btc-data/data/',
       'https://raw.githubusercontent.com/akpasz/btc-data/main/data/']
LOCAL=os.environ.get('SNAP_DIR')
MIN_EPISODES=20
HORIZON=365
CLUSTER=90          # days after a trigger treated as the same episode

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
    hit=tot=cens=0
    for s,_ in eps:
        o=outcome(s)
        if o is None: cens+=1; continue
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
    return dict(episodes=tot,censored=cens,
        hit_rate=(100*hit/tot) if tot else None,
        baseline_rate=(100*bh/bt) if bt else None,
        baseline_days=bt,
        eligible_days=sum(1 for i in range(n) if eligible[i]))

def main():
    bc=load('blockchain'); cm=load('coinmetrics'); lp=load('lppls')
    if not bc: print('no blockchain.json'); return 1
    pr=[(d,v) for d,v in series(bc,'price') if v>0]
    dates=[d for d,_ in pr]; px=[v for _,v in pr]; n=len(px)
    ma50,ma111,ma200,ma350=sma(px,50),sma(px,111),sma(px,200),sma(px,350)
    ma200w,ma2y=sma(px,1400),sma(px,730)
    r=rsi(px,14)
    mv={d:v for d,v in series(cm,'CapMVRVCur')}
    mvrv=[mv.get(d) for d in dates]

    def el(*arrs):
        return [all(a[i] is not None for a in arrs) and i+HORIZON<n for i in range(n)]

    RULES=[]
    def add(key,name,claim,direction,fires,eligible,detail):
        s=score(dates,px,fires,eligible,direction)
        s.update(key=key,name=name,claim=claim,direction=direction,detail=detail)
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
    e=[r[i] is not None and i+HORIZON<n for i in range(n)]
    add('rsi_hot','RSI-14 above 70','An overbought RSI precedes a fall.',
        'top',[bool(e[i] and r[i]>70) for i in range(n)],e,'/tools/bitcoin-technical-signals')
    add('rsi_cold','RSI-14 below 30','An oversold RSI precedes a rally.',
        'bottom',[bool(e[i] and r[i]<30) for i in range(n)],e,'/tools/bitcoin-technical-signals')
    e=[mvrv[i] is not None and i+HORIZON<n for i in range(n)]
    add('mvrv_high','MVRV above 3.7','An MVRV above 3.7 marks the top of the cycle.',
        'top',[bool(e[i] and mvrv[i]>3.7) for i in range(n)],e,'/tools/bitcoin-realised-value-monitor')
    add('mvrv_low','MVRV below 1','An MVRV below 1 means the average holder is under water: a bottom.',
        'bottom',[bool(e[i] and mvrv[i]<1.0) for i in range(n)],e,'/tools/bitcoin-realised-value-monitor')

    # LPPL comes precomputed with its own pre-registered specification
    rb=(lp or {}).get('random_baseline') or {}
    if rb:
        RULES.append(dict(key='lppls',name='LPPLS bubble model',
            claim='A log-periodic power-law fit identifies a bubble approaching its critical time.',
            direction='top',episodes=rb.get('runs'),censored=0,
            hit_rate=100*float(rb.get('hit_rate_signal',0)),
            baseline_rate=100*float(rb.get('hit_rate_all_days',0)),
            baseline_days=rb.get('eligible_days'),
            eligible_days=rb.get('eligible_days'),
            detail='/tools/bitcoin-indicator-autopsy',
            note=f"random-block p = {rb.get('p_random_at_least_observed')}"))

    for x in RULES:
        h,b=x.get('hit_rate'),x.get('baseline_rate')
        x['difference']=None if (h is None or b is None) else round(h-b,1)
        if x.get('episodes',0) is None or x.get('episodes',0)<MIN_EPISODES:
            x['verdict']='Not enough episodes to score'
        elif x['difference'] is None: x['verdict']='Not scored'
        elif x['difference']>=5: x['verdict']='Beats the baseline'
        elif x['difference']<=-5: x['verdict']='Worse than the baseline'
        else: x['verdict']='Indistinguishable'
        for k in ('hit_rate','baseline_rate'):
            if x.get(k) is not None: x[k]=round(x[k],1)

    out=dict(source='derived',generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
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
