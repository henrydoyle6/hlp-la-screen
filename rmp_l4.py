"""One-pass, metadata-driven RMP extraction; no replay/LA/comparison code.

Only requested coin JSONs are published. If metadata follows its books (as spot
metadata does in the supplied states), bounded temporary MessagePack spools are
used and deleted before returning. These are never loader inputs or cache hits.
"""
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

CONVERSION_VERSION = 'rmp-l4-integer-v2'


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


def scan_rmp(ref, requested, staging, log):
    """Consume the original source exactly once; keep at most one raw book in RAM."""
    import msgpack
    outputs={};errors={};seen=set();context=None;spot_meta=None;spot_pending=[]
    def process(raw, kind, meta):
        coin,sz,px=market_info(kind,meta,raw['asset'])
        if coin in seen:raise ValueError(f'Duplicate market: {coin}')
        seen.add(coin)
        if coin not in requested:return
        try:
            value=to_l4(raw,coin,sz,px,ref.block_number)
            p=staging/(digest(coin)+'.json');save(p,value)
            outputs[coin]={'path':p,'asset':raw['asset'],'kind':kind,
                'size_decimals':sz,'price_decimals':px,'orders':len(value['orders'])}
        except (ValueError,KeyError,TypeError) as exc:
            errors[coin]=ExtractionError('RMP_COIN_CONVERSION_FAILED',exc,ref.path)
    def read_books(u, kind, meta, pending):
        for _ in range(u.read_array_header()):
            raw=selected_map(u,{'asset','book_orders'})
            if set(raw)!={'asset','book_orders'} or not isinstance(raw['book_orders'],dict):
                raise ValueError('Missing asset/book_orders in snapshot')
            if meta is not None:process(raw,kind,meta)
            else:
                p=staging/('spool_'+str(len(pending))+'_'+kind+'.msgpack')
                with p.open('wb') as f:msgpack.pack(raw,f,use_bin_type=True)
                pending.append(p)
            log.heartbeat('rmp_extract',ref.path,len(seen)+len(pending))
    def finish_pending(pending,kind,meta):
        if pending and meta is None:raise ValueError(f'Missing {kind} metadata')
        for p in pending:
            with p.open('rb') as f:raw=msgpack.unpack(f,raw=False,strict_map_key=False)
            process(raw,kind,meta);p.unlink()
        pending.clear()
    with source_stream(ref.path) as f:
        reader=HashReader(f)
        u=msgpack.Unpacker(reader,raw=False,strict_map_key=False,max_buffer_size=2**31-1)
        found_exchange=False
        for _ in range(u.read_map_header()):
            key=u.unpack()
            if key!='exchange':u.skip();continue
            if found_exchange:raise ValueError('Duplicate exchange')
            found_exchange=True
            for _ in range(u.read_map_header()):
                key=u.unpack()
                if key=='context':context=u.unpack()
                elif key=='perp_dexs':
                    for dex in range(u.read_array_header()):
                        meta=None;pending=[]
                        for _ in range(u.read_map_header()):
                            key=u.unpack()
                            if key=='clearinghouse':
                                meta=selected_map(u,{'meta'})['meta']
                                finish_pending(pending,'perp',meta)
                            elif key=='books':read_books(u,'perp',meta,pending)
                            else:u.skip()
                        finish_pending(pending,'perp',meta)
                elif key=='spot_books':read_books(u,'spot',spot_meta,spot_pending)
                elif key=='spot_clearinghouse':
                    spot_meta=selected_map(u,{'meta'})['meta']
                    finish_pending(spot_pending,'spot',spot_meta)
                else:u.skip()
        finish_pending(spot_pending,'spot',spot_meta)
        parsed_end=u.tell()
        try:u.unpack()
        except msgpack.OutOfData:pass
        else:raise ValueError('Trailing object after snapshot')
        # OutOfData may also mean trailing incomplete bytes, which must fail.
        if parsed_end!=reader.byte_count:
            raise ValueError('Trailing/unconsumed snapshot bytes')
        content_sha=reader.sha.hexdigest()
    if context is None or context['height']!=ref.block_number or time_ms(context['time'])!=ref.time_ms:
        raise ValueError('Embedded snapshot height/time differs from selected source metadata')
    for coin in requested-outputs.keys()-errors.keys():
        errors[coin]=ExtractionError('SNAPSHOT_COIN_MISSING',f'No book for {coin}',ref.path)
    return outputs,errors,content_sha


