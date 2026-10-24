"""vNext acceptance: strict stages, single-side schemas and crash recovery."""
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch
from decimal import Decimal
import hlp_la_screen as app
import run_hlp
import rmp_l4
import input_discovery as discovery
import csv_pipeline as pipe
import state_store as store
from test_support import Fixture
from test_order_book_replay import update_diff,remove_diff,new_diff


class CSVFlowTests(Fixture):
    def test_csv1_does_not_probe_book_or_diff(self):
        with patch.object(discovery,'rmp_header',side_effect=AssertionError('raw RMP')), patch.object(pipe.DiffCache,'ensure',side_effect=AssertionError('diff')):
            self.run_stage('csv1')
        self.assertEqual(self.rows(1)[0]['status'],'grouped')
        self.assertEqual(self.rows(2),[])

    def test_csv2_reads_completed_csv1_after_fills_deleted(self):
        self.run_stage('csv1');shutil.rmtree(self.root/'fills')
        with patch.object(pipe,'ingest',side_effect=AssertionError('no fills')):
            self.run_stage('csv2')
        self.assertEqual(self.rows(2)[0]['status'],'book_ready')

    def test_csv3_uses_only_csvs_even_without_raw_roots_and_cache(self):
        self.run_stage('all')
        expected=self.rows(3)
        db=sqlite3.connect(self.directory/'progress.sqlite')
        db.execute("UPDATE outputs SET offset=(SELECT end FROM output_chunks WHERE name='csv3' AND position=0),rows=0,complete=0 WHERE name='csv3'")
        db.execute("DELETE FROM output_chunks WHERE name='csv3' AND position>0")
        db.execute("DELETE FROM meta WHERE key='csv3_done'");db.commit();db.close()
        for name in ['fills','diffs','snapshots']:shutil.rmtree(self.root/name)
        (self.directory/'cache.sqlite').unlink()
        with patch.object(store.Cache,'__init__',side_effect=AssertionError('cache opened')), patch.object(discovery,'hour_paths',side_effect=AssertionError('raw discovery')), patch.object(app,'load_snapshot',side_effect=AssertionError('snapshot')):
            self.run_stage('csv3')
        self.assertEqual(self.rows(3),expected)

    def test_completed_resume_has_no_raw_reads_or_duplicate_rows(self):
        self.run_stage();expected=[self.rows(n) for n in (1,2,3)]
        with patch.object(app,'open_json_text',side_effect=AssertionError('raw')), patch.object(pipe,'BookService',side_effect=AssertionError('raw service')):
            self.run_stage()
        self.assertEqual([self.rows(n) for n in (1,2,3)],expected)

    def test_csv1_staging_is_not_a_business_source_for_later_stages(self):
        self.run_stage('csv1')
        db=sqlite3.connect(self.directory/'progress.sqlite');db.execute('DELETE FROM ingest');db.execute('DELETE FROM assignment');db.commit();db.close()
        self.run_stage('csv2');self.run_stage('csv3')
        self.assertEqual(self.rows(3)[0]['status'],'calculated_full')

    def test_phase_prerequisites_do_not_fall_back_to_raw_readers(self):
        with patch.object(app,'open_json_text',side_effect=AssertionError('raw')):
            with self.assertRaisesRegex(store.CheckpointError,'CSV1'):self.run_stage('csv2')
            with self.assertRaisesRegex(store.CheckpointError,'CSV1'):self.run_stage('csv3')
        self.run_stage('csv1')
        with self.assertRaisesRegex(store.CheckpointError,'CSV2'):self.run_stage('csv3')

    def test_strict_phase_order_and_no_csv1_rewrite(self):
        calls=[]
        originals=[pipe.generate_csv1,pipe.generate_csv2,pipe.generate_csv3]
        def wrap(number):
            def run(*args,**kwargs):
                calls.append(number)
                if number>1:self.assertTrue(store.completed(args[2],number-1))
                return originals[number-1](*args,**kwargs)
            return run
        with patch.object(pipe,'generate_csv1',side_effect=wrap(1)),patch.object(pipe,'generate_csv2',side_effect=wrap(2)),patch.object(pipe,'generate_csv3',side_effect=wrap(3)):
            self.run_stage()
        self.assertEqual(calls,[1,2,3])
        before=(self.directory/'csv1.csv').read_bytes();self.run_stage('csv3')
        self.assertEqual((self.directory/'csv1.csv').read_bytes(),before)

    def test_csv2_contains_only_close_side_and_same_price_orders(self):
        self.run_stage();row=self.rows(2)[0]
        self.assertEqual({o['oid'] for o in json.loads(row['orders_json'])},{'111','112'})
        self.assertEqual(row['order_count'],'2');self.assertEqual(row['price_level_count'],'1')
        self.assertFalse(any('replay_state' in k for k in row))
        self.assertEqual(row['total_depth'],'5');self.assertEqual(row['book_state_point'],'block_end')

    def test_successful_empty_side_is_different_from_missing_data(self):
        self.fill(1,102,coin='EMPTY')
        self.run_stage();book=self.rows(2)[0];row=self.rows(3)[0]
        self.assertEqual((book['status'],book['orders_json'],book['total_depth']),('book_ready','[]','0'))
        self.assertEqual((row['status'],row['la'],row['filled_qty'],row['exit_vwap']),('empty_book','0','0',''))
        self.assertEqual(row['depth_insufficient'],'true')

    def test_missing_diff_keeps_unknown_metrics_blank(self):
        (self.root/'diffs'/'20260101'/'0.zip').unlink()
        self.run_stage();book=self.rows(2)[0];row=self.rows(3)[0]
        self.assertEqual((book['status'],book['orders_json'],book['total_depth']),('skipped','',''))
        self.assertEqual((row['status'],row['la'],row['filled_qty']),('skipped','',''))
        self.assertEqual(row['reason_code'],book['reason_code'])

    def test_six_files_after_normal_close_and_after_stage_switch(self):
        for stage in ['csv1','csv2','csv3']:
            self.run_stage(stage)
            self.assertEqual({p.name for p in self.directory.iterdir()}, {'csv1.csv','csv2.csv','csv3.csv','run.log','progress.sqlite','cache.sqlite'})

    def add_second(self):
        self.fill(3,104,side='A')
        self.hour('diffs',2,[self.block(103,'2026-01-01T02:00:00Z')])
        self.hour('diffs',3,[self.block(104,'2026-01-01T03:00:00Z')])

    def edit_csv2(self,change):
        path=self.directory/'csv2.csv'
        with path.open(encoding='utf-8',newline='') as f:rows=list(csv.DictReader(f))
        rows=change(rows)
        with path.open('w',encoding='utf-8',newline='') as f:
            w=csv.DictWriter(f,pipe.CSV2_FIELDS,lineterminator='\n');w.writeheader();w.writerows(rows)

    def test_bad_json_only_skips_one_episode(self):
        self.add_second();self.run_stage('csv1');self.run_stage('csv2')
        def change(rows):rows[0]['orders_json']='{broken';return rows
        self.edit_csv2(change);self.run_stage('csv3')
        rows=self.rows(3);self.assertEqual(rows[0]['status'],'skipped');self.assertNotEqual(rows[1]['status'],'skipped')

    def test_missing_csv2_row_only_skips_its_episode(self):
        self.add_second();self.run_stage('csv1');self.run_stage('csv2');self.edit_csv2(lambda rows:rows[1:]);self.run_stage('csv3')
        self.assertEqual(self.rows(3)[0]['status'],'skipped');self.assertNotEqual(self.rows(3)[1]['status'],'skipped')

    def test_duplicate_csv2_row_only_skips_its_episode(self):
        self.add_second();self.run_stage('csv1');self.run_stage('csv2');self.edit_csv2(lambda rows:[rows[0],*rows]);self.run_stage('csv3')
        self.assertEqual(self.rows(3)[0]['status'],'skipped');self.assertNotEqual(self.rows(3)[1]['status'],'skipped')

    def test_wrong_csv2_association_and_depth_are_not_trusted(self):
        for field,value in [('episode_input_hash','wrong'),('total_depth','999')]:
            with self.subTest(field=field):
                self.run_stage('csv1',RESUME=False);self.settings['RESUME']=True;self.run_stage('csv2')
                def change(rows):rows[0][field]=value;return rows
                self.edit_csv2(change);self.run_stage('csv3');self.assertEqual(self.rows(3)[0]['status'],'skipped')

    def test_live_snapshots_changing_during_replay_are_not_successful_cache(self):
        original=app.replay_blocks
        def mutate(*args,**kwargs):
            result=original(*args,**kwargs)
            with (self.root/'snapshots'/'100.rmp').open('ab') as f:f.write(b'x')
            return result
        with patch.object(app,'replay_blocks',side_effect=mutate):self.run_stage()
        self.assertEqual(self.rows(2)[0]['reason_code'],'SOURCE_CHANGED')
        db=sqlite3.connect(self.directory/'cache.sqlite')
        self.assertEqual(db.execute("SELECT count(*) FROM entries WHERE kind='replay'").fetchone()[0],0);db.close()

    def test_simulation_does_not_mutate_csv2_or_other_episodes(self):
        self.add_second();self.run_stage('csv1');self.run_stage('csv2');before=(self.directory/'csv2.csv').read_bytes()
        self.run_stage('csv3');self.assertEqual((self.directory/'csv2.csv').read_bytes(),before)
        self.assertEqual(self.rows(3)[0]['filled_qty'],'1')


