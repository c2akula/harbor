"""Effort levels — the one place numbers live.

The user-facing dial is a label; every token number behind it sits in a
single table so tuning is one edit and provenance is one comment.
"""
import unittest

from harbor import effort


class EffortLevels(unittest.TestCase):
    def test_the_ladder_is_ordered(self):
        budgets = [effort.budget(lvl)
                   for lvl in ("none", "low", "medium", "high")]
        self.assertEqual(budgets, sorted(budgets))

    def test_none_disables_thinking_entirely(self):
        self.assertEqual(effort.budget("none"), 0)

    def test_max_is_uncapped(self):
        self.assertIsNone(effort.budget("max"))

    def test_unknown_label_fails_naming_the_valid_set(self):
        with self.assertRaises(ValueError) as ctx:
            effort.budget("extreme")
        for lvl in ("none", "low", "medium", "high", "max"):
            self.assertIn(lvl, str(ctx.exception))

    def test_every_budget_leaves_answer_room(self):
        """No level may allow thinking to consume the whole output cap."""
        for lvl in ("low", "medium", "high"):
            self.assertLessEqual(effort.budget(lvl),
                                 effort.MAX_TOKENS - effort.ANSWER_RESERVE)


if __name__ == "__main__":
    unittest.main()
