"""materiality.py - §10 and §11: is a candidate's crypto business material,
and which sleeve does it primarily belong to?

WHY THIS IS THE GATE

The candidate universe comes from full-text search, so it contains every filer
whose annual report mentions a crypto phrase - including a bank naming bitcoin
once in a risk factor. That is correct for §5 and useless as an index. Something
has to narrow it, and the narrowing must be a rule rather than a judgment or the
whole exercise reduces to a hand-list with extra steps.

WHAT IS MEASURABLE, AND WHAT IS NOT

Measurable from XBRL, for most filers:
  crypto assets over total assets      the balance-sheet share
  crypto revenue over total revenue    ONLY where the filer reports a crypto
                                       segment, which many do not

That second gap is the important one. §11B wants crypto revenue percentages,
and for a diversified issuer with no crypto segment there is no such figure to
read. Deriving one would be estimation, and §14 requires estimation to be
flagged rather than reported as measurement. So a company whose crypto business
is real but unsegmented comes out NOT PROVEN rather than immaterial, and the
distinction is carried into the output.

THRESHOLDS ARE CHOSEN FOR STABILITY, NOT FOR RESULT

§10 requires threshold sensitivity testing, and the criterion matters: a
threshold is selected for how little the classification moves when it moves,
never for what it does to a backtest. Each one below sits in a flat part of the
classification surface, and `sensitivity()` reports how many companies change
side when a threshold shifts, so the claim is checkable rather than asserted.
"""
import io, json, os, sys, urllib.request

from rules import is_fund, fund_reason

SEC = 'https://data.sec.gov'
UA = os.environ.get('SEC_CONTACT', 'research contact@example.com')
OUT = 'data'

# Balance-sheet crypto: the concepts a filer uses to report holdings at value.
# IndefiniteLivedIntangibleAssetsExcludingGoodwill WAS IN THIS LIST AND SHOULD
# NEVER HAVE BEEN. Before ASU 2023-08 crypto was accounted for as an
# indefinite-lived intangible, so the element sometimes carries a holding - but
# it is the GENERAL element for trademarks, licences and brand names, and most
# filers using it hold no crypto at all.
#
# The cost of that mistake, from the live run: NovaBay Pharmaceuticals came out
# at 93.5% "treasury", Greenlane Holdings at 184.8%, CDT Equity at 781.3%. A
# pharmaceutical company was about to be classified as a bitcoin treasury
# because its trademarks are large relative to its balance sheet.
#
# A pre-2024 holding is better missed than a brand name counted. Missing it
# produces "not-proven", which is honest; counting it produces a confident
# classification that is simply wrong.
CRYPTO_ASSET_CONCEPTS = ('CryptoAssetFairValue', 'CryptoAssetFairValueNoncurrent',
                         'CryptoAssetCost')
TOTAL_ASSET_CONCEPTS = ('Assets',)
REVENUE_CONCEPTS = ('Revenues', 'RevenueFromContractWithCustomerExcludingAssessedTax',
                    'RevenueFromContractWithCustomerIncludingAssessedTax')

THRESHOLDS = {
    'asset_share_material': 0.10,     # crypto assets at a tenth of the balance sheet
    'asset_share_dominant': 0.50,     # or the majority of it
    'revenue_share_material': 0.10,
    'min_total_assets_usd': 10_000_000,
    'why': ('A tenth is where a holding stops being incidental and starts moving the equity. '
            'A half is where the company is better described by the holding than by its '
            'operations. Both are round numbers chosen for STABILITY of classification - see '
            'sensitivity() - and never for what it does to a backtest.'),
}


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
    raise RuntimeError(str(last)[:120])


