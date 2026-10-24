"""Demanded-coin block projection in the one SQLite cache; preserve dense coverage."""
from decimal import Decimal
import hashlib
import json
import zipfile
from pathlib import Path
from rmp_l4 import source_identity
from state_store import digest, text
PROJECTION_VERSION = 'sqlite-block-prefix-v3'

def has_float(value):
    if isinstance(value,float):return True
    if isinstance(value,dict):return any(has_float(v) for v in value.values())
    if isinstance(value,list):return any(has_float(v) for v in value)
    return False


def loads_exact_selected(raw, wanted):
    """Fast integer/string common case; selected floats/large integers use Decimal.

    orjson overflow-to-float is also detected. No selected numerical float ever
    reaches an output. Invalid/unsupported JSON falls back to the standard parser.
    """
    try:
        import orjson
        value=orjson.loads(raw)
    except (ImportError,ValueError):return json.loads(raw,parse_float=Decimal)
    if isinstance(value,dict) and isinstance(value.get('events'),list):
        for event in value['events']:
            if isinstance(event,dict) and isinstance(event.get('coin'),str) and event['coin'] in wanted and has_float(event):
                return json.loads(raw,parse_float=Decimal)
    return value


def encode(value):
    try:
        import orjson
        return orjson.dumps(value,default=lambda n:format(n,'f'))+b'\n'
    except ImportError:
        return (json.dumps(value,ensure_ascii=False,separators=(',',':'),default=lambda n:format(n,'f'))+'\n').encode()


