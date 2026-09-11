"""treasury.py - point-in-time digital-asset treasury economics for CTI.

WHAT THIS IS

Step 1 of the CEC programme: the part the Phase 0 gate found can be built
properly today, because SEC XBRL returns every reported fact together with the
date it was FILED. That is the point-in-time property the specification's §29
requires - what was known, and when it became knowable - and it is available
free for US filers.

THREE DESIGN DECISIONS, EACH LOAD-BEARING

1. POINT-IN-TIME MEANS FILING DATE, NEVER PERIOD END.
   A 10-Q for the quarter ended 30 June is filed in August. On 1 July the
   market did not know the 30 June holding. Every series here is indexed by
   `filed`, and `end` is carried alongside as the period the fact describes.
   Using `end` as the as-of date is the single easiest way to build a
   look-ahead bias into a treasury backtest, and it looks completely
   reasonable while you do it.

2. LIABILITY CLASSIFICATION IS A SOURCED HUMAN FILE, NOT CODE.
   §15 requires each claim to be classified treasury-attributed, operating,
   recourse, non-recourse or ambiguous. No XBRL element carries that. Code
   that infers it from element names is guessing, and a wrong guess produces
   a net NAV that is precisely wrong rather than honestly uncertain. So the
   classification lives in a registry file where every entry cites the filing
   it came from, and this module refuses to net a claim it has no
   classification for.

3. AMBIGUOUS CLAIMS PRODUCE A BAND, NOT A NUMBER.
   Where a claim's attribution is genuinely arguable - a convertible whose
   proceeds funded both treasury purchases and operations - the honest output
   is a net NAV bounded by both treatments. A single point estimate hides the
   disagreement; the band shows its size. mNAV inherits the band.

WHAT IT DOES NOT DO

Non-SEC issuers. Metaplanet files to the Tokyo exchange, not EDGAR. Those
need a per-issuer adapter and carry a lower data-confidence score; they belong
in CTI-Economic, not CTI-Investable, until an adapter exists and is tested.
This module records them as `source: manual` and will not silently invent
their figures.
"""
import io, json, os, sys, datetime as dt, urllib.request

from rules import is_fund, fund_reason

SEC = 'https://data.sec.gov'
UA = os.environ.get('SEC_CONTACT', 'research contact@example.com')
OUT = 'data'

# Elements that carry a token count. The label is frequently null on
# CryptoAssetNumberOfUnits and its unit is the token name rather than a
# standard unit, so an extractor that filters on the label finds nothing -
# a false negative that reads exactly like "this company holds no crypto".
# That fault shipped once and hid a 700,000 BTC position for months.
UNIT_CONCEPTS = (
    'CryptoAssetNumberOfUnits',           # ASU 2023-08, the standard since 2024
    'InvestmentOwnedBalanceShares',       # used by several trusts
    'InvestmentOwnedBalanceContracts',
)
SHARE_CONCEPTS = (
    ('dei', 'EntityCommonStockSharesOutstanding'),   # cover page, most timely
    ('us-gaap', 'CommonStockSharesOutstanding'),
)
DILUTED_CONCEPTS = (
    ('us-gaap', 'WeightedAverageNumberOfDilutedSharesOutstanding'),
)
FORMS = ('10-Q', '10-K', '10-Q/A', '10-K/A', '8-K', '20-F', '40-F')


def _get(url, tries=3):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA,
                                                       'Accept-Encoding': 'gzip, deflate'})
            with urllib.request.urlopen(req, timeout=45) as r:
                raw = r.read()
                if r.headers.get('Content-Encoding') == 'gzip':
                    import gzip
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode())
        except Exception as e:
            last = e
    raise RuntimeError(f'{url}: {last}')


# ------------------------------------------------------------------ extract ---

def token_series(facts, token, declared=False):
    """Every reported holding of one token, indexed by FILING date.

    Returns [{filed, end, units, form, accn}] sorted by filed. Where two
    filings report the same period end, the later filing wins: a 10-K restates
    the Q4 instant, and an amendment supersedes the original.
    """
    out = {}
    for taxonomy, concepts in (facts or {}).items():
        for name, c in concepts.items():
            if name not in UNIT_CONCEPTS:
                continue
            for unit, rows in (c.get('units') or {}).items():
                # the unit IS the token name on the standard element: "Bitcoin",
                # "BTC", "Ethereum", "ETH". Matching on it is how one filer's
                # multi-token balance sheet is separated.
                # Some filers tag the unit as the token name ("Bitcoin"), which
                # identifies it. Others tag it generically - BMNR uses "Integer"
                # and "cryptoAsset" - and companyfacts FLATTENS AWAY the
                # dimensional members that would say which token it is. So a
                # generic unit is accepted only where the registry declares the
                # token with a source, and it is reconciled against the
                # reported fair value below.
                generic = str(unit).strip().lower() in GENERIC_UNITS
                if not generic and not _unit_is(unit, token):
                    continue
                if generic and not declared:
                    continue
                for r in rows:
                    if r.get('val') is None or not r.get('end') or not r.get('filed'):
                        continue
                    if r.get('start'):            # duration facts are not balances
                        continue
                    if r.get('form') not in FORMS:
                        continue
                    # KEY ON (end, value), AND KEEP THE EARLIEST FILING.
                    #
                    # "Latest filing wins" is right for the value and wrong for
                    # the date it became knowable, and the difference is a real
                    # point-in-time error. MSTR first reported its 2025-12-31
                    # balance in the 10-K filed 2026-02-19; two later 10-Qs
                    # restated the identical figure. Keying on `end` alone and
                    # taking the latest filing moved that fact to August, so a
                    # series asked what was known in February answered with the
                    # 2024 balance - lagging reality by two quarters.
                    #
                    # Keying on the value too means a genuine RESTATEMENT to a
                    # different number becomes its own entry, knowable from the
                    # date it was actually filed. A repetition of the same
                    # number does not.
                    k = (r['end'], float(r['val']))
                    prev = out.get(k)
                    if prev is None or r['filed'] < prev['filed']:
                        out[k] = {'filed': r['filed'], 'end': r['end'],
                                  'units': float(r['val']), 'form': r.get('form'),
                                  'accn': r.get('accn'), 'concept': f'{taxonomy}:{name}',
                                  'xbrl_unit': unit, 'token_from': 'registry' if generic else 'xbrl unit'}
    return sorted(out.values(), key=lambda r: (r['filed'], r['end']))


def diagnose(facts, tokens):
    """When nothing is found, say what IS there. A silent zero reads exactly
    like "this company holds no crypto", which is the most dangerous failure in
    a materiality screen: a large holder is excluded and the exclusion looks
    like a fact. Ported from edgar.py, where the same fault cost months."""
    seen, units_seen = [], set()
    for taxonomy, concepts in (facts or {}).items():
        for name, c in concepts.items():
            us = [u for u in (c.get('units') or {}) if str(u).lower() not in
                  ('usd', 'shares', 'share', 'usd/shares', 'pure')]
            if not us:
                continue
            units_seen |= set(us)
            rows = [r for u in us for r in (c['units'][u] or [])]
            ends = sorted({r.get('end') for r in rows if r.get('end')})
            seen.append(f"{taxonomy}:{name} units={','.join(us)} n={len(rows)} "
                        f"last={ends[-1] if ends else '-'}")
    return {'looked_for_tokens': tokens,
            'recognised_concepts': list(UNIT_CONCEPTS),
            'non_dollar_units_present': sorted(units_seen),
            'non_dollar_concepts': seen[:25],
            'likely_cause': (
                'no non-dollar concept at all: the issuer may not tag a unit count, or may '
                'report only a USD carrying value'
                if not seen else
                'a unit string this module does not map to a token, or an element outside '
                'UNIT_CONCEPTS. Compare non_dollar_units_present with the token aliases.')}


def _unit_is(unit, token):
    u = str(unit).strip().lower().replace('-', '').replace('_', '')
    aliases = {'btc': ('btc', 'bitcoin', 'bitcoins'),
               'eth': ('eth', 'ethereum', 'ether'),
               'sol': ('sol', 'solana')}
    return u in aliases.get(token.lower(), (token.lower(),))


# Unit strings that carry no token identity. A filer using one of these has not
# said which asset the count refers to, and companyfacts does not expose the
# dimensional member that would.
GENERIC_UNITS = {'integer', 'cryptoasset', 'pure', 'unit', 'units', 'number', 'shares'}


def fair_value_series(facts):
    """us-gaap:CryptoAssetFairValue, the USD carrying value. Not used as a
    holding, but as the reconciliation that identifies the token: units times a
    candidate price should land near it. BMNR reports 5,700,049 units against
    $10.87bn, which is $1,907 a unit - ether, not bitcoin, unambiguously."""
    out = {}
    for name in ('CryptoAssetFairValue', 'CryptoAssetFairValueNoncurrent'):
        c = ((facts or {}).get('us-gaap') or {}).get(name)
        for unit, rows in ((c or {}).get('units') or {}).items():
            if str(unit).upper() != 'USD':
                continue
            for r in rows:
                if r.get('val') is None or not r.get('end') or r.get('start'):
                    continue
                k = r['end']
                if k not in out or r.get('filed', '') < out[k]['filed']:
                    out[k] = {'end': k, 'usd': float(r['val']), 'filed': r.get('filed', '')}
    return out


