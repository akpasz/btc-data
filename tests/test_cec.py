"""Offline tests for the crypto-equity index and its position measure."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'fetch'))
import cec as C

# fixtures that isolate one behaviour use two or three constituents; the
# membership floor is a separate rule with its own tests below
SMALL = {'min_members_to_start': 2}


def caps(start_day, n, first=1e9, growth=1.0, sleeve='mining'):
    import datetime as dt
    d0 = dt.date.fromisoformat(start_day)
    return {'sleeve': sleeve,
            'caps': [((d0 + dt.timedelta(days=i)).isoformat(), first * (growth ** i))
                     for i in range(n)]}


class TestMarketCap:
    def test_the_share_count_is_the_last_one_filed(self):
        """Pricing a company on a share count filed later prices it on
        information the market did not have."""
        px = [('2026-01-05', 10.0), ('2026-03-05', 10.0)]
        sh = [('2026-01-01', 100.0), ('2026-02-01', 200.0)]
        out = dict(C.market_caps(px, sh))
        assert out['2026-01-05'] == 1000.0
        assert out['2026-03-05'] == 2000.0

    def test_days_before_the_first_filing_are_dropped(self):
        px = [('2025-01-05', 10.0), ('2026-01-05', 10.0)]
        sh = [('2026-01-01', 100.0)]
        assert [d for d, _ in C.market_caps(px, sh)] == ['2026-01-05']

    def test_a_missing_price_costs_one_constituent(self):
        assert C.market_caps([], [('2026-01-01', 1.0)]) == []


class TestWeights:
    def test_no_constituent_exceeds_the_single_cap(self):
        live = [(0, 90e9), (1, 5e9), (2, 3e9), (3, 1e9), (4, 1e9), (5, 1e9),
                (6, 1e9), (7, 1e9), (8, 1e9), (9, 1e9)]
        ms = [{'sleeve': 'mining'} for _ in range(10)]
        w = C._weights(live, ms, C.RULES)
        assert max(w.values()) <= C.RULES['single_name_cap'] + 1e-6
        assert abs(sum(w.values()) - 1.0) < 1e-9

    def test_the_order_is_preserved(self):
        """A cap that lifts a smaller holding above a larger one is not limiting
        concentration, it is inventing a ranking."""
        live = [(0, 90e9), (1, 5e9), (2, 3e9), (3, 2e9), (4, 1e9),
                (5, 1e9), (6, 1e9), (7, 1e9), (8, 1e9), (9, 1e9)]
        ms = [{'sleeve': 'mining'} for _ in range(10)]
        w = C._weights(live, ms, C.RULES)
        assert w[0] >= w[1] >= w[2] - 1e-9

    def test_a_sleeve_cannot_be_the_index_once_the_cap_can_bind(self):
        """Without a sleeve cap the treasury sleeve is the index, and the index
        then measures one company holding bitcoin - which you can buy directly."""
        live = [(i, 30e9) for i in range(3)] + [(i, 2e9) for i in range(3, 12)]
        ms = ([{'sleeve': 'treasury'}] * 3 + [{'sleeve': 'mining'}] * 3
              + [{'sleeve': 'exchange'}] * 3 + [{'sleeve': 'infrastructure'}] * 3)
        w = C._weights(live, ms, C.RULES)
        for sl in ('treasury', 'mining', 'exchange', 'infrastructure'):
            tot = sum(w[i] for i in range(len(ms)) if ms[i]['sleeve'] == sl)
            assert tot <= C.RULES['sleeve_cap'] + 1e-6, sl

    def test_an_unreachable_sleeve_cap_is_skipped_not_forced(self):
        """With two sleeves a 40% cap is unreachable: two times forty is eighty.
        Capping the larger pushes its excess into the smaller and lands THAT one
        above the cap. The first version did exactly that and produced a 60%
        sleeve while claiming a 40% limit."""
        live = [(i, 30e9) for i in range(3)] + [(i, 2e9) for i in range(3, 9)]
        ms = [{'sleeve': 'treasury'}] * 3 + [{'sleeve': 'mining'}] * 6
        w = C._weights(live, ms, C.RULES)
        assert abs(sum(w.values()) - 1.0) < 1e-9
        assert max(w.values()) <= C.RULES['single_name_cap'] + 1e-6, \
            'the single-name cap must still hold'
        assert w[0] >= w[3] - 1e-9, 'and the order must survive'

    def test_an_unreachable_single_cap_is_not_pretended(self):
        live = [(0, 5e9), (1, 1e9)]
        w = C._weights(live, [{'sleeve': 'mining'}] * 2, C.RULES)
        assert abs(sum(w.values()) - 1.0) < 1e-9


class TestIndex:
    def test_a_constituent_never_enters_before_it_qualifies(self):
        """Backfilling a company into the period when it was a furniture
        business is the survivorship error, and it is easy to do by accident
        because the price series reaches that far back."""
        # a long-listed constituent and one that qualifies four years later.
        # The series must OVERLAP or there is never more than one live member
        # and the index correctly publishes nothing - my first fixture had them
        # ending before the other began, which tested the empty path instead.
        a = caps('2020-01-01', 2200)
        b = caps('2024-01-01', 400)
        ix = C.build([a, b], SMALL)
        # with one constituent for the first four years the index has fewer than
        # two live members and publishes nothing, which is correct: an index of
        # one company is that company
        assert ix['levels'], 'the overlapping period must produce levels'
        assert all(x['date'] >= '2024-01-01' for x in ix['levels']), \
            'no level before both constituents qualify'
        assert all(x['members'] >= 2 for x in ix['levels'])

    def test_the_level_starts_at_the_base(self):
        ix = C.build([caps('2024-01-01', 300), caps('2024-01-01', 300, first=5e8)], SMALL)
        assert abs(ix['levels'][0]['level'] - C.RULES['base_level']) < 1e-6

    def test_a_rebalance_does_not_move_the_level_by_itself(self):
        """Chain-linking on the previous day's weights. Re-weighting and then
        measuring against the new weights makes the index jump on a day when no
        price changed - the classic index error."""
        flat = [caps('2024-01-01', 300, first=1e9, growth=1.0),
                caps('2024-01-01', 300, first=4e8, growth=1.0)]
        ix = C.build(flat, SMALL)
        lv = [x['level'] for x in ix['levels']]
        assert max(lv) - min(lv) < 1e-6, 'a flat market must produce a flat index'

    def test_it_reports_rather_than_raises_with_no_data(self):
        assert C.build([], SMALL)['levels'] == []


class TestPosition:
    def _levels(self, vals):
        import datetime as dt
        d0 = dt.date(2024, 1, 1)
        return [{'date': (d0 + dt.timedelta(days=i)).isoformat(), 'level': v, 'members': 5}
                for i, v in enumerate(vals)]

    def test_it_reports_a_percentile_of_its_own_history(self):
        p = C.position(self._levels(list(range(100))))
        assert p['percentile_of_own_history'] == 100.0
        p2 = C.position(self._levels(list(range(100, 0, -1))))
        assert p2['percentile_of_own_history'] <= 2.0

    def test_it_refuses_cycle_language(self):
        """One cycle cannot establish where tops occur. "60% of the way to a
        peak" would be a claim about a distribution observed once."""
        p = C.position(self._levels(list(range(100))))
        w = p['sample_warning'].lower()
        assert 'not a statement about how far a cycle has to run' in w
        assert 'observed once' in w

    def test_it_carries_the_sample_with_the_reading(self):
        p = C.position(self._levels(list(range(400))))
        assert p['history_days'] == 399 and p['history_years'] > 1

    def test_a_short_history_gets_no_reading_at_all(self):
        p = C.position(self._levels([1, 2, 3]))
        assert p['reading'] is None and 'thirty' in p['why']

    def test_the_ratio_to_bitcoin_is_a_different_question(self):
        import datetime as dt
        d0 = dt.date(2024, 1, 1)
        lv = self._levels([100 + i for i in range(200)])
        btc = [((d0 + dt.timedelta(days=i)).isoformat(), 1000.0 + 2 * i) for i in range(200)]
        p = C.position(lv, btc)
        assert 'vs_bitcoin' in p
        # the index triples while bitcoin gains 40%, so the ratio rises
        # throughout and sits at the top of its own range
        assert p['vs_bitcoin']['ratio_percentile'] >= 98.0


