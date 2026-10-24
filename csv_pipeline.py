"""Durable CSV1 -> CSV2 -> CSV3 using the unchanged book engine.
CSV1 checkpoints fills shards; CSV2/3 checkpoint complete episode rows.
Durable byte offsets and SHA256 distinguish torn tails from committed corruption.
CSV3 has no raw-data discovery/reader dependency.
"""
from __future__ import annotations
import contextlib
import csv
from decimal import Decimal, localcontext
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import hlp_la_screen as app
from input_discovery import atomic_json
from rmp_l4 import digest, source_identity

VERSION = 'csv-stages-v2'
CSV1_FIELDS = app.CSV1_FIELDS + ['csv_schema', 'episode_input_hash', 'data_start',
    'data_end_exclusive', 'formal_start', 'formal_end_exclusive']
CSV2_FIELDS = app.CSV2_FIELDS + ['csv_schema', 'episode_input_hash', 'total_qty',
    'entry_vwap', 'partial_episode', 'replay_state_json', 'replay_state_hash',
    'snapshot_identity_json', 'strict_orig_sz']
CSV3_FIELDS = app.CSV3_FIELDS + ['csv_schema', 'episode_input_hash', 'csv2_row_hash',
    'tn', 'target_block', 'book_side', 'order_count', 'total_depth']
csv.field_size_limit(512 * 1024 * 1024)

class CheckpointError(RuntimeError):
    pass

def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def hash_file(path, limit=None):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        remaining = limit
        while remaining is None or remaining > 0:
            data = f.read(1024 * 1024 if remaining is None else min(1024 * 1024, remaining))
            if not data: break
            h.update(data)
            if remaining is not None: remaining -= len(data)
    if remaining not in (None, 0):
        raise CheckpointError(f'Committed file is truncated: {path}')
    return h

def csv_bytes(fields, row=None):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator='\n')
    if row is None: writer.writeheader()
    else: writer.writerow({key: app._csv_value(value) for key, value in row.items()})
    return stream.getvalue().encode('utf-8')

@contextlib.contextmanager
def run_lock(path):
    """OS-held lock is released on crashes; never remove another run's lock."""
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as f:
        if f.seek(0, 2) == 0: f.write(b' '); f.flush()
        f.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise CheckpointError('This run is already active in another process') from exc
        try: yield
        finally:
            f.seek(0)
            if os.name == 'nt': msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(f.fileno(), fcntl.LOCK_UN)

