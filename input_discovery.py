"""Discover hourly inputs and ABCI metadata; calculations remain in hlp_la_screen.

The discovered block ranges are routing envelopes. Only the original dense
reader/replay establishes complete coverage. Header probes never decode a full
ABCI state; each demanded RMP body is scanned once by rmp_l4.ExtractionCache.
"""
from __future__ import annotations
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile

from rmp_l4 import digest, is_rmp, save, source_stream, source_identity

HOUR = 3_600_000
INDEX_VERSION = 'input-header-v1'
RMP_HEADER_LIMIT = 1024 * 1024
JSON_HEADER_LIMIT = 32 * 1024 * 1024
SUFFIXES = ('', '.zip', '.jsonl', '.jsonl.zip', '.jsonl.gz', '.gz',
            '.bz2', '.xz', '.zst', '.lz4')


def fingerprint(path):
    return source_identity(path)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.writing_', dir=path.parent)
    os.close(fd)
    try:
        save(tmp, value)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


class LimitedReader:
    def __init__(self, stream, limit):
        self.stream, self.limit, self.count = stream, limit, 0

    def read(self, n=-1):
        if self.count >= self.limit:
            raise ValueError('ABCI context not found within header limit; no full-state discovery scan')
        n = min(n if n >= 0 else self.limit, self.limit - self.count)
        data = self.stream.read(n)
        self.count += len(data)
        return data


def rmp_header(path):
    import msgpack
    from hlp_la_screen import parse_time
    with source_stream(path) as stream:
        reader = LimitedReader(stream, RMP_HEADER_LIMIT)
        u = msgpack.Unpacker(reader, raw=False, strict_map_key=False, read_size=4096,
                             max_buffer_size=RMP_HEADER_LIMIT * 2)
        for _ in range(u.read_map_header()):
            if u.unpack() != 'exchange':
                u.skip()
                continue
            for _ in range(u.read_map_header()):
                if u.unpack() != 'context':
                    u.skip()
                    continue
                value = u.unpack()
                height = value.get('height') if isinstance(value, dict) else None
                if type(height) is not int or height < 0:
                    raise ValueError('ABCI exchange.context.height must be a non-negative integer')
                return dict(block_number=height, time_ms=parse_time(value['time']),
                            header_bytes_read=reader.count)
    raise ValueError('ABCI exchange.context missing')


def json_header(path):
    from hlp_la_screen import open_json_text, parse_time
    with open_json_text(str(path)) as stream:
        while True:
            line = stream.readline(JSON_HEADER_LIMIT + 1)
            if len(line) > JSON_HEADER_LIMIT:
                raise ValueError('First nonblank block line exceeds 32 MiB header limit')
            if not line:
                return dict(empty=True)
            if line.strip():
                try:
                    import orjson
                    value = orjson.loads(line)
                except ImportError:
                    value = json.loads(line)
                if not isinstance(value, dict) or not isinstance(value.get('events'), list):
                    raise ValueError('Hourly input requires complete block JSONL with events array')
                height = value.get('block_number')
                if type(height) is not int or height < 0:
                    raise ValueError('Hourly block_number must be a non-negative integer')
                return dict(block_number=height, time_ms=parse_time(value['block_time']), empty=False)


class HeaderIndex:
    def __init__(self, root, log):
        self.root, self.log = Path(root), log
        self.hits = self.probes = self.errors = 0

    def get(self, path, kind):
        ident = fingerprint(path)
        key = digest(dict(source=ident, kind=kind, version=INDEX_VERSION))
        receipt = self.root / (key + '.json')
        try:
            previous = json.loads(receipt.read_text(encoding='utf-8'))
            value = previous['header']
            if (previous['source'] != ident or previous['version'] != INDEX_VERSION
                    or previous['kind'] != kind or previous['header_digest'] != digest(value)):
                raise ValueError('Header index changed')
            self.hits += 1
            self.log.info('INPUT_HEADER_CACHE_HIT', kind=kind, source=str(path), header=value)
            return value
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.probes += 1
        self.log.info('INPUT_HEADER_PROBE', kind=kind, source=str(path))
        try:
            value = rmp_header(path) if kind == 'rmp' else json_header(path)
            if fingerprint(path) != ident:
                raise ValueError('Input changed during header probe')
            atomic_json(receipt, dict(source=ident, kind=kind, version=INDEX_VERSION,
                                     header=value, header_digest=digest(value)))
            self.log.info('INPUT_HEADER_FOUND', kind=kind, source=str(path), header=value)
            return value
        except Exception:
            self.errors += 1
            raise