import cec_run as R


class TestClassificationPrecedence:
    """§9 wants exactly one primary sleeve. The screen refuses to guess when
    text signals conflict, which was correct for the screen and stalled the
    index - roughly half the material companies match more than one phrase.

    The answer is a precedence order applied mechanically, not a judgment per
    company."""

    def test_the_balance_sheet_wins_first(self):
        """A company whose assets are mostly crypto is a treasury vehicle
        whatever its text says, because that is what its shareholders own."""
        sl, why, ok = R.sleeve_for({'crypto_asset_share': 0.93,
                                'sleeve_signals': ['mining', 'exchange']})
        assert sl == 'treasury' and '93%' in why

    def test_the_most_specific_business_term_wins(self):
        """"bitcoin mining" describes one activity; "blockchain infrastructure"
        describes anything."""
        sl, why, ok = R.sleeve_for({'crypto_asset_share': 0.2,
                                    'sleeve_signals': ['infrastructure', 'mining']})
        assert sl == 'mining' and ok is False, 'a text guess is never confirmed'
        assert 'PROVISIONAL' in why

    def test_infrastructure_is_the_catch_all_not_a_peer(self):
        sl, _, _ok = R.sleeve_for({'crypto_asset_share': 0.2,
                              'sleeve_signals': ['infrastructure']})
        assert sl == 'infrastructure'
        assert R.PRECEDENCE[-1] == 'infrastructure'

    def test_a_guess_never_looks_like_a_decision(self):
        """The whole point. A provisional classification that reads identically
        to a decided one is how BitGo ends up in a mining sleeve unnoticed."""
        for sig, share, expect in ((['mining'], 0.1, False),
                                   (['mining', 'exchange'], 0.1, False),
                                   ([], 0.9, True)):
            sl, why, ok = R.sleeve_for({'crypto_asset_share': share, 'sleeve_signals': sig})
            assert sl and ok is expect
            assert ('PROVISIONAL' in why) is (not expect)

    def test_the_classification_file_overrides_every_guess(self):
        sl, why, ok = R.sleeve_for(
            {'cik': 1740604, 'crypto_asset_share': 0.05, 'sleeve_signals': ['mining']},
            {1740604: 'infrastructure'})
        assert sl == 'infrastructure' and ok is True

    def test_a_company_with_nothing_gets_no_sleeve(self):
        sl, why, ok = R.sleeve_for({'crypto_asset_share': None, 'sleeve_signals': []})
        assert sl is None and 'no business signal' in why

    def test_a_treasury_signal_alone_does_not_beat_a_business(self):
        """Matching the phrase "bitcoin treasury" is not the same as holding
        one. The balance sheet decides that, and the text decides the business."""
        sl, _, _ok = R.sleeve_for({'crypto_asset_share': 0.15,
                              'sleeve_signals': ['treasury', 'mining']})
        assert sl == 'mining'

    def test_the_order_is_the_documented_one(self):
        assert R.PRECEDENCE == ('mining', 'exchange', 'stablecoin',
                                'tokenization', 'infrastructure')


