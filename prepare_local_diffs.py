"""Prepare demanded-coin block INPUTS; never replay, sort, fill gaps, or repair.

Whole source rows including empty blocks remain represented. The original reader,
replay and LA functions consume the resulting files. A source that cannot safely
be projected falls back to its original file, preserving local failure semantics.
"""
from collections import defaultdict
from contextlib import ExitStack
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import zipfile
from rmp_l4 import digest, file_hash, save, source_identity

PROJECTION_VERSION='demanded-coin-input-v2-boundary-check'


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


def prepare_diff_inputs(config, episodes, manager, log):
    options=config.get('local_diff_preparation')
    if not options:return {}
    cache=Path(options['cache_dir']);cache.mkdir(parents=True,exist_ok=True)
    scopes={}
    for ep in episodes:
        if ep.snapshot is None or ep.episode_id in manager.planning_errors or (ep.snapshot,ep.coin) in manager.extraction_errors:continue
        start,end=ep.snapshot.block_number+1,ep.last.block_number
        if ep.coin not in scopes:scopes[ep.coin]=[start,end]
        else:scopes[ep.coin]=[min(start,scopes[ep.coin][0]),max(end,scopes[ep.coin][1])]
    sections={coin:{'coverage_mode':config['diffs'].get('coverage_mode','dense'),'chunks':[]} for coin in scopes}
    log.info('DIFF_INPUT_PREPARATION_START',coins=sorted(scopes),scopes=scopes,cache_dir=str(cache),version=PROJECTION_VERSION)
    totals={'source_scans':0,'cache_hits':0,'fallback_files':0,'rows':0,'original_events':0,'selected_events':0}
    if os.name=='nt':
        import ctypes
        ctypes.CDLL('ucrtbase')._setmaxstdio(max(2048,len(scopes)+100))
    for index,chunk in enumerate(config['diffs'].get('chunks',[]),1):
        lo,hi=chunk['start_block'],chunk['end_block'];path=Path(chunk['path'])
        needed={c:(max(a,lo),min(b,hi)) for c,(a,b) in scopes.items() if a<=hi and b>=lo}
        if not needed:continue
        started=time.monotonic()
        try:
            ident=source_identity(path)
            scan_stop=max(b for a,b in scopes.values()) if config.get('auto_input_discovery') else max(b for a,b in needed.values())
            key=digest({'source':ident,'needed':needed,'scan_stop':scan_stop,'version':PROJECTION_VERSION})
            directory=cache/key;receipt=directory/'success.json'
            try:
                previous=json.loads(receipt.read_text(encoding='utf-8'))
                if previous['source_identity']!=ident or previous['version']!=PROJECTION_VERSION:raise ValueError('identity/version')
                for item in previous['coins']:
                    p=directory/item['filename']
                    if p.stat().st_size!=item['bytes'] or file_hash(p)!=item['sha256']:raise ValueError('projection cache changed')
                records=previous['coins'];totals['cache_hits']+=1
                log.info('DIFF_INPUT_CACHE_HIT',source=str(path),coins=sorted(needed),rows=previous['rows'],directory=str(directory))
            except (OSError,ValueError,KeyError,TypeError):
                totals['source_scans']+=1
                log.info('DIFF_INPUT_SOURCE_START',index=index,source=str(path),needed=needed,compressed_bytes=ident['size'])
                with tempfile.TemporaryDirectory(prefix='.projecting_',dir=cache) as temp:
                    staging=Path(temp);counts=defaultdict(int);first={};last={};rows=events=selected=0
                    hashes={coin:hashlib.sha256() for coin in needed};sizes=defaultdict(int)
                    with ExitStack() as stack:
                        handles={coin:stack.enter_context((staging/(digest(coin)+'.jsonl')).open('wb',buffering=256*1024)) for coin in needed}
                        if path.suffix.lower()=='.zip':
                            z=stack.enter_context(zipfile.ZipFile(path));members=[m for m in z.infolist() if not m.is_dir()]
                            if len(members)!=1:raise ValueError('Expected exactly one diff member')
                            stream=stack.enter_context(z.open(members[0]));member=members[0].filename
                        else:stream=stack.enter_context(path.open('rb'));member=''
                        lower=min(v[0] for v in needed.values());upper=max(v[1] for v in needed.values());previous_height=None
                        for number,raw in enumerate(stream,1):
                            if not raw.strip():continue
                            value=loads_exact_selected(raw,needed)
                            if not isinstance(value,dict) or 'gap' in value:raise ValueError(f'Cannot project gap/non-block: line {number}')
                            height=value.get('block_number')
                            if type(height) is not int:raise ValueError(f'Invalid block at line {number}')
                            if height<lower:continue
                            if height>scan_stop:break
                            if height>hi:
                                raise ValueError(f'Observed block {height} beyond discovered shard boundary {hi}; use original reader')
                            if previous_height is not None and height<=previous_height:
                                raise ValueError(f'Duplicate/unsorted original block {height} at line {number}; use original reader')
                            previous_height=height
                            original=value.get('events')
                            if not isinstance(original,list):raise ValueError(f'Invalid events at line {number}')
                            groups=defaultdict(list);indices=defaultdict(list);global_errors=[]
                            for i,event in enumerate(original):
                                if not isinstance(event,dict) or 'coin' not in event:
                                    global_errors.append((i,event));continue
                                coin=event['coin']
                                if isinstance(coin,str) and coin in needed:groups[coin].append(event);indices[coin].append(i)
                            base={k:v for k,v in value.items() if k!='events'}
                            base.setdefault('source_file',str(path));base.setdefault('source_line',number)
                            if member:base.setdefault('source_member',member)
                            empty=encode({**base,'events':[]})
                            for coin,(a,b) in needed.items():
                                if not a<=height<=b:continue
                                items=groups.get(coin,[]);positions=indices.get(coin,[])
                                if global_errors:
                                    merged=list(zip(positions,items))+global_errors
                                    merged.sort(key=lambda v:v[0]);positions=[i for i,e in merged];items=[e for i,e in merged]
                                data=encode({**base,'events':items,'source_event_indices':positions}) if items else empty
                                handles[coin].write(data);hashes[coin].update(data);sizes[coin]+=len(data)
                                counts[coin]+=len(items);selected+=len(items)
                                first.setdefault(coin,height);last[coin]=height
                            rows+=1;events+=len(original)
                            if rows%1000==0:
                                log.info('DIFF_INPUT_PROGRESS',source=str(path),source_line=number,block=height,target_block=upper,
                                         blocks_read=rows,original_events=events,selected_events=selected,
                                         output_bytes=sum(sizes.values()),elapsed_seconds=round(time.monotonic()-started,3))
                            log.heartbeat('prepare_diff_inputs',str(path),number)
                            if height==scan_stop:break
                        for h in handles.values():h.flush();os.fsync(h.fileno())
                    if source_identity(path)!=ident:raise ValueError('Source changed during projection')
                    directory.mkdir(parents=True,exist_ok=True)
                    records=[]
                    for coin,(a,b) in needed.items():
                        filename=digest(coin)+'.jsonl';p=staging/filename
                        records.append({'coin':coin,'filename':filename,'start_block':a,'end_block':b,
                                        'first_observed':first.get(coin),'last_observed':last.get(coin),
                                        'events':counts[coin],'bytes':sizes[coin],'sha256':hashes[coin].hexdigest()})
                        os.replace(p,directory/filename)
                    result={'version':PROJECTION_VERSION,'source_identity':ident,'coins':records,'rows':rows,
                            'original_events':events,'selected_events':selected,'no_blocks_synthesized':True,'event_order_preserved':True}
                    save(staging/'success.json',result);os.replace(staging/'success.json',receipt)
                totals['rows']+=rows;totals['original_events']+=events;totals['selected_events']+=selected
                log.info('DIFF_INPUT_SOURCE_DONE',source=str(path),rows=rows,original_events=events,selected_events=selected,
                         output_bytes=sum(sizes.values()),elapsed_seconds=round(time.monotonic()-started,3),directory=str(directory))
            for item in records:
                sections[item['coin']]['chunks'].append({'path':str(directory/item['filename']),
                    'start_block':item['start_block'],'end_block':item['end_block']})
        except Exception as exc:
            # No partially prepared source is promoted as success. Let the ORIGINAL
            # reader see this file on demand, so an error after an earlier target
            # does not invalidate that earlier episode.
            totals['fallback_files']+=1
            log.warning('DIFF_INPUT_USE_ORIGINAL',source=str(path),reason=type(exc).__name__,detail=str(exc),coins=sorted(needed))
            for coin in needed:
                fallback=dict(chunk)
                # Conservative discovery envelopes are routing hints, never proof
                # of source contents. The original reader decides validity.
                if config.get('auto_input_discovery') and 'beyond discovered shard boundary' in str(exc):
                    fallback['end_block']=max(fallback['end_block'],scopes[coin][1])
                sections[coin]['chunks'].append(fallback)
    save(cache/'last_plan.json',{'scopes':scopes,'sections':sections,'totals':totals})
    log.info('DIFF_INPUT_PREPARATION_DONE',**totals)
    return sections
