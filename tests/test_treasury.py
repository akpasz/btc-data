"""test_treasury.py - offline tests for the CTI treasury module.

Every test here exists because the fault it checks either has happened in
production or is the specific way this calculation goes wrong quietly. None of
them need network access: they run against fixtures shaped like SEC XBRL.
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'fetch'))
import treasury as T


# ----------------------------------------------------------------- fixtures ---

def facts_with(units_rows, unit='Bitcoin', concept='CryptoAssetNumberOfUnits'):
    return {'us-gaap': {concept: {'label': None, 'units': {unit: units_rows}}}}


ROWS = [
    {'end': '2025-03-31', 'val': 100.0, 'form': '10-Q', 'filed': '2025-05-05', 'accn': 'a1'},
    {'end': '2025-06-30', 'val': 150.0, 'form': '10-Q', 'filed': '2025-08-04', 'accn': 'a2'},
]


class TestExtraction:
    def test_null_label_is_not_a_rejection(self):
        """CryptoAssetNumberOfUnits ships with a null label and the token name
        as its unit. An extractor that filters on the label finds nothing and
        reports "no crypto", which is a false negative that reads exactly like
        a fact. That shipped once and hid a 700,000 BTC position."""
        s = T.token_series(facts_with(ROWS), 'BTC')
        assert len(s) == 2 and s[-1]['units'] == 150.0

    def test_the_unit_string_separates_a_multi_token_balance_sheet(self):
        f = facts_with(ROWS, unit='Bitcoin')
        f['us-gaap']['CryptoAssetNumberOfUnits']['units']['Ethereum'] = [
            {'end': '2025-06-30', 'val': 9000.0, 'form': '10-Q', 'filed': '2025-08-04'}]
        assert T.token_series(f, 'BTC')[-1]['units'] == 150.0
        assert T.token_series(f, 'ETH')[-1]['units'] == 9000.0
        assert T.token_series(f, 'SOL') == []

    def test_a_restatement_does_not_erase_what_was_believed_before_it(self):
        """This test previously asserted that the later filing simply wins.
        That was wrong, and the MSTR data proved it: collapsing a period end to
        its newest filing also moves the DATE the fact became knowable, which
        made the series lag reality by two quarters.

        A changed value is two facts. Both are kept, each from the date it was
        filed, and `as_of` chooses by date."""
        rows = ROWS + [{'end': '2025-06-30', 'val': 148.0, 'form': '10-K/A',
                        'filed': '2026-01-15', 'accn': 'a3'}]
        s = T.token_series(facts_with(rows), 'BTC')
        vals = sorted(r['units'] for r in s if r['end'] == '2025-06-30')
        assert vals == [148.0, 150.0]
        assert T.as_of(s, '2025-09-01', 'units')[0] == 150.0, 'what was believed then'
        assert T.as_of(s, '2026-02-01', 'units')[0] == 148.0, 'what replaced it'

    def test_duration_facts_are_not_balances(self):
        rows = ROWS + [{'start': '2025-04-01', 'end': '2025-06-30', 'val': 50.0,
                        'form': '10-Q', 'filed': '2025-08-04'}]
        assert len(T.token_series(facts_with(rows), 'BTC')) == 2


class TestPointInTime:
    def test_as_of_uses_the_filing_date_not_the_period_end(self):
        """The single easiest way to build look-ahead into a treasury backtest,
        and it looks entirely reasonable while you do it: on 1 July the market
        did not know the 30 June holding. It was filed on 4 August."""
        s = T.token_series(facts_with(ROWS), 'BTC')
        assert T.as_of(s, '2025-07-01', 'units')[0] == 100.0, 'June figure leaked into July'
        assert T.as_of(s, '2025-08-04', 'units')[0] == 150.0
        assert T.as_of(s, '2025-05-04', 'units')[0] is None, 'nothing was knowable yet'

    def test_a_claim_is_not_netted_before_it_is_disclosed(self):
        claims = [{'instrument': 'notes', 'amount_usd': 1000.0, 'attribution': 'treasury',
                   'from': '2025-06-01', 'source': 'accn, Note 7'}]
        assert T.net_nav(5000.0, claims, '2025-05-01')[1] == 5000.0
        assert T.net_nav(5000.0, claims, '2025-07-01')[1] == 4000.0


class TestClaims:
    def test_ambiguous_claims_make_a_band_not_a_point(self):
        """A convertible whose proceeds funded both purchases and operations is
        genuinely arguable. A point estimate hides the disagreement; the band
        is its size."""
        claims = [{'instrument': 'A', 'amount_usd': 1000.0, 'attribution': 'treasury', 'source': 's'},
                  {'instrument': 'B', 'amount_usd': 500.0, 'attribution': 'ambiguous', 'source': 's'}]
        low, high, d = T.net_nav(5000.0, claims, '2026-01-01')
        assert (low, high) == (3500.0, 4000.0)
        assert d['ambiguous_usd'] == 500.0

    def test_an_unclassified_claim_is_reported_and_never_silently_dropped(self):
        """Omitting a claim understates the claims and flatters NAV, which is
        the direction of error that sells a story."""
        claims = [{'instrument': 'mystery note', 'amount_usd': 900.0, 'source': 's'}]
        low, high, d = T.net_nav(5000.0, claims, '2026-01-01')
        assert (low, high) == (5000.0, 5000.0)
        assert d['unclassified'] == ['mystery note']

    def test_operating_claims_are_not_netted(self):
        claims = [{'instrument': 'revolver', 'amount_usd': 800.0, 'attribution': 'operating', 'source': 's'}]
        assert T.net_nav(5000.0, claims, '2026-01-01')[0] == 5000.0


class TestAccretion:
    def test_buying_more_while_diluting_more_is_named_as_such(self):
        """The distinction §17 exists for: a company can double its holdings
        and leave every holder worse off. Three of the four cases look like
        success in a press release."""
        rows = [{'filed': '2025-05-05', 'units': 100.0, 'diluted_shares': 1000.0,
                 'tokens_per_diluted_share': 0.10},
                {'filed': '2025-08-04', 'units': 150.0, 'diluted_shares': 2000.0,
                 'tokens_per_diluted_share': 0.075}]
        a = T.accretion(rows)[0]
        assert a['units_change'] == 50.0 and a['verdict'] == 'dilutive despite buying'

    def test_genuine_accretion(self):
        rows = [{'filed': '2025-05-05', 'units': 100.0, 'diluted_shares': 1000.0,
                 'tokens_per_diluted_share': 0.10},
                {'filed': '2025-08-04', 'units': 150.0, 'diluted_shares': 1200.0,
                 'tokens_per_diluted_share': 0.125}]
        assert T.accretion(rows)[0]['verdict'] == 'accretive'


class TestMnav:
    def test_mnav_inherits_the_band(self):
        m = T.mnav(10000.0, 4000.0, 5000.0)
        assert round(m['low'], 2) == 2.0 and round(m['high'], 2) == 2.5

    def test_negative_nav_is_undefined_not_a_number(self):
        """A company whose claims exceed its holdings has no meaningful
        multiple. Returning one would be worse than returning nothing."""
        m = T.mnav(10000.0, -500.0, -100.0)
        assert m['low'] is None and m['high'] is None and m['undefined_reason']


class TestPricing:
    def test_price_convention_is_mandatory(self):
        """A treasury NAV struck at a 00:00 UTC token price against an equity
        market cap struck at a 16:00 New York close compares two different
        moments. On a 5% intraday move that is a 5% error in mNAV, which is the
        central diagnostic of §18."""
        import pytest
        with pytest.raises(SystemExit):
            T.main(registry_path=os.devnull, prices={'BTC': {'values': []}})

    def test_price_lookup_never_uses_a_future_price(self):
        p = {'BTC': {'values': [['2025-05-01', 60000], ['2025-09-01', 80000]]}}
        assert T.price_at(p, 'BTC', '2025-06-01') == 60000
        assert T.price_at(p, 'BTC', '2025-04-01') is None


class TestRegistry:
    def test_registry_ships_with_no_guessed_values(self):
        """CIKs and claims are deliberately empty. A wrong CIK returns another
        company's facts and every downstream figure is confidently wrong; an
        assumed claim produces a net NAV nobody can source."""
        p = os.path.join(os.path.dirname(__file__), '..', 'src', 'treasury', 'registry.json')
        if not os.path.exists(p):
            import pytest; pytest.skip('registry not installed yet')
        reg = json.load(open(p, encoding='utf-8'))
        for c in reg['companies']:
            assert c.get('cik') is None or isinstance(c['cik'], int)
            for claim in c.get('claims') or []:
                assert claim.get('source'), f"{c['ticker']}: a claim without a filing citation is not evidence"
                assert claim.get('attribution') in ('treasury', 'operating', 'ambiguous')

    def test_non_sec_issuers_are_marked_and_not_invented(self):
        p = os.path.join(os.path.dirname(__file__), '..', 'src', 'treasury', 'registry.json')
        if not os.path.exists(p):
            import pytest; pytest.skip('registry not installed yet')
        reg = json.load(open(p, encoding='utf-8'))
        for c in reg['companies']:
            if c.get('listing') not in ('NASDAQ', 'NYSE', 'NYSE American'):
                assert c.get('source') == 'manual', f"{c['ticker']} is not an SEC filer and must say so"
                assert c.get('confidence') == 'low'


class TestNavBasisIsStated:
    """A field named net_nav that contains gross is the kind of label that gets
    quoted. With no claims recorded the two are equal, and for a leveraged
    issuer the difference is the entire point of the module."""

    def test_gross_is_labelled_gross(self):
        facts = facts_with(ROWS)
        T._get = lambda u, **k: {'facts': facts}
        c = T.company({'ticker': 'X', 'cik': 1, 'tokens': ['BTC'], 'claims': []},
                      {'convention': 'test', 'BTC': {'values': [['2025-01-01', 100000]]}})
        assert c['claims_populated'] is False
        assert c['nav_basis'].startswith('GROSS')
        assert all(r['nav_basis'].startswith('GROSS') for r in c['rows'])

    def test_net_is_labelled_net_once_a_claim_exists(self):
        facts = facts_with(ROWS)
        T._get = lambda u, **k: {'facts': facts}
        c = T.company({'ticker': 'X', 'cik': 1, 'tokens': ['BTC'],
                       'claims': [{'instrument': 'n', 'amount_usd': 1e6,
                                   'attribution': 'treasury', 'source': 's'}]},
                      {'convention': 'test', 'BTC': {'values': [['2025-01-01', 100000]]}})
        assert c['claims_populated'] is True and c['nav_basis'] == 'net'


class TestDuplicateQuotations:
    """§6. Metaplanet trades in Tokyo, on US OTC as MPJPY, and in Frankfurt as
    DN3.F. They are one economic exposure. Counting two of them would double
    count the same bitcoin, which is the exact failure §19 is written to
    prevent - and it is easy to do, because they have different tickers,
    different currencies and different share counts."""

    def _reg(self):
        import os, json
        p = os.path.join(os.path.dirname(__file__), '..', 'src', 'treasury', 'registry.json')
        if not os.path.exists(p):
            import pytest; pytest.skip('registry not installed')
        return json.load(open(p, encoding='utf-8'))

    def test_no_secondary_quotation_appears_as_its_own_company(self):
        reg = self._reg()
        tickers = {c['ticker'] for c in reg['companies']}
        secondary = set()
        for c in reg['companies']:
            secondary |= set((c.get('other_quotations') or {}).keys())
        clash = tickers & secondary
        assert not clash, f'{clash} is listed as a company AND as another company\'s quotation'

    def test_a_multi_listed_issuer_names_its_primary_security(self):
        for c in self._reg()['companies']:
            if c.get('other_quotations'):
                assert c.get('primary_security'), f"{c['ticker']}: §6 requires a named primary security"

    def test_an_adr_issuer_records_the_ratio_field(self):
        """Per-share figures on the ADR count differ from those on the ordinary
        count by the ratio. The field must exist even while it is null, so its
        absence is visible rather than assumed to be one."""
        for c in self._reg()['companies']:
            q = c.get('other_quotations') or {}
            if any('ADR' in str(v) for v in q.values()):
                assert 'adr_ratio' in c, f"{c['ticker']}: record the ADR ratio from the deposit agreement"


class TestSilentZeroIsNeverReturned:
    """A company that returns no rows must say why. A silent zero reads exactly
    like "holds no crypto", which is the most dangerous failure in a
    materiality screen: a large holder is excluded and the exclusion looks like
    a fact rather than a miss."""

    def test_an_empty_result_carries_a_diagnosis(self):
        facts = {'us-gaap': {'SomeOtherConcept': {'units': {'ETH': [
            {'end': '2026-06-30', 'val': 3000000.0, 'form': '10-Q', 'filed': '2026-08-04'}]}}}}
        T._get = lambda u, **k: {'facts': facts}
        c = T.company({'ticker': 'X', 'cik': 1, 'tokens': ['BTC'], 'claims': []},
                      {'convention': 'test'})
        assert c['rows'] == [] and c['status'] == 'no unit series found'
        d = c['diagnosis']
        assert 'ETH' in d['non_dollar_units_present']
        assert 'SomeOtherConcept' in ' '.join(d['non_dollar_concepts'])

    def test_the_diagnosis_names_what_was_looked_for(self):
        T._get = lambda u, **k: {'facts': {}}
        c = T.company({'ticker': 'X', 'cik': 1, 'tokens': ['SOL'], 'claims': []},
                      {'convention': 'test'})
        assert c['diagnosis']['looked_for_tokens'] == ['SOL']
        assert 'no non-dollar concept at all' in c['diagnosis']['likely_cause']


class TestFirstDisclosureNotLatestRestatement:
    """Found against real MSTR data. The 2025-12-31 balance was first reported
    in the 10-K filed 2026-02-19 and then repeated verbatim in two later 10-Qs.
    Keying on period end alone and taking the latest filing moved that fact to
    August, so a series asked what was known in February answered with the 2024
    balance - lagging reality by two quarters. That is a point-in-time error in
    the opposite direction from look-ahead, and it is just as wrong."""

    MSTR = [
        {'end': '2022-12-31', 'val': 132500.0, 'form': '10-K', 'filed': '2026-02-19'},
        {'end': '2023-12-31', 'val': 189150.0, 'form': '10-K', 'filed': '2026-02-19'},
        {'end': '2024-12-31', 'val': 447470.0, 'form': '10-K', 'filed': '2026-02-19'},
        {'end': '2025-12-31', 'val': 672500.0, 'form': '10-K', 'filed': '2026-02-19'},
        {'end': '2025-12-31', 'val': 672500.0, 'form': '10-Q', 'filed': '2026-05-06'},
        {'end': '2025-12-31', 'val': 672500.0, 'form': '10-Q', 'filed': '2026-08-03'},
        {'end': '2026-03-31', 'val': 762099.0, 'form': '10-Q', 'filed': '2026-05-06'},
        {'end': '2026-06-30', 'val': 846000.0, 'form': '10-Q', 'filed': '2026-08-03'},
    ]

    def test_a_repeated_figure_keeps_its_first_filing_date(self):
        s = T.token_series(facts_with(self.MSTR), 'BTC')
        row = [r for r in s if r['end'] == '2025-12-31']
        assert len(row) == 1, 'the same number repeated is one fact, not three'
        assert row[0]['filed'] == '2026-02-19', 'it became knowable at the 10-K, not the later 10-Q'

    def test_the_february_reader_sees_the_december_balance(self):
        s = T.token_series(facts_with(self.MSTR), 'BTC')
        assert T.as_of(s, '2026-02-19', 'units')[0] == 672500.0
        assert T.as_of(s, '2026-05-06', 'units')[0] == 762099.0
        assert T.as_of(s, '2026-08-03', 'units')[0] == 846000.0

    def test_nothing_is_knowable_before_the_first_filing(self):
        s = T.token_series(facts_with(self.MSTR), 'BTC')
        assert T.as_of(s, '2026-01-01', 'units')[0] is None, \
            'MSTR tagged 2022 and 2023 comparatives only in the 2026 10-K'

    def test_a_genuine_restatement_is_its_own_knowable_fact(self):
        """A repeated number is one fact. A CHANGED number is two: what was
        believed, and what replaced it, each from the date it was filed."""
        rows = [{'end': '2026-06-30', 'val': 846000.0, 'form': '10-Q', 'filed': '2026-08-03'},
                {'end': '2026-06-30', 'val': 840000.0, 'form': '10-Q/A', 'filed': '2026-11-10'}]
        s = T.token_series(facts_with(rows), 'BTC')
        assert len(s) == 2
        assert T.as_of(s, '2026-09-01', 'units')[0] == 846000.0
        assert T.as_of(s, '2026-12-01', 'units')[0] == 840000.0

    def test_one_filing_carrying_several_period_ends_answers_with_the_latest(self):
        """A 10-K reports four year ends on one date. Picking by filing date
        alone left the answer to iteration order."""
        s = T.token_series(facts_with(self.MSTR), 'BTC')
        v, meta = T.as_of(s, '2026-02-19', 'units')
        assert meta['end'] == '2025-12-31' and v == 672500.0


class TestGenericUnitsNeedADeclarationAndACheck:
    """BMNR tags the standard element with the units "Integer" and
    "cryptoAsset", which do not name the asset, and companyfacts flattens away
    the dimensional member that would. Getting the token wrong is not a small
    error: bitcoin and ether differ by a factor of forty."""

    GEN = [{'end': '2026-05-31', 'val': 5700049.0, 'form': '10-Q', 'filed': '2026-07-14'}]

    def test_a_generic_unit_is_ignored_without_a_declaration(self):
        f = facts_with(self.GEN, unit='cryptoAsset')
        assert T.token_series(f, 'ETH') == []
        assert T.token_series(f, 'ETH', declared=True)[0]['units'] == 5700049.0

    def test_the_declaration_is_recorded_as_such(self):
        f = facts_with(self.GEN, unit='Integer')
        assert T.token_series(f, 'ETH', declared=True)[0]['token_from'] == 'registry'

    def test_a_token_named_by_its_unit_needs_no_declaration(self):
        f = facts_with(self.GEN, unit='Ethereum')
        assert T.token_series(f, 'ETH')[0]['token_from'] == 'xbrl unit'

    def test_fair_value_reconciliation_identifies_the_token(self):
        """BMNR's real figures: 5,700,049 units against a reported fair value
        of $10.87bn is $1,907 a unit. That is ether. The same units read as
        bitcoin would be out by 98%, and the check says so."""
        rows = [{'end': '2026-05-31', 'filed': '2026-07-14', 'units': 5700049.0}]
        fv = {'2026-05-31': {'usd': 10871934000.0, 'filed': '2026-07-14'}}
        eth = T.reconcile_token(rows, fv, {'ETH': {'values': [['2026-01-01', 1907.0]]}}, 'ETH')[0]
        btc = T.reconcile_token(rows, fv, {'BTC': {'values': [['2026-01-01', 78000.0]]}}, 'BTC')[0]
        assert eth['ok'] is True and eth['off_by_pct'] < 1
        assert btc['ok'] is False and btc['off_by_pct'] > 90

    def test_fair_value_takes_the_first_disclosure_like_everything_else(self):
        f = {'us-gaap': {'CryptoAssetFairValue': {'units': {'USD': [
            {'end': '2026-05-31', 'val': 1.0e10, 'filed': '2026-07-14'},
            {'end': '2026-05-31', 'val': 1.0e10, 'filed': '2026-10-14'}]}}}}
        assert T.fair_value_series(f)['2026-05-31']['filed'] == '2026-07-14'


class TestPriceConvention:
    """§7 and §15: the convention must be published, and the mismatch against
    the equity close named rather than discovered later."""

    REL = {'series': {'btc_usd': [['2026-06-30', 78000.0], ['2026-08-03', 81000.0]],
                      'eth_usd': [['2026-05-31', 1907.0]],
                      'sol_usd': []}}

    def test_the_convention_travels_with_the_prices(self):
        p = T.prices_from_relative(self.REL)
        assert 'Coinbase' in p['convention'] and '00:00 UTC' in p['convention']
        assert 'New York close' in p['convention'], 'the pairing must be stated, not assumed'
        assert p['mismatch_hours'], 'a known non-simultaneity must be quantified'

    def test_an_empty_series_is_omitted_not_shipped_empty(self):
        """A price file that looks valid and prices nothing is worse than one
        that is missing: the first produces a zero NAV that reads as a fact."""
        p = T.prices_from_relative(self.REL)
        assert 'SOL' not in p and 'BTC' in p and 'ETH' in p

    def test_prices_are_never_read_from_the_future(self):
        p = T.prices_from_relative(self.REL)
        assert T.price_at(p, 'BTC', '2026-07-15') == 78000.0
        assert T.price_at(p, 'BTC', '2026-01-01') is None


class TestReconciliationUsesThePeriodEnd:
    """Found on BMNR's live figures. The reconciliation compares units against
    a fair value reported ON THE BALANCE SHEET DATE, so it must price at the
    period end. Pricing at the filing date - weeks later - measures how far the
    token moved in between and reports it as a reconciliation error.

    The signature was a consistent ~15% miss on two quarters and 0.9% on a
    third. A wrong TOKEN misses by a factor; a wrong DATE misses by a drift,
    and the size varies with how much the token moved."""

    ROWS = [{'end': '2026-05-31', 'filed': '2026-07-14', 'units': 5700049.0}]
    FV = {'2026-05-31': {'usd': 10871934000.0, 'filed': '2026-07-14'}}

    def test_it_prices_on_the_balance_sheet_date(self):
        p = {'ETH': {'values': [['2026-05-31', 1907.0], ['2026-07-14', 2600.0]]}}
        c = T.reconcile_token(self.ROWS, self.FV, p, 'ETH')[0]
        assert c['priced_at'] == '2026-05-31'
        assert c['ETH_price_usd'] == 1907.0, 'the filing-date price would be 2600'
        assert c['off_by_pct'] < 1 and c['ok'] is True

    def test_the_old_behaviour_would_have_flagged_a_correct_token(self):
        """Same data, priced at the filing date: a 36% miss on a declaration
        that is right. That is how a date bug looks like a data problem."""
        p = {'ETH': {'values': [['2026-05-31', 1907.0], ['2026-07-14', 2600.0]]}}
        wrong = abs(2600.0 - (self.FV['2026-05-31']['usd'] / self.ROWS[0]['units'])) / 2600.0
        assert wrong > 0.25, 'the bug was material, not cosmetic'

    def test_a_wrong_token_still_fails_loudly(self):
        p = {'BTC': {'values': [['2026-05-31', 78000.0]]}}
        c = T.reconcile_token(self.ROWS, self.FV, p, 'BTC')[0]
        assert c['ok'] is False and c['off_by_pct'] > 90


class TestClaimsDrafter:
    """Finding the candidate claims is mechanical. Classifying them is the
    judgment §15 requires, and the drafter must not fake it."""

    FACTS = {'us-gaap': {
        'ConvertibleNotesPayable': {'units': {'USD': [
            {'end': '2026-06-30', 'val': 3.0e9, 'form': '10-Q', 'filed': '2026-08-03', 'accn': 'a1'},
            {'end': '2026-06-30', 'val': 3.0e9, 'form': '10-K', 'filed': '2027-02-01', 'accn': 'a2'}]}},
        'PreferredStockLiquidationPreferenceValue': {'units': {'USD': [
            {'end': '2026-06-30', 'val': 1.0e9, 'form': '10-Q', 'filed': '2026-08-03', 'accn': 'a1'}]}},
        'Revenues': {'units': {'USD': [
            {'start': '2026-01-01', 'end': '2026-06-30', 'val': 5.0e8, 'filed': '2026-08-03'}]}}}}

    def test_it_finds_debt_and_preferred_and_ignores_income(self):
        T._get = lambda u, **k: {'facts': self.FACTS}
        rows = T.draft_claims(1)
        kinds = {r['kind'] for r in rows}
        assert kinds == {'convertible', 'preferred'}, 'a duration fact is not a claim'

    def test_it_carries_the_citation_every_claim_needs(self):
        T._get = lambda u, **k: {'facts': self.FACTS}
        for r in T.draft_claims(1):
            assert r['accn'] and r['form'] and r['filed'], 'an entry without a source is not evidence'

    def test_it_uses_first_disclosure_like_the_holdings_do(self):
        T._get = lambda u, **k: {'facts': self.FACTS}
        conv = [r for r in T.draft_claims(1) if r['kind'] == 'convertible']
        assert len(conv) == 1 and conv[0]['filed'] == '2026-08-03', \
            'the 10-K repeating the figure does not make it newly knowable'

    def test_it_never_proposes_an_attribution(self):
        """The one thing it must not do. A guessed attribution becomes a number
        on a page and the guess stops being visible."""
        T._get = lambda u, **k: {'facts': self.FACTS}
        for r in T.draft_claims(1):
            assert 'attribution' not in r


class TestPredecessorEntityGuard:
    """Found on ABTC. Its holdings run to a 2026-06-30 period end while its
    claims stop at 2025-06-30, with notes going back to 2021 at a scale that
    belongs to the predecessor. A CIK survives a reverse merger; the company
    does not. Netting pre-combination debt against post-combination bitcoin
    subtracts a different company's claims."""

    def test_a_wide_gap_is_flagged(self):
        claims = [{'end': '2025-06-30'}, {'end': '2021-12-31'}]
        holds = [{'held': {'BTC': {'period_end': '2026-06-30'}}}]
        g = T.claims_holdings_gap(claims, holds)
        assert g and g['gap_days'] == 365
        assert g['oldest_claim_period'] == '2021-12-31'
        assert 'PREDECESSOR' in g['warning']

    def test_a_normal_reporting_lag_is_not_flagged(self):
        """One quarter between the newest claim and the newest holding is
        ordinary; flagging it would train the reader to ignore the warning."""
        claims = [{'end': '2026-03-31'}]
        holds = [{'held': {'BTC': {'period_end': '2026-06-30'}}}]
        assert T.claims_holdings_gap(claims, holds) is None

    def test_it_says_nothing_when_there_is_nothing_to_compare(self):
        assert T.claims_holdings_gap([], [{'held': {'BTC': {'period_end': '2026-06-30'}}}]) is None
        assert T.claims_holdings_gap([{'end': '2025-06-30'}], []) is None