class TestPriceProviders:
    """Stooq now answers with a JavaScript challenge page instead of data. A CSV
    parser reads that as zero rows, so the index reported "0 constituents, 55
    skipped" with every skip saying "no price history" - true, and useless.

    An empty series because every provider refused is a DIFFERENT FACT from a
    company having no price history, and the two must not look the same."""

    def test_a_challenge_page_is_not_a_price_series(self):
        C._get = lambda u, **k: b'<!DOCTYPE html><html><head><meta charset="utf-8">'
        assert C._stooq('MSTR') == []

    def test_the_source_that_answered_is_recorded(self):
        rows = [(f'2026-01-{d:02d}', 100.0) for d in range(1, 29)] * 2
        C._yahoo = lambda t, m='us': rows
        got, src = C.prices('MSTR')
        assert src == 'yahoo' and len(got) > 30

    def test_it_falls_through_to_the_next_provider(self):
        rows = [(f'2026-01-{d:02d}', 100.0) for d in range(1, 29)] * 2
        C._yahoo = lambda t, m='us': []
        C._stooq = lambda t, m='us': rows
        assert C.prices('MSTR')[1] == 'stooq'

    def test_every_provider_failing_returns_a_none_source(self):
        """Not an empty string, not 'unknown'. None, so the caller cannot treat
        it as a successful fetch that happened to be short."""
        C._yahoo = lambda t, m='us': []
        C._stooq = lambda t, m='us': []
        assert C.prices('MSTR') == ([], None)

    def test_a_short_series_is_not_accepted(self):
        """Ten rows from a provider having a bad day would enter the index as a
        constituent with no history and distort every weight around it."""
        C._yahoo = lambda t, m='us': [('2026-01-01', 1.0)] * 10
        C._stooq = lambda t, m='us': []
        assert C.prices('MSTR')[1] is None


