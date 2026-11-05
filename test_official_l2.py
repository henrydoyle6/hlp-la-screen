"""Offline regression tests for the public L2 data-source contract."""

import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import official_l2 as l2


ORIGINAL_CSV2_FIELDS = [
    'schema_version', 'run_id', 'record_type', 'episode_id', 'status', 'reason_code',
    'reason_message', 'error_file', 'error_member', 'error_line', 'error_block', 'issues_json',
    'episode_input_hash', 'coin', 'direction', 'total_qty', 'entry_notional', 'entry_vwap',
    'partial_episode', 'partial_reason', 'split_before', 'split_after', 'tn', 'target_block',
    'book_state_point', 'book_side', 'snapshot_file', 'snapshot_member', 'snapshot_block',
    'snapshot_time', 'snapshot_source_id', 'snapshot_selection_status',
    'replay_from_block_exclusive', 'replay_to_block_inclusive', 'last_applied_block',
    'replay_coverage_mode', 'diff_sources_json', 'order_count', 'price_level_count',
    'total_depth', 'side_notional', 'best_price', 'worst_price', 'book_is_empty',
    'orders_json', 'book_hash',
]
COMMON = ORIGINAL_CSV2_FIELDS[:12]
FILL = ['event_id', 'event_time', 'block_number', 'coin', 'direction', 'price', 'qty',
        'source_file', 'source_member', 'source_line', 'source_event_index']
SUMMARY = ['coin', 'direction', 'segment_id', 'episode_start', 'episode_end', 'start_block',
           'end_block', 'event_count', 'formal_event_count', 'buffer_event_count', 'duration_ms',
           'total_qty', 'entry_notional', 'entry_vwap', 'partial_episode', 'partial_reason',
           'split_before', 'split_after']
