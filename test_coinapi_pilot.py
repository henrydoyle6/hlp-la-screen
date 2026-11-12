"""Verify the public pilot result against its authoritative input and native answer."""
import unittest
from tools.verify_coinapi_pilot import verify


class CoinapiPilotAcceptance(unittest.TestCase):
    def test_single_approximate_result_matches_native_orders(self):
        report = verify()
        self.assertEqual(report['status'], 'PASS')


if __name__ == '__main__':
    unittest.main()