def _latest(facts, names, taxonomy='us-gaap', unit='USD', at_end=None):
    """The most recently FILED instant for the first concept that has one.

    Filing date, not period end, as everywhere else in this work: what the
    market knew, and when it could have known it.
    """
    for name in names:
        c = ((facts or {}).get(taxonomy) or {}).get(name)
        if not c:
            continue
        best = None
        for u, rows in (c.get('units') or {}).items():
            if str(u).upper() != unit:
                continue
            for r in rows:
                if r.get('val') is None or not r.get('filed'):
                    continue
                if at_end and r.get('end') != at_end:
                    continue
                key = (r.get('end') or '', r['filed'])
                if best is None or key > best[0]:
                    best = (key, {'value': float(r['val']), 'end': r.get('end'),
                                  'filed': r['filed'], 'form': r.get('form'),
                                  'concept': f'{taxonomy}:{name}'})
        if best:
            return best[1]
    return None


def measure(cik, facts=None):
    """Every materiality figure obtainable for one filer, each with its source
    and each absent where it cannot be read."""
    facts = facts if facts is not None else (
        _get(f'{SEC}/api/xbrl/companyfacts/CIK{int(cik):010d}.json').get('facts'))
    crypto = _latest(facts, CRYPTO_ASSET_CONCEPTS)
    # THE TWO FIGURES MUST DESCRIBE THE SAME BALANCE SHEET. Taking the newest
    # filed instant of each independently pairs a Q2 crypto holding with a
    # year-old total, and the ratio is then neither figure's truth. Total assets
    # are read at the crypto holding's period end, not at their own.
    assets = (_latest(facts, TOTAL_ASSET_CONCEPTS, at_end=crypto['end'])
              if crypto else _latest(facts, TOTAL_ASSET_CONCEPTS))
    revenue = _latest(facts, REVENUE_CONCEPTS)
    out = {'cik': int(cik),
           'crypto_assets_usd': crypto['value'] if crypto else None,
           'total_assets_usd': assets['value'] if assets else None,
           'revenue_usd': revenue['value'] if revenue else None,
           'sources': {k: v for k, v in (('crypto_assets', crypto), ('total_assets', assets),
                                         ('revenue', revenue)) if v}}
    if crypto and assets and assets['value']:
        share = crypto['value'] / assets['value']
        # A HOLDING CANNOT EXCEED THE BALANCE SHEET IT SITS ON. Above 1.0 the
        # two figures do not describe the same thing, whatever the elements
        # claim, and a screen that reports 781% has stopped measuring. The
        # answer is not-proven, with the arithmetic, rather than a number.
        if share > 1.0:
            out['crypto_asset_share'] = None
            out['asset_share_unavailable'] = (
                f'crypto assets ${crypto["value"]:,.0f} exceed total assets '
                f'${assets["value"]:,.0f} at {crypto.get("end")} ({share:.0%}). A holding cannot '
                f'be larger than the balance sheet it sits on, so the two figures do not '
                f'describe the same thing - most often a units mismatch, or an element that '
                f'carries something other than crypto.')
            out['impossible_share'] = round(share, 4)
        else:
            out['crypto_asset_share'] = share
    else:
        out['crypto_asset_share'] = None
        out['asset_share_unavailable'] = (
            'no crypto carrying value tagged' if not crypto else 'no total assets tagged')
    # §11B wants crypto revenue as a share. There is no standard element for it:
    # it lives in segment reporting, which companyfacts does not expose
    # dimensionally. Saying so is the honest answer; deriving one would be
    # estimation reported as measurement.
    out['crypto_revenue_share'] = None
    out['revenue_share_unavailable'] = (
        'crypto revenue is a SEGMENT disclosure and has no standard XBRL element; '
        'companyfacts flattens the dimensions that would carry it. Not measurable here, '
        'which is different from zero.')
    return out



