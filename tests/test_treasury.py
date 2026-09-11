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
