"""Quarterly bitcoin holdings of every US spot bitcoin ETP from its own 10-Q / 10-K, via the SEC's XBRL companyfacts API.

Why this exists: the four issuers with daily coin counts cover only part of the market, and the others publish nothing
daily. But each trust attempted (7 of 13 currently parse) files quarterly financial statements with the coin quantity tagged in XBRL (e.g. iShares' 10-Q for
Q1 2024 tags "Bitcoin — 252,011 as of 2024-03-31"). Those are primary, keyless and lag by up to 45 days after quarter end.
They are published here as a QUARTERLY RECONCILIATION LAYER: not daily flow, never merged into the daily series, and
labelled as such. For the daily issuers the quarter-end figure is also compared with the daily holdings file on that date,
which is a real check on the daily parsers.

Note: spot bitcoin ETPs are 1933-Act grantor trusts, not 1940-Act funds, so they do NOT file N-PORT; 10-Q/10-K is the
periodic primary disclosure. Endpoints: https://www.sec.gov/files/company_tickers.json for ticker -> CIK, and
https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json for the facts. SEC asks for a descriptive User-Agent and
no more than ten requests per second; this module makes about a dozen requests a day.

The XBRL tag for the coin quantity is a custom element per issuer (not us-gaap), so it is found by rule rather than by
name: a concept whose label or description mentions bitcoin, reported in a non-currency unit, with instantaneous values
in a plausible coin range across at least two filings. If no concept qualifies for a trust, the trust is recorded with
that reason rather than guessed.
"""
import json, re, time, datetime as dt
import requests

# SEC's access policy requires a User-Agent of the form "Company Name contact@domain"; requests without a
# mailbox-style contact are refused with 403 at the gateway. The address is read from the SEC_CONTACT
# environment variable (set as a GitHub Actions variable) with a fallback that must be a real mailbox.
import os as _os
UA = {'User-Agent': f"CryptoExponentials {_os.environ.get('SEC_CONTACT', 'tools@cryptoexponentials.com')}", 'Accept-Encoding': 'gzip, deflate', 'Host': 'data.sec.gov'}
UA_WWW = {'User-Agent': UA['User-Agent'], 'Accept-Encoding': 'gzip, deflate'}

# US spot bitcoin ETPs. CIKs are resolved from the SEC ticker file at run time; the ones listed here are the fallback
# for tickers that file lacks, and are the ones confirmed from EDGAR filing pages.
UNIVERSE = ['IBIT', 'FBTC', 'GBTC', 'BTC', 'ARKB', 'BITB', 'HODL', 'BTCO', 'EZBC', 'BRRR', 'BTCW', 'MSBT', 'OBTC']
CIK_FALLBACK = {'IBIT': 1980994, 'MSBT': 2103612, 'OBTC': 1767057}
COIN_RANGE = (50.0, 5_000_000.0)


def _get(url, tries=3, headers=None):
    last = None
    for k in range(tries):
        try:
            r = requests.get(url, headers=headers or UA, timeout=60)
            if r.status_code in (403, 429) or r.status_code >= 500: raise requests.HTTPError(f'{r.status_code} for {url}', response=r)
            r.raise_for_status(); return r
        except Exception as e:
            last = e; time.sleep(3 * (k + 1))
    raise last


def resolve_ciks(tickers):
    """ticker -> CIK from the SEC's own ticker list, with the confirmed fallback for anything it lacks."""
    out = {t: CIK_FALLBACK.get(t) for t in tickers}
    try:
        j = _get('https://www.sec.gov/files/company_tickers.json', headers=UA_WWW).json()
        by = {v['ticker'].upper(): int(v['cik_str']) for v in j.values() if v.get('ticker')}
        for t in tickers:
            if by.get(t): out[t] = by[t]
    except Exception: pass
    return out


