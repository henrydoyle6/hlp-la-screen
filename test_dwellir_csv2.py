"""Synthetic native RMP + dense diff integration, permission gates, and audits."""
import copy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import lz4.frame
import msgpack
import dwellir_csv2 as d
import csv_pipeline as pipe
import hlp_la_screen as app
from test_support import fixture, raw_order


class DwellirTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root/'raw'; self.prep = self.root/'prepared'
        self.plan_path = self.prep/'tasks.json'
        self.manifest_path = self.root/'sample.local.json'
        self.ep_coins = dict(zip([n for _,ns in d.SAMPLES for n in ns],
                                ['FARTCOIN','ETH','BTC','XPL','XRP','CRV']))
        rows, windows, objects = [], [], []
        for index, ((wid, ids), day) in enumerate(zip(d.SAMPLES, ['2026-06-15','2026-08-19','2026-08-22','2026-10-30']), 1):
            base = index*100
            before_key = f'periodic_abci_states/{day.replace("-", "")}/{base}.rmp.lz4'
            after_key = f'periodic_abci_states/{day.replace("-", "")}/{base+4}.rmp.lz4'
            diff_key = f'node_raw_book_diffs_by_block/{day.replace("-", "")}/1.lz4'
            before = fixture(base, day+'T01:00:00Z')
            main = before['exchange']['perp_dexs'][0]
            main['clearinghouse']['meta']['universe'] = [dict(name=c,szDecimals=2) for c in self.ep_coins.values()]
            main['books'] = [dict(asset=i, book_orders={1:raw_order(111),2:raw_order(112),3:raw_order(113,'A',124000,50)})
                             for i in range(6)]
            after = copy.deepcopy(before)
            after['exchange']['context'].update(height=base+4,time=day+'T01:00:04Z')
            for book in after['exchange']['perp_dexs'][0]['books']:
                book['book_orders'].pop(1)
                book['book_orders'][4] = raw_order(444,qty=100)
            changes = [dict(update=dict(origSz='2.5',newSz='1.5')),
                       dict(update=dict(origSz='1.5',newSz='0')), 'remove',dict(new=dict(sz='1'))]
            blocks = []
            for n, mutation in enumerate(changes, 1):
                blocks.append(dict(block_number=base+n, block_time=f'{day}T01:00:0{n}.000',
                    events=[dict(coin=c,oid=444 if n==4 else 111,side='B',px='12.345',raw_book_diff=mutation)
                            for c in self.ep_coins.values()]))
            for key, value in [(before_key,msgpack.packb(before,use_bin_type=True)),
                               (after_key,msgpack.packb(after,use_bin_type=True)),
                               (diff_key,''.join(json.dumps(b)+'\n' for b in blocks).encode())]:
                path = d.object_path(self.raw,key);path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(lz4.frame.compress(value))
                objects.append(dict(key=key,size_bytes=path.stat().st_size,sha256=d.sha(path),url='https://data.dwellir.com/sample'))
            for j, name in enumerate(ids):
                time=f'{day}T01:00:0{2+j}.000Z'
                row={k:'' for k in pipe.CSV1_FIELDS}
                row.update(schema_version='hlp-csv-v3',run_id='fixture',record_type='fill',episode_id=name,status='grouped',
                    issues_json='[]',event_id=name,event_time=time,block_number=str(base+2+j),coin=self.ep_coins[name],
                    direction='short' if index==2 else 'long',price='12',qty='1',source_file='synthetic',source_line='1',
                    source_event_index='0',data_start='2025-03-22T00:00:00.000Z',data_end_exclusive='2026-11-01T00:00:00.000Z',
                    formal_start_inclusive='2025-03-22T01:00:00.000Z',formal_end_inclusive='2026-10-31T23:00:00.000Z',
                    is_formal_event='true',segment_id='0',episode_start=time,episode_end=time,start_block=str(base+2+j),
                    end_block=str(base+2+j),event_count='1',formal_event_count='1',buffer_event_count='0',duration_ms='0',
                    total_qty='1',entry_notional='12',entry_vwap='12',partial_episode='false')
                row['episode_input_hash']=pipe.episode_hash([row]); rows.append(row)
            windows.append(dict(id=wid,episode_ids=ids,before_key=before_key,after_key=after_key,
                                after_block=base+4,diff_keys=[diff_key]))
        for i in range(4):
            row={k:'' for k in pipe.CSV1_FIELDS}
            row.update(schema_version='hlp-csv-v3',run_id='fixture',record_type='issue',status='input_issue',reason_code='SOURCE_GAP',issues_json='[]')
            rows.append(row)
        self.source=self.root/'source.csv'
        self.source.write_bytes(d.csv_bytes(d.CHINESE_CSV1,[dict(zip(d.CHINESE_CSV1,[r[k] for k in pipe.CSV1_FIELDS])) for r in rows]))
        self.plan=d.prepare(self.source,self.prep,expected_sha=d.sha(self.source))
        self.manifest=dict(plan_hash=self.plan['plan_hash'],stage='sample',data_kind='synthetic',cost_usd=0,objects=objects,windows=windows)
        self.save_manifest()

    def save_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest),encoding='utf-8')

    def test_prepare_preserves_cells_diagnostics_and_is_immutable(self):
        selected=d.native_rows(self.prep/'csv1_selected.csv')
        self.assertEqual(selected,d.native_rows(self.source))
        self.assertEqual(self.plan['counts']['issues'],4)
        d.prepare(self.source,self.prep,expected_sha=d.sha(self.source))
        with self.assertRaisesRegex(d.InvalidInput,'SHA256'):
            d.prepare(self.source,self.root/'bad')
        (self.prep/'csv1_selected.csv').write_bytes(b'changed')
        with self.assertRaisesRegex(d.InvalidInput,'CSV1 changed'):d.load_plan(self.plan_path)

    def test_permission_gates_rpc_credits_are_not_archive_entitlement(self):
        self.assertEqual(d.assess_access()['status'],'awaiting_registration')
        v=dict(registered=True,email_verified=True,rpc_credits=5000000)
        self.assertEqual(d.assess_access(v)['status'],'blocked_free_archive_permission')
        v.update(free_archive_access=True,cost_usd=0,transport='s3')
        self.assertEqual(d.assess_access(v)['status'],'blocked_s3_transport')
        v.update(transport='dwellir_https',prefixes=d.PREFIXES,observation_reference='operator observation')
        self.assertEqual(d.assess_access(v)['status'],'operator_confirmed_free_archive_access')

    def test_manifest_limits_identity_and_coverage(self):
        d.load_manifest(self.plan_path,self.manifest_path)
        for mutate in [lambda m:m.update(plan_hash='bad'),lambda m:m.update(cost_usd=1),
                       lambda m:m['objects'][0].update(size_bytes=d.SAMPLE_LIMIT+1),
                       lambda m:m['objects'].append(m['objects'][0]),
                       lambda m:m['windows'][0].update(episode_ids=['EP000999'])]:
            m=copy.deepcopy(self.manifest);mutate(m)
            self.manifest_path.write_text(json.dumps(m))
            with self.assertRaises(d.InvalidInput):d.load_manifest(self.plan_path,self.manifest_path)

    def test_aws_other_hosts_redirects_and_path_escape_rejected(self):
        for url in ['http://data.dwellir.com/a','https://dwellir.com.evil.test/a','https://s3.amazonaws.com/a',
                    'https://s3.ap-southeast-1.wasabisys.com/a','https://x@data.dwellir.com/a']:
            with self.assertRaises(d.InvalidInput):d.allowed_url(url)
        for key in ['../state.rmp','periodic_abci_states/../../state','node_raw_book_diffs_by_block/20260819/24.lz4']:
            with self.assertRaises(d.InvalidInput):d.object_path(self.raw,key)
        with self.assertRaises(d.InvalidInput):d.NoRedirect().redirect_request(None,None,302,'',{},'https://s3.amazonaws.com/a')

    def test_fetch_gate_precedes_network(self):
        evidence=self.root/'evidence.json';evidence.write_text(json.dumps(dict(registered=True,email_verified=True)))
        with patch('urllib.request.build_opener') as network:
            with self.assertRaises(d.InvalidInput):d.fetch(self.plan_path,self.manifest_path,self.raw,evidence)
            network.assert_not_called()

    def test_download_verifies_bytes_and_reuses_only_complete_objects(self):
        obj=self.manifest['objects'][0]
        target=d.object_path(self.raw,obj['key']); original=target.read_bytes();target.unlink()
        self.manifest['data_kind']='provider_archive';self.save_manifest()
        evidence=self.root/'access.json'
        evidence.write_text(json.dumps(dict(registered=True,email_verified=True,free_archive_access=True,cost_usd=0,
            transport='dwellir_https',prefixes=d.PREFIXES,observation_reference='synthetic test only')))
        class Response(io.BytesIO):
            status=200
            headers={'Content-Length':str(len(original))}
            def geturl(self):return 'https://data.dwellir.com/sample'
        with patch('urllib.request.build_opener') as transport:
            transport.return_value.open.return_value=Response(original[:-1])
            with self.assertRaises(d.InvalidInput):d.fetch(self.plan_path,self.manifest_path,self.raw,evidence)
            self.assertFalse(target.exists())
            self.assertTrue(target.with_name(target.name+'.part').exists())
            transport.return_value.open.return_value=Response(original)
            result=d.fetch(self.plan_path,self.manifest_path,self.raw,evidence)
            self.assertEqual(result['transferred_bytes'],len(original))
            transport.return_value.open.reset_mock()
            self.assertEqual(d.fetch(self.plan_path,self.manifest_path,self.raw,evidence)['transferred_bytes'],0)
            transport.return_value.open.assert_not_called()
        self.assertEqual(d.sha(target),obj['sha256'])

    def test_synthetic_receipt_never_unlocks_full_run(self):
        receipt=self.root/'receipt.json'
        r=d.verify_sample(self.plan_path,self.manifest_path,self.raw,receipt)
        self.assertEqual(r['status'],'PASS');self.assertFalse(r['real_archive_sample_verified'])
        with self.assertRaises(d.InvalidInput):d.sample_gate(self.plan_path,receipt,self.raw)

    def test_exact_real_schema_pair_gap_mismatch_and_corruption(self):
        first=self.manifest['windows'][0]
        path=d.object_path(self.raw,first['diff_keys'][0])
        blocks=lz4.frame.decompress(path.read_bytes()).decode().splitlines()
        path.write_bytes(lz4.frame.compress(('\n'.join(blocks[1:])+'\n').encode()))
        for obj in self.manifest['objects']:
            if obj['key']==first['diff_keys'][0]:obj.update(size_bytes=path.stat().st_size,sha256=d.sha(path))
        self.save_manifest()
        with self.assertRaises(app.BookError):d.verify_sample(self.plan_path,self.manifest_path,self.raw,self.root/'gap.json')
        path.write_bytes(b'broken')
        with self.assertRaises(d.InvalidInput):d.checked_files(self.manifest,self.raw)

    def test_exact_mismatch_saves_failed_receipt_and_never_accepts_archive(self):
        key=self.manifest['windows'][0]['after_key']
        path=d.object_path(self.raw,key)
        answer=msgpack.unpackb(lz4.frame.decompress(path.read_bytes()),raw=False,strict_map_key=False)
        answer['exchange']['perp_dexs'][0]['books'][0]['book_orders'][2]['r']=123
        path.write_bytes(lz4.frame.compress(msgpack.packb(answer,use_bin_type=True)))
        for obj in self.manifest['objects']:
            if obj['key']==key:obj.update(size_bytes=path.stat().st_size,sha256=d.sha(path))
        self.manifest['data_kind']='provider_archive';self.save_manifest()
        output=self.root/'mismatch.json'
        with self.assertRaisesRegex(d.InvalidInput,'Exact sample comparison failed'):
            d.verify_sample(self.plan_path,self.manifest_path,self.raw,output)
        receipt=d.read_json(output)
        self.assertEqual(receipt['status'],'FAIL')
        self.assertFalse(receipt['real_archive_sample_verified'])

    @unittest.skipIf(__import__('sys').version_info<(3,12),'Native production uses Python 3.12+')
    def test_native_target_block_six_files_resume_and_47_column_export(self):
        out=self.root/'runs'
        run=d.run_native(self.plan_path,self.manifest_path,self.raw,out)
        directory=Path(run['directory'])
        self.assertEqual({p.name for p in directory.iterdir()},d.SIX_FILES)
        with (directory/'csv2.csv').open() as f:rows=list(csv.DictReader(f))
        business=[r for r in rows if r['record_type']=='episode']
        self.assertEqual(len(business),6)
        self.assertTrue(all(r['status']=='book_ready' and r['last_applied_block']==r['target_block'] for r in business))
        self.assertEqual(len(list((self.raw/'routes').rglob('*.rmp.lz4'))),4)
        original=d.sha(directory/'csv2.csv')
        self.assertEqual(run,d.run_native(self.plan_path,self.manifest_path,self.raw,out))
        self.assertEqual(original,d.sha(directory/'csv2.csv'))
        export=self.root/'delivery.csv'
        r=d.export(self.plan_path,directory,export,stage='sample')
        self.assertEqual((r['episodes'],r['columns']),(6,47))
        with export.open(encoding='utf-8-sig') as f:
            self.assertTrue(all(row['snapshot_lag_ms']=='' for row in csv.DictReader(f)))
        with (directory/'csv2.csv').open() as f: damaged=list(csv.DictReader(f))
        damaged[0]['total_depth']='999'
        (directory/'csv2.csv').write_bytes(d.csv_bytes(pipe.CSV2_FIELDS,damaged))
        with self.assertRaises(d.InvalidInput):d.export(self.plan_path,directory,self.root/'bad.csv',stage='sample')


if __name__=='__main__':unittest.main()
