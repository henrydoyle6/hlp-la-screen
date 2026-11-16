"""Recheck every published U/M against independent exact rational arithmetic."""
import json,unittest
from pathlib import Path
from tools.validate_csv1_um import check

ROOT=Path(__file__).resolve().parent
class EventUMResultTests(unittest.TestCase):
    def test_all_published_rows_match_formula_and_original_inputs(self):
        folder=ROOT/'results/csv1_full_20250322_20261031'
        actual=check(folder/'csv1_20250322_20261031_um_zh.csv',folder/'csv1_20250322_20261031_selected_entries_zh.csv')
        expected=json.loads((folder/'um_validation.json').read_text(encoding='utf-8'))
        for key in ['status','sha256','bytes','columns','data_rows','baseline_sha256','counts','missing_event_details']:
            self.assertEqual(actual[key],expected[key],key)
        self.assertEqual(expected['native_direction_verification']['checked_events'],22044)
        self.assertFalse(expected['production_engine_changed'])
        self.assertFalse(expected['rounding_or_zero_clamping'])
        self.assertFalse(expected['group_quantity_used'])
        self.assertFalse(expected['proxy_prices_used'])
        self.assertEqual(expected['aws_requests'],0)
if __name__=='__main__':unittest.main()