class TestAccretingClaims:
    """XXI's convertible carries 484,326,591 at 2025-12-31 and 484,543,716 six
    months later. One instrument, several carrying amounts. A single figure
    would net a 2026 balance against a 2025 holding, and entering each period
    as its own claim would count a half-billion obligation three times."""

    XXI = {'instrument': 'ConvertibleLongTermNotesPayable', 'attribution': 'treasury',
           'source': 's', 'from': '2026-03-31',
           'schedule': [{'from': '2026-03-31', 'amount_usd': 484326591.0},
                        {'from': '2026-05-13', 'amount_usd': 484434554.0},
                        {'from': '2026-08-11', 'amount_usd': 484543716.0}]}

    def test_the_carrying_amount_is_read_point_in_time(self):
        assert T._claim_amount(self.XXI, '2026-04-01') == 484326591.0
        assert T._claim_amount(self.XXI, '2026-06-01') == 484434554.0
        assert T._claim_amount(self.XXI, '2026-09-01') == 484543716.0

    def test_nothing_is_netted_before_the_first_disclosure(self):
        assert T._claim_amount(self.XXI, '2026-01-01') == 0.0
        low, high, _ = T.net_nav(3.0e9, [self.XXI], '2026-01-01')
        assert low == high == 3.0e9

    def test_a_flat_claim_still_works(self):
        flat = {'instrument': 'note', 'amount_usd': 1.0e6, 'attribution': 'treasury', 'source': 's'}
        assert T._claim_amount(flat, '2030-01-01') == 1.0e6

    def test_net_nav_uses_the_amount_in_force(self):
        low, high, d = T.net_nav(3.0e9, [self.XXI], '2026-06-01')
        assert high == 3.0e9 - 484434554.0
        assert d['claims_used'][0]['amount_usd'] == 484434554.0

    def test_the_drafter_groups_periods_into_one_instrument(self):
        facts = {'us-gaap': {'ConvertibleLongTermNotesPayable': {'units': {'USD': [
            {'end': '2025-12-31', 'val': 484326591.0, 'form': '10-K', 'filed': '2026-03-31', 'accn': 'a1'},
            {'end': '2026-03-31', 'val': 484434554.0, 'form': '10-Q', 'filed': '2026-05-13', 'accn': 'a2'},
            {'end': '2026-06-30', 'val': 484543716.0, 'form': '10-Q', 'filed': '2026-08-11', 'accn': 'a3'}]}}}}
        T._get = lambda u, **k: {'facts': facts}
        rows = T.draft_claims(1)
        assert len({r['concept'] for r in rows}) == 1, 'three periods of one instrument'
        assert len(rows) == 3, 'each period is still listed, to become a schedule entry'


