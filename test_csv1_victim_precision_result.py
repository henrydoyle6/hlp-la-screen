"""Curated repair scope, original cells and executed notebook integrity."""
import hashlib,json,unittest
from pathlib import Path
from tools.validate_csv1_victim_precision import check

ROOT=Path(__file__).resolve().parent
class VictimPrecisionResultTests(unittest.TestCase):
    def test_only_authorized_price_cells_change_and_audit_is_complete(self):
        folder=ROOT/'results/csv1_full_20250322_20261031'
        output=folder/'csv1_20250322_20261031_victim_prices_precision_zh.csv'
        baseline=folder/'csv1_20250322_20261031_victim_prices_zh.csv'
        audit=folder/'precision_event_audit.json'
        expected=json.loads((folder/'precision_validation.json').read_text(encoding='utf-8'))
        actual=check(output,baseline,audit)
        for key in ['sha256','bytes','columns','data_rows','counts','changed_cells']:
            self.assertEqual(actual[key],expected[key],key)
        self.assertEqual(hashlib.sha256(audit.read_bytes()).hexdigest(),expected['event_audit_sha256'])
        self.assertEqual(expected['aws_requests'],0)
        self.assertFalse(expected['production_engine_changed'])
        notebook=json.loads((folder/'victim_precision_audit.ipynb').read_text(encoding='utf-8'))
        code=[c for c in notebook['cells'] if c['cell_type']=='code']
        self.assertEqual(len(code),5)
        self.assertEqual([c['execution_count'] for c in code],list(range(1,6)))
        self.assertTrue(all(c['outputs'] and all(o['output_type']!='error' for o in c['outputs']) for c in code))
if __name__=='__main__':unittest.main()
