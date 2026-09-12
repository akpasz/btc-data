"""rules.py - exclusions that apply everywhere, defined once.

THREE TIMES TODAY a rule was written in one module and not applied in another:
the reconciliation lived in the universe sweep and the index never called it;
the trust exclusion lived in the treasury sweep and the materiality screen
never called it. Each time the rule looked correct where it was written and
did nothing where it mattered.

A rule that governs the whole system belongs in one place that every module
imports. That is what this is. Nothing here is new logic - it is the same
logic, made impossible to forget.
"""

# §19: a trust or ETF holds crypto FOR ITS SHAREHOLDERS. That is custody, not
# issuer treasury. Counting it puts the same coins in an index twice - once in
# the fund and once in whoever owns the fund - and these vehicles look exactly
# like the largest treasury companies on every balance-sheet measure, because
# their balance sheet IS the holding. A screen on crypto-assets-over-total-
# assets ranks them at 100% and puts them at the top.
TRUST_MARKERS = (
    ' trust', ' etf', ' etp', 'ishares', 'grayscale', 'bitwise ', 'osprey ',
    'franklin ', 'valkyrie', 'wisdomtree', 'invesco', 'vaneck', '21shares',
    'fidelity wise', 'abrdn', 'hashdex', 'proshares', 'defiance ', 'global x ',
)


def is_fund(name):
    """True where the name marks a fund, trust or ETP rather than an operating
    company. Name matching is crude, and it is used only to EXCLUDE - so its
    failure mode is admitting a fund, never rejecting a real company, and every
    exclusion it produces is reported with the marker that triggered it."""
    n = f' {(name or "").lower()} '
    for m in TRUST_MARKERS:
        if m in n:
            return m.strip()
    return None


def fund_reason(marker):
    return (f'fund, trust or ETP (matched "{marker}"): holds crypto for its shareholders, which '
            f'is custody and not issuer treasury (§19). Its balance sheet IS the holding, so it '
            f'ranks at or near 100% on any assets-based screen and would top the index.')