def reconcile_token(units_rows, fv, prices, declared_token, tolerance=0.25):
    """Does units x the declared token's price land near the reported fair
    value? A filer with a generic unit string has not told us which asset it
    holds, and getting that wrong is not a small error - bitcoin and ether
    differ by a factor of forty. This turns the registry's declaration into a
    checkable claim rather than an assumption."""
    checks = []
    for r in units_rows:
        v = fv.get(r['end'])
        if not v:
            continue
        # PRICE AT THE PERIOD END, not the filing date. The fair value being
        # reconciled against is the balance-sheet value AT THE PERIOD END; the
        # filing comes weeks later. Pricing at the filing date measures how far
        # the token moved in between and calls it a reconciliation error.
        #
        # It showed as a consistent ~15% miss on BMNR's November and February
        # quarters and 0.9% on May - which is the shape of a date error, not of
        # a wrong token. A wrong token misses by a factor, not by a drift.
        #
        # Note this is deliberately a DIFFERENT date from the one gross NAV
        # uses. Gross NAV is a point-in-time figure and is correctly struck at
        # the filing date, when the market first knows the units. The
        # reconciliation is an accounting check and belongs at the period end.
        px = price_at(prices, declared_token, r['end'])
        if not px or not r['units']:
            continue
        implied = v['usd'] / r['units']
        off = abs(implied - px) / px
        checks.append({'end': r['end'], 'units': r['units'], 'reported_fair_value_usd': v['usd'],
                       'implied_price_usd': round(implied, 2), f'{declared_token}_price_usd': px,
                       'priced_at': r['end'], 'off_by_pct': round(100 * off, 1),
                       'ok': off <= tolerance})
    return checks


def share_series(facts, concepts):
    """Shares outstanding by filing date, same supersession rule."""
    out = {}
    for tax, name in concepts:
        c = ((facts or {}).get(tax) or {}).get(name)
        if not c:
            continue
        for unit, rows in (c.get('units') or {}).items():
            if str(unit).lower() not in ('shares', 'share'):
                continue
            for r in rows:
                if r.get('val') is None or not r.get('filed'):
                    continue
                key = r.get('end') or r.get('filed')
                prev = out.get(key)
                if prev is None or r['filed'] > prev['filed']:
                    out[key] = {'filed': r['filed'], 'end': key, 'shares': float(r['val']),
                                'form': r.get('form'), 'concept': f'{tax}:{name}'}
    return sorted(out.values(), key=lambda r: r['filed'])


def as_of(series, when, field):
    """The latest value that had been FILED on or before `when`.

    This one function is what makes the whole module point-in-time. Reading
    the series by period end instead would let a June balance inform a July
    calculation that could not have known it.
    """
    # Among everything filed on or before `when`, the answer is the one
    # describing the LATEST PERIOD. Picking by filing date alone leaves ties
    # resolved by list order, and a 10-K carries several period ends on one
    # filing date - so the answer depended on iteration order rather than on
    # which balance was most recent.
    best = None
    for r in series:
        if r['filed'] > when:
            continue
        if best is None or (r['end'], r['filed']) > (best['end'], best['filed']):
            best = r
    return (best[field], best) if best else (None, None)


# --------------------------------------------------------------------- NAV ---

def _claim_amount(c, when):
    """The carrying amount in force at `when`.

    `schedule` is [{from, amount_usd}, ...]; the latest entry disclosed on or
    before `when` wins. Falls back to a flat `amount_usd` for instruments whose
    carrying amount does not move.
    """
    sched = c.get('schedule')
    if sched:
        best = None
        for e in sched:
            f = e.get('from') or ''
            if f <= when and (best is None or f >= (best.get('from') or '')):
                best = e
        return float((best or {}).get('amount_usd') or 0)
    return float(c.get('amount_usd') or 0)


def net_nav(gross, claims, when):
    """Net treasury asset value as a BAND.

    Returns (low, high, detail). `high` nets only the claims classified
    treasury-attributed; `low` also nets everything classified ambiguous.
    Where the two differ, the difference is the size of the disagreement and
    the site should show both rather than pick one.

    A claim with no classification is never netted and is reported as an
    error: silently ignoring it understates the claims and flatters NAV.
    """
    attributed, ambiguous, unclassified = 0.0, 0.0, []
    used = []
    for c in claims or []:
        if c.get('from') and c['from'] > when:
            continue                              # not yet disclosed at `when`
        if c.get('until') and c['until'] <= when:
            continue                              # repaid or converted
        # A convertible accretes: XXI's carries 484,326,591 at 2025-12-31 and
        # 484,543,716 six months later. One instrument, several carrying
        # amounts. A single figure would net a 2026 balance against a 2025
        # holding, so a claim may carry a SCHEDULE and the amount is read
        # point-in-time like everything else here.
        amt = _claim_amount(c, when)
        kind = (c.get('attribution') or '').lower()
        if kind == 'treasury':
            attributed += amt
        elif kind == 'ambiguous':
            ambiguous += amt
        elif kind in ('operating', 'none'):
            pass
        else:
            unclassified.append(c.get('instrument') or '(unnamed claim)')
            continue
        used.append({'instrument': c.get('instrument'), 'amount_usd': amt,
                     'attribution': kind, 'source': c.get('source')})
    return (gross - attributed - ambiguous,      # low: ambiguous treated as treasury
            gross - attributed,                  # high: ambiguous treated as operating
            {'claims_used': used, 'unclassified': unclassified,
             'attributed_usd': attributed, 'ambiguous_usd': ambiguous})


def accretion(rows):
    """§17. Whether existing holders gained economic tokens per diluted share,
    which is a different question from whether the company bought more tokens.

    A company can double its holdings and leave every shareholder worse off if
    it issued more than it bought. This is the metric that distinguishes the
    two, and it is the one least often published.
    """
    out = []
    prev = None
    for r in rows:
        if r.get('tokens_per_diluted_share') is None:
            continue
        if prev is not None:
            d_tok = r['tokens_per_diluted_share'] - prev['tokens_per_diluted_share']
            d_sh = (r.get('diluted_shares') or 0) - (prev.get('diluted_shares') or 0)
            d_units = (r.get('units') or 0) - (prev.get('units') or 0)
            out.append({
                'from': prev['filed'], 'to': r['filed'],
                'units_change': round(d_units, 4),
                'diluted_shares_change': round(d_sh, 0),
                'tokens_per_share_change': d_tok,
                # the four cases worth naming, because three of them look like
                # success in a press release
                'verdict': ('accretive' if d_tok > 0 and d_units > 0 else
                            'dilutive despite buying' if d_tok < 0 and d_units > 0 else
                            'accretive by shrinking' if d_tok > 0 and d_units <= 0 else
                            'dilutive'),
            })
        prev = r
    return out


def mnav(market_cap, nav_low, nav_high):
    """Equity market capitalisation over net treasury asset value, §18's
    primary definition. Returns a band because NAV is a band. Undefined where
    NAV is zero or negative, and says so rather than returning a number."""
    def one(nav):
        return None if not nav or nav <= 0 else market_cap / nav
    hi = one(nav_low)     # the smaller NAV gives the larger multiple
    lo = one(nav_high)
    return {'low': lo, 'high': hi,
            'undefined_reason': None if (lo or hi) else 'net NAV is zero or negative'}


# ------------------------------------------------------------------ company ---

