import os
import unittest
from decimal import Decimal
from unittest.mock import patch

from usage_history import logs_csv, polish_cost, safe_error_message, summarize, transcription_cost


class PricingTests(unittest.TestCase):
    def test_gpt_transcribe_four_minutes(self):
        self.assertEqual(transcription_cost("gpt-transcribe", 240), Decimal("0.01800000"))

    def test_whisper_one_minute(self):
        self.assertEqual(transcription_cost("whisper-1", 60), Decimal("0.00600000"))

    def test_polish_cost_uses_actual_tokens(self):
        self.assertEqual(polish_cost("gpt-5.4-mini", 1000, 500), Decimal("0.00300000"))

    def test_price_override(self):
        with patch.dict(os.environ, {"TRANSCRIPTION_PRICE_PER_MINUTE": "0.01"}):
            self.assertEqual(transcription_cost("custom", 90), Decimal("0.01500000"))


class PrivacyTests(unittest.TestCase):
    def test_redacts_api_keys(self):
        error = RuntimeError("bad sk-secret_123 and sb_secret_abcdef")
        message = safe_error_message(error)
        self.assertNotIn("sk-secret_123", message)
        self.assertNotIn("sb_secret_abcdef", message)


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{
            "id": "1", "created_at": "2026-09-10T00:00:00Z", "status": "success",
            "audio_duration_seconds": "60.5", "raw_characters": 100,
            "output_characters": 90, "total_cost_usd": "0.005",
        }, {
            "id": "2", "created_at": "2026-09-10T01:00:00Z", "status": "error",
            "audio_duration_seconds": None, "raw_characters": 0,
            "output_characters": 0, "total_cost_usd": "0",
        }]

    def test_summary(self):
        result = summarize(self.rows)
        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["successes"], 1)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["audio_duration_seconds"], 60.5)
        self.assertEqual(result["total_cost_usd"], 0.005)

    def test_csv_has_utf8_bom(self):
        self.assertTrue(logs_csv(self.rows).startswith("\ufeff"))


if __name__ == "__main__":
    unittest.main()
