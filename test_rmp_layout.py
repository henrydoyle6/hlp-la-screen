"""Real-schema regressions: nameless spot metadata and the locus layout."""
import copy
import json
import unittest
from unittest.mock import patch
import hlp_la_screen as app
import input_discovery as discovery
import rmp_l4
import state_store as store
from test_support import Fixture,fixture,Log


def locus_fixture(late=False,nameless=False,pad=0):
    value=fixture();ex=value['exchange']
    cls=[dex.pop('clearinghouse') for dex in ex['perp_dexs']]
    if pad:cls[0]['large_unrelated_state']=b'x'*pad
    locus=dict(cls=cls,scl=ex.pop('spot_clearinghouse'),ctx=ex.pop('context'))
    if nameless:locus['scl']['meta']['spot_infos'][0].pop('name')
    if late:ex['locus']=locus
    else:value['exchange']={'locus':locus,**ex}
    return value


class LayoutTests(Fixture):
    def extract(self,value,coins):
        path=self.snapshot(value=value)
        log=Log();cache=store.Cache(self.root/'isolated.sqlite',log)
        self.addCleanup(cache.close)
        ref=app.SnapshotRef(str(path),100,app.parse_time('2026-01-01T00:00:00Z'))
        result=rmp_l4.ExtractionCache(cache,log).ensure(ref,set(coins))
        return result,cache,log

    def test_legacy_unrequested_nameless_spot_does_not_poison_perp(self):
        value=fixture();value['exchange']['spot_clearinghouse']['meta']['spot_infos'][0].pop('name')
        result,cache,_=self.extract(value,['BTC','xyz:ALPHA'])
        self.assertTrue(all(isinstance(v,str) for v in result.values()))
        self.assertEqual(cache.read('extract',result['BTC'])['orders'][0]['sz'],'2.5')

    def test_unknown_requested_name_is_not_fabricated_from_spot_tokens(self):
        result,cache,_=self.extract(locus_fixture(nameless=True),['BTC','@TEST'])
        self.assertIsInstance(result['BTC'],str)
        self.assertEqual(result['@TEST'].code,'RMP_REQUIRED_MARKET_UNRESOLVED')
        self.assertEqual(cache.db.execute("SELECT count(*) FROM entries WHERE kind='extract'").fetchone()[0],1)

    def test_locus_metadata_before_and_after_books(self):
        for late in (False,True):
            with self.subTest(late=late):
                result,cache,log=self.extract(locus_fixture(late),['BTC','xyz:ALPHA','@TEST'])
                self.assertTrue(all(isinstance(v,str) for v in result.values()))
                self.assertEqual(cache.read('extract',result['xyz:ALPHA'])['orders'][0]['limitPx'],'5')
                self.assertEqual(cache.db.execute('SELECT count(*) FROM rmp_pending').fetchone()[0],0)

    def test_locus_actual_pdi_not_metadata_array_position(self):
        value=locus_fixture();value['exchange']['locus']['cls'].reverse()
        result,cache,_=self.extract(value,['BTC','xyz:ALPHA'])
        self.assertEqual(cache.read('extract',result['BTC'])['orders'][0]['limitPx'],'12.345')
        self.assertIsInstance(result['xyz:ALPHA'],str)

    def test_late_context_header_reads_real_offset_and_caches(self):
        self.snapshot(value=locus_fixture(pad=2*1024*1024))
        header=discovery.rmp_header(self.root/'snapshots'/'100.rmp')
        self.assertEqual(header['block_number'],100);self.assertEqual(header['schema'],'locus')
        self.assertGreater(header['context_offset'],2*1024*1024)
        self.assertGreater(header['header_bytes_read'],1024*1024)
        self.run_stage()
        self.assertEqual(self.rows(2)[0]['status'],'book_ready')

    def test_selected_metadata_failure_local_to_coin(self):
        value=locus_fixture();value['exchange']['locus']['scl']['meta']['token_infos'][0]['spec'].pop('szDecimals')
        result,cache,_=self.extract(value,['BTC','@TEST'])
        self.assertIsInstance(result['BTC'],str)
        self.assertEqual(result['@TEST'].code,'RMP_COIN_CONVERSION_FAILED')
        self.assertIn('spot asset=0',str(result['@TEST']))

    def test_locus_header_real_height_time_not_filename(self):
        path=self.snapshot(name='999999.rmp',height=101,value=locus_fixture())
        header=discovery.rmp_header(path)
        self.assertEqual(header['block_number'],100)

    def test_locus_conflicting_context_never_publishes(self):
        value=locus_fixture();value['exchange']['context']=dict(height=101,time='2026-01-01T00:00:00Z')
        result,cache,_=self.extract(value,['BTC'])
        self.assertIsInstance(result['BTC'],rmp_l4.ExtractionError)
        self.assertEqual(cache.db.execute("SELECT count(*) FROM entries WHERE kind='extract'").fetchone()[0],0)

    def test_malformed_locus_metadata_is_source_error_not_program_failure(self):
        value=locus_fixture();value['exchange']['locus']['cls'][0]['meta']=None
        result,cache,_=self.extract(value,['BTC'])
        self.assertIsInstance(result['BTC'],rmp_l4.ExtractionError)
        self.assertEqual(result['BTC'].code,'RMP_SCAN_FAILED')
        self.assertIn('must be a map',str(result['BTC']))
        self.assertEqual(cache.db.execute("SELECT count(*) FROM entries WHERE kind='extract'").fetchone()[0],0)

    def test_duplicate_requested_market_does_not_publish_ambiguous_book(self):
        value=locus_fixture();books=value['exchange']['perp_dexs'][0]['books']
        books.append(copy.deepcopy(books[0]))
        result,cache,_=self.extract(value,['BTC','@TEST'])
        self.assertEqual(result['BTC'].code,'RMP_AMBIGUOUS_MARKET')
        self.assertIsInstance(result['@TEST'],str)
        self.assertEqual(cache.db.execute("SELECT count(*) FROM entries WHERE kind='extract'").fetchone()[0],1)

    def test_original_native_compare_accepts_locus_and_rejects_wrong_answer(self):
        from test_order_book_replay import compare_multiple_snapshots
        before=self.snapshot(value=locus_fixture(nameless=True))
        after_value=locus_fixture(nameless=True)
        after_value['exchange']['locus']['ctx'].update(height=102,time='2026-01-01T01:00:00Z')
        after=self.snapshot('102.rmp',value=after_value)
        diff=self.hour('diffs',1,[self.block(101,'2026-01-01T00:30:00Z'),self.block(102,'2026-01-01T01:00:00Z')])
        args=dict(before_state=before,after_state=after,before_block=100,before_time='2026-01-01T00:00:00Z',
            target_block=102,coins=['BTC','xyz:ALPHA'],diffs=[diff])
        self.assertEqual(compare_multiple_snapshots(**args)['status'],'PASS')
        after_value['exchange']['perp_dexs'][0]['books'][0]['book_orders'][91]['r']=251
        self.snapshot('102.rmp',value=after_value)
        self.assertEqual(compare_multiple_snapshots(**args)['status'],'FAIL')