def company(reg, prices, equities=None, today=None):
    """One issuer's full point-in-time treasury series."""
    today = today or dt.date.today().isoformat()
    if reg.get('source') == 'manual':
        # non-SEC filers: recorded, never invented. The registry carries what a
        # person read from the issuer's own disclosure, with its citation.
        return {'ticker': reg['ticker'], 'name': reg.get('name'),
                'source': 'manual', 'confidence': reg.get('confidence', 'low'),
                'note': reg.get('note') or 'not an SEC filer; figures require a per-issuer adapter',
                'rows': reg.get('manual_rows') or [], 'claims': reg.get('claims') or []}

    facts = _get(f'{SEC}/api/xbrl/companyfacts/CIK{int(reg["cik"]):010d}.json').get('facts')
    tokens = reg.get('tokens') or ['BTC']
    declared = bool(reg.get('token_declared'))
    per_token = {t: token_series(facts, t, declared=declared) for t in tokens}
    if not any(per_token.values()):
        return {'ticker': reg['ticker'], 'name': reg.get('name'), 'cik': reg.get('cik'),
                'source': 'sec-xbrl', 'tokens': tokens, 'rows': [],
                'status': 'no unit series found', 'diagnosis': diagnose(facts, tokens)}
    basic = share_series(facts, SHARE_CONCEPTS)
    diluted = share_series(facts, DILUTED_CONCEPTS) or basic

    # every distinct filing date at which any holding was reported
    dates = sorted({r['filed'] for s in per_token.values() for r in s})
    rows = []
    for d in dates:
        gross, held = 0.0, {}
        for t, s in per_token.items():
            u, meta = as_of(s, d, 'units')
            if u is None:
                continue
            px = price_at(prices, t, d)
            held[t] = {'units': u, 'price_usd': px, 'value_usd': (u * px) if px else None,
                       'period_end': meta['end'], 'form': meta['form'], 'accn': meta['accn'],
                       'concept': meta['concept']}
            if px:
                gross += u * px
        sh, sh_meta = as_of(basic, d, 'shares')
        dsh, _ = as_of(diluted, d, 'shares')
        dsh = dsh or sh
        low, high, detail = net_nav(gross, reg.get('claims'), d)
        mcap = market_cap_at(equities, reg['ticker'], d, sh)
        mn = mnav(mcap, low, high) if mcap else None
        # A field named net_nav that contains gross is the kind of label that
        # gets quoted. Until the claims are recorded from the filings the basis
        # is stated on every row, and for a leveraged issuer the difference is
        # the whole story.
        basis = 'net' if detail['claims_used'] else 'GROSS - no claims recorded yet'
        total_units = sum(h['units'] for h in held.values())
        rows.append({
            'filed': d, 'gross_nav_usd': round(gross, 2),
            'net_nav_low_usd': round(low, 2), 'net_nav_high_usd': round(high, 2),
            'held': held, 'basic_shares': sh, 'diluted_shares': dsh,
            'units': total_units,
            'nav_per_diluted_share_low': (low / dsh) if dsh else None,
            'nav_per_diluted_share_high': (high / dsh) if dsh else None,
            'tokens_per_diluted_share': (total_units / dsh) if dsh else None,
            'claims': detail, 'nav_basis': basis,
            'market_cap_usd': round(mcap, 2) if mcap else None, 'mnav': mn,
            'shares_as_of': sh_meta['filed'] if sh_meta else None,
        })
    claims_done = bool(reg.get('claims'))
    # RECONCILE EVERY COMPANY, not only those whose token was declared. The
    # check lived in the universe sweep and the index never called it, so a
    # registry entry went straight into the weights unexamined - which is how
    # a 1,719,000 bitcoin mis-tag came within one command of being 60% of a
    # published index.
    fv_all = fair_value_series(facts)
    failures = []
    for t, srs in per_token.items():
        for r in srs:
            v = fv_all.get(r['end']) or _nearest_fair_value(fv_all, r['end'])
            if not v:
                continue
            bad = price_plausible(t, r['units'], v['usd'], prices, r['end'])
            if bad:
                failures.append({'token': t, 'period': r['end'], 'reason': bad})
    recon = {}
    if declared:
        fv = fair_value_series(facts)
        for t, srs in per_token.items():
            r = reconcile_token(srs, fv, prices, t)
            if r:
                recon[t] = r
    mis = reg.get('xbrl_mis_tag')
    return {'ticker': reg['ticker'], 'name': reg.get('name'), 'cik': reg.get('cik'),
            'source': 'sec-xbrl', 'tokens': tokens, 'rows': rows,
            # a documented mis-tag keeps the company out even where the filer
            # publishes no carrying value to reconcile against
            'registry_excluded': (
                f'registry records a mis-tagged unit count: {mis.get("reported"):,.0f} reported '
                f'against a verified {mis.get("actual_reference"):,} '
                f'({mis.get("actual_source", "")[:90]})') if mis else None,
            'token_declared': declared, 'fair_value_reconciliation': recon,
            'reconciliation_failures': failures,
            'index_eligible': not failures,
            'claims_populated': claims_done,
            'nav_basis': 'net' if claims_done else 'GROSS - no claims recorded yet',
            'accretion': accretion(rows),
            'confidence': reg.get('confidence', 'high'),
            'unclassified_claims': sorted({u for r in rows for u in r['claims']['unclassified']})}


def price_at(prices, token, when):
    """Independent reference price, never the issuer's own valuation (§15).

    The convention must be stated and must match the equity close used in the
    same calculation, or a 5% intraday move becomes a 5% error in mNAV. The
    price file carries its own `convention` field and this module does not
    silently accept one that is missing.
    """
    s = (prices or {}).get(token.upper())
    if not s:
        return None
    best = None
    for d, v in s.get('values') or []:
        if d <= when and (best is None or d >= best[0]):
            best = (d, v)
    return float(best[1]) if best else None


# --------------------------------------------------------------- prices ---

# THE CONVENTION, STATED - because §15 requires an independent reference price
# and §7 requires the convention published, and because an unstated one is how
# a 5% intraday move becomes a 5% error in mNAV.
#
# Source        Coinbase Exchange daily candles, already fetched by this
#               pipeline into relative.json. A true daily CLOSE, not an
#               average: averaging smears each day's move across its
#               neighbours, which is right for a valuation level and wrong for
#               anything compared against an equity close.
#
# Timestamp     A Coinbase daily candle labelled D covers D 00:00 UTC to
#               D+1 00:00 UTC, so its close is at D+1 00:00 UTC.
#
# THE KNOWN MISMATCH, NAMED RATHER THAN HIDDEN. Equity market capitalisation is
# struck at the 16:00 New York close, which is 20:00 or 21:00 UTC on the same
# calendar date. The token close above is therefore 3 to 4 HOURS LATER than the
# equity close it is paired with. On a quiet day that is immaterial; on a day
# bitcoin moves 5% after the US close it is a 5% error in gross NAV and in
# mNAV, in a direction that changes daily.
#
# This is disclosed, quantified per row where it can be, and left as a known
# limitation rather than papered over. The exact fix is hourly candles taken at
# the NYSE close, which is a bounded piece of work and the right next
# improvement to this module.
PRICE_CONVENTION = (
    'Coinbase Exchange daily candle close (candle D closes D+1 00:00 UTC), via relative.json. '
    'Equity market capitalisation is struck at the 16:00 New York close, 3 to 4 hours earlier, '
    'so the pair is not simultaneous; see mismatch_hours.'
)


def prices_from_relative(rel):
    """Shape relative.json into what this module reads, with the convention
    attached. Refuses silently-empty series rather than returning a price file
    that looks valid and prices nothing."""
    out = {'convention': PRICE_CONVENTION, 'mismatch_hours': '3-4 (token close after equity close)',
           'source_file': 'relative.json', 'series': {}}
    for token, key in (('BTC', 'btc_usd'), ('ETH', 'eth_usd'), ('SOL', 'sol_usd')):
        vals = ((rel or {}).get('series') or {}).get(key) or []
        rows = [[d, float(v)] for d, v in vals if v]
        if rows:
            out[token] = {'values': rows}
            out['series'][token] = {'n': len(rows), 'from': rows[0][0], 'to': rows[-1][0],
                                    'key': key}
    return out



# ---------------------------------------------------------- claims draft ---

# Concepts that carry a CLAIM against the issuer. Finding them is mechanical;
# deciding whether the proceeds funded digital assets or the operating business
# is not, and never will be. This lists the candidates with their amounts,
# dates and accession numbers so the judgment is the only part left - which is
# how §15 should work in practice.
CLAIM_CONCEPTS = [
    ('ConvertibleNotesPayable', 'convertible'), ('ConvertibleDebtNoncurrent', 'convertible'),
    ('ConvertibleLongTermNotesPayable', 'convertible'), ('ConvertibleDebt', 'convertible'),
    ('LongTermDebt', 'debt'), ('LongTermDebtNoncurrent', 'debt'), ('LongTermNotesPayable', 'debt'),
    ('NotesPayable', 'debt'), ('SecuredDebt', 'debt'), ('UnsecuredDebt', 'debt'),
    ('LineOfCredit', 'debt'), ('DebtInstrumentFaceAmount', 'debt (face)'),
    ('DebtLongtermAndShorttermCombinedAmount', 'debt'),
    ('PreferredStockLiquidationPreferenceValue', 'preferred'),
    ('PreferredStockValue', 'preferred'),
    ('TemporaryEquityLiquidationPreference', 'preferred'),
    ('TemporaryEquityCarryingAmountAttributableToParent', 'preferred'),
]