class TestEquityAndMnav:
    """mNAV pairs an equity close with a token close taken 3-4 hours later, so
    it inherits that mismatch. Both conventions are published."""

    EQ = {'convention': 'test', 'MSTR': {'values': [['2026-08-03', 300.0]]}}

    def test_market_cap_uses_basic_shares(self):
        """§18's primary definition is equity market capitalisation - what the
        market actually capitalises. The diluted count belongs in NAV per
        share, where the claim on the assets is the question. Two different
        questions that look like one."""
        assert T.market_cap_at(self.EQ, 'MSTR', '2026-08-03', 1.0e6) == 3.0e8

    def test_a_missing_equity_price_costs_an_mnav_not_an_issuer(self):
        assert T.market_cap_at(self.EQ, 'NOSUCH', '2026-08-03', 1.0e6) is None
        assert T.market_cap_at(self.EQ, 'MSTR', '2020-01-01', 1.0e6) is None

    def test_mnav_inherits_the_nav_band(self):
        m = T.mnav(3.0e9, 2.0e9, 2.5e9)
        assert round(m['low'], 2) == 1.20 and round(m['high'], 2) == 1.50

    def test_the_equity_convention_names_the_mismatch(self):
        assert 'New York' in T.EQUITY_CONVENTION and '00:00 UTC' in T.EQUITY_CONVENTION

    def test_a_non_us_listing_is_skipped_not_guessed(self):
        """3350.T needs a Tokyo market code and a JPY conversion. Fetching it
        as a US symbol would return either nothing or the wrong company."""
        reg = {'companies': [{'ticker': '3350.T', 'source': 'manual'},
                             {'ticker': 'MPJPY'}]}
        T.equity_series = lambda t, market='us': [['2026-01-01', 1.0]]
        eq = T.load_equity_prices(reg)
        assert '3350.T' not in eq


