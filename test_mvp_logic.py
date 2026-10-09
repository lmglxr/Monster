from __future__ import annotations

import json
import time
import unittest
from pathlib import Path

from mvp_bot import APP_DIR, AsyncFatigueOCR, Bot, FatigueOCR


class FatigueParsingTests(unittest.TestCase):
    def test_extracts_positive_and_negative_denominator(self) -> None:
        self.assertEqual(FatigueOCR._extract_value(["805/1000"]), 805)
        self.assertEqual(FatigueOCR._extract_value(["805/-1000"]), -805)
        self.assertEqual(FatigueOCR._extract_value(["-805/1000"]), 805)

    def test_rejects_invalid_or_out_of_range_value(self) -> None:
        self.assertIsNone(FatigueOCR._extract_value(["1001/1000"]))
        self.assertIsNone(FatigueOCR._extract_value(["no fatigue here"]))


class AsyncOcrTests(unittest.TestCase):
    def test_worker_result_can_be_polled_without_blocking(self) -> None:
        worker = AsyncFatigueOCR()
        worker._read_snapshot = lambda *_args: (123, ["123/1000"])  # type: ignore[method-assign]
        try:
            self.assertTrue(worker.request(__import__("numpy").zeros((4, 4, 3), dtype="uint8"), [0, 0, 1, 1]))
            self.assertTrue(worker.busy)
            deadline = time.monotonic() + 2.0
            result = None
            while time.monotonic() < deadline and result is None:
                result = worker.poll()
                time.sleep(0.01)
            self.assertEqual(result, (123, ["123/1000"]))
            self.assertFalse(worker.busy)
        finally:
            worker.close()


class PureVisualBotTests(unittest.TestCase):
    def test_preferred_fatigue_path_uses_ocr_method_only(self) -> None:
        bot = Bot.__new__(Bot)
        bot.read_fatigue = lambda attempts, refresh_rounds: 456  # type: ignore[method-assign]
        self.assertEqual(bot.read_fatigue_preferred(attempts=2, refresh_rounds=3), 456)

    def test_close_stops_ocr_worker(self) -> None:
        bot = Bot.__new__(Bot)
        calls: list[str] = []
        bot.ocr = type("OcrStub", (), {"close": lambda self: calls.append("closed")})()
        bot.close()
        self.assertEqual(calls, ["closed"])

    def test_default_configuration_is_pure_visual(self) -> None:
        config = json.loads((APP_DIR / "config.json").read_text(encoding="utf-8"))
        combat = config["combat"]
        self.assertIn("boss_fatigue_gain_threshold", combat)
        self.assertIn("boss_fatigue_confirm_samples", combat)
        self.assertEqual(config["dark_boss"]["identification_mode"], "image")


if __name__ == "__main__":
    unittest.main()
