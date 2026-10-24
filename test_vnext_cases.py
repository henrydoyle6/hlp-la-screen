"""Hand-calculated specification cases A-G and large-field/locking adversarial checks."""
import contextlib
import io
import json
from decimal import Decimal
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch
import state_store as store
import rmp_l4
import hlp_la_screen as app
from test_support import Fixture,run_fixture,fixture,raw_order
from test_order_book_replay import fill,make_pipeline_config,native_order,write_jsonl


class HandCases(Fixture):
    def test_case_A_four_separate_orders_last_partial_and_long_la_plus_seven(self):
        events=[fill('long1','2026-01-01T01:00:00Z',101,qty='6',price='99'),
                fill('eth','2026-01-01T01:10:00Z',102,'ETH','short','20','1'),
                fill('short','2026-01-01T01:15:00Z',103,direction='short',price='105'),
                fill('long2','2026-01-01T01:25:00Z',104,qty='4',price='101.5')]
        books=[['BTC',[[native_order(101,'102','2'),native_order(102,'102','1'),native_order(103,'101','4'),native_order(104,'99','10')],
                        [native_order(105,'110','20',side='A')]]],['ETH',[[],[native_order(211,'21','20',coin='ETH',side='A')]]]]
        c=make_pipeline_config(self.root/'hand',events,books=books);out=self.root/'hand_output';run_fixture(c,out)
        import csv
        with (out/'csv2.csv').open(newline='') as f:b=[r for r in csv.DictReader(f) if r['record_type']=='episode' and r['coin']=='BTC' and r['direction']=='long'][0]
        with (out/'csv3.csv').open(newline='') as f:r=[r for r in csv.DictReader(f) if r['record_type']=='episode' and r['coin']=='BTC' and r['direction']=='long'][0]
        self.assertEqual((b['order_count'],b['price_level_count'],b['total_depth']),('4','3','17'))
        self.assertEqual((r['total_qty'],r['entry_notional'],r['entry_vwap']),('10','1000','100'))
        self.assertEqual((r['close_notional'],r['exit_vwap'],r['la'],r['status']),('1007','100.7','7','calculated_full'))
        self.assertEqual([t['filled_qty'] for t in json.loads(r['fill_trace_json'])],['2','1','4','3'])
        self.assertEqual(json.loads(b['orders_json'])[-1]['qty'],'10')

    def test_case_C_partial_long_and_short_use_full_episode_cost(self):
        import csv
        for direction,levels,expected in [('long',[('99','3'),('98','2')],'-7'),('short',[('98','2'),('99','3')],'7')]:
            orders=[native_order(i,p,q,side='B' if direction=='long' else 'A') for i,(p,q) in enumerate(levels,1)]
            books=[['BTC',[orders if direction=='long' else [],orders if direction=='short' else []]]]
            c=make_pipeline_config(self.root/direction,[fill('x','2026-01-01T01:00:00Z',101,direction=direction,price='100',qty='10')],books=books)
            out=self.root/(direction+'_out');run_fixture(c,out)
            with (out/'csv3.csv').open(newline='') as f:r=next(csv.DictReader(f))
            self.assertEqual((r['filled_qty'],r['unfilled_qty'],r['close_notional'],r['exit_vwap'],r['la']),('5','5','493','98.6',expected))
            self.assertEqual((r['entry_cost_of_filled_qty'],r['status']),('500','calculated_partial'))

    def gap_input(self):
        early=[self.owner,dict(coin='BTC',side='B',px='12',sz='1',time=app.parse_time('2026-01-01T01:00:00Z'),tid=1,liquidation=dict(method='backstop',liquidatedUser='other'))]
        late=[self.owner,dict(coin='BTC',side='B',px='12',sz='1',time=app.parse_time('2026-01-01T01:15:00Z'),tid=2,liquidation=dict(method='backstop',liquidatedUser='other'))]
        rows=[self.block(102,'2026-01-01T01:00:00Z',[early]),{'gap':{'start':'2026-01-01T01:05:00Z','end':'2026-01-01T01:10:00Z'}},self.block(103,'2026-01-01T01:15:00Z',[late])]
        self.hour('fills',1,rows);self.hour('diffs',1,[self.block(102,'2026-01-01T01:00:00Z'),self.block(103,'2026-01-01T01:15:00Z')])
        return rows

    def test_case_B_five_minute_gap_splits_but_does_not_invalidate(self):
        self.gap_input();self.run_stage()
        rows=self.rows(1);self.assertNotEqual(rows[0]['episode_id'],rows[1]['episode_id'])
        self.assertTrue(rows[0]['split_after']);self.assertEqual(rows[0]['split_after'],rows[1]['split_before'])
        self.assertTrue(all(r['partial_episode']=='false' for r in rows))
        self.assertTrue(all(r['status']=='calculated_full' for r in self.rows(3)))
        self.assertEqual(self.summary['episodes'],2)

    def test_case_G_true_keeps_old_gap_and_false_regroups_after_data_repair(self):
        rows=self.gap_input();self.run_stage();old=self.directory
        self.hour('fills',1,[rows[0],rows[2]])
        self.run_stage();self.assertEqual(self.summary['episodes'],2);self.assertEqual(self.directory,old)
        self.run_stage(RESUME=False);self.assertEqual(self.summary['episodes'],1);self.assertNotEqual(self.directory,old)
        self.assertEqual(self.rows(3)[0]['total_qty'],'2')

    def test_case_E_right_buffer_is_retained_and_pure_buffer_group_is_not_selected(self):
        self.hour('fills',1,[])
        self.snapshot(height=100,time='2026-01-01T22:00:00Z')
        self.fill(22,101,time='2026-01-01T22:50:00Z');self.fill(23,102,time='2026-01-01T23:10:00Z')
        self.hour('diffs',22,[self.block(101,'2026-01-01T22:50:00Z')]);self.hour('diffs',23,[self.block(102,'2026-01-01T23:10:00Z')])
        self.run_stage();self.assertEqual(len(self.rows(1)),2)
        self.assertEqual([r['is_formal_event'] for r in self.rows(1)],['true','false'])
        self.assertEqual(self.rows(2)[0]['target_block'],'102');self.assertEqual(self.rows(3)[0]['total_qty'],'2')

    def test_case_F_missing_history_only_skips_earlier_snapshot_path(self):
        self.snapshot(time='2025-12-31T23:50:00Z');(self.root/'diffs'/'20260101'/'0.zip').unlink()
        self.snapshot('103.rmp',103,'2026-01-01T02:00:00Z');self.fill(3,104)
        self.hour('diffs',3,[self.block(104,'2026-01-01T03:00:00Z')])
        self.run_stage();self.assertEqual([r['status'] for r in self.rows(2)],['skipped','book_ready'])

    def test_outer_right_exact_thirty_minutes_is_partial(self):
        self.hour('fills',1,[])
        events=[]
        for i,hour,minute in [(0,22,50),(1,23,10),(2,23,30)]:
            time=f'2026-01-01T{hour:02d}:{minute:02d}:00Z'
            e=[self.owner,dict(coin='BTC',side='B',px='12',sz='1',time=app.parse_time(time),tid=i,liquidation=dict(method='backstop',liquidatedUser='other'))]
            events.append((hour,self.block(101+i,time,[e])))
        self.hour('fills',22,[events[0][1]]);self.hour('fills',23,[e for h,e in events if h==23])
        self.run_stage('csv1');self.assertTrue(all(r['partial_episode']=='true' for r in self.rows(1)))


