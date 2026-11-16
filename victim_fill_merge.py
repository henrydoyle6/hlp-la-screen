"""Research history merge; never merge distinct native trade identities."""
import hashlib,json
from collections import Counter,defaultdict
from victim_entry_math import dec

def legacy_fill(fill):
    return bool(fill.get('legacy_source_identity')) or str(fill.get('tid','')).startswith(('legacy_source_row:','legacy_raw_row:'))

def native_identity(user,coin,stamp,fill):
    return hashlib.sha256(json.dumps([user,coin,stamp,str(fill['tid']),str(fill.get('oid')),fill['side']],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def merge_overlay(records,additional,policy):
    records=[{**r,'f':dict(r['f'])} for r in records]
    ids={r['id']:r for r in records};by_key=defaultdict(list);stats=Counter();used=defaultdict(set)
    def key(f):return (f['time'],str(f.get('hash','')).lower(),f['side'],dec(f['px']))
    for r in records:by_key[key(r['f'])].append(r)
    for source_id,user,coin,stamp,body,source,line,digest in additional:
        f=json.loads(body);legacy=legacy_fill(f);ident=native_identity(user,coin,stamp,f)
        found=ids.get(ident) if not legacy else None
        if found is None:
            # A financial fingerprint aliases a source without a native trade
            # ID. It must never collapse two different native trade IDs.
            possible=[r for r in by_key[key(f)] if (legacy or legacy_fill(r['f']))
                and r['id'] not in used[source] and policy.equal(r['f']['sz'],f['sz'])
                and policy.equal(r['f']['startPosition'],f['startPosition'])
                and (r['f'].get('oid') is None or f.get('oid') is None or str(r['f']['oid'])==str(f['oid']))]
            if len(possible)==1:found=possible[0]
            elif len(possible)>1:
                # Preserve uncertainty rather than silently dropping a possible
                # extra fill and calling the hour complete.
                for r in possible:r['conflict']=1
                stats['ambiguous_legacy_alias_rows']+=1
                continue
        if found is not None:
            if not legacy:ids[ident]=found
            used[source].add(found['id']);prior=found['f'];bad=[]
            for name in ['px','sz','startPosition','side','time','hash']:
                if name in ['sz','startPosition']:
                    if not policy.equal(prior[name],f[name]):bad.append(name)
                elif name=='px':
                    if dec(prior[name])!=dec(f[name]):bad.append(name)
                elif str(prior.get(name)).lower()!=str(f.get(name)).lower():bad.append(name)
            if bad:found['conflict']=1;stats['financial_conflicts']+=1
            else:
                for name in ['sz','startPosition']:
                    difference=abs(dec(prior[name])-dec(f[name]))
                    if difference:
                        stats['quantity_alias_tail_comparisons']+=1
                        stats['max_quantity_alias_tail']=str(max(dec(stats['max_quantity_alias_tail']),difference))
                for name in ['native_order','block_number','event_index']:
                    if f.get(name) is not None:prior[name]=f[name]
                stats['matched_rows_with_native_order_verified']+=1
            continue
        r={'id':ident if not legacy else 'legacy_raw_row:'+source_id,'f':f,'source':source,'source_line':line,'conflict':0}
        records.append(r);ids[r['id']]=r;by_key[key(f)].append(r);used[source].add(r['id']);stats['new_raw_fills']+=1
    return sorted(records,key=lambda r:r['f']['time'],reverse=True),dict(stats)
