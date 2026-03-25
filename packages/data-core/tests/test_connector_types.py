import unittest

from data_core.connector_types import FetchResult


class TestFetchResult(unittest.TestCase):
    def test_success_result(self):
        r = FetchResult(status=200, data={"revenue": 100}, source_label="fmp:income")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.data, {"revenue": 100})
        self.assertIsNone(r.error)

    def test_error_result(self):
        r = FetchResult(status=429, error="rate limited", source_label="fmp:income")
        self.assertEqual(r.status, 429)
        self.assertIsNone(r.data)
        self.assertEqual(r.error, "rate limited")

    def test_defaults(self):
        r = FetchResult(status=200)
        self.assertIsNone(r.data)
        self.assertIsNone(r.error)
        self.assertEqual(r.latency_ms, 0.0)
        self.assertEqual(r.source_label, "")


if __name__ == "__main__":
    unittest.main()