class RecoveryTests(Fixture):
    def crash_once(self,point,stage='all',csv_name=None):
        original=store.fault;fired=[]
        def fail(actual,**context):
            if actual==point and (csv_name is None or context.get('csv')==csv_name) and not fired:
                fired.append(context);raise KeyboardInterrupt(point)
        with patch.object(store,'fault',side_effect=fail):
            with self.assertRaises(KeyboardInterrupt):self.run_stage(stage)
        self.assertTrue(fired)
        self.run_stage(stage)

    def test_csv1_ingest_group_emit_each_resume_without_rereading_committed_input(self):
        for point in ['ingest_committed','group_committed','emit_committed']:
            with self.subTest(point=point):
                self.settings['RESUME']=False
                original=store.fault;fired=[]
                def fail(actual,**kw):
                    if actual==point and not fired:fired.append(1);raise KeyboardInterrupt(point)
                with patch.object(store,'fault',side_effect=fail):
                    with self.assertRaises(KeyboardInterrupt):self.run_stage('csv1')
                self.settings['RESUME']=True
                if point!='ingest_committed':
                    with patch.object(app,'open_json_text',side_effect=AssertionError('fills reread')):self.run_stage('csv1')
                else:self.run_stage('csv1')
                self.assertEqual(len(self.rows(1)),1)

    def test_csv1_partial_record_and_uncommitted_tail_are_truncated(self):
        self.crash_once('csv_half_written','csv1','csv1');self.assertEqual(len(self.rows(1)),1)

    def test_csv2_after_csv_fsync_before_progress_has_no_duplicate(self):
        self.crash_once('csv_fsynced','all','csv2');self.assertEqual(len(self.rows(2)),1)

    def test_csv3_partial_long_json_is_truncated_and_csv_only_resumes(self):
        self.run_stage('csv1');self.run_stage('csv2')
        original=store.fault;fired=[]
        def fail(point,**kw):
            if point=='csv_half_written' and kw.get('csv')=='csv3' and not fired:fired.append(1);raise KeyboardInterrupt()
        with patch.object(store,'fault',side_effect=fail):
            with self.assertRaises(KeyboardInterrupt):self.run_stage('csv3')
        with patch.object(store.Cache,'__init__',side_effect=AssertionError('raw cache')):self.run_stage('csv3')
        self.assertEqual(len(self.rows(3)),1);self.assertEqual(self.rows(3)[0]['status'],'calculated_full')

    def test_committed_csv2_result_is_not_redone_after_interruption(self):
        self.crash_once('progress_committed','all','csv2')
        self.assertEqual(len(self.rows(2)),1)

    def test_cache_commit_before_csv_is_safe_orphan_and_does_not_rescan_rmp(self):
        fired=[]
        def fail(point,**kw):
            if point=='replay_cache_committed' and not fired:fired.append(1);raise KeyboardInterrupt()
        with patch.object(store,'fault',side_effect=fail):
            with self.assertRaises(KeyboardInterrupt):self.run_stage()
        with patch.object(rmp_l4,'scan_rmp',side_effect=AssertionError('unnecessary rescan')):self.run_stage()
        self.assertEqual(self.rows(3)[0]['status'],'calculated_full')

    def test_forward_replay_restores_zero_pending_remove_and_both_sides(self):
        self.fill(3,104,side='A')
        self.hour('diffs',1,[self.block(102,'2026-01-01T01:00:00Z',[update_diff(111,'2.5','0')])])
        self.hour('diffs',2,[self.block(103,'2026-01-01T02:00:00Z')])
        self.hour('diffs',3,[self.block(104,'2026-01-01T03:00:00Z',[remove_diff(111)])])
        fired=[]
        def fail(point,**kw):
            if point=='progress_committed' and kw.get('csv')=='csv2' and not fired:fired.append(1);raise KeyboardInterrupt()
        with patch.object(store,'fault',side_effect=fail):
            with self.assertRaises(KeyboardInterrupt):self.run_stage()
        self.run_stage()
        self.assertEqual([r['status'] for r in self.rows(2)],['book_ready','book_ready'])
        self.assertEqual(self.rows(3)[1]['status'],'calculated_partial')
        self.assertIn('REPLAY_CACHE_HIT',(self.directory/'run.log').read_text())

    def test_committed_corruption_is_rejected_when_its_stage_resumes(self):
        self.run_stage('csv1');path=self.directory/'csv1.csv';data=path.read_bytes();path.write_bytes(data.replace(b'grouped',b'broken!',1))
        with self.assertRaisesRegex(store.CheckpointError,'corrupt'):self.run_stage('csv1')

    def test_truncated_committed_csv_and_corrupt_progress_are_system_failures(self):
        self.run_stage('csv1');path=self.directory/'csv1.csv';path.write_bytes(path.read_bytes()[:-3])
        with self.assertRaises(store.CheckpointError):self.run_stage('csv1')

    def test_code_version_changes_refuse_mixed_resume(self):
        self.run_stage('csv1')
        with patch.object(run_hlp,'engine_hashes',return_value={'changed':'yes'}):
            with self.assertRaisesRegex(store.CheckpointError,'incompatible'):self.run_stage('csv2')

    def test_stage_and_logging_options_do_not_change_run_identity(self):
        self.run_stage('csv1');before=self.directory
        self.run_stage('csv2',DETAILED_LOG=False,LOG_BLOCK_INTERVAL=19)
        self.assertEqual(before,self.directory)

    def test_resume_true_does_not_retry_skipped_false_recomputes_new_run(self):
        source=self.root/'diffs'/'20260101'/'0.zip';source.unlink();self.run_stage();old=self.directory
        self.hour('diffs',0,[self.block(101,'2026-01-01T00:30:00Z')]);self.run_stage()
        self.assertEqual(self.directory,old);self.assertEqual(self.rows(3)[0]['status'],'skipped')
        self.run_stage(RESUME=False);self.assertNotEqual(self.directory,old)
        self.assertEqual(self.rows(3)[0]['status'],'calculated_full');self.assertTrue(old.exists())

    def test_single_writer_lock_rejects_without_changing_outputs(self):
        self.run_stage('csv1');before=(self.directory/'csv1.csv').read_bytes()
        with store.run_lock(self.directory/'progress.sqlite'):
            with self.assertRaises(store.CheckpointError):self.run_stage('csv2')
        self.assertEqual((self.directory/'csv1.csv').read_bytes(),before)
        self.assertFalse(list(self.directory.glob('*.lock')))