def draft_claims(cik, ticker=''):
    """Every candidate claim this issuer has tagged, newest first, ready to
    classify. It deliberately does NOT guess the attribution: a wrong guess
    produces a net NAV that is precisely wrong rather than honestly uncertain,
    and the guess would be invisible once it is a number on a page."""
    facts = _get(f'{SEC}/api/xbrl/companyfacts/CIK{int(cik):010d}.json').get('facts') or {}
    found = []
    for name, kind in CLAIM_CONCEPTS:
        c = (facts.get('us-gaap') or {}).get(name)
        if not c:
            continue
        for unit, rows in (c.get('units') or {}).items():
            if str(unit).upper() != 'USD':
                continue
            best = {}
            for r in rows:
                if r.get('val') is None or not r.get('end') or r.get('start'):
                    continue
                k = (r['end'], float(r['val']))
                if k not in best or r.get('filed', '') < best[k]['filed']:
                    best[k] = {'end': r['end'], 'amount_usd': float(r['val']),
                               'filed': r.get('filed', ''), 'form': r.get('form'),
                               'accn': r.get('accn'), 'concept': f'us-gaap:{name}', 'kind': kind}
            found += list(best.values())
    found.sort(key=lambda r: (r['end'], -r['amount_usd']), reverse=True)
    return found


def claims_holdings_gap(claim_rows, holding_rows):
    """Compare the newest CLAIM period against the newest HOLDING period.

    A wide gap is the signature of a reverse merger or a change of business:
    the CIK's facts run continuously but they describe two different companies
    either side of the combination. ABTC shows holdings to a 2026-06-30 period
    end and claims stopping at 2025-06-30, with notes running back to 2021 at a
    scale that belongs to the predecessor.

    Netting those against this company's bitcoin would subtract a different
    company's debt. The module cannot tell which side of a combination an
    instrument belongs to - only a person reading the filings can - so it
    refuses to let the question go unasked."""
    if not claim_rows or not holding_rows:
        return None
    c_end = max(r['end'] for r in claim_rows)
    h_end = max(r['held'][t]['period_end'] for r in holding_rows for t in r['held'])
    gap_days = (dt.date.fromisoformat(h_end) - dt.date.fromisoformat(c_end)).days
    if gap_days < 200:
        return None
    return {'newest_claim_period': c_end, 'newest_holding_period': h_end,
            'gap_days': gap_days, 'oldest_claim_period': min(r['end'] for r in claim_rows),
            'warning': (
                'the claims and the holdings describe periods more than six months apart. '
                'On a CIK that has been through a reverse merger or a change of business, the '
                'older facts belong to the PREDECESSOR and must not be netted against this '
                "company's digital assets. Establish which instruments survive the combination "
                'before classifying any of them.')}


def print_draft(cik, ticker=''):
    rows = draft_claims(cik, ticker)
    if not rows:
        print(f'  {ticker}: no claim concepts tagged. Either unlevered, or the claims are '
              f'disclosed only in narrative text - read the notes by hand.')
        return []
    latest = rows[0]['end']
    # warn before the table, not after it: the classification decision is made
    # while reading the list
    try:
        held = json.load(io.open(f'{OUT}/treasury.json', encoding='utf-8'))
        me = next((c for c in held.get('companies', []) if c.get('ticker') == ticker), None)
        g = claims_holdings_gap(rows, (me or {}).get('rows') or [])
        if g:
            print(f'\n  !! {ticker}: claims run to {g["newest_claim_period"]} but holdings run to '
                  f'{g["newest_holding_period"]} - {g["gap_days"]} days apart, and the oldest claim '
                  f'is {g["oldest_claim_period"]}.')
            print(f'     {g["warning"]}')
    except Exception:
        pass
    print(f'\n  {ticker}: candidate claims. Newest balance sheet is {latest}.')
    print(f'  {"period":<12}{"kind":<14}{"amount":>18}  {"first filed":<12} concept')
    for r in rows[:24]:
        print(f'  {r["end"]:<12}{r["kind"]:<14}{r["amount_usd"]:>18,.0f}  {r["filed"]:<12}'
              f'{r["concept"].replace("us-gaap:", "")}')
    # Group by concept: several balance-sheet dates of ONE instrument are one
    # claim with a schedule, not several claims. Entering each row separately
    # would multiply a single obligation by the number of quarters it appears
    # in - on XXI, a half-billion convertible counted three times.
    by_concept = {}
    for r in rows:
        by_concept.setdefault(r['concept'], []).append(r)
    print(f'\n  {len(by_concept)} distinct concept(s). Each is ONE claim with a schedule, '
          f'not one per period.')
    print(f'  Classify each as treasury / operating / ambiguous, then paste into the '
          f'registry\'s claims list.\n')
    draft = []
    for concept, rs in sorted(by_concept.items(), key=lambda kv: -max(x['amount_usd'] for x in kv[1])):
        rs = sorted(rs, key=lambda x: x['end'])
        first = rs[0]
        draft.append({
            'instrument': concept.replace('us-gaap:', ''),
            'attribution': 'treasury|operating|ambiguous',
            'seniority': '',
            'from': first['filed'],
            'until': None,
            'schedule': [{'from': x['filed'], 'amount_usd': x['amount_usd'], 'period': x['end']}
                         for x in rs],
            'source': f'{first["accn"]}, {first["form"]} for {first["end"]}'
                      + (f' and {len(rs) - 1} later filing(s)' if len(rs) > 1 else ''),
        })
    print(json.dumps(draft, indent=2))
    return rows



# --------------------------------------------------------- equity prices ---

# mNAV needs equity market capitalisation, which needs a SHARE PRICE. The
# pipeline's existing sources give token prices and index levels, not single
# equities, so this adds one small source.
#
# Stooq: free daily CSV, no key, no registration. Chosen over the unofficial
# Yahoo endpoint because it is a documented download rather than a scraped
# API, and over a paid vendor because §4's whole point is to establish what is
# obtainable without one.
#
# THE SAME CONVENTION PROBLEM, FROM THE OTHER SIDE. Stooq's daily close is the
# exchange close - 16:00 New York for a US listing. The token price this
# module uses closes at 00:00 UTC the following day. They are 3 to 4 hours
# apart and that is already recorded in PRICE_CONVENTION; naming it here too
# because an mNAV pairs the two directly and inherits the error.
STOOQ = 'https://stooq.com/q/d/l/'
EQUITY_CONVENTION = (
    'Stooq daily close (exchange close, 16:00 New York for a US listing). Paired with a token '
    'price closing 00:00 UTC the following day, so an mNAV computed from the two is not struck '
    'at a single moment; see mismatch_hours.'
)


def equity_series(ticker, market='us'):
    """Daily closes for one listing. Returns [] rather than raising: a missing
    equity price must cost an mNAV, never a whole issuer's holdings."""
    sym = f'{ticker.lower()}.{market}'
    try:
        req = urllib.request.Request(f'{STOOQ}?s={sym}&i=d', headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=45) as r:
            text = r.read().decode()
    except Exception:
        return []
    out = []
    for line in text.splitlines()[1:]:
        parts = line.split(',')
        if len(parts) < 5:
            continue
        try:
            out.append([parts[0], float(parts[4])])
        except ValueError:
            continue
    return out


def load_equity_prices(registry):
    eq = {'convention': EQUITY_CONVENTION, 'series': {}}
    for c in registry.get('companies', []):
        t = c.get('ticker', '')
        if c.get('source') == 'manual' or '.' in t:
            continue                      # non-US listings need their own market code
        rows = equity_series(t)
        if rows:
            eq[t] = {'values': rows}
            eq['series'][t] = {'n': len(rows), 'from': rows[0][0], 'to': rows[-1][0]}
    return eq


def market_cap_at(eq, ticker, when, shares):
    """Basic shares times the close. Deliberately BASIC and not diluted: §18's
    primary definition is equity market capitalisation, which is what the
    market actually capitalises. The diluted figure belongs in NAV per share,
    where the claim on the assets is what matters - two different questions
    that look like one."""
    if not shares:
        return None
    px = price_at(eq, ticker, when)
    return (px * shares) if px else None


# ================================================================ CTI-US ===
#
# The universe, discovered rather than chosen.
#
# Every previous step worked from six issuers we happened to name. That is a
# monitor, not an index: §5 requires the broadest defensible universe and every
# exclusion documented, and a hand-list fails both - it has no stated inclusion
# rule and its omissions are invisible.
#
# SEC's XBRL FRAMES API returns every filer reporting a concept in a period. One
# call to CryptoAssetNumberOfUnits therefore enumerates every US filer that
# reports a crypto holding, mechanically and completely. That is a real
# universe with a rule behind it, and anything absent is absent for a reason
# the rule states.
#
# WHAT THIS IS AND IS NOT. It is CTI-US: one sleeve, US filers, treasury
# exposure. It is NOT CEC, CEI or CEE - those need point-in-time free float
# across global markets, which Phase 0 found is not reconstructable on public
# data. Naming it accurately is the difference between an index and a claim.

FRAMES = SEC + '/api/xbrl/frames/us-gaap/CryptoAssetNumberOfUnits'


