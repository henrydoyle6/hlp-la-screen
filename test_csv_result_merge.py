import copy
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

import csv_result_merge as merge
import official_l2
import state_store


def fixture(direction='long',qty='4',levels=None):
    levels=levels if levels is not None else [{'price':'110','qty':'2','n':3},{'price':'100','qty':'5','n':2}]
    ep={k:'' for k in merge.pipeline.SUMMARY}
    ep.update(episode_id='EP000001',episode_input_hash='fixture-hash',coin='BTC',direction=direction,
              total_qty=qty,entry_notional=str(int(qty)*100),entry_vwap='100',event_count='1',
              episode_start='2025-10-10T00:00:00.000Z',tn='2025-10-10T00:00:00.000Z',target_block='',
              partial_episode='false')
    row={k:'' for k in merge.CSV2_FIELDS}
    for k in ['episode_id','episode_input_hash','coin','direction','total_qty','entry_notional','entry_vwap','tn','target_block','partial_episode']:
        row[k]=ep[k]
    row.update(schema_version='hlp-csv-v3',run_id='source',record_type='episode',status='book_ready',issues_json='[]',
               book_side='bid' if direction=='long' else 'ask',book_state_point='post_takeover_l2_snapshot',
               snapshot_time='2025-10-10T00:00:00.250Z',snapshot_lag_ms='250')
    sides=(levels,[]) if direction=='long' else ([],levels)
    row.update({k:merge.pipeline.value_text(v) for k,v in official_l2.book_metrics(SimpleNamespace(levels=sides),direction).items()})
    return ep,row


class L2CSV3Tests(unittest.TestCase):
    def test_long_partial_level_does_not_multiply_n_or_invent_order_count(self):
        ep,row=fixture();before=copy.deepcopy(row)
        result=merge.calculate(ep,row,'new')
        self.assertEqual((result['filled_qty'],result['close_notional'],result['la']),('4','420','20'))
        self.assertEqual(result['executed_order_count'],'')
        self.assertEqual(result['executed_price_level_count'],'2')
        self.assertTrue(all(t['oid'] is None for t in merge.json.loads(result['fill_trace_json'])))
        self.assertEqual(row,before)

    def test_short_sign_and_full_level_order_count(self):
        ep,row=fixture('short','5',[{'price':'101','qty':'2','n':3},{'price':'102','qty':'3','n':2}])
        result=merge.calculate(ep,row,'new')
        self.assertEqual((result['close_notional'],result['la']),('508','-8'))
        self.assertEqual(result['executed_order_count'],'5')
        self.assertEqual(result['status'],'calculated_full')

    def test_insufficient_depth_uses_cost_only_for_filled_quantity(self):
        ep,row=fixture('long','10',[{'price':'90','qty':'3','n':7}])
        result=merge.calculate(ep,row,'new')
        self.assertEqual((result['filled_qty'],result['unfilled_qty'],result['entry_cost_of_filled_qty'],result['la']),('3','7','300','-30'))
        self.assertEqual((result['status'],result['depth_insufficient']),('calculated_partial','true'))
        self.assertEqual(result['executed_order_count'],'7')

    def test_partial_single_order_level_has_known_count(self):
        ep,row=fixture('long','1',[{'price':'90','qty':'3','n':1}])
        self.assertEqual(merge.calculate(ep,row,'new')['executed_order_count'],'1')

    def test_missing_snapshot_preserves_skip_and_blank_la(self):
        ep,row=fixture();row.update(status='skipped',reason_code='NO_L2_AT_OR_AFTER_TN_IN_RANGE',orders_json='')
        result=merge.calculate(ep,row,'new')
        self.assertEqual((result['status'],result['reason_code'],result['la']),('skipped','NO_L2_AT_OR_AFTER_TN_IN_RANGE',''))
        self.assertEqual(result['csv2_row_hash'],state_store.digest(row))

    def test_empty_book_does_not_turn_into_missing_snapshot(self):
        ep,row=fixture('long','1',[])
        result=merge.calculate(ep,row,'new')
        self.assertEqual((result['status'],result['filled_qty'],result['unfilled_qty']),('empty_book','0','1'))

    def test_invalid_level_is_rejected(self):
        ep,row=fixture();row['orders_json']='[{"price":"10","qty":"-2","n":1}]'
        with self.assertRaisesRegex(ValueError,'Invalid L2 price'):
            merge.calculate(ep,row,'new')

    def test_business_matching_preserves_legacy_id_namespace_but_not_quantity_changes(self):
        old=dict(event_id='legacy',event_time='2025-10-22T00:00:00Z',block_number='42',
                 coin='BTC',direction='long',price='100',qty='2')
        current={**old,'event_id':'new-hash'}
        self.assertEqual(merge.event_business([old]),merge.event_business([current]))
        current['qty']='3'
        self.assertNotEqual(merge.event_business([old]),merge.event_business([current]))

    def test_six_file_results_resume_and_detect_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'run';config={'version':merge.VERSION,'inputs':'fixture'}
            hashes=merge.write_run(out,[[],[],[]],config)
            self.assertEqual(hashes,merge.write_run(out,[[],[],[]],config))
            self.assertEqual(len(list(out.iterdir())),6)
            with self.assertRaisesRegex(ValueError,'Different inputs'):
                merge.write_run(out,[[],[],[]],{**config,'inputs':'changed'})
            raw=(out/'csv3.csv').read_bytes();(out/'csv3.csv').write_bytes(b'X'+raw[1:])
            with self.assertRaises(state_store.CheckpointError):
                merge.write_run(out,[[],[],[]],config)


if __name__=='__main__':unittest.main()
