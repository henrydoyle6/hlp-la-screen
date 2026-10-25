"""Structural RMP layout adapters; never infer state metadata from file names."""
HEADER_VERSION = 'rmp-header-layout-v3'


class ProbeReader:
    def __init__(self, stream, limit=None):
        self.stream, self.limit, self.count = stream, limit, 0

    def read(self, size=-1):
        if self.limit is not None:
            if self.count >= self.limit:
                raise ValueError('Snapshot layout not found within bounded header probe')
            size = min(size if size >= 0 else self.limit, self.limit-self.count)
        value = self.stream.read(size)
        self.count += len(value)
        return value


def detect_layout(path, opener, limit):
    """Cheap dispatch only. A locus context can be hundreds of MB into a ZIP."""
    import msgpack
    with opener(path) as stream:
        reader = ProbeReader(stream, limit)
        u = msgpack.Unpacker(reader, raw=False, strict_map_key=False, read_size=4096,
                             max_buffer_size=max(8192,limit*2))
        for _ in range(u.read_map_header()):
            if u.unpack() != 'exchange':
                u.skip();continue
            for _ in range(u.read_map_header()):
                key = u.unpack()
                if key == 'context':return 'legacy',reader.count
                if key == 'locus':return 'locus',reader.count
                u.skip()
    raise ValueError('Unsupported snapshot: exchange context/locus missing')


def locus_header(path, opener):
    """Stream to the real ctx, skipping large cls/scl states without materializing them."""
    import msgpack
    from hlp_la_screen import parse_time
    with opener(path) as stream:
        reader = ProbeReader(stream)
        u = msgpack.Unpacker(reader, raw=False, strict_map_key=False,
                             read_size=65536,max_buffer_size=2**31-1)
        for _ in range(u.read_map_header()):
            if u.unpack() != 'exchange':u.skip();continue
            for _ in range(u.read_map_header()):
                if u.unpack() != 'locus':u.skip();continue
                for _ in range(u.read_map_header()):
                    if u.unpack() != 'ctx':u.skip();continue
                    offset=u.tell();value=u.unpack()
                    if not isinstance(value,dict) or type(value.get('height')) is not int or value['height']<0:
                        raise ValueError('ABCI exchange.locus.ctx.height must be a non-negative integer')
                    return dict(block_number=value['height'],time_ms=parse_time(value['time']),
                        header_bytes_read=reader.count,context_offset=offset,schema='locus')
    raise ValueError('ABCI exchange.locus.ctx missing')


def market_name(kind, meta, asset):
    """Identify demand before converting units. Unnamed unrelated spot entries are valid."""
    if type(asset) is not int:raise ValueError('Book asset must be integer')
    if not isinstance(meta,dict):raise ValueError(f'Missing {kind} metadata for asset {asset}')
    if kind=='perp':
        pdi=meta.get('pdi')
        if type(pdi) is not int:raise ValueError('Perp metadata pdi must be integer')
        index=asset-10000*pdi;values=meta['universe']
    else:index=asset;values=meta['spot_infos']
    if not 0<=index<len(values):raise ValueError(f'{kind} asset outside metadata: {asset}')
    info=values[index]
    if not isinstance(info,dict):raise ValueError(f'Invalid {kind} metadata entry for asset {asset}')
    name=info.get('name')
    return name if isinstance(name,str) and name else None