def discover_universe(periods=None):
    """Every US filer reporting a crypto unit count, from the frames API.

    Returns {cik: {ticker?, name, units, unit_label, period, accn}}. The frame
    is per period, so several are queried and merged: a filer that reported in
    Q1 but not Q2 belongs in the universe with a stale reading, not absent.
    Dropping it would be survivorship bias of exactly the kind §30 forbids.
    """
    periods = periods or _recent_frames()
    found, tried, failed = {}, [], []
    for unit, per in periods:
        url = f'{FRAMES}/{unit}/{per}.json'
        tried.append(f'{unit}/{per}')
        try:
            data = _get(url, tries=2)
        except Exception as e:
            failed.append(f'{unit}/{per}: {str(e)[:60]}')
            continue
        for row in data.get('data') or []:
            cik = row.get('cik')
            if cik is None or row.get('val') is None:
                continue
            prev = found.get(cik)
            if prev is None or per > prev['period']:
                found[cik] = {'cik': int(cik), 'name': row.get('entityName'),
                              'units': float(row['val']), 'unit_label': unit,
                              'period': per,
                              # the ACTUAL balance-sheet date, not one derived
                              # from the frame label. A filer with a non-calendar
                              # fiscal year is assigned to the nearest calendar
                              # quarter - BitMine's 31 May sits in CY2026Q2I -
                              # so deriving 30 June from the label and demanding
                              # an exact match found nothing for every such filer.
                              'end': row.get('end'),
                              'accn': row.get('accn'),
                              'form': row.get('form'), 'filed': row.get('filed'),
                              'frame': f'{unit}/{per}'}
    return {'companies': list(found.values()), 'frames_tried': tried,
            'frames_failed': failed,
            'note': ('every US filer reporting us-gaap:CryptoAssetNumberOfUnits in the frames '
                     'queried. A filer absent here either does not tag the concept or did not '
                     'report in these periods; absence is not evidence of no holding, and §14 '
                     'requires that distinction to survive into the output.')}


def _recent_frames(n=6):
    """Recent instantaneous frames, newest first. The unit segment is the token
    name, so several are queried - a bitcoin filer and an ether filer appear in
    different frames entirely, and querying only one would silently exclude a
    whole class of issuer."""
    today = dt.date.today()
    out = []
    for unit in ('Bitcoin', 'BTC', 'Ethereum', 'ETH', 'Integer', 'cryptoAsset', 'shares', 'pure'):
        y, q = today.year, (today.month - 1) // 3 + 1
        for _ in range(n):
            out.append((unit, f'CY{y}Q{q}I'))
            q -= 1
            if q == 0:
                q, y = 4, y - 1
    return out


# --------------------------------------------------- CTI-US construction ---

# THE RULES, FROZEN AND STATED. Every one is a parameter that could be tuned to
# flatter a backtest, which is why §23 ranks historical return sixteenth and
# §32 requires rejecting anything that depends on a narrow choice. These are
# set for STABILITY - a company should not enter and leave on a quiet quarter -
# and the effect of changing them is a test, not a tweak.
CTI_RULES = {
    'version': 'CTI-US v0.1',
    'universe': 'every US filer reporting us-gaap:CryptoAssetNumberOfUnits in the frames queried',
    'min_gross_nav_usd': 25_000_000,
    'min_market_cap_usd': 50_000_000,
    'entry_buffer_nav_usd': 25_000_000,
    'exit_buffer_nav_usd': 15_000_000,      # lower than entry: §26, so a borderline
                                            # issuer does not churn in and out
    'single_name_cap': 0.15,
    'top_five_cap': 0.55,
    'weight': 'gross treasury NAV, capped; NOT market capitalisation',
    'why_nav_weight': (
        'Weighting by market capitalisation would weight by the treasury PREMIUM, which is '
        'the thing CTP exists to measure. An index whose weights move with the premium cannot '
        'then report on it - that is the circularity §18 rules out for mNAV, arriving through '
        'the weight instead.'),
    'rebalance': 'on each pipeline run; a production index would use quarterly with buffers (§27)',
    'not_included': (
        'no free float, no ADV screen, no corporate-action rules, no seasoning. CTI-US v0.1 is '
        'an ECONOMIC measure in the CEE sense, not an investable one. Calling it investable '
        'would require the eligibility work in §25 that has not been done.'),
}


def build_cti(companies, rules=None):
    """Weights from gross treasury NAV, capped. Returns the index plus every
    exclusion with its reason, because §5 requires that and because an index
    that only shows what it kept cannot be checked."""
    r = dict(CTI_RULES); r.update(rules or {})
    members, excluded = [], []
    for c in companies:
        rows = c.get('rows') or []
        if not rows:
            excluded.append({'ticker': c.get('ticker'), 'name': c.get('name'),
                             'reason': c.get('status') or 'no point-in-time holding series'})
            continue
        if c.get('reconciliation_failures'):
            f = c['reconciliation_failures'][-1]
            excluded.append({'ticker': c.get('ticker'), 'name': c.get('name'),
                             'reason': f['reason'], 'needs_verification': True})
            continue
        if c.get('registry_excluded'):
            excluded.append({'ticker': c.get('ticker'), 'name': c.get('name'),
                             'reason': c['registry_excluded']})
            continue
        last = rows[-1]
        nav = last.get('gross_nav_usd') or 0
        mcap = last.get('market_cap_usd')
        if nav < r['min_gross_nav_usd']:
            excluded.append({'ticker': c.get('ticker'), 'name': c.get('name'),
                             'reason': f'gross NAV ${nav:,.0f} below the ${r["min_gross_nav_usd"]:,.0f} floor'})
            continue
        if mcap is not None and mcap < r['min_market_cap_usd']:
            excluded.append({'ticker': c.get('ticker'), 'name': c.get('name'),
                             'reason': f'market cap ${mcap:,.0f} below the ${r["min_market_cap_usd"]:,.0f} floor'})
            continue
        members.append({'ticker': c.get('ticker'), 'name': c.get('name'), 'cik': c.get('cik'),
                        'tokens': c.get('tokens'), 'as_of': last.get('filed'),
                        'gross_nav_usd': nav, 'market_cap_usd': mcap,
                        'net_nav_low_usd': last.get('net_nav_low_usd'),
                        'net_nav_high_usd': last.get('net_nav_high_usd'),
                        'nav_basis': last.get('nav_basis'), 'mnav': last.get('mnav'),
                        'claims_populated': c.get('claims_populated'),
                        'confidence': c.get('confidence')})
    total = sum(m['gross_nav_usd'] for m in members) or 1.0
    for m in members:
        m['raw_weight'] = m['gross_nav_usd'] / total
    cap_info = _apply_caps(members, r['single_name_cap'], r['top_five_cap'])
    members.sort(key=lambda m: -m['weight'])
    return {'name': 'CTI-US', 'version': r['version'], 'rules': r,
            'as_of': max((m['as_of'] for m in members), default=None),
            'members': members, 'excluded': excluded,
            'member_count': len(members), 'capping': cap_info,
            'gross_nav_total_usd': round(total, 2),
            'concentration': {
                'top_1': round(sum(m['weight'] for m in members[:1]), 4),
                'top_5': round(sum(m['weight'] for m in members[:5]), 4),
                'top_10': round(sum(m['weight'] for m in members[:10]), 4)},
            'caveat': (
                'An economic measure, not an investable one. Weights are gross treasury NAV: '
                'where an issuer\'s claims are not yet recorded its NAV is gross of debt and '
                'preferred, which OVERSTATES its weight against a fully-recorded peer. The '
                'nav_basis field on every member says which it is.')}


def _apply_caps(members, single, top5):
    """Single-name cap, applied properly. Top-five cap REPORTED, never forced.

    Three attempts at this, and the third is the one that is provably right.

    The single-name cap is a standard iterative capping problem and converges:
    cap whoever is over, share the excess among those under, repeat. It is
    order-preserving by construction, because a name is only ever raised toward
    the cap and never past a larger one.

    THE TOP-FIVE CAP IS DIFFERENT, and this is the part worth recording. On a
    small universe it can be INFEASIBLE. With eight members and a 55% limit on
    the top five, the other three must carry 45% - 15% each, more than any of
    the capped five. There is no solution that keeps the ranking. My first two
    attempts forced it anyway and produced an index where the smallest holding
    outweighed the largest, which is indefensible: a cap meant to LIMIT
    concentration cannot be allowed to invert the order it is limiting.

    So it is measured and reported as a breach with its cause, not applied. A
    cap that cannot bind without inverting is a fact about the universe, and
    the honest response is to say the universe is too small - not to publish
    weights that satisfy a rule by violating a more basic one.
    """
    if not members:
        return {'single_applied': False, 'top5_breach': None}
    n = len(members)
    for m in members:
        m['weight'] = m['raw_weight']
        m['capped'] = False

    if n * single < 1.0 - 1e-9:
        return {'single_applied': False, 'top5_breach': None,
                'single_skipped_reason': (
                    f'{n} members at a {single:.0%} cap reach only {n * single:.0%}. The cap is '
                    f'unreachable on a universe this small; weights are uncapped and say so.')}

    for _ in range(500):
        over = [m for m in members if m['weight'] > single + 1e-12]
        if not over:
            break
        excess = sum(m['weight'] - single for m in over)
        for m in over:
            m['weight'] = single
            m['capped'] = True
        under = [m for m in members if m['weight'] < single - 1e-12]
        pool = sum(m['weight'] for m in under)
        if not under or pool <= 0:
            # everyone is at the cap: share the remainder equally, the only
            # allocation that does not invent an ordering
            each = excess / n
            for m in members:
                m['weight'] += each
            break
        shares = [(m, m['weight'] / pool) for m in under]
        for m, sh in shares:
            m['weight'] += excess * sh

    tot = sum(m['weight'] for m in members) or 1.0
    for m in members:
        m['weight'] = m['weight'] / tot

    ranked = sorted(members, key=lambda m: -m['weight'])
    t5 = sum(m['weight'] for m in ranked[:5])
    breach = None
    if len(ranked) > 5 and t5 > top5 + 1e-9:
        breach = {'top_five': round(t5, 4), 'limit': top5, 'members': n,
                  'reason': (
                      f'the top five hold {t5:.1%} against a {top5:.0%} guideline. On {n} '
                      f'members the limit cannot be met without lifting smaller holdings above '
                      f'larger ones, so it is reported rather than enforced. It becomes '
                      f'bindable as the universe grows.')}
    for m in members:
        m['weight'] = round(m['weight'], 6)
    return {'single_applied': any(m['capped'] for m in members),
            'single_cap': single, 'top5_limit': top5,
            'top_five_actual': round(t5, 4), 'top5_breach': breach}


