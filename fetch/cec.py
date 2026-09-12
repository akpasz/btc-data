"""cec.py - the crypto-equity index across all six sleeves, and where it sits.

WHAT THIS IS FOR

One index covering the listed crypto economy - treasury companies, miners,
exchanges, infrastructure, stablecoin and payments, tokenization - weighted by
market value, with a stated position in its own range.

WHAT THE TREASURY WORK DID AND DID NOT GIVE US

The treasury module computed net asset value from filings, and that machinery
does NOT generalise: a miner has no treasury NAV, an exchange has no treasury
NAV, and asking for one is the wrong question. What carries over is the
universe and the materiality screen. Everything below weights by MARKET VALUE,
which is the only measure every sleeve shares.

THE HISTORY PROBLEM, STATED FIRST BECAUSE IT LIMITS THE ANSWER

Most constituents are recent. Several are 2025-26 pivots - a furniture company,
a medical-device company, a biopharma - that had no crypto business before, and
a cap-weighted index correctly admits them only from the date they qualify.

So the index has at most ONE CYCLE of history: the 2021 top, the 2022 trough,
the 2024-25 rise, and only from the few constituents that existed through it.
That supports "where this sits in its own range". It does NOT support "how far
we are from a top", because one cycle cannot establish where tops occur. The
output carries the sample size with the reading, and `position()` refuses to
express itself in cycle language.
"""
import io, json, math, os, sys, time, datetime as dt, urllib.request

SEC = 'https://data.sec.gov'
STOOQ = 'https://stooq.com/q/d/l/'
UA = os.environ.get('SEC_CONTACT', 'research contact@example.com')
# the SEC wants a contact string; a commercial endpoint wants a browser
BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
OUT = 'data'

SLEEVES = ('treasury', 'mining', 'exchange', 'infrastructure', 'stablecoin', 'tokenization')

RULES = {
    'version': 'CEC v0.1',
    'weight': 'market capitalisation, capped',
    'why_market_cap': (
        'The only measure every sleeve shares. Treasury NAV does not exist for a miner or an '
        'exchange, and revenue is not comparable across a company holding bitcoin and one '
        'selling custody.'),
    'float_adjustment': None,
    'why_no_float': (
        'Point-in-time free float is a commercial product and is not reconstructable from '
        'filings without judgment. This is therefore an ECONOMIC measure of the listed crypto '
        'economy, not an investable benchmark, and the difference is the §25 work that has not '
        'been done.'),
    'single_name_cap': 0.12,
    'sleeve_cap': 0.40,
    'why_sleeve_cap': (
        'Without it the treasury sleeve is the index. One constituent is most of that sleeve '
        'and the index would measure a single company holding bitcoin, which is a thing you '
        'can already buy directly.'),
    'min_market_cap_usd': 50_000_000,
    'min_days_listed': 60,
    'base_level': 100.0,
    # THE INDEX DOES NOT START UNTIL IT IS AN INDEX.
    #
    # The first version began the day two constituents qualified, so the base of
    # 100 was set by whichever two arrived first and they drove the whole level;
    # by the end there were 31. That is not an index, it is two stocks with
    # company arriving later, and every reading taken against that base inherits
    # the accident of who was early.
    'min_members_to_start': 10,
}


def _get(url, tries=4, pause=0.4, ua=None, timeout=120):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': ua or UA,
                                                       'Accept-Encoding': 'gzip, deflate'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get('Content-Encoding') == 'gzip':
                    import gzip
                    raw = gzip.decompress(raw)
                time.sleep(pause)
                return raw
        except Exception as e:
            last = e
            time.sleep(pause * 2)
    raise RuntimeError(str(last)[:110])


