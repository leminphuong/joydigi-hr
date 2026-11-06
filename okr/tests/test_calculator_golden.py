"""Gate 2: the Python calculator gives JOYOKR v1.6.45's PHP results.

``golden/joyokr_calculator.json`` was produced by running JOYOKR's own
``class-joyokr-calculator.php`` in an isolated PHP process on fake inputs
(``golden/generate_calculator_vectors.php``). Every vector must match
exactly at two decimal places.
"""

import datetime
import json
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase

from okr.services import calculator

GOLDEN = json.loads((Path(__file__).parent / "golden" / "joyokr_calculator.json").read_text(encoding="utf-8"))


def php(value) -> Decimal:
    """A PHP float result (already rounded to 2 places) as an exact Decimal."""
    return Decimal(repr(float(value)))


class GoldenVectorTests(SimpleTestCase):
    def assertAllMatch(self, mismatches, total):
        self.assertGreater(total, 0)
        self.assertEqual(mismatches, [], f"{len(mismatches)} of {total} vectors differ; first: {mismatches[:5]}")

    def test_fixture_comes_from_the_audited_calculator(self):
        self.assertEqual(
            GOLDEN["source_sha256"], "a76621c6ba1e114e39ee8f9cb93814e32595c83a5c234dd19cee20796ae7996b"
        )

    def test_key_result_progress(self):
        rows = GOLDEN["progress"]
        bad = []
        for row in rows:
            got = calculator.progress(Decimal(row["start"]), Decimal(row["target"]), Decimal(row["current"]))
            if got != php(row["result"]):
                bad.append((row, str(got)))
        self.assertAllMatch(bad, len(rows))

    def test_weighted_progress(self):
        rows = GOLDEN["weighted"]
        bad = []
        for row in rows:
            pairs = [(Decimal(item["progress"]), Decimal(item["weight"])) for item in row["items"]]
            got = calculator.weighted_progress(pairs)
            if got != php(row["result"]):
                bad.append((row, str(got)))
        self.assertAllMatch(bad, len(rows))

    def test_expected_progress(self):
        rows = GOLDEN["expected"]
        bad = []
        for row in rows:
            got = calculator.expected_progress(
                datetime.date.fromisoformat(row["start"]),
                datetime.date.fromisoformat(row["end"]),
                datetime.date.fromisoformat(row["as_of"]),
            )
            if got != php(row["result"]):
                bad.append((row, str(got)))
        self.assertAllMatch(bad, len(rows))

    def test_health_status(self):
        rows = GOLDEN["status"]
        bad = [
            row
            for row in rows
            if calculator.status(Decimal(str(row["actual"])), Decimal(str(row["expected"])), Decimal(str(row["threshold"])))
            != row["result"]
        ]
        self.assertAllMatch(bad, len(rows))

    def test_rating_bands(self):
        from okr.models import default_rating_ranges

        rows = GOLDEN["rating"]
        bad = [row for row in rows if calculator.rating(row["score"], default_rating_ranges()) != row["result"]]
        self.assertAllMatch(bad, len(rows))

    def test_weight_total_tolerance(self):
        rows = GOLDEN["weight_total_ok"]
        bad = [row for row in rows if calculator.weight_total_ok(Decimal(row["total"])) != row["result"]]
        self.assertAllMatch(bad, len(rows))

    def test_php_round_two_places(self):
        rows = GOLDEN["round2"]
        bad = [row for row in rows if calculator.round2(float(row["value"])) != php(row["result"])]
        self.assertAllMatch(bad, len(rows))


class CalculatorRuleTests(SimpleTestCase):
    """JOYOKR rules worth naming on their own."""

    def test_inverse_key_result_counts_down(self):
        # Start 9, target 4: lower is better.
        self.assertEqual(calculator.progress(9, 4, Decimal("5.2")), Decimal("76.00"))

    def test_flat_range_is_all_or_nothing(self):
        self.assertEqual(calculator.progress(5, 5, 5), Decimal("100.00"))
        self.assertEqual(calculator.progress(5, 5, 4), Decimal("0.00"))

    def test_gap_equal_to_threshold_is_at_risk(self):
        self.assertEqual(calculator.status(22, 37, 15), calculator.AT_RISK)
        self.assertEqual(calculator.status(Decimal("21.99"), 37, 15), calculator.OFF_TRACK)

    def test_last_cycle_day_already_counts_as_full(self):
        start, end = datetime.date(2026, 10, 1), datetime.date(2026, 12, 31)
        self.assertEqual(calculator.expected_progress(start, end, end), Decimal("100.00"))
        self.assertEqual(calculator.expected_progress(start, end, end - datetime.timedelta(days=1)), Decimal("98.91"))

    def test_unapproved_objective_is_provisional_without_health(self):
        projection = calculator.objective_projection("draft", 0, [(50, 50), (100, 50)], 37, 15)
        self.assertEqual(projection, {"progress": Decimal("75.00"), "provisional": True, "health": None})
        # Approved: the stored progress wins over the key results passed in.
        approved = calculator.objective_projection("approved", Decimal("25"), [(100, 100)], 37, 15)
        self.assertEqual(approved, {"progress": Decimal("25.00"), "provisional": False, "health": "at_risk"})

    def test_weight_total_needs_exactly_100(self):
        self.assertTrue(calculator.weight_total_ok(Decimal("100.00")))
        self.assertFalse(calculator.weight_total_ok(Decimal("99.99")))
        self.assertFalse(calculator.weight_total_ok(Decimal("100.01")))
