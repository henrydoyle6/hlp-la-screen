"""Prepared-date boundaries, lazy source routing, local input failures."""
import contextlib
import copy
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
import msgpack
import input_discovery as discovery
import rmp_l4 as rmp
import run_hlp
import csv_pipeline as pipe
import hlp_la_screen as app
import state_store as store
from test_support import Fixture,fixture,Log


class UniversalTests(Fixture):
    def test_prepared_dates_and_inclusive_formal_boundaries(self):
        c,_=run_hlp.build_config(self.settings)
        self.assertEqual(c['data_start'],'2026-01-01T00:00:00.000Z')
        self.assertEqual(c['data_end_exclusive'],'2026-01-02T00:00:00.000Z')
        self.assertEqual(c['formal_start_inclusive'],'2026-01-01T01:00:00.000Z')
        self.assertEqual(c['formal_end_inclusive'],'2026-01-01T23:00:00.000Z')

    def test_date_leap_day_and_cross_year(self):
        for first,last,end in [('2024-02-29','2024-03-01','2024-03-02'),('2025-12-31','2026-01-01','2026-01-02')]:
            c,_=run_hlp.build_config({**self.settings,'START_DATE':first,'END_DATE':last})
            self.assertTrue(c['data_end_exclusive'].startswith(end))

    def test_invalid_dates_paths_accounts_and_intervals(self):
        for changes in [dict(START_DATE='2026/01/01'),dict(END_DATE='2025-12-31'),dict(FILLS_ROOT='relative'),
                        dict(OUTPUT_ROOT=str(self.root/'fills'/'output')),dict(HLP_ACCOUNTS=[]),dict(LOG_BLOCK_INTERVAL=0)]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):run_hlp.build_config({**self.settings,**changes})

    def test_hourly_nested_and_padded_names(self):
        path=self.root/'fills'/'hourly'/'2026-01-01'/'01.jsonl';path.parent.mkdir(parents=True);path.write_text('')
        (self.root/'fills'/'20260101'/'1.zip').unlink()
        self.assertEqual(discovery.hour_paths(self.root/'fills',app.parse_time('2026-01-01T01:00:00Z')),[path])

    def test_ambiguous_hour_does_not_block_other_hour(self):
        self.hour('fills','01',[],suffix='.jsonl')
        self.fill(2,103);self.hour('diffs',2,[self.block(103,'2026-01-01T02:00:00Z')])
        self.run_stage();self.assertEqual(len(self.rows(1)),1)
        self.assertIn('AMBIGUOUS_HOUR',{r['reason_code'] for r in self.rows(1,False)})
        self.assertNotEqual(self.rows(3)[0]['status'],'skipped')

    def test_bad_first_file_large_time_does_not_poison_normal_later_file(self):
        self.hour('fills',0,['{broken\n'])
        self.run_stage();self.assertEqual(len(self.rows(1)),1)
        self.assertEqual(self.rows(3)[0]['status'],'calculated_full')
        self.assertTrue(any(r['record_type']=='issue' for r in self.rows(1,False)))

    def test_no_takeovers_with_errors_still_has_issues_not_fake_episodes(self):
        self.hour('fills',1,['{broken\n'])
        self.run_stage();self.assertEqual(self.rows(1),[]);self.assertEqual(self.rows(2),[]);self.assertEqual(self.rows(3),[])
        issues=[r for r in self.rows(1,False) if r['record_type']=='issue']
        self.assertTrue(issues);self.assertEqual(issues[0]['qty'],'');self.assertEqual(issues[0]['episode_id'],'')
        self.assertEqual(self.summary['episodes'],0);self.assertEqual(self.summary['fills'],0)
        self.assertEqual(self.summary['completion'],'completed_with_input_issues_or_skips')

    def test_empty_files_are_not_gaps_and_missing_file_splits_only(self):
        self.fill(2,103);(self.root/'fills'/'20260101'/'0.zip').unlink()
        self.hour('diffs',2,[self.block(103,'2026-01-01T02:00:00Z')])
        self.run_stage();self.assertEqual(len(self.rows(1)),2)
        self.assertTrue(all(r['status']=='grouped' for r in self.rows(1)))
        self.assertTrue(all(r['partial_episode']=='false' for r in self.rows(1)))

    def test_valid_fill_time_may_cross_nominal_hour_partition(self):
        source=self.root/'fills'/'20260101'/'1.zip'
        content=__import__('zipfile').ZipFile(source).read('hourly/20260101/1').decode()
        self.hour('fills',0,[content]);self.hour('fills',1,[])
        self.run_stage();self.assertEqual(self.rows(1)[0]['is_formal_event'],'true')

    def test_formal_end_equal_included_next_millisecond_not_formal_next_day_excluded(self):
        self.hour('fills',1,[])
        rows=[]
        for i,time in enumerate(['2026-01-01T22:59:59.999Z','2026-01-01T23:00:00Z','2026-01-01T23:00:00.001Z','2026-01-02T00:00:00Z']):
            event=[self.owner,dict(coin='BTC',side='B',px='12',sz='1',time=app.parse_time(time),tid=i,liquidation=dict(method='backstop',liquidatedUser='other'))]
            rows.append(self.block(102+i,time,[event]))
        self.hour('fills',23,rows)
        self.run_stage('csv1');self.assertEqual(len(self.rows(1)),3)
        self.assertEqual([r['is_formal_event'] for r in self.rows(1)],['true','true','false'])
        self.assertEqual(self.rows(1)[0]['formal_event_count'],'2')

    def test_raw_outside_dates_is_not_located_even_when_present(self):
        self.hour('fills',23,[],day='20251231');self.hour('diffs',23,[self.block(101,'2025-12-31T23:50:00Z')],day='20251231')
        seen=[];original=discovery.hour_paths
        def watch(root,hour):seen.append(hour);return original(root,hour)
        with patch.object(discovery,'hour_paths',side_effect=watch):self.run_stage()
        self.assertTrue(all(app.parse_time('2026-01-01T00:00:00Z')<=t<app.parse_time('2026-01-02T00:00:00Z') for t in seen))

    def test_older_seed_does_not_read_outside_diff_dates(self):
        self.snapshot(time='2025-12-31T23:50:00Z')
        (self.root/'diffs'/'20260101'/'0.zip').unlink()
        self.hour('diffs',23,[self.block(101,'2025-12-31T23:55:00Z')],day='20251231')
        seen=[];original=pipe.DiffCache.ensure
        def watch(manager,path,*args,**kw):seen.append(str(path));return original(manager,path,*args,**kw)
        with patch.object(pipe.DiffCache,'ensure',autospec=True,side_effect=watch):self.run_stage()
        self.assertEqual(self.rows(2)[0]['status'],'skipped');self.assertFalse(any('20251231' in p for p in seen))

    def test_snapshot_metadata_not_filename_controls_selection(self):
        p=self.root/'snapshots'/'100.rmp';p.rename(self.root/'snapshots'/'999999999.rmp')
        self.run_stage();self.assertEqual(self.rows(2)[0]['snapshot_block'],'100')

    def test_bounded_header_probe_does_not_full_scan_state(self):
        native=self.root/'snapshots'/'100.rmp'
        with patch.object(discovery,'RMP_HEADER_LIMIT',16):
            with self.assertRaises(ValueError):discovery.rmp_header(native)

    def test_future_snapshot_never_rescues_missing_prior(self):
        (self.root/'snapshots'/'100.rmp').unlink();self.snapshot('103.rmp',103,'2026-01-01T01:01:00Z')
        self.run_stage();self.assertEqual(self.rows(2)[0]['reason_code'],'NO_PRIOR_SNAPSHOT')

    def test_unknown_corrupt_backup_warns_instead_of_global_veto(self):
        (self.root/'snapshots'/'backup.rmp').write_bytes(b'broken')
        self.run_stage();self.assertEqual(self.rows(2)[0]['status'],'book_ready')
        self.assertEqual(self.rows(2)[0]['snapshot_selection_status'],'nearest_resolved_with_warning')
        self.assertTrue(any(r['record_type']=='issue' for r in self.rows(2,False)))

    def test_selected_known_header_corrupt_body_only_skips_dependent_episode(self):
        path=self.root/'snapshots'/'100.rmp';path.write_bytes(path.read_bytes()[:180])
        self.fill(3,104);self.snapshot('103.rmp',103,'2026-01-01T02:00:00Z')
        self.hour('diffs',3,[self.block(104,'2026-01-01T03:00:00Z')])
        self.run_stage();self.assertEqual([r['status'] for r in self.rows(2)],['skipped','book_ready'])

    def test_duplicate_selected_snapshot_only_blocks_its_dependencies(self):
        self.snapshot('100.rmp.zip',zipped=True)
        self.run_stage();self.assertEqual(self.rows(2)[0]['reason_code'],'AMBIGUOUS_SNAPSHOT')

    def test_json_extraction_files_are_not_candidates(self):
        (self.root/'snapshots'/'future.json').write_text('{"orders":[],"coin":"BTC"}')
        self.run_stage();self.assertEqual(self.rows(2)[0]['status'],'book_ready')

    def test_no_episode_does_not_probe_any_snapshot_or_diff(self):
        self.hour('fills',1,[])
        with patch.object(discovery,'rmp_header',side_effect=AssertionError('header')),patch.object(pipe.DiffCache,'ensure',side_effect=AssertionError('diff')):self.run_stage()
        self.assertEqual(self.summary['episodes'],0)

    def test_one_source_unions_all_needed_coins_before_one_body_scan(self):
        self.fill(2,103,coin='xyz:ALPHA',price='5')
        self.hour('diffs',2,[self.block(103,'2026-01-01T02:00:00Z')])
        original=rmp.scan_rmp;demands=[]
        def watch(ref,coins,*args):demands.append(set(coins));return original(ref,coins,*args)
        with patch.object(rmp,'scan_rmp',side_effect=watch):self.run_stage()
        self.assertEqual(demands,[{'BTC','xyz:ALPHA'}]);self.assertEqual(len(self.rows(2)),2)

    def test_overlap_in_diffs_is_not_sorted_or_synthesized_away(self):
        self.hour('diffs',1,[self.block(101,'2026-01-01T00:59:00Z'),self.block(102,'2026-01-01T01:00:00Z')])
        self.run_stage();self.assertEqual(self.rows(2)[0]['reason_code'],'DIFF_BLOCK_ORDER')

    def test_damaged_replay_cache_is_rebuilt_locally_from_extraction(self):
        fired=[]
        def fail(point,**kw):
            if point=='replay_cache_committed' and not fired:fired.append(1);raise KeyboardInterrupt()
        with patch.object(store,'fault',side_effect=fail):
            with self.assertRaises(KeyboardInterrupt):self.run_stage()
        db=sqlite3.connect(self.directory/'cache.sqlite');db.execute("UPDATE entries SET body=x'78' WHERE kind='replay'");db.commit();db.close()
        with patch.object(rmp,'scan_rmp',side_effect=AssertionError('RMP rescan')):self.run_stage()
        self.assertEqual(self.rows(2)[0]['status'],'book_ready')

    def test_source_mutation_during_fills_discards_unit_and_keeps_later_inputs(self):
        self.fill(2,103);self.hour('diffs',2,[self.block(103,'2026-01-01T02:00:00Z')])
        original=pipe.source_identity;seen={}
        def identity(path):
            value=original(path)
            if str(path).endswith('/1.zip') and '/fills/' in str(path):
                seen[str(path)]=seen.get(str(path),0)+1
                if seen[str(path)]==2:value['size']+=1
            return value
        with patch.object(pipe,'source_identity',side_effect=identity):self.run_stage()
        self.assertEqual(len(self.rows(1)),1);self.assertEqual(self.rows(1)[0]['block_number'],'103')
        self.assertIn('SOURCE_CHANGED',{r['reason_code'] for r in self.rows(1,False)})
