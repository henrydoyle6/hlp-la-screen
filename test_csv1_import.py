"""Portable CSV1 -> CSV2/CSV3, source authority, interruption and zero raw fills reads."""
import builtins
import csv
import hashlib
from pathlib import Path
import shutil
import sqlite3
from unittest.mock import patch
import csv_pipeline as pipe
import input_discovery as discovery
import run_hlp
import state_store as store
from test_support import Fixture


class CSV1ImportTests(Fixture):
    def prepare_export(self):
        self.run_stage('csv1')
        source=self.root/'provided.csv';shutil.copy2(self.directory/'csv1.csv',source)
        self.original=source.read_bytes()
        self.settings.update(OUTPUT_ROOT=str(self.root/'student'),CSV1_INPUT=str(source),RESUME=False)
        return source

    def no_fills(self):return patch.object(discovery,'fills_sources',side_effect=AssertionError('raw fills forbidden'))

    def test_fresh_all_from_csv1_without_source_database_or_fills(self):
        source=self.prepare_export()
        shutil.rmtree(self.root/'output');shutil.rmtree(self.root/'fills')
        with patch.object(pipe,'ingest',side_effect=AssertionError('ingest')),patch.object(pipe,'group',side_effect=AssertionError('regroup')):
            self.run_stage('all')
        self.assertEqual(self.rows(3)[0]['status'],'calculated_full')
        self.assertEqual(source.read_bytes(),self.original)
        self.assertEqual(self.summary['input_mode'],'imported_csv1')
        self.assertIsNone(self.summary['raw_records'])
        self.assertEqual({p.name for p in self.directory.iterdir()},{'csv1.csv','csv2.csv','csv3.csv','run.log','progress.sqlite','cache.sqlite'})
        self.assertIn('CSV1_IMPORT_COMPLETE',(self.directory/'run.log').read_text())

    def test_import_rewrites_only_run_id_and_preserves_episode_hash(self):
        source=self.prepare_export();self.run_stage('csv2')
        with source.open(newline='') as f:original=list(csv.DictReader(f))
        imported=self.rows(1)
        self.assertNotEqual(original[0]['run_id'],imported[0]['run_id'])
        self.assertEqual([{k:v for k,v in r.items() if k!='run_id'} for r in original],
                         [{k:v for k,v in r.items() if k!='run_id'} for r in imported])

    def test_completed_csv1_resume_and_csv3_need_no_external_export(self):
        source=self.prepare_export();self.run_stage('csv2');source.unlink()
        with patch.object(discovery,'fills_sources',side_effect=AssertionError('fills')):
            self.run_stage('csv3',RESUME=True)
        before={n:(self.directory/f'csv{n}.csv').read_bytes() for n in (1,2,3)}
        with patch.object(store.Cache,'__init__',side_effect=AssertionError('cache')):
            self.run_stage('all',RESUME=True)
        self.assertTrue(all((self.directory/f'csv{n}.csv').read_bytes()==before[n] for n in before))

    def test_bad_hash_and_date_and_duplicate_are_rejected_before_csv2(self):
        source=self.prepare_export()
        with source.open(newline='') as f:rows=list(csv.DictReader(f))
        for variant in ('hash','date','duplicate','order'):
            changed=[dict(r) for r in rows]
            if variant=='hash':changed[0]['qty']='999'
            elif variant=='date':changed[0]['formal_end_inclusive']='2026-01-01T22:00:00.000Z'
            elif variant=='duplicate':changed.append(dict(changed[0]))
            else:changed[0]['event_time']='2026-01-02T00:00:00.000Z'
            with source.open('w',newline='') as f:
                writer=csv.DictWriter(f,pipe.CSV1_FIELDS);writer.writeheader();writer.writerows(changed)
            with self.assertRaises((store.CheckpointError,ValueError)):
                self.run_stage('all',RESUME=False)
            self.assertEqual(self.rows(2),[])

    def test_import_crash_each_phase_no_duplicate_or_raw_fills(self):
        source=self.prepare_export()
        for seam in ('csv1_import_row_staged','csv1_import_validated','csv1_import_emit_committed'):
            with self.subTest(seam=seam):
                self.settings['OUTPUT_ROOT']=str(self.root/seam)
                def stop(point,**kw):
                    if point==seam:raise KeyboardInterrupt()
                with patch.object(store,'fault',side_effect=stop):
                    with self.assertRaises(KeyboardInterrupt):self.run_stage('all',RESUME=False)
                with patch.object(pipe,'ingest',side_effect=AssertionError('fills')):
                    self.run_stage('all',RESUME=True)
                self.assertEqual(len(self.rows(1)),1);self.assertEqual(len(self.rows(3)),1)

    def test_changed_export_during_partial_import_requires_new_round(self):
        source=self.prepare_export()
        def stop(point,**kw):
            if point=='csv1_import_row_staged':raise KeyboardInterrupt()
        with patch.object(store,'fault',side_effect=stop):
            with self.assertRaises(KeyboardInterrupt):self.run_stage('all')
        with source.open('a') as f:f.write('\n')
        with self.assertRaisesRegex(store.CheckpointError,'source changed'):
            self.run_stage('all',RESUME=True)

    def test_bom_csv1_and_empty_export_supported(self):
        source=self.prepare_export();source.write_bytes(b'\xef\xbb\xbf'+self.original)
        self.run_stage('all');self.assertEqual(len(self.rows(3)),1)
        source.write_text(','.join(pipe.CSV1_FIELDS)+'\n')
        self.run_stage('all',RESUME=False);self.assertEqual(self.summary['episodes'],0)

    def test_csv3_never_imports_or_rebuilds_missing_upstream(self):
        self.prepare_export()
        with patch.object(pipe,'ingest',side_effect=AssertionError('fills')):
            with self.assertRaises(store.CheckpointError):self.run_stage('csv3')

    def test_committed_read_prefix_resumes_without_duplicate_rows(self):
        stamp='2026-01-01T01:00:00Z'
        import hlp_la_screen as app
        events=[[self.owner,dict(coin='BTC',side='B',px='12',sz='0.001',time=app.parse_time(stamp),tid=i,
                    liquidation=dict(method='backstop',liquidatedUser='other'))] for i in range(300)]
        self.hour('fills',1,[self.block(102,stamp,events)])
        self.prepare_export()
        def stop(point,**kw):
            if point=='csv1_import_read_committed':raise KeyboardInterrupt()
        with patch.object(store,'fault',side_effect=stop):
            with self.assertRaises(KeyboardInterrupt):self.run_stage('all')
        db=sqlite3.connect(self.directory/'progress.sqlite')
        self.assertEqual(db.execute('SELECT count(*) FROM csv1_import_rows').fetchone()[0],256);db.close()
        with patch.object(pipe,'ingest',side_effect=AssertionError('fills')):self.run_stage('all',RESUME=True)
        self.assertEqual(len(self.rows(1)),300);self.assertEqual(self.summary['fills'],300)
        self.assertEqual(len(self.rows(3)),1)

    def test_export_can_be_inside_an_older_run_in_same_output_root(self):
        self.run_stage('csv1');source=self.directory/'csv1.csv';old=source.read_bytes()
        self.run_stage('all',CSV1_INPUT=str(source),RESUME=False)
        self.assertEqual(source.read_bytes(),old)
        self.assertNotEqual(self.directory,source.parent)
        self.assertEqual(self.rows(3)[0]['status'],'calculated_full')
