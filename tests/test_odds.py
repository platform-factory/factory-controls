"""The registered promotion-odds table, recomputed from the streak recurrence.

The readiness note registers a table of promotion odds under the frozen rule
(N = 20 consecutive clean merges) for independent changes that each fail,
detectably, with probability p. This test recomputes every cell, so the
registered numbers are checked by code rather than by trust. The table is
read from tests/data/promotion-odds-registered.md, a verbatim copy of the
note's paragraph; each cell is compared at the precision it is printed with.

The recurrence: track the length s of the current run of clean merges. Each
change succeeds with probability q = 1 - p (s -> s + 1) or fails (s -> 0); a
run reaching N promotes and stops. P(promoted within m changes) is the mass
absorbed at N after m steps.
"""

import re
import unittest
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from tests import fakes

TABLE = Path(__file__).resolve().parent / "data" / "promotion-odds-registered.md"


def promoted_within(p: float, m: int, n: int = 20) -> float:
    """P(a run of n consecutive successes occurs within m independent trials)."""
    dist = [0.0] * n          # dist[s]: probability the current streak is s, not yet promoted
    dist[0] = 1.0
    absorbed = 0.0
    q = 1.0 - p
    for _ in range(m):
        nxt = [0.0] * n
        for s, mass in enumerate(dist):
            if not mass:
                continue
            nxt[0] += mass * p
            if s + 1 == n:
                absorbed += mass * q
            else:
                nxt[s + 1] += mass * q
        dist = nxt
    return absorbed


def bad_merges_in_l2(p: float, items: int = 5) -> float:
    """The note's last column: 1 - (1 - p)^5."""
    return 1.0 - (1.0 - p) ** items


def rounds_to(value: float, cell: str) -> bool:
    """Whether `value` prints as `cell` at the cell's own precision."""
    pct = cell.endswith("%")
    text = cell.rstrip("%")
    places = len(text.split(".")[1]) if "." in text else 0
    v = Decimal(repr(value * 100 if pct else value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return v == Decimal(text)


def registered_rows() -> list[list[str]]:
    rows = []
    for line in TABLE.read_text().splitlines():
        if re.match(r"^\|\s*[\d.]+%\s*\|", line):
            rows.append([c.strip() for c in line.strip("|").split("|")])
    return rows


class PromotionOddsTest(unittest.TestCase):
    def test_the_frozen_n_and_the_m4_horizon(self):
        cf = fakes.load_cf()
        self.assertEqual(cf["n"], 20)
        # "the streak must end within 22 items": 25 queue items, 3 kept for the L2 programme.
        self.assertEqual(len(cf["queue"]) - 3, 22)

    def test_seven_rows_registered(self):
        self.assertEqual([r[0] for r in registered_rows()],
                         ["1%", "2%", "5%", "10%", "13.9%", "20%", "30%"])

    def test_every_registered_cell_is_what_the_recurrence_gives(self):
        failures = []
        for p_cell, m22, m50, m100, bad in registered_rows():
            p = float(p_cell.rstrip("%")) / 100
            for m, cell in ((22, m22), (50, m50), (100, m100)):
                v = promoted_within(p, m)
                if not rounds_to(v, cell):
                    failures.append(f"p={p_cell}, within {m}: computed {v:.6f}, registered {cell}")
            v = bad_merges_in_l2(p)
            if not rounds_to(v, bad):
                failures.append(f"p={p_cell}, bad merges: computed {v:.6f}, registered {bad}")
        self.assertEqual(failures, [])

    def test_the_figures_quoted_from_issue_24(self):
        # "a class that fails 10% of the time is promoted within 50 changes 46% of the time,
        # and within 100 changes 78%"
        self.assertTrue(rounds_to(promoted_within(0.10, 50), "46%"))
        self.assertTrue(rounds_to(promoted_within(0.10, 100), "78%"))

    def test_recurrence_against_the_closed_form_for_22_items(self):
        # Within 22 trials a run of 20 can start only at trial 1, 2 or 3: q^20 (1 + 2p).
        for p in (0.01, 0.05, 0.139, 0.3):
            q = 1 - p
            self.assertAlmostEqual(promoted_within(p, 22), q ** 20 * (1 + 2 * p), places=12)

    def test_zero_failures_in_twenty_bounds_at_13_9_percent(self):
        # ADR-0024 §8: zero failures in 20 independent trials bounds the failure rate at 13.9%
        # (one-sided 95%): (1 - p)^20 = 0.05.
        p = 1 - 0.05 ** (1 / 20)
        self.assertTrue(rounds_to(p, "13.9%"))

    def test_the_comparison_is_strict_enough_to_catch_a_wrong_cell(self):
        self.assertFalse(rounds_to(promoted_within(0.05, 22), "40%"))
        self.assertFalse(rounds_to(promoted_within(0.30, 22), "0.2%"))
        self.assertTrue(rounds_to(promoted_within(0.30, 22), "0.1%"))


if __name__ == "__main__":
    unittest.main()