def _u(pairs):
    return [{'ticker': t, 'name': t, 'claims_populated': True,
             'rows': [{'filed': '2026-08-03', 'gross_nav_usd': n, 'market_cap_usd': n * 1.5,
                       'nav_basis': 'net'}]} for t, n in pairs]


class TestCapping:
    """Three attempts at this. The first turned a 77.8% raw weight into 11.0%
    and a 0.4% raw weight into 22.5%; the second put the smallest holding above
    the largest. Both passed a casual reading. These are the assertions that
    would have caught them."""

    EIGHT = _u((('A', 53.7e9), ('B', 10.8e9), ('C', 3.0e9), ('D', 0.5e9),
                ('E', 0.4e9), ('F', 0.3e9), ('G', 0.2e9), ('H', 0.1e9)))

    def test_weights_sum_to_one(self):
        for u in (self.EIGHT, _u([(chr(65 + k), 1e9 * (20 - k)) for k in range(20)])):
            ms = T.build_cti(u)['members']
            assert abs(sum(m['weight'] for m in ms) - 1.0) < 1e-6

    def test_no_member_exceeds_the_single_cap(self):
        ms = T.build_cti(self.EIGHT)['members']
        assert max(m['weight'] for m in ms) <= T.CTI_RULES['single_name_cap'] + 1e-6

    def test_order_is_preserved(self):
        """The one that matters. A cap meant to limit concentration must never
        lift a smaller holding above a larger one."""
        ms = T.build_cti(self.EIGHT)['members']
        for a, b in zip(ms, ms[1:]):
            assert a['raw_weight'] >= b['raw_weight'] - 1e-9
            assert a['weight'] >= b['weight'] - 1e-9

    def test_an_infeasible_top_five_cap_is_reported_not_forced(self):
        """On eight members a 55% top-five limit would require the other three
        to carry 45%, more than any capped constituent. No solution keeps the
        ranking, so it is recorded as a breach with its cause."""
        cap = T.build_cti(self.EIGHT)['capping']
        b = cap['top5_breach']
        assert b and b['top_five'] > b['limit']
        assert 'cannot be met without lifting smaller holdings' in b['reason']

    def test_a_large_universe_has_no_breach(self):
        cap = T.build_cti(_u([(chr(65 + k), 1e9 * (30 - k)) for k in range(20)]))['capping']
        assert cap['top5_breach'] is None

    def test_a_cap_unreachable_on_a_tiny_universe_is_not_pretended(self):
        """Three names at a 15% cap reach 45%, not 100%. Applying it would mean
        inventing weight."""
        cap = T.build_cti(_u((('A', 9e9), ('B', 1e9), ('C', 0.5e9))))['capping']
        assert cap['single_applied'] is False
        assert 'unreachable on a universe this small' in cap['single_skipped_reason']

    def test_exclusions_carry_their_reason(self):
        u = self.EIGHT + _u((('TINY', 1e6),))
        idx = T.build_cti(u)
        assert any('below the' in e['reason'] for e in idx['excluded'])

    def test_weight_is_nav_not_market_cap(self):
        """Weighting by market capitalisation would weight by the treasury
        premium, which is the thing CTP measures. An index whose weights move
        with the premium cannot then report on it."""
        u = _u((('A', 1e9), ('B', 1e9)))
        u[0]['rows'][0]['market_cap_usd'] = 10e9      # a huge premium on A
        ms = T.build_cti(u)['members']
        assert abs(ms[0]['weight'] - ms[1]['weight']) < 1e-9, 'the premium moved the weight'


