"""Acceptance: true CSV dependencies, crash recovery and prepared-date boundaries."""
import contextlib
import copy
import csv
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import hlp_la_screen as app
import input_discovery as discovery
import rmp_l4 as rmp
import run_hlp
import csv_pipeline as pipe
import test_universal as helpers
import test_order_book_replay as old

class CSVStagesTests(unittest.TestCase):
    hour = helpers.UniversalTests.hour
    block = helpers.UniversalTests.block
    snapshot = helpers.UniversalTests.snapshot

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name in ('fills','diffs','snapshots'): (self.root/name).mkdir()
        self.settings=run_hlp.settings_from_top()
        self.settings.update(START_DATE='2026-01-01',END_DATE='2026-01-01',
            FILLS_ROOT=str(self.root/'fills'),DIFFS_ROOT=str(self.root/'diffs'),
            SNAPSHOTS_ROOT=str(self.root/'snapshots'),OUTPUT_ROOT=str(self.root/'output'),
            CACHE_ROOT=str(self.root/'cache'),CONSOLE_LOG=False,RESUME=True)
        for h in range(24): self.hour('fills',h,[])
        self.owner=self.settings['HLP_ACCOUNTS'][0]
        for h,b,side in [(1,101,'B'),(2,103,'B'),(3,104,'A')]:
            self.fill(h,b,side=side)
        self.snapshot()
        self.hour('diffs',0,[self.block(101,'2026-01-01T00:30:00Z')])
        self.hour('diffs',1,[self.block(102,'2026-01-01T01:30:00Z',[old.update_diff(111,'2.5','0')])])
        self.hour('diffs',2,[self.block(103,'2026-01-01T02:00:00Z')])
        self.hour('diffs',3,[self.block(104,'2026-01-01T03:00:00Z',[old.remove_diff(111)])])

    def fill(self,h,b,side='B',qty='1',price='12',time=None):
        time=time or f'2026-01-01T{h:02d}:00:00Z'
        event=[self.owner,dict(coin='BTC',side=side,px=price,sz=qty,time=app.parse_time(time),tid=b,
                            liquidation=dict(method='backstop',liquidatedUser='0xother'))]
        self.hour('fills',h,[self.block(b,time,[event])])

    def invoke(self,stage='all',**changes):
        with contextlib.redirect_stdout(io.StringIO()):
            return run_hlp.run({**self.settings,'RUN_STAGE':stage,**changes})

    def directory(self):
        return next((self.root/'output').glob('run_*'))

    def rows(self,number,directory=None):
        directory=directory or self.directory()
        with (directory/'result'/f'csv{number}.csv').open(encoding='utf-8',newline='') as f:
            return list(csv.DictReader(f))

    def state(self,number):
        return pipe.read_json(self.directory()/'result/.state'/f'csv{number}.json')

    def interrupt_after(self,stage,units):
        original=pipe.AppendCSV.commit
        def commit(table,**extra):
            original(table,**extra)
            value=extra.get('sources_done') if stage==1 else table.state['rows']
            if table.path.name==f'csv{stage}.csv' and value==units:
                raise KeyboardInterrupt('simulated power interruption after durable commit')
        with patch.object(pipe.AppendCSV,'commit',commit):
            with self.assertRaises(KeyboardInterrupt): self.invoke(f'csv{stage}')

    @contextlib.contextmanager
    def forbid_raw(self):
        with contextlib.ExitStack() as stack:
            for target,name in [(app,'iter_fills'),(app,'load_snapshot'),(app,'iter_diff_blocks'),
                (app,'replay_blocks'),(rmp,'scan_rmp'),(rmp,'source_stream'),
                (discovery,'discover_fills'),(discovery,'discover_book_inputs'),
                (app.ReplayManager,'prepare'),(app.ReplayManager,'get')]:
                stack.enter_context(patch.object(target,name,side_effect=AssertionError('raw source API must not run')))
            yield

    def test_csv1_stage_does_not_read_rmp_or_diff(self):
        with patch.object(rmp,'scan_rmp',side_effect=AssertionError('RMP')),patch.object(app,'iter_diff_blocks',side_effect=AssertionError('diff')):
            directory,summary=self.invoke('csv1')
        self.assertEqual(summary,{'stage':'csv1','episodes':3,'fills':3})
        self.assertFalse((directory/'result/csv2.csv').exists())
        self.assertEqual({r['status'] for r in self.rows(1)},{'grouped'})
        self.assertEqual(self.rows(1)[0]['formal_start'],'2026-01-01T01:00:00.000Z')
        self.assertEqual(self.rows(1)[0]['formal_end_exclusive'],'2026-01-01T23:00:00.000Z')

    def test_csv2_uses_persisted_csv1_after_fills_removed(self):
        self.invoke('csv1'); shutil.rmtree(self.root/'fills')
        with patch.object(app,'iter_fills',side_effect=AssertionError('fills reread')):
            self.invoke('csv2')
        self.assertEqual([r['order_count'] for r in self.rows(2)],['2','1','1'])
        self.assertEqual([r['status'] for r in self.rows(2)],['ok']*3)
        self.assertFalse((self.directory()/'result/csv3.csv').exists())

    def test_csv3_works_without_any_raw_data_or_raw_reader(self):
        self.invoke('csv1'); self.invoke('csv2')
        before=[(self.directory()/'result'/f'csv{n}.csv').read_bytes() for n in (1,2)]
        for name in ('fills','diffs','snapshots'): shutil.rmtree(self.root/name)
        with self.forbid_raw(): _,summary=self.invoke('csv3')
        self.assertEqual(summary,dict(episodes=3,fills=3,calculated=3,skipped=0,depth_insufficient=1,partial_episodes=0))
        self.assertEqual([r['la'] for r in self.rows(3)],['0.345','0.345','-0.2'])
        self.assertEqual([r['fill_trace_json'] for r in self.rows(3)],
            ['[{"oid":"111","price":"12.345","available_qty":"2.5","filled_qty":"1"}]',
             '[{"oid":"112","price":"12.345","available_qty":"2.5","filled_qty":"1"}]',
             '[{"oid":"113","price":"12.4","available_qty":"0.5","filled_qty":"0.5"}]'])
        self.assertEqual(before,[(self.directory()/'result'/f'csv{n}.csv').read_bytes() for n in (1,2)])

    def test_completed_all_resume_has_zero_raw_reads_and_no_duplicate_rows(self):
        directory,first=self.invoke(); contents={n:(directory/'result'/f'csv{n}.csv').read_bytes() for n in (1,2,3)}
        with self.forbid_raw(): again,second=self.invoke()
        self.assertEqual(directory,again); self.assertEqual(first,second)
        self.assertEqual(contents,{n:(directory/'result'/f'csv{n}.csv').read_bytes() for n in (1,2,3)})

    def test_csv1_resume_skips_committed_shards_and_keeps_cross_shard_groups(self):
        self.interrupt_after(1,2)
        self.assertEqual(self.state(1)['extra']['sources_done'],2)
        self.assertEqual(pipe.read_json(self.directory()/'process_exit.json')['status'],'INTERRUPTED')
        seen=[]; original=app.iter_fills
        def reader(config,log):
            seen.append(Path(config['fills']['chunks'][0]['path']).stem)
            return original(config,log)
        with patch.object(app,'iter_fills',reader),patch.object(discovery,'discover_fills',side_effect=AssertionError('header discovery repeated')):
            self.invoke('csv1')
        self.assertNotIn('0',seen); self.assertNotIn('1',seen)
        self.assertEqual(len(self.rows(1)),3)
        self.assertEqual(len({r['event_id'] for r in self.rows(1)}),3)

    def test_csv1_resume_rolls_back_uncommitted_csv_and_journal_tail(self):
        original=pipe.AppendCSV.commit
        def crash(table,**extra):
            if table.path.name=='csv1.csv' and extra.get('sources_done')==2:
                raise KeyboardInterrupt('before commit marker')
            original(table,**extra)
        with patch.object(pipe.AppendCSV,'commit',crash),self.assertRaises(KeyboardInterrupt): self.invoke('csv1')
        self.assertEqual(self.state(1)['extra']['sources_done'],1)
        self.invoke('csv1')
        self.assertEqual(len(self.rows(1)),3)
        self.assertIn('CSV_TAIL_ROLLED_BACK',(self.directory()/'result/run.log').read_text())

    def test_csv1_atomic_finalization_recovers_without_fills(self):
        original=pipe.atomic_json
        def save(path,value):
            if Path(path).name=='csv1.json' and value.get('complete'):
                raise KeyboardInterrupt('after replacement before final receipt')
            original(path,value)
        with patch.object(pipe,'atomic_json',save),self.assertRaises(KeyboardInterrupt): self.invoke('csv1')
        self.assertIn('finalizing',self.state(1))
        with patch.object(app,'iter_fills',side_effect=AssertionError('fills must not reread')):
            self.invoke('csv1')
        self.assertTrue(self.state(1)['complete']); self.assertEqual(len(self.rows(1)),3)

    def test_csv2_resume_restores_both_sides_and_zero_qty_then_continues_forward(self):
        self.invoke('csv1'); self.interrupt_after(2,2)
        second=self.rows(2)[1]; state=json.loads(second['replay_state_json'])
        self.assertEqual(next(x['qty'] for x in state if x['oid']=='111'),'0')
        seen=[]; replay=app.replay_blocks
        def checked(book,blocks,snapshot_block,target_block,**kw):
            seen.append((snapshot_block,target_block))
            return replay(book,blocks,snapshot_block,target_block,**kw)
        with patch.object(app,'replay_blocks',checked),patch.object(app,'iter_fills',side_effect=AssertionError('fills')):
            self.invoke('csv2')
        self.assertEqual(seen,[(103,104)])
        self.assertEqual(len(self.rows(2)),3)
        self.assertNotIn('111',{o['oid'] for o in json.loads(self.rows(2)[2]['replay_state_json'])})
        self.invoke('csv3'); self.assertEqual(self.rows(3)[2]['la'],'-0.2')

    def test_csv2_uncommitted_row_is_recomputed_not_duplicated(self):
        self.invoke('csv1'); original=pipe.AppendCSV.commit
        def crash(table,**extra):
            if table.path.name=='csv2.csv' and table.state['rows']==2:
                raise KeyboardInterrupt('before CSV2 commit')
            original(table,**extra)
        with patch.object(pipe.AppendCSV,'commit',crash),self.assertRaises(KeyboardInterrupt): self.invoke('csv2')
        self.assertEqual(self.state(2)['rows'],1)
        self.invoke('csv2'); self.assertEqual(len(self.rows(2)),3)

    def test_csv3_resume_uses_csv_only_and_retains_committed_rows(self):
        self.invoke('csv1'); self.invoke('csv2'); self.interrupt_after(3,1)
        first=self.rows(3)[0]; calls=[]; simulate=app.simulate_close
        def checked(*args): calls.append(args[1]); return simulate(*args)
        with self.forbid_raw(),patch.object(app,'simulate_close',checked): self.invoke('csv3')
        self.assertEqual(calls,['long','short']); self.assertEqual(self.rows(3)[0],first)
        self.assertEqual(len(self.rows(3)),3)

    def test_csv3_uncommitted_tail_is_rolled_back_without_raw_reads(self):
        self.invoke('csv1'); self.invoke('csv2'); original=pipe.AppendCSV.commit
        def crash(table,**extra):
            if table.path.name=='csv3.csv' and table.state['rows']==2: raise KeyboardInterrupt('uncommitted CSV3 row')
            original(table,**extra)
        with patch.object(pipe.AppendCSV,'commit',crash),self.assertRaises(KeyboardInterrupt): self.invoke('csv3')
        self.assertEqual(self.state(3)['rows'],1)
        with self.forbid_raw(): self.invoke('csv3')
        self.assertEqual(len(self.rows(3)),3)
        self.assertEqual([r['la'] for r in self.rows(3)],['0.345','0.345','-0.2'])

    def test_csv_header_commit_interruption_is_recoverable(self):
        original=pipe.atomic_json
        def crash(path,value):
            if Path(path).name=='csv1.json': raise KeyboardInterrupt('first marker not written')
            original(path,value)
        with patch.object(pipe,'atomic_json',crash),self.assertRaises(KeyboardInterrupt): self.invoke('csv1')
        self.assertFalse((self.directory()/'result/.state/csv1.json').exists())
        self.invoke('csv1'); self.assertEqual(len(self.rows(1)),3)

    def test_committed_csv_corruption_is_rejected_at_every_stage(self):
        self.invoke()
        directory=self.directory()
        for number in (1,2,3):
            path=directory/'result'/f'csv{number}.csv'; original=path.read_bytes()
            data=bytearray(original); data[-3]=ord('X') if data[-3]!=ord('X') else ord('Y'); path.write_bytes(data)
            with self.forbid_raw(),self.assertRaisesRegex(pipe.CheckpointError,'integrity mismatch'):
                self.invoke('csv3')
            path.write_bytes(original)

    def test_truncated_committed_csv_is_rejected(self):
        self.invoke('csv1'); path=self.directory()/'result/csv1.csv'
        path.write_bytes(path.read_bytes()[:-4])
        with self.assertRaisesRegex(pipe.CheckpointError,'truncated'): self.invoke('csv1')

    def test_completed_csv_external_append_is_not_silently_discarded(self):
        self.invoke('csv1'); path=self.directory()/'result/csv1.csv'
        with path.open('ab') as f: f.write(b'changed\n')
        with self.assertRaisesRegex(pipe.CheckpointError,'externally appended'): self.invoke('csv1')

    def rewrite_csv2_and_receipt(self,change):
        path=self.directory()/'result/csv2.csv'; rows=self.rows(2); change(rows)
        path.write_bytes(pipe.csv_bytes(pipe.CSV2_FIELDS)+b''.join(pipe.csv_bytes(pipe.CSV2_FIELDS,r) for r in rows))
        receipt=self.directory()/'result/.state/csv2.json'; state=pipe.read_json(receipt)
        state.update(offset=path.stat().st_size,sha256=pipe.hash_file(path).hexdigest())
        discovery.atomic_json(receipt,state)

    def test_csv2_wrong_episode_link_is_rejected_by_csv3(self):
        self.invoke('csv1'); self.invoke('csv2')
        self.rewrite_csv2_and_receipt(lambda rows:rows[0].update(target_block='999'))
        with self.forbid_raw(),self.assertRaisesRegex(pipe.CheckpointError,'not linked'): self.invoke('csv3')

    def test_csv2_depth_metadata_is_verified_before_simulation(self):
        self.invoke('csv1'); self.invoke('csv2')
        self.rewrite_csv2_and_receipt(lambda rows:rows[0].update(total_depth='999'))
        with self.forbid_raw(),self.assertRaisesRegex(pipe.CheckpointError,'depth mismatch'): self.invoke('csv3')

    def test_replay_state_hash_is_checked_before_resume(self):
        self.invoke('csv1'); self.interrupt_after(2,1)
        self.rewrite_csv2_and_receipt(lambda rows:rows[0].update(replay_state_hash='bad'))
        with self.assertRaisesRegex(pipe.CheckpointError,'replay state hash mismatch'): self.invoke('csv2')

    def test_csv2_does_not_need_csv1_journal_after_grouping(self):
        self.invoke('csv1'); (self.directory()/'result/.state/csv1_records.jsonl').unlink()
        with patch.object(app,'iter_fills',side_effect=AssertionError('fills')): self.invoke('csv2')
        self.assertEqual(len(self.rows(2)),3)

    def test_phase_prerequisites_never_fall_back_to_raw_readers(self):
        for stage in ('csv2','csv3'):
            with self.forbid_raw(),self.assertRaisesRegex(pipe.CheckpointError,'CSV1 is not ready'): self.invoke(stage)
        self.invoke('csv1')
        with self.forbid_raw(),self.assertRaisesRegex(pipe.CheckpointError,'CSV2 is not ready'): self.invoke('csv3')

    def test_new_run_and_latest_resume_selection(self):
        first,_=self.invoke()
        second,_=self.invoke(RESUME=False)
        self.assertNotEqual(first,second)
        with self.forbid_raw(): third,_=self.invoke()
        self.assertEqual(second,third)

    def test_code_change_refuses_mixed_checkpoints(self):
        directory,_=self.invoke('csv1'); before=(directory/'result/csv1.csv').read_bytes()
        with patch.object(run_hlp,'engine_hashes',return_value={'changed':'code'}),self.assertRaisesRegex(pipe.CheckpointError,'version changed'):
            self.invoke('csv2')
        self.assertEqual((directory/'result/csv1.csv').read_bytes(),before)

    def test_concurrent_writer_is_rejected(self):
        path=self.root/'lock'
        with pipe.run_lock(path):
            with self.assertRaisesRegex(pipe.CheckpointError,'already active'):
                with pipe.run_lock(path): pass
        with pipe.run_lock(path): pass

    def test_formal_interval_includes_start_and_excludes_end(self):
        self.hour('fills',2,[]); self.hour('fills',3,[])
        event=[self.owner,dict(coin='ETH',side='B',px='12',sz='1',time=app.parse_time('2026-01-01T23:00:00Z'),tid=400,liquidation=dict(method='backstop'))]
        self.hour('fills',23,[self.block(400,'2026-01-01T23:00:00Z',[event])])
        self.invoke('csv1'); rows=self.rows(1)
        self.assertEqual(len(rows),1); self.assertEqual(rows[0]['time'],'2026-01-01T01:00:00.000Z')
        self.assertEqual(rows[0]['coin'],'BTC')

    def test_buffer_fills_are_retained_in_a_core_touching_episode(self):
        self.hour('fills',2,[]); self.hour('fills',3,[])
        self.fill(0,100, time='2026-01-01T00:30:00Z')
        self.fill(22,200,time='2026-01-01T22:30:00Z')
        self.fill(23,201,time='2026-01-01T23:00:00Z')
        self.invoke('csv1'); rows=self.rows(1)
        self.assertEqual(len(rows),4)
        self.assertEqual({r['event_count'] for r in rows},{'2'})
        self.assertEqual(len({r['episode_id'] for r in rows}),2)
        self.assertIn('2026-01-01T23:00:00.000Z',{r['time'] for r in rows})

    def test_no_fills_outside_prepared_dates_is_probed_or_read(self):
        self.hour('fills',23,[],day='20251231'); self.hour('fills',0,[],day='20260102')
        hours=[]; source_paths=[]; probe=discovery.hour_paths; reader=app.open_json_text
        def checked(root,hour): hours.append(hour); return probe(root,hour)
        def opened(path): source_paths.append(str(path)); return reader(path)
        with patch.object(discovery,'hour_paths',checked),patch.object(app,'open_json_text',opened): self.invoke('csv1')
        a=app.parse_time('2026-01-01T00:00:00Z'); z=app.parse_time('2026-01-02T00:00:00Z')
        self.assertEqual(hours,list(range(a,z,3600000)))
        self.assertTrue(all('/20260101/' in p.replace('\\','/') for p in source_paths))
        log=(self.directory()/'result/run.log').read_text(); self.assertEqual(log.count('READ_FILLS_FILE'),24)
        self.assertNotIn('20251231/23.zip',log); self.assertNotIn('20260102/0.zip',log)

    def test_diff_discovery_does_not_request_an_outside_hour(self):
        self.snapshot(time='2025-12-31T23:00:00Z'); self.invoke('csv1')
        hours=[]; probe=discovery.hour_paths
        def checked(root,hour): hours.append(hour); return probe(root,hour)
        with patch.object(discovery,'hour_paths',checked): self.invoke('csv2')
        a=app.parse_time('2026-01-01T00:00:00Z'); z=app.parse_time('2026-01-02T00:00:00Z')
        self.assertTrue(hours); self.assertTrue(all(a<=h<z for h in hours))

    def test_fills_gap_splits_without_invalidating_known_episodes(self):
        (self.root/'fills/20260101/2.zip').unlink()
        self.fill(3,104,side='B')
        self.invoke('csv1'); rows=self.rows(1)
        self.assertEqual(len(rows),2)
        self.assertNotEqual(rows[0]['episode_id'],rows[1]['episode_id'])
        self.assertTrue(rows[0]['split_after']); self.assertTrue(rows[1]['split_before'])
        self.invoke('csv2'); self.assertEqual({r['status'] for r in self.rows(2)},{'ok'})

    def test_missing_diff_is_skipped_and_csv3_preserves_skip_reason(self):
        (self.root/'diffs/20260101/1.zip').unlink()
        self.invoke(); two=self.rows(2); three=self.rows(3)
        self.assertEqual([r['status'] for r in two],['ok','skipped','skipped'])
        self.assertEqual([r['status'] for r in three],['ok','skipped','skipped'])
        self.assertEqual(two[1]['skip_reason'],three[1]['skip_reason']); self.assertEqual(three[1]['la'],'')

    def test_decimal_tails_and_same_price_orders_survive_csv_boundaries(self):
        self.hour('diffs',0,[self.block(101,'2026-01-01T00:30:00Z',[
            old.new_diff(999,'12.345','0.123456789012345678901')])])
        self.invoke(); orders=json.loads(self.rows(2)[0]['orders_json'])
        self.assertEqual([o['oid'] for o in orders],['111','112','999'])
        self.assertEqual(orders[-1]['qty'],'0.123456789012345678901')
        self.assertEqual(self.rows(3)[0]['la'],'0.345')

    def test_snapshot_change_during_replay_is_not_committed_as_success(self):
        self.invoke('csv1'); original=app.ReplayManager.get; changed=[]
        def get(manager,ep):
            book=original(manager,ep)
            if not changed:
                changed.append(True)
                self.snapshot(time='2026-01-01T00:00:01Z')
            return book
        with patch.object(app.ReplayManager,'get',get): self.invoke('csv2')
        self.assertEqual(self.rows(2)[0]['status'],'skipped')
        self.assertIn('SNAPSHOT_CHANGED',self.rows(2)[0]['skip_reason'])
        self.assertEqual(self.rows(2)[0]['replay_state_json'],'')

if __name__=='__main__': unittest.main(verbosity=2)
