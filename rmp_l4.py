"""Shared native RMP adapter; metadata-driven integer units, SQLite-only cache."""
from __future__ import annotations
import bz2
import contextlib
from datetime import datetime, timezone
import gzip
import hashlib
import json
import lzma
import os
from pathlib import Path
import tempfile
import zipfile

CONVERSION_VERSION = 'rmp-l4-sqlite-v3'


class ExtractionError(RuntimeError):
    def __init__(self, code, message, source=''):
        self.code, self.message, self.source = code, str(message), str(source)
        super().__init__(self.message)


def is_rmp(path):
    name = str(path).lower()
    return name.endswith('.rmp') or any(name.endswith('.rmp'+s) for s in ('.zip','.gz','.bz2','.xz','.lzma','.zst','.lz4'))


def source_identity(path):
    """Stat plus compressed/raw edge fingerprints; no full source re-scan.

    Edge hashes also detect rapid same-size rewrites on coarse-time filesystems.
    Raw inputs are expected to remain immutable during and between cached runs.
    """
    p=Path(path).resolve();s=p.stat();h=hashlib.sha256()
    with p.open('rb') as f:
        h.update(f.read(65536))
        if s.st_size>65536:
            f.seek(max(65536,s.st_size-65536));h.update(f.read(65536))
    after=p.stat()
    if (s.st_size,s.st_mtime_ns,s.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
        raise OSError('Input changed during identity probe')
    return {'path':os.path.normcase(str(p)),'size':s.st_size,'mtime_ns':s.st_mtime_ns,
            'ctime_ns':s.st_ctime_ns,'device':s.st_dev,'inode':s.st_ino,'edge_sha256':h.hexdigest()}


def identity(ref):
    return {**source_identity(ref.path),'block_number':ref.block_number,'time_ms':ref.time_ms}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()


def save(path, value):
    with Path(path).open('w',encoding='utf-8',newline='\n') as f:
        json.dump(value,f,ensure_ascii=False,separators=(',',':'))
        f.write('\n');f.flush();os.fsync(f.fileno())


def scaled_integer(value, decimals):
    if type(value) is not int or value<0 or type(decimals) is not int or decimals<0:
        raise ValueError(f'Invalid integer/precision: {value!r}/{decimals!r}')
    digits=str(value).zfill(decimals+1)
    return (digits[:-decimals]+'.'+digits[-decimals:]).rstrip('0').rstrip('.') if decimals else digits


def time_ms(value):
    dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
    delta=dt.astimezone(timezone.utc)-datetime(1970,1,1,tzinfo=timezone.utc)
    return (delta.days*86400+delta.seconds)*1000+delta.microseconds//1000


@contextlib.contextmanager
def source_stream(path):
    p=Path(path);suffix=p.suffix.lower()
    if suffix=='.zip':
        with zipfile.ZipFile(p) as z:
            members=[m for m in z.infolist() if not m.is_dir()]
            if len(members)!=1 or not members[0].filename.lower().endswith('.rmp'):
                raise ValueError('ABCI ZIP must contain exactly one .rmp member')
            with z.open(members[0]) as f: yield f
    elif suffix in ('.gz','.bz2','.xz','.lzma'):
        opener={'.gz':gzip.open,'.bz2':bz2.open,'.xz':lzma.open,'.lzma':lzma.open}[suffix]
        with opener(p,'rb') as f: yield f
    elif suffix=='.zst':
        import zstandard
        with zstandard.open(p,'rb') as f: yield f
    elif suffix=='.lz4':
        import lz4.frame
        with lz4.frame.open(p,'rb') as f: yield f
    else:
        with p.open('rb') as f: yield f


class HashReader:
    def __init__(self, stream): self.stream,self.sha,self.byte_count=stream,hashlib.sha256(),0
    def read(self, n=-1):
        data=self.stream.read(n);self.sha.update(data);self.byte_count+=len(data);return data


def selected_map(u, keep):
    found={}
    for _ in range(u.read_map_header()):
        key=u.unpack()
        if key in keep:
            if key in found: raise ValueError(f'Duplicate map key: {key}')
            found[key]=u.unpack()
        else:u.skip()
    return found


def market_info(kind, meta, asset):
    if type(asset) is not int: raise ValueError('Book asset must be integer')
    if kind=='perp':
        local=asset-10000*meta['pdi']
        if not 0<=local<len(meta['universe']):raise ValueError(f'Perp asset outside metadata: {asset}')
        info=meta['universe'][local]
        name,sz=info['name'],info['szDecimals'];px=6-sz
    else:
        if not 0<=asset<len(meta['spot_infos']):raise ValueError(f'Spot asset outside metadata: {asset}')
        info=meta['spot_infos'][asset]
        name=info['name'];sz=meta['token_infos'][info['tokens'][0]]['spec']['szDecimals'];px=8-sz
    if not isinstance(name,str) or not name or type(sz) is not int or sz<0 or px<0:
        raise ValueError('Invalid market metadata')
    return name,sz,px


def to_l4(raw, coin, sz_dec, px_dec, height):
    orders=[];seen=set()
    for order in raw['book_orders'].values():
        oid,side=order['o'],order['c']['s']
        if type(oid) is not int or oid<0 or oid in seen or side not in ('A','B'):
            raise ValueError(f'Invalid/duplicate order: {coin}/{oid}')
        seen.add(oid)
        px=scaled_integer(order['c']['l'],px_dec)
        if px=='0':raise ValueError(f'Zero resting price: {coin}/{oid}')
        user=order.get('u','')
        if isinstance(user,bytes):user=user.hex()
        orders.append({'coin':coin,'oid':str(oid),'side':side,'limitPx':px,
                       'sz':scaled_integer(order['r'],sz_dec),'isTrigger':False,'user':user})
    return {'block_number':height,'state_point':'block_end','coin':coin,'orders':orders}


def scan_rmp(ref, requested, cache, log):
    """One sequential source scan; late metadata spools one market per SQLite row."""
    import msgpack
    from state_store import text
    scan = digest(identity(ref))
    outputs, errors, seen = {}, {}, set()
    context = None
    spot_meta = None
    db = cache.db
    with db:
        db.execute('DELETE FROM rmp_pending WHERE scan=?', (scan,))

    def process(raw, kind, meta):
        coin, sz, px = market_info(kind, meta, raw['asset'])
        if coin in seen:
            raise ValueError(f'Duplicate market: {coin}')
        seen.add(coin)
        if requested is not None and coin not in requested:
            return
        try:
            value = to_l4(raw, coin, sz, px, ref.block_number)
            # Conversion failures are per-market; successes publish only after source validation.
            cache.write('rmp_staging', scan + ':' + coin, value)
            outputs[coin] = dict(asset=raw['asset'], kind=kind, size_decimals=sz,
                                 price_decimals=px, orders=len(value['orders']))
        except (ValueError, KeyError, TypeError) as exc:
            errors[coin] = ExtractionError('RMP_COIN_CONVERSION_FAILED', exc, ref.path)

    def finish(family, kind, meta):
        pending = db.execute('SELECT position,body FROM rmp_pending WHERE scan=? AND family=? ORDER BY position', (scan, family))
        for row in pending:
            if meta is None:
                raise ValueError(f'Missing {kind} metadata')
            process(msgpack.unpackb(row['body'], raw=False, strict_map_key=False), kind, meta)
        db.execute('DELETE FROM rmp_pending WHERE scan=? AND family=?', (scan, family))

    def books(unpacker, kind, meta, family):
        for position in range(unpacker.read_array_header()):
            raw = selected_map(unpacker, {'asset', 'book_orders'})
            if set(raw) != {'asset', 'book_orders'} or not isinstance(raw['book_orders'], dict):
                raise ValueError('Missing asset/book_orders in snapshot')
            if meta is None:
                db.execute('INSERT INTO rmp_pending VALUES (?,?,?,?)',
                           (scan, family, position, msgpack.packb(raw, use_bin_type=True)))
            else:
                process(raw, kind, meta)
            # Staging is explicitly not a cache success. Bounded writes keep RAM limited.
            db.commit()
            log.heartbeat('rmp_extract', ref.path, len(seen) + position)

    try:
        with source_stream(ref.path) as stream:
            reader = HashReader(stream)
            unpacker = msgpack.Unpacker(reader, raw=False, strict_map_key=False, max_buffer_size=2**31 - 1)
            found_exchange = False
            for _ in range(unpacker.read_map_header()):
                if unpacker.unpack() != 'exchange':
                    unpacker.skip()
                    continue
                if found_exchange:
                    raise ValueError('Duplicate exchange')
                found_exchange = True
                for _ in range(unpacker.read_map_header()):
                    key = unpacker.unpack()
                    if key == 'context':
                        context = unpacker.unpack()
                    elif key == 'perp_dexs':
                        for dex in range(unpacker.read_array_header()):
                            meta, family = None, 'perp:' + str(dex)
                            for _ in range(unpacker.read_map_header()):
                                name = unpacker.unpack()
                                if name == 'clearinghouse':
                                    meta = selected_map(unpacker, {'meta'})['meta']
                                    finish(family, 'perp', meta)
                                elif name == 'books':
                                    books(unpacker, 'perp', meta, family)
                                else:
                                    unpacker.skip()
                            finish(family, 'perp', meta)
                    elif key == 'spot_books':
                        books(unpacker, 'spot', spot_meta, 'spot')
                    elif key == 'spot_clearinghouse':
                        spot_meta = selected_map(unpacker, {'meta'})['meta']
                        finish('spot', 'spot', spot_meta)
                    else:
                        unpacker.skip()
            finish('spot', 'spot', spot_meta)
            parsed_end = unpacker.tell()
            try:
                unpacker.unpack()
            except msgpack.OutOfData:
                pass
            else:
                raise ValueError('Trailing object after snapshot')
            if parsed_end != reader.byte_count:
                raise ValueError('Trailing/unconsumed snapshot bytes')
            content_sha = reader.sha.hexdigest()
        if context is None or context['height'] != ref.block_number or time_ms(context['time']) != ref.time_ms:
            raise ValueError('Embedded snapshot height/time differs from selected metadata')
        if requested is not None:
            for coin in requested - outputs.keys() - errors.keys():
                errors[coin] = ExtractionError('SNAPSHOT_COIN_MISSING', f'No book for {coin}', ref.path)
        return outputs, errors, content_sha
    except BaseException:
        db.rollback()
        with db:
            db.execute('DELETE FROM rmp_pending WHERE scan=?', (scan,))
            db.execute("DELETE FROM entries WHERE kind='rmp_staging' AND key LIKE ?", (scan + ':%',))
        raise


class ExtractionCache:
    def __init__(self, cache, log, version=CONVERSION_VERSION):
        self.cache, self.log, self.version = cache, log, version
        self.stats = dict(rmp_scans=0, extract_hits=0, extract_misses=0, extract_written=0)

    def ensure(self, ref, coins):
        import msgpack
        from hlp_la_screen import _book_from_payload
        try:
            ident = identity(ref)
        except OSError as exc:
            return {coin: ExtractionError('SNAPSHOT_READ_ERROR', exc, ref.path) for coin in coins}
        source = digest(ident)
        result, missing = {}, set()
        for coin in sorted(coins):
            key = digest([source, coin, self.version])
            value = self.cache.read('extract', key)
            if value is not None:
                try:
                    _book_from_payload(value, ref, coin)
                    result[coin] = key
                    self.stats['extract_hits'] += 1
                    self.cache.count('EXTRACTION_CACHE_HIT', source=ref.path, coin=coin)
                    continue
                except Exception as exc:
                    self.log.warning('EXTRACTION_CACHE_INVALID', coin=coin, detail=str(exc))
            missing.add(coin)
        if not missing:
            return result
        self.stats['rmp_scans'] += 1
        self.stats['extract_misses'] += len(missing)
        self.cache.count('RMP_BODY_SCAN', source=ref.path, coins=sorted(missing))
        try:
            outputs, errors, content_sha = scan_rmp(ref, missing, self.cache, self.log)
            if identity(ref) != ident:
                raise ExtractionError('SOURCE_CHANGED', 'Snapshot changed during extraction', ref.path)
            for coin in outputs:
                value = self.cache.read('rmp_staging', source + ':' + coin)
                _book_from_payload(value, ref, coin)
                with self.cache.db:
                    self.cache.write('extract', digest([source, coin, self.version]), value)
                result[coin] = digest([source, coin, self.version])
                self.stats['extract_written'] += 1
            result.update(errors)
            self.log.info('RMP_BODY_COMPLETE', source=ref.path, source_sha256=content_sha,
                          markets_seen=len(outputs), requested=sorted(missing))
        except (OSError, ValueError, KeyError, TypeError, EOFError, ExtractionError, msgpack.UnpackException, zipfile.BadZipFile) as exc:
            code = getattr(exc, 'code', 'RMP_SCAN_FAILED')
            result.update({coin: ExtractionError(code, str(exc), ref.path) for coin in missing})
        finally:
            with self.cache.db:
                self.cache.db.execute('DELETE FROM rmp_pending WHERE scan=?', (source,))
                self.cache.db.execute("DELETE FROM entries WHERE kind='rmp_staging' AND key LIKE ?", (source + ':%',))
        return result


def load_book(ref, coin, cache=None, log=None):
    """Shared native path for production, extraction and independent comparison."""
    from hlp_la_screen import SnapshotRef, _book_from_payload, BookError
    from input_discovery import rmp_header
    from state_store import Cache
    class QuietLog:
        def info(self, *args, **kwargs): pass
        warning = info
        def heartbeat(self, *args): pass
    log = log or QuietLog()
    header = rmp_header(ref.path)
    if header['block_number'] != ref.block_number:
        raise BookError('SNAPSHOT_HEIGHT_MISMATCH', 'Embedded height differs from target', ref.path)
    if ref.time_ms and header['time_ms'] != ref.time_ms:
        raise BookError('SNAPSHOT_TIME_MISMATCH', 'Embedded time differs from declaration', ref.path)
    actual = SnapshotRef(ref.path, ref.block_number, header['time_ms'])
    if cache is None:
        with tempfile.TemporaryDirectory(prefix='hlp_compare_') as directory:
            store = Cache(Path(directory) / 'cache.sqlite', log)
            try:
                return load_book(actual, coin, store, log)
            finally:
                store.close()
    entry = ExtractionCache(cache, log).ensure(actual, {coin})[coin]
    if isinstance(entry, ExtractionError):
        raise BookError(entry.code, str(entry), ref.path)
    return _book_from_payload(cache.read('extract', entry), actual, coin)
