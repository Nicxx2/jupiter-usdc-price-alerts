import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from community_rules import evaluate_rules, normalize_rules_config
from rule_history import (
    delete_rule_history,
    get_rule_history,
    maintain_rule_history,
    mark_rule_history_gaps,
    record_rule_history,
)


MINT = "So11111111111111111111111111111111111111112"


def rules_config(rule_type="min_holders", target=1000, *, enabled=True, amount_mode="tracked_usdc", token_amount=None):
    return normalize_rules_config({
        "enabled": True,
        "alert_enabled": False,
        "sell_amount_mode": amount_mode,
        "sell_token_amount": token_amount,
        "items": [{"type": rule_type, "enabled": enabled, "target": target}],
    })


def token_info(updated_at, holders=1000):
    return {
        "holderCount": holders,
        "mcap": 1_000_000,
        "liquidity": 100_000,
        "stats24h": {"buyVolume": 55_000, "sellVolume": 45_000},
        "updatedAt": updated_at,
    }


class RuleHistoryTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(dir=os.getcwd(), suffix=".sqlite3", delete=False)
        self.path = handle.name
        handle.close()
        self.now = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self.path}{suffix}")
            except FileNotFoundError:
                pass


    def evaluation(self, config, source_time, *, observed_time=None, holders=1000, fetch_error=""):
        observed = observed_time or source_time
        return evaluate_rules(
            config,
            token_info(source_time.isoformat(), holders=holders) if not fetch_error else None,
            None,
            evaluated_at=observed.isoformat(),
            fetch_error=fetch_error,
        )

    def test_repeated_provider_timestamp_is_deduplicated(self):
        config = rules_config()
        first = self.evaluation(config, self.now)
        later = self.evaluation(config, self.now, observed_time=self.now + timedelta(minutes=2))

        self.assertEqual(record_rule_history(MINT, config, first, path=self.path), 1)
        self.assertEqual(record_rule_history(MINT, config, later, path=self.path), 0)

        payload = get_rule_history(MINT, "min_holders", "24h", path=self.path, now=self.now + timedelta(minutes=3))
        points = [point for point in payload["points"] if point["kind"] == "point"]
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["value"], 1000)
        self.assertEqual(points[0]["source_timestamp"], self.now.isoformat())

    def test_unknown_creates_a_gap_and_recovery_is_not_blocked_by_deduplication(self):
        config = rules_config()
        first = self.evaluation(config, self.now)
        unknown = self.evaluation(
            config,
            self.now + timedelta(minutes=2),
            fetch_error="Jupiter token data is unavailable",
        )
        recovered = self.evaluation(
            config,
            self.now,
            observed_time=self.now + timedelta(minutes=4),
        )

        record_rule_history(MINT, config, first, path=self.path)
        record_rule_history(MINT, config, unknown, path=self.path)
        record_rule_history(MINT, config, unknown, path=self.path)
        record_rule_history(MINT, config, recovered, path=self.path)

        payload = get_rule_history(MINT, "min_holders", "24h", path=self.path, now=self.now + timedelta(minutes=5))
        self.assertEqual([point["kind"] for point in payload["points"]], ["point", "gap", "point"])
        gap = payload["points"][1]
        self.assertIsNone(gap["value"])
        self.assertIn("unavailable", gap["reason"])

    def test_target_changes_and_price_impact_scenarios_are_segmented(self):
        first_config = rules_config("max_price_impact", 0.5, amount_mode="tracked_usdc")
        first = evaluate_rules(
            first_config,
            token_info(self.now.isoformat()),
            {
                "value": 0.4,
                "checked_at": self.now.isoformat(),
                "scenario": "tracked_usdc",
                "tracked_usdc": 100,
            },
            evaluated_at=self.now.isoformat(),
        )
        second_config = rules_config("max_price_impact", 1.0, amount_mode="token_amount", token_amount=2500)
        second_time = self.now + timedelta(minutes=2)
        second = evaluate_rules(
            second_config,
            token_info(second_time.isoformat()),
            {
                "value": 0.8,
                "checked_at": second_time.isoformat(),
                "scenario": "token_amount",
                "token_amount": 2500,
            },
            evaluated_at=second_time.isoformat(),
        )

        record_rule_history(MINT, first_config, first, path=self.path)
        record_rule_history(MINT, second_config, second, path=self.path)

        payload = get_rule_history(MINT, "max_price_impact", "24h", path=self.path, now=second_time)
        self.assertEqual([point["kind"] for point in payload["points"]], ["point", "target", "point"])
        self.assertNotEqual(payload["points"][0]["scenario_key"], payload["points"][-1]["scenario_key"])
        self.assertEqual(payload["points"][1]["target"], 1.0)

    def test_disabled_rule_marks_an_existing_series_as_a_gap(self):
        enabled = rules_config()
        disabled = rules_config(enabled=False)
        record_rule_history(MINT, enabled, self.evaluation(enabled, self.now), path=self.path)
        disabled_runtime = evaluate_rules(
            disabled,
            token_info((self.now + timedelta(minutes=2)).isoformat()),
            None,
            evaluated_at=(self.now + timedelta(minutes=2)).isoformat(),
        )
        record_rule_history(MINT, disabled, disabled_runtime, path=self.path)

        payload = get_rule_history(MINT, "min_holders", "24h", path=self.path, now=self.now + timedelta(minutes=3))
        self.assertEqual(payload["points"][-1]["kind"], "gap")
        self.assertIn("disabled", payload["points"][-1]["reason"].lower())

    def test_long_pause_is_rendered_as_a_gap_when_collection_resumes(self):
        config = rules_config()
        record_rule_history(MINT, config, self.evaluation(config, self.now), path=self.path, max_gap_seconds=360)
        later = self.now + timedelta(hours=1)
        record_rule_history(MINT, config, self.evaluation(config, later), path=self.path, max_gap_seconds=360)

        payload = get_rule_history(MINT, "min_holders", "24h", path=self.path, now=later)
        self.assertEqual([point["kind"] for point in payload["points"]], ["point", "gap", "point"])
        self.assertEqual(payload["points"][1]["reason"], "Rule collection paused")

    def test_settings_transition_marks_an_immediate_gap(self):
        config = rules_config()
        record_rule_history(MINT, config, self.evaluation(config, self.now), path=self.path)
        marked_at = self.now + timedelta(minutes=2)
        self.assertEqual(mark_rule_history_gaps(
            MINT, config, rule_types={"min_holders"},
            observed_at=marked_at.isoformat(), path=self.path,
        ), 1)

        payload = get_rule_history(MINT, "min_holders", "24h", path=self.path, now=marked_at)
        self.assertEqual([point["kind"] for point in payload["points"]], ["point", "gap"])
        self.assertIn("disabled", payload["points"][-1]["reason"].lower())

    def test_history_is_bounded_downsampled_and_deleted_per_mint(self):
        config = rules_config()
        for index in range(180):
            moment = self.now - timedelta(minutes=179 - index)
            holders = 999_999 if index == 91 else 1000 + index
            record_rule_history(
                MINT,
                config,
                self.evaluation(config, moment, holders=holders),
                path=self.path,
            )

        payload = get_rule_history(
            MINT,
            "min_holders",
            "24h",
            path=self.path,
            max_points=120,
            now=self.now,
        )
        self.assertTrue(payload["sampled"])
        self.assertLessEqual(len(payload["points"]), 120)
        sampled_values = [point["value"] for point in payload["points"] if point["kind"] == "point"]
        self.assertIn(999_999, sampled_values)
        self.assertEqual(payload["latest_valid"]["value"], 1179)

        self.assertGreater(delete_rule_history(MINT, path=self.path), 0)
        empty = get_rule_history(MINT, "min_holders", "24h", path=self.path, now=self.now)
        self.assertEqual(empty["points"], [])
        self.assertIsNone(empty["latest_valid"])

    def test_gap_dominated_history_keeps_a_useful_trend_in_every_window(self):
        config = rules_config()
        start = self.now - timedelta(hours=20)
        for index in range(130):
            moment = start + timedelta(minutes=index * 9)
            holders = 999_999 if index == 63 else 1000 + index
            record_rule_history(
                MINT,
                config,
                self.evaluation(config, moment, holders=holders),
                path=self.path,
            )
            unavailable_at = moment + timedelta(minutes=1)
            record_rule_history(
                MINT,
                config,
                self.evaluation(
                    config,
                    unavailable_at,
                    fetch_error="Jupiter token data is unavailable",
                ),
                path=self.path,
            )

        for window in ("24h", "7d", "30d", "90d"):
            with self.subTest(window=window):
                payload = get_rule_history(
                    MINT,
                    "min_holders",
                    window,
                    path=self.path,
                    max_points=120,
                    now=self.now,
                )
                returned = payload["points"]
                valid = [point for point in returned if point["kind"] == "point"]

                self.assertTrue(payload["sampled"])
                self.assertEqual(payload["total_events"], 260)
                self.assertLessEqual(len(returned), 120)
                self.assertGreaterEqual(len(valid), 50)
                self.assertEqual(valid[0]["value"], 1000)
                self.assertEqual(valid[-1]["value"], 1129)
                self.assertIn(999_999, [point["value"] for point in valid])
                self.assertEqual(payload["latest_valid"]["value"], 1129)
                self.assertEqual(returned[-1]["kind"], "gap")

                previous_point_index = None
                for point_index, point in enumerate(returned):
                    if point["kind"] != "point":
                        continue
                    if previous_point_index is not None:
                        self.assertIn(
                            "gap",
                            [
                                item["kind"]
                                for item in returned[previous_point_index + 1:point_index]
                            ],
                        )
                    previous_point_index = point_index

    def test_each_window_applies_its_own_cutoff(self):
        config = rules_config()
        readings = (
            (self.now - timedelta(days=80), 1001),
            (self.now - timedelta(days=20), 1002),
            (self.now - timedelta(days=5), 1003),
            (self.now - timedelta(hours=12), 1004),
        )
        for moment, holders in readings:
            record_rule_history(
                MINT,
                config,
                self.evaluation(config, moment, holders=holders),
                path=self.path,
            )

        expected = {
            "24h": [1004],
            "7d": [1003, 1004],
            "30d": [1002, 1003, 1004],
            "90d": [1001, 1002, 1003, 1004],
        }
        for window, values in expected.items():
            with self.subTest(window=window):
                payload = get_rule_history(MINT, "min_holders", window, path=self.path, now=self.now)
                self.assertEqual(
                    [point["value"] for point in payload["points"] if point["kind"] == "point"],
                    values,
                )

    def test_downsampling_preserves_a_long_outage_between_valid_segments(self):
        config = rules_config()
        start = self.now - timedelta(hours=4)
        for index in range(70):
            moment = start + timedelta(minutes=index)
            record_rule_history(
                MINT,
                config,
                self.evaluation(config, moment, holders=1000 + index),
                path=self.path,
            )

        unavailable_at = self.now - timedelta(hours=2, minutes=30)
        record_rule_history(
            MINT,
            config,
            self.evaluation(
                config,
                unavailable_at,
                fetch_error="Jupiter token data is unavailable",
            ),
            path=self.path,
        )

        recovery = self.now - timedelta(hours=2)
        for index in range(70):
            moment = recovery + timedelta(minutes=index)
            record_rule_history(
                MINT,
                config,
                self.evaluation(config, moment, holders=2000 + index),
                path=self.path,
            )

        payload = get_rule_history(
            MINT,
            "min_holders",
            "24h",
            path=self.path,
            max_points=120,
            now=self.now,
        )
        returned = payload["points"]
        left = max(
            index
            for index, point in enumerate(returned)
            if point["kind"] == "point" and point["value"] < 2000
        )
        right = min(
            index
            for index, point in enumerate(returned)
            if point["kind"] == "point" and point["value"] >= 2000
        )

        self.assertTrue(payload["sampled"])
        self.assertLessEqual(len(returned), 120)
        self.assertIn("gap", [point["kind"] for point in returned[left + 1:right]])

    def test_downsampling_retains_target_transitions(self):
        first_config = rules_config(target=1000)
        second_config = rules_config(target=1100)
        start = self.now - timedelta(hours=3)
        for index in range(130):
            moment = start + timedelta(minutes=index)
            config = first_config if index < 65 else second_config
            record_rule_history(
                MINT,
                config,
                self.evaluation(config, moment, holders=1000 + index),
                path=self.path,
            )

        payload = get_rule_history(
            MINT,
            "min_holders",
            "24h",
            path=self.path,
            max_points=120,
            now=self.now,
        )
        returned = payload["points"]

        self.assertTrue(payload["sampled"])
        self.assertLessEqual(len(returned), 120)
        self.assertEqual(
            [point["target"] for point in returned if point["kind"] == "target"],
            [1100],
        )
        self.assertIn(1000, [point["target"] for point in returned if point["kind"] == "point"])
        self.assertIn(1100, [point["target"] for point in returned if point["kind"] == "point"])

    def test_target_churn_keeps_boundaries_between_sampled_configurations(self):
        start = self.now - timedelta(hours=3)
        for index in range(90):
            moment = start + timedelta(minutes=index)
            config = rules_config(target=1000 + index)
            record_rule_history(
                MINT,
                config,
                self.evaluation(config, moment, holders=2000 + index),
                path=self.path,
            )

        payload = get_rule_history(
            MINT,
            "min_holders",
            "24h",
            path=self.path,
            max_points=120,
            now=self.now,
        )
        returned = payload["points"]
        point_indexes = [
            index
            for index, point in enumerate(returned)
            if point["kind"] == "point"
        ]

        self.assertTrue(payload["sampled"])
        self.assertLessEqual(len(returned), 120)
        self.assertGreater(len(point_indexes), 10)
        self.assertEqual(returned[point_indexes[-1]]["value"], 2089)
        for left, right in zip(point_indexes, point_indexes[1:]):
            left_point = returned[left]
            right_point = returned[right]
            if left_point["target"] == right_point["target"]:
                continue
            self.assertIn(
                "target",
                [point["kind"] for point in returned[left + 1:right]],
            )

    def test_retention_removes_expired_events(self):
        config = rules_config()
        old = self.now - timedelta(days=91)
        current = self.now - timedelta(days=1)
        record_rule_history(MINT, config, self.evaluation(config, old), path=self.path)
        record_rule_history(MINT, config, self.evaluation(config, current), path=self.path)

        maintain_rule_history(path=self.path, now=self.now)
        connection = sqlite3.connect(self.path)
        try:
            count = connection.execute("SELECT COUNT(*) FROM rule_history_events").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 1)

    def test_older_points_are_compacted_without_losing_the_range(self):
        config = rules_config()
        bucket_start = self.now - timedelta(days=14)
        for index in range(10):
            moment = bucket_start + timedelta(minutes=index)
            record_rule_history(
                MINT, config, self.evaluation(config, moment, holders=1000 + index),
                path=self.path,
            )

        maintain_rule_history(path=self.path, now=self.now)
        payload = get_rule_history(MINT, "min_holders", "30d", path=self.path, now=self.now)
        values = [point["value"] for point in payload["points"] if point["kind"] == "point"]
        self.assertLessEqual(len(values), 4)
        self.assertIn(1000, values)
        self.assertIn(1009, values)

    def test_invalid_history_queries_fail_cleanly(self):
        with self.assertRaises(ValueError):
            get_rule_history(MINT, "not_a_rule", "7d", path=self.path, now=self.now)
        with self.assertRaises(ValueError):
            get_rule_history(MINT, "min_holders", "forever", path=self.path, now=self.now)


if __name__ == "__main__":
    unittest.main()