class DiagnosticRecoveryTests(Fixture):
    def test_unowned_snapshot_issue_survives_last_episode_commit_interruption(self):
        (self.root/'snapshots'/'backup.rmp').write_bytes(b'broken')
        fired=[]
        def stop(point,**kw):
            if point=='progress_committed' and kw.get('csv')=='csv2' and not fired:fired.append(1);raise KeyboardInterrupt()
        with patch.object(store,'fault',side_effect=stop):
            with self.assertRaises(KeyboardInterrupt):self.run_stage()
        self.run_stage()
        self.assertTrue(any(r['record_type']=='issue' and r['reason_code']=='SNAPSHOT_METADATA_UNRESOLVED' for r in self.rows(2,False)))

    def test_multicoin_decoder_cache_keys_are_stable_after_one_episode_commits(self):
        self.fill(2,103,coin='xyz:ALPHA',price='5')
        self.hour('diffs',2,[self.block(103,'2026-01-01T02:00:00Z')])
        fired=[]
        def stop(point,**kw):
            if point=='progress_committed' and kw.get('csv')=='csv2' and not fired:fired.append(1);raise KeyboardInterrupt()
        with patch.object(store,'fault',side_effect=stop):
            with self.assertRaises(KeyboardInterrupt):self.run_stage()
        self.run_stage()
        db=sqlite3.connect(self.directory/'cache.sqlite')
        decodes=json.loads(db.execute("SELECT value FROM meta WHERE key='count:DIFF_DECODE_START'").fetchone()[0]);db.close()
        self.assertEqual(decodes,3)

    def test_locatable_bad_fill_only_splits_its_coin_direction(self):
        events=[]
        for height,minute,coin,qty in [(102,0,'BTC','1'),(103,1,'xyz:ALPHA','1'),(104,2,'BTC','bad'),(105,3,'BTC','1'),(106,4,'xyz:ALPHA','1')]:
            tm=f'2026-01-01T01:{minute:02d}:00Z'
            e=[self.owner,dict(coin=coin,side='B',px='12',sz=qty,time=app.parse_time(tm),tid=height,liquidation=dict(method='backstop',liquidatedUser='other'))]
            events.append(self.block(height,tm,[e]))
        self.hour('fills',1,events);self.run_stage('csv1')
        rows=self.rows(1);btc={r['episode_id'] for r in rows if r['coin']=='BTC'};hip={r['episode_id'] for r in rows if r['coin']=='xyz:ALPHA'}
        self.assertEqual(len(btc),2);self.assertEqual(len(hip),1)


