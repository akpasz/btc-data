"""cec_run.py - universe to index to position, in one command.

    python fetch/cec_run.py

Chains what already exists: the 412-candidate universe, the materiality screen,
a sleeve for every material company, market-cap history per constituent, and
the weighted index with its position. Writes data/cec.json.

THE CLASSIFICATION RULE, WHICH WAS THE BLOCKER

§9 requires exactly one primary sleeve per company, and the screen deliberately
refuses to guess when the text signals conflict - a company matching both
"bitcoin mining" and "digital asset exchange" gets neither. That refusal was
correct for the screen and it stalled the index, because roughly half the
material companies match more than one phrase.

The answer is not a judgment call per company. It is a PRECEDENCE ORDER, stated
once and applied mechanically:

    1. Balance sheet first. Crypto assets at half or more of total assets makes
       a company a treasury vehicle whatever its text says, because that is what
       its shareholders own.

    2. Then the most SPECIFIC business term wins. "bitcoin mining" describes one
       activity; "blockchain infrastructure" describes anything. So mining beats
       exchange beats stablecoin beats tokenization beats infrastructure, and
       infrastructure is the catch-all rather than a peer.

Every assignment records which rule produced it, so a wrong one is visible and
correctable in the registry rather than buried in a score.
"""
import io, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cec as C
import materiality as M

OUT = 'data'

# Most specific first. Infrastructure last because "blockchain infrastructure"
# and "digital asset custody" are the vaguest phrases in the search set and
# would otherwise swallow companies that are plainly something else.
PRECEDENCE = ('mining', 'exchange', 'stablecoin', 'tokenization', 'infrastructure')
SLEEVE_FILE = 'src/cec/sleeves.json'
CONSTITUENT_FILE = 'src/cec/constituents.json'
TREASURY_SHARE = 0.50


def load_sleeves(path=None):
    # The hand-written classification: {cik: sleeve}. Absent is fine; the run
    # then reports how many constituents are on a provisional guess.
    try:
        d = json.load(io.open(path or SLEEVE_FILE, encoding='utf-8'))
    except Exception:
        return {}
    return {int(k): v for k, v in (d.get('sleeves') or {}).items() if v}


def sleeve_for(row, assigned=None):
    # Returns (sleeve, why, confirmed). `confirmed` is False where the sleeve
    # was GUESSED from text, because a provisional classification that looks
    # identical to a decided one is how BitGo ends up in a mining sleeve and
    # nobody notices.
    cik = row.get('cik')
    if assigned and cik in assigned:
        return assigned[cik], 'assigned in the classification file', True
    share = row.get('crypto_asset_share')
    if share is not None and share >= TREASURY_SHARE:
        # measured, not described
        return 'treasury', f'crypto assets are {share:.0%} of the balance sheet', True
    sig = [x for x in (row.get('sleeve_signals') or []) if x != 'treasury']
    for x in PRECEDENCE:
        if x in sig:
            return x, (f'PROVISIONAL: guessed from {len(sig)} text signal(s) '
                       f'({", ".join(sorted(sig))}) and not confirmed'), False
    if share is not None and share > 0:
        return 'treasury', f'crypto assets are {share:.0%}, no business signal', True
    return None, 'no balance-sheet share and no business signal', False

def ticker_for(row):
    ts = row.get('tickers') or []
    return ts[0] if ts else None


def load_constituents(path=None):
    """The curated list: {ticker: {sleeve, name, note}} plus the file's own
    metadata, so the run can report the rules it was built under."""
    try:
        d = json.load(io.open(path or CONSTITUENT_FILE, encoding='utf-8'))
    except Exception:
        return {}, {}
    return (d.get('constituents') or {}), d


def cik_for(ticker, cache={}):
    """Resolve a ticker to a CIK from the SEC's own file, fetched once."""
    if not cache:
        try:
            raw = C._get('https://www.sec.gov/files/company_tickers.json')
            for _k, v in json.loads(raw).items():
                cache[str(v['ticker']).upper()] = int(v['cik_str'])
        except Exception:
            cache['_failed'] = True
    return cache.get(ticker.upper())


