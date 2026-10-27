"""Normalize resting-order storage only; all units and replay remain in the original engine."""

ARENA_FIELDS = {'halfs','oid_to_key','bod'}


def _integer(value, name):
    if type(value) is not int or value < 0:
        raise ValueError('Arena '+name+' must be a non-negative integer')
    return value


def normalize_book(raw):
    """Decode the arena layout into the legacy asset/book_orders contract.

    Occupied slots, the OID index and both resting-price chains must identify
    exactly the same orders. Free slots and account/trigger state are never
    projected into L4. Zero remaining size is retained for a later remove diff.
    """
    if 'book_orders' in raw:
        if ARENA_FIELDS.intersection(raw):
            raise ValueError('Ambiguous legacy/arena book storage')
        if not isinstance(raw['book_orders'],dict):
            raise ValueError('Legacy book_orders must be a map')
        return raw, 'book_orders'
    if not ARENA_FIELDS.issubset(raw):
        raise ValueError('Unsupported book storage: expected book_orders or halfs/oid_to_key/bod')
    bod=raw['bod'];halfs=raw['halfs'];index=raw['oid_to_key']
    if not isinstance(bod,dict) or not isinstance(bod.get('e'),list):
        raise ValueError('Arena bod.e must be a list')
    if not isinstance(halfs,list) or len(halfs)!=2 or not isinstance(index,list):
        raise ValueError('Arena requires two halfs and an oid_to_key pair list')
    entries=bod['e'];orders={}
    for slot,entry in enumerate(entries):
        if not isinstance(entry,dict) or set(entry) not in ({'o'},{'v'}):
            raise ValueError('Arena slot must be occupied o or vacant v')
        if 'o' in entry:
            if not isinstance(entry['o'],dict):raise ValueError('Arena occupied order must be a map')
            orders[slot]=entry['o']
    if _integer(bod.get('l'),'occupied count')!=len(orders):
        raise ValueError('Arena occupied count differs from bod.l')
    seen_oids=set();seen_slots=set()
    for pair in index:
        if not isinstance(pair,list) or len(pair)!=2:raise ValueError('Arena OID index must contain pairs')
        oid=_integer(pair[0],'oid');slot=_integer(pair[1],'index slot')
        if oid in seen_oids or slot in seen_slots:raise ValueError('Duplicate arena OID/index slot')
        if slot not in orders or orders[slot].get('o')!=oid:
            raise ValueError('Arena OID index does not match an occupied order')
        seen_oids.add(oid);seen_slots.add(slot)
    if seen_slots!=set(orders):raise ValueError('Arena occupied slots and OID index differ')
    linked=set()
    for side,half in zip(('B','A'),halfs):
        if not isinstance(half,dict) or not isinstance(half.get('levels'),list):
            raise ValueError('Arena half.levels must be a list')
        prices=set()
        for level in half['levels']:
            if not isinstance(level,list) or len(level)!=2:raise ValueError('Arena price level must be a pair')
            key,ends=level
            if type(key) is not int or key==0 or (key<0)!=(side=='B') or key in prices:
                raise ValueError('Invalid/duplicate arena side-price key')
            prices.add(key)
            if not isinstance(ends,dict) or set(ends)!={'f','l'}:raise ValueError('Arena level needs first/last slots')
            current=_integer(ends['f'],'first slot');last=_integer(ends['l'],'last slot');previous=None
            while True:
                if current not in orders or current in linked:raise ValueError('Arena chain points to free/repeated slot')
                order=orders[current];common=order.get('c')
                if not isinstance(common,dict) or common.get('s')!=side or common.get('l')!=abs(key):
                    raise ValueError('Arena chain side/price disagrees with order')
                if order.get('p')!=previous:raise ValueError('Arena previous link mismatch')
                linked.add(current)
                if current==last:
                    if order.get('n') is not None:raise ValueError('Arena final order has a next link')
                    break
                previous,current=current,_integer(order.get('n'),'next slot')
    if linked!=set(orders):raise ValueError('Arena chains do not cover exactly the occupied orders')
    return dict(asset=raw['asset'],book_orders=orders), 'arena'
