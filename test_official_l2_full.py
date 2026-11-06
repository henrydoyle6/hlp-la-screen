"""Small offline checks for the full-run wrapper and frozen transport reuse."""

from datetime import datetime, timedelta, timezone
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

CLOUD_TOOLS = Path(__file__).resolve().parent / 'tools' / 'official_l2'
_prior_path = list(sys.path)
try:
    sys.path.insert(0, str(CLOUD_TOOLS))
    import cloud_worker_full as full
    import cloud_worker as sample
    import generate_full_plan as plan_generator
finally:
    sys.path[:] = _prior_path
assert Path(full.__file__).resolve() == CLOUD_TOOLS / 'cloud_worker_full.py'
assert Path(sample.__file__).resolve() == CLOUD_TOOLS / 'cloud_worker.py'
assert Path(plan_generator.__file__).resolve() == CLOUD_TOOLS / 'generate_full_plan.py'


class FullScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        for name in full.BUNDLE_FILES:
            (self.directory / name).write_bytes(b'offline fixture')
        approved = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.plan = {
            'account_id': '555066174629', 'region': 'us-east-1',
            'source_bucket': 'hyperliquid-archive', 'approval_recorded': True,
            'approved_at_utc': approved.isoformat(),
            'expires_at_utc': (approved + timedelta(hours=6)).isoformat(),
            'selection_policy': 'validated_next_hour', 'csv2_start_inclusive': full.START,
            'csv2_end_exclusive': full.END, 'expected_episode_count': 2,
            'csv1_sha256': hashlib.sha256((self.directory / 'csv1.csv').read_bytes()).hexdigest(),
            'episode_ids': ['EP-A', 'EP-B'],
            'allowed_keys': ['market_data/20251021/22/l2Book/BTC.lz4',
                             'market_data/20251021/23/l2Book/BTC.lz4'],
            'limits': dict(full.LIMIT_CAPS),
        }
        self.episodes = [
            {'episode_id': 'EP-A', 'coin': 'BTC', 'tn_ns': full.parse_time_ns('2025-10-21T22:01:00Z')},
            {'episode_id': 'EP-B', 'coin': 'BTC', 'tn_ns': full.parse_time_ns('2025-10-21T23:01:00Z')},
        ]
        self.input = SimpleNamespace(episodes=self.episodes)
        self.patchers = [mock.patch.object(full, 'EPISODE_COUNT', 2),
                         mock.patch.object(full, 'ALLOWED_KEY_COUNT', 2),
                         mock.patch.object(full, 'load_csv1', return_value=self.input)]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary.cleanup()

    def validate(self):
        return full.validate_inputs(self.directory, self.plan, 'vpce-0123456789abcdef0')

    def test_full_wrapper_reuses_frozen_sample_transport_and_validates_exact_scope(self):
        self.assertIs(full.ApprovedS3Source, sample.ApprovedS3Source)
        self.assertIs(full.metadata_session, sample.metadata_session)
        self.assertIs(self.validate(), self.input)
        self.assertEqual(full.LIMIT_CAPS['data_runtime_seconds'], 18900)
        self.assertEqual(full.LIMIT_CAPS['source_bytes'], 8 * 1024**3)

    def test_unapproved_plan_is_rejected_before_any_aws_session(self):
        self.plan['approval_recorded'] = False
        with mock.patch.object(full, 'metadata_session') as metadata:
            with self.assertRaises(PermissionError):
                self.validate()
            metadata.assert_not_called()

    def test_expiry_cannot_extend_beyond_six_hours_from_approval(self):
        approved = datetime.fromisoformat(self.plan['approved_at_utc'])
        self.plan['expires_at_utc'] = (approved + timedelta(hours=6, seconds=1)).isoformat()
        with self.assertRaises(PermissionError):
            self.validate()

    def test_changed_byte_or_cache_limit_is_rejected(self):
        for name in ['source_bytes', 'cache_bytes']:
            with self.subTest(limit=name):
                saved = self.plan['limits'][name]
                self.plan['limits'][name] = saved + 1
                with self.assertRaises(PermissionError):
                    self.validate()
                self.plan['limits'][name] = saved

    def test_whitelist_cannot_replace_an_in_scope_object_with_an_unrelated_coin(self):
        self.plan['allowed_keys'][0] = 'market_data/20251021/22/l2Book/ETH.lz4'
        with self.assertRaisesRegex(PermissionError, 'omits an in-scope'):
            self.validate()

    def test_smaller_continuation_caps_are_accepted_with_boot_and_export_margins(self):
        self.plan['limits'].update(source_bytes=1024**3, source_requests=10000,
                                    output_bytes=100 * 1024**2, max_instance_minutes=120,
                                    instance_shutdown_minutes=105, data_runtime_seconds=90 * 60)
        self.assertIs(self.validate(), self.input)

    def test_instance_timer_cannot_consume_the_boot_margin_of_a_smaller_budget(self):
        self.plan['limits'].update(max_instance_minutes=120, instance_shutdown_minutes=106,
                                    data_runtime_seconds=90 * 60)
        with self.assertRaisesRegex(PermissionError, 'boot margin'):
            self.validate()

    def test_data_runtime_cannot_consume_the_export_margin_of_a_smaller_timer(self):
        self.plan['limits'].update(max_instance_minutes=120, instance_shutdown_minutes=105,
                                    data_runtime_seconds=90 * 60 + 1)
        with self.assertRaisesRegex(PermissionError, 'export and shutdown'):
            self.validate()

    def continuation_plan(self):
        self.plan['prior_usage'] = dict(source_bytes_charged=512 * 1024**2, source_requests=1000,
                                       instance_seconds=3600, output_bytes=20 * 1024**2)
        self.plan['remaining_instance_minutes'] = 300
        self.plan['prior_attempt_summary_sha256'] = 'a' * 64
        approved = datetime.fromisoformat(self.plan['approved_at_utc'])
        expiry = (approved + timedelta(hours=5)).isoformat()
        self.plan.update(original_expires_at_utc=expiry, expires_at_utc=expiry)
        self.plan['limits'].update(source_bytes=8 * 1024**3 - 512 * 1024**2,
                                    source_requests=312063 - 1000, output_bytes=232 * 1024**2,
                                    max_instance_minutes=300, instance_shutdown_minutes=285,
                                    data_runtime_seconds=270 * 60)

    def test_continuation_subtracts_prior_usage_and_accepts_the_remaining_task_allowance(self):
        self.continuation_plan()
        self.assertIs(self.validate(), self.input)

    def test_continuation_cannot_replenish_bytes_requests_minutes_or_export_budget(self):
        self.continuation_plan()
        for key in ['source_bytes', 'source_requests', 'max_instance_minutes', 'output_bytes']:
            with self.subTest(limit=key):
                before = self.plan['limits'][key]
                self.plan['limits'][key] = before + 1
                with self.assertRaisesRegex(PermissionError, 'remaining original task allowance'):
                    self.validate()
                self.plan['limits'][key] = before

    def test_continuation_cannot_refresh_the_original_absolute_source_expiry(self):
        self.continuation_plan()
        self.plan['expires_at_utc'] = (datetime.fromisoformat(self.plan['original_expires_at_utc'])
                                      + timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(PermissionError, 'original source-access expiry'):
            self.validate()

    def test_continuation_rejects_negative_predecessor_usage(self):
        self.continuation_plan()
        self.plan['prior_usage']['source_bytes_charged'] = -1
        with self.assertRaisesRegex(PermissionError, 'Invalid prior-attempt usage'):
            self.validate()


class FullExportTests(unittest.TestCase):
    def test_exports_only_six_durable_files_with_fresh_sts_and_owned_bucket(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for name in full.DURABLE:
                (path / name).write_bytes(b'result')
            (path / 'raw_snapshot.json').write_bytes(b'never export')
            sts, s3 = mock.Mock(), mock.Mock()
            sts.get_caller_identity.return_value = {
                'Account': '555066174629',
                'Arn': 'arn:aws:sts::555066174629:assumed-role/full-role/i-offline',
            }
            session = mock.Mock()
            session.client.side_effect = lambda service, **kwargs: {'sts': sts, 's3': s3}[service]
            full.export_results(session, 'full-role', 'owned-full-results', path,
                                {'limits': {'output_bytes': 256 * 1024**2}})
        self.assertEqual(sts.get_caller_identity.call_count, 6)
        self.assertEqual(s3.put_object.call_count, 6)
        keys = [call.kwargs['Key'] for call in s3.put_object.call_args_list]
        self.assertEqual(keys, ['output/full/' + name for name in full.DURABLE])
        self.assertTrue(all(call.kwargs['ExpectedBucketOwner'] == '555066174629'
                            and call.kwargs['ServerSideEncryption'] == 'AES256'
                            for call in s3.put_object.call_args_list))
        sts.close.assert_called_once()
        s3.close.assert_called_once()

    def test_export_size_cap_fails_before_creating_any_client(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'csv2.csv').write_bytes(b'too large')
            session = mock.Mock()
            with self.assertRaisesRegex(RuntimeError, 'export exceeds'):
                full.export_results(session, 'full-role', 'owned-full-results', path,
                                    {'limits': {'output_bytes': 1}})
            session.client.assert_not_called()


class FullPlanAndInterruptionTests(unittest.TestCase):
    def test_continuation_generator_debits_verified_usage_and_keeps_default_unapproved(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        launch = now - timedelta(seconds=121)
        terminated = now - timedelta(seconds=30, microseconds=900000)
        summary = {
            'verification_status': 'verified_full_incomplete_attempts',
            'account_id': '555066174629', 'region': 'us-east-1',
            'csv1_sha256': plan_generator.EXPECTED_CSV1_SHA256,
            'original_expires_at_utc': plan_generator.ORIGINAL_TASK_EXPIRY,
            'attempts': [{
                'instance_id': 'i-0123456789abcdef0', 'state': 'terminated',
                'launch_time_utc': launch.isoformat().replace('+00:00', 'Z'),
                'observed_terminated_utc': terminated.isoformat().replace('+00:00', 'Z'),
                'full_statistics': {'event': 'full_incomplete', 'account': '555066174629',
                                    'region': 'us-east-1', 'instance': 'i-0123456789abcdef0',
                                    'expected_episodes': 354, 'source_bytes_received': 4321,
                                    'source_bytes_reserved': 4567, 'source_requests': 123},
                'export_files': {name: 10 for name in plan_generator.EXPORT_NAMES},
            }],
        }
        source = Path(__file__).resolve().parent / 'results/csv1_full_20250322_20261031/csv1_20250322_20261031_zh.csv'
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'verified_predecessor.json'
            raw = json.dumps(summary, indent=2).encode()
            path.write_bytes(raw)
            plan = plan_generator.make_plan(source, prior_attempt_summary=path)
        self.assertFalse(plan['approval_recorded'])
        self.assertEqual(plan['expires_at_utc'], 'SET_AFTER_USER_APPROVAL')
        self.assertEqual(plan['prior_attempt_summary_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(plan['original_expires_at_utc'], '2026-11-06T04:55:00Z')
        self.assertEqual(plan['prior_usage'], {'source_bytes_charged': 4567, 'source_requests': 123,
                                             'instance_seconds': 91, 'output_bytes': 70})
        self.assertEqual(plan['remaining_instance_minutes'], 358)
        self.assertEqual(plan['limits']['source_bytes'], 8 * 1024**3 - 4567)
        self.assertEqual(plan['limits']['source_requests'], 312063 - 123)
        self.assertEqual(plan['limits']['output_bytes'], 252 * 1024**2 - 70)
        self.assertLessEqual(plan['limits']['instance_shutdown_minutes'], 358 - 15)
        self.assertLessEqual(plan['limits']['data_runtime_seconds'],
                             (plan['limits']['instance_shutdown_minutes'] - 15) * 60)

    def test_tracked_csv1_produces_a_bounded_unapproved_draft_plan(self):
        path = Path(__file__).resolve().parent / 'results/csv1_full_20250322_20261031/csv1_20250322_20261031_zh.csv'
        plan = plan_generator.make_plan(path)
        self.assertFalse(plan['approval_recorded'])
        self.assertEqual(plan['approved_at_utc'], 'SET_AFTER_USER_APPROVAL')
        self.assertEqual(plan['expires_at_utc'], 'SET_AFTER_USER_APPROVAL')
        self.assertEqual(plan['expected_episode_count'], 354)
        self.assertEqual(len(plan['allowed_keys']), 156031)
        self.assertEqual(len(set(plan['allowed_keys'])), 156031)
        self.assertEqual(plan['limits']['output_bytes'], 252 * 1024**2)
        self.assertEqual(plan['limits']['source_bytes'], 8 * 1024**3)
        self.assertEqual(plan['limits']['data_runtime_seconds'], 18900)
        self.assertEqual(plan['limits']['source_requests'], 312063)
        self.assertTrue(all(key.startswith('market_data/') and '/l2Book/' in key for key in plan['allowed_keys']))
        self.assertTrue(all('20251022' not in key for key in plan['allowed_keys']))

    def test_plan_generator_rejects_a_different_input_without_authorizing_anything(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'wrong.csv'
            path.write_text('unrelated input', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'authoritative input'):
                plan_generator.make_plan(path)

    def test_budget_interruption_keeps_committed_rows_and_incomplete_progress(self):
        from test_official_l2 import MemorySource, object_key, payload, read_result, source_rows, write_csv1
        import official_l2 as engine
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            csv1, run = directory / 'input.csv', directory / 'run'
            rows = source_rows('EP-FIRST') + source_rows('EP-LATER', times=('2025-05-01T12:00:00Z',))
            write_csv1(csv1, rows)
            source = MemorySource({
                object_key(10): [payload(1746093600000)],
                object_key(11): [payload(1746097200000, captured='2025-05-01T11:00:00Z')],
                object_key(12): engine.SourceAccessError('Approved request limit reached'),
            })
            arguments = dict(start='2025-05-01T10:00:00Z', end='2025-05-01T14:00:00Z',
                             policy='validated_next_hour')
            with self.assertRaisesRegex(engine.SourceAccessError, 'request limit reached'):
                engine.run_csv2(csv1, run, source, **arguments)
            result = read_result(run / 'csv2.csv')
            self.assertEqual([row['episode_id'] for row in result], ['EP-FIRST'])
            self.assertEqual(result[0]['status'], 'book_ready')
            with closing(sqlite3.connect(run / 'progress.sqlite')) as db:
                flag = db.execute("SELECT value FROM meta WHERE key='completed'").fetchone()
                self.assertTrue(flag is None or json.loads(flag[0]) is False)
            # This resumes the offline engine with the same fixture identity. It
            # neither creates a cloud session nor authorizes a second paid run.
            source.records[object_key(12)] = [payload(1746100800000, captured='2025-05-01T12:00:00Z')]
            source.records[object_key(13)] = [payload(1746104400000, captured='2025-05-01T13:00:00Z')]
            source.calls.clear()
            engine.run_csv2(csv1, run, source, **arguments)
            final = read_result(run / 'csv2.csv')
            self.assertEqual([row['episode_id'] for row in final], ['EP-FIRST', 'EP-LATER'])
            self.assertEqual(source.calls, [object_key(12), object_key(13)])
            with closing(sqlite3.connect(run / 'progress.sqlite')) as db:
                self.assertTrue(json.loads(db.execute("SELECT value FROM meta WHERE key='completed'").fetchone()[0]))


if __name__ == '__main__':
    unittest.main()
