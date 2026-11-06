import unittest

from tests.jetstream_experiment import percentiles


class MessagingSelectionTests(unittest.TestCase):
    def test_percentiles_bounds(self):
        values = [0.001 * i for i in range(1, 101)]
        result = percentiles(values)
        self.assertLessEqual(0.05, result['p50'])
        self.assertLessEqual(result['p50'], 0.051)
        self.assertLessEqual(result['p95'], result['p99'])
        self.assertLessEqual(result['p99'], result['max'])

    def test_percentiles_empty(self):
        self.assertEqual(percentiles([]), {})


if __name__ == '__main__':
    unittest.main()
