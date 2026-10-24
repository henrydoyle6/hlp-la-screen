"""Ubuntu entry/discovery regression tests using the real loader/replay pipeline."""
import contextlib
import copy
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile
import msgpack

import hlp_la_screen as app
import input_discovery as discovery
import run_hlp
from prepare_local_diffs import prepare_diff_inputs
from test_ondemand import fixture, Log


class UniversalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('fills', 'diffs', 'snapshots'):
            (self.root / name).mkdir()
        self.settings = run_hlp.settings_from_top()
        self.settings.update(START_DATE='2026-01-01', END_DATE='2026-01-01',
            FILLS_ROOT=str(self.root / 'fills'), DIFFS_ROOT=str(self.root / 'diffs'),
            SNAPSHOTS_ROOT=str(self.root / 'snapshots'), OUTPUT_ROOT=str(self.root / 'output'),
            CACHE_ROOT=str(self.root / 'cache'), CONSOLE_LOG=False)
        self.log = Log()
        self.config, _ = run_hlp.build_config(self.settings)
        self.run_dir = self.root / 'discovery_run'
        self.run_dir.mkdir()
        self.config['auto_input_discovery'].update(run_dir=str(self.run_dir), missing_root=str(self.run_dir / 'missing'))

    def hour(self, kind, hour, rows, day='20260101', suffix='.zip', nested=False):
        p = self.root / kind / ('hourly' if nested else '') / day / (str(hour) + suffix)
        p.parent.mkdir(parents=True, exist_ok=True)
        content = ''.join(json.dumps(r) + '\n' for r in rows)
        if suffix.endswith('.zip'):
            with zipfile.ZipFile(p, 'w') as z:
                z.writestr(f'hourly/{day}/{hour}', content)
        else:
            p.write_text(content, encoding='utf-8')
        return p

    def block(self, height, time, events=None):
        return dict(block_number=height, block_time=time, events=[] if events is None else events)

    def snapshot(self, name='100.rmp', height=100, time='2026-01-01T00:00:00Z'):
        p = self.root / 'snapshots' / name
        p.write_bytes(msgpack.packb(fixture(height, time), use_bin_type=True))
        return p

    def ep(self, height=102, time='2026-01-01T01:00:00Z', name='e'):
        return SimpleNamespace(episode_id=name, coin='BTC', snapshot=None,
            last=SimpleNamespace(block_number=height, time_ms=app.parse_time(time)))

    def index(self):
        return discovery.HeaderIndex(self.root / 'headers', self.log)

    def full_inputs(self, missing_diff=False):
        self.hour('fills', 23, [], day='20251231')
        for h in range(24):
            self.hour('fills', h, [])
        for h in (0, 1):
            self.hour('fills', h, [], day='20260102')
        owner = self.settings['HLP_ACCOUNTS'][0]
        event = [owner, dict(coin='BTC', side='B', px='12', sz='1', time=app.parse_time('2026-01-01T01:00:00Z'),
                            tid=1, liquidation=dict(method='backstop', liquidatedUser='0xother'))]
        self.hour('fills', 1, [self.block(102, '2026-01-01T01:00:00Z', [event])])
        self.snapshot()
        if not missing_diff:
            self.hour('diffs', 0, [self.block(101, '2026-01-01T00:30:00Z')])
        self.hour('diffs', 1, [self.block(102, '2026-01-01T01:00:00Z')])

    def test_prepared_date_range_has_internal_buffers(self):
        self.assertEqual(self.config['data_start'], '2026-01-01T00:00:00.000Z')
        self.assertEqual(self.config['data_end'], '2026-01-01T23:59:59.999Z')
        self.assertEqual(app.parse_time(self.config['data_end']) - app.BUFFER_MS,
                         app.parse_time('2026-01-01T22:59:59.999Z'))

    def test_cross_year_and_leap_day_dates(self):
        self.settings.update(START_DATE='2024-02-29', END_DATE='2024-03-01')
        c, _ = run_hlp.build_config(self.settings)
        self.assertEqual(c['data_end'], '2024-03-01T23:59:59.999Z')

    def test_configuration_rejects_bad_dates_relative_and_unsafe_output(self):
        for change in (dict(START_DATE='2026/01/01'), dict(END_DATE='2025-12-31'),
                       dict(FILLS_ROOT='relative'), dict(OUTPUT_ROOT=str(self.root / 'fills' / 'output')),
                       dict(HLP_ACCOUNTS=[]), dict(LOG_BLOCK_INTERVAL=0)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                run_hlp.build_config({**self.settings, **change})

    def test_hourly_raw_and_nested_padded_filenames(self):
        p = self.hour('fills', '01', [self.block(1, '2026-01-01T01:00:00Z')], suffix='.jsonl', nested=True)
        self.assertEqual(discovery.hour_paths(self.root / 'fills', app.parse_time('2026-01-01T01:20:00Z') // discovery.HOUR * discovery.HOUR), [p.resolve()])

    def test_ambiguous_hour_becomes_explicit_gap_not_arbitrary_selection(self):
        self.hour('fills', 1, [])
        self.hour('fills', '01', [], suffix='.jsonl')
        t = app.parse_time('2026-01-01T01:00:00Z')
        row = discovery.hourly_sources(self.root / 'fills', t, t, self.index(), self.log, 'fills', self.root / 'missing')[0]
        self.assertIn('Multiple files', row['error'])
        self.assertFalse(Path(row['path']).exists())
        self.assertEqual(len(row['candidates']), 2)

    def test_header_cache_reuses_metadata_and_source_change_invalidates(self):
        p = self.snapshot()
        index = self.index()
        a = index.get(p, 'rmp')
        with patch.object(discovery, 'rmp_header', side_effect=AssertionError('no second probe')):
            self.assertEqual(self.index().get(p, 'rmp'), a)
        self.snapshot(height=101)
        self.assertEqual(self.index().get(p, 'rmp')['block_number'], 101)

    def test_snapshot_metadata_is_actual_context_not_filename(self):
        p = self.snapshot('99999999.rmp')
        self.assertEqual(discovery.rmp_header(p)['block_number'], 100)
        ep = self.ep()
        discovery.discover_book_inputs(self.config, [ep], self.log)
        self.assertEqual(ep.snapshot.block_number, 100)

    def test_rmp_header_probe_is_bounded_and_not_full_state_scan(self):
        p = self.snapshot()
        with p.open('ab') as f:
            f.write(b'x' * (discovery.RMP_HEADER_LIMIT * 2))
        self.assertLessEqual(discovery.rmp_header(p)['header_bytes_read'], 4096)
        delayed = self.root / 'snapshots' / 'delayed.rmp'
        delayed.write_bytes(msgpack.packb(dict(junk='x' * (discovery.RMP_HEADER_LIMIT + 100), exchange={'context':{'height':1,'time':'2026-01-01'}})))
        with self.assertRaises(ValueError):
            discovery.rmp_header(delayed)

    def test_future_snapshot_is_never_selected(self):
        before = self.snapshot()
        self.snapshot('103.rmp', 103, '2026-01-01T01:01:00Z')
        ep = self.ep()
        discovery.discover_book_inputs(self.config, [ep], self.log)
        self.assertEqual(ep.snapshot.path, str(before.resolve()))

    def test_no_prior_snapshot_leaves_original_local_failure(self):
        self.snapshot('103.rmp', 103, '2026-01-01T01:01:00Z')
        ep = self.ep()
        discovery.discover_book_inputs(self.config, [ep], self.log)
        manager = app.ReplayManager(self.config, self.log)
        manager.prepare([ep])
        with self.assertRaises(app.BookError) as c:
            manager.get(ep)
        self.assertIn('SNAPSHOT', c.exception.code)
        self.assertEqual(manager.extraction.stats['rmp_scans'], 0)

    def test_unreadable_potential_nearest_snapshot_prohibits_older_fallback(self):
        self.snapshot()
        (self.root / 'snapshots' / '101.rmp').write_bytes(b'broken')
        ep = self.ep()
        discovery.discover_book_inputs(self.config, [ep], self.log)
        manager = app.ReplayManager(self.config, self.log)
        manager.prepare([ep])
        self.assertEqual(manager.planning_errors['e'].code, 'SNAPSHOT_METADATA_UNREADABLE')
        self.assertEqual(manager.extraction.stats['rmp_scans'], 0)

    def test_duplicate_selected_snapshot_is_explicit_ambiguity(self):
        p = self.snapshot()
        z = self.root / 'snapshots' / '100.rmp.zip'
        with zipfile.ZipFile(z, 'w') as out:
            out.write(p, arcname=p.name)
        ep = self.ep()
        discovery.discover_book_inputs(self.config, [ep], self.log)
        self.assertEqual(ep.discovery_error.code, 'AMBIGUOUS_SNAPSHOT')

    def test_single_coin_json_is_not_snapshot_candidate(self):
        self.snapshot()
        (self.root / 'snapshots' / '101.json').write_text('{}')
        ep = self.ep()
        discovery.discover_book_inputs(self.config, [ep], self.log)
        self.assertEqual(len(self.config['snapshots']), 1)

    def test_actual_fill_boundary_includes_late_local_partition(self):
        # An event before the UTC hour sits in the NEXT local_time partition.
        owner = self.settings['HLP_ACCOUNTS'][0]
        t = app.parse_time('2026-01-01T22:59:59.900Z')
        event = [owner, dict(coin='BTC', side='B', px='12', sz='1', time=t, tid=9,
                            liquidation=dict(method='backstop'))]
        p = self.hour('fills', 23, [self.block(999, app.time_text(t), [event])])
        discovery.discover_fills(self.config, self.log)
        chunk = next(c for c in self.config['fills']['chunks'] if c['path'] == str(p.resolve()))
        self.assertEqual(chunk['start'], app.time_text(t))
        records = list(app.iter_fills(self.config, self.log))
        self.assertEqual([r.event_id for r in records if isinstance(r, app.FillRecord)], [f'{owner}:9:23.zip:1:0'])

    def test_empty_hour_is_available_and_missing_hour_remains_gap(self):
        self.hour('fills', 0, [])
        discovery.discover_fills(self.config, self.log)
        records = list(app.iter_fills(self.config, self.log))
        gaps = [g.source_file for g in records if isinstance(g, app.FillGap)]
        self.assertNotIn(str(self.root / 'fills' / '20260101' / '0.zip'), gaps)
        self.assertTrue(gaps)

    def test_diff_discovery_stays_inside_prepared_range_even_with_older_seed(self):
        self.snapshot(time='2025-12-30T23:00:00Z')
        ep = self.ep()
        discovery.discover_book_inputs(self.config, [ep], self.log)
        manifest = json.loads((self.run_dir / 'book_discovery.json').read_text())
        self.assertEqual(manifest['diff_sources'][0]['hour'], app.parse_time('2026-01-01T00:00:00Z'))

    def test_projection_does_not_hide_overlap_between_hourly_sources(self):
        self.snapshot()
        self.hour('diffs', 0, [self.block(101, '2026-01-01T00:30:00Z'), self.block(102, '2026-01-01T00:40:00Z')])
        self.hour('diffs', 1, [self.block(102, '2026-01-01T01:00:00Z', [{'coin':'BTC','oid':111,'raw_book_diff':'remove'}]),
                               self.block(103, '2026-01-01T01:01:00Z')])
        ep = self.ep(103, '2026-01-01T01:01:00Z')
        discovery.discover_book_inputs(self.config, [ep], self.log)
        manager = app.ReplayManager(self.config, self.log)
        manager.prepare([ep])
        manager.config['_prepared_coin_diffs'] = prepare_diff_inputs(self.config, [ep], manager, self.log)
        with self.assertRaises(app.InputProblem) as c:
            manager.get(ep)
        self.assertEqual(c.exception.code, 'DIFF_BLOCK_ORDER')
        self.assertFalse(manager.cache)

    def test_full_entry_uses_original_pipeline_and_warm_cache(self):
        self.full_inputs()
        with contextlib.redirect_stdout(io.StringIO()):
            first, summary = run_hlp.run(self.settings)
        self.assertEqual(summary['fills'], 1)
        self.assertEqual(summary['calculated'], 1)
        self.assertEqual(summary['skipped'], 0)
        rows = list(csv.DictReader(io.StringIO((first / 'result' / 'csv2.csv').read_text())))
        orders = json.loads(rows[0]['orders_json'])
        self.assertEqual([(o['oid'],o['qty']) for o in orders], [('111','2.5'),('112','2.5')])
        la = list(csv.DictReader(io.StringIO((first / 'result' / 'csv3.csv').read_text())))[0]
        self.assertEqual(la['la'], '0.345')
        self.settings['RESUME'] = False
        with patch('rmp_l4.scan_rmp', side_effect=AssertionError('warm run must not scan RMP')), contextlib.redirect_stdout(io.StringIO()):
            second, repeat = run_hlp.run(self.settings)
        self.assertNotEqual(first, second)
        self.assertEqual(summary, repeat)
        text = (second / 'result' / 'run.log').read_text()
        self.assertIn('DIFF_INPUT_CACHE_HIT', text)
        self.assertIn('INPUT_HEADER_CACHE_HIT', text)
        self.assertIn('"rmp_scans":0', text)
        self.assertTrue((second / 'source' / 'run_hlp.py').is_file())

    def test_full_entry_missing_diff_skips_without_future_rescue(self):
        self.full_inputs(missing_diff=True)
        self.snapshot('104.rmp',104,'2026-01-01T01:10:00Z')
        with contextlib.redirect_stdout(io.StringIO()):
            run, summary = run_hlp.run(self.settings)
        self.assertEqual(summary['skipped'], 1)
        row = list(csv.DictReader(io.StringIO((run / 'result' / 'csv3.csv').read_text())))[0]
        self.assertEqual(row['la'], '')
        self.assertEqual(row['status'], 'skipped')

    def test_missing_middle_hour_only_skips_targets_that_need_it(self):
        self.snapshot()
        self.snapshot('104.rmp', 104, '2026-01-01T02:00:00Z')
        self.hour('diffs', 0, [self.block(101, '2026-01-01T00:30:00Z')])
        self.hour('diffs', 2, [self.block(105, '2026-01-01T02:30:00Z')])
        early = self.ep(101, '2026-01-01T00:30:00Z', 'early')
        gap = self.ep(103, '2026-01-01T01:30:00Z', 'gap')
        late = self.ep(105, '2026-01-01T02:30:00Z', 'late')
        episodes = [early, gap, late]
        discovery.discover_book_inputs(self.config, episodes, self.log)
        manager = app.ReplayManager(self.config, self.log)
        manager.prepare(episodes)
        manager.config['_prepared_coin_diffs'] = prepare_diff_inputs(self.config, episodes, manager, self.log)
        self.assertTrue(manager.get(early).orders)
        with self.assertRaises((app.InputProblem, app.BookError)):
            manager.get(gap)
        self.assertTrue(manager.get(late).orders)
        self.assertEqual(manager.stats['replay_commits'], 2)
        self.assertEqual(manager.extraction.stats['rmp_scans'], 2)

    def test_one_source_unions_coins_before_only_one_body_scan(self):
        self.snapshot()
        self.hour('diffs', 0, [self.block(101, '2026-01-01T00:30:00Z')])
        btc = self.ep(101, '2026-01-01T00:30:00Z', 'btc')
        spot = self.ep(101, '2026-01-01T00:30:00Z', 'spot')
        spot.coin = '@TEST'
        discovery.discover_book_inputs(self.config, [btc, spot], self.log)
        manager = app.ReplayManager(self.config, self.log)
        manager.prepare([btc, spot])
        self.assertEqual(manager.extraction.stats['rmp_scans'], 1)
        self.assertEqual(len(manager.l4_paths), 2)
        self.assertEqual(set(json.loads(Path(manager.l4_paths[(spot.snapshot,spot.coin)]).read_text())['orders'][0]),
                         {'coin','oid','side','limitPx','sz','isTrigger','user'})

    def test_unreadable_snapshot_subtree_is_not_silently_ignored(self):
        self.snapshot()
        def unreadable(root, onerror):
            onerror(PermissionError('unreadable date directory'))
        with patch.object(discovery.os, 'walk', side_effect=unreadable):
            with self.assertRaisesRegex(OSError, 'Cannot inventory snapshot directory'):
                discovery.discover_book_inputs(self.config, [self.ep()], self.log)

    def test_no_episodes_does_not_probe_or_scan_any_snapshot(self):
        self.snapshot()
        with patch.object(discovery, 'rmp_header', side_effect=AssertionError('no demanded episode')):
            discovery.discover_book_inputs(self.config, [], self.log)
        self.assertEqual(self.config['snapshots'], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