# EQUITY PRICES ARE THE WEAK LINK IN THIS WHOLE BUILD.
#
# Stooq was the first choice: free, keyless, a documented CSV download. It now
# answers with a JavaScript challenge page instead of data, which a CSV parser
# reads as zero rows - so the index reported "0 constituents, 55 skipped" and
# every skip said "no price history", which is true and useless.
#
# There is no free, keyless, stable source of daily equity history. Yahoo's
# chart endpoint is unofficial but widely used and needs no key; the others
# need registration and cap the daily calls below what 55 constituents require.
#
# So: a chain, tried in order, and the source that answered is RECORDED on
# every series. When this breaks again - and an unofficial endpoint will - the
# output will say which source failed rather than implying the company has no
# shares.
def prices(ticker, market='us'):
    """Daily closes, oldest first, as (rows, source). Empty rows with a source
    of None means every provider refused, which is a different fact from a
    company having no price history and must not look the same."""
    for name, fn in (('yahoo', _yahoo), ('stooq', _stooq)):
        try:
            rows = fn(ticker, market)
        except Exception:
            rows = []
        if len(rows) > 30:
            return rows, name
    return [], None


def _yahoo(ticker, market='us'):
    url = (f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}'
           f'?range=10y&interval=1d&events=div%2Csplit')
    d = json.loads(_get(url, ua=BROWSER_UA))
    res = ((d.get('chart') or {}).get('result') or [None])[0]
    if not res:
        return []
    ts = res.get('timestamp') or []
    # adjclose carries splits and dividends. A split not adjusted for turns a
    # 4-for-1 into a 75% crash in the index on a day nothing happened.
    q = ((res.get('indicators') or {}).get('adjclose') or [{}])[0].get('adjclose') \
        or ((res.get('indicators') or {}).get('quote') or [{}])[0].get('close') or []
    out = []
    for t, c in zip(ts, q):
        if c is None:
            continue
        out.append((dt.datetime.utcfromtimestamp(t).date().isoformat(), float(c)))
    return out


def _stooq(ticker, market='us'):
    raw = _get(f'{STOOQ}?s={ticker.lower()}.{market}&i=d').decode()
    if '<' in raw[:80]:
        return []                      # a challenge page, not a CSV
    out = []
    for line in raw.splitlines()[1:]:
        p = line.split(',')
        if len(p) < 5:
            continue
        try:
            out.append((p[0], float(p[4])))
        except ValueError:
            continue
    return out


class FetchFailed(Exception):
    """The request did not answer. Distinct from a company that reports nothing,
    which is the distinction the first version lost."""


