"""A failed real-data pilot must stay unusable and preserve its exact evidence."""
import unittest
import json
from pathlib import Path
import tempfile
import zipfile
from tools.verify_coinapi_ep000409 import verify
from tools.coinapi_ep000409_replay import read_boundary_metadata
from official_l2 import parse_time_ns


class CoinAPIFailedPilotTests(unittest.TestCase):
    def test_failed_source_is_skipped_and_comparison_is_exact(self):
        result=verify()
        self.assertEqual(result['source_validation'],'FAILED')
        self.assertEqual(result['successful'],0)
        self.assertEqual(result['extra_orders'],133)

    def test_snapshot_before_hour_boundary_can_be_in_next_hour_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for hour,height,stamp in ((5,985795306,'2026-05-06T05:59:59.760892197'),(6,985795307,'2026-05-06T05:59:59.850436904')):
                with zipfile.ZipFile(root/(str(hour)+'.zip'),'w') as archive:
                    archive.writestr('hourly/20260506/'+str(hour),json.dumps(dict(block_number=height,block_time=stamp,events=[]))+'\n')
            result=read_boundary_metadata(root,parse_time_ns('2026-05-06T05:59:59.8504369Z'))
            self.assertEqual(result['block'],985795307)
            self.assertEqual(Path(result['source']).name,'6.zip')
            self.assertTrue(result['metadata_only'])
            self.assertFalse(result['native_bridge_applied'])


if __name__=='__main__':unittest.main()