class TestTheModuleLoadsCompletely:
    """The runner crashed on its LAST print statement, after doing all the work
    and printing everything else, because a constant referenced there had been
    lost in a rewrite. Every test passed: they exercise the functions, not the
    module's own message paths."""

    def test_the_constants_the_messages_use_exist(self):
        for n in ('SLEEVE_FILE', 'PRECEDENCE', 'TREASURY_SHARE'):
            assert hasattr(R, n), f'{n} is referenced in main() and must exist'

    def test_no_name_in_main_is_undefined(self):
        """Compiles main() and checks every global it names is resolvable. A
        crash after the work is done is the most expensive kind: it throws away
        a run that took minutes and produced correct output."""
        import inspect, ast as A
        src = inspect.getsource(R.main)
        tree = A.parse(src.lstrip())
        names = {n.id for n in A.walk(tree) if isinstance(n, A.Name) and isinstance(n.ctx, A.Load)}
        # anything BOUND anywhere in the function is local: plain assignment,
        # tuple unpacking, loop variables, comprehension targets. Enumerating
        # the forms individually missed tuple unpacking and flagged nine names
        # that were perfectly fine.
        local = {a.arg for a in tree.body[0].args.args}
        local |= {n.id for n in A.walk(tree)
                  if isinstance(n, A.Name) and isinstance(n.ctx, (A.Store, A.Del))}
        # `except X as e` binds e through ExceptHandler.name, which is a plain
        # string and not a Name node - the test flagged its own blind spot when
        # a try/except was added to main()
        local |= {n.name for n in A.walk(tree) if isinstance(n, A.ExceptHandler) and n.name}
        local |= {n.optional_vars.id for n in A.walk(tree)
                  if isinstance(n, A.withitem) and isinstance(n.optional_vars, A.Name)}
        import builtins
        missing = [n for n in names - local
                   if not hasattr(R, n) and not hasattr(builtins, n)]
        assert not missing, f'main() names undefined globals: {missing}'


class TestTheTwoBugsThatMadeTheLevelZero:
    """The first live run produced a level of 0.0 from a base of 100, and a
    series starting in 2016. Both tests below existed in spirit and neither
    caught the real thing."""

    def _c(self, dates, val=1e9, sleeve='mining', qual=None, tk='X'):
        m = {'sleeve': sleeve, 'ticker': tk,
             'caps': [(d, val) for d in dates]}
        if qual:
            m['qualified_from'] = qual
        return m

    def test_a_constituent_missing_a_day_does_not_destroy_the_level(self):
        """These tickers do not share trading days - several are thin OTC names
        that go days without a print. Keeping one in the numerator while
        dropping it from the denominator made the ratio wrong on most days and
        decayed the level to zero."""
        import datetime as dt
        d0 = dt.date(2024, 1, 1)
        alld = [(d0 + dt.timedelta(days=i)).isoformat() for i in range(400)]
        thin = [d for i, d in enumerate(alld) if i % 3]          # trades 2 days in 3
        ix = C.build([self._c(alld, tk='A'), self._c(alld, tk='B'),
                      self._c(thin, val=5e8, tk='C')], SMALL)
        lv = [x['level'] for x in ix['levels']]
        assert lv, 'levels must be produced'
        assert min(lv) > 50, f'a flat market must not decay: low was {min(lv):.3f}'
        assert max(lv) - min(lv) < 1e-6, 'and must not drift either'

    def test_a_constituent_enters_when_it_became_crypto_not_when_it_listed(self):
        """USBC has 2,514 days of price history because it was Cigar King Corp.
        Admitting it from its listing date put a cigar retailer in a crypto
        index in 2016."""
        import datetime as dt
        d0 = dt.date(2016, 1, 1)
        long = [(d0 + dt.timedelta(days=i)).isoformat() for i in range(3000)]
        ix = C.build([self._c(long, qual='2024-01-01', tk='USBC'),
                      self._c(long, qual='2024-01-01', tk='B')], SMALL)
        assert ix['levels'], 'levels must be produced'
        assert ix['levels'][0]['date'] >= '2024-01-01', \
            f'index began {ix["levels"][0]["date"]}, before either qualified'

    def test_seasoning_still_applies_on_top_of_qualification(self):
        """A company that qualified yesterday and listed yesterday is not
        admitted today just because it qualified."""
        import datetime as dt
        d0 = dt.date(2024, 1, 1)
        d = [(d0 + dt.timedelta(days=i)).isoformat() for i in range(400)]
        ix = C.build([self._c(d, qual='2024-01-01', tk='A'),
                      self._c(d, qual='2024-01-01', tk='B')], SMALL)
        assert ix['levels'][0]['date'] > '2024-02-01', 'seasoning must still delay entry'

    def test_a_constituent_without_a_qualification_date_is_named(self):
        """It enters on listing alone, which is a guess about when its history
        starts, and a guess must be visible."""
        import datetime as dt
        d0 = dt.date(2024, 1, 1)
        d = [(d0 + dt.timedelta(days=i)).isoformat() for i in range(400)]
        ix = C.build([self._c(d, tk='NOQUAL'), self._c(d, qual='2024-01-01', tk='B')], SMALL)
        assert 'NOQUAL' in ix['entered_on_seasoning_only']
        assert 'B' not in ix['entered_on_seasoning_only']