# ------------------------------------------------- universe qualification ---

# THREE EXCLUSIONS, each with a rule rather than a judgment call.

# 1. §19: a trust or ETF holds crypto FOR ITS SHAREHOLDERS. That is assets
#    under custody, not issuer-owned treasury, and counting it would put the
#    same coins in the index twice - once in the trust and once in whoever owns
#    the trust. The frames sweep returns them because they tag the same
#    concept, which is exactly why the exclusion has to be explicit.
TRUST_MARKERS = (' trust', ' etf', 'ishares', 'grayscale', 'bitwise ', 'osprey ',
                 'franklin ', 'valkyrie', 'wisdomtree', 'invesco', 'vaneck',
                 'fidelity wise', 'abrdn', 'hashdex', '21shares')

# 2. Unit strings that name nothing. Two thirds of the universe tags one of
#    these, so the token cannot be identified from the API and the holding
#    cannot be priced. They stay IN the universe - absence would be a lie - and
#    out of the index until a registry declaration names the token, checked
#    against reported fair value the way BMNR's was.
UNIT_TO_TOKEN = {'bitcoin': 'BTC', 'btc': 'BTC',
                 'ethereum': 'ETH', 'eth': 'ETH', 'ether': 'ETH',
                 'solana': 'SOL', 'sol': 'SOL'}
IDENTIFYING_UNITS = set(UNIT_TO_TOKEN)



# A HOLDING CANNOT EXCEED THE ASSET THAT EXISTS.
#
# The frames sweep returned CleanSpark tagging 1,719,000 under the unit
# "Bitcoin". That is 8.6% of every bitcoin ever mined, more than all the ETFs
# combined and twice Strategy's position; the real figure is around 12,500. At
# $78k it prices as $134bn and would have been 70% of CTI-US before capping.
#
# One mis-tagged filer would have defined the index, and nothing in the
# pipeline would have objected: the unit string said Bitcoin, the concept was
# the standard one, the number was a number.
#
# So every holding is checked against the circulating supply of the asset it
# claims to be. A corporate treasury above a few per cent of supply is not
# impossible in principle, but it is extraordinary enough that it must be
# verified by a person rather than admitted by a parser. Flagged, never
# silently dropped and never silently kept.
CIRCULATING_SUPPLY = {'BTC': 19_900_000, 'ETH': 120_500_000, 'SOL': 580_000_000}

# A PERCENTAGE-OF-SUPPLY THRESHOLD IS THE WRONG GATE, and the test proved it.
#
# Strategy holds 4.25% of every bitcoin. BitMine holds 4.7% of every ether.
# Both are real. CleanSpark's mis-tagged 1,719,000 is 8.6%. A threshold that
# catches the bad figure and spares the good ones has to sit in a gap of four
# percentage points - and Strategy keeps buying, so the gap closes on its own.
# Any such threshold eventually rejects the largest honest holder, which is the
# worst possible failure for an index of large holders.
#
# So this is a FLAG, not a gate: it marks a holding for a human to look at. The
# gate is the fair-value reconciliation, which needs no magic number - CleanSpark
# tagging 1,719,000 "Bitcoin" against a reported fair value near $1bn implies
# $582 a coin, which is nothing like bitcoin and is caught precisely.
FLAG_SUPPLY_SHARE = 0.06


def supply_check(token, units):
    """None if unremarkable, otherwise a note that a person should look.

    Deliberately NOT an exclusion. It exists to raise the question, and the
    reconciliation answers it.
    """
    sup = CIRCULATING_SUPPLY.get((token or '').upper())
    if not sup or not units:
        return None
    share = units / sup
    if share <= FLAG_SUPPLY_SHARE:
        return None
    return (f'{units:,.0f} {token} is {share:.1%} of circulating supply ({sup:,} {token}). '
            f'Larger than any known corporate treasury, so it is flagged for verification '
            f'against the filing. Not excluded on this alone: the largest honest holders are '
            f'several per cent of supply and growing, and a threshold that catches a mis-tag '
            f'today would reject them tomorrow.')


def price_plausible(token, units, fair_value_usd, prices, when, tolerance=0.5):
    """The real gate. Units times a reference price should land near the
    filer's own reported fair value.

    CleanSpark's 1,719,000 "Bitcoin" against a fair value near $1bn implies
    about $582 a coin. Strategy's 846,000 against $53bn implies $63,000. One is
    bitcoin and the other is not, and no threshold on quantity is needed to
    tell them apart.
    """
    if not (units and fair_value_usd):
        return None
    px = price_at(prices, token, when)
    if not px:
        return None
    implied = fair_value_usd / units
    off = abs(implied - px) / px
    if off <= tolerance:
        return None
    return (f'{units:,.0f} {token} against a reported fair value of ${fair_value_usd:,.0f} '
            f'implies ${implied:,.2f} a unit, against a reference price of ${px:,.2f} '
            f'({off:.0%} apart). The unit count and the carrying value do not describe the '
            f'same holding; one of them is mis-tagged.')



# ------------------------------------------------- token identification ----

# TWO THIRDS OF THE UNIVERSE TAGS A UNIT STRING THAT NAMES NOTHING, and
# companyfacts flattens away the dimensional member that would. Asking for a
# human declaration on each is eighteen judgments, which is eighteen chances to
# be wrong and no way to check any of them.
#
# But the filer reports TWO numbers: a unit count and a fair value. Their
# quotient is an implied price per unit, and a price identifies an asset. This
# infers the token from the issuer's own accounts and then requires the match
# to be UNIQUE - if two candidate assets both fit within tolerance, it stays
# unidentified rather than guessing between them.
#
# Where nothing matches, the implied price is still reported. A person reading
# "$2.17 a unit" identifies it in a second; reading "Integer" cannot.
STABLE_BAND = (0.97, 1.03)


def infer_token(units, fair_value_usd, prices, when, tolerance=0.12):
    """Identify the asset from the filer's own numbers.

    Returns (token, detail). `token` is None where no unique match exists, and
    `detail` always carries the implied price so an unidentified row is still
    informative.
    """
    if not (units and fair_value_usd):
        return None, {'reason': 'needs both a unit count and a reported fair value'}
    implied = fair_value_usd / units
    cands = []
    for tok in (prices or {}):
        if tok in ('convention', 'mismatch_hours', 'source_file', 'series'):
            continue
        px = price_at(prices, tok, when)
        if not px:
            continue
        off = abs(implied - px) / px
        if off <= tolerance:
            cands.append({'token': tok, 'price_usd': px, 'off_by_pct': round(100 * off, 1)})
    detail = {'implied_price_usd': round(implied, 4), 'candidates': cands, 'priced_at': when}
    if len(cands) == 1:
        return cands[0]['token'], {**detail, 'matched': cands[0]}
    if len(cands) > 1:
        # two assets within tolerance of each other cannot be told apart this
        # way, and picking the closer one would be a coin flip dressed as a
        # measurement
        return None, {**detail, 'reason': 'more than one asset matches; cannot be distinguished '
                                          'from the implied price alone'}
    if STABLE_BAND[0] <= implied <= STABLE_BAND[1]:
        return 'USD-STABLE', {**detail, 'matched': {'token': 'USD-STABLE', 'price_usd': 1.0},
                              'note': 'implies about a dollar a unit: a stablecoin. Valued at '
                                      'unit count, and worth recording as such because a '
                                      'stablecoin treasury is not crypto price exposure.'}
    return None, {**detail, 'reason': 'no reference price within tolerance of the implied price'}


