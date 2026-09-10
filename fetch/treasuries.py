"""fetch/treasuries.py - bitcoin held by listed treasury companies and by
sovereigns, from PRIMARY sources only. Writes data/treasuries.json and
data/sovereigns.json.

The rule for inclusion is the same as everywhere else on this site: the
pipeline reads the figure itself, from the holder's own filing or the holder's
own published address. Figures that are only "widely reported" - press
estimates, aggregator sites, seized-coin guesses - are not here, and the
files say so. The residual on the flow monitor is everyone else.

TREASURY COMPANIES
    Same route as the ETFs in edgar.py: the SEC XBRL companyfacts API and the
    us-gaap:CryptoAssetNumberOfUnits concept. A company is listed only if that
    concept resolves. Strategy (MSTR) is the anchor; add CIKs as they file.

SOVEREIGNS
    El Salvador publishes its reserve address. The balance is read on chain
    from the same Blockchain.com API the pipeline already uses for price. No
    other sovereign publishes an address the pipeline can read, so no other
    sovereign is listed.
"""
import io, json, os, sys, datetime as dt
import urllib.request

OUT = os.environ.get('DATA_DIR', 'data')
sys.path.insert(0, os.path.dirname(__file__))

TREASURIES = {
    # ticker: (CIK, name)
    # Strategy's filings do not expose bitcoin under any of the three XBRL
    # elements edgar.find_bitcoin_quantity knows (CryptoAssetNumberOfUnits,
    # InvestmentOwnedBalanceShares, InvestmentOwnedBalanceContracts). The first
    # live run returned "no unit-count concept". They almost certainly use a
    # custom extension; it has to be read from a 10-Q and added to edgar.py.
    # Left in so the status stays visible on the page rather than vanishing.
    'MSTR': (1050446, 'Strategy Inc'),
}
# Empty, deliberately. The first version carried an El Salvador address written
# from memory; the chain reported a zero balance, which means it was wrong. An
# address goes in here only after it has been read from the holder's own
# publication and its balance checked by hand. Until then no sovereign is
# listed, and the flow page says so.
SOVEREIGNS = {}


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _write(name, doc):
    os.makedirs(OUT, exist_ok=True)
    tmp = os.path.join(OUT, name + '.json.tmp')
    io.open(tmp, 'w', encoding='utf-8').write(json.dumps(doc, separators=(',', ':')))
    os.replace(tmp, os.path.join(OUT, name + '.json'))


def treasuries():
    import edgar
    out = {'source': 'SEC XBRL companyfacts, us-gaap:CryptoAssetNumberOfUnits', 'fetched_at': _now(),
           'companies': {}, 'note': ('Only companies whose filings expose a machine-readable bitcoin unit '
                                     'count. A company that discloses only in prose is not here.')}
    for tk, (cik, name) in TREASURIES.items():
        try:
            facts = edgar._get(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json',
                               headers=edgar.UA).json().get('facts', {})
            key, rows = edgar.find_bitcoin_quantity(facts)
            # On failure find_bitcoin_quantity returns (None, reason) - a
            # STRING, not a list. Filtering it as a list swallowed the reason
            # and reported "no unit-count concept" for every kind of failure,
            # including a 403 from SEC. The reason is now recorded verbatim,
            # which is how this was finally diagnosed.
            if key is None:
                out['companies'][tk] = {'name': name, 'cik': cik, 'status': 'no unit-count concept',
                                        'reason': str(rows)[:400]}
                continue
            # find_bitcoin_quantity normalises the coin count to 'btc', not
            # 'val' - reading 'val' dropped every row, which is the whole of
            # why Strategy showed "no unit-count concept" for months while its
            # figure sat in the API under the standard element.
            rows = [r for r in rows if isinstance(r, dict) and r.get('btc') is not None]
            if not rows:
                out['companies'][tk] = {'name': name, 'cik': cik, 'status': 'concept found but no usable rows',
                                        'concept': key}
                continue
            last = max(rows, key=lambda r: r['end'])
            out['companies'][tk] = {'name': name, 'cik': cik, 'status': 'ok', 'concept': key,
                                    'btc': float(last['btc']), 'as_of': last['end'],
                                    'form': last.get('form'), 'filed': last.get('filed'),
                                    'quarters': [{'end': r['end'], 'btc': float(r['btc'])} for r in sorted(rows, key=lambda r: r['end'])][-12:]}
        except Exception as e:
            # the fetch itself failing is a different fact from the concept
            # being absent, and used to be indistinguishable on the page
            out['companies'][tk] = {'name': name, 'cik': cik, 'status': f'error: {type(e).__name__}: {str(e)[:200]}'}
    _write('treasuries', out)
    ok = [k for k, v in out['companies'].items() if v.get('status') == 'ok']
    print(f'  treasuries: {len(ok)} of {len(TREASURIES)} resolved ({", ".join(ok) or "none"})')
    return out


def sovereigns():
    out = {'source': 'published reserve addresses, balance read on chain', 'fetched_at': _now(),
           'holders': {}, 'note': ('Only holders that publish an address this pipeline can read. Governments '
                                   'that hold seized or purchased coin without a published address are '
                                   'absent by design, not by oversight.')}
    for name, spec in SOVEREIGNS.items():
        try:
            url = f"https://api.blockchain.info/q/addressbalance/{spec['address']}?confirmations=6"
            req = urllib.request.Request(url, headers={'User-Agent': 'CryptoExponentials tools@cryptoexponentials.com'})
            sats = int(urllib.request.urlopen(req, timeout=40).read().decode().strip())
            out['holders'][name] = {'address': spec['address'], 'btc': sats / 1e8, 'as_of': _now()[:10],
                                    'source': spec['source'], 'status': 'ok'}
        except Exception as e:
            out['holders'][name] = {'address': spec['address'], 'source': spec['source'],
                                    'status': f'error: {str(e)[:120]}'}
    _write('sovereigns', out)
    ok = [k for k, v in out['holders'].items() if v.get('status') == 'ok']
    print(f'  sovereigns: {len(ok)} of {len(SOVEREIGNS)} read on chain')
    return out


def main():
    treasuries()
    sovereigns()
    return 0


if __name__ == '__main__':
    sys.exit(main())
