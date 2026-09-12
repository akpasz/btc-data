"""Offline tests for the broad crypto-equity universe discovery."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'fetch'))
import universe as U


class TestTermChoice:
    """Precision costs recall, and the materiality screen cannot rescue a
    candidate set full of noise - it can only narrow an honest one."""

    def test_bare_blockchain_is_not_a_term(self):
        """Every consultancy and logistics company has claimed blockchain at
        some point."""
        terms = ' '.join(t for t, _ in U.TERMS)
        assert '"blockchain"' not in terms

    def test_bare_digital_asset_is_not_a_term(self):
        """It means a media file in half its uses."""
        assert '"digital asset"' not in [t for t, _ in U.TERMS]

    def test_every_term_is_a_quoted_phrase(self):
        for t, _ in U.TERMS:
            assert t.startswith('"') and t.endswith('"'), f'{t} would match the words separately'

    def test_the_sleeves_named_are_the_ones_the_spec_uses(self):
        sleeves = {s for _, s in U.TERMS}
        assert sleeves <= {'mining', 'exchange', 'infrastructure', 'treasury',
                           'stablecoin', 'tokenization'}


class TestDiscovery:
    def _stub(self, mapping):
        def search(term, forms=U.FORMS, pages=40, days=None):
            return mapping.get(term, ({}, False, 0))
        return search

    def test_one_company_matching_several_terms_is_one_candidate(self):
        U.search = self._stub({
            '"bitcoin mining"': ({1: {'cik': 1, 'name': 'Miner Co', 'hits': 3}}, False, 0),
            '"hash rate"': ({1: {'cik': 1, 'name': 'Miner Co', 'hits': 2}}, False, 0)})
        u = U.discover(terms=[('"bitcoin mining"', 'mining'), ('"hash rate"', 'mining')])
        assert len(u['candidates']) == 1
        assert len(u['candidates'][0]['terms']) == 2

    def test_it_records_signals_but_never_assigns_a_sleeve(self):
        """§9 wants exactly one primary classification per company, and that
        needs materiality. Choosing one from a text match would be a guess
        wearing a rule's clothes."""
        U.search = self._stub({
            '"bitcoin mining"': ({1: {'cik': 1, 'name': 'Both Co', 'hits': 1}}, False, 0),
            '"bitcoin treasury"': ({1: {'cik': 1, 'name': 'Both Co', 'hits': 1}}, False, 0)})
        u = U.discover(terms=[('"bitcoin mining"', 'mining'), ('"bitcoin treasury"', 'treasury')])
        c = u['candidates'][0]
        assert set(c['sleeve_signals']) == {'mining', 'treasury'}
        assert 'sleeve' not in c, 'a signal is not a classification'

    def test_a_capped_term_is_recorded(self):
        """A very common phrase returns a sample, not every match, and saying
        so is the difference between a limit and a silent gap."""
        U.search = self._stub({'"stablecoin"': ({1: {'cik': 1, 'name': 'X', 'hits': 1}}, True, 4200)})
        u = U.discover(terms=[('"stablecoin"', 'stablecoin')])
        c = u['terms_capped'][0]
        assert c['term'] == '"stablecoin"' and c['matches'] == 4200
        assert c['retrieved'] < c['matches'], 'the shortfall must be visible, not just the fact'

    def test_a_failed_term_does_not_lose_the_others(self):
        def search(term, forms=U.FORMS, pages=40, days=None):
            if term == '"hash rate"':
                raise RuntimeError('503')
            return ({1: {'cik': 1, 'name': 'X', 'hits': 1}}, False, 0)
        U.search = search
        u = U.discover(terms=[('"hash rate"', 'mining'), ('"stablecoin"', 'stablecoin')])
        assert len(u['candidates']) == 1 and len(u['terms_failed']) == 1

    def test_the_output_says_it_is_not_an_index(self):
        U.search = self._stub({})
        u = U.discover(terms=[('"stablecoin"', 'stablecoin')])
        assert 'NOT AN INDEX' in u['note']
        assert any('absence here is not evidence' in k for k in u['known_limits'])


class TestTheDateRule:
    """Unbounded, "tokenization" matches 739 annual reports going back two
    decades, most from companies with no crypto business now. The universe is
    meant to be companies whose CURRENT business involves crypto, and one that
    has files an annual report saying so."""

    def test_the_window_is_long_enough_for_every_annual_filer(self):
        assert U.LOOKBACK_DAYS >= 400, 'a shorter window misses filers on an off-cycle year end'

    def test_the_window_is_recorded_in_the_output(self):
        U.search = lambda t, f=U.FORMS, p=40, d=None: ({}, False, 0)
        u = U.discover(terms=[('"stablecoin"', 'stablecoin')])
        assert u['lookback_days'] == U.LOOKBACK_DAYS, 'the rule must travel with the result'

    def test_the_window_reaches_the_search(self):
        seen = {}
        def search(term, forms=U.FORMS, pages=40, days=None):
            seen['days'] = days
            return ({}, False, 0)
        U.search = search
        U.discover(terms=[('"stablecoin"', 'stablecoin')], days=365)
        assert seen['days'] == 365, 'a date rule that never reaches the query is not a rule'


class TestFundsAreCountedNotHidden:
    """Funds belong in the candidate set - §5 wants it broad with documented
    exclusions - and they are removed downstream. But they match eight or nine
    phrases each, because a crypto ETF prospectus discusses mining, exchanges
    and custody at length, so they crowd the top and inflate the headline.
    "412 candidates" would be quoted as "412 crypto companies"."""

    def _u(self):
        U.search = lambda t, f=U.FORMS, p=40, d=None: (
            {1: {'cik': 1, 'name': 'Strategy Inc', 'hits': 1},
             2: {'cik': 2, 'name': 'Ark 21Shares Bitcoin ETF', 'hits': 1},
             3: {'cik': 3, 'name': 'Bitwise Bitcoin ETF', 'hits': 1}}, False, 0)
        return U.discover(terms=[('"bitcoin treasury"', 'treasury')])

    def test_funds_are_counted(self):
        u = self._u()
        assert u['fund_candidates'] == 2 and u['operating_candidates'] == 1

    def test_they_stay_in_the_candidate_set(self):
        """Removing them here would hide an exclusion that §5 requires be
        documented."""
        assert len(self._u()['candidates']) == 3

    def test_each_is_marked_with_what_matched(self):
        u = self._u()
        marked = [c for c in u['candidates'] if c.get('fund_marker')]
        assert len(marked) == 2 and all(m['fund_marker'] for m in marked)

    def test_an_operating_company_is_not_marked(self):
        u = self._u()
        s = next(c for c in u['candidates'] if c['cik'] == 1)
        assert 'fund_marker' not in s