def hour_paths(root, hour):
    dt = datetime.fromtimestamp(hour // 1000, timezone.utc)
    dates = (dt.strftime('%Y%m%d'), dt.strftime('%Y-%m-%d'))
    bases = (Path(root), Path(root) / 'hourly')
    paths = set()
    for base in bases:
        for day in dates:
            for h in {str(dt.hour), f'{dt.hour:02d}'}:
                for suffix in SUFFIXES:
                    p = base / day / (h + suffix)
                    if p.is_file():
                        paths.add(p.resolve())
    return sorted(paths, key=str)


def hourly_sources(root, first, last, index, log, kind, missing_root):
    """Probe one header per expected hour; ambiguity is an explicit input gap."""
    rows = []
    for hour in range(first - first % HOUR, last - last % HOUR + HOUR, HOUR):
        matches = hour_paths(root, hour)
        dt = datetime.fromtimestamp(hour // 1000, timezone.utc)
        canonical = Path(root) / dt.strftime('%Y%m%d') / f'{dt.hour}.zip'
        row = dict(hour=hour, path=str(canonical), header=None)
        try:
            if len(matches) > 1:
                # Do not arbitrarily choose between two encodings of one hour.
                row['path'] = str(Path(missing_root) / kind / dt.strftime('%Y%m%d') / f'ambiguous_{dt.hour}')
                row['candidates'] = [str(p) for p in matches]
                raise ValueError('Multiple files represent the same hour; keep one encoding under the input root')
            if matches:
                row['path'] = str(matches[0])
                row['header'] = index.get(matches[0], 'jsonl')
            else:
                raise FileNotFoundError(str(canonical))
        except Exception as exc:
            row['error'] = f'{type(exc).__name__}: {exc}'
            log.warning('INPUT_HOUR_UNAVAILABLE', kind=kind, **row)
        rows.append(row)
    return rows


def discover_fills(config, log):
    from hlp_la_screen import parse_time, time_text
    options = config['auto_input_discovery']
    start, end = parse_time(config['data_start']), parse_time(config['data_end'])
    index = HeaderIndex(options['index_dir'], log)
    # A first block can precede its local_time partition hour. Probe/read the
    # following hour too when its ACTUAL block_time intersects the data interval.
    rows = hourly_sources(options['fills_root'], start, end + HOUR, index, log,
                          'fills', options['missing_root'])
    boundaries = [r['header']['time_ms'] if r['header'] and not r['header'].get('empty')
                  else r['hour'] for r in rows]
    if any(b <= a for a, b in zip(boundaries, boundaries[1:])):
        raise ValueError('Hourly fills first block times are not increasing; check partition names and input root')
    chunks = []
    for i, row in enumerate(rows):
        a = boundaries[i]
        z = boundaries[i + 1] if i + 1 < len(rows) else row['hour'] + HOUR
        if z > start and a <= end:
            chunks.append(dict(path=row['path'], start=time_text(a), end=time_text(z)))
    config['fills']['chunks'] = chunks
    atomic_json(Path(options['run_dir']) / 'fills_discovery.json', dict(sources=rows, chunks=chunks))
    log.info('FILLS_DISCOVERY_DONE', chunks=len(chunks), header_probes=index.probes,
             header_cache_hits=index.hits, header_errors=index.errors,
             data_start=time_text(start), data_end=time_text(end))


def discover_snapshots(config, episodes, index, log):
    from hlp_la_screen import BookError, SnapshotRef, select_snapshot, time_text
    options = config['auto_input_discovery']
    refs, bad, seen = [], [], set()
    def directory_error(exc):
        # An unlisted subtree could contain the nearest prior state. Fail
        # explicitly rather than silently selecting an older visible state.
        raise OSError(f'Cannot inventory snapshot directory: {exc}') from exc
    paths = []
    for directory, subdirs, files in os.walk(options['snapshots_root'], onerror=directory_error):
        for filename in files:
            path = Path(directory) / filename
            if is_rmp(path):
                paths.append(path)
    for path in sorted(paths, key=str):
        path = path.resolve()
        if str(path) in seen:
            continue
        seen.add(str(path))
        try:
            header = index.get(path, 'rmp')
            refs.append(SnapshotRef(str(path), header['block_number'], header['time_ms']))
        except Exception as exc:
            match = re.fullmatch(r'(\d+)\.rmp(?:\..+)?', path.name, re.IGNORECASE)
            item = dict(path=str(path), height_hint=int(match[1]) if match else None,
                        error=f'{type(exc).__name__}: {exc}')
            bad.append(item)
            log.warning('SNAPSHOT_METADATA_UNREADABLE', **item)
    config['snapshots'] = [dict(path=r.path, block_number=r.block_number, time=time_text(r.time_ms)) for r in refs]
    plans = []
    for ep in episodes:
        selected = None
        try:
            selected = select_snapshot(refs, ep.last.time_ms, ep.last.block_number)
        except BookError:
            pass  # Original manager will report NO_PRIOR_SNAPSHOT.
        blockers = [b for b in bad if b['height_hint'] is None or
                    (b['height_hint'] < ep.last.block_number and
                     (selected is None or b['height_hint'] >= selected.block_number))]
        duplicates = [r for r in refs if selected and r.block_number == selected.block_number
                      and r.time_ms == selected.time_ms]
        if blockers:
            ep.discovery_error = BookError('SNAPSHOT_METADATA_UNREADABLE',
                'Potential nearest prior snapshot cannot be identified; older fallback prohibited',
                '; '.join(b['path'] for b in blockers))
        elif len(duplicates) > 1:
            ep.discovery_error = BookError('AMBIGUOUS_SNAPSHOT',
                'Multiple original snapshots have the same selected height/time; keep one encoding',
                '; '.join(r.path for r in duplicates))
        ep.snapshot = selected
        plans.append(dict(episode_id=ep.episode_id, coin=ep.coin, tn=time_text(ep.last.time_ms),
                          target_block=ep.last.block_number,
                          snapshot=selected.path if selected else None,
                          snapshot_block=selected.block_number if selected else None,
                          error=str(getattr(ep, 'discovery_error', ''))))
    log.info('SNAPSHOT_DISCOVERY_DONE', original_snapshots=len(refs), unreadable=len(bad), episodes=len(episodes))
    return plans, bad


def discover_diffs(config, episodes, index, log):
    options = config['auto_input_discovery']
    needed = [ep for ep in episodes if ep.snapshot and not getattr(ep, 'discovery_error', None)]
    if not needed:
        config['diffs'] = dict(coverage_mode='dense', chunks=[])
        return []
    # The source snapshot may substantially predate the screening dates. Include
    # every required hour from that actual snapshot, plus boundary lookahead.
    first = min(ep.snapshot.time_ms for ep in needed)
    last = max(ep.last.time_ms for ep in needed) + HOUR
    lower = min(ep.snapshot.block_number + 1 for ep in needed)
    upper = max(ep.last.block_number for ep in needed)
    rows = hourly_sources(options['diffs_root'], first, last, index, log,
                          'diffs', options['missing_root'])
    chunks = []
    for i, row in enumerate(rows):
        hdr = row['header']
        known = hdr is not None and not hdr.get('empty')
        # Unknown/empty hours get a conservative envelope. They do not create
        # synthetic block rows; replay must still observe EVERY required height.
        preceding = [r['header']['block_number'] for r in rows[:i]
                     if r['header'] and not r['header'].get('empty')]
        following = [r['header']['block_number'] for r in rows[i + 1:]
                     if r['header'] and not r['header'].get('empty')]
        a = hdr['block_number'] if known else (preceding[-1] if preceding else lower)
        z = following[0] - 1 if following else upper
        if known and following and z < a:
            raise ValueError('Diff first block heights are not increasing across hourly partitions')
        a, z = max(a, lower), min(z, upper)
        if a <= z:
            chunks.append(dict(path=row['path'], start_block=a, end_block=z))
    config['diffs'] = dict(coverage_mode='dense', chunks=chunks)
    log.info('DIFF_DISCOVERY_DONE', chunks=len(chunks), first_required_block=lower,
             last_required_block=upper, header_probes=index.probes, header_cache_hits=index.hits)
    return rows


def discover_book_inputs(config, episodes, log):
    options = config['auto_input_discovery']
    index = HeaderIndex(options['index_dir'], log)
    log.info('PHASE_START', phase='discover_episode_book_inputs', episodes=len(episodes))
    if episodes:
        plans, bad = discover_snapshots(config, episodes, index, log)
        rows = discover_diffs(config, episodes, index, log)
    else:
        config['snapshots'], config['diffs'] = [], dict(coverage_mode='dense', chunks=[])
        plans, bad, rows = [], [], []
    atomic_json(Path(options['run_dir']) / 'book_discovery.json',
                dict(episodes=plans, snapshots=config['snapshots'], unreadable_snapshots=bad,
                     diff_sources=rows, diffs=config['diffs'],
                     routing_envelopes_are_not_coverage_proof=True))
    atomic_json(Path(options['run_dir']) / 'resolved_config.json', config)
    log.info('PHASE_DONE', phase='discover_episode_book_inputs', header_probes=index.probes,
             header_cache_hits=index.hits, header_errors=index.errors)
