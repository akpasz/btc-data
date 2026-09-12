"""Offline tests for materiality screening and sleeve classification."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'fetch'))
import materiality as M


def facts(crypto=None, assets=None, revenue=None, filed='2026-08-03', end='2026-06-30'):
    f = {'us-gaap': {}}
    def put(name, v):
        f['us-gaap'][name] = {'units': {'USD': [
            {'val': v, 'end': end, 'filed': filed, 'form': '10-Q'}]}}
    if crypto is not None: put('CryptoAssetFairValue', crypto)
    if assets is not None: put('Assets', assets)
    if revenue is not None: put('Revenues', revenue)
    return f


class TestMeasurement:
    def test_it_reads_the_balance_sheet_share(self):
        m = M.measure(1, facts(crypto=5e8, assets=1e9))
        assert abs(m['crypto_asset_share'] - 0.5) < 1e-9

    def test_total_assets_follow_the_holding_not_their_own_latest(self):
        """This test previously asserted the newest total assets won. That was
        wrong: it pairs a Q2 holding with a Q3 total and the ratio is neither
        figure's truth. Total assets are read at the HOLDING's period end."""
        f = facts(crypto=1e8, assets=1e9)
        f['us-gaap']['Assets']['units']['USD'].append(
            {'val': 2e9, 'end': '2026-09-30', 'filed': '2026-11-03', 'form': '10-Q'})
        m = M.measure(1, f)
        assert m['total_assets_usd'] == 1e9, 'the total must match the holding\'s balance sheet'
        assert abs(m['crypto_asset_share'] - 0.1) < 1e-9

    def test_the_latest_filing_still_wins_for_the_same_period(self):
        f = facts(crypto=1e8, assets=1e9)
        f['us-gaap']['Assets']['units']['USD'].append(
            {'val': 1.2e9, 'end': '2026-06-30', 'filed': '2026-11-03', 'form': '10-K'})
        assert M.measure(1, f)['total_assets_usd'] == 1.2e9

    def test_a_missing_figure_says_which(self):
        m = M.measure(1, facts(assets=1e9))
        assert m['crypto_asset_share'] is None
        assert 'no crypto carrying value' in m['asset_share_unavailable']

    def test_crypto_revenue_is_reported_as_unmeasurable_not_zero(self):
        """§11B wants a crypto revenue share. It is a SEGMENT disclosure with no
        standard element, and companyfacts flattens the dimensions. Returning
        zero would be asserting something the filings do not say."""
        m = M.measure(1, facts(crypto=1e8, assets=1e9, revenue=5e8))
        assert m['crypto_revenue_share'] is None
        assert 'different from zero' in m['revenue_share_unavailable']

    def test_every_figure_carries_its_source(self):
        m = M.measure(1, facts(crypto=1e8, assets=1e9))
        assert m['sources']['crypto_assets']['concept'].startswith('us-gaap:')
        assert m['sources']['crypto_assets']['filed']


class TestClassification:
    def test_a_dominant_holding_is_a_treasury_company(self):
        v, s, why = M.classify(M.measure(1, facts(crypto=9e8, assets=1e9)))
        assert (v, s) == ('material', 'treasury')
        assert 'better described by the holding' in why

    def test_a_miner_with_a_dominant_holding_flags_the_double_count(self):
        v, s, why = M.classify(M.measure(1, facts(crypto=9e8, assets=1e9)), ['mining'])
        assert s == 'treasury' and '19' in why

    def test_a_material_holding_takes_its_sleeve_from_the_business(self):
        v, s, _ = M.classify(M.measure(1, facts(crypto=2e8, assets=1e9)), ['mining'])
        assert (v, s) == ('material', 'mining')

    def test_conflicting_signals_leave_the_sleeve_unsettled(self):
        """§9 wants exactly ONE primary classification. Taking the first of two
        in arbitrary order would be a coin flip."""
        v, s, why = M.classify(M.measure(1, facts(crypto=2e8, assets=1e9)),
                               ['mining', 'exchange'])
        assert v == 'material' and s is None
        assert 'cannot be settled' in why

    def test_a_small_holding_is_immaterial(self):
        v, s, _ = M.classify(M.measure(1, facts(crypto=1e6, assets=1e9)))
        assert v == 'immaterial'

    def test_unmeasurable_is_not_immaterial(self):
        """The distinction that matters. A company whose crypto business is real
        but unsegmented must not be recorded as having none."""
        v, s, why = M.classify(M.measure(1, facts(assets=1e9)), ['mining'])
        assert v == 'not-proven'
        assert 'not the same as immaterial' in why

    def test_a_tiny_company_is_screened_out_before_anything_else(self):
        v, s, why = M.classify(M.measure(1, facts(crypto=9e5, assets=1e6)))
        assert v == 'immaterial' and 'floor' in why


class TestThresholdDiscipline:
    def test_sensitivity_reports_how_many_change_side(self):
        ms = [M.measure(i, facts(crypto=c, assets=1e9))
              for i, c in enumerate([5e7, 9e7, 1.1e8, 3e8, 9e8])]
        s = M.sensitivity(ms)
        assert s['base_threshold'] == M.THRESHOLDS['asset_share_material']
        assert len(s['results']) == 4
        assert all('changed' in r for r in s['results'])

    def test_the_reading_names_the_criterion(self):
        """A threshold is chosen for stability, never for what it does to a
        return series."""
        s = M.sensitivity([M.measure(1, facts(crypto=1e8, assets=1e9))])
        assert 'artefact of the threshold' in s['reading']
        assert 'never for what it does to a backtest' in M.THRESHOLDS['why'] \
            or 'not by looking at what it did to a return series' in M.THRESHOLDS['why'] \
            or 'never' in M.THRESHOLDS['why']


class TestFundsAreExcludedEverywhere:
    """Ark 21Shares and Franklin Templeton came out at 100% "treasury" and
    topped the screen. Their balance sheet IS the holding, so any
    assets-over-assets measure ranks them first.

    The rule existed in the treasury sweep and this module did not call it -
    the third time today a rule was correct where it was written and absent
    where it mattered. It now lives in rules.py and both import it."""

    def test_a_fund_is_excluded_however_material_it_looks(self):
        m = M.measure(1, facts(crypto=1e9, assets=1e9))
        v, s, why = M.classify(m, name='Ark 21Shares Bitcoin ETF')
        assert v == 'excluded' and s is None
        assert '19' in why and 'custody' in why

    def test_the_marker_that_matched_is_named(self):
        v, s, why = M.classify(M.measure(1, facts(crypto=1e9, assets=1e9)),
                               name='Franklin Templeton Digital Holdings Trust')
        assert 'franklin' in why or 'trust' in why

    def test_an_operating_company_is_untouched(self):
        v, s, _ = M.classify(M.measure(1, facts(crypto=9e8, assets=1e9)), name='Strategy Inc')
        assert v == 'material' and s == 'treasury'

    def test_the_rule_lives_in_one_place(self):
        import rules
        assert rules.is_fund('iShares Bitcoin Trust ETF')
        assert rules.is_fund('Strategy Inc') is None

    def test_it_only_ever_excludes(self):
        """Name matching is crude. Its failure mode must be admitting a fund,
        never rejecting a real company."""
        import rules
        for real in ('Strategy Inc', 'CleanSpark, Inc.', 'Coinbase Global, Inc.',
                     'BitMine Immersion Technologies, Inc.', 'American Bitcoin Corp.'):
            assert rules.is_fund(real) is None, real


class TestImpossibleSharesAndWrongElements:
    """The live run on 412 companies put NovaBay Pharmaceuticals at 93.5%
    "treasury", Greenlane Holdings at 184.8% and CDT Equity at 781.3%.

    Two causes. IndefiniteLivedIntangibleAssetsExcludingGoodwill was in the
    crypto concept list - it is the GENERAL element for trademarks and
    licences, and most filers using it hold no crypto. And total assets were
    read at their own latest date rather than at the holding's, pairing a Q2
    holding with a year-old total."""

    def test_the_general_intangibles_element_is_not_a_crypto_concept(self):
        assert 'IndefiniteLivedIntangibleAssetsExcludingGoodwill' not in M.CRYPTO_ASSET_CONCEPTS

    def test_a_pharma_company_with_large_trademarks_is_not_a_treasury(self):
        f = {'us-gaap': {
            'IndefiniteLivedIntangibleAssetsExcludingGoodwill': {'units': {'USD': [
                {'val': 9.35e7, 'end': '2026-06-30', 'filed': '2026-08-03'}]}},
            'Assets': {'units': {'USD': [
                {'val': 1e8, 'end': '2026-06-30', 'filed': '2026-08-03'}]}}}}
        v, s, why = M.classify(M.measure(1, f), name='NovaBay Pharmaceuticals')
        assert v == 'not-proven' and s != 'treasury'

    def test_total_assets_are_read_at_the_holdings_period_end(self):
        """Pairing a current holding with a stale total is a ratio that is
        neither figure's truth."""
        f = {'us-gaap': {
            'CryptoAssetFairValue': {'units': {'USD': [
                {'val': 5e8, 'end': '2026-06-30', 'filed': '2026-08-03'}]}},
            'Assets': {'units': {'USD': [
                {'val': 1e9, 'end': '2026-06-30', 'filed': '2026-08-03'},
                {'val': 2e8, 'end': '2025-06-30', 'filed': '2025-08-03'}]}}}}
        m = M.measure(1, f)
        assert m['total_assets_usd'] == 1e9
        assert abs(m['crypto_asset_share'] - 0.5) < 1e-9

    def test_a_share_above_one_is_refused(self):
        """781% is not a measurement. A holding cannot exceed the balance sheet
        it sits on."""
        f = {'us-gaap': {
            'CryptoAssetFairValue': {'units': {'USD': [
                {'val': 7.8e9, 'end': '2026-06-30', 'filed': '2026-08-03'}]}},
            'Assets': {'units': {'USD': [
                {'val': 1e9, 'end': '2026-06-30', 'filed': '2026-08-03'}]}}}}
        m = M.measure(1, f)
        assert m['crypto_asset_share'] is None
        assert m['impossible_share'] > 7
        assert 'cannot be larger than the balance sheet' in m['asset_share_unavailable']
        assert M.classify(m)[0] == 'not-proven'

    def test_a_holding_equal_to_the_balance_sheet_is_allowed(self):
        """A pure treasury vehicle legitimately sits at or near 100%."""
        f = {'us-gaap': {
            'CryptoAssetFairValue': {'units': {'USD': [
                {'val': 1e9, 'end': '2026-06-30', 'filed': '2026-08-03'}]}},
            'Assets': {'units': {'USD': [
                {'val': 1e9, 'end': '2026-06-30', 'filed': '2026-08-03'}]}}}}
        assert M.measure(1, f)['crypto_asset_share'] == 1.0


