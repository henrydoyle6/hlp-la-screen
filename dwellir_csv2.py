"""Zero-cost Dwellir acquisition gates and local native-L4 CSV2 preparation.

No AWS SDK, credentials chain, account registration, or paid service activation.
The original run_hlp.py and exact replay/comparison engine remain authoritative.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import csv_pipeline as pipe
import hlp_la_screen as engine
from official_l2 import CHINESE_CSV1
from state_store import digest, CheckpointError

VERSION = 'dwellir-csv2-v1'
SOURCE_SHA256 = '89bbee26d9ebad3f43d759d8406785bd049062d460365f6aab814e158f4d0022'
START = '2026-05-28T00:00:00Z'
END = '2026-11-01T00:00:00Z'
SAMPLES = [('june', ['EP000411']), ('aug19', ['EP000414', 'EP000415']),
           ('aug22', ['EP000418', 'EP000420']), ('october', ['EP000443'])]
PREFIXES = ['periodic_abci_states', 'node_raw_book_diffs_by_block']
SAMPLE_LIMIT = 16 * 1024**3
SIX_FILES = {'csv1.csv', 'csv2.csv', 'csv3.csv', 'run.log', 'progress.sqlite', 'cache.sqlite'}
csv.field_size_limit(2**31 - 1)


class InvalidInput(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise InvalidInput(message)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024**2), b''):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text('utf-8-sig'))


def write_bytes(path, data):
    """Idempotent immutable artifacts; never replace a different prior result."""
    path = Path(path)
    if path.exists():
        require(path.read_bytes() == data, 'Existing artifact differs; use a new output path')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def write_json(path, value):
    write_bytes(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode())


def csv_bytes(fields, rows):
    buf = io.StringIO(newline='')
    writer = csv.DictWriter(buf, fields, lineterminator='\r\n')
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode('utf-8-sig')


def native_rows(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f, strict=True)
        require(next(reader, None) in (pipe.CSV1_FIELDS, CHINESE_CSV1), 'Unsupported CSV1 header')
        result = []
        for values in reader:
            require(len(values) == len(pipe.CSV1_FIELDS), 'Malformed CSV1 row')
            result.append(dict(zip(pipe.CSV1_FIELDS, values)))
    return result


def validated_episodes(rows):
    first = next((r for r in rows if r['record_type'] == 'fill'), None)
    require(first is not None, 'No selected fills')
    config = {k: first[k] for k in pipe.RANGE + ['run_id']}
    return pipe.validate_csv1_rows(config, rows), config


def prepare(source, output, expected_sha=SOURCE_SHA256):
    require(sha(source) == expected_sha, 'CSV1 SHA256 mismatch')
    all_rows = native_rows(source)
    selected_ids = {r['episode_id'] for r in all_rows if r['record_type'] == 'fill'
                    and engine.parse_time(START) <= engine.parse_time(r['episode_end']) < engine.parse_time(END)}
    rows = [r for r in all_rows if r['record_type'] == 'issue' or r['episode_id'] in selected_ids]
    episodes, config = validated_episodes(rows)
    sample_ids = {e for _, ids in SAMPLES for e in ids}
    require(sample_ids <= selected_ids, 'Sample episode missing')
    if expected_sha == SOURCE_SHA256:
        require(selected_ids == {f'EP{i:06d}' for i in range(411, 444)}, 'Unexpected target episode set')
        require(sum(r['record_type'] == 'fill' for r in rows) == 965, 'Unexpected target fill count')
    sample = [r for r in rows if r['record_type'] == 'issue' or r['episode_id'] in sample_ids]
    selected_data = csv_bytes(pipe.CSV1_FIELDS, rows)
    sample_data = csv_bytes(pipe.CSV1_FIELDS, sample)
    plan = dict(version=VERSION, source_sha256=expected_sha, start=START, end_exclusive=END,
                prepared_metadata=config, selected_csv1_sha256=hashlib.sha256(selected_data).hexdigest(),
                sample_csv1_sha256=hashlib.sha256(sample_data).hexdigest(), episodes=episodes,
                samples=[dict(id=k, episode_ids=v) for k, v in SAMPLES],
                counts=dict(episodes=len(episodes), fills=sum(r['record_type']=='fill' for r in rows),
                            issues=sum(r['record_type']=='issue' for r in rows),
                            coins=len({e['coin'] for e in episodes}),
                            target_hours=len({e['tn'][:13] for e in episodes})),
                cost_usd=0, sample_limits=dict(abci_objects=8, diff_objects=12, compressed_bytes=SAMPLE_LIMIT))
    plan['plan_hash'] = digest(plan)
    out = Path(output)
    write_bytes(out/'csv1_selected.csv', selected_data)
    write_bytes(out/'csv1_sample.csv', sample_data)
    write_json(out/'tasks.json', plan)
    return plan


def load_plan(path):
    plan = read_json(path)
    value = dict(plan)
    signature = value.pop('plan_hash')
    require(digest(value) == signature and plan['version'] == VERSION, 'Task plan changed/version mismatch')
    for name, key in [('csv1_selected.csv', 'selected_csv1_sha256'), ('csv1_sample.csv', 'sample_csv1_sha256')]:
        require(sha(Path(path).parent/name) == plan[key], 'Prepared CSV1 changed')
    return plan


def assess_access(evidence=None):
    """Record operator observations, never infer entitlement from RPC credits."""
    status = 'awaiting_registration'
    if evidence:
        if evidence.get('registered') is not True:
            status = 'awaiting_registration'
        elif evidence.get('email_verified') is not True:
            status = 'awaiting_email_verification'
        elif evidence.get('free_archive_access') is not True or evidence.get('cost_usd') != 0:
            status = 'blocked_free_archive_permission'
        elif evidence.get('transport') == 's3':
            status = 'blocked_s3_transport'
        elif (evidence.get('transport') != 'dwellir_https' or
              not set(PREFIXES) <= set(evidence.get('prefixes', [])) or
              not evidence.get('observation_reference')):
            status = 'blocked_incomplete_access_evidence'
        else:
            status = 'operator_confirmed_free_archive_access'
    return dict(version=VERSION, status=status, budget_usd=0, aws_requests=0,
                evidence_kind='operator_observation' if evidence else 'no_account_evidence',
                real_archive_sample_verified=False,
                documentation='https://www.dwellir.com/docs/hyperliquid/historical-data')


def allowed_url(url):
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname or ''
        valid = (p.scheme == 'https' and (host == 'dwellir.com' or host.endswith('.dwellir.com'))
                 and p.username is None and p.password is None and p.port in (None, 443)
                 and not p.fragment)
    except ValueError:
        valid = False
    require(valid, 'Only direct Dwellir HTTPS delivery is enabled; AWS/S3 and other hosts are blocked')


def object_path(root, key):
    require(isinstance(key, str) and re.fullmatch(
        r'(periodic_abci_states/\d{8}/\d+\.rmp\.lz4|node_raw_book_diffs_by_block/\d{8}/\d{1,2}\.lz4)', key),
        'Invalid archive key')
    parts = PurePosixPath(key).parts
    datetime.strptime(parts[1], '%Y%m%d')
    if parts[0] == PREFIXES[1]:
        require(0 <= int(parts[2].split('.')[0]) <= 23, 'Invalid hour')
    path = Path(root).joinpath(*parts).resolve()
    require(Path(root).resolve() in path.parents, 'Archive key escapes destination')
    return path


def load_manifest(plan_path, manifest_path):
    plan, manifest = load_plan(plan_path), read_json(manifest_path)
    require(manifest.get('plan_hash') == plan['plan_hash'], 'Manifest/task plan mismatch')
    stage = manifest.get('stage')
    require(stage in ('sample', 'full'), 'Manifest stage must be sample or full')
    require(manifest.get('cost_usd') == 0, 'Zero cost is required')
    require(manifest.get('data_kind') in ('provider_archive', 'synthetic'), 'Declare data provenance')
    objects = manifest.get('objects', [])
    require(bool(objects), 'No objects listed')
    keys = set()
    for obj in objects:
        key = obj['key']
        object_path(Path(manifest_path).parent/'unused', key)
        require(key not in keys, 'Duplicate archive object')
        keys.add(key)
        require(type(obj.get('size_bytes')) is int and obj['size_bytes'] > 0, 'Object size required')
        require(re.fullmatch('[0-9a-f]{64}', obj.get('sha256', '')) is not None, 'Object SHA256 required')
        if obj.get('url'):
            allowed_url(obj['url'])
        day = key.split('/')[1]
        require(plan['prepared_metadata']['data_start'][:10].replace('-', '') <= day < END[:10].replace('-', ''),
                'Object outside prepared dates')
    if stage == 'sample':
        require(sum(k.startswith(PREFIXES[0]+'/') for k in keys) <= 8, 'Sample ABCI limit exceeded')
        require(sum(k.startswith(PREFIXES[1]+'/') for k in keys) <= 12, 'Sample diff limit exceeded')
        require(sum(o['size_bytes'] for o in objects) <= SAMPLE_LIMIT, 'Sample byte limit exceeded')
    expected_ids = ({e for _, ids in SAMPLES for e in ids} if stage == 'sample'
                    else {e['episode_id'] for e in plan['episodes']})
    seen, used = [], set()
    windows = manifest.get('windows', [])
    if stage == 'sample':
        require({w['id']:w['episode_ids'] for w in windows} == dict(SAMPLES), 'Sample windows changed')
    for window in windows:
        seen.extend(window['episode_ids'])
        require(window['before_key'].startswith(PREFIXES[0]+'/'), 'ABCI before seed required')
        used.add(window['before_key'])
        if stage == 'sample':
            require(window.get('after_key') and window['after_key'] != window['before_key'], 'Distinct ABCI answer required')
            require(type(window.get('after_block')) is int and window['after_block'] > 0, 'Declared answer height required')
        if window.get('after_key'):
            require(window['after_key'].startswith(PREFIXES[0]+'/'), 'ABCI answer required')
            used.add(window['after_key'])
        require(bool(window['diff_keys']) and len(set(window['diff_keys'])) == len(window['diff_keys']), 'Unique diff objects required')
        require(all(k.startswith(PREFIXES[1]+'/') for k in window['diff_keys']), 'Invalid diff object type')
        used.update(window['diff_keys'])
    require(set(seen) == expected_ids and len(seen) == len(expected_ids), 'Episode windows omit or duplicate targets')
    require(used == keys, 'Missing or unused objects in manifest')
    return plan, manifest


def checked_files(manifest, root, defer_answers=False):
    files = {}
    answers = {w.get('after_key') for w in manifest['windows']} if defer_answers else set()
    for obj in manifest['objects']:
        path = object_path(root, obj['key'])
        if obj['key'] not in answers:
            require(path.is_file() and path.stat().st_size == obj['size_bytes'] and sha(path) == obj['sha256'],
                    'Missing or altered archive object: '+obj['key'])
        files[obj['key']] = path
    return files


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise InvalidInput('Redirect refused; direct free Dwellir delivery required')


def fetch(plan_path, manifest_path, raw_root, evidence_path, sample_receipt=None):
    plan, manifest = load_manifest(plan_path, manifest_path)
    access = assess_access(read_json(evidence_path))
    require(access['status'] == 'operator_confirmed_free_archive_access', access['status'])
    require(manifest['data_kind'] == 'provider_archive', 'Network retrieval requires provider archive provenance')
    if manifest['stage'] == 'full':
        sample_gate(plan_path, sample_receipt, raw_root)
    # Validate every URL and bound before any network request.
    for obj in manifest['objects']:
        allowed_url(obj.get('url', ''))
    opener = urllib.request.build_opener(NoRedirect())
    transferred = reused = 0
    for obj in manifest['objects']:
        path = object_path(raw_root, obj['key'])
        if path.exists():
            require(path.stat().st_size == obj['size_bytes'] and sha(path) == obj['sha256'], 'Cached object changed')
            reused += 1
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name+'.part')
        h, count = hashlib.sha256(), 0
        try:
            with opener.open(obj['url'], timeout=30) as response, partial.open('wb') as output:
                allowed_url(response.geturl())
                require(response.status == 200, 'Expected HTTP 200')
                length = response.headers.get('Content-Length')
                require(length is not None and int(length) == obj['size_bytes'], 'Download length differs from manifest')
                for chunk in iter(lambda: response.read(1024**2), b''):
                    count += len(chunk)
                    require(count <= obj['size_bytes'], 'Object byte limit exceeded')
                    h.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            require(count == obj['size_bytes'] and h.hexdigest() == obj['sha256'], 'Downloaded checksum mismatch')
            os.replace(partial, path)
            transferred += count
        except (urllib.error.URLError, OSError) as exc:
            # Exceptions may contain signed URLs/API keys. Never echo them.
            raise InvalidInput('Download failed ('+type(exc).__name__+'); complete objects retained') from None
    return dict(status='downloaded', objects=len(manifest['objects']), reused=reused,
                transferred_bytes=transferred, aws_requests=0, cost_usd=0)


def before_headers(plan, manifest, files):
    from input_discovery import rmp_header
    episodes = {e['episode_id']:e for e in plan['episodes']}
    headers = {}
    for window in manifest['windows']:
        key = window['before_key']
        if key not in headers:
            headers[key] = rmp_header(files[key])
        h = headers[key]
        for name in window['episode_ids']:
            ep = episodes[name]
            require(h['block_number'] < ep['target_block'] and h['time_ms'] < ep['tn_ms'], 'Seed is not strictly prior')
            if manifest['stage'] == 'sample':
                require(window['after_block'] > ep['target_block'], 'Answer must follow all window targets')
    refs = [engine.SnapshotRef(str(files[k]), h['block_number'], h['time_ms']) for k,h in headers.items()]
    for window in manifest['windows']:
        for name in window['episode_ids']:
            ep = episodes[name]
            chosen = engine.select_snapshot(refs, ep['tn_ms'], ep['target_block'])
            require(chosen.path == str(files[window['before_key']]), 'Window seed differs from native selection')
    return headers


def engine_signature():
    import run_hlp
    return digest(dict(native=run_hlp.engine_hashes(), adapter=sha(__file__),
                       comparison=sha(Path(__file__).with_name('test_order_book_replay.py'))))


def verify_sample(plan_path, manifest_path, raw_root, output):
    from test_order_book_replay import compare_multiple_snapshots
    plan, manifest = load_manifest(plan_path, manifest_path)
    require(manifest['stage'] == 'sample', 'Sample manifest required')
    files = checked_files(manifest, raw_root, defer_answers=True)
    headers = before_headers(plan, manifest, files)
    episodes = {e['episode_id']:e for e in plan['episodes']}
    results = []
    for window in manifest['windows']:
        h = headers[window['before_key']]
        # The original comparator reads the answer header/content only AFTER replay.
        result = compare_multiple_snapshots(before_state=files[window['before_key']],
            after_state=files[window['after_key']], before_block=h['block_number'],
            before_time=engine.time_text(h['time_ms']), target_block=window['after_block'],
            coins=sorted({episodes[n]['coin'] for n in window['episode_ids']}),
            diffs=[files[k] for k in window['diff_keys']])
        results.append(dict(id=window['id'], status=result['status'],
                            results=result['results'], expected_state_used_only_after_replay=True))
    checked_files(manifest, raw_root)
    report = dict(version=VERSION, plan_hash=plan['plan_hash'], manifest_sha256=sha(manifest_path),
                  manifest_path=str(Path(manifest_path).resolve()), engine_signature=engine_signature(),
                  data_kind=manifest['data_kind'], status='PASS' if all(r['status']=='PASS' for r in results) else 'FAIL',
                  windows=results, real_archive_sample_verified=manifest['data_kind']=='provider_archive', cost_usd=0)
    # A mismatch is evidence, never a passing real-data receipt.
    report['real_archive_sample_verified'] &= report['status'] == 'PASS'
    write_json(output, report)
    require(report['status'] == 'PASS', 'Exact sample comparison failed; report saved')
    return report


def sample_gate(plan_path, receipt_path, raw_root):
    require(receipt_path is not None, 'Passing real sample receipt required before full acquisition/run')
    plan = load_plan(plan_path)
    receipt = read_json(receipt_path)
    require(receipt.get('version') == VERSION and receipt.get('plan_hash') == plan['plan_hash']
            and receipt.get('status') == 'PASS' and receipt.get('real_archive_sample_verified') is True
            and receipt.get('engine_signature') == engine_signature(), 'Missing/incompatible real sample acceptance')
    require(sha(receipt['manifest_path']) == receipt['manifest_sha256'], 'Sample manifest changed')
    _, manifest = load_manifest(plan_path, receipt['manifest_path'])
    require(manifest['stage'] == 'sample' and manifest['data_kind'] == 'provider_archive', 'Not a real sample manifest')
    checked_files(manifest, raw_root)


def run_native(plan_path, manifest_path, raw_root, output_root, sample_receipt=None):
    import run_hlp
    require(sys.version_info[:2] >= (3, 12), 'Native production requires Python 3.12+')
    plan, manifest = load_manifest(plan_path, manifest_path)
    if manifest['stage'] == 'full':
        sample_gate(plan_path, sample_receipt, raw_root)
    files = checked_files(manifest, raw_root)
    before_headers(plan, manifest, files)
    # Exclude every answer snapshot from production discovery, including old answers.
    route = Path(raw_root)/'routes'/sha(manifest_path)
    before = {w['before_key'] for w in manifest['windows']}
    diff = {k for w in manifest['windows'] for k in w['diff_keys']}
    for key in sorted(before | diff):
        link = object_path(route, key)
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            os.link(files[key], link)
        require(sha(link) == sha(files[key]), 'Routed source changed')
    settings = run_hlp.settings_from_top()
    metadata = plan['prepared_metadata']
    settings.update(START_DATE=metadata['data_start'][:10], END_DATE='2026-10-31',
        CSV1_INPUT=str((Path(plan_path).parent/('csv1_sample.csv' if manifest['stage']=='sample' else 'csv1_selected.csv')).resolve()),
        FILLS_ROOT=str((route/'unused_fills').resolve()),
        SNAPSHOTS_ROOT=str((route/PREFIXES[0]).resolve()), DIFFS_ROOT=str((route/PREFIXES[1]).resolve()),
        OUTPUT_ROOT=str(Path(output_root).resolve()), RUN_STAGE='csv2', RESUME=True,
        DETAILED_LOG=False, CONSOLE_LOG=False)
    directory, _ = run_hlp.run(settings)
    require({p.name for p in directory.iterdir()} == SIX_FILES, 'Unexpected native run files')
    return dict(status='native_csv2_finished', directory=str(directory))


def export(plan_path, run_dir, output, stage='full'):
    import sqlite3
    import state_store as store
    plan = load_plan(plan_path)
    directory = Path(run_dir)
    require(directory.resolve() not in Path(output).resolve().parents, 'Export must be outside the six-file native run')
    require({p.name for p in directory.iterdir()} == SIX_FILES, 'Expected six-file native run')
    # Read progress without creating/migrating a database or accepting an unfinished CSV.
    with sqlite3.connect((directory/'progress.sqlite').resolve().as_uri()+'?mode=ro', uri=True) as db:
        require(store.completed(db, 2), 'Native CSV2 has not committed completion')
        for number in (1, 2):
            name = 'csv'+str(number)
            state = db.execute('SELECT offset,complete FROM outputs WHERE name=?',(name,)).fetchone()
            require(state and state[1] and (directory/(name+'.csv')).stat().st_size==state[0], 'Uncommitted CSV bytes')
            with (directory/(name+'.csv')).open('rb') as f:
                cursor = 0
                for position, end, signature in db.execute('SELECT position,end,sha FROM output_chunks WHERE name=? ORDER BY position',(name,)):
                    require(position==cursor, 'Noncontiguous CSV commitment')
                    require(hashlib.sha256(f.read(end-position)).hexdigest()==signature, 'Committed CSV content changed')
                    cursor = end
                require(cursor==state[0], 'Incomplete CSV commitment')
    rows = native_rows(directory/'csv1.csv')
    episodes, _ = validated_episodes(rows)
    ids = ({e for _, values in SAMPLES for e in values} if stage == 'sample'
           else {e['episode_id'] for e in plan['episodes']})
    require({e['episode_id'] for e in episodes} == ids, 'Native CSV1 target set differs')
    expected = {e['episode_id']:e for e in plan['episodes'] if e['episode_id'] in ids}
    source_rows = [r for r in native_rows(Path(plan_path).parent/('csv1_sample.csv' if stage=='sample' else 'csv1_selected.csv'))]
    require([{k:v for k,v in r.items() if k!='run_id'} for r in rows] ==
            [{k:v for k,v in r.items() if k!='run_id'} for r in source_rows], 'Native CSV1 changed input cells')
    with (directory/'csv2.csv').open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f, strict=True)
        require(reader.fieldnames == pipe.CSV2_FIELDS, 'Unexpected native CSV2 header')
        result = list(reader)
    seen, states = set(), Counter()
    for row in result:
        require(None not in row and all(v is not None for v in row.values()), 'Malformed CSV2 row')
        if row['record_type'] == 'issue':
            continue
        name = row['episode_id']
        require(row['record_type']=='episode' and name in ids and name not in seen, 'Duplicate/unexpected episode')
        seen.add(name)
        ep = expected[name]
        for key in ['episode_input_hash','coin','direction','total_qty','entry_notional','entry_vwap',
                    'partial_episode','partial_reason','split_before','split_after','tn','target_block']:
            require(row[key] == pipe.value_text(ep[key]), 'CSV2 inherited value changed: '+key)
        side = 'bid' if ep['direction']=='long' else 'ask'
        require(row['book_state_point']=='block_end' and row['book_side']==side, 'CSV2 side/state mismatch')
        require(row['status'] in ('book_ready', 'skipped'), 'Unknown CSV2 state')
        states[row['status']] += 1
        if row['status']=='book_ready':
            require(row['last_applied_block']==str(ep['target_block']) and row['replay_coverage_mode']=='dense', 'Incomplete replay')
            orders = json.loads(row['orders_json'])
            # Reuse exact OID/ordering checks, independently sum the numerical fields.
            metrics = pipe.book_metrics(pipe.book_from_orders(ep['coin'], orders, side), side)
            for key, value in metrics.items():
                require(row[key] == pipe.value_text(value), 'CSV2 book metric mismatch: '+key)
            with localcontext() as ctx:
                ctx.prec = engine.CALC_PRECISION
                require(Decimal(row['total_depth'])==sum((Decimal(o['qty']) for o in orders), Decimal(0)), 'Depth audit failed')
                require(Decimal(row['side_notional'])==sum((Decimal(o['price'])*Decimal(o['qty']) for o in orders), Decimal(0)), 'Notional audit failed')
        else:
            require(bool(row['reason_code']), 'Skipped episode lacks reason')
    require(seen == ids, 'Missing CSV2 episodes')
    fields = pipe.CSV2_FIELDS + ['snapshot_lag_ms']
    write_bytes(output, csv_bytes(fields, [dict(r, snapshot_lag_ms='') for r in result]))
    return dict(status='exported', episodes=len(seen), states=dict(states), columns=len(fields), sha256=sha(output))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare'); p.add_argument('--csv1', required=True); p.add_argument('--output', required=True)
    p = sub.add_parser('access'); p.add_argument('--evidence'); p.add_argument('--output', required=True)
    for name in ('validate-manifest', 'fetch', 'verify-sample', 'run'):
        p = sub.add_parser(name)
        p.add_argument('--plan', required=True); p.add_argument('--manifest', required=True)
        if name != 'validate-manifest': p.add_argument('--raw-root', required=True)
        if name in ('fetch', 'run'): p.add_argument('--sample-receipt')
        if name == 'fetch': p.add_argument('--evidence', required=True)
        if name in ('verify-sample', 'run'): p.add_argument('--output', required=True)
    p = sub.add_parser('export'); p.add_argument('--plan', required=True); p.add_argument('--run-dir', required=True)
    p.add_argument('--output', required=True); p.add_argument('--stage', choices=['sample','full'], default='full')
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            plan = prepare(args.csv1, args.output)
            result = dict(status='prepared', plan_hash=plan['plan_hash'], counts=plan['counts'])
        elif args.command == 'access':
            result = assess_access(read_json(args.evidence) if args.evidence else None)
            write_json(args.output, result)
        elif args.command == 'validate-manifest':
            _, manifest = load_manifest(args.plan, args.manifest)
            result = dict(status='manifest_valid', stage=manifest['stage'], objects=len(manifest['objects']))
        elif args.command == 'fetch':
            result = fetch(args.plan,args.manifest,args.raw_root,args.evidence,args.sample_receipt)
        elif args.command == 'verify-sample':
            result = verify_sample(args.plan,args.manifest,args.raw_root,args.output)
            result = {k:result[k] for k in ('status','data_kind','real_archive_sample_verified')}
        elif args.command == 'run':
            result = run_native(args.plan,args.manifest,args.raw_root,args.output,args.sample_receipt)
        else:
            result = export(args.plan,args.run_dir,args.output,args.stage)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, OSError, engine.BookError, CheckpointError) as exc:
        # Provider transport/local manifests can contain secrets. Details stay local.
        message = str(exc) if isinstance(exc, InvalidInput) else type(exc).__name__
        print(json.dumps(dict(status='blocked', reason=message), ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