class ExtractionCache:
    def __init__(self, root, log, version=CONVERSION_VERSION):
        self.root,self.log,self.version=Path(root),log,version
        self.attempted={};self.identities={};self.stats={'rmp_scans':0,'extract_hits':0,'extract_misses':0,'extract_written':0}

    def check_identity(self, ref):
        try:
            if identity(ref)!=self.identities.get(ref):
                raise ExtractionError('SNAPSHOT_CHANGED','Selected snapshot identity changed',ref.path)
        except OSError as exc:raise ExtractionError('SNAPSHOT_READ_ERROR',exc,ref.path) from exc

    def ensure(self, ref, coins, validator=None):
        requested=set(coins)
        try:
            source_id=identity(ref)
        except OSError as exc:raise ExtractionError('SNAPSHOT_READ_ERROR',exc,ref.path) from exc
        self.identities[ref]=source_id
        source_key=digest({'source':source_id,'conversion_version':self.version})
        directory=self.root/source_key
        results={};missing=set()
        for coin in sorted(requested):
            key=digest(coin);p=directory/(key+'.json');receipt=directory/(key+'.success.json')
            try:
                record=json.loads(receipt.read_text(encoding='utf-8'))
                if (record['source_identity']!=source_id or record['coin']!=coin or
                    record['conversion_version']!=self.version or record['json_sha256']!=file_hash(p)):
                    raise ValueError('Cache receipt/hash mismatch')
                if validator:validator(p,coin)
                results[coin]=p;self.stats['extract_hits']+=1
                self.log.info('EXTRACT_CACHE_HIT',source=ref.path,coin=coin,file=str(p))
            except Exception:
                missing.add(coin);self.stats['extract_misses']+=1
                self.log.info('EXTRACT_CACHE_MISS',source=ref.path,coin=coin)
        if not missing:return results
        if source_key in self.attempted:
            for coin in missing:
                results[coin]=self.attempted[source_key].get(coin,ExtractionError(
                    'RMP_ALREADY_SCANNED','Plan all required coins before the single source scan',ref.path))
            return results
        self.attempted[source_key]={}
        self.root.mkdir(parents=True,exist_ok=True)
        self.stats['rmp_scans']+=1
        self.log.info('RMP_SCAN_START',source=ref.path,coins=sorted(missing),scan_number=self.stats['rmp_scans'],source_key=source_key)
        try:
            with tempfile.TemporaryDirectory(prefix='.staging_',dir=self.root) as temp:
                staging=Path(temp)
                outputs,errors,content_sha=scan_rmp(ref,missing,staging,self.log)
                self.check_identity(ref)
                directory.mkdir(parents=True,exist_ok=True)
                for coin,item in outputs.items():
                    p=item['path']
                    try:
                        if validator:validator(p,coin)
                    except Exception as exc:
                        errors[coin]=ExtractionError('RMP_L4_VALIDATION_FAILED',exc,ref.path)
                        continue
                    key=digest(coin);target=directory/(key+'.json');receipt=directory/(key+'.success.json')
                    record={**{k:v for k,v in item.items() if k!='path'},'source_identity':source_id,
                        'coin':coin,'conversion_version':self.version,'rmp_sha256':content_sha,'json_sha256':file_hash(p)}
                    staged_receipt=staging/(key+'.success.json');save(staged_receipt,record)
                    # Receipt is the commit marker. Orphan JSONs never count as hits.
                    os.replace(p,target);os.replace(staged_receipt,receipt)
                    results[coin]=target;self.stats['extract_written']+=1
                    self.log.info('EXTRACT_CACHE_WRITE',source=ref.path,coin=coin,file=str(target),orders=item['orders'])
                results.update(errors)
                self.attempted[source_key]=dict(results)
            self.log.info('RMP_SCAN_DONE',source=ref.path,coins_written=sum(not isinstance(v,ExtractionError) for c,v in results.items() if c in missing),failed_coins=sorted(errors),rmp_sha256=content_sha,**self.stats)
            return results
        except Exception as exc:
            code=getattr(exc,'code','RMP_EXTRACTION_FAILED')
            error=ExtractionError(code,exc,ref.path)
            self.attempted[source_key]={coin:error for coin in missing}
            self.log.warning('RMP_SCAN_FAILED',source=ref.path,reason=str(exc),**self.stats)
            raise error from exc