class LargeAndCrashCases(Fixture):
    def test_long_csv_fields_and_half_written_trace_recover_without_raw_cache(self):
        value=fixture();value['exchange']['perp_dexs'][0]['books'][0]['book_orders']={n:raw_order(n,qty=100) for n in range(1,6001)}
        self.snapshot(value=value);self.fill(1,102,qty='5000.5')
        self.run_stage('csv1');self.run_stage('csv2')
        self.assertGreater(len(self.rows(2)[0]['orders_json']),131072)
        fired=[]
        def fail(point,**kw):
            if point=='csv_half_written' and kw.get('csv')=='csv3' and not fired:fired.append(1);raise KeyboardInterrupt()
        with patch.object(store,'fault',side_effect=fail):
            with self.assertRaises(KeyboardInterrupt):self.run_stage('csv3')
        with patch.object(store.Cache,'__init__',side_effect=AssertionError('raw cache')):self.run_stage('csv3')
        row=self.rows(3)[0];trace=json.loads(row['fill_trace_json'])
        self.assertEqual(len(trace),5001);self.assertEqual(trace[-1]['filled_qty'],'0.5')
        self.assertEqual(row['la'],'1725.1725');self.assertEqual(row['status'],'calculated_full')
        self.assertEqual(len(self.rows(3)),1)

    def test_interruption_during_replay_does_not_cache_partial_book(self):
        fired=[]
        def fail(point,**kw):
            if point=='replay_block_applied' and not fired:fired.append(1);raise KeyboardInterrupt()
        with patch.object(store,'fault',side_effect=fail):
            with self.assertRaises(KeyboardInterrupt):self.run_stage()
        db=sqlite3.connect(self.directory/'cache.sqlite');self.assertEqual(db.execute("SELECT count(*) FROM entries WHERE kind='replay'").fetchone()[0],0);db.close()
        with patch.object(rmp_l4,'scan_rmp',side_effect=AssertionError('rescan')):self.run_stage()
        self.assertEqual(self.rows(3)[0]['status'],'calculated_full')

    def test_corrupt_cache_database_is_rebuildable_without_discarding_progress(self):
        self.run_stage('csv1');(self.directory/'cache.sqlite').write_bytes(b'not a database')
        self.run_stage('csv2');self.assertEqual(self.rows(2)[0]['status'],'book_ready')
        self.assertIn('CACHE_DATABASE_REBUILT',(self.directory/'run.log').read_text())

    def test_two_processes_cannot_write_the_same_run(self):
        self.run_stage('csv1')
        code="import sys,state_store;ctx=state_store.run_lock(sys.argv[1]);ctx.__enter__();print('LOCKED',flush=True);sys.stdin.readline();ctx.__exit__(None,None,None)"
        child=subprocess.Popen([sys.executable,'-c',code,str(self.directory/'progress.sqlite')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(),'LOCKED')
            with self.assertRaises(store.CheckpointError):self.run_stage('csv2')
        finally:child.communicate('done\n',timeout=10)
