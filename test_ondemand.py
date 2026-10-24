"""Regression tests for the new extraction/transaction boundaries, not a replay clone."""
import contextlib
import copy
import csv
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import msgpack
from decimal import Decimal
import hlp_la_screen as app
import rmp_l4 as ext
import test_order_book_replay as old


class Log:
    def __init__(self):self.events=[];self.lines=0
    def info(self,event,**kw):self.events.append((event,kw))
    warning=info
    def heartbeat(self,*args):pass


def raw_order(oid,side='B',price=123450,qty=250):
    return {'o':oid,'c':{'s':side,'l':price},'r':qty,'O':999999,'u':'0xowner','t':0}


def fixture(height=100,time='2026-01-01T00:00:00Z',reversed_meta=False):
    main={'clearinghouse':{'meta':{'pdi':0,'universe':[{'name':'BTC','szDecimals':2},{'name':'EMPTY','szDecimals':0}]}},
          'books':[{'book_orders':{91:raw_order(111),92:raw_order(112),93:raw_order(113,'A',124000,50)},'asset':0},
                   {'asset':1,'book_orders':{}}]}
    hip={'clearinghouse':{'meta':{'pdi':1,'universe':[{'name':'xyz:ALPHA','szDecimals':3}]}},
         'books':[{'asset':10000,'book_orders':{1:raw_order(211,price=5000,qty=10)}}]}
    if reversed_meta:
        main={'books':main['books'],'clearinghouse':main['clearinghouse']}
        hip={'books':hip['books'],'clearinghouse':hip['clearinghouse']}
    return {'exchange':{'context':{'height':height,'time':time},'perp_dexs':[main,hip],
        'spot_books':[{'book_orders':{1:raw_order(311,price=123456,qty=125)},'asset':0}],
        'spot_clearinghouse':{'meta':{'spot_infos':[{'name':'@TEST','tokens':[0,1]}],
            'token_infos':[{'spec':{'szDecimals':1}},{'spec':{'szDecimals':6}}]}},
        'user_states':{'must_not_be_read_as_book':{'untriggered_orders':[raw_order(999)]}}}}


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=self.root/'state.rmp'
        self.write(fixture());self.ref=app.SnapshotRef(str(self.source),100,app.parse_time('2026-01-01T00:00:00Z'))
        self.log=Log();self.cache=ext.ExtractionCache(self.root/'cache',self.log)
    def write(self,value):self.source.write_bytes(msgpack.packb(value,use_bin_type=True))
    def ensure(self,coins,cache=None,ref=None):
        ref=ref or self.ref
        return (cache or self.cache).ensure(ref,set(coins),lambda p,c:app.load_snapshot(app.SnapshotRef(str(p),ref.block_number,ref.time_ms),c))
    def receipts(self):return list((self.root/'cache').rglob('*.success.json'))

    def test_all_market_families_metadata_units_oid_remaining_and_same_price(self):
        result=self.ensure(['BTC','xyz:ALPHA','@TEST','EMPTY'])
        self.assertEqual(self.cache.stats['rmp_scans'],1)
        btc=app.load_snapshot(app.SnapshotRef(str(result['BTC']),100,0),'BTC')
        self.assertEqual(set(btc.orders),{'111','112','113'})
        self.assertEqual(btc.orders['111'].price,Decimal('12.345'))
        self.assertEqual(btc.orders['111'].qty,Decimal('2.5'))
        self.assertEqual(btc.orders['113'].side,'ask')
        self.assertEqual(len(btc.side_orders('bid')),2)
        for coin,price,qty in [('xyz:ALPHA','5','0.01'),('@TEST','0.0123456','12.5')]:
            value=json.loads(result[coin].read_text());order=value['orders'][0]
            self.assertEqual((order['limitPx'],order['sz']),(price,qty))
        self.assertEqual(json.loads(result['EMPTY'].read_text())['orders'],[])

    def test_only_demanded_coin_is_exported_and_no_spool_survives(self):
        result=self.ensure(['@TEST'])
        self.assertEqual(set(result),{'@TEST'})
        self.assertEqual(len(self.receipts()),1)
        self.assertEqual(len(list((self.root/'cache').rglob('*.json'))),2)
        self.assertFalse(list((self.root/'cache').rglob('*.msgpack')))
        self.assertFalse(list((self.root/'cache').glob('.staging_*')))

    def test_metadata_after_perp_books_works_in_one_scan(self):
        self.write(fixture(reversed_meta=True));result=self.ensure(['BTC','xyz:ALPHA','@TEST'])
        self.assertTrue(all(isinstance(v,Path) for v in result.values()))
        self.assertEqual(self.cache.stats['rmp_scans'],1)

    def test_persistent_hits_do_not_scan_source(self):
        first=self.ensure(['BTC','@TEST'])
        warm=ext.ExtractionCache(self.root/'cache',self.log)
        with patch.object(ext,'scan_rmp',side_effect=AssertionError('must not rescan')):
            second=self.ensure(['BTC','@TEST'],cache=warm)
        self.assertEqual(first,second)
        self.assertEqual(warm.stats,{'rmp_scans':0,'extract_hits':2,'extract_misses':0,'extract_written':0})

    def test_missing_coin_is_not_empty_book_or_success_cache(self):
        result=self.ensure(['BTC','ABSENT'])
        self.assertEqual(result['ABSENT'].code,'SNAPSHOT_COIN_MISSING')
        self.assertIsInstance(result['BTC'],Path);self.assertEqual(len(self.receipts()),1)

    def test_late_unplanned_coin_does_not_trigger_second_scan(self):
        self.ensure(['BTC']);result=self.ensure(['@TEST'])
        self.assertEqual(result['@TEST'].code,'RMP_ALREADY_SCANNED')
        self.assertEqual(self.cache.stats['rmp_scans'],1)

    def test_truncated_input_never_commits_partial_success(self):
        self.source.write_bytes(self.source.read_bytes()[:-15])
        with self.assertRaises(ext.ExtractionError):self.ensure(['BTC','@TEST'])
        self.assertEqual(self.receipts(),[])
        self.assertFalse(list((self.root/'cache').rglob('*.json')))
        result=self.ensure(['BTC']);self.assertIsInstance(result['BTC'],ext.ExtractionError)
        self.assertEqual(self.cache.stats['rmp_scans'],1)

    def test_trailing_incomplete_bytes_are_rejected(self):
        self.source.write_bytes(self.source.read_bytes()+b'\x81')
        with self.assertRaises(ext.ExtractionError):self.ensure(['BTC'])
        self.assertEqual(self.receipts(),[])

    def test_wrong_declared_height_or_time_never_commits(self):
        for ref in [app.SnapshotRef(str(self.source),99,self.ref.time_ms),app.SnapshotRef(str(self.source),100,self.ref.time_ms+1)]:
            with self.assertRaises(ext.ExtractionError):self.ensure(['BTC'],ref=ref)
        self.assertEqual(self.receipts(),[])

    def test_corrupt_cached_json_is_not_a_hit(self):
        result=self.ensure(['BTC']);result['BTC'].write_text('{broken')
        cache=ext.ExtractionCache(self.root/'cache',self.log)
        self.ensure(['BTC'],cache=cache)
        self.assertEqual(cache.stats['rmp_scans'],1);self.assertEqual(cache.stats['extract_hits'],0)

    def test_orphan_json_without_receipt_is_not_a_hit(self):
        self.ensure(['BTC']);self.receipts()[0].unlink()
        cache=ext.ExtractionCache(self.root/'cache',self.log);self.ensure(['BTC'],cache=cache)
        self.assertEqual(cache.stats['rmp_scans'],1)

    def test_source_change_and_conversion_version_invalidate_cache(self):
        first=self.ensure(['BTC'])['BTC']
        value=fixture();value['exchange']['perp_dexs'][0]['books'][0]['book_orders'][91]['r']=301
        self.write(value)
        second=self.ensure(['BTC'],cache=ext.ExtractionCache(self.root/'cache',self.log))['BTC']
        self.assertNotEqual(first,second)
        versioned=ext.ExtractionCache(self.root/'cache',self.log,version='test-new-version')
        third=self.ensure(['BTC'],cache=versioned)['BTC'];self.assertNotEqual(second,third)
        self.assertEqual(versioned.stats['rmp_scans'],1)

    def test_bad_selected_coin_does_not_poison_another_coin(self):
        value=fixture();value['exchange']['perp_dexs'][0]['books'][0]['book_orders'][91]['r']=-1
        self.write(value);result=self.ensure(['BTC','@TEST'])
        self.assertIsInstance(result['BTC'],ext.ExtractionError)
        self.assertIsInstance(result['@TEST'],Path);self.assertEqual(len(self.receipts()),1)

    def test_zip_member_stream_uses_same_exact_mapping(self):
        z=self.root/'state.rmp.zip'
        with zipfile.ZipFile(z,'w',compression=zipfile.ZIP_DEFLATED) as f:f.write(self.source,'nested/state.rmp')
        ref=app.SnapshotRef(str(z),100,self.ref.time_ms)
        result=self.ensure(['BTC','@TEST'],ref=ref)
        self.assertEqual(len(result),2);self.assertEqual(self.cache.stats['rmp_scans'],1)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.log=Log()
    def config(self):
        return old.make_pipeline_config(self.root,[old.fill('a','2026-01-01T01:00:00Z',101),
            old.fill('b','2026-01-01T02:00:00Z',103)],diff_events={102:[old.update_diff(1,'2','1')]})
    def episodes(self,config):
        return app.build_episodes(app.iter_fills(config,self.log),app.parse_time(config['data_start']),app.parse_time(config['data_end']))

    def test_forward_replay_cache_and_simulation_do_not_mutate_history(self):
        config=self.config();eps=self.episodes(config);manager=app.ReplayManager(config,self.log);manager.prepare(eps)
        first=manager.get(eps[0]);key=(eps[0].coin,eps[0].snapshot)
        before=copy.deepcopy(manager.cache[key][3].normalized())
        app.simulate_close(first,'long',Decimal(10),Decimal(100))
        self.assertEqual(manager.cache[key][3].normalized(),before)
        second=manager.get(eps[1])
        self.assertEqual(second.orders['1'].qty,Decimal(1))
        self.assertEqual(manager.stats['replay_hits'],1)
        self.assertEqual(manager.cache[key][1],103)
        self.assertEqual(first.orders['1'].qty,Decimal(2))

    def test_failed_replay_does_not_advance_or_mutate_success_cache(self):
        config=self.config();eps=self.episodes(config);manager=app.ReplayManager(config,self.log);manager.prepare(eps)
        manager.get(eps[0]);key=(eps[0].coin,eps[0].snapshot)
        before=copy.deepcopy(manager.cache[key][3].normalized())
        path=Path(config['diffs']['chunks'][0]['path'])
        old.write_jsonl(path,[old.block(101),old.block(102,old.update_diff(1,'2','1'))])
        with self.assertRaises(app.BookError):manager.get(eps[1])
        self.assertEqual(manager.cache[key][1],101)
        self.assertEqual(manager.cache[key][3].normalized(),before)

    def test_result_release_waits_for_successful_flush(self):
        ep=self.episodes(self.config())[0];ep.book_result={'orders':[]};ep.la_result={'large':'trace'}
        class Handle:
            def __init__(self,fail=False):self.fail=fail
            def flush(self):
                self_outer.assertIsNotNone(ep.book_result);self_outer.assertIsNotNone(ep.la_result)
                if self.fail:raise OSError('flush failed')
        self_outer=self
        with patch.object(app,'write_episode'):
            with self.assertRaises(OSError):app.commit_episode_outputs(ep,[],[Handle(),Handle(True)])
            self.assertIsNotNone(ep.book_result);self.assertIsNotNone(ep.la_result)
            app.commit_episode_outputs(ep,[],[Handle(),Handle()])
        self.assertIsNone(ep.book_result);self.assertIsNone(ep.la_result)

    def test_csv1_draft_precedes_rmp_scan_and_final_rows_have_no_duplicates(self):
        config=self.config();source=self.root/'real.rmp';source.write_bytes(msgpack.packb(fixture(),use_bin_type=True))
        config['snapshots'][0]['path']=str(source);config['rmp_cache_dir']=str(self.root/'cache');config['console_log']=False
        config['diffs']['chunks'][0]['path']=str(old.write_jsonl(self.root/'emptydiff.jsonl',[old.block(i) for i in range(101,104)]))
        out=self.root/'out';captured=[];scan=ext.scan_rmp;build=app.build_episodes
        def checked_scan(*args,**kwargs):
            rows=old.read_csv(out/'csv1.csv');self.assertEqual(len(rows),2)
            self.assertEqual({r['status'] for r in rows},{'pending'})
            return scan(*args,**kwargs)
        def checked_build(*args,**kwargs):
            eps=build(*args,**kwargs);captured.extend(eps);return eps
        with patch.object(ext,'scan_rmp',side_effect=checked_scan),patch.object(app,'build_episodes',side_effect=checked_build):
            summary=app.run_screen(config,out)
        self.assertEqual(summary['calculated'],2)
        rows=old.read_csv(out/'csv1.csv');self.assertEqual(len(rows),2)
        self.assertEqual({r['status'] for r in rows},{'ok'})
        self.assertTrue(all(e.book_result is None and e.la_result is None for e in captured))
        self.assertEqual({p.name for p in out.iterdir()},{'csv1.csv','csv2.csv','csv3.csv','run.log'})
        log=(out/'run.log').read_text();self.assertEqual(log.count('RMP_SCAN_START'),1)
        self.assertIn('BOOK_CACHE',log);self.assertIn('CSV1_FINALIZED',log)
        self.assertLess(log.index('CSV1_DRAFT_WRITTEN'),log.index('RMP_SCAN_START'))

    def test_replay_cache_is_kept_until_last_planned_consumer(self):
        config=self.config();eps=self.episodes(config);manager=app.ReplayManager(config,self.log);manager.prepare(eps)
        manager.get(eps[0]);key=(eps[0].coin,eps[0].snapshot)
        manager.finish_episode(eps[0]);self.assertIn(key,manager.cache)
        self.assertEqual(manager.remaining[key],1)
        manager.get(eps[1]);self.assertEqual(manager.stats['replay_hits'],1)
        manager.finish_episode(eps[1]);self.assertNotIn(key,manager.cache)

    def test_same_coin_different_source_books_have_separate_cache_keys(self):
        config=self.config();books=json.loads(Path(config['snapshots'][0]['path']).read_text())
        books[0]=102;books[1][0][1][0][0]['sz']='7'
        newer=old.write_json(self.root/'newer.json',books)
        config['snapshots'].append({'path':str(newer),'block_number':102,'time':'2026-01-01T01:30:00Z'})
        eps=self.episodes(config);manager=app.ReplayManager(config,self.log);manager.prepare(eps)
        first=manager.get(eps[0]);second=manager.get(eps[1])
        self.assertEqual(first.orders['1'].qty,Decimal(2));self.assertEqual(second.orders['1'].qty,Decimal(7))
        self.assertEqual(len(manager.cache),2)
        self.assertEqual({key[1].block_number for key in manager.cache},{100,102})

    def test_single_coin_cache_files_are_not_snapshot_candidates(self):
        config=self.config();manager=app.ReplayManager(config,self.log)
        original=list(manager.refs);manager.prepare(self.episodes(config));self.assertEqual(manager.refs,original)
        self.assertTrue(all(not p.path.endswith('.success.json') for p in manager.refs))

    def test_same_source_aliases_union_demand_before_scanning(self):
        config=self.config();source=self.root/'raw.rmp';source.write_bytes(msgpack.packb(fixture(),use_bin_type=True))
        ref={'path':str(source),'block_number':100,'time':'2026-01-01T00:00:00Z'}
        config['snapshots']=[ref,{**ref,'path':str(source.parent/'.'/source.name)}]
        config['rmp_cache_dir']=str(self.root/'cache')
        eps=self.episodes(config);eps[1].coin='@TEST'
        manager=app.ReplayManager(config,self.log);manager.prepare(eps)
        self.assertEqual(len(manager.refs),1);self.assertEqual(manager.extraction.stats['rmp_scans'],1)
        self.assertEqual(len(manager.l4_paths),2)


if __name__=='__main__':unittest.main(verbosity=2)