class TestDilutionIsNotAReturn:
    """The index produced a range of 18 to 41,042 on a 1.8-year series, because
    it chain-linked on MARKET CAPITALISATION. These companies dilute enormously
    - a shell issuing from one million shares to a hundred million moves its
    market cap a hundredfold on the filing date - and every issuance read as a
    gain.

    A holder did not make a hundred times their money that day. They were
    diluted. Weights come from market cap; returns come from price."""

    def _m(self, dates, px, sh, tk='X', q='2024-01-01'):
        return {'sleeve': 'mining', 'ticker': tk, 'qualified_from': q,
                'px': [(d, px[i]) for i, d in enumerate(dates)],
                'caps': [(d, px[i] * sh[i]) for i, d in enumerate(dates)]}

    def _days(self, n, start='2024-01-01'):
        import datetime as dt
        d0 = dt.date.fromisoformat(start)
        return [(d0 + dt.timedelta(days=i)).isoformat() for i in range(n)]

    def test_a_hundredfold_issuance_does_not_move_the_index(self):
        d = self._days(400)
        px = [10.0] * 400                       # price never moves
        sh = [1e7] * 200 + [1e9] * 200          # shares jump 100x halfway
        ix = C.build([self._m(d, px, sh, 'A'), self._m(d, px, [1e7] * 400, 'B')], SMALL)
        lv = [x['level'] for x in ix['levels']]
        assert lv, 'levels must be produced'
        assert max(lv) - min(lv) < 1e-6, \
            f'dilution moved the index: range {min(lv):.2f} to {max(lv):.2f}'

    def test_a_real_price_move_does_move_the_index(self):
        d = self._days(400)
        flat = [10.0] * 400
        rise = [10.0 * (1.001 ** i) for i in range(400)]
        ix = C.build([self._m(d, rise, [1e7] * 400, 'A'),
                      self._m(d, flat, [1e7] * 400, 'B')], SMALL)
        lv = [x['level'] for x in ix['levels']]
        assert lv[-1] > lv[0] * 1.05, 'a genuine rise must show'

    def test_a_buyback_is_not_a_loss_either(self):
        d = self._days(400)
        px = [10.0] * 400
        sh = [1e9] * 200 + [1e7] * 200          # shares fall 100x
        ix = C.build([self._m(d, px, sh, 'A'), self._m(d, px, [1e7] * 400, 'B')], SMALL)
        lv = [x['level'] for x in ix['levels']]
        assert max(lv) - min(lv) < 1e-6


class TestTheIndexWaitsUntilItIsAnIndex:
    """The first live run set its base of 100 on a day when TWO constituents
    qualified, and finished with 31. Every reading was taken against a base
    struck by whichever two happened to arrive first."""

    def _m(self, start, n, tk, px=10.0):
        import datetime as dt
        d0 = dt.date.fromisoformat(start)
        ds = [(d0 + dt.timedelta(days=i)).isoformat() for i in range(n)]
        return {'sleeve': 'mining', 'ticker': tk, 'qualified_from': start,
                'px': [(d, px) for d in ds],
                'caps': [(d, px * 1e7) for d in ds]}

    def test_it_does_not_start_on_two_constituents(self):
        ms = [self._m('2024-01-01', 900, 'A'), self._m('2024-01-01', 900, 'B')]
        ms += [self._m('2025-01-01', 500, f'C{i}') for i in range(9)]
        ix = C.build(ms)
        assert ix['levels'], 'levels must eventually be produced'
        assert ix['members_at_start'] >= C.RULES['min_members_to_start'], \
            f'index began with {ix["members_at_start"]} members'
        assert ix['levels'][0]['date'] >= '2025-01-01', \
            'it must wait for the later cohort rather than basing on the early pair'

    def test_a_universe_that_never_reaches_the_floor_publishes_nothing(self):
        """Better no index than one whose base is an accident."""
        ms = [self._m('2024-01-01', 500, f'X{i}') for i in range(3)]
        assert C.build(ms)['levels'] == []

    def test_once_started_it_continues_through_a_thin_day(self):
        """The floor is a condition for STARTING. An index that stopped every
        time membership dipped would have gaps rather than a level."""
        ms = [self._m('2024-01-01', 900, f'A{i}') for i in range(11)]
        ms[0]['caps'] = ms[0]['caps'][:400]      # one drops out partway
        ms[0]['px'] = ms[0]['px'][:400]
        ix = C.build(ms)
        assert len(ix['levels']) > 700, 'the series must continue past the dropout'


