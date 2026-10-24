"""Shared native extraction and exact before/diff/after comparison acceptance."""
import contextlib
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile
import msgpack
from decimal import Decimal
import hlp_la_screen as app
import input_discovery as discovery
import rmp_l4 as ext
import state_store as store
from test_support import fixture,raw_order,Log


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'state.rmp'
        self.write(fixture());self.log=Log()
        self.cache=store.Cache(self.root/'cache.sqlite',self.log);self.addCleanup(self.cache.close)
        self.ref=app.SnapshotRef(str(self.path),100,app.parse_time('2026-01-01T00:00:00Z'))
        self.extractor=ext.ExtractionCache(self.cache,self.log)

    def write(self,value):self.path.write_bytes(msgpack.packb(value,use_bin_type=True))
    def ensure(self,coins,ref=None):return self.extractor.ensure(ref or self.ref,set(coins))
    def payload(self,entry):return self.cache.read('extract',entry)
    def successes(self):return self.cache.db.execute("SELECT count(*) FROM entries WHERE kind='extract'").fetchone()[0]

    def test_all_families_exact_units_remaining_qty_oid_and_same_price(self):
        results=self.ensure(['BTC','@TEST','xyz:ALPHA','EMPTY'])
        self.assertEqual(self.extractor.stats['rmp_scans'],1)
        book=app._book_from_payload(self.payload(results['BTC']),self.ref,'BTC')
        self.assertEqual(book.orders['111'].qty,Decimal('2.5'));self.assertEqual(book.orders['111'].price,Decimal('12.345'))
        self.assertEqual(len(book.side_orders('bid')),2)
        for name,price,qty in [('xyz:ALPHA','5','0.01'),('@TEST','0.0123456','12.5')]:
            order=self.payload(results[name])['orders'][0];self.assertEqual((order['limitPx'],order['sz']),(price,qty))
        self.assertEqual(self.payload(results['EMPTY'])['orders'],[])

    def test_only_demanded_markets_in_success_cache_no_spool_files(self):
        self.ensure(['@TEST']);self.assertEqual(self.successes(),1)
        self.assertEqual(self.cache.db.execute('SELECT count(*) FROM rmp_pending').fetchone()[0],0)
        self.assertEqual({p.name for p in self.root.iterdir()},{'state.rmp','cache.sqlite'})

    def test_metadata_after_perp_and_spot_books_is_one_scan(self):
        self.write(fixture(reversed_meta=True));results=self.ensure(['BTC','xyz:ALPHA','@TEST'])
        self.assertTrue(all(isinstance(x,str) for x in results.values()));self.assertEqual(self.extractor.stats['rmp_scans'],1)

    def test_persistent_success_cache_avoids_second_body_scan(self):
        first=self.ensure(['BTC','@TEST'])
        extractor=ext.ExtractionCache(self.cache,self.log)
        with patch.object(ext,'scan_rmp',side_effect=AssertionError('scan')):second=extractor.ensure(self.ref,{'BTC','@TEST'})
        self.assertEqual(first,second);self.assertEqual(extractor.stats['extract_hits'],2)

    def test_missing_coin_is_not_empty_book_or_success(self):
        result=self.ensure(['MISSING']);self.assertIsInstance(result['MISSING'],ext.ExtractionError)
        self.assertEqual(self.successes(),0)

    def test_truncated_body_and_trailing_bytes_publish_no_partial_success(self):
        original=self.path.read_bytes()
        for bad in [original[:-5],original+b'\x81',original+msgpack.packb(3)]:
            with self.subTest(size=len(bad)):
                self.path.write_bytes(bad);result=self.ensure(['BTC','@TEST'])
                self.assertTrue(all(isinstance(x,ext.ExtractionError) for x in result.values()))
                self.assertEqual(self.successes(),0)

    def test_declared_height_or_time_mismatch_is_not_cached(self):
        for height,tm in [(99,self.ref.time_ms),(100,self.ref.time_ms+1)]:
            result=self.ensure(['BTC'],app.SnapshotRef(str(self.path),height,tm))
            self.assertIsInstance(result['BTC'],ext.ExtractionError);self.assertEqual(self.successes(),0)

    def test_corrupt_cache_entry_rebuilt_without_other_markets_poisoning(self):
        first=self.ensure(['BTC','@TEST'])
        with self.cache.db:self.cache.db.execute("UPDATE entries SET body=x'78' WHERE kind='extract' AND key=?",(first['BTC'],))
        result=self.ensure(['BTC','@TEST']);self.assertIsInstance(result['BTC'],str)
        self.assertEqual(self.extractor.stats['rmp_scans'],2)

    def test_staging_without_success_entry_never_counts_as_hit(self):
        with self.cache.db:self.cache.write('rmp_staging','orphan',{'orders':[]})
        self.ensure(['BTC']);self.assertEqual(self.extractor.stats['rmp_scans'],1)

    def test_identity_or_parser_version_changes_invalidate_entry(self):
        self.ensure(['BTC']);value=fixture();value['exchange']['perp_dexs'][0]['books'][0]['book_orders'][91]['r']=200
        self.write(value);self.ensure(['BTC']);self.assertEqual(self.extractor.stats['rmp_scans'],2)
        other=ext.ExtractionCache(self.cache,self.log,version='test-new-version');other.ensure(self.ref,{'BTC'})
        self.assertEqual(other.stats['rmp_scans'],1)

    def test_bad_selected_coin_does_not_poison_other_market(self):
        value=fixture();value['exchange']['perp_dexs'][0]['books'][0]['book_orders'][91]['c']['l']=-1
        self.write(value);result=self.ensure(['BTC','@TEST'])
        self.assertIsInstance(result['BTC'],ext.ExtractionError);self.assertIsInstance(result['@TEST'],str)
        self.assertEqual(self.successes(),1)

    def test_raw_and_zip_share_native_adapter_no_converter(self):
        path=self.root/'state.rmp.zip'
        with zipfile.ZipFile(path,'w') as z:z.writestr('nested/state.rmp',self.path.read_bytes())
        ref=app.SnapshotRef(str(path),100,self.ref.time_ms)
        self.assertEqual(app.load_snapshot(self.ref,'BTC').normalized(),app.load_snapshot(ref,'BTC').normalized())

    def test_source_mutation_after_scan_never_publishes(self):
        original=ext.scan_rmp
        def mutate(*args):
            value=original(*args)
            with self.path.open('ab') as f:f.write(b'x')
            return value
        with patch.object(ext,'scan_rmp',side_effect=mutate):result=self.ensure(['BTC'])
        self.assertEqual(result['BTC'].code,'SOURCE_CHANGED');self.assertEqual(self.successes(),0)

    def test_header_cache_uses_real_metadata_and_invalidates_changed_identity(self):
        from input_discovery import SnapshotCatalog
        config={'roots':{'snapshots':str(self.root)}}
        catalog=SnapshotCatalog(config,self.cache,self.log);catalog.discover()
        with patch.object(discovery,'rmp_header',side_effect=AssertionError('probe')):SnapshotCatalog(config,self.cache,self.log).discover()
        self.write(fixture(101,'2026-01-01T00:01:00Z'));new=SnapshotCatalog(config,self.cache,self.log);new.discover()
        self.assertEqual(new.refs[0].block_number,101)


class NativeComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.before=self.root/'before.rmp';self.after=self.root/'after.rmp.zip';self.diff=self.root/'diff.jsonl'
        self.before.write_bytes(msgpack.packb(fixture(),use_bin_type=True))
        target=fixture(102,'2026-01-01T01:00:00Z')
        book=target['exchange']['perp_dexs'][0]['books'][0]['book_orders'];book[91]['r']=100;del book[92]
        self.expected=target;self.write_after()
        from test_order_book_replay import update_diff,remove_diff
        self.diff.write_text(json.dumps({'block_number':101,'events':[update_diff(111,'2.5','1')]})+'\n'+json.dumps({'block_number':102,'events':[remove_diff(112)]})+'\n')

    def write_after(self):
        with zipfile.ZipFile(self.after,'w') as z:z.writestr('after.rmp',msgpack.packb(self.expected,use_bin_type=True))

    def compare(self):
        from test_order_book_replay import compare_real_snapshots
        return compare_real_snapshots(before_state=self.before,after_state=self.after,before_block=100,
            before_time='2026-01-01T00:00:00Z',target_block=102,coin='BTC',diffs=[self.diff])

    def test_native_before_diff_after_passes_exactly(self):self.assertEqual(self.compare()['status'],'PASS')

    def test_wrong_native_answer_fails_without_tolerance(self):
        self.expected['exchange']['perp_dexs'][0]['books'][0]['book_orders'][91]['r']+=1;self.write_after()
        self.assertEqual(self.compare()['status'],'FAIL')

    def test_expected_rmp_is_not_opened_until_replay_is_finished(self):
        original_stream=ext.source_stream;original_replay=app.replay_blocks;finished=[]
        @contextlib.contextmanager
        def guarded(path):
            if Path(path)==self.after:self.assertTrue(finished)
            with original_stream(path) as stream:yield stream
        def replay(*args,**kw):
            book=original_replay(*args,**kw);finished.append(1);return book
        with patch.object(ext,'source_stream',guarded),patch.object(discovery,'source_stream',guarded),patch.object(app,'replay_blocks',side_effect=replay):
            self.assertEqual(self.compare()['status'],'PASS')

    def test_missing_diff_fails_before_native_answer_is_read(self):
        self.diff.write_text('{"block_number":102,"events":[]}\n')
        original=ext.source_stream
        @contextlib.contextmanager
        def guarded(path):
            self.assertNotEqual(Path(path),self.after)
            with original(path) as stream:yield stream
        with patch.object(ext,'source_stream',guarded),patch.object(discovery,'source_stream',guarded):
            with self.assertRaises(app.BookError):self.compare()


