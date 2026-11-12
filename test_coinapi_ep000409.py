"""A failed real-data pilot must stay unusable and preserve its exact evidence."""
import unittest
from tools.verify_coinapi_ep000409 import verify


class CoinAPIFailedPilotTests(unittest.TestCase):
    def test_failed_source_is_skipped_and_comparison_is_exact(self):
        result=verify()
        self.assertEqual(result['source_validation'],'FAILED')
        self.assertEqual(result['successful'],0)
        self.assertEqual(result['extra_orders'],133)


if __name__=='__main__':unittest.main()