class SystemFailureTests(Fixture):
    def test_corrupt_progress_is_rejected_without_recreating_business_results(self):
        self.run_stage('csv1');expected=(self.directory/'csv1.csv').read_bytes()
        (self.directory/'progress.sqlite').write_bytes(b'corrupt progress')
        with self.assertRaises(sqlite3.DatabaseError):self.run_stage('csv2')
        self.assertEqual((self.directory/'csv1.csv').read_bytes(),expected)

    def test_unlocatable_csv2_header_damage_is_not_a_batch_of_fake_skips(self):
        self.run_stage('csv1');self.run_stage('csv2')
        path=self.directory/'csv2.csv';path.write_bytes(path.read_bytes().replace(b'schema_version',b'broken_header!',1))
        with self.assertRaises(store.CheckpointError):self.run_stage('csv3')

    def test_unknown_programming_failure_is_not_swallowed_as_input_issue(self):
        self.run_stage('csv1');self.run_stage('csv2')
        with patch.object(app,'simulate_close',side_effect=RuntimeError('programming defect')):
            with self.assertRaisesRegex(RuntimeError,'programming defect'):self.run_stage('csv3')
        self.run_stage('csv3');self.assertEqual(len(self.rows(3)),1)


class MemoryAndPositionTests(Fixture):
    def test_diff_failure_reports_original_event_position_not_filtered_index(self):
        self.hour('diffs',0,[self.block(101,'2026-01-01T00:30:00Z',[
            dict(coin='ETH',oid=42,raw_book_diff='remove'),update_diff(99999,'1','2')])])
        self.run_stage()
        row=self.rows(2)[0];problem=json.loads(row['issues_json'])[0]
        self.assertEqual(row['status'],'skipped');self.assertEqual(problem['event_index'],1)
        self.assertEqual(problem['source_line'],1);self.assertEqual(problem['block_number'],101)
        self.assertTrue(problem['source_file'].endswith('/0.zip'));self.assertEqual(problem['source_member'],'hourly/20260101/0')

    def test_csv2_index_memory_is_bounded_and_stores_only_offsets(self):
        import tracemalloc
        self.run_stage('csv1');self.run_stage('csv2')
        row=self.rows(2)[0]
        row['orders_json']=json.dumps([dict(oid=str(i),price='12',qty='1') for i in range(6000)],separators=(',',':'))
        path=self.directory/'csv2.csv';db=store.connect(self.directory/'progress.sqlite')
        try:
            with path.open('w',encoding='utf-8',newline='') as stream,db:
                writer=csv.DictWriter(stream,pipe.CSV2_FIELDS,lineterminator='\n');writer.writeheader()
                for i in range(80):
                    row['episode_id']=f'EP{i:06d}';writer.writerow(row)
                    db.execute("INSERT OR REPLACE INTO row_hashes VALUES ('csv2',?,?)",(row['episode_id'],store.digest(row)))
            tracemalloc.start()
            try:
                from test_support import Log
                pipe.index_csv2(self.directory,db,Log());_,peak=tracemalloc.get_traced_memory()
            finally:tracemalloc.stop()
            self.assertLess(peak,8*1024*1024)
            self.assertGreater(path.stat().st_size,20*1024*1024)
            columns={r[1] for r in db.execute('PRAGMA table_info(csv2_index)')}
            self.assertNotIn('orders_json',columns);self.assertEqual(db.execute('SELECT count(*) FROM csv2_index').fetchone()[0],80)
            print('CSV2 index memory acceptance: source_bytes=',path.stat().st_size,'peak_python_bytes=',peak)
        finally:db.close()
