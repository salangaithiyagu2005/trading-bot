from __future__ import annotations

import unittest

from nsebot.review import ReviewError, _parse_draft


class ReviewTests(unittest.TestCase):
    def test_draft_requires_valid_tags_and_json_shape(self) -> None:
        result = _parse_draft(
            "2026-11-16",
            '{"tags":["no_signal_correct_skip"],"note":"No entry met the rules."}',
        )
        self.assertEqual(result["day"], "2026-11-16")
        self.assertEqual(result["tags"], ["no_signal_correct_skip"])
        self.assertEqual(result["draft"], "No entry met the rules.")

        invalid = (
            '{"tags":["made_up"],"note":"text"}',
            '{"tags":[],"note":"text"}',
            '{"tags":["bias_wrong","bias_wrong"],"note":"text"}',
            '{"tags":["bias_wrong"],"note":"text","extra":"ignored"}',
        )
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(ReviewError):
                _parse_draft("2026-11-16", response)


if __name__ == "__main__":
    unittest.main()