class TestUniverseQualification:
    """The frames sweep returns every filer tagging the concept, which includes
    trusts holding crypto for shareholders and filers whose unit string names
    no token. Both must come out, by rule and with a reason."""

    U = {'companies': [
        {'cik': 1980994, 'name': 'iShares Bitcoin Trust ETF', 'units': 734261.0,
         'unit_label': 'Bitcoin', 'period': 'p'},
        {'cik': 1050446, 'name': 'STRATEGY INC', 'units': 846000.0,
         'unit_label': 'Bitcoin', 'period': 'p'},
        {'cik': 862861, 'name': 'AI FINANCIAL CORPORATION', 'units': 7.28e9,
         'unit_label': 'Integer', 'period': 'p'},
        {'cik': 1829311, 'name': 'BITMINE IMMERSION', 'units': 5700049.0,
         'unit_label': 'cryptoAsset', 'period': 'p'},
        {'cik': 1604191, 'name': 'GRIDAI TECHNOLOGIES CORP', 'units': 0.0,
         'unit_label': 'pure', 'period': 'p'}]}
    REG = {'companies': [{'cik': 1829311, 'ticker': 'BMNR', 'token_declared': True,
                          'tokens': ['ETH']}]}

    def test_a_unit_string_is_mapped_not_truncated(self):
        """"bitcoin"[:3] is "bit", which is not a ticker and matches no price
        series. It shipped in the first version."""
        q = T.qualify_universe(self.U, self.REG)
        mstr = next(c for c in q['qualifying'] if c['cik'] == 1050446)
        assert mstr['token'] == 'BTC'

    def test_trusts_are_excluded_as_custody(self):
        q = T.qualify_universe(self.U, self.REG)
        ex = next(d for d in q['excluded'] if d['cik'] == 1980994)
        assert '19' in ex['reason'] and 'custody' in ex['reason']

    def test_an_unidentified_token_is_excluded_with_its_unit_named(self):
        q = T.qualify_universe(self.U, self.REG)
        ex = next(d for d in q['excluded'] if d['cik'] == 862861)
        assert 'Integer' in ex['reason'] and 'declared' in ex['reason']

    def test_a_registry_declaration_qualifies_a_generic_unit(self):
        q = T.qualify_universe(self.U, self.REG)
        b = next(c for c in q['qualifying'] if c['cik'] == 1829311)
        assert b['token'] == 'ETH' and b['token_from'] == 'registry declaration'

    def test_without_the_declaration_it_would_not_qualify(self):
        q = T.qualify_universe(self.U, {'companies': []})
        assert not any(c['cik'] == 1829311 for c in q['qualifying'])

    def test_a_zero_holding_is_excluded(self):
        q = T.qualify_universe(self.U, self.REG)
        assert any(d['cik'] == 1604191 for d in q['excluded'])

    def test_every_exclusion_carries_a_reason(self):
        q = T.qualify_universe(self.U, self.REG)
        assert all(d.get('reason') for d in q['excluded'])
        assert sum(q['exclusion_summary'].values()) == len(q['excluded'])


