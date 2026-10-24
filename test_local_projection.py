"""SQLite block-prefix cache never sorts, repairs, rounds, or hides gaps."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from decimal import Decimal
import zipfile
import state_store as store
import prepare_local_diffs as projection
import hlp_la_screen as app
from test_support import Log

class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'0.zip';self.log=Log()
        self.cache=store.Cache(self.root/'cache.sqlite',self.log);self.addCleanup(self.cache.close)
        self.manager=projection.DiffCache({},self.cache,{'BTC'},103,self.log)

    def write(self,rows):
        with zipfile.ZipFile(self.source,'w') as z:z.writestr('hourly/0',''.join(r if isinstance(r,str) else json.dumps(r)+'\n' for r in rows))

    def rows(self,start=100,target=103):return list(self.manager.source_rows(self.source,start,target))

    def test_empty_blocks_same_order_and_decimal_tails_are_retained(self):
        raw='{"block_number":102,"events":[{"coin":"ETH","oid":2},{"coin":"BTC","oid":1,"raw_book_diff":{"new":{"sz":0.123456789012345678901}}}]}\n'
        self.write([{'block_number':101,'events':[]},raw,{'block_number':103,'events':[]}])
        rows=self.rows();self.assertEqual([r['block_number'] for r in rows],[101,102,103])
        self.assertEqual(rows[1]['events'][0]['raw_book_diff']['new']['sz'],'0.123456789012345678901')
        self.assertEqual(rows[1]['source_event_indices'],[1])

    def test_missing_block_is_not_synthesized(self):
        self.write([{'block_number':101,'events':[]},{'block_number':103,'events':[]}])
        rows=self.rows();self.assertEqual([r['block_number'] for r in rows],[101,103])
        with self.assertRaisesRegex(app.BookError,'MISSING_DIFF_BLOCK'):app.replay_blocks(app.OrderBook('BTC'),rows,100,103)

    def test_global_invalid_event_is_not_filtered_as_another_coin(self):
        self.write([{'block_number':101,'events':[{'coin':'ETH'},17,{'oid':1}]}])
        row=self.rows(target=101)[0];self.assertEqual(row['events'],[17,{'oid':1}])
        self.assertEqual(row['source_event_indices'],[1,2])

    def test_conflicting_duplicate_blocks_are_not_sorted_or_merged(self):
        self.write([{'block_number':101,'events':[]},{'block_number':101,'events':[]},{'block_number':103,'events':[]}])
        with self.assertRaisesRegex(app.BookError,'DIFF_BLOCK_ORDER'):app.replay_blocks(app.OrderBook('BTC'),self.rows(),100,103)

    def test_warm_cache_avoids_reopening_compressed_stream(self):
        self.write([{'block_number':101,'events':[]},{'block_number':102,'events':[]},{'block_number':103,'events':[]}])
        expected=self.rows()
        self.manager=projection.DiffCache({},self.cache,{'BTC'},103,self.log)
        with patch.object(app,'open_json_text',side_effect=AssertionError('decompression')):self.assertEqual(self.rows(),expected)
        self.assertEqual(self.cache.stats['DIFF_DECODE_START'],1)

    def test_large_integer_is_not_converted_through_float(self):
        data=projection.loads_exact_selected(b'{"events":[{"coin":"BTC","oid":123456789012345678901234567890}]}',{'BTC'})
        self.assertEqual(data['events'][0]['oid'],123456789012345678901234567890)

    def test_bad_tail_keeps_valid_prefix_for_earlier_target(self):
        self.write([{'block_number':101,'events':[]},'{broken\n',{'block_number':103,'events':[]}])
        self.assertEqual(self.rows(target=101)[0]['block_number'],101)
        with self.assertRaisesRegex(app.BookError,'UNREADABLE_DIFF_LINE'):self.rows(target=103)

    def test_corrupt_cached_block_is_rescanned_locally(self):
        self.write([{'block_number':101,'events':[]},{'block_number':102,'events':[]},{'block_number':103,'events':[]}])
        before=self.rows()
        with self.cache.db:self.cache.db.execute("UPDATE diff_blocks SET body=x'78' WHERE height=102")
        self.assertEqual(self.rows(),before);self.assertEqual(self.cache.stats['DIFF_DECODE_START'],2)

    def test_committed_prefix_survives_decoder_interruption(self):
        self.write([{'block_number':n,'events':[]} for n in range(1,1025)])
        self.manager.stop_block=1024
        fired=[]
        def stop(point,**kw):
            if point=='diff_prefix_committed' and not fired:fired.append(1);raise KeyboardInterrupt()
        with patch.object(store,'fault',side_effect=stop):
            with self.assertRaises(KeyboardInterrupt):self.rows(start=0,target=1024)
        self.cache.db.rollback()
        original=projection.loads_exact_selected;count=[]
        def parse(*args):count.append(1);return original(*args)
        with patch.object(projection,'loads_exact_selected',side_effect=parse):rows=self.rows(start=0,target=1024)
        self.assertEqual(len(rows),1024);self.assertEqual(len(count),512)