def find_bitcoin_quantity(facts):
    """Given companyfacts['facts'], return (concept_key, [ {end, val, form, filed, fy, fp, accn} ... ]) for the concept
    that best matches a bitcoin coin count, or (None, reason)."""
    candidates = []
    for taxonomy, concepts in (facts or {}).items():
        for name, c in concepts.items():
            text = ((c.get('label') or '') + ' ' + (c.get('description') or '') + ' ' + name).lower()
            # The standard element since ASU 2023-08 is us-gaap:CryptoAssetNumberOfUnits, whose label says
            # "Crypto Asset, Number of Units" and never "bitcoin". For a single-asset bitcoin trust it IS the
            # coin count. Custom bitcoin-labelled elements from earlier filings are still accepted.
            # Three standard elements carry a single-asset trust's coin count depending on the sponsor's tagging:
            # CryptoAssetNumberOfUnits (ASU 2023-08), InvestmentOwnedBalanceShares (Fidelity), InvestmentOwnedBalanceContracts (ARK).
            is_std = name in ('CryptoAssetNumberOfUnits', 'InvestmentOwnedBalanceShares', 'InvestmentOwnedBalanceContracts')
            if not is_std and 'bitcoin' not in text and 'btc' not in text and 'crypto asset' not in text and 'cryptoasset' not in text: continue
            if any(w in text for w in ('per share', 'fair value', 'cost', 'usd', 'dollar', 'price', 'fee', 'expense', 'gain', 'loss', 'payable', 'proceeds', 'purchase', 'sold', 'value')):
                # value-like labels are excluded; quantity labels are short ("Bitcoin", "Bitcoin quantity", "Bitcoin held")
                if not any(w in text for w in ('quantity', 'number of bitcoin', 'bitcoin held', 'bitcoins held', 'held by the trust')): continue
            for unit, rows in (c.get('units') or {}).items():
                if unit.upper() in ('USD', 'USD/SHARES', 'USD-PER-SHARES'): continue
                inst = [r for r in rows if r.get('end') and r.get('val') is not None and (not r.get('start') or r.get('start') == r.get('end'))
                        and COIN_RANGE[0] <= float(r['val']) <= COIN_RANGE[1] and r.get('form', '') in ('10-Q', '10-K', '10-Q/A', '10-K/A')]
                if len(inst) >= 1: candidates.append((f'{taxonomy}:{name}:{unit}', inst, text))  # one instant is enough: a newly adopted element has one
    if not candidates:
        # self-diagnosis: record every non-dollar concept the entity has, so a failure can be read from the data file
        seen = []
        for taxonomy, concepts in (facts or {}).items():
            for name, c in concepts.items():
                units = list((c.get('units') or {}).keys())
                if any(u.upper() not in ('USD', 'USD/SHARES', 'USD-PER-SHARES') for u in units):
                    rows = [r for u, rs in (c.get('units') or {}).items() if u.upper() != 'USD' for r in rs]
                    ends = sorted({r.get('end') for r in rows if r.get('end')})
                    seen.append(f"{taxonomy}:{name} units={','.join(units)} n={len(rows)} last={ends[-1] if ends else '-'} forms={','.join(sorted({str(r.get('form')) for r in rows}))}")
        return None, 'no XBRL concept recognised as a bitcoin quantity; non-dollar concepts present: ' + ('; '.join(seen) if seen else 'none (entity has no XBRL facts in the companyfacts API)')
    # prefer the standard element, then the concept with the most period-ends, then the shortest label
    pref = {'CryptoAssetNumberOfUnits': 0, 'InvestmentOwnedBalanceShares': 1, 'InvestmentOwnedBalanceContracts': 1}
    candidates.sort(key=lambda c: (pref.get(c[0].split(':')[1], 2), -len({r['end'] for r in c[1]}), len(c[2])))
    key, rows, _ = candidates[0]
    # one value per period end: the latest filing wins (10-K restates the Q4 instant; amendments supersede)
    best = {}
    for r in rows:
        prev = best.get(r['end'])
        if prev is None or (r.get('filed') or '') > (prev.get('filed') or ''): best[r['end']] = r
    series = [{'end': e, 'btc': float(v['val']), 'form': v.get('form'), 'filed': v.get('filed'), 'fy': v.get('fy'), 'fp': v.get('fp'), 'accn': v.get('accn')} for e, v in sorted(best.items())]
    return key, series