class TestNamesFollowThePivot:
    """CIK 1389545 is "NovaBay Pharmaceuticals" in full-text search and
    "Stablecoin Development Corporation" in the XBRL frames: one pharmaceutical
    shell that became a crypto treasury, and two datasets disagreeing.

    The screen was RIGHT about that company and the label made it look wrong -
    a reader dismisses a correct finding because the name contradicts it."""

    SUB = {'name': 'Stablecoin Development Corporation',
           'formerNames': [{'name': 'NovaBay Pharmaceuticals, Inc.'}],
           'tickers': ['NBY'], 'sicDescription': 'Pharmaceutical Preparations'}

    def test_it_returns_the_current_name_and_keeps_the_former(self):
        M._get = lambda url, tries=3: self.SUB
        n = M.current_name(1389545)
        assert n['name'] == 'Stablecoin Development Corporation'
        assert 'NovaBay Pharmaceuticals, Inc.' in n['former_names']

    def test_a_former_name_is_kept_not_discarded(self):
        """A pivot IS the interesting fact about several of these companies.
        Hiding it flattens the story into a list of treasuries."""
        M._get = lambda url, tries=3: self.SUB
        assert M.current_name(1389545)['former_names']

    def test_a_lookup_failure_costs_a_name_not_a_company(self):
        def boom(url, tries=3):
            raise RuntimeError('503')
        M._get = boom
        n = M.current_name(1)
        assert n['name'] is None and n['former_names'] == []

    def test_the_cache_prevents_a_second_call(self):
        calls = []
        def once(url, tries=3):
            calls.append(url)
            return self.SUB
        M._get = once
        cache = {}
        M.current_name(1389545, cache)
        M.current_name(1389545, cache)
        assert len(calls) == 1

    def test_the_fund_test_uses_the_current_name(self):
        """A shell that became a fund must be judged on what it is now."""
        M._get = lambda url, tries=3: {'name': 'Bitwise Bitcoin ETF',
                                       'formerNames': [{'name': 'Some Shell Corp'}]}
        n = M.current_name(1)
        v, s, why = M.classify(M.measure(1, facts(crypto=1e9, assets=1e9)), name=n['name'])
        assert v == 'excluded'