def identify_unknowns(universe, prices, limit=40):
    """For every filer whose unit string names nothing, fetch its reported fair
    value and infer the asset from the implied price.

    One companyfacts call per filer. Slow, and run on demand rather than on
    every pipeline pass, but it converts an opaque row into either an
    identified holding or an implied price a person can name at a glance.
    """
    out = []
    unknown = [c for c in universe.get('companies', [])
               if str(c.get('unit_label', '')).lower() not in IDENTIFYING_UNITS]
    for c in unknown[:limit]:
        rec = {'cik': c['cik'], 'name': c.get('name'), 'units': c.get('units'),
               'unit_label': c.get('unit_label'), 'period': c.get('period')}
        try:
            facts = _get(f'{SEC}/api/xbrl/companyfacts/CIK{int(c["cik"]):010d}.json').get('facts')
        except Exception as e:
            out.append({**rec, 'status': f'fetch failed: {str(e)[:60]}'})
            continue
        fv = fair_value_series(facts)
        # the fair value for the same period end as the unit count, or nothing:
        # pairing a holding with a carrying value from a different quarter would
        # imply a price that is neither
        # the row's own end first; the frame label only as a fallback for data
        # that predates this field being captured
        end = c.get('end') or _period_end(c.get('period'))
        v = fv.get(end) if end else None
        if not v and end:
            # a filer may report the holding and the carrying value on dates a
            # few days apart; anything wider is a different balance sheet
            v = _nearest_fair_value(fv, end, days=10)
        if not v:
            out.append({**rec, 'status': 'no CryptoAssetFairValue for the same period end',
                        'fair_values_available': sorted(fv)[-3:]})
            continue
        tok, detail = infer_token(c.get('units'), v['usd'], prices, v['end'])
        out.append({**rec, 'fair_value_usd': v['usd'], 'inferred_token': tok,
                    'implied_price_usd': detail.get('implied_price_usd'),
                    'basis': 'inferred from the issuer\'s own unit count and carrying value',
                    'detail': detail,
                    'status': 'identified' if tok else 'implied price reported, asset not matched'})
    return out


def _nearest_fair_value(fv, end, days=10):
    """The carrying value within `days` of the holding's date, or nothing.

    Pairing a unit count with a fair value from a different quarter implies a
    price that is neither, so the window is deliberately narrow.
    """
    try:
        target = dt.date.fromisoformat(end)
    except Exception:
        return None
    best = None
    for k, v in fv.items():
        try:
            gap = abs((dt.date.fromisoformat(k) - target).days)
        except Exception:
            continue
        if gap <= days and (best is None or gap < best[0]):
            best = (gap, v)
    return best[1] if best else None


def _period_end(frame):
    """CY2026Q2I -> 2026-06-30. The frames label is a quarter, the fair value is
    keyed by balance-sheet date, and they have to be made to meet."""
    if not frame or len(frame) < 8:
        return None
    try:
        y = int(frame[2:6]); q = int(frame[7])
    except (ValueError, IndexError):
        return None
    return {1: f'{y}-03-31', 2: f'{y}-06-30', 3: f'{y}-09-30', 4: f'{y}-12-31'}.get(q)


def qualify_universe(universe, registry=None, min_units=1e-9, prices=None, facts_for=None):
    """Split the discovered universe into qualifying issuers and exclusions,
    each with its reason. §5 requires the reasons; an index that shows only
    what it kept cannot be checked."""
    declared = {}
    for c in (registry or {}).get('companies', []):
        if c.get('cik') and c.get('token_declared'):
            declared[int(c['cik'])] = (c.get('tokens') or [None])[0]
    keep, drop = [], []
    for c in universe.get('companies', []):
        name = (c.get('name') or '').lower()
        unit = str(c.get('unit_label') or '').lower()
        cik = int(c.get('cik'))
        marker = is_fund(c.get('name'))
        if marker:
            drop.append({**c, 'reason': fund_reason(marker)})
            continue
        if not c.get('units') or c['units'] < min_units:
            drop.append({**c, 'reason': f'reports {c.get("units")} units'})
            continue
        tok = UNIT_TO_TOKEN.get(unit) or (declared.get(cik) if cik in declared else None)
        flag = supply_check(tok, c.get('units')) if tok else None
        if unit in IDENTIFYING_UNITS:
            # map, never truncate: "bitcoin"[:3] is "bit", which is not a
            # ticker and would not match any price series
            #
            # AND RECONCILE IT. The gate ran only where the token had been
            # INFERRED, which is backwards: an inferred token is already a
            # price agreeing with a carrying value, while a STATED one has had
            # nothing question it at all. CleanSpark tags "Bitcoin" and so
            # skipped the check entirely - 1,719,000 units, 8.6% of every
            # bitcoin mined, would have entered the index unexamined.
            tok2 = UNIT_TO_TOKEN[unit]
            bad, v = None, None
            if prices and facts_for:
                fv = fair_value_series(facts_for(cik) or {})
                end = c.get('end') or _period_end(c.get('period'))
                v = (fv.get(end) if end else None) or (_nearest_fair_value(fv, end) if end else None)
                if v:
                    bad = price_plausible(tok2, c.get('units'), v['usd'], prices, v['end'])
            if bad:
                drop.append({**c, 'token': tok2, 'reason': bad, 'needs_verification': True})
                continue
            if prices and facts_for and not v:
                flag = (flag or '') + (' ' if flag else '') + (
                    'no CryptoAssetFairValue reported for this period, so the unit count could '
                    'not be reconciled against the issuer\'s own accounts. It is unchecked, not '
                    'verified.')
            keep.append({**c, 'token': tok2, 'token_from': 'xbrl unit',
                         'supply_flag': flag,
                         # whether this member was actually checked, per member:
                         # an aggregate count would hide which ones were not
                         'reconciled': bool(prices and facts_for and v),
                         'implied_price_usd': (round(v['usd'] / c['units'], 4)
                                               if (v and c.get('units')) else None)})
        elif cik in declared:
            keep.append({**c, 'token': declared[cik], 'token_from': 'registry declaration',
                         'supply_flag': flag})
        else:
            drop.append({**c, 'reason': (
                f'unit string "{c.get("unit_label")}" names no token, and companyfacts flattens '
                f'away the dimensional member that would. Cannot be priced until the token is '
                f'declared and reconciled against reported fair value.')})
    flagged = [c['name'] for c in keep if c.get('supply_flag')]
    # A CHECK THAT DID NOT RUN LOOKS EXACTLY LIKE A CHECK THAT PASSED, and this
    # one silently does nothing without a price file or a facts fetcher. That
    # is the most dangerous shape a guard can take: CleanSpark's mis-tag would
    # sail through and the output would look identical to a clean run.
    recon_state = ('ran' if (prices and facts_for) else
                   'NOT RUN: no reference prices' if not prices else
                   'NOT RUN: no companyfacts fetcher')
    return {'qualifying': keep, 'excluded': drop, 'flagged_for_verification': flagged,
            'reconciliation': recon_state,
            'reconciled': sum(1 for c in keep if c.get('reconciled')),
            'counts': {'discovered': len(universe.get('companies', [])),
                       'qualifying': len(keep), 'excluded': len(drop)},
            'exclusion_summary': _summarise([d['reason'] for d in drop])}


def _summarise(reasons):
    out = {}
    for r in reasons:
        k = ('trust or ETF' if 'trust or ETF' in r else
             'token not identified' if 'names no token' in r else
             'implausible against circulating supply' if 'circulating supply' in r else
             'unit count and carrying value disagree' if 'same holding' in r else
             'zero or negative holding')
        out[k] = out.get(k, 0) + 1
    return out


def registry_from_universe(reg, universe_path=f'{OUT}/treasury_universe.json'):
    """Every QUALIFIED filer becomes an index candidate, whether or not a person
    has written it into the registry.

    Discovery, qualification and the index were three things running beside each
    other: the sweep found 33 filers and verified 7, and the index read a
    hand-list of 6. The verification was worth nothing to the index because
    nothing consumed it.

    A hand-written entry always WINS, because it carries judgment a sweep cannot
    have - a declared token, recorded claims, a documented mis-tag. A generated
    entry carries none of that and says so, so the two are never mistaken for
    each other.
    """
    try:
        u = json.load(io.open(universe_path, encoding='utf-8'))
    except Exception:
        return reg, {'added': 0, 'reason': 'no universe file; run --discover first'}
    q = (u.get('qualification') or {}).get('qualifying') or []
    have = {int(c['cik']) for c in reg.get('companies', []) if c.get('cik')}
    added = []
    for c in q:
        cik = int(c['cik'])
        if cik in have:
            continue
        reg.setdefault('companies', []).append({
            'ticker': f'CIK{cik}', 'name': c.get('name'), 'cik': cik,
            'tokens': [c.get('token')] if c.get('token') else ['BTC'],
            'token_declared': c.get('token_from') == 'registry declaration',
            'confidence': 'medium' if c.get('reconciled') else 'low',
            'source_note': (
                f'generated from the qualified universe on the {c.get("token_from")}; '
                f'{"reconciled against the issuer\u2019s own carrying value" if c.get("reconciled") else "NOT reconciled - the filer reports no carrying value for this period"}. '
                f'No claims have been read for this company, so its value is gross.'),
            'generated': True,
            'claims': [],
        })
        added.append(c.get('name'))
    return reg, {'added': len(added), 'names': added}