class TestSupplyPlausibility:
    """CleanSpark tagged 1,719,000 under the unit "Bitcoin" - 8.6% of every
    bitcoin mined, against a real holding near 12,500. At $78k it would have
    priced as $134bn and been most of the index before capping.

    My first fix was a 3% supply threshold, and the test below killed it:
    Strategy holds 4.25% of all bitcoin and BitMine 4.7% of all ether. A
    threshold catching the bad figure while sparing the real ones has four
    points of room, and Strategy keeps buying. So the supply share is a FLAG
    and the reconciliation is the gate."""

    def test_the_largest_honest_holders_are_not_rejected(self):
        """The assertion that killed the threshold approach."""
        assert T.supply_check('BTC', 846_000) is None, 'Strategy is 4.25% of supply and real'
        assert T.supply_check('ETH', 5_700_049) is None, 'BitMine is 4.7% of supply and real'

    def test_an_extraordinary_figure_is_flagged_not_excluded(self):
        r = T.supply_check('BTC', 1_719_000)
        assert r and 'flagged for verification' in r
        assert 'Not excluded on this alone' in r

    def test_the_reconciliation_catches_it_precisely(self):
        """No magic number needed. 1,719,000 against a $1bn carrying value
        implies $582 a coin; 846,000 against $53bn implies $63,000."""
        p = {'BTC': {'values': [['2026-06-30', 78000.0]]}}
        bad = T.price_plausible('BTC', 1_719_000, 1.0e9, p, '2026-06-30')
        good = T.price_plausible('BTC', 846_000, 53.7e9, p, '2026-06-30')
        assert bad and 'mis-tagged' in bad
        assert good is None

    def test_the_gate_needs_no_per_token_calibration(self):
        p = {'ETH': {'values': [['2026-05-31', 1907.0]]}}
        assert T.price_plausible('ETH', 5_700_049, 10.87e9, p, '2026-05-31') is None

    def test_it_says_nothing_without_a_fair_value(self):
        p = {'BTC': {'values': [['2026-06-30', 78000.0]]}}
        assert T.price_plausible('BTC', 1_719_000, None, p, '2026-06-30') is None

    def test_a_flagged_issuer_is_named_in_the_output(self):
        u = {'companies': [{'cik': 827876, 'name': 'CleanSpark, Inc.', 'units': 1_719_000.0,
                            'unit_label': 'Bitcoin', 'period': 'p'}]}
        q = T.qualify_universe(u, {'companies': []})
        assert 'CleanSpark, Inc.' in q['flagged_for_verification']