class AppendCSV:
    def __init__(self, path, fields, dependencies, log):
        self.path, self.fields, self.log = Path(path), fields, log
        self.receipt = self.path.parent / '.state' / (self.path.stem + '.json')
        self.dependencies = dependencies; self.handle = None
        if self.receipt.exists():
            self.state = read_json(self.receipt)
            if (self.state.get('version') != VERSION or self.state.get('fields') != fields
                    or self.state.get('dependencies') != dependencies):
                raise CheckpointError(f'Checkpoint dependencies/schema changed: {self.path.name}')
            self._recover_finalization()
            offset = self.state['offset']
            if type(offset) is not int or offset < 0 or self.path.stat().st_size < offset:
                raise CheckpointError(f'Committed file is truncated: {self.path}')
            self.hasher = hash_file(self.path, offset)
            if self.hasher.hexdigest() != self.state['sha256']:
                raise CheckpointError(f'Committed CSV integrity mismatch: {self.path}')
            if self.path.stat().st_size > offset:
                if self.state['complete']:
                    raise CheckpointError(f'Completed CSV was externally appended: {self.path}')
                with self.path.open('r+b') as f:
                    f.truncate(offset); f.flush(); os.fsync(f.fileno())
                log.warning('CSV_TAIL_ROLLED_BACK', csv=self.path.name, committed_bytes=offset)
            log.info('CSV_CHECKPOINT_LOADED', csv=self.path.name, rows=self.state['rows'], complete=self.done)
        else:
            data = csv_bytes(fields)
            if self.path.exists():
                if self.path.stat().st_size > len(data) or not data.startswith(self.path.read_bytes()):
                    raise CheckpointError(f'CSV exists without a checkpoint; use a new run: {self.path}')
                log.info('CSV_HEADER_RECOVERED', csv=self.path.name)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('wb') as f:
                f.write(data); f.flush(); os.fsync(f.fileno())
            self.hasher = hashlib.sha256(data)
            self.state = dict(version=VERSION, fields=fields, dependencies=dependencies,
                offset=len(data), sha256=self.hasher.hexdigest(), rows=0, complete=False, extra={})
            atomic_json(self.receipt, self.state)
        if not self.done: self.handle = self.path.open('ab')

    @property
    def done(self): return self.state['complete']

    def _recover_finalization(self):
        pending = self.state.get('finalizing')
        if not pending: return
        temp = self.path.parent / pending['temp']
        if self.path.exists() and hash_file(self.path).hexdigest() == pending['sha256']: pass
        elif temp.exists() and hash_file(temp).hexdigest() == pending['sha256']: os.replace(temp, self.path)
        else: raise CheckpointError(f'Cannot recover CSV1 finalization: {self.path}')
        self.state.update(offset=pending['offset'], sha256=pending['sha256'], rows=pending['rows'], complete=True)
        self.state.pop('finalizing', None)
        atomic_json(self.receipt, self.state)
        self.log.info('CSV_FINALIZATION_RECOVERED', csv=self.path.name)

    def write(self, row):
        if self.done: raise CheckpointError('Cannot append a completed CSV')
        data = csv_bytes(self.fields, row)
        self.handle.write(data); self.hasher.update(data); self.state['rows'] += 1

    def commit(self, **extra):
        self.handle.flush(); os.fsync(self.handle.fileno())
        self.state['offset'] = self.path.stat().st_size
        self.state['sha256'] = self.hasher.hexdigest()
        self.state['extra'].update(extra)
        atomic_json(self.receipt, self.state)
        self.log.info('CSV_CHECKPOINT_COMMITTED', csv=self.path.name, rows=self.state['rows'], **extra)

    def finish(self, **extra):
        self.commit(**extra); self.state['complete'] = True
        atomic_json(self.receipt, self.state); self.close()

    def replace_complete(self, temp, rows):
        self.close(); temp = Path(temp)
        pending = dict(temp=temp.name, sha256=hash_file(temp).hexdigest(), offset=temp.stat().st_size, rows=rows)
        self.state['finalizing'] = pending
        atomic_json(self.receipt, self.state); os.replace(temp, self.path)
        self.state.update(offset=pending['offset'], sha256=pending['sha256'], rows=rows, complete=True)
        self.state.pop('finalizing'); atomic_json(self.receipt, self.state)

    def rows(self):
        with self.path.open(encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != self.fields: raise CheckpointError(f'Unexpected CSV header: {self.path}')
            for row in reader:
                if None in row or any(v is None for v in row.values()):
                    raise CheckpointError(f'Incomplete CSV record: {self.path}')
                yield row

    def close(self):
        if self.handle is not None: self.handle.close(); self.handle = None

def range_columns(config):
    start, end = app.parse_time(config['data_start']), app.parse_time(config['data_end']) + 1
    return dict(data_start=app.time_text(start), data_end_exclusive=app.time_text(end),
                formal_start=app.time_text(start + app.BUFFER_MS), formal_end_exclusive=app.time_text(end - app.BUFFER_MS))

def episode_hash(ep):
    return digest(dict(coin=ep.coin, direction=ep.direction,
        fills=[dict(event_id=f.event_id, time_ms=f.time_ms, block_number=f.block_number,
                    price=app.dec_text(f.price), qty=app.dec_text(f.qty),
                    source_file=f.source_file, source_line=f.source_line) for f in ep.fills],
        partial_reasons=ep.partial_reasons, split_before=ep.split_before, split_after=ep.split_after))

def record_data(record):
    if isinstance(record, app.FillGap):
        return dict(kind='gap', source_file=record.source_file, reason=record.reason, start_ms=record.start_ms, end_ms=record.end_ms)
    return dict(kind='fill', event_id=record.event_id, coin=record.coin, direction=record.direction,
                price=app.dec_text(record.price), qty=app.dec_text(record.qty), time_ms=record.time_ms,
                block_number=record.block_number, source_file=record.source_file, source_line=record.source_line)

def journal_records(path):
    with Path(path).open(encoding='utf-8') as f:
        for line in f:
            row = json.loads(line); kind = row.pop('kind')
            if kind == 'gap': yield app.FillGap(**row)
            elif kind == 'fill':
                row['price'], row['qty'] = Decimal(row['price']), Decimal(row['qty'])
                yield app.FillRecord(**row)
            else: raise CheckpointError('Unknown CSV1 journal record')

class ShardLog:
    """Outer reader owns coverage; suppress only per-call synthetic warnings."""
    def __init__(self, log): self.log = log
    @property
    def lines(self): return self.log.lines
    @lines.setter
    def lines(self, value): self.log.lines = value
    def info(self, event, **kw): self.log.info(event, **kw)
    def warning(self, event, **kw):
        if event == 'FILLS_GAP' and kw.get('reason') == 'MISSING_FILLS_COVERAGE': return
        self.log.warning(event, **kw)
    def heartbeat(self, *args): self.log.heartbeat(*args)

def stat_token(path):
    try:
        s = Path(path).stat(); return (s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino, s.st_dev)
    except FileNotFoundError: return None

def generate_csv1(config, out, log):
    out = Path(out)
    table = AppendCSV(out/'csv1.csv', CSV1_FIELDS, {'semantic':config['_semantic_hash']}, log)
    try:
        if table.done:
            log.info('STAGE_REUSED', stage='csv1', raw_fills_read=False)
            return table.state['sha256']
        plan = out/'.state/csv1_sources.json'
        if plan.exists(): config['fills']['chunks'] = read_json(plan)['chunks']
        else:
            if config.get('auto_input_discovery'):
                if not Path(config['auto_input_discovery']['fills_root']).is_dir():
                    raise ValueError('FILLS_ROOT directory does not exist')
                from input_discovery import discover_fills
                discover_fills(config, log)
            chunks = app._shards(config['fills'], app.parse_time(config['data_start']), app.parse_time(config['data_end']))
            config['fills']['chunks'] = chunks
            atomic_json(plan, {'chunks':chunks})
        chunks = config['fills']['chunks']
        journal = out/'.state/csv1_records.jsonl'
        extra = table.state['extra']; committed = extra.get('journal_bytes', 0)
        if committed:
            if not journal.exists() or hash_file(journal, committed).hexdigest() != extra['journal_sha256']:
                raise CheckpointError('CSV1 committed journal integrity mismatch')
        else: journal.touch(exist_ok=True)
        if journal.stat().st_size < committed: raise CheckpointError('CSV1 journal is truncated')
        with journal.open('r+b') as f: f.truncate(committed)
        jhash = hash_file(journal)
        start, end = app.parse_time(config['data_start']), app.parse_time(config['data_end'])
        covered = extra.get('covered', start)
        last_seen = tuple(extra['last_seen']) if extra.get('last_seen') is not None else None
        with journal.open('ab') as handle:
            def append(record):
                nonlocal last_seen
                if isinstance(record, app.FillRecord):
                    key = (record.time_ms, record.block_number)
                    if last_seen is not None and key < last_seen:
                        log.warning('FILLS_GAP', reason='UNREADABLE_FILL', file=record.source_file,
                                    detail='fills are not in chronological block order')
                        record = app.FillGap(record.source_file, 'UNREADABLE_FILL: chronological block order')
                    else:
                        last_seen = key
                        table.write(dict(event_id=record.event_id, coin=record.coin, direction=record.direction,
                            time=app.time_text(record.time_ms), block_number=record.block_number,
                            price=record.price, qty=record.qty, source_file=record.source_file,
                            source_line=record.source_line, status='reading', csv_schema=VERSION, **range_columns(config)))
                data = (app.json_text(record_data(record))+'\n').encode('utf-8')
                handle.write(data); jhash.update(data)
            for index, chunk in enumerate(chunks):
                if index < extra.get('sources_done', 0): continue
                a, z = app.parse_time(chunk['start']), app.parse_time(chunk['end'])
                if z <= start or a > end: continue
                if a < covered and covered != start: raise CheckpointError('Fills source coverage overlaps or is out of order')
                if a > covered:
                    append(app.FillGap('<unlisted fills coverage>', 'MISSING_FILLS_COVERAGE', covered, a))
                    log.warning('FILLS_GAP', reason='MISSING_FILLS_COVERAGE', start=app.time_text(covered), end=app.time_text(a))
                before = stat_token(chunk['path'])
                single = {**config, 'fills':{**config['fills'], 'chunks':[chunk]}}
                for record in app.iter_fills(single, ShardLog(log)):
                    if isinstance(record, app.FillGap) and record.source_file == '<unlisted fills coverage>' and record.reason == 'MISSING_FILLS_COVERAGE': continue
                    append(record)
                if stat_token(chunk['path']) != before:
                    raise CheckpointError(f'Fills file changed while reading: {chunk["path"]}')
                covered = max(covered, z)
                handle.flush(); os.fsync(handle.fileno())
                table.commit(sources_done=index+1, journal_bytes=handle.tell(),
                             journal_sha256=jhash.hexdigest(), covered=covered, last_seen=last_seen)
            if not table.state['extra'].get('all_sources_done'):
                if covered < end: append(app.FillGap('<unlisted fills coverage>', 'MISSING_FILLS_COVERAGE', covered, end))
                handle.flush(); os.fsync(handle.fileno())
                table.commit(all_sources_done=True, journal_bytes=handle.tell(),
                             journal_sha256=jhash.hexdigest(), covered=covered, last_seen=last_seen)
        episodes = app.build_episodes(journal_records(journal), start, end)
        fd, name = tempfile.mkstemp(prefix='.csv1_final_', suffix='.tmp', dir=out)
        with os.fdopen(fd, 'wb') as f:
            f.write(csv_bytes(CSV1_FIELDS)); rows = 0
            for ep in episodes:
                ep.status = 'grouped'
                input_hash = episode_hash(ep)
                class Writer:
                    def writerow(self, row):
                        nonlocal rows
                        f.write(csv_bytes(CSV1_FIELDS, {**row, 'csv_schema':VERSION,
                            'episode_input_hash':input_hash, **range_columns(config)}))
                        rows += 1
                app.write_csv1_episode(ep, Writer())
            f.flush(); os.fsync(f.fileno())
        table.replace_complete(name, rows)
        log.info('CSV1_FINALIZED', episodes=len(episodes), fills=rows, status='grouped', sha256=table.state['sha256'])
        return table.state['sha256']
    finally: table.close()

def load_csv1(path, config):
    expected = range_columns(config); grouped, repeated = {}, {}
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != CSV1_FIELDS: raise CheckpointError('CSV1 schema is incompatible; generate CSV1 with this version')
        for row in reader:
            if any(row.get(k) != v for k, v in expected.items()) or row.get('status') != 'grouped':
                raise CheckpointError('CSV1 range/status differs from the completed grouping stage')
            eid = row['episode_id']
            if not eid: raise CheckpointError('CSV1 contains a pending episode')
            if eid not in grouped:
                ep = app.Episode(row['coin'], row['direction'], episode_id=eid,
                    partial_reasons=row['partial_reason'].split(';') if row['partial_reason'] else [],
                    split_before=row['split_before'], split_after=row['split_after'], status='grouped')
                grouped[eid] = ep; repeated[eid] = row
            else:
                ep = grouped[eid]
                keys = ['coin','direction','episode_start','episode_end','start_block','end_block',
                        'event_count','total_qty','entry_vwap','partial_episode','partial_reason',
                        'split_before','split_after','episode_input_hash','csv_schema']
                if any(row[k] != repeated[eid][k] for k in keys):
                    raise CheckpointError(f'CSV1 episode metadata is inconsistent: {eid}')
            fill = app.FillRecord(row['event_id'], row['coin'], row['direction'],
                app.positive_decimal(row['price'], 'price'), app.positive_decimal(row['qty'], 'qty'),
                app.parse_time(row['time']), app._book_int(row['block_number'], 'block_number'),
                row['source_file'], app._book_int(row['source_line'], 'source_line'))
            if ep.fills and (fill.time_ms, fill.block_number) < (ep.last.time_ms, ep.last.block_number):
                raise CheckpointError(f'CSV1 episode order is invalid: {eid}')
            ep.fills.append(fill)
    for eid, ep in grouped.items():
        row = repeated[eid]
        actual = dict(event_count=str(len(ep.fills)), total_qty=app.dec_text(ep.total_qty),
            entry_vwap=app.dec_text(ep.entry_vwap), episode_start=app.time_text(ep.first.time_ms),
            episode_end=app.time_text(ep.last.time_ms), start_block=str(ep.first.block_number),
            end_block=str(ep.last.block_number), episode_input_hash=episode_hash(ep),
            partial_episode='true' if ep.partial_reasons else 'false')
        if any(row[k] != v for k, v in actual.items()): raise CheckpointError(f'CSV1 episode aggregate/hash mismatch: {eid}')
        if ep.direction not in ('long', 'short'): raise CheckpointError(f'Unknown CSV1 direction: {eid}')
    return sorted(grouped.values(), key=lambda e:(e.last.block_number, e.last.time_ms, e.episode_id))

def validate_row(row, ep):
    expected = dict(episode_id=ep.episode_id, coin=ep.coin, direction=ep.direction,
        tn=app.time_text(ep.last.time_ms), target_block=str(ep.last.block_number),
        episode_input_hash=episode_hash(ep), total_qty=app.dec_text(ep.total_qty),
        entry_vwap=app.dec_text(ep.entry_vwap), book_side='bid' if ep.direction == 'long' else 'ask')
    if any(row.get(k) != v for k, v in expected.items()):
        raise CheckpointError(f'CSV2 is not linked to its CSV1 episode: {ep.episode_id}')

def book_from_orders(coin, orders, side=None, strict=False):
    if not isinstance(orders, list): raise CheckpointError('CSV2 orders must be a JSON array')
    book = app.OrderBook(coin, strict_orig_sz=strict)
    for value in orders:
        if not isinstance(value, dict): raise CheckpointError('CSV2 order must be an object')
        actual_side = value.get('side', side)
        if side is not None and actual_side != side: raise CheckpointError('CSV2 order side differs from execution side')
        book._insert_snapshot_order({**value, 'coin':coin, 'side':actual_side})
    return book

def replay_state(book):
    return [dict(oid=o.oid, side=o.side, price=app.dec_text(o.price),
                 qty=app.dec_text(o.qty), user=o.user) for o in book.orders.values()]

def book_plan(config, episodes, out, log):
    plan = Path(out)/'.state/book_inputs.json'
    if plan.exists():
        saved = read_json(plan); config['snapshots'], config['diffs'] = saved['snapshots'], saved['diffs']
        for ep in episodes:
            error = saved['errors'].get(ep.episode_id)
            if error: ep.discovery_error = app.BookError(**error)
        log.info('BOOK_INPUT_PLAN_REUSED', episodes=len(episodes), fills_read=False)
        return
    if config.get('auto_input_discovery'):
        from input_discovery import discover_book_inputs
        discover_book_inputs(config, episodes, log)
    errors = {e.episode_id:dict(code=e.discovery_error.code, message=e.discovery_error.message,
                              source=e.discovery_error.source) for e in episodes if getattr(e,'discovery_error',None)}
    atomic_json(plan, dict(snapshots=config['snapshots'], diffs=config['diffs'], errors=errors))

def generate_csv2(config, out, log, csv1_hash):
    out = Path(out); episodes = load_csv1(out/'csv1.csv', config)
    table = AppendCSV(out/'csv2.csv', CSV2_FIELDS, {'csv1':csv1_hash}, log)
    try:
        completed = 0
        needed_coins = {e.coin for e in episodes[table.state['rows']:]}; restore = {}
        for row in table.rows():
            if completed >= len(episodes): raise CheckpointError('CSV2 contains extra episodes')
            ep = episodes[completed]; validate_row(row, ep)
            if row['status'] == 'ok' and ep.coin in needed_coins:
                restore[(ep.coin, row['snapshot_file'], int(row['snapshot_block']))] = row
            completed += 1
        if completed != table.state['rows']: raise CheckpointError('CSV2 checkpoint row count is invalid')
        if table.done:
            if completed != len(episodes): raise CheckpointError('Completed CSV2 omits episodes')
            log.info('STAGE_REUSED', stage='csv2', raw_book_data_read=False)
            return table.state['sha256']
        pending = episodes[completed:]
        if not pending:
            table.finish(); return table.state['sha256']
        book_plan(config, pending, out, log)
        manager = app.ReplayManager(config, log); manager.prepare(pending)
        pending_keys = {(ep.coin,ep.snapshot) for ep in pending if ep.snapshot is not None
                        and ep.episode_id not in manager.planning_errors
                        and (ep.snapshot,ep.coin) not in manager.extraction_errors}
        for key in pending_keys:
            coin, ref = key
            previous = restore.pop((coin,ref.path,ref.block_number), None)
            if previous is None: continue
            if json.loads(previous['snapshot_identity_json']) != source_identity(ref.path):
                raise CheckpointError(f'Selected snapshot changed since committed CSV2: {ref.path}')
            raw = json.loads(previous['replay_state_json'])
            if digest(raw) != previous['replay_state_hash']: raise CheckpointError('CSV2 replay state hash mismatch')
            book = book_from_orders(coin, raw, strict=previous['strict_orig_sz']=='true')
            height = int(previous['target_block'])
            if height > min(e.last.block_number for e in pending if (e.coin,e.snapshot)==key):
                raise CheckpointError('CSV2 cache would start after a pending target')
            manager.cache[key] = (ref,height,app.parse_time(previous['tn']),book)
            log.info('REPLAY_CHECKPOINT_RESTORED', coin=coin, snapshot=ref.path, through_block=height)
        restore.clear()
        if config.get('local_diff_preparation'):
            from prepare_local_diffs import prepare_diff_inputs
            manager.config['_prepared_coin_diffs'] = prepare_diff_inputs(config, pending, manager, log)
        for ep in pending:
            row = dict(episode_id=ep.episode_id, coin=ep.coin, direction=ep.direction,
                tn=app.time_text(ep.last.time_ms), target_block=ep.last.block_number,
                book_state_point='block_end', book_side='bid' if ep.direction=='long' else 'ask',
                total_qty=ep.total_qty, entry_vwap=ep.entry_vwap, partial_episode=bool(ep.partial_reasons),
                episode_input_hash=episode_hash(ep), csv_schema=VERSION, status='ok', skip_reason='', error_file='')
            book = None
            try:
                log.info('EPISODE_START', stage='csv2', episode=ep.episode_id, coin=ep.coin, target_block=ep.last.block_number)
                before = source_identity(ep.snapshot.path) if ep.snapshot is not None else None
                book = manager.get(ep)
                if source_identity(ep.snapshot.path) != before:
                    raise app.BookError('SNAPSHOT_CHANGED','Selected snapshot changed during replay',ep.snapshot.path)
                orders = book.side_orders(row['book_side'])
                with localcontext() as ctx:
                    ctx.prec = app.CALC_PRECISION
                    depth = sum((o.qty for o in orders),Decimal(0))
                state = replay_state(book)
                row.update(snapshot_file=ep.snapshot.path, snapshot_block=ep.snapshot.block_number,
                    order_count=len(orders), total_depth=depth,
                    orders_json=app.json_text([dict(oid=o.oid,price=app.dec_text(o.price),qty=app.dec_text(o.qty)) for o in orders]),
                    replay_state_json=app.json_text(state), replay_state_hash=digest(state),
                    snapshot_identity_json=app.json_text(before), strict_orig_sz=book.strict_orig_sz)
                del state, orders
            except (app.BookError,app.InputProblem,OSError,EOFError,UnicodeError) as exc:
                row.update(status='skipped', skip_reason=f'{getattr(exc,"code",type(exc).__name__)}: {getattr(exc,"message",str(exc))}',
                           error_file=str(getattr(exc,'source','') or (ep.snapshot.path if ep.snapshot else '')),
                           snapshot_file=ep.snapshot.path if ep.snapshot else '', snapshot_block=ep.snapshot.block_number if ep.snapshot else '')
                log.warning('EPISODE_SKIPPED', stage='csv2', episode=ep.episode_id, reason=row['skip_reason'])
            table.write(row); table.commit(last_episode=ep.episode_id)
            manager.finish_episode(ep); ep.book_result = ep.la_result = None
            book = row = None
            log.info('EPISODE_RESULTS_RELEASED', stage='csv2', episode=ep.episode_id)
        table.finish()
        log.info('CACHE_SUMMARY', replay_entries=len(manager.cache), **manager.stats, **manager.extraction.stats)
        return table.state['sha256']
    finally: table.close()

def generate_csv3(config, out, log, csv1_hash, csv2_hash):
    """Read only completed CSVs + checkpoint metadata; never call raw-data APIs."""
    out = Path(out); episodes = load_csv1(out/'csv1.csv', config)
    table = AppendCSV(out/'csv3.csv', CSV3_FIELDS, {'csv1':csv1_hash,'csv2':csv2_hash}, log)
    try:
        done = 0
        summary = dict(episodes=len(episodes), fills=sum(len(e.fills) for e in episodes),
                       calculated=0, skipped=0, depth_insufficient=0,
                       partial_episodes=sum(bool(e.partial_reasons) for e in episodes))
        def count(row):
            summary['skipped'] += int(row['status']=='skipped')
            summary['calculated'] += int(row['status']!='skipped')
            summary['depth_insufficient'] += int(row.get('depth_insufficient') in (True,'true'))
        for row in table.rows():
            if done >= len(episodes) or row['episode_id'] != episodes[done].episode_id or row['episode_input_hash'] != episode_hash(episodes[done]):
                raise CheckpointError('CSV3 committed prefix differs from CSV1')
            count(row); done += 1
        if done != table.state['rows']: raise CheckpointError('CSV3 checkpoint row count is invalid')
        if table.done:
            if done != len(episodes): raise CheckpointError('Completed CSV3 omits episodes')
            log.info('STAGE_REUSED', stage='csv3', raw_data_read=False); return summary
        with (out/'csv2.csv').open(encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != CSV2_FIELDS: raise CheckpointError('CSV2 schema is incompatible')
            seen = 0
            for index, source in enumerate(reader):
                if index >= len(episodes): raise CheckpointError('CSV2 contains extra episodes')
                seen += 1; ep = episodes[index]; validate_row(source, ep)
                if index < done: continue
                row = dict(episode_id=ep.episode_id, coin=ep.coin, direction=ep.direction,
                    total_qty=ep.total_qty, entry_vwap=ep.entry_vwap, partial_episode=bool(ep.partial_reasons),
                    csv_schema=VERSION, episode_input_hash=episode_hash(ep), csv2_row_hash=digest(source),
                    tn=source['tn'], target_block=source['target_block'], book_side=source['book_side'],
                    order_count=source['order_count'], total_depth=source['total_depth'],
                    status=source['status'], skip_reason=source['skip_reason'])
                result = book = None
                if source['status'] == 'ok':
                    raw = json.loads(source['orders_json']); book = book_from_orders(ep.coin, raw, side=source['book_side'])
                    orders = book.side_orders(source['book_side'])
                    with localcontext() as ctx:
                        ctx.prec = app.CALC_PRECISION
                        depth = sum((o.qty for o in orders),Decimal(0))
                    if len(orders) != int(source['order_count']) or app.dec_text(depth) != source['total_depth']:
                        raise CheckpointError(f'CSV2 order count/depth mismatch: {ep.episode_id}')
                    result = app.simulate_close(book, ep.direction, ep.total_qty, ep.entry_vwap)
                    row.update(filled_qty=result['filled_qty'], unfilled_qty=result['unfilled_qty'],
                        exit_vwap=result['exit_vwap'], la=result['la'], depth_insufficient=result['depth_insufficient'],
                        fill_trace_json=app.json_text(result['fill_trace']),
                        status='empty_book' if result['filled_qty']==0 else 'partial_depth' if result['depth_insufficient'] else 'ok')
                    if config.get('detailed_log',False):
                        for i, trace in enumerate(result['fill_trace'],1):
                            log.info('SIMULATED_FILL', stage='csv3', episode=ep.episode_id, index=i, **trace)
                    del raw, orders
                elif source['status'] != 'skipped': raise CheckpointError(f'Unknown CSV2 status: {source["status"]}')
                table.write(row); table.commit(last_episode=ep.episode_id); count(row)
                log.info('EPISODE_DONE', stage='csv3', episode=ep.episode_id, status=row['status'], la=app.dec_text(result['la']) if result else None)
                ep.book_result = ep.la_result = result = book = row = None
                log.info('EPISODE_RESULTS_RELEASED', stage='csv3', episode=ep.episode_id)
            if seen != len(episodes): raise CheckpointError('CSV2 omits episodes')
        table.finish(); return summary
    finally: table.close()

def require_complete(config, out, number, log):
    fields = CSV1_FIELDS if number==1 else CSV2_FIELDS
    deps = {'semantic':config['_semantic_hash']} if number==1 else {'csv1':require_complete(config,out,1,log)}
    if not (Path(out)/'.state'/f'csv{number}.json').exists():
        raise CheckpointError(f'CSV{number} is not ready; run its stage or RUN_STAGE="all" first')
    table = AppendCSV(Path(out)/f'csv{number}.csv',fields,deps,log)
    try:
        if not table.done: raise CheckpointError(f'CSV{number} is incomplete; resume that stage first')
        return table.state['sha256']
    finally: table.close()

def run_pipeline(config, out, log, stage='all'):
    if stage not in ('all','csv1','csv2','csv3'): raise ValueError('RUN_STAGE must be all, csv1, csv2 or csv3')
    one = generate_csv1(config,out,log) if stage in ('all','csv1') else require_complete(config,out,1,log)
    if stage=='csv1':
        episodes = load_csv1(Path(out)/'csv1.csv',config)
        return dict(stage='csv1', episodes=len(episodes), fills=sum(len(e.fills) for e in episodes))
    two = generate_csv2(config,out,log,one) if stage in ('all','csv2') else require_complete(config,out,2,log)
    if stage=='csv2':
        episodes = load_csv1(Path(out)/'csv1.csv',config)
        return dict(stage='csv2', episodes=len(episodes), fills=sum(len(e.fills) for e in episodes))
    summary = generate_csv3(config,out,log,one,two)
    log.info('RUN_DONE', **summary); return summary
