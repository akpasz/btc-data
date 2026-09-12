"""universe.py - the crypto-equity universe, discovered by rule.

WHY THIS IS HARDER THAN THE TREASURY SLEEVE

Treasury companies are enumerable from one XBRL frames sweep because they tag a
specific concept: us-gaap:CryptoAssetNumberOfUnits. A holding is a fact with an
element behind it.

No other sleeve works that way. A miner does not tag "I am a miner". An
exchange does not tag "I am an exchange". There is no crypto SIC code. The
information that a company's BUSINESS is crypto lives in prose, not in a
tagged fact - which is exactly why most crypto-equity lists are hand-made, and
exactly what §5 forbids: a universe with no inclusion rule and invisible
omissions.

WHAT IS DISCOVERABLE

EDGAR full-text search indexes the text of every filing since 2001. A company
whose business involves crypto says so in its own 10-K, because it must: the
business description, the risk factors and the revenue discussion are all
required disclosures. So the rule is "every filer whose annual report
discusses this", and the candidate set is enumerable.

THE COST OF THAT RULE, STATED

It returns false positives by construction. A bank naming bitcoin once in a
risk factor is in the candidate set; so is a software company listing a
crypto customer. That is the correct behaviour for §5 - the candidate universe
should be over-inclusive and every exclusion documented - but it means the
candidate set is NOT the index, and treating it as one would be worse than a
hand-list, because it would be a hand-list with a false claim of rigour.

Materiality (§10, §11) does the narrowing, from XBRL financials rather than
from the text. This module discovers; it deliberately does not decide.
"""
import io, json, os, sys, time, datetime as dt, urllib.parse, urllib.request

from rules import is_fund

FTS = 'https://efts.sec.gov/LATEST/search-index?q={q}&dateRange=custom&startdt={s}&enddt={e}&forms={f}'
FTS_SIMPLE = 'https://efts.sec.gov/LATEST/search-index?q={q}&forms={f}'
FTS_DATED = FTS_SIMPLE + '&dateRange=custom&startdt={s}&enddt={e}'

# A DATE RULE, AND IT IS NOT ONLY ABOUT SPEED.
#
# Unbounded, "tokenization" matches 739 annual reports going back two decades -
# most of them companies with no crypto business now, several with none ever.
# Retrieving all of them would be 74 calls for one phrase and would fill the
# candidate set with history.
#
# The universe is meant to be companies whose CURRENT business involves crypto,
# and a company that still has one files an annual report saying so. So the
# rule is the most recent annual reports, which is both narrower and more
# relevant - not a compromise for speed but the correct definition.
#
# The cost, stated: a company that filed its last 10-K just outside the window
# is absent. Widening the window trades relevance for recall, and both numbers
# are printed so the trade is visible.
LOOKBACK_DAYS = 550          # a little over 18 months: every annual filer, once
UA = os.environ.get('SEC_CONTACT', 'research contact@example.com')
OUT = 'data'

# Phrases specific enough that a filer using them is discussing crypto as a
# business matter, not in passing. Each is quoted, so the search is for the
# phrase and not the words.
#
# Deliberately NOT included: "blockchain" alone, which every consultancy and
# logistics company has claimed at some point, and "digital asset" alone, which
# means a media file in half its uses. Precision here costs recall, and the
# materiality screen cannot fix a candidate set full of noise - it can only
# narrow one that is honest.
TERMS = [
    ('"bitcoin mining"', 'mining'),
    ('"hash rate"', 'mining'),
    ('"digital asset mining"', 'mining'),
    ('"digital asset exchange"', 'exchange'),
    ('"cryptocurrency exchange"', 'exchange'),
    ('"digital asset custody"', 'infrastructure'),
    ('"digital asset treasury"', 'treasury'),
    ('"bitcoin treasury"', 'treasury'),
    ('"stablecoin"', 'stablecoin'),
    ('"tokenization"', 'tokenization'),
    ('"crypto asset trading"', 'exchange'),
    ('"blockchain infrastructure"', 'infrastructure'),
]
FORMS = '10-K'


def _get(url, tries=3, pause=0.35):
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
                time.sleep(pause)
                return json.loads(raw.decode())
        except Exception as e:
            last = e
            time.sleep(pause * 2)
    raise RuntimeError(str(last)[:120])


def search(term, forms=FORMS, pages=40, days=LOOKBACK_DAYS):
    """Every filer whose filing of this form contains this phrase.

    Returns {cik: {name, hits, terms}}. Paginated: EDGAR returns 10 per page
    and caps the offset, so this is a sample of the matches rather than all of
    them where a term is very common - which is recorded, not hidden.
    """
    found, capped, e_total = {}, False, 0
    for page in range(pages):
        if days:
            end = dt.date.today()
            url = FTS_DATED.format(q=urllib.parse.quote(term), f=forms,
                                   s=(end - dt.timedelta(days=days)).isoformat(),
                                   e=end.isoformat())
        else:
            url = FTS_SIMPLE.format(q=urllib.parse.quote(term), f=forms)
        if page:
            url += f'&from={page * 10}'
        try:
            data = _get(url)
        except Exception:
            break
        hits = ((data.get('hits') or {}).get('hits') or [])
        if not hits:
            break
        total = ((data.get('hits') or {}).get('total') or {}).get('value') or 0
        if total > pages * 10:
            capped = True
            e_total = total
        for h in hits:
            src = h.get('_source') or {}
            for cik in (src.get('ciks') or []):
                c = int(cik)
                e = found.setdefault(c, {'cik': c, 'name': (src.get('display_names') or [''])[0],
                                         'hits': 0})
                e['hits'] += 1
    return found, capped, e_total