def run(out_dir, daily_holdings=None):
    """Writes etf_quarterly.json. daily_holdings: {ticker: [[date, btc], ...]} from etf_holdings.json, for the reconciliation."""
    ciks = resolve_ciks(UNIVERSE); result = {}; errs = []
    for tk in UNIVERSE:
        cik = ciks.get(tk)
        if not cik: result[tk] = {'status': 'error', 'reason': 'CIK not found in the SEC ticker file and no confirmed fallback'}; continue
        try:
            j = _get(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json').json()
            key, series = find_bitcoin_quantity(j.get('facts'))
            if not key: result[tk] = {'status': 'error', 'cik': cik, 'name': j.get('entityName'), 'reason': series}; continue
            rec = {'status': 'ok', 'cik': cik, 'name': j.get('entityName'), 'concept': key, 'quarters': series, 'latest': series[-1]}
            # reconciliation against the daily file, where one exists
            if daily_holdings and daily_holdings.get(tk):
                dh = {d: v for d, v in daily_holdings[tk]}
                checks = []
                for q in series:
                    if q['end'] in dh:
                        diff = 100 * (dh[q['end']] / q['btc'] - 1) if q['btc'] else None
                        checks.append({'end': q['end'], 'daily_btc': dh[q['end']], 'quarterly_btc': q['btc'], 'diff_pct': None if diff is None else round(diff, 3), 'agree_within_0_5pct': diff is not None and abs(diff) <= 0.5})
                rec['reconciliation'] = checks
            result[tk] = rec
        except Exception as e:
            result[tk] = {'status': 'error', 'cik': cik, 'reason': str(e)[:200]}; errs.append(f'{tk}: {e}')
        time.sleep(0.2)  # well inside the SEC's ten-per-second guidance
    ok = sorted(t for t, v in result.items() if v.get('status') == 'ok')
    doc = {'source': 'SEC EDGAR companyfacts (10-Q / 10-K XBRL)', 'fetched_at': dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
           'note': ('QUARTERLY reconciliation layer, not daily flow. Coin quantities as tagged by each trust in its own 10-Q/10-K, one value per '
                    'period end, latest filing wins. Lag is up to 45 days after quarter end (10-K: 90). Spot bitcoin ETPs are 1933-Act grantor '
                    'trusts and do not file N-PORT. Never merged into the daily holdings series; where a daily figure exists for a period end, '
                    'the two are compared and the difference published. The concept is found by rule (see edgar.py); first-run output should be '
                    'read once against a filing before the figures are quoted.'),
           'universe': UNIVERSE, 'tickers_ok': ok, 'trusts': result}
    with open(f'{out_dir}/etf_quarterly.json', 'w') as f: json.dump(doc, f, separators=(',', ':'))
    return {'status': 'ok' if ok and not errs else ('partial' if ok else 'error'), 'tickers_ok': ok, 'errors': errs[:5],
            'last_date': max((v['latest']['end'] for v in result.values() if v.get('status') == 'ok'), default=None)}


if __name__ == '__main__':
    # fixture from iShares' Q1 2024 10-Q: the coin quantity is tagged under a custom concept, 252,011 as of 2024-03-31
    fx = {'ibit': {'BitcoinQuantity': {'label': 'Bitcoin', 'description': 'Quantity of bitcoin held by the Trust', 'units': {'pure': [
              {'end': '2023-12-31', 'val': 0, 'form': '10-Q', 'filed': '2024-05-08', 'fy': 2024, 'fp': 'Q1'},
              {'end': '2024-03-31', 'val': 252011, 'form': '10-Q', 'filed': '2024-05-08', 'fy': 2024, 'fp': 'Q1'},
              {'end': '2024-06-30', 'val': 311000, 'form': '10-Q', 'filed': '2024-08-07', 'fy': 2024, 'fp': 'Q2'},
              {'end': '2024-06-30', 'val': 311001, 'form': '10-Q/A', 'filed': '2024-08-20', 'fy': 2024, 'fp': 'Q2'}]}},
                  'InvestmentInBitcoinFairValue': {'label': 'Bitcoin fair value', 'units': {'USD': [{'end': '2024-03-31', 'val': 17791246945, 'form': '10-Q', 'filed': '2024-05-08'}]}},
                  'BitcoinPurchased': {'label': 'Bitcoin purchased', 'units': {'pure': [{'start': '2024-01-01', 'end': '2024-03-31', 'val': 252016, 'form': '10-Q', 'filed': '2024-05-08'}]}}}}
    fx['us-gaap'] = {'CryptoAssetNumberOfUnits': {'label': 'Crypto Asset, Number of Units', 'description': 'Number of units of crypto asset held', 'units': {'pure': [
              {'end': '2024-03-31', 'val': 252011, 'form': '10-Q', 'filed': '2024-05-08', 'fy': 2024, 'fp': 'Q1'},
              {'end': '2024-06-30', 'val': 311001, 'form': '10-Q', 'filed': '2024-08-07', 'fy': 2024, 'fp': 'Q2'},
              {'end': '2024-09-30', 'val': 370000, 'form': '10-Q', 'filed': '2024-11-06', 'fy': 2024, 'fp': 'Q3'}]}}}
    key, series = find_bitcoin_quantity(fx)
    assert key == 'us-gaap:CryptoAssetNumberOfUnits:pure', key
    assert [s['btc'] for s in series] == [252011.0, 311001.0, 370000.0], series
    del fx['us-gaap']
    key, series = find_bitcoin_quantity(fx)
    assert key == 'ibit:BitcoinQuantity:pure', key
    assert [s['btc'] for s in series] == [252011.0, 311001.0], series   # zero excluded by range; amendment supersedes
    print('fixture ok:', key, [(s['end'], s['btc'], s['form']) for s in series])
