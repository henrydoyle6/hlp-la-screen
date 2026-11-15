"""Curated artifact integrity, including every original CSV1 cell."""
import hashlib,json,unittest
from pathlib import Path
from tools.validate_csv1_victim_prices import check
ROOT=Path(__file__).resolve().parent

class VictimPriceResultTests(unittest.TestCase):
    def test_curated_prices_preserve_source_and_match_validation(self):
        folder=ROOT/'results/csv1_full_20250322_20261031'
        source=folder/'csv1_20250322_20261031_account_value_zh.csv'
        output=folder/'csv1_20250322_20261031_victim_prices_zh.csv'
        evidence=json.loads((folder/'victim_price_validation.json').read_text(encoding='utf-8'))
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),
                         '08b8bb70601e974deea1336b5aaacee0203ec2f6c81a36a6fb5199ab6e072743')
        actual=check(output,source)
        for key in ['sha256','bytes','columns','rows','counts']:
            self.assertEqual(actual[key],evidence[key],key)

if __name__=='__main__':unittest.main()