def shares_series(cik):
    """Shares outstanding by FILING date, so market capitalisation before a
    filing uses the count the market actually knew."""
    # DO NOT SWALLOW THE FETCH FAILURE.
    #
    # This returned [] for any error, so a request that timed out looked
    # identical to a company that tags nothing. Strategy - the largest corporate
    # bitcoin treasury in the world - came back with "no shares-outstanding
    # series" while the treasury module read the same endpoint successfully.
    # Its companyfacts file is tens of megabytes and the request simply did not
    # finish.
    #
    # A failure that reads as a finding is the shape of nearly every bug in this
    # build, and this is the last place it was still doing it.
    try:
        raw = _get(f'{SEC}/api/xbrl/companyfacts/CIK{int(cik):010d}.json')
    except Exception as e:
        raise FetchFailed(f'companyfacts for CIK {cik}: {str(e)[:90]}')
    try:
        facts = json.loads(raw).get('facts')
    except Exception as e:
        raise FetchFailed(f'companyfacts for CIK {cik} is not JSON: {str(e)[:60]}')
    # SUM THE SHARE CLASSES, DO NOT PICK ONE.
    #
    # Robinhood has Class A, B and C. companyfacts flattens the dimensions, so
    # the classes arrive as separate facts on the same filing date, and taking
    # the last one seen returned whichever the iteration reached - for HOOD a
    # class with none outstanding, giving a MARKET CAP OF ZERO for a $100bn
    # company, which then sat in the index at weight zero and said nothing.
    #
    # Summing is right for a multi-class issuer and harmless for a single-class
    # one, which reports the same number once. Zeros are dropped rather than
    # summed: a class with no shares is not information about the company.
    # SUM WITHIN A CONCEPT, MAX ACROSS CONCEPTS.
    #
    # Summing everything tripled every company - BMNR from $14.5bn to $61.7bn,
    # RIOT from $8.1bn to $24.2bn - because the same total is reported under
    # BOTH dei:EntityCommonStockSharesOutstanding and
    # us-gaap:CommonStockSharesOutstanding, and across several period ends on
    # one filing date. Two concepts carrying the same number is ONE number.
    #
    # Within a single concept on a single period, several values are share
    # CLASSES and do sum: that is how Robinhood's A, B and C arrive, and taking
    # one of them gave a $100bn company a market cap of zero.
    #
    # companyfacts does not expose the class dimension, so these two cases
    # cannot be told apart directly. Summing within and maxing across is the
    # treatment that is right for both, and it is stated here because it is a
    # judgment rather than a reading.
    # THE CONCEPTS LARGE FILERS ACTUALLY USE.
    #
    # Strategy, Circle and Block tag NEITHER
    # dei:EntityCommonStockSharesOutstanding NOR
    # us-gaap:CommonStockSharesOutstanding. I picked those two names in the
    # first hour without checking what the biggest filers use, and it kept the
    # largest corporate bitcoin treasury in the world out of a crypto index.
    #
    # What they do tag is the weighted average share count. That is a DURATION
    # fact - an average over the period - not a count at an instant, so it lags
    # a company that issued heavily mid-quarter. In this universe that is not a
    # small caveat: treasury companies fund purchases by issuing stock, so the
    # average understates the current count for exactly the constituents that
    # dilute most.
    #
    # It is used as a FALLBACK, only where no instant count exists, and every
    # series records which concept produced it so an approximation is never
    # mistaken for a reading.
    by = {}
    for tax, name in (('dei', 'EntityCommonStockSharesOutstanding'),
                      ('us-gaap', 'CommonStockSharesOutstanding')):
        c = ((facts or {}).get(tax) or {}).get(name)
        for unit, rows in ((c or {}).get('units') or {}).items():
            if str(unit).lower() not in ('shares', 'share'):
                continue
            for r in rows:
                v = r.get('val')
                if not v or not r.get('filed'):
                    continue
                # (filing, concept, period) -> the classes reported there
                k = (r['filed'], f'{tax}:{name}', r.get('end') or '')
                # a SET, so an identical value repeated is counted once.
                # companyfacts carries the same figure across accessions
                # routinely; two share classes with exactly equal counts is
                # possible and rare, and would be understated here. That is the
                # safer error: understating a share count understates a market
                # cap, where overstating one lets a company dominate an index
                # it should not.
                by.setdefault(k, set()).add(float(v))
    # one total per (filing, concept): the latest period reported on it
    per_concept = {}
    for (filed, concept, end), vals in by.items():
        kk = (filed, concept)
        if kk not in per_concept or end > per_concept[kk][0]:
            per_concept[kk] = (end, sum(vals))
    out = {}
    for (filed, _concept), (_end, total) in per_concept.items():
        if total > 0:
            out[filed] = max(out.get(filed, 0.0), total)
    if out:
        return sorted(out.items())

    # nothing instant: fall back to the weighted average
    fb = {}
    c = ((facts or {}).get('us-gaap') or {}).get('WeightedAverageNumberOfSharesOutstandingBasic')
    for unit, rows in ((c or {}).get('units') or {}).items():
        if str(unit).lower() not in ('shares', 'share'):
            continue
        for r in rows:
            v, f = r.get('val'), r.get('filed')
            if not v or not f:
                continue
            # the longest period reported on a filing is the one covering it
            if f not in fb or float(v) > fb[f]:
                fb[f] = float(v)
    return sorted(fb.items())