def current_name(cik, cache=None):
    """The name the company files under NOW, plus what it used to be called.

    This universe is full of pivots. CIK 1389545 appears as "NovaBay
    Pharmaceuticals" in EDGAR full-text search and as "Stablecoin Development
    Corporation" in the XBRL frames - one pharmaceutical shell that became a
    crypto treasury, and two datasets disagreeing about what to call it.

    The screen was right about that company and the label made it look wrong,
    which is its own kind of error: a reader dismisses a correct finding
    because the name contradicts it. Former names are kept rather than
    discarded, because a pivot IS the interesting fact about several of these
    companies and hiding it would flatten the story into a list of treasuries.
    """
    if cache is not None and cik in cache:
        return cache[cik]
    try:
        d = _get(f'{SEC}/submissions/CIK{int(cik):010d}.json')
    except Exception:
        out = {'name': None, 'former_names': [], 'tickers': []}
        if cache is not None:
            cache[cik] = out
        return out
    former = []
    for f in (d.get('formerNames') or []):
        n = f.get('name')
        if n and n not in former:
            former.append(n)
    out = {'name': d.get('name'), 'former_names': former,
           'tickers': (d.get('tickers') or []),
           'sic_description': d.get('sicDescription')}
    if cache is not None:
        cache[cik] = out
    return out


def classify(m, signals=None, thresholds=None, name=None):
    """§9: one primary classification, by a deterministic rule.

    Returns (verdict, sleeve, why). `verdict` is one of material, immaterial,
    or not-proven - and the third is a real answer, not a failure. A company
    whose crypto business is genuine but unsegmented lands there, and calling
    it immaterial would be asserting something the filings do not say.
    """
    t = dict(THRESHOLDS); t.update(thresholds or {})
    sig = list(signals or [])
    # a fund's balance sheet IS its holding, so it scores 100% on the assets
    # screen and tops the list. The rule for this lives in rules.py because it
    # was written in the treasury sweep and not applied here, and that is the
    # third time today a rule has been correct in one module and absent in
    # another.
    marker = is_fund(name)
    if marker:
        return ('excluded', None, fund_reason(marker))
    share = m.get('crypto_asset_share')
    ta = m.get('total_assets_usd')

    if ta is not None and ta < t['min_total_assets_usd']:
        return ('immaterial', None,
                f'total assets ${ta:,.0f} below the ${t["min_total_assets_usd"]:,.0f} floor')

    if share is None:
        return ('not-proven', None,
                'crypto materiality is not measurable from this filer\'s XBRL: '
                + (m.get('asset_share_unavailable') or '')
                + '. That is not the same as immaterial, and it is not recorded as such.')

    if share >= t['asset_share_dominant']:
        sleeve = 'treasury'
        why = (f'crypto assets are {share:.0%} of the balance sheet, so the company is better '
               f'described by the holding than by its operations')
        if 'mining' in sig:
            why += '. It also signals mining, and §19 requires the double count to be settled ' \
                   'before both are counted'
        return ('material', sleeve, why)

    if share >= t['asset_share_material']:
        # material, but the holding does not define it: the sleeve comes from
        # what the business does, and where the signals disagree the answer is
        # that classification needs revenue data we cannot read
        sleeve = _sleeve_from_signals(sig)
        why = f'crypto assets are {share:.0%} of the balance sheet'
        if sleeve is None:
            why += ('; the primary sleeve cannot be settled from the balance sheet alone and '
                    'the text signals are ' + (', '.join(sig) if sig else 'absent'))
        return ('material', sleeve, why)

    return ('immaterial', None,
            f'crypto assets are {share:.1%} of the balance sheet, below the '
            f'{t["asset_share_material"]:.0%} threshold')


def _sleeve_from_signals(sig):
    """One signal is a classification. Several is not, and §9 wants exactly one
    primary sleeve - so a company signalling both mining and exchange returns
    None rather than the first in an arbitrary order."""
    s = {x for x in sig if x != 'treasury'}
    return next(iter(s)) if len(s) == 1 else None