class TestMultiClassShareCounts:
    """HOOD came back with a market capitalisation of ZERO. Robinhood has Class
    A, B and C; companyfacts flattens the dimensions so the classes arrive as
    separate facts on the same filing date, and taking the last one seen
    returned a class with none outstanding.

    A $100bn company then sat in the index at weight zero and said nothing."""

    def _facts(self, rows):
        return {'dei': {'EntityCommonStockSharesOutstanding': {'units': {'shares': rows}}}}

    def test_the_classes_are_summed(self):
        f = self._facts([
            {'val': 8.0e8, 'end': '2026-06-30', 'filed': '2026-08-01'},
            {'val': 7.0e7, 'end': '2026-06-30', 'filed': '2026-08-01'},
            {'val': 0.0, 'end': '2026-06-30', 'filed': '2026-08-01'}])
        C._get = lambda u, **k: __import__('json').dumps({'facts': f}).encode()
        s = C.shares_series(1)
        assert s and abs(s[-1][1] - 8.7e8) < 1, f'got {s[-1][1]:,.0f}, expected 870,000,000'

    def test_a_zero_class_is_not_the_answer(self):
        """The specific failure: a class with none outstanding is not
        information about the company."""
        f = self._facts([{'val': 0.0, 'end': '2026-06-30', 'filed': '2026-08-01'}])
        C._get = lambda u, **k: __import__('json').dumps({'facts': f}).encode()
        assert C.shares_series(1) == []

    def test_a_repeated_figure_is_not_added_to_itself(self):
        """The same count restated in a later filing must not double the
        company."""
        f = self._facts([
            {'val': 5.0e8, 'end': '2026-06-30', 'filed': '2026-08-01'},
            {'val': 5.0e8, 'end': '2026-06-30', 'filed': '2026-08-01'}])
        C._get = lambda u, **k: __import__('json').dumps({'facts': f}).encode()
        assert abs(C.shares_series(1)[-1][1] - 5.0e8) < 1

    def test_a_single_class_issuer_is_unaffected(self):
        f = self._facts([{'val': 1.0e8, 'end': '2026-06-30', 'filed': '2026-08-01'}])
        C._get = lambda u, **k: __import__('json').dumps({'facts': f}).encode()
        assert abs(C.shares_series(1)[-1][1] - 1.0e8) < 1

    def test_a_zero_cap_never_enters_the_index(self):
        """Weighting it at nothing is a bug that looks like a small company."""
        import datetime as dt
        d0 = dt.date(2024, 1, 1)
        ds = [(d0 + dt.timedelta(days=i)).isoformat() for i in range(400)]
        good = {'sleeve': 'mining', 'ticker': 'A', 'qualified_from': '2024-01-01',
                'px': [(d, 10.0) for d in ds], 'caps': [(d, 1e8) for d in ds]}
        zero = {'sleeve': 'mining', 'ticker': 'Z', 'qualified_from': '2024-01-01',
                'px': [(d, 10.0) for d in ds], 'caps': [(d, 0.0) for d in ds]}
        ix = C.build([good, dict(good, ticker='B'), zero], SMALL)
        assert all(x['members'] == 2 for x in ix['levels']), 'the zero-cap name must be out'


class TestShareCountsAreNotDoubleCounted:
    """Summing everything tripled every company - BMNR $14.5bn to $61.7bn, RIOT
    $8.1bn to $24.2bn - because the same total is reported under BOTH dei and
    us-gaap, and across several period ends on one filing date.

    Two concepts carrying the same number is one number. Several values within
    one concept and period are share CLASSES and do sum."""

    def _f(self, dei=None, gaap=None):
        out = {}
        if dei is not None:
            out['dei'] = {'EntityCommonStockSharesOutstanding': {'units': {'shares': dei}}}
        if gaap is not None:
            out['us-gaap'] = {'CommonStockSharesOutstanding': {'units': {'shares': gaap}}}
        return out

    def _series(self, facts):
        C._get = lambda u, **k: __import__('json').dumps({'facts': facts}).encode()
        return C.shares_series(1)

    def test_the_same_total_under_two_concepts_counts_once(self):
        row = [{'val': 3.0e8, 'end': '2026-06-30', 'filed': '2026-08-01'}]
        s = self._series(self._f(dei=row, gaap=list(row)))
        assert abs(s[-1][1] - 3.0e8) < 1, f'got {s[-1][1]:,.0f}, expected 300,000,000'

    def test_share_classes_within_one_concept_still_sum(self):
        """Robinhood's A, B and C. Taking one gave a $100bn company zero."""
        s = self._series(self._f(dei=[
            {'val': 8.0e8, 'end': '2026-06-30', 'filed': '2026-08-01'},
            {'val': 7.0e7, 'end': '2026-06-30', 'filed': '2026-08-01'}]))
        assert abs(s[-1][1] - 8.7e8) < 1

    def test_two_period_ends_on_one_filing_do_not_add(self):
        """A 10-K reports the current and prior year. They are the same company
        at two moments, not two companies."""
        s = self._series(self._f(dei=[
            {'val': 1.0e8, 'end': '2025-12-31', 'filed': '2026-02-01'},
            {'val': 1.2e8, 'end': '2026-06-30', 'filed': '2026-02-01'}]))
        assert abs(s[-1][1] - 1.2e8) < 1, 'the later period wins, they do not sum'

    def test_the_larger_concept_wins_where_they_disagree(self):
        """One concept covering a single class and another the total: the total
        is the one that describes the company."""
        s = self._series(self._f(
            dei=[{'val': 8.7e8, 'end': '2026-06-30', 'filed': '2026-08-01'}],
            gaap=[{'val': 8.0e8, 'end': '2026-06-30', 'filed': '2026-08-01'}]))
        assert abs(s[-1][1] - 8.7e8) < 1