class TestTokenInference:
    """Two thirds of the universe tags a unit string naming nothing. Asking for
    eighteen human declarations is eighteen chances to be wrong with no way to
    check any. The filer reports a unit count AND a fair value; their quotient
    is a price, and a price identifies an asset."""

    P = {'BTC': {'values': [['2026-06-30', 78000.0]]},
         'ETH': {'values': [['2026-06-30', 1907.0]]},
         'SOL': {'values': [['2026-06-30', 95.0]]}}

    def test_it_identifies_from_the_issuers_own_numbers(self):
        tok, d = T.infer_token(5_700_049, 10.87e9, self.P, '2026-06-30')
        assert tok == 'ETH' and d['matched']['off_by_pct'] < 1

    def test_a_stablecoin_is_recognised_and_named_as_one(self):
        tok, d = T.infer_token(2_286_511_374, 2.29e9, self.P, '2026-06-30')
        assert tok == 'USD-STABLE'
        assert 'not crypto price exposure' in d['note']

    def test_an_ambiguous_match_stays_unidentified(self):
        """Two assets within tolerance cannot be told apart this way, and
        picking the closer one is a coin flip dressed as a measurement."""
        p = {'A': {'values': [['2026-06-30', 100.0]]}, 'B': {'values': [['2026-06-30', 103.0]]}}
        tok, d = T.infer_token(1000, 101_000.0, p, '2026-06-30')
        assert tok is None and len(d['candidates']) == 2
        assert 'cannot be distinguished' in d['reason']

    def test_an_unmatched_asset_still_reports_its_implied_price(self):
        """The row stays useful. "$2.17 a unit" is identifiable by a person;
        "Integer" is not."""
        tok, d = T.infer_token(230_520_792, 5.0e8, self.P, '2026-06-30')
        assert tok is None and round(d['implied_price_usd'], 2) == 2.17

    def test_the_bad_cleanspark_tag_is_not_identified_as_bitcoin(self):
        tok, d = T.infer_token(1_719_000, 1.0e9, self.P, '2026-06-30')
        assert tok is None and d['implied_price_usd'] < 1000

    def test_a_frame_label_maps_to_a_balance_sheet_date(self):
        """Pairing a holding with a carrying value from a different quarter
        would imply a price that is neither."""
        assert T._period_end('CY2026Q2I') == '2026-06-30'
        assert T._period_end('CY2025Q4I') == '2025-12-31'
        assert T._period_end('rubbish') is None

    def test_it_needs_both_numbers(self):
        assert T.infer_token(1000, None, self.P, '2026-06-30')[0] is None
        assert T.infer_token(None, 1000.0, self.P, '2026-06-30')[0] is None