def shares_basis(cik, facts=None):
    """Which concept a company's share count came from, so an approximation is
    labelled as one."""
    if facts is None:
        try:
            facts = json.loads(_get(f'{SEC}/api/xbrl/companyfacts/CIK{int(cik):010d}.json')).get('facts')
        except Exception:
            return 'unknown'
    for tax, name in (('dei', 'EntityCommonStockSharesOutstanding'),
                      ('us-gaap', 'CommonStockSharesOutstanding')):
        c = ((facts or {}).get(tax) or {}).get(name)
        for unit, rows in ((c or {}).get('units') or {}).items():
            if str(unit).lower() in ('shares', 'share') and any(r.get('val') for r in rows):
                return 'instant'
    return 'weighted average (approximate: lags mid-quarter issuance)'



# A SHARE COUNT GOES STALE, AND A STALE ONE IS WORSE THAN NONE.
#
# Robinhood's last share fact is from FEBRUARY 2022. It stopped tagging the
# concept, so the series ends at 232m - and the code carried that forward four
# years. 232m against today's ~$111 gives $25.8bn for a company nearer $100bn,
# which is not a small error and looked entirely plausible.
#
# Carrying a count forward is right across a quarter and wrong across years. A
# constituent whose newest count predates the price by more than this cannot be
# weighted, and saying so is better than weighting it on a number from another
# era.
MAX_SHARE_AGE_DAYS = 400


def market_caps(px, shares, max_age_days=MAX_SHARE_AGE_DAYS):
    """A market-capitalisation series from a price series and a step function of
    share counts.

    The share count in force on a day is the last one FILED on or before it -
    never the next one, which would price a company on information that had not
    been published.
    """
    if not px or not shares:
        return []
    out, si = [], 0
    for d, p in px:
        while si + 1 < len(shares) and shares[si + 1][0] <= d:
            si += 1
        if shares[si][0] > d:
            continue                       # nothing filed yet on this date
        if _age(shares[si][0], d) > max_age_days:
            continue                       # the count is from another era
        out.append((d, p * shares[si][1]))
    return out


def _age(a, b):
    try:
        return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days
    except Exception:
        return 0


# ------------------------------------------------------------------- index ---