def sensitivity(measures, signals_by_cik=None, deltas=(-0.05, -0.02, 0.02, 0.05)):
    """§10: how many companies change side when a threshold moves.

    The criterion for choosing a threshold is STABILITY - a flat region of the
    classification surface - never what it does to a return series. This is what
    makes that claim checkable.
    """
    base = {m['cik']: classify(m, (signals_by_cik or {}).get(m['cik']))[0] for m in measures}
    out = []
    for d in deltas:
        t = dict(THRESHOLDS)
        t['asset_share_material'] = max(0.0, t['asset_share_material'] + d)
        moved = sum(1 for m in measures
                    if classify(m, (signals_by_cik or {}).get(m['cik']), t)[0] != base[m['cik']])
        out.append({'threshold': round(t['asset_share_material'], 4), 'delta': d,
                    'changed': moved,
                    'share_changed': round(moved / len(measures), 4) if measures else 0})
    return {'base_threshold': THRESHOLDS['asset_share_material'], 'results': out,
            'reading': ('a threshold in a flat region moves few companies. If a small shift moves '
                        'many, the classification is an artefact of the threshold and the rule '
                        'is not stable enough to publish.')}


def main(universe_path=f'{OUT}/crypto_universe.json', out_dir=OUT, limit=0):
    u = json.load(io.open(universe_path, encoding='utf-8'))
    cands = u.get('candidates') or []
    sigs = {c['cik']: c.get('sleeve_signals') for c in cands}
    measures, rows, names = [], [], {}
    for c in (cands[:limit] if limit else cands):
        try:
            m = measure(c['cik'])
        except Exception as e:
            rows.append({**c, 'verdict': 'error', 'why': str(e)[:80]})
            continue
        measures.append(m)
        nm = current_name(c['cik'], names)
        # the CURRENT name decides the fund test too: a shell that became a
        # fund, or a fund that became an operating company, must be judged on
        # what it is now
        display = nm.get('name') or c.get('name')
        verdict, sleeve, why = classify(m, c.get('sleeve_signals'), name=display)
        rows.append({**c, **m, 'verdict': verdict, 'sleeve': sleeve, 'why': why,
                     'current_name': nm.get('name'), 'former_names': nm.get('former_names'),
                     'tickers': nm.get('tickers'), 'sic_description': nm.get('sic_description'),
                     'renamed': bool(nm.get('former_names'))})
    res = {'rows': rows, 'thresholds': THRESHOLDS,
           'sensitivity': sensitivity(measures, sigs),
           'counts': _counts(rows)}
    os.makedirs(out_dir, exist_ok=True)
    io.open(f'{out_dir}/crypto_materiality.json', 'w', encoding='utf-8').write(json.dumps(res))
    print(f'  {len(rows)} candidates screened')
    for k, v in sorted(res['counts'].items(), key=lambda kv: -kv[1]):
        print(f'    {v:>4}  {k}')
    print()
    mat = [r for r in rows if r['verdict'] == 'material']
    print(f'  {"share":>8}  {"sleeve":<16}{"cik":<10} name')
    for r in sorted(mat, key=lambda r: -(r.get('crypto_asset_share') or 0))[:40]:
        nm = r.get('current_name') or r.get('name') or ''
        was = (r.get('former_names') or [])
        tail = f'  (was {was[-1][:26]})' if was else ''
        print(f'  {(r.get("crypto_asset_share") or 0):>7.1%}  {(r.get("sleeve") or "unsettled"):<16}'
              f'{r["cik"]:<10} {nm[:38]:<40}{tail}')
    renamed = [r for r in mat if r.get('renamed')]
    if renamed:
        print()
        print(f'  {len(renamed)} of {len(mat)} material companies have filed under another name. '
              f'A pivot is')
        print('  the interesting fact about several of these, not a footnote.')
    s = res['sensitivity']
    print()
    print(f'  THRESHOLD SENSITIVITY around {s["base_threshold"]:.0%}:')
    for e in s['results']:
        print(f'    {e["threshold"]:.0%}  ({e["delta"]:+.0%})  {e["changed"]} of {len(measures)} '
              f'companies change verdict')
    return res


def _counts(rows):
    out = {}
    for r in rows:
        k = r.get('verdict', '?')
        out[k] = out.get(k, 0) + 1
    return out


if __name__ == '__main__':
    sys.exit(0 if main() else 1)
