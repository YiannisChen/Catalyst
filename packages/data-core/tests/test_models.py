import unittest
from datetime import datetime

from pydantic import ValidationError

from data_core.models import CatalystDataRequest, DataAsset


class TestCatalystDataRequestValid(unittest.TestCase):
    def test_valid_request_fields(self):
        request = CatalystDataRequest(
            ticker="NVDA",
            date="2026-03-20",
            sources=["fmp_fundamentals", "gdelt_news"],
            force_refresh=True,
        )
        self.assertEqual(request.ticker, "NVDA")
        self.assertEqual(request.date, "2026-03-20")
        self.assertEqual(request.sources, ["fmp_fundamentals", "gdelt_news"])
        self.assertTrue(request.force_refresh)

    def test_ticker_is_uppercased(self):
        request = CatalystDataRequest(
            ticker="nvda",
            date="2026-03-20",
            sources=["fmp_fundamentals"],
        )
        self.assertEqual(request.ticker, "NVDA")

    def test_force_refresh_defaults_to_false(self):
        request = CatalystDataRequest(
            ticker="NVDA",
            date="2026-03-20",
            sources=["fmp_fundamentals"],
        )
        self.assertFalse(request.force_refresh)


class TestCatalystDataRequestRejection(unittest.TestCase):
    def test_empty_ticker_rejected(self):
        with self.assertRaises(ValidationError):
            CatalystDataRequest(
                ticker="",
                date="2026-03-20",
                sources=["fmp_fundamentals"],
            )

    def test_whitespace_only_ticker_rejected(self):
        with self.assertRaises(ValidationError):
            CatalystDataRequest(
                ticker="   ",
                date="2026-03-20",
                sources=["fmp_fundamentals"],
            )

    def test_invalid_date_format_rejected(self):
        with self.assertRaises(ValidationError):
            CatalystDataRequest(
                ticker="NVDA",
                date="banana",
                sources=["fmp_fundamentals"],
            )

    def test_wrong_date_order_rejected(self):
        with self.assertRaises(ValidationError):
            CatalystDataRequest(
                ticker="NVDA",
                date="03-20-2026",
                sources=["fmp_fundamentals"],
            )

    def test_impossible_date_rejected(self):
        with self.assertRaises(ValidationError):
            CatalystDataRequest(
                ticker="NVDA",
                date="2026-02-30",
                sources=["fmp_fundamentals"],
            )

    def test_empty_sources_rejected(self):
        with self.assertRaises(ValidationError):
            CatalystDataRequest(
                ticker="NVDA",
                date="2026-03-20",
                sources=[],
            )

    def test_missing_required_field_rejected(self):
        with self.assertRaises(ValidationError):
            CatalystDataRequest(ticker="NVDA", date="2026-03-20")


class TestDataAssetValid(unittest.TestCase):
    def test_defaults_and_types(self):
        asset = DataAsset(
            asset_id="abc123",
            ticker="NVDA",
            source_type="fmp_fundamentals",
            reference_date_utc=datetime(2026, 3, 20, 20, 0, 0),
            reference_date_et="2026-03-20 16:00 ET",
            last_updated=datetime(2026, 3, 20, 20, 1, 0),
            content_raw=b"compressed-bytes",
            content_clean="|col1|col2|\n|---|---|\n|1|2|",
        )
        self.assertEqual(asset.data_version, "v1")
        self.assertTrue(asset.is_final)
        self.assertIsInstance(asset.content_raw, bytes)
        self.assertIsInstance(asset.metadata, dict)

    def test_content_raw_defaults_to_none(self):
        asset = DataAsset(
            asset_id="abc123",
            ticker="NVDA",
            source_type="fmp_fundamentals",
            reference_date_utc=datetime(2026, 3, 20, 20, 0, 0),
            reference_date_et="2026-03-20 16:00 ET",
            last_updated=datetime(2026, 3, 20, 20, 1, 0),
            content_clean="some text",
        )
        self.assertIsNone(asset.content_raw)


class TestDataAssetRejection(unittest.TestCase):
    def test_non_serializable_metadata_rejected(self):
        with self.assertRaises(ValidationError):
            DataAsset(
                asset_id="abc123",
                ticker="NVDA",
                source_type="fmp_fundamentals",
                reference_date_utc=datetime(2026, 3, 20, 20, 0, 0),
                reference_date_et="2026-03-20 16:00 ET",
                last_updated=datetime(2026, 3, 20, 20, 1, 0),
                content_clean="text",
                metadata={"bad_value": datetime(2026, 1, 1)},
            )


if __name__ == "__main__":
    unittest.main()