def discover(terms=None, forms=FORMS, pages=40, days=LOOKBACK_DAYS):
    """The candidate universe: every filer matching any term, with which terms
    matched and the sleeve each term suggests.

    A company matching several terms is ONE candidate with several signals, not
    several candidates. Which sleeve it belongs to is not decided here: §9 wants
    exactly one primary classification per company and that needs materiality,
    which needs financials. Suggesting one from a text match would be a guess
    wearing a rule's clothes.
    """
    terms = terms or TERMS
    out, capped_terms, failed = {}, [], []
    for term, sleeve in terms:
        try:
            found, capped, total = search(term, forms, pages, days)
        except Exception as e:
            failed.append(f'{term}: {str(e)[:60]}')
            continue
        if capped:
            capped_terms.append({'term': term, 'matches': total, 'retrieved': pages * 10})
        for cik, e in found.items():
            rec = out.setdefault(cik, {'cik': cik, 'name': e['name'], 'terms': [],
                                       'sleeve_signals': []})
            rec['terms'].append(term)
            if sleeve not in rec['sleeve_signals']:
                rec['sleeve_signals'].append(sleeve)
    # Funds belong in the CANDIDATE set - §5 wants it broad with documented
    # exclusions - and they are removed downstream. But they match eight or nine
    # phrases each, because a crypto ETF prospectus discusses mining, exchanges
    # and custody at length, so they crowd the top of the list and inflate the
    # headline. Counting them here keeps "412 candidates" from being read as
    # "412 crypto companies", which is the number that would get quoted.
    cands = sorted(out.values(), key=lambda r: (-len(r['terms']), r['name'] or ''))
    for c in cands:
        m = is_fund(c.get('name'))
        if m:
            c['fund_marker'] = m
    funds = sum(1 for c in cands if c.get('fund_marker'))
    return {
        'candidates': cands,
        'fund_candidates': funds,
        'operating_candidates': len(cands) - funds,
        'terms_searched': [t for t, _ in terms],
        'terms_capped': capped_terms,
        'terms_failed': failed,
        'forms': forms, 'lookback_days': days,
        'note': (
            'THIS IS A CANDIDATE SET, NOT AN INDEX. Full-text search returns every filer whose '
            'annual report contains the phrase, which by construction includes a bank naming '
            'bitcoin once in a risk factor. That over-inclusiveness is what §5 asks for - the '
            'candidate universe should be broad and every exclusion documented - but nothing '
            'here has been screened for materiality, and publishing it as a universe of crypto '
            'equities would be a hand-list with a false claim of rigour.'),
        'known_limits': [
            'EDGAR full-text search caps how deep a result set can be paged, so a very common '
            'term returns a sample rather than every match. The capped terms are listed.',
            'It searches TEXT. A company whose crypto business is real but described in words '
            'these phrases do not contain is absent, and absence here is not evidence.',
            'US filers only, as everywhere else in this work.',
        ],
    }


def main(out_dir=OUT, pages=40, days=LOOKBACK_DAYS):
    u = discover(pages=pages, days=days)
    os.makedirs(out_dir, exist_ok=True)
    io.open(f'{out_dir}/crypto_universe.json', 'w', encoding='utf-8').write(json.dumps(u))
    cs = u['candidates']
    print(f'  {len(cs)} candidate filers from {len(u["terms_searched"])} phrases in {u["forms"]}s '
          f'filed in the last {u.get("lookback_days")} days')
    print(f'  of which {u.get("fund_candidates", 0)} are funds, trusts or ETPs and will be '
          f'excluded downstream (§19), leaving {u.get("operating_candidates", 0)} operating '
          f'companies to screen')
    if u['terms_capped']:
        print(f'  {len(u["terms_capped"])} term(s) have more matches than were retrieved:')
        for c in u['terms_capped']:
            print(f'    {c["term"]:<30} {c["matches"]:>6} matches, {c["retrieved"]} retrieved')
        print('    THE CANDIDATE SET IS A SAMPLE FOR THESE TERMS, not the full universe.')
    if u['terms_failed']:
        print(f'  {len(u["terms_failed"])} term(s) failed: {u["terms_failed"][0]}')
    print()
    print(f'  {"terms":>6}  {"cik":<10} {"signals":<34} name')
    for c in cs[:50]:
        tag = 'FUND ' if c.get('fund_marker') else '     '
        print(f'  {len(c["terms"]):>6}  {tag}{c["cik"]:<10} {",".join(c["sleeve_signals"])[:30]:<32} '
              f'{(c["name"] or "")[:42]}')
    print()
    print('  A CANDIDATE SET, NOT AN INDEX. Nothing here is screened for materiality: a bank')
    print('  naming bitcoin once in a risk factor is in this list, and belongs in it until a')
    print('  documented rule removes it.')
    return u


if __name__ == '__main__':
    sys.exit(0 if main() else 1)