RANGE = ['data_start', 'data_end_exclusive', 'formal_start_inclusive', 'formal_end_inclusive']
CSV1_FIELDS = COMMON + FILL + RANGE + ['is_formal_event'] + [k for k in SUMMARY if k not in FILL] + ['episode_input_hash']
CHINESE_CSV1_FIELDS = [
    '模式版本', '运行编号', '记录类型', '接管组编号', '处理状态', '原因代码', '原因说明', '错误文件',
    '错误文件内路径', '错误行号', '错误区块号', '问题明细JSON', '接管事件编号', '接管时间UTC', '区块号',
    '币种', '接管方向', '接管价格', '接管数量', '来源文件', '来源文件内路径', '来源行号', '来源事件序号',
    '数据开始时间UTC', '数据结束时间UTC（不含）', '正式开始时间UTC（含）', '正式结束时间UTC（含）',
    '是否正式范围接管', '连续数据段编号', '接管组开始时间UTC', '接管组结束时间UTC', '接管组首区块号',
    '接管组末区块号', '接管笔数', '正式范围接管笔数', '缓冲区接管笔数', '持续时间毫秒',
    '接管总数量', '接管总金额', '加权接管均价', '是否部分接管组', '部分接管组原因',
    '前侧分段依据', '后侧分段依据', '接管组输入哈希',
]
START = '2025-03-22T00:00:00Z'
END = '2025-10-22T00:00:00Z'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def source_rows(name='EP-A', times=('2025-05-01T10:00:00.000Z',), direction='long', coin='BTC', heights=None):
    """Produce complete valid CSV1 fills, independently of the new importer."""
    rows = []
    heights = heights if heights is not None else [''] * len(times)
    for number, timestamp in enumerate(times):
        row = dict.fromkeys(CSV1_FIELDS, '')
        row.update(schema_version='hlp-csv-v3', run_id='source-run', record_type='fill',
                   episode_id=name, status='grouped', issues_json='[]',
                   event_id=f'{name}-fill-{number}', event_time=timestamp, block_number=heights[number],
                   coin=coin, direction=direction, price='100', qty='1',
                   source_file='fixture.jsonl', source_line=str(number + 1), source_event_index='0',
                   data_start=START, data_end_exclusive='2026-11-01T00:00:00Z',
                   formal_start_inclusive='2025-03-22T01:00:00Z', formal_end_inclusive='2026-10-31T23:00:00Z',
                   is_formal_event='true', segment_id='1', episode_start=times[0], episode_end=times[-1],
                   start_block=heights[0], end_block=heights[-1], event_count=str(len(times)),
                   formal_event_count=str(len(times)), buffer_event_count='0',
                   duration_ms=str((l2.parse_time_ns(times[-1]) - l2.parse_time_ns(times[0])) // 1_000_000),
                   total_qty=str(len(times)), entry_notional=str(100 * len(times)), entry_vwap='100',
                   partial_episode='false')
        rows.append(row)
    payload = {'fills': [{k: row[k] for k in FILL} for row in rows],
               'summary': {k: rows[0][k] for k in SUMMARY}}
    fingerprint = hashlib.sha256(canonical(payload).encode()).hexdigest()
    for row in rows:
        row['episode_input_hash'] = fingerprint
    return rows


def write_csv1(path, rows, headers=None):
    headers = headers or CSV1_FIELDS
    with Path(path).open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows([[row[k] for k in CSV1_FIELDS] for row in rows])


def payload(timestamp, bids=None, asks=None, coin='BTC', captured='2025-05-01T10:00:10.999999999'):
    return {'time': captured, 'ver_num': 1, 'raw': {'channel': 'l2Book', 'data': {
        'coin': coin, 'time': timestamp, 'levels': [
            bids if bids is not None else [{'px': '100', 'sz': '2', 'n': 1}, {'px': '99', 'sz': '5', 'n': 3}],
            asks if asks is not None else [{'px': '101', 'sz': '3', 'n': 2}, {'px': '102', 'sz': '4', 'n': 5}],
        ]}}}


def episode(name='EP-A', tn='2025-05-01T10:00:00Z', direction='long'):
    row = source_rows(name, times=(tn,), direction=direction)[0]
    return {**{k: row[k] for k in SUMMARY}, 'episode_id': name,
            'episode_input_hash': row['episode_input_hash'], 'tn': tn,
            'tn_ns': l2.parse_time_ns(tn), 'target_block': row['block_number']}


def compressed(records):
    import lz4.frame
    return lz4.frame.compress(('\n'.join(canonical(record) for record in records) + '\n').encode())


class MemoryObject:
    def __init__(self, body, key):
        self.body = body
        self.identity = {'bucket': 'hyperliquid-archive', 'key': key,
                         'etag': hashlib.sha256(body).hexdigest(), 'content_length': len(body)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_chunks(self):
        for offset in range(0, len(self.body), 31):
            yield self.body[offset:offset + 31]


class MemorySource:
    def __init__(self, records):
        self.records = records
        self.calls = []
        self.fingerprint = 'memory:offline-fixtures-v1'

    def open(self, key):
        self.calls.append(key)
        value = self.records.get(key)
        if isinstance(value, BaseException):
            raise value
        if value is None:
            raise l2.ObjectNotFound(key)
        return MemoryObject(value if isinstance(value, bytes) else compressed(value), key)


def object_key(hour, coin='BTC', day='20250501'):
    return f'market_data/{day}/{hour}/l2Book/{coin}.lz4'


def read_result(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


class InputContractTests(unittest.TestCase):
    def test_csv2_preserves_exact_original_field_order_and_appends_only_lag(self):
        self.assertEqual(l2.CSV2_FIELDS, ORIGINAL_CSV2_FIELDS + ['snapshot_lag_ms'])

    def test_time_parser_preserves_nanoseconds(self):
        base = l2.parse_time_ns('2025-05-01T10:00:00Z')
        self.assertEqual(l2.parse_time_ns('2025-05-01T10:00:00.000000001Z'), base + 1)
        self.assertEqual(l2.parse_time_ns('2025-05-01T18:00:00.123456789+08:00'), base + 123_456_789)

    def test_english_csv1_unknown_blocks_preserves_entire_episode(self):
        rows = source_rows(times=('2025-05-01T09:40:00Z', '2025-05-01T10:00:00Z'))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.csv'
            write_csv1(path, rows)
            data = l2.load_csv1(path, l2.parse_time_ns(START), l2.parse_time_ns(END))
        self.assertEqual(len(data.episodes), 1)
        episode = data.episodes[0]
        self.assertEqual(episode['episode_id'], 'EP-A')
        self.assertEqual(episode['episode_input_hash'], rows[0]['episode_input_hash'])
        self.assertEqual(episode['total_qty'], '2')
        self.assertEqual(episode['entry_notional'], '200')
        self.assertEqual(episode['target_block'], '')
        self.assertEqual(episode['tn'], rows[-1]['event_time'])

    def test_source_numerical_contradiction_is_isolated_to_its_episode(self):
        rows = source_rows('EP-BAD')
        rows[0]['total_qty'] = '2'
        evidence = {'fills': [{k: row[k] for k in FILL} for row in rows],
                    'summary': {k: rows[0][k] for k in SUMMARY}}
        rows[0]['episode_input_hash'] = hashlib.sha256(canonical(evidence).encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.csv'
            write_csv1(path, rows + source_rows('EP-GOOD'))
            data = l2.load_csv1(path, l2.parse_time_ns(START), l2.parse_time_ns(END))
        by_id = {e['episode_id']: e for e in data.episodes}
        self.assertEqual(by_id['EP-BAD']['_input_problem']['code'], 'INCONSISTENT_CSV1_EPISODE')
        self.assertNotIn('_input_problem', by_id['EP-GOOD'])

    def test_chinese_header_mapping_preserves_unknown_height_episode(self):
        rows = source_rows(coin='JELLY', direction='short')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '来源.csv'
            write_csv1(path, rows, CHINESE_CSV1_FIELDS)
            data = l2.load_csv1(path, l2.parse_time_ns(START), l2.parse_time_ns(END))
        self.assertEqual(len(data.episodes), 1)
        self.assertEqual(data.episodes[0]['coin'], 'JELLY')
        self.assertEqual(data.episodes[0]['direction'], 'short')
        self.assertEqual(data.episodes[0]['episode_input_hash'], rows[0]['episode_input_hash'])
        self.assertEqual(data.episodes[0]['target_block'], '')

    def test_csv2_selection_uses_complete_episode_tn_and_includes_end_date(self):
        inside = source_rows('EP-LAST', times=('2025-10-21T23:20:00Z', '2025-10-21T23:50:00Z'))
        after = source_rows('EP-AFTER', times=('2025-10-21T23:50:00Z', '2025-10-22T00:10:00Z'))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.csv'
            write_csv1(path, inside + after)
            data = l2.load_csv1(path, l2.parse_time_ns(START), l2.parse_time_ns(END))
        self.assertEqual([e['episode_id'] for e in data.episodes], ['EP-LAST'])
        self.assertEqual(data.episodes[0]['tn'], '2025-10-21T23:50:00Z')
        self.assertEqual(data.episodes[0]['total_qty'], '2')

    def test_issue_row_does_not_create_an_episode(self):
        rows = source_rows()
        issue = dict.fromkeys(CSV1_FIELDS, '')
        issue.update(schema_version='hlp-csv-v3', run_id='source-run', record_type='issue',
                     status='input_issue', reason_code='MISSING_FILLS_FILE', issues_json='[]')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.csv'
            write_csv1(path, rows + [issue])
            data = l2.load_csv1(path, l2.parse_time_ns(START), l2.parse_time_ns(END))
        self.assertEqual([e['episode_id'] for e in data.episodes], ['EP-A'])
        self.assertEqual(len(data.issues), 1)


class SnapshotParsingTests(unittest.TestCase):
    def test_exchange_time_drives_snapshot_time_in_historical_wrapper(self):
        timestamp = 1746093600123
        snapshot = l2.parse_snapshot(payload(timestamp, captured='2025-05-01T10:00:12.999999999'))
        self.assertEqual(snapshot.time_ns, timestamp * 1_000_000)
        self.assertEqual(snapshot.coin, 'BTC')
        self.assertIn(snapshot.block, ('', None))

    def test_unwrapped_exchange_l2_record_is_supported(self):
        record = payload(1746093600123)['raw']['data']
        snapshot = l2.parse_snapshot(record)
        self.assertEqual(snapshot.time_ns, 1746093600123 * 1_000_000)
        self.assertEqual(snapshot.coin, 'BTC')

    def test_capture_timestamp_cannot_fill_missing_exchange_timestamp(self):
        record = payload(1746093600123)
        del record['raw']['data']['time']
        with self.assertRaises((l2.SelectionError, l2.InvalidInput)):
            l2.parse_snapshot(record)

    def test_real_source_height_is_retained_only_when_supplied(self):
        record = payload(1746093600123)['raw']['data']
        record['block_number'] = 123456
        snapshot = l2.parse_snapshot(record)
        self.assertEqual(str(snapshot.block), '123456')


class SelectionTests(unittest.TestCase):
    def select(self, records, targets=None):
        return l2.select_snapshots(records, 'BTC', targets or [episode()],
                                   l2.parse_time_ns(START), l2.parse_time_ns(END))

    def test_exact_timestamp_is_selected(self):
        target = episode()
        before = payload(1746093599999)
        exact = payload(1746093600000)
        later = payload(1746093600100)
        result = self.select([before, exact, later], [target])
        self.assertEqual(result['EP-A'].snapshot.time_ns, target['tn_ns'])

    def test_nanosecond_tn_rejects_snapshot_one_ns_too_early(self):
        target = episode(tn='2025-05-01T10:00:00.000000001Z')
        result = self.select([payload(1746093600000), payload(1746093600001)], [target])
        row = l2.result_row(target, result['EP-A'])
        self.assertEqual(result['EP-A'].snapshot.time_ns, 1746093600001 * 1_000_000)
        self.assertEqual(row['snapshot_lag_ms'], '0.999999')

    def test_unordered_object_chooses_true_earliest_qualifying_record(self):
        result = self.select([payload(1746093601000), payload(1746093600001), payload(1746093600100)])
        self.assertEqual(result['EP-A'].snapshot.time_ns, 1746093600001 * 1_000_000)

    def test_same_timestamp_conflict_is_reported_without_arbitrary_choice(self):
        first = payload(1746093600000)
        conflict = payload(1746093600000, bids=[{'px': '100', 'sz': '900', 'n': 1}])
        candidate = self.select([first, conflict])['EP-A']
        row = l2.result_row(episode(), candidate)
        self.assertEqual(row['status'], 'skipped')
        self.assertEqual(row['reason_code'], 'CONFLICTING_L2_AT_SAME_TIME')

    def test_exact_duplicate_records_do_not_create_conflict(self):
        record = payload(1746093600000)
        candidate = self.select([record, json.loads(json.dumps(record))])['EP-A']
        self.assertFalse(candidate.conflict)

    def test_conflict_after_the_selected_timestamp_does_not_invalidate_selection(self):
        first = payload(1746093600000)
        later = payload(1746093600001)
        later_conflict = payload(1746093600001, asks=[])
        candidate = self.select([later, later_conflict, first])['EP-A']
        self.assertFalse(candidate.conflict)
        self.assertEqual(candidate.snapshot.time_ns, 1746093600000 * 1_000_000)

    def test_empty_selected_side_stays_selected(self):
        candidate = self.select([payload(1746093600000, bids=[]), payload(1746093600001)])['EP-A']
        row = l2.result_row(episode(), candidate)
        self.assertEqual(row['status'], 'book_ready')
        self.assertTrue(row['book_is_empty'])
        self.assertEqual(json.loads(row['orders_json']), [])
        self.assertEqual(row['total_depth'], '0')

    def test_end_bound_is_exclusive_even_when_no_other_snapshot_available(self):
        end_ns = l2.parse_time_ns(END)
        target = episode(tn='2025-10-21T23:59:59.999999999Z')
        result = self.select([payload(end_ns // 1_000_000)], [target])
        self.assertNotIn('EP-A', result)

    def test_long_and_short_extract_their_own_side_of_same_snapshot(self):
        snapshot = l2.parse_snapshot(payload(1746093600000))
        bid = l2.book_metrics(snapshot, 'long')
        ask = l2.book_metrics(snapshot, 'short')
        self.assertEqual((bid['total_depth'], bid['side_notional'], bid['order_count']), ('7', '695', 4))
        self.assertEqual((ask['total_depth'], ask['side_notional'], ask['order_count']), ('7', '711', 7))
        self.assertEqual((bid['best_price'], bid['worst_price']), ('100', '99'))
        self.assertEqual((ask['best_price'], ask['worst_price']), ('101', '102'))
        self.assertTrue(all('oid' not in level for level in json.loads(bid['orders_json'])))

    def test_any_missing_order_count_leaves_order_count_unknown(self):
        record = payload(1746093600000, bids=[{'px': '100', 'sz': '2', 'n': 1}, {'px': '99', 'sz': '5'}])
        metrics = l2.book_metrics(l2.parse_snapshot(record), 'long')
        self.assertEqual(metrics['order_count'], '')
        self.assertEqual(metrics['total_depth'], '7')

    def test_decimal_metrics_preserve_small_exact_depth_and_notional(self):
        record = payload(1746093600000, bids=[{'px': '0.3', 'sz': '0.1', 'n': 9},
                                              {'px': '0.2', 'sz': '0.2', 'n': 8}])
        metrics = l2.book_metrics(l2.parse_snapshot(record), 'long')
        self.assertEqual(metrics['total_depth'], '0.3')
        self.assertEqual(metrics['side_notional'], '0.07')
        self.assertEqual(metrics['order_count'], 17)

    def test_l2_output_has_blank_replay_fields_and_unknown_height(self):
        candidate = self.select([payload(1746093600000)])['EP-A']
        candidate.key = 's3://hyperliquid-archive/market_data/20250501/10/l2Book/BTC.lz4'
        row = l2.result_row(episode(), candidate)
        for field in ['target_block', 'snapshot_block', 'snapshot_member', 'replay_from_block_exclusive',
                      'replay_to_block_inclusive', 'last_applied_block', 'replay_coverage_mode', 'diff_sources_json']:
            self.assertEqual(row[field], '', field)
        self.assertEqual(row['book_state_point'], 'post_takeover_l2_snapshot')
        self.assertEqual(row['snapshot_selection_status'], 'nearest_at_or_after_tn')


class StreamingTests(unittest.TestCase):
    def test_stream_reads_to_end_and_handles_one_byte_chunks(self):
        raw = compressed([payload(1746093601000), payload(1746093600001)])
        consumed = []
        def chunks():
            for byte in raw:
                consumed.append(byte)
                yield bytes([byte])
        result = l2.scan_object(chunks(), 'BTC', [episode()], 'test.lz4', {'etag': 'fixture'})
        self.assertEqual(len(consumed), len(raw))
        self.assertTrue(result.stats['complete'])
        self.assertFalse(result.stats['time_ordered'])
        self.assertEqual(result.candidates['EP-A'].snapshot.time_ns, 1746093600001 * 1_000_000)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_path = self.root / 'source.csv'
        self.run_dir = self.root / 'run'

    def tearDown(self):
        self.temporary.cleanup()

    def run_case(self, source, rows=None, end='2025-05-01T13:00:00Z'):
        if rows is not None:
            write_csv1(self.source_path, rows)
        return l2.run_csv2(self.source_path, self.run_dir, source,
                           start='2025-05-01T10:00:00Z', end=end, policy='validated_next_hour')

    def test_next_hour_spillover_replaces_first_hours_later_candidate(self):
        source = MemorySource({
            object_key(10): [payload(1746093600200)],
            object_key(11): [payload(1746093600100, captured='2025-05-01T11:00:00Z')],
            object_key(12): [payload(1746100800000, captured='2025-05-01T12:00:00Z')],
        })
        self.run_case(source, source_rows())
        result = read_result(self.run_dir / 'csv2.csv')
        self.assertEqual(result[0]['status'], 'book_ready')
        self.assertEqual(result[0]['snapshot_lag_ms'], '100')
        self.assertEqual(result[0]['snapshot_file'], 's3://hyperliquid-archive/' + object_key(11))
        self.assertIn(object_key(11), source.calls)

    def test_cross_object_same_time_conflict_is_skipped(self):
        source = MemorySource({
            object_key(10): [payload(1746093600000)],
            object_key(11): [payload(1746093600000, bids=[], captured='2025-05-01T11:00:00Z')],
            object_key(12): [payload(1746100800000, captured='2025-05-01T12:00:00Z')],
        })
        self.run_case(source, source_rows())
        result = read_result(self.run_dir / 'csv2.csv')
        self.assertEqual(result[0]['reason_code'], 'CONFLICTING_L2_AT_SAME_TIME')

    def test_same_hour_long_short_targets_share_each_object_read(self):
        rows = source_rows('EP-LONG') + source_rows('EP-SHORT', direction='short')
        source = MemorySource({
            object_key(10): [payload(1746093600000)],
            object_key(11): [payload(1746097200000, captured='2025-05-01T11:00:00Z')],
        })
        self.run_case(source, rows)
        result = read_result(self.run_dir / 'csv2.csv')
        self.assertEqual(len(result), 2)
        self.assertEqual({r['book_side'] for r in result}, {'bid', 'ask'})
        self.assertEqual(source.calls, [object_key(10), object_key(11)])

    def test_verified_absent_current_hour_continues_to_later_available_snapshot(self):
        source = MemorySource({
            object_key(11): [payload(1746097200000, captured='2025-05-01T11:00:00Z')],
            object_key(12): [payload(1746100800000, captured='2025-05-01T12:00:00Z')],
        })
        self.run_case(source, source_rows())
        result = read_result(self.run_dir / 'csv2.csv')
        self.assertEqual(result[0]['status'], 'book_ready')
        self.assertEqual(result[0]['snapshot_lag_ms'], '3600000')
        self.assertIn('MISSING_L2_OBJECT', result[0]['issues_json'])

    def test_permission_failure_stops_source_without_searching_later_hours(self):
        source = MemorySource({object_key(10): l2.SourceAccessError('403 AccessDenied')})
        with self.assertRaises(l2.SourceAccessError):
            self.run_case(source, source_rows())
        self.assertEqual(source.calls, [object_key(10)])
        self.assertEqual(read_result(self.run_dir / 'csv2.csv'), [])

    def test_corrupt_needed_object_cannot_be_rescued_by_a_later_good_book(self):
        source = MemorySource({object_key(10): compressed([payload(1746093600000)])[:-3],
                               object_key(11): [payload(1746097200000, captured='2025-05-01T11:00:00Z')]})
        self.run_case(source, source_rows())
        result = read_result(self.run_dir / 'csv2.csv')
        self.assertEqual(result[0]['status'], 'skipped')
        self.assertEqual(result[0]['reason_code'], 'L2_SELECTION_UNCONFIRMED')

    def test_gap_between_demanded_hours_is_not_scanned(self):
        rows = source_rows('EP-FIRST') + source_rows('EP-LATER', times=('2025-05-03T10:00:00Z',))
        source = MemorySource({
            object_key(10): [payload(1746093600000)],
            object_key(11): [payload(1746097200000, captured='2025-05-01T11:00:00Z')],
            object_key(10, day='20250503'): [payload(1746266400000, captured='2025-05-03T10:00:00Z')],
            object_key(11, day='20250503'): [payload(1746270000000, captured='2025-05-03T11:00:00Z')],
        })
        self.run_case(source, rows, end='2025-05-03T12:00:00Z')
        self.assertEqual(source.calls, [object_key(10), object_key(11),
                                        object_key(10, day='20250503'), object_key(11, day='20250503')])
        self.assertEqual(len(read_result(self.run_dir / 'csv2.csv')), 2)

    def test_corruption_before_future_demand_does_not_skip_future_episode(self):
        rows = source_rows('EP-FIRST') + source_rows('EP-FUTURE', times=('2025-05-01T12:00:00Z',))
        source = MemorySource({
            object_key(10): compressed([payload(1746093600000)])[:-3],
            object_key(12): [payload(1746100800000, captured='2025-05-01T12:00:00Z')],
            object_key(13): [payload(1746104400000, captured='2025-05-01T13:00:00Z')],
        })
        self.run_case(source, rows, end='2025-05-01T14:00:00Z')
        result = {r['episode_id']: r for r in read_result(self.run_dir / 'csv2.csv')}
        self.assertEqual(result['EP-FIRST']['status'], 'skipped')
        self.assertEqual(result['EP-FUTURE']['status'], 'book_ready')

    def test_resume_repairs_half_row_after_fsync_without_repeating_network_read(self):
        source = MemorySource({
            object_key(10): [payload(1746093600000)],
            object_key(11): [payload(1746097200000, captured='2025-05-01T11:00:00Z')],
        })
        def fail(point, **context):
            if point == 'csv2_fsynced_before_progress':
                raise RuntimeError('simulated process interruption')
        with mock.patch.object(l2, '_fault', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'simulated process interruption'):
                self.run_case(source, source_rows())
        calls_before_resume = list(source.calls)
        with (self.run_dir / 'csv2.csv').open('ab') as handle:
            handle.write(b'partial,csv,record')
        self.run_case(source)
        self.assertEqual(source.calls, calls_before_resume)
        self.assertEqual(len(read_result(self.run_dir / 'csv2.csv')), 1)
        self.assertNotIn(b'partial,csv,record', (self.run_dir / 'csv2.csv').read_bytes())

    def test_resume_keeps_committed_row_and_does_not_reopen_source(self):
        source = MemorySource({
            object_key(10): [payload(1746093600000)],
            object_key(11): [payload(1746097200000, captured='2025-05-01T11:00:00Z')],
        })
        self.run_case(source, source_rows())
        before = (self.run_dir / 'csv2.csv').read_bytes()
        source.calls.clear()
        self.run_case(source)
        self.assertEqual(source.calls, [])
        self.assertEqual((self.run_dir / 'csv2.csv').read_bytes(), before)
        self.assertEqual({p.name for p in self.run_dir.iterdir()},
                         {'csv1.csv', 'csv2.csv', 'csv3.csv', 'run.log', 'progress.sqlite', 'cache.sqlite'})

    def test_resume_rejects_changed_imported_csv1(self):
        source = MemorySource({
            object_key(10): [payload(1746093600000)],
            object_key(11): [payload(1746097200000, captured='2025-05-01T11:00:00Z')],
        })
        self.run_case(source, source_rows())
        with (self.run_dir / 'csv1.csv').open('ab') as handle:
            handle.write(b'changed')
        source.calls.clear()
        with self.assertRaises(l2.InvalidInput):
            self.run_case(source)
        self.assertEqual(source.calls, [])

    def test_resume_rejects_modified_csv2_header(self):
        source = MemorySource({
            object_key(10): [payload(1746093600000)],
            object_key(11): [payload(1746097200000, captured='2025-05-01T11:00:00Z')],
        })
        self.run_case(source, source_rows())
        with (self.run_dir / 'csv2.csv').open('r+b') as handle:
            handle.write(b'X')
        source.calls.clear()
        with self.assertRaises(l2.InvalidInput):
            self.run_case(source)
        self.assertEqual(source.calls, [])

    def test_truncated_frame_cannot_yield_confirmed_candidate(self):
        raw = compressed([payload(1746093600000)])[:-4]
        result = l2.scan_object([raw], 'BTC', [episode()], 'truncated.lz4', {})
        self.assertFalse(result.stats['complete'])
        self.assertEqual(result.candidates, {})
        self.assertEqual(result.problems[0]['code'], 'CORRUPT_L2_OBJECT')

    def test_trailing_invalid_json_invalidates_earlier_candidate(self):
        import lz4.frame
        raw = lz4.frame.compress((canonical(payload(1746093600000)) + '\n{"raw":').encode())
        result = l2.scan_object([raw], 'BTC', [episode()], 'corrupt.lz4', {})
        self.assertEqual(result.candidates, {})
        self.assertTrue(result.problems)

    def test_wrong_coin_is_not_silently_assigned_to_requested_coin(self):
        result = l2.scan_object([compressed([payload(1746093600000, coin='ETH')])],
                                'BTC', [episode()], 'BTC.lz4', {})
        self.assertEqual(result.candidates, {})
        self.assertEqual(result.problems[0]['code'], 'L2_COIN_MISMATCH')

    def test_concatenated_lz4_frames_are_fully_scanned(self):
        raw = compressed([payload(1746093601000)]) + compressed([payload(1746093600001)])
        result = l2.scan_object([raw], 'BTC', [episode()], 'two-frames.lz4', {})
        self.assertTrue(result.stats['complete'])
        self.assertEqual(result.stats['records'], 2)
        self.assertEqual(result.candidates['EP-A'].snapshot.time_ns, 1746093600001 * 1_000_000)


if __name__ == '__main__':
    unittest.main()