def main(registry_path='src/treasury/registry.json', prices=None, out_dir=OUT,
         relative_path='data/relative.json'):
    # default to the pipeline's own price file rather than making the caller
    # supply one: a module that runs without prices silently reports zero NAV
    if prices is None and os.path.exists(relative_path):
        prices = prices_from_relative(json.load(io.open(relative_path, encoding='utf-8')))
    # the convention check comes FIRST and does not depend on the registry: a
    # price file without a stated timestamp convention invalidates every mNAV
    # downstream, whatever the registry happens to contain
    conv = (prices or {}).get('convention')
    if prices and not conv:
        raise SystemExit('price file carries no `convention`: state the source, timestamp and '
                         'timezone, and align it with the equity close (see §7 and finding 5)')
    reg = json.load(io.open(registry_path, encoding='utf-8'))
    reg, gen = registry_from_universe(reg)
    equities = load_equity_prices(reg)
    out = {'schema_version': 1, 'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
           'price_convention': conv, 'generated_from_universe': gen,
           'equity_convention': equities.get('convention'),
           'equity_coverage': equities.get('series'), 'companies': []}
    for c in reg.get('companies', []):
        try:
            out['companies'].append(company(c, prices, equities))
        except Exception as e:
            out['companies'].append({'ticker': c.get('ticker'), 'status': 'error',
                                     'reason': str(e)[:200]})
    os.makedirs(out_dir, exist_ok=True)
    io.open(f'{out_dir}/treasury.json', 'w', encoding='utf-8').write(json.dumps(out))
    # the index, built from whatever the registry covers. Discovery of the
    # wider universe is a separate command: it is a long set of SEC calls and
    # should not run on every pipeline pass.
    out['cti'] = build_cti(out['companies'])
    ok = sum(1 for c in out['companies'] if c.get('rows'))
    gross_only = [c['ticker'] for c in out['companies'] if c.get('rows') and not c.get('claims_populated')]
    print(f'  treasury: {ok} of {len(out["companies"])} issuers with a point-in-time series')
    for c in out['companies']:
        if c.get('diagnosis'):
            d = c['diagnosis']
            print(f'  treasury: {c["ticker"]} found nothing. units present: '
                  f'{", ".join(d["non_dollar_units_present"]) or "(none)"}')
    if gen.get('added'):
        print(f'  treasury: {gen["added"]} qualified filer(s) added from the universe sweep '
              f'(no claims read, so gross)')
    elif gen.get('reason'):
        print(f'  treasury: {gen["reason"]}')
    ix = out['cti']
    print(f'  CTI-US: {ix["member_count"]} members, {len(ix["excluded"])} excluded, '
          f'${ix["gross_nav_total_usd"]/1e9:,.1f}bn gross treasury NAV')
    if ix['capping'].get('top5_breach'):
        print(f'  CTI-US: top five hold {ix["capping"]["top5_breach"]["top_five"]:.0%} against a '
              f'{ix["capping"]["top5_breach"]["limit"]:.0%} guideline - reported, not enforced, '
              f'on {ix["member_count"]} members')
    if gross_only:
        print(f'  treasury: GROSS ONLY, claims not yet recorded: {", ".join(gross_only)}')
        print('            these figures are not net of debt or preferred claims; do not publish them')
    return out


if __name__ == '__main__':
    # `python fetch/treasury.py --claims MSTR` lists that issuer's candidate
    # claims instead of running the pipeline. Finding them is mechanical;
    # classifying them is the judgment §15 requires and this will not fake it.
    if len(sys.argv) > 1 and sys.argv[1] == '--discover':
        # every US filer reporting a crypto unit count, from the frames API.
        # This is §5's universe: a rule, not a hand-list.
        u = discover_universe()
        try:
            _reg = json.load(io.open('src/treasury/registry.json', encoding='utf-8'))
        except Exception:
            _reg = {}
        _px = None
        try:
            _px = prices_from_relative(json.load(io.open('data/relative.json', encoding='utf-8')))
        except Exception:
            pass
        _cache = {}
        def _facts(cik):
            if cik not in _cache:
                try:
                    _cache[cik] = _get(f'{SEC}/api/xbrl/companyfacts/CIK{int(cik):010d}.json').get('facts')
                except Exception:
                    _cache[cik] = {}
            return _cache[cik]
        q = qualify_universe(u, _reg, prices=_px, facts_for=_facts if _px else None)
        u['qualification'] = q
        cs = sorted(u['companies'], key=lambda c: -c['units'])
        print(f'  {len(cs)} US filers report a crypto unit count '
              f'({len(u["frames_tried"])} frames queried, {len(u["frames_failed"])} failed)')
        print(f'  {"units":>16}  {"period":<10} {"cik":<10} name')
        for c in cs[:60]:
            print(f'  {c["units"]:>16,.2f}  {c["period"]:<10} {c["cik"]:<10} {(c["name"] or "")[:52]}')
        os.makedirs(OUT, exist_ok=True)
        io.open(f'{OUT}/treasury_universe.json', 'w', encoding='utf-8').write(json.dumps(u))
        print()
        print(f'  QUALIFICATION: {q["counts"]["qualifying"]} of {q["counts"]["discovered"]} '
              f'qualify for CTI-US')
        print(f'  RECONCILIATION: {q["reconciliation"]}'
              + (f' - {q["reconciled"]} of {q["counts"]["qualifying"]} members checked against '
                 f'their own reported fair value' if q['reconciliation'] == 'ran' else
                 '  <-- every unit count is UNCHECKED'))
        for k, v in sorted(q['exclusion_summary'].items(), key=lambda kv: -kv[1]):
            print(f'    {v:>3} excluded: {k}')
        print()
        print('  QUALIFYING:')
        for c in sorted(q['qualifying'], key=lambda c: -c['units']):
            print(f'    {c["units"]:>14,.2f} {c["token"]:<4} {c["cik"]:<9} '
                  f'{(c["name"] or "")[:44]:<46}{c["token_from"]}')
        if q.get('flagged_for_verification'):
            print()
            print('  !! FLAGGED FOR VERIFICATION - a holding larger than any known corporate')
            print('     treasury. Check the unit count against the filing before this is used:')
            for c in q['qualifying']:
                if c.get('supply_flag'):
                    print(f'       {c["name"]}')
                    print(f'       {c["supply_flag"]}')
        print(f'\n  written to {OUT}/treasury_universe.json')
        print('  NOTE: units are not comparable across tokens. A filer with 5,700,049 units of '
              'ether\n  is not larger than one with 846,000 units of bitcoin. Ranking by units '
              'is for\n  eyeballing the list only; the index ranks by NAV.')
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == '--identify':
        u = json.load(io.open(f'{OUT}/treasury_universe.json', encoding='utf-8'))
        rel = json.load(io.open('data/relative.json', encoding='utf-8'))
        px = prices_from_relative(rel)
        res = identify_unknowns(u, px)
        ok = [r for r in res if r.get('inferred_token')]
        print(f'  {len(res)} filers with an unidentified unit string')
        print(f'  {len(ok)} identified from the implied price\n')
        print(f'  {"implied $/unit":>16}  {"token":<12}{"units":>18}  name')
        for r in sorted(res, key=lambda r: -(r.get('implied_price_usd') or 0)):
            ip = r.get('implied_price_usd')
            print(f'  {("$" + format(ip, ",.4f")) if ip is not None else "-":>16}  '
                  f'{(r.get("inferred_token") or "?"):<12}{r["units"]:>18,.2f}  {(r["name"] or "")[:44]}')
        io.open(f'{OUT}/treasury_identify.json', 'w', encoding='utf-8').write(json.dumps(res))
        print(f'\n  written to {OUT}/treasury_identify.json')
        print('  An inferred token is the ISSUER\'S OWN two numbers agreeing with a reference')
        print('  price, not a guess. Where nothing matched, the implied price is still shown:')
        print('  a person reading "$2.17 a unit" names the asset in a second.')
        sys.exit(0)
    if len(sys.argv) > 2 and sys.argv[1] == '--claims':
        reg = json.load(io.open('src/treasury/registry.json', encoding='utf-8'))
        want = sys.argv[2].upper()
        hit = [c for c in reg['companies'] if c['ticker'].upper() == want and c.get('cik')]
        if not hit:
            sys.exit(f'  {want}: not in the registry, or has no CIK')
        print_draft(hit[0]['cik'], hit[0]['ticker'])
        sys.exit(0)
    sys.exit(0 if main() else 1)