def build(members, rules=None):
    """A capitalisation-weighted index level from per-constituent market-cap
    series, with a single-name cap and a sleeve cap.

    A constituent enters on the first day it has BOTH a market cap and enough
    listing history, and never before. Backfilling a company into a period when
    it was a furniture business would be the survivorship error §30 forbids, and
    it is easy to do by accident because the price series reaches back that far.
    """
    r = dict(RULES); r.update(rules or {})
    days = sorted({d for m in members for d, _ in m['caps']})
    if not days:
        return {'levels': [], 'note': 'no constituent has a market-cap series'}

    cap_at = [{d: c for d, c in m['caps']} for m in members]
    # the price series drives the return; the cap series drives the weight
    px_at = [{d: p for d, p in (m.get('px') or [])} for m in members]
    # A CONSTITUENT ENTERS WHEN IT BECAME A CRYPTO COMPANY, NOT WHEN IT LISTED.
    #
    # USBC has 2,514 days of price history because it was Cigar King Corp. AI
    # Financial was Appliance Recycling Centers. Admitting them from their
    # listing date put a cigar retailer in a crypto index in 2016 and started
    # the whole series four years before the sector existed in this form.
    #
    # I had written a test against exactly this and it passed, because it
    # checked SEASONING - had the company been listed long enough - rather than
    # QUALIFICATION, which is a different question with the same shape.
    #
    # `qualified_from` is the first date the company disclosed a crypto fact.
    # Where it is missing the constituent is admitted on seasoning alone and
    # the output says how many are in that position, because a company with no
    # known qualification date is a guess about when its history starts.
    seen, unqualified = [None] * len(members), []
    for i, m in enumerate(members):
        if not m['caps']:
            continue
        idx = min(r['min_days_listed'], len(m['caps']) - 1)
        seasoned = m['caps'][idx][0] if len(m['caps']) > idx else None
        q = m.get('qualified_from')
        if q:
            seen[i] = max(seasoned, q) if seasoned else q
        else:
            seen[i] = seasoned
            unqualified.append(m.get('ticker') or i)

    levels, prev_w, turnover, started = [], None, [], False
    level = r['base_level']
    prev_val = None
    for d in days:
        # a zero or missing capitalisation excludes rather than weighting at
        # nothing: a constituent silently carrying no weight is a bug that
        # looks like a small company
        live = [(i, cap_at[i][d]) for i in range(len(members))
                if d in cap_at[i] and seen[i] and d >= seen[i]
                and cap_at[i][d] and cap_at[i][d] >= r['min_market_cap_usd']]
        if not started:
            if len(live) < r['min_members_to_start']:
                continue
            started = True
        if len(live) < 2:
            continue
        w = _weights(live, members, r)
        val = sum(w[i] * cap_at[i][d] for i, _ in live)
        if prev_val is not None and prev_w is not None:
            # CHAIN-LINK ONLY ON CONSTITUENTS PRESENT ON BOTH DAYS.
            #
            # The first version kept a constituent in the numerator when it had
            # no price on the previous day and dropped it from the denominator.
            # These tickers do not share trading days - several are thin OTC
            # names that go days without a print - so the ratio was wrong on
            # most days and the level decayed to ZERO against a base of 100.
            #
            # A day-on-day return can only be measured on something that has a
            # price on both days. Anything else is comparing two different
            # baskets and calling the difference a return.
            # WEIGHTS COME FROM MARKET CAP. RETURNS COME FROM PRICE.
            #
            # Chain-linking on market capitalisation was the fundamental error
            # and it produced a range of 18 to 41,042 on a 1.8-year series.
            # These companies dilute enormously - a shell issuing from one
            # million shares to a hundred million moves its market cap a
            # hundredfold on the filing date - and the index read every
            # issuance as a gain.
            #
            # A share issuance is a CAPITAL CHANGE, not a return. A holder of
            # the stock did not make a hundred times their money that day; they
            # were diluted. Index arithmetic must capture what an investor
            # earned, which is the price return, weighted by how much of the
            # index each name represented yesterday.
            both = [i for i, _ in live
                    if i in prev_w and d in px_at[i] and pd in px_at[i]
                    and px_at[i][pd] > 0]
            if len(both) >= 2:
                ret = sum(prev_w[i] * (px_at[i][d] / px_at[i][pd]) for i in both)
                wsum = sum(prev_w[i] for i in both)
                if wsum > 0:
                    level *= ret / wsum
                turnover.append(sum(abs(w.get(i, 0) - prev_w.get(i, 0))
                                    for i in set(w) | set(prev_w)) / 2)
        levels.append({'date': d, 'level': round(level, 4), 'members': len(live)})
        prev_w, prev_val, pd = w, val, d

    return {'levels': levels, 'rules': r,
            'entered_on_seasoning_only': unqualified,
            'median_turnover': round(sorted(turnover)[len(turnover) // 2], 5) if turnover else None,
            'first': levels[0]['date'] if levels else None,
            'members_at_start': levels[0]['members'] if levels else None,
            'last': levels[-1]['date'] if levels else None}


def _weights(live, members, r):
    """Capitalisation weights, single-name capped, then sleeve capped, with the
    excess redistributed to the uncapped. Order-preserving: a cap that lifts a
    smaller holding above a larger one is not limiting concentration, it is
    inventing a ranking."""
    tot = sum(c for _, c in live) or 1.0
    w = {i: c / tot for i, c in live}
    n = len(w)
    if n * r['single_name_cap'] >= 1.0:
        for _ in range(200):
            over = [i for i in w if w[i] > r['single_name_cap'] + 1e-12]
            if not over:
                break
            excess = sum(w[i] - r['single_name_cap'] for i in over)
            for i in over:
                w[i] = r['single_name_cap']
            free = [i for i in w if w[i] < r['single_name_cap'] - 1e-12]
            pool = sum(w[i] for i in free)
            if not free or pool <= 0:
                break
            share = {i: w[i] / pool for i in free}
            for i in free:
                w[i] += excess * share[i]
    # SLEEVE CAP, AND WHETHER IT CAN BIND AT ALL.
    #
    # The same infeasibility as a top-five cap on a small index, in a new place.
    # With TWO sleeves a 40% cap is unreachable - two times forty is eighty, not
    # a hundred - so capping the larger pushes its excess into the smaller and
    # lands that one above the cap instead. The first version did exactly that
    # and produced a 60% sleeve while claiming a 40% limit.
    #
    # A cap that cannot be met is skipped and recorded, never forced. Which
    # means the sleeve cap only starts binding once the index covers three
    # sleeves, and until then the output has to say the index is concentrated
    # rather than pretend it is not.
    by = {}
    for i in w:
        by.setdefault(members[i].get('sleeve') or 'unclassified', []).append(i)
    if len(by) * r['sleeve_cap'] < 1.0 - 1e-9:
        t = sum(w.values()) or 1.0
        return {i: v / t for i, v in w.items()}
    for sl, idxs in by.items():
        s = sum(w[i] for i in idxs)
        if s <= r['sleeve_cap'] + 1e-12 or len(by) < 2:
            continue
        others = [i for i in w if i not in idxs]
        pool = sum(w[i] for i in others)
        if pool <= 0:
            continue
        freed = s - r['sleeve_cap']
        for i in idxs:
            w[i] *= r['sleeve_cap'] / s
        for i in others:
            w[i] += freed * (w[i] / pool)
    t = sum(w.values()) or 1.0
    return {i: v / t for i, v in w.items()}


# ---------------------------------------------------------------- position ---

def position(levels, bitcoin=None):
    """Where the index sits in its own range, with the sample that supports it.

    DELIBERATELY NOT EXPRESSED IN CYCLE LANGUAGE. Most constituents are recent,
    so the series covers about one cycle. One cycle cannot establish where tops
    occur, and "60% of the way to a peak" would be a claim about a distribution
    observed once. "The 60th percentile of its own history" is a description,
    and it is the strongest statement the data supports.
    """
    if len(levels) < 30:
        return {'reading': None, 'why': 'fewer than thirty published days'}
    vals = [x['level'] for x in levels]
    now = vals[-1]
    lo, hi = min(vals), max(vals)
    pct = 100.0 * sum(1 for v in vals if v <= now) / len(vals)
    span_days = _days(levels[0]['date'], levels[-1]['date'])
    out = {
        'level': round(now, 2),
        'percentile_of_own_history': round(pct, 1),
        'range_low': round(lo, 2), 'range_high': round(hi, 2),
        'drawdown_from_high_pct': round(100 * (now / hi - 1), 1) if hi else None,
        'history_days': span_days,
        'history_years': round(span_days / 365.25, 2),
        'sample_warning': (
            f'This is a position in a {span_days / 365.25:.1f}-year history, which spans about '
            f'one bitcoin cycle. A percentile describes where the index sits among its own '
            f'published days. It is NOT a statement about how far a cycle has to run: one cycle '
            f'cannot establish where tops occur, and any reading phrased that way would be a '
            f'claim about a distribution observed once.'),
    }
    if bitcoin:
        bt = {d: v for d, v in bitcoin}
        pairs = [(x['date'], x['level']) for x in levels if x['date'] in bt]
        if len(pairs) > 30:
            ratio = [(d, l / bt[d]) for d, l in pairs if bt[d]]
            rv = [v for _, v in ratio]
            rnow = rv[-1]
            out['vs_bitcoin'] = {
                'ratio_percentile': round(100.0 * sum(1 for v in rv if v <= rnow) / len(rv), 1),
                'note': ('the index against bitcoin itself. Equities leading or lagging the asset '
                         'is a different question from either being high, and the ratio answers '
                         'it without either series having to be at an extreme.'),
            }
    return out


def _days(a, b):
    import datetime as dt
    try:
        return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days
    except Exception:
        return 0
