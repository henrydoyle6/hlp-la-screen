"""Lazy source location and snapshot metadata; no global raw-data preflight."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from rmp_l4 import source_stream, source_identity, is_rmp
from state_store import digest
HOUR = 3_600_000
RMP_HEADER_LIMIT = 1024 * 1024
SUFFIXES = ('', '.zip', '.jsonl', '.jsonl.zip', '.jsonl.gz', '.gz', '.bz2', '.xz', '.zst', '.lz4')

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

def _rmp_header(path):
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


def rmp_header(path):
    import msgpack
    import zipfile
    from rmp_schema import detect_layout, locus_header
    try:
        schema,probe_bytes=detect_layout(path,source_stream,RMP_HEADER_LIMIT)
        header=locus_header(path,source_stream) if schema=='locus' else _rmp_header(path)
        return dict(header,schema=schema,layout_probe_bytes=probe_bytes)
    except (msgpack.UnpackException, zipfile.BadZipFile) as exc:
        raise ValueError('Cannot parse native snapshot header: ' + str(exc)) from exc

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


def issue(stage, code, message, source_file='', source_member='', source_line='',
          event_index='', block_number='', **known):
    value = dict(stage=stage, code=code, message=str(message).replace('\n', ' ')[:2000],
                 source_file=str(source_file), source_member=source_member,
                 source_line=source_line, event_index=event_index, block_number=block_number, **known)
    value['issue_id'] = 'IS' + digest(value)[:20]
    return value


def locate_hour(root, hour):
    from hlp_la_screen import InputProblem
    matches = hour_paths(root, hour)
    if len(matches) > 1:
        raise InputProblem('AMBIGUOUS_HOUR', 'Multiple encodings for the same hour: ' + ', '.join(map(str, matches)), str(root))
    if matches:
        return matches[0]
    stamp = datetime.fromtimestamp(hour // 1000, timezone.utc)
    return Path(root) / stamp.strftime('%Y%m%d') / f'{stamp.hour}.zip'


def member_name(path):
    import zipfile
    if str(path).lower().endswith('.zip'):
        with zipfile.ZipFile(path) as archive:
            members = [m for m in archive.infolist() if not m.is_dir()]
            if len(members) != 1:
                raise ValueError('Expected exactly one non-directory archive member')
            return members[0].filename
    return ''


def fills_sources(config):
    """Yield names lazily; locate/stat/open only the hour currently ingested."""
    from hlp_la_screen import parse_time
    if 'chunks' in config.get('fills', {}):
        yield from config['fills']['chunks']
        return
    first, end = parse_time(config['data_start']), parse_time(config['data_end_exclusive'])
    root = config['roots']['fills']
    for hour in range(first, end, HOUR):
        yield dict(hour=hour, start=hour, end=hour + HOUR, root=root)


class SnapshotCatalog:
    def __init__(self, config, cache, log):
        self.config, self.cache, self.log = config, cache, log
        self.refs, self.issues, self.groups = [], [], {}
        self.scanned = False

    def discover(self):
        from hlp_la_screen import SnapshotRef, parse_time, _book_read_json
        if self.scanned:
            return
        self.scanned = True
        declared = self.config.get('snapshots')
        if declared:
            self.refs = [SnapshotRef(str(v['path']), v['block_number'], parse_time(v.get('time', v.get('time_ms')))) for v in declared]
        else:
            root = self.config['roots']['snapshots']
            def walk_error(exc):
                self.issues.append(issue('csv2', 'SNAPSHOT_DIRECTORY', str(exc), root))
            found = False
            for directory, _, files in os.walk(root, onerror=walk_error):
                for name in sorted(files):
                    path = Path(directory) / name
                    if not is_rmp(path):
                        continue
                    found = True
                    try:
                        ident = source_identity(path)
                        from rmp_schema import HEADER_VERSION
                        key = digest([HEADER_VERSION, ident])
                        header = self.cache.read('header', key)
                        if header is None:
                            self.cache.count('RMP_HEADER_PROBE', source=str(path))
                            header = rmp_header(path)
                            self.log.info('RMP_HEADER_RESOLVED',source=str(path),schema=header.get('schema','legacy'),
                                header_bytes_read=header.get('header_bytes_read'),context_offset=header.get('context_offset'),
                                layout_probe_bytes=header.get('layout_probe_bytes'),block=header['block_number'])
                            if source_identity(path) != ident:
                                raise ValueError('SOURCE_CHANGED during header read')
                            with self.cache.db:
                                self.cache.write('header', key, header)
                        else:
                            self.cache.count('RMP_HEADER_CACHE_HIT', source=str(path))
                        self.refs.append(SnapshotRef(str(path.resolve()), header['block_number'], header['time_ms']))
                    except (OSError, ValueError, KeyError, EOFError) as exc:
                        problem = issue('csv2', 'SNAPSHOT_METADATA_UNRESOLVED', str(exc), path)
                        self.issues.append(problem)
                        self.log.warning('SNAPSHOT_METADATA_UNRESOLVED', **problem)
            if not found:
                self.issues.append(issue('csv2', 'NO_SNAPSHOT_FILES', 'No native snapshot candidates found', root))
        self.refs=list(dict.fromkeys(self.refs))
        for ref in self.refs:
            self.groups.setdefault((ref.time_ms, ref.block_number), []).append(ref)

    def choose(self, episode):
        from hlp_la_screen import select_snapshot, BookError
        self.discover()
        ref = select_snapshot(self.refs, episode['tn_ms'], episode['target_block'])
        if len(self.groups[(ref.time_ms, ref.block_number)]) > 1:
            raise BookError('AMBIGUOUS_SNAPSHOT', 'Multiple encodings of selected snapshot', ref.path)
        status = 'nearest_resolved_with_warning' if self.issues else 'nearest_resolved'
        return ref, status