class TestStaleShareCounts:
    """Robinhood's last share fact is from FEBRUARY 2022 - it stopped tagging
    the concept. The code carried a 2021 count of 232m forward four years, and
    232m against today's ~$111 gives $25.8bn for a company nearer $100bn.

    Not a small error, and it looked entirely plausible in the output."""

    def _px(self, n, start='2024-01-01', p=100.0):
        import datetime as dt
        d0 = dt.date.fromisoformat(start)
        return [((d0 + dt.timedelta(days=i)).isoformat(), p) for i in range(n)]

    def test_a_count_from_four_years_ago_prices_nothing(self):
        px = self._px(400, '2026-01-01')
        sh = [('2022-02-24', 2.3e8)]
        assert C.market_caps(px, sh) == []

    def test_a_count_from_last_quarter_is_carried_forward(self):
        """Carrying forward is right across a quarter. That is the normal case
        and must keep working."""
        px = self._px(120, '2026-01-01')
        sh = [('2025-11-04', 1.0e8)]
        caps = C.market_caps(px, sh)
        assert len(caps) == 120 and caps[-1][1] == 1.0e10

    def test_the_series_stops_where_the_counts_stop(self):
        """A company that tagged until 2024 and then stopped is weighted until
        the count goes stale, and not after."""
        px = self._px(1200, '2024-01-01')
        sh = [('2024-01-01', 1.0e8)]
        caps = C.market_caps(px, sh)
        assert caps, 'the early period must still be priced'
        assert len(caps) < 500, 'and it must stop once the count is stale'
        assert caps[-1][0] < '2025-03-01'

    def test_the_window_is_longer_than_a_reporting_gap(self):
        """A filer that misses a quarter must not fall out of the index for it."""
        assert C.MAX_SHARE_AGE_DAYS >= 370


class TestTheIndexIsBuiltFromTheCuratedList:
    """Two automated screens selected the wrong universe. The balance-sheet test
    scored Coinbase at 5.5% and a $2m shell at 79%. The text test had Coinbase
    matching four phrases and the same shell five. Nothing the SEC publishes
    encodes whether a company IS a crypto company."""

    def _write(self, tmp_path, cons):
        import json
        p = tmp_path / 'c.json'
        p.write_text(json.dumps({'constituents': cons,
                                 'criterion': 'test',
                                 'ai_rule': {'excluded_on_this_rule': ['IREN', 'WULF']}}))
        return str(p)

    def test_it_loads_the_list_and_its_rules(self, tmp_path):
        p = self._write(tmp_path, {'COIN': {'sleeve': 'exchange', 'name': 'Coinbase'}})
        cons, meta = R.load_constituents(p)
        assert cons['COIN']['sleeve'] == 'exchange'
        assert meta['ai_rule']['excluded_on_this_rule'] == ['IREN', 'WULF']

    def test_a_missing_file_stops_the_run(self):
        """Falling back to the screen would silently rebuild the wrong index."""
        cons, meta = R.load_constituents('/nonexistent.json')
        assert cons == {} and meta == {}

    def test_the_sleeve_always_comes_from_the_file(self):
        """No text guess can override a decision. The provisional path exists
        for the screen, and the screen no longer selects constituents."""
        import inspect
        src = inspect.getsource(R.main)
        assert "'assigned in the constituent file'" in src
        assert 'sleeve_for(r, assigned)' not in src

    def test_coinbase_and_circle_are_in_the_shipped_list(self):
        """The two companies the balance-sheet screen threw out are the two
        largest listed crypto businesses in America."""
        import os, json
        p = os.path.join(os.path.dirname(__file__), '..', 'src', 'cec', 'constituents.json')
        if not os.path.exists(p):
            import pytest; pytest.skip('constituent file not installed')
        d = json.load(open(p, encoding='utf-8'))
        assert 'COIN' in d['constituents'] and 'CRCL' in d['constituents']

    def test_the_completed_ai_pivots_are_out(self):
        """IREN holds zero bitcoin and reaches ~71% AI revenue; TeraWulf is
        exiting mining entirely. Keeping them means measuring the AI trade."""
        import os, json
        p = os.path.join(os.path.dirname(__file__), '..', 'src', 'cec', 'constituents.json')
        if not os.path.exists(p):
            import pytest; pytest.skip('constituent file not installed')
        d = json.load(open(p, encoding='utf-8'))
        for t in ('IREN', 'WULF'):
            assert t not in d['constituents'] and t in d['excluded']

    def test_every_constituent_has_a_sleeve(self):
        import os, json
        p = os.path.join(os.path.dirname(__file__), '..', 'src', 'cec', 'constituents.json')
        if not os.path.exists(p):
            import pytest; pytest.skip('constituent file not installed')
        d = json.load(open(p, encoding='utf-8'))
        sleeves = {'treasury', 'mining', 'exchange', 'infrastructure',
                   'stablecoin', 'tokenization'}
        for tk, v in d['constituents'].items():
            assert v.get('sleeve') in sleeves, f'{tk} has sleeve {v.get("sleeve")}'