class TestNonCalendarFiscalYears:
    """BitMine's balance-sheet date is 31 May; the frames API files it under
    CY2026Q2I anyway, because a filer with a non-calendar fiscal year is
    assigned to the nearest calendar quarter. Deriving 30 June from the frame
    label and demanding an exact match found NO fair value for BitMine, CEA
    Industries, AI Financial or Next Technology - five rows reported as having
    no carrying value when they all have one."""

    def test_the_rows_own_end_date_is_preferred(self):
        fv = {'2026-05-31': {'end': '2026-05-31', 'usd': 10.87e9, 'filed': 'f'}}
        assert T._nearest_fair_value(fv, '2026-05-31') is not None

    def test_a_few_days_apart_still_pairs(self):
        fv = {'2026-06-30': {'end': '2026-06-30', 'usd': 1.0, 'filed': 'f'}}
        assert T._nearest_fair_value(fv, '2026-07-02', days=10) is not None

    def test_a_different_quarter_never_pairs(self):
        """A unit count against a carrying value from another quarter implies a
        price that is neither."""
        fv = {'2026-03-31': {'end': '2026-03-31', 'usd': 1.0, 'filed': 'f'}}
        assert T._nearest_fair_value(fv, '2026-06-30', days=10) is None

    def test_the_frame_label_remains_a_fallback(self):
        assert T._period_end('CY2026Q2I') == '2026-06-30'