def main(out_dir=OUT, limit=0):
    # THE INDEX IS BUILT FROM THE CURATED LIST, NOT THE SCREEN.
    #
    # Two automated screens were built and both selected the wrong universe. The
    # balance-sheet test measures what proportion of a company IS a pile of
    # coins, so Coinbase scored 5.5% and a $2m shell scored 79%. The text test
    # measures how broadly a filing discusses crypto, so Coinbase matched four
    # phrases and that shell matched five.
    #
    # The screen still runs and still matters: it produces the 412-candidate
    # universe, which is the rule-based part and the reason nothing can be
    # omitted without having been considered. What it cannot do is decide which
    # candidate is a crypto company, because nothing the SEC publishes encodes
    # that.
    cons, meta = load_constituents()
    if not cons:
        raise SystemExit(
            f'no constituent file at {CONSTITUENT_FILE}. The index is built from a curated '
            f'list rather than the screen - the file explains why.')
    print(f'  {len(cons)} constituents from {CONSTITUENT_FILE}')
    ai = (meta.get('ai_rule') or {}).get('excluded_on_this_rule') or []
    if ai:
        print(f'  AI rule: {", ".join(ai)} excluded - crypto is no longer material to them')

    mat = json.load(io.open(f'{out_dir}/crypto_materiality.json', encoding='utf-8'))
    by_cik = {r.get('cik'): r for r in (mat.get('rows') or [])}
    rows = []
    for tk, info in cons.items():
        cik = cik_for(tk)
        if not cik:
            rows.append({'ticker': tk, 'name': info.get('name'), 'no_cik': True,
                         'sleeve_override': info.get('sleeve')})
            continue
        r = dict(by_cik.get(cik) or {})
        r.update({'cik': cik, 'tickers': [tk],
                  'current_name': info.get('name') or r.get('current_name'),
                  'sleeve_override': info.get('sleeve')})
        rows.append(r)

    members, skipped, sources = [], [], {}
    for r in (rows[:limit] if limit else rows):
        if r.get('no_cik'):
            skipped.append({'ticker': r['ticker'], 'name': r.get('name'),
                            'reason': 'ticker not found in the SEC company file'})
            continue
        sl, why, confirmed = r['sleeve_override'], 'assigned in the constituent file', True
        tk = ticker_for(r)
        if not sl:
            skipped.append({**_thin(r), 'reason': why}); continue
        if not tk:
            skipped.append({**_thin(r), 'reason': 'no ticker in the SEC submissions record'})
            continue
        px, src = C.prices(tk)
        if not px:
            skipped.append({**_thin(r), 'sleeve': sl, 'ticker': tk,
                            'reason': f'no provider returned a price series for {tk}'})
            continue
        sources[src] = sources.get(src, 0) + 1
        try:
            sh = C.shares_series(r['cik'])
        except C.FetchFailed as e:
            skipped.append({**_thin(r), 'sleeve': sl, 'ticker': tk, 'fetch_failed': True,
                            'reason': f'THE REQUEST FAILED, which is not a fact about the '
                                      f'company: {e}'})
            continue
        if not sh:
            skipped.append({**_thin(r), 'sleeve': sl, 'ticker': tk,
                            'reason': ('no shares-outstanding series: the filer tags neither '
                                       'dei:EntityCommonStockSharesOutstanding nor '
                                       'us-gaap:CommonStockSharesOutstanding with a non-zero '
                                       'value')}); continue
        caps = C.market_caps(px, sh)
        if caps and not caps[-1][1]:
            skipped.append({**_thin(r), 'sleeve': sl, 'ticker': tk,
                            'reason': ('market capitalisation is zero - the share count could '
                                       'not be read, which is a data failure and not a fact '
                                       'about the company')})
            continue
        if not caps:
            last_sh = sh[-1][0] if sh else None
            skipped.append({**_thin(r), 'sleeve': sl, 'ticker': tk,
                            'reason': (
                                f'the newest share count is from {last_sh} and the price runs to '
                                f'{px[-1][0]}, so every day would be weighted on a count from '
                                f'another era. The filer appears to have stopped tagging the '
                                f'concept.' if last_sh else
                                'price and share series do not overlap')})
            continue
        if caps[-1][0] < px[-1][0]:
            # the dates that produced the decision, so a wrong rule is visible
            # rather than inferred. I guessed at this one twice and was wrong
            # both times.
            skipped.append({**_thin(r), 'sleeve': sl, 'ticker': tk,
                            'price_first': px[0][0], 'price_last': px[-1][0],
                            'shares_first': sh[0][0], 'shares_last': sh[-1][0],
                            'shares_n': len(sh), 'caps_last': caps[-1][0],
                            'reason': (
                                f'priced to {px[-1][0]} but the capitalisation series stops at '
                                f'{caps[-1][0]}; newest share filing {sh[-1][0]}')})
            continue
        members.append({'cik': r['cik'], 'ticker': tk,
                        'name': r.get('current_name') or r.get('name'),
                        'sleeve': sl, 'sleeve_reason': why, 'sleeve_confirmed': confirmed,
                        'crypto_asset_share': r.get('crypto_asset_share'),
                        'renamed': r.get('renamed'), 'caps': caps,
                        'latest_cap_usd': caps[-1][1], 'first_cap_date': caps[0][0],
                        'qualified_from': r.get('crypto_first_filed'),
                        'px': px,
                        'price_source': src,
                        'shares_basis': C.shares_basis(r['cik'])})
        print(f'    {tk:<8}{sl:<16}{len(caps):>5} days  {r.get("current_name") or ""}'[:86])

    ix = C.build(members)
    btc = _btc(out_dir)
    pos = C.position(ix.get('levels') or [], btc)

    by = {}
    for m in members:
        by.setdefault(m['sleeve'], []).append(m['ticker'])

    out = {'schema_version': 1, 'rules': C.RULES,
           'classification': {'precedence': list(PRECEDENCE),
                              'treasury_share_threshold': TREASURY_SHARE,
                              'note': ('a precedence order applied mechanically, never a '
                                       'judgment per company; each assignment records the rule '
                                       'that produced it')},
           'members': [{k: v for k, v in m.items() if k not in ('caps', 'px')}
                       for m in members],
           'sleeves': {k: sorted(v) for k, v in by.items()},
           'skipped': skipped, 'price_sources': sources,
           'index': {k: v for k, v in ix.items() if k != 'levels'},
           'levels': ix.get('levels') or [],
           'position': pos}
    os.makedirs(out_dir, exist_ok=True)
    io.open(f'{out_dir}/cec.json', 'w', encoding='utf-8').write(json.dumps(out))

    print()
    prov = [m['ticker'] for m in members if not m.get('sleeve_confirmed')]
    out['provisional_sleeves'] = prov
    io.open(f'{out_dir}/cec.json', 'w', encoding='utf-8').write(json.dumps(out))
    print(f'  prices from: ' + (', '.join(f'{k} {v}' for k, v in sources.items())
                                 if sources else 'NO PROVIDER ANSWERED'))
    print(f'  CEC: {len(members)} constituents across {len(by)} sleeves, '
          f'{len(skipped)} skipped')
    failed = [s2['ticker'] for s2 in skipped if s2.get('fetch_failed')]
    if failed:
        print(f'  !! {len(failed)} constituent(s) were skipped because A REQUEST FAILED, not '
              f'because of anything about them:')
        print(f'     {", ".join(str(x) for x in failed)}')
        print(f'     Re-run before reading the index: these are missing constituents, not '
              f'absent ones.')
    nopx = [s2 for s2 in skipped if 'no provider' in (s2.get('reason') or '')]
    if len(nopx) > len(members):
        print(f'  !! {len(nopx)} constituents have NO PRICE SERIES. That is a provider failure,')
        print(f'     not a fact about the companies - check one by hand before believing it.')
    if prov:
        print(f'  !! {len(prov)} of {len(members)} sleeves are PROVISIONAL - guessed from text, '
              f'not confirmed:')
        print(f'     {", ".join(prov)}')
        print(f'     Text search finds the universe; it cannot classify it. "bitcoin mining"')
        print(f'     appears in nearly every crypto 10-K, so a text guess made BitGo a miner.')
        print(f'     Write {SLEEVE_FILE} with one line per company to settle these.')
    for k in sorted(by, key=lambda k: -len(by[k])):
        print(f'    {len(by[k]):>3}  {k:<16}{", ".join(by[k][:8])}')
    approx = [m['ticker'] for m in members if 'weighted' in (m.get('shares_basis') or '')]
    if approx:
        print(f'  {len(approx)} constituent(s) use a WEIGHTED AVERAGE share count because they '
              f'tag no instant one:')
        print(f'     {", ".join(approx)}')
        print(f'     It lags mid-quarter issuance, which understates exactly the companies that '
              f'dilute most.')
    never = ix.get('never_qualified') or []
    if never:
        print(f'  {len(never)} listed constituent(s) NEVER cleared the floors and are not in '
              f'the index on any day:')
        print(f'     {", ".join(str(x) for x in never)}')
        print(f'     Usually a market value below the ${C.RULES["min_market_cap_usd"]:,} floor, '
              f'which for a recent listing often means an unusable share count.')
    seas = ix.get('entered_on_seasoning_only') or []
    if seas:
        print(f'  {len(seas)} constituent(s) have no known qualification date and enter on '
              f'listing alone:')
        print(f'     {", ".join(str(x) for x in seas[:14])}')
    lv = ix.get('levels') or []
    if lv:
        print()
        print(f'  index: {len(lv)} days, {lv[0]["date"]} to {lv[-1]["date"]}, '
              f'level {lv[-1]["level"]:.1f} from a base of {C.RULES["base_level"]:.0f}')
    if pos.get('percentile_of_own_history') is not None:
        print(f'  POSITION: {pos["percentile_of_own_history"]:.0f}th percentile of its own '
              f'{pos["history_years"]:.1f}-year history')
        print(f'            {pos["drawdown_from_high_pct"]:+.0f}% from its high, '
              f'range {pos["range_low"]:.0f} to {pos["range_high"]:.0f}')
        if pos.get('vs_bitcoin'):
            print(f'            against bitcoin: '
                  f'{pos["vs_bitcoin"]["ratio_percentile"]:.0f}th percentile of that ratio')
        print()
        print('  ' + pos['sample_warning'][:150])
    else:
        print(f'  POSITION: none - {pos.get("why")}')
    return out


def _thin(r):
    # THE TICKER. Six skipped rows printed blank because this never carried it,
    # so the output could not say which companies were missing - and one of them
    # was Strategy, the largest corporate bitcoin treasury in the world.
    return {'cik': r.get('cik'),
            'ticker': (r.get('tickers') or [r.get('ticker')])[0] if (r.get('tickers')
                                                                     or r.get('ticker')) else None,
            'name': r.get('current_name') or r.get('name'),
            'crypto_asset_share': r.get('crypto_asset_share')}


def _btc(out_dir):
    """Bitcoin itself, for the ratio. Missing is fine: the ratio is a second
    reading, not the reading."""
    for f in ('series/price.json', 'relative.json'):
        try:
            d = json.load(io.open(f'{out_dir}/{f}', encoding='utf-8'))
        except Exception:
            continue
        v = d.get('values') or ((d.get('series') or {}).get('btc_usd'))
        if v:
            return [(a, float(b)) for a, b in v if b]
    return None


if __name__ == '__main__':
    sys.exit(0 if main() else 1)
