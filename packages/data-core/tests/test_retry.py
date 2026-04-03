import unittest

from catalyst_data.retry import (
    ErrorClass,
    classify_error,
    compute_backoff,
    should_retry,
    MAX_RETRIES,
)


class TestClassifyError(unittest.TestCase):
    def test_401_immediate_fallback(self):
        self.assertEqual(classify_error(401), ErrorClass.IMMEDIATE_FALLBACK)

    def test_403_immediate_fallback(self):
        self.assertEqual(classify_error(403), ErrorClass.IMMEDIATE_FALLBACK)

    def test_429_retryable(self):
        self.assertEqual(classify_error(429), ErrorClass.RETRYABLE)

    def test_500_retryable(self):
        self.assertEqual(classify_error(500), ErrorClass.RETRYABLE)

    def test_502_retryable(self):
        self.assertEqual(classify_error(502), ErrorClass.RETRYABLE)

    def test_503_retryable(self):
        self.assertEqual(classify_error(503), ErrorClass.RETRYABLE)

    def test_504_retryable(self):
        self.assertEqual(classify_error(504), ErrorClass.RETRYABLE)

    def test_timeout_retryable(self):
        self.assertEqual(
            classify_error(None, is_timeout=True), ErrorClass.RETRYABLE
        )

    def test_200_no_fallback(self):
        self.assertEqual(classify_error(200), ErrorClass.NO_FALLBACK)

    def test_404_no_fallback(self):
        self.assertEqual(classify_error(404), ErrorClass.NO_FALLBACK)

    def test_none_status_no_timeout_no_fallback(self):
        self.assertEqual(classify_error(None), ErrorClass.NO_FALLBACK)


class TestComputeBackoff(unittest.TestCase):
    def test_attempt_1_is_2s(self):
        self.assertAlmostEqual(compute_backoff(1), 2.0)

    def test_attempt_2_is_4s(self):
        self.assertAlmostEqual(compute_backoff(2), 4.0)

    def test_attempt_3_is_8s(self):
        self.assertAlmostEqual(compute_backoff(3), 8.0)

    def test_zero_attempt_raises(self):
        with self.assertRaises(ValueError):
            compute_backoff(0)


class TestShouldRetry(unittest.TestCase):
    def test_below_max_true(self):
        for i in range(1, MAX_RETRIES):
            self.assertTrue(should_retry(i))

    def test_at_max_false(self):
        self.assertFalse(should_retry(MAX_RETRIES))

    def test_above_max_false(self):
        self.assertFalse(should_retry(MAX_RETRIES + 5))


if __name__ == "__main__":
    unittest.main()
