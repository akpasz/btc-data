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
        amt = float(c.get('amount_usd') or 0)
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

def company(reg, prices, today=None):
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
            'shares_as_of': sh_meta['filed'] if sh_meta else None,
        })
    claims_done = bool(reg.get('claims'))
    recon = {}
    if declared:
        fv = fair_value_series(facts)
        for t, srs in per_token.items():
            r = reconcile_token(srs, fv, prices, t)
            if r:
                recon[t] = r
    return {'ticker': reg['ticker'], 'name': reg.get('name'), 'cik': reg.get('cik'),
            'source': 'sec-xbrl', 'tokens': tokens, 'rows': rows,
            'token_declared': declared, 'fair_value_reconciliation': recon,
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
    print(f'\n  Classify each as treasury / operating / ambiguous, then paste into the '
          f'registry\'s claims list. Template:')
    r = next((x for x in rows if x['end'] == latest), rows[0])
    print(json.dumps({'instrument': r['concept'].replace('us-gaap:', '') + f' at {r["end"]}',
                      'amount_usd': r['amount_usd'], 'attribution': 'treasury|operating|ambiguous',
                      'seniority': '', 'from': r['filed'], 'until': None,
                      'source': f'{r["accn"]}, {r["form"]} for {r["end"]}'}, indent=2))
    return rows


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
    out = {'schema_version': 1, 'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
           'price_convention': conv, 'companies': []}
    for c in reg.get('companies', []):
        try:
            out['companies'].append(company(c, prices))
        except Exception as e:
            out['companies'].append({'ticker': c.get('ticker'), 'status': 'error',
                                     'reason': str(e)[:200]})
    os.makedirs(out_dir, exist_ok=True)
    io.open(f'{out_dir}/treasury.json', 'w', encoding='utf-8').write(json.dumps(out))
    ok = sum(1 for c in out['companies'] if c.get('rows'))
    gross_only = [c['ticker'] for c in out['companies'] if c.get('rows') and not c.get('claims_populated')]
    print(f'  treasury: {ok} of {len(out["companies"])} issuers with a point-in-time series')
    for c in out['companies']:
        if c.get('diagnosis'):
            d = c['diagnosis']
            print(f'  treasury: {c["ticker"]} found nothing. units present: '
                  f'{", ".join(d["non_dollar_units_present"]) or "(none)"}')
    if gross_only:
        print(f'  treasury: GROSS ONLY, claims not yet recorded: {", ".join(gross_only)}')
        print('            these figures are not net of debt or preferred claims; do not publish them')
    return out


if __name__ == '__main__':
    # `python fetch/treasury.py --claims MSTR` lists that issuer's candidate
    # claims instead of running the pipeline. Finding them is mechanical;
    # classifying them is the judgment §15 requires and this will not fake it.
    if len(sys.argv) > 2 and sys.argv[1] == '--claims':
        reg = json.load(io.open('src/treasury/registry.json', encoding='utf-8'))
        want = sys.argv[2].upper()
        hit = [c for c in reg['companies'] if c['ticker'].upper() == want and c.get('cik')]
        if not hit:
            sys.exit(f'  {want}: not in the registry, or has no CIK')
        print_draft(hit[0]['cik'], hit[0]['ticker'])
        sys.exit(0)
    sys.exit(0 if main() else 1)