class DiffCache:
    def __init__(self, config, cache, coins, stop_block, log):
        self.config, self.cache, self.coins = config, cache, set(coins)
        self.stop_block, self.log = stop_block, log
        self.issues, self.used = [], []

    def _problem(self, code, message, path, **where):
        from input_discovery import issue
        problem = issue('csv2', code, message, path, **where)
        self.issues.append(problem)
        self.log.warning('DIFF_INPUT_ISSUE', **problem)
        return problem

    def ensure(self, path, force=False):
        from hlp_la_screen import open_json_text, InputProblem, BookError
        from input_discovery import member_name
        self.last_attempted=str(path)
        try:
            ident = source_identity(path)
            key = digest([ident, sorted(self.coins), PROJECTION_VERSION])
            prior = self.cache.read('diff_source', key)
            if force:
                with self.cache.db:
                    self.cache.db.execute('DELETE FROM diff_blocks WHERE source=?', (key,))
                prior = None
            if prior and (prior.get('complete') or (prior.get('last_height') or -1) >= self.stop_block):
                self.cache.count('DIFF_CACHE_HIT', source=str(path), line=prior['line'])
                known={p['issue_id'] for p in self.issues}
                for row in self.cache.db.execute('SELECT body,sha FROM diff_blocks WHERE source=? AND height IS NULL',(key,)):
                    if hashlib.sha256(row['body']).hexdigest()!=row['sha']:continue
                    value=json.loads(row['body']);problem=value.get('error')
                    if problem and problem['issue_id'] not in known:
                        self.issues.append(problem);known.add(problem['issue_id'])
                if prior.get('decode_error') and prior['decode_error']['issue_id'] not in known:
                    self.issues.append(prior['decode_error'])
                return key, prior
        except OSError as exc:
            self._problem('MISSING_DIFF_FILE', str(exc), path)
            return None, None
        member = ''
        line_no = 0
        last = None
        resume_line = prior['line'] if prior else 0
        if prior:
            last = prior.get('last_height')
        state = dict(source=str(path), member='', line=resume_line,
                     first_height=prior.get('first_height') if prior else None,
                     last_height=last, complete=False, source_identity=ident)
        with self.cache.db:
            self.cache.db.execute('DELETE FROM diff_blocks WHERE source=? AND line>?',(key,resume_line))
        self.cache.count('DIFF_DECODE_START', source=str(path), resume_line=resume_line,
                         note='Compressed prefixes may be decompressed again after interruption; committed blocks are not parsed twice')
        try:
            member = member_name(path)
            state['member'] = member
            with open_json_text(str(path)) as stream:
                for line_no, raw in enumerate(stream, 1):
                    if line_no <= resume_line:
                        continue
                    if not raw.strip():
                        state['line'] = line_no
                        continue
                    height = None
                    known_end=None
                    try:
                        value = loads_exact_selected(raw, self.coins)
                        if not isinstance(value, dict) or 'gap' in value:
                            if isinstance(value,dict) and isinstance(value.get('gap'),dict):
                                known_end=value['gap'].get('end_block')
                            raise ValueError('Gap marker or non-block input')
                        height = value.get('block_number')
                        if type(height) is not int or height < 0 or height > 2**63 - 1:
                            raise ValueError('Invalid block_number')
                        if height > self.stop_block:
                            state['stopped_at_target'] = True
                            break
                        if not isinstance(value.get('events'), list):
                            raise ValueError('Block events must be an array')
                        chosen, positions = [], []
                        for index, event in enumerate(value['events']):
                            if not isinstance(event, dict) or not isinstance(event.get('coin'), str) or event['coin'] in self.coins:
                                chosen.append(event)
                                positions.append(index)
                        value = dict(block_number=height, block_time=value.get('block_time'),
                                     events=chosen, source_event_indices=positions,
                                     source_file=str(path), source_member=member, source_line=line_no)
                        last = height
                        state['last_height'] = height
                        if state['first_height'] is None:
                            state['first_height'] = height
                    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
                        problem = self._problem('UNREADABLE_DIFF_LINE', str(exc), path,
                                                source_member=member, source_line=line_no,
                                                block_number=height if type(height) is int else '')
                        value = dict(error=problem, previous_height=last,
                                     known_end=height if type(height) is int else known_end)
                        height = None
                    body = encode(value)
                    self.cache.db.execute('INSERT OR REPLACE INTO diff_blocks VALUES (?,?,?,?,?)',
                                          (key, line_no, height, body, hashlib.sha256(body).hexdigest()))
                    state['line'] = line_no
                    if line_no % 512 == 0:
                        with self.cache.db:
                            self.cache.write('diff_source', key, state)
                        from state_store import fault
                        fault('diff_prefix_committed',source=str(path),line=line_no)
                    self.log.heartbeat('diff_decode', str(path), line_no)
                else:
                    state['complete'] = True
            try:
                after=source_identity(path)
            except OSError as changed:
                raise BookError('SOURCE_CHANGED',str(changed),str(path)) from changed
            if after != ident:
                with self.cache.db:
                    self.cache.db.execute('DELETE FROM diff_blocks WHERE source=?', (key,))
                    self.cache.db.execute("DELETE FROM entries WHERE kind='diff_source' AND key=?", (key,))
                raise BookError('SOURCE_CHANGED', 'Diff changed during decoding; prefix discarded', str(path))
        except (OSError, EOFError, UnicodeError, InputProblem, zipfile.BadZipFile, ValueError) as exc:
            state['decode_error'] = self._problem('UNREADABLE_DIFF_FILE', str(exc), path,
                                                  source_member=member, source_line=line_no)
            state['complete'] = True
            # The verified prefix stays usable for a target already complete before the fault.
            try:
                unchanged=source_identity(path)==ident
            except OSError:
                unchanged=False
            if not unchanged:
                with self.cache.db:
                    self.cache.db.execute('DELETE FROM diff_blocks WHERE source=?', (key,))
                    self.cache.db.execute("DELETE FROM entries WHERE kind='diff_source' AND key=?",(key,))
                raise BookError('SOURCE_CHANGED', 'Diff changed during failed decoding', str(path))
        with self.cache.db:
            self.cache.write('diff_source', key, state)
        self.log.info('DIFF_DECODE_BOUNDARY', source=str(path), **{k:state[k] for k in ('line','first_height','last_height','complete')})
        return key, state

    def source_rows(self, path, start, target):
        from hlp_la_screen import BookError
        key, state = self.ensure(path)
        if key is None:
            return
        # Check the small projected cache, never rescan the raw file merely for a hit.
        corrupt = False
        relevant='source=? AND ((height>? AND height<=?) OR height IS NULL)'
        for row in self.cache.db.execute('SELECT body,sha FROM diff_blocks WHERE '+relevant, (key,start,target)):
            if hashlib.sha256(row['body']).hexdigest() != row['sha']:
                corrupt = True
                break
        if corrupt:
            self.cache.count('DIFF_CACHE_INVALID', source=str(path))
            key, state = self.ensure(path, force=True)
        for row in self.cache.db.execute('SELECT * FROM diff_blocks WHERE '+relevant+' ORDER BY line', (key,start,target)):
            value = json.loads(row['body'])
            if 'error' in value:
                previous = value.get('previous_height')
                if type(value.get('known_end')) is int and value['known_end']<=start:
                    continue
                later = self.cache.db.execute('SELECT height FROM diff_blocks WHERE source=? AND line>? AND height IS NOT NULL ORDER BY line LIMIT 1', (key, row['line'])).fetchone()
                if (previous is not None and previous >= target) or (later and later[0] <= start):
                    continue
                problem = value['error']
                error=BookError(problem['code'],problem['message'],f"{path}:line={row['line']}")
                error.input_position={k:problem[k] for k in ['source_file','source_member','source_line','event_index','block_number']}
                raise error
            height = value['block_number']
            if height <= start:
                continue
            if height > target:
                return
            used=next((v for v in self.used if v['file']==str(path)),None)
            if used is None:
                used=dict(file=str(path),member=state['member'],first_block=height,last_block=height)
                self.used.append(used)
            used['last_block']=height
            self.current_block=value
            yield value
            if height == target:
                return

    def between(self, ref, target, tn_ms):
        from hlp_la_screen import parse_time, InputProblem
        from input_discovery import HOUR, locate_hour
        self.used = []
        chunks = self.config.get('diffs', {}).get('chunks')
        if chunks is not None:
            paths = (Path(c['path']) for c in chunks if c.get('end_block', target) > ref.block_number and c.get('start_block', 0) <= target)
        else:
            first = max(parse_time(self.config['data_start']), ref.time_ms // HOUR * HOUR - HOUR)
            end = min(parse_time(self.config['data_end_exclusive']), tn_ms // HOUR * HOUR + 2 * HOUR)
            def hourly():
                for hour in range(first, end, HOUR):
                    try:
                        yield locate_hour(self.config['roots']['diffs'], hour)
                    except (OSError, InputProblem) as exc:
                        self._problem('AMBIGUOUS_DIFF_HOUR', str(exc), self.config['roots']['diffs'], hour=hour)
            paths = hourly()
        for path in paths:
            for block in self.source_rows(path, ref.block_number, target):
                yield block
                if block['block_number'] == target:
                    return