class NativeBatchComparisonTests(NativeComparisonTests):
    def compare(self):
        from test_order_book_replay import compare_multiple_snapshots
        return compare_multiple_snapshots(before_state=self.before,after_state=self.after,
            before_block=100,before_time='2026-01-01T00:00:00Z',target_block=102,
            coins=['BTC','@TEST'],diffs=[self.diff])

    def test_both_native_sources_and_compressed_diff_are_scanned_once(self):
        result=self.compare()
        self.assertEqual(result['status'],'PASS');self.assertEqual(len(result['results']),2)
        self.assertEqual(result['cache_counts']['RMP_BODY_SCAN'],2)
        self.assertEqual(result['cache_counts']['DIFF_DECODE_START'],1)


    def test_all_coins_finish_replay_before_any_native_answer_open(self):
        original_stream=ext.source_stream;original_replay=app.replay_blocks;finished=[]
        @contextlib.contextmanager
        def guarded(path):
            if Path(path)==self.after:self.assertEqual(set(finished),{'BTC','@TEST'})
            with original_stream(path) as stream:yield stream
        def replay(book,*args,**kw):
            value=original_replay(book,*args,**kw);finished.append(book.coin);return value
        with patch.object(ext,'source_stream',guarded),patch.object(discovery,'source_stream',guarded),patch.object(app,'replay_blocks',side_effect=replay):
            self.assertEqual(self.compare()['status'],'PASS')

    def test_second_coin_failure_still_never_opens_expected(self):
        from test_order_book_replay import update_diff
        self.diff.write_text(json.dumps({'block_number':101,'events':[update_diff(99999,'1','2')]})+'\n'+json.dumps({'block_number':102,'events':[]})+'\n')
        original=ext.source_stream
        @contextlib.contextmanager
        def guarded(path):
            self.assertNotEqual(Path(path),self.after)
            with original(path) as stream:yield stream
        with patch.object(ext,'source_stream',guarded),patch.object(discovery,'source_stream',guarded):
            with self.assertRaises(app.BookError):self.compare()