class TestAFailedRequestIsNotAFinding:
    """shares_series returned [] for any error, so a request that timed out
    looked identical to a company that tags nothing. Strategy came back with "no
    shares-outstanding series" while the treasury module read the same endpoint
    successfully - its companyfacts file is tens of megabytes and the request
    did not finish.

    A failure that reads as a finding is the shape of nearly every bug in this
    build."""

    def test_a_fetch_failure_raises_rather_than_returning_empty(self):
        def boom(url, **k):
            raise RuntimeError('timed out')
        C._get = boom
        import pytest
        with pytest.raises(C.FetchFailed):
            C.shares_series(1050446)

    def test_the_error_names_the_company(self):
        def boom(url, **k):
            raise RuntimeError('timed out')
        C._get = boom
        try:
            C.shares_series(1050446)
        except C.FetchFailed as e:
            assert '1050446' in str(e)

    def test_a_company_that_tags_nothing_still_returns_empty(self):
        """The other half of the distinction: an empty answer is still empty
        when the request succeeded."""
        C._get = lambda u, **k: b'{"facts": {}}'
        assert C.shares_series(1) == []

    def test_the_timeout_is_long_enough_for_a_large_filer(self):
        """Strategy's companyfacts runs to tens of megabytes. Read from the
        SOURCE rather than the function, because earlier tests replace _get."""
        import io as _io, os
        src = _io.open(os.path.join(os.path.dirname(__file__), '..', 'fetch', 'cec.py'),
                       encoding='utf-8').read()
        assert 'timeout=120' in src, 'a large filer needs more than the default'


class TestTheConceptsLargeFilersActuallyUse:
    """Strategy, Circle and Block tag NEITHER dei:EntityCommonStockSharesOutstanding
    NOR us-gaap:CommonStockSharesOutstanding. I chose those two names in the
    first hour without checking what the biggest filers use, and it kept the
    largest corporate bitcoin treasury in the world out of a crypto index."""

    MSTR = {'us-gaap': {'WeightedAverageNumberOfSharesOutstandingBasic': {'units': {'shares': [
        {'val': 3.3e8, 'start': '2026-01-01', 'end': '2026-03-31', 'filed': '2026-05-05'},
        {'val': 352534000.0, 'start': '2026-01-01', 'end': '2026-06-30', 'filed': '2026-08-03'}]}}}}

    def _s(self, facts):
        C._get = lambda u, **k: __import__('json').dumps({'facts': facts}).encode()
        return C.shares_series(1050446)

    def test_the_weighted_average_is_used_when_nothing_instant_exists(self):
        s = self._s(self.MSTR)
        assert s and s[-1][1] == 352534000.0

    def test_the_instant_count_still_wins_where_it_exists(self):
        """The fallback must never displace a real count."""
        f = dict(self.MSTR)
        f['dei'] = {'EntityCommonStockSharesOutstanding': {'units': {'shares': [
            {'val': 2.8e8, 'end': '2026-06-30', 'filed': '2026-08-03'}]}}}
        assert self._s(f)[-1][1] == 2.8e8

    def test_the_basis_is_recorded(self):
        """An approximation must never be mistaken for a reading."""
        assert 'weighted average' in C.shares_basis(1, self.MSTR)
        f = {'dei': {'EntityCommonStockSharesOutstanding': {'units': {'shares': [
            {'val': 1e8, 'end': '2026-06-30', 'filed': '2026-08-03'}]}}}}
        assert C.shares_basis(1, f) == 'instant'

    def test_the_caveat_names_what_it_understates(self):
        """Treasury companies fund purchases by issuing stock, so a weighted
        average understates exactly the constituents that dilute most."""
        assert 'lags mid-quarter issuance' in C.shares_basis(1, self.MSTR)

    def test_strategy_is_no_longer_empty(self):
        assert len(self._s(self.MSTR)) == 2
