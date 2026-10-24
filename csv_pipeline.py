"""Strict CSV1 -> CSV2 -> CSV3; six durable files, local diagnostics and checkpoints."""
from contextlib import closing
import csv
from decimal import Decimal, localcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import zipfile
import hlp_la_screen as app
import state_store as store
from state_store import CheckpointError, AppendCSV, digest, text, get, put, completed
from input_discovery import issue, fills_sources, locate_hour, member_name, SnapshotCatalog
from rmp_l4 import source_identity, ExtractionCache, ExtractionError, identity, CONVERSION_VERSION, is_rmp
from prepare_local_diffs import DiffCache, has_float

VERSION = 'sqlite-stages-v3'
SCHEMA = 'hlp-csv-v3'
COMMON = ['schema_version','run_id','record_type','episode_id','status','reason_code',
          'reason_message','error_file','error_member','error_line','error_block','issues_json']
RANGE = ['data_start','data_end_exclusive','formal_start_inclusive','formal_end_inclusive']
FILL = ['event_id','event_time','block_number','coin','direction','price','qty',
        'source_file','source_member','source_line','source_event_index']
SUMMARY = ['coin','direction','segment_id','episode_start','episode_end','start_block',
           'end_block','event_count','formal_event_count','buffer_event_count','duration_ms',
           'total_qty','entry_notional','entry_vwap','partial_episode','partial_reason',
           'split_before','split_after']
CSV1_FIELDS = COMMON + FILL + RANGE + ['is_formal_event'] + [k for k in SUMMARY if k not in FILL] + ['episode_input_hash']
CSV2_FIELDS = COMMON + ['episode_input_hash','coin','direction','total_qty','entry_notional',
    'entry_vwap','partial_episode','partial_reason','split_before','split_after','tn',
    'target_block','book_state_point','book_side','snapshot_file','snapshot_member',
    'snapshot_block','snapshot_time','snapshot_source_id','snapshot_selection_status',
    'replay_from_block_exclusive','replay_to_block_inclusive','last_applied_block',
    'replay_coverage_mode','diff_sources_json','order_count','price_level_count','total_depth',
    'side_notional','best_price','worst_price','book_is_empty','orders_json','book_hash']
CSV3_FIELDS = COMMON + ['episode_input_hash','csv2_row_hash','book_hash','coin','direction',
    'episode_start','tn','target_block','event_count','total_qty','entry_notional','entry_vwap',
    'partial_episode','partial_reason','split_before','split_after','book_side','order_count',
    'price_level_count','total_depth','best_price','worst_price','filled_qty','unfilled_qty',
    'fill_ratio','close_notional','exit_vwap','entry_cost_of_filled_qty','executed_order_count',
    'executed_price_level_count','last_execution_price','la','depth_insufficient','fill_trace_json']


def value_text(value):
    return ('true' if value else 'false') if isinstance(value,bool) else '' if value is None else str(value)


def episode_hash(rows):
    return digest({'fills':[{k:value_text(row.get(k,'')) for k in FILL} for row in rows],
                   'summary':{k:value_text(rows[0].get(k,'')) for k in SUMMARY}})


def common(config, kind, status, ep=None, problems=None):
    result = dict(schema_version=SCHEMA,run_id=config['run_id'],record_type=kind,
                  episode_id=ep.get('episode_id','') if ep else '',status=status,
                  reason_code='',reason_message='',error_file='',error_member='',
                  error_line='',error_block='',issues_json=text(problems or []))
    return result


def issue_row(config, problem):
    result = common(config,'issue','input_issue',problems=[problem])
    result.update(reason_code=problem['code'],reason_message=problem['message'],
                  error_file=problem['source_file'],error_member=problem['source_member'],
                  error_line=problem['source_line'],error_block=problem['block_number'])
    return result


def failed(row, problem):
    row.update(status='skipped',reason_code=problem['code'],reason_message=problem['message'],
               error_file=problem['source_file'],error_member=problem['source_member'],
               error_line=problem['source_line'],error_block=problem['block_number'],
               issues_json=text([problem]))
    return row


def parse_fills_json(raw, owners, fmt):
    try:
        import orjson
        value=orjson.loads(raw)
    except (ImportError,ValueError):
        return json.loads(raw,parse_float=Decimal)
    candidates=value.get('events',[value]) if isinstance(value,dict) else [value]
    for event in candidates if isinstance(candidates,list) else []:
        relevant=fmt!='node' or (isinstance(event,list) and len(event)==2 and str(event[0]).lower() in owners)
        if relevant and has_float(event):
            return json.loads(raw,parse_float=Decimal)
    return value


def ingest(config, db, log):
    if get(db,'ingest_done',False):
        return
    start, end = app.parse_time(config['data_start']), app.parse_time(config['data_end_exclusive'])
    owners = set(config['fills'].get('hlp_addresses', []))
    fmt = config['fills'].get('format', 'node')
    cursor = get(db, 'ingest_cursor', 0)
    last_seen = get(db, 'fills_last_seen')
    raw_count = get(db, 'raw_record_count', 0)
    recognized = get(db, 'recognized_fills', 0)
    log.info('CSV1_INGEST_START', next_source=cursor)
    for index, spec in enumerate(fills_sources(config)):
        if index < cursor:
            continue
        path, member, line_no = spec.get('path', ''), '', 0
        count_before, recognized_before = raw_count, recognized
        def gap(problem):
            nonlocal last_seen
            db.execute('INSERT INTO ingest(kind,body) VALUES (?,?)', ('issue', text(problem)))
            last_seen = None
            log.warning('FILLS_SEGMENT_BOUNDARY', **problem)
        try:
            if not path:
                path = str(locate_hour(spec['root'], spec['hour']))
            ident = source_identity(path)
            member = member_name(path)
            log.info('FILLS_SOURCE_READ', source=path, member=member, source_index=index)
            with db:
                with app.open_json_text(path) as stream:
                    for line_no, raw in enumerate(stream, 1):
                        if not raw.strip():
                            continue
                        raw_count += 1
                        log.lines += 1
                        log.heartbeat('fills_ingest', path, line_no)
                        try:
                            batch = parse_fills_json(raw, owners, fmt)
                            if isinstance(batch, dict) and 'gap' in batch:
                                declared=batch['gap']
                                if isinstance(declared,dict) and 'start' in declared and 'end' in declared:
                                    if app.parse_time(declared['end'])<=start or app.parse_time(declared['start'])>=end:
                                        continue
                                gap(issue('csv1','DECLARED_FILLS_GAP','Declared fills gap',path,member,line_no,
                                          gap=batch['gap']))
                                continue
                            header = batch if isinstance(batch,dict) and 'events' in batch else {}
                            events = header.get('events', [batch])
                            if not isinstance(events,list):
                                raise ValueError('events must be an array')
                            for position, event in enumerate(events):
                                try:
                                    record=app._parse_fill(event,header,fmt,owners,path,line_no,position,start,end-1)
                                    if record is None:
                                        continue
                                    order=[record.time_ms,record.block_number]
                                    if last_seen is not None and order < last_seen:
                                        raise ValueError('Takeover is out of chronological block order')
                                    # Only accepted records advance this segment's ordering state.
                                    last_seen=order
                                    row=dict(event_id=record.event_id,event_time=app.time_text(record.time_ms),
                                        block_number=record.block_number,coin=record.coin,direction=record.direction,
                                        price=app.dec_text(record.price),qty=app.dec_text(record.qty),source_file=path,
                                        source_member=member,source_line=line_no,source_event_index=position)
                                    if fmt=='node':
                                        row['event_id'] += ':' + digest([path,member,line_no,position])[:12]
                                    db.execute('INSERT INTO ingest(kind,body) VALUES (?,?)',('fill',text(row)))
                                    recognized+=1
                                    if config.get('detailed_log', True):
                                        log.info('TAKEOVER_READ', **row)
                                except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
                                    raw_event=event[1] if fmt=='node' and isinstance(event,list) and len(event)==2 else event
                                    scope={}
                                    if isinstance(raw_event,dict) and isinstance(raw_event.get('coin'),str) and raw_event['coin']:
                                        direction=raw_event.get('direction') if fmt!='node' else {'A':'short','B':'long'}.get(raw_event.get('side'))
                                        if direction in ('long','short'):scope=dict(affected_coin=raw_event['coin'],affected_direction=direction)
                                    gap(issue('csv1','UNREADABLE_FILL',str(exc),path,member,line_no,position,
                                              header.get('block_number',''),**scope))
                        except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
                            gap(issue('csv1','UNREADABLE_FILLS_LINE',str(exc),path,member,line_no))
                if source_identity(path)!=ident:
                    raise app.InputProblem('SOURCE_CHANGED','Fills source changed; discard this uncommitted source',path)
                put(db,'ingest_cursor',index+1)
                put(db,'fills_last_seen',last_seen)
                put(db,'raw_record_count',raw_count)
                put(db,'recognized_fills',recognized)
        except (OSError,EOFError,UnicodeError,ValueError,app.InputProblem,zipfile.BadZipFile) as exc:
            # The source transaction rolls back. Never combine versions of a changed source.
            db.rollback()
            raw_count,recognized=count_before,recognized_before
            code=getattr(exc,'code','MISSING_FILLS_FILE' if isinstance(exc,FileNotFoundError) else 'UNREADABLE_FILLS_FILE')
            with db:
                gap(issue('csv1',code,str(exc),path or spec.get('root',''),member,line_no,
                          known_start=spec.get('start',''),known_end=spec.get('end','')))
                put(db,'ingest_cursor',index+1)
                put(db,'fills_last_seen',None)
        log.info('CSV1_INGEST_COMMIT',source_index=index,raw_records=raw_count,recognized_fills=recognized)
        store.fault('ingest_committed',source=index)
    with db:
        put(db,'ingest_done',True)
    log.info('CSV1_INGEST_COMPLETE',raw_records=raw_count,recognized_fills=recognized)


def group(config, db, log):
    if get(db,'group_done',False):
        return
    start,end=app.parse_time(config['data_start']),app.parse_time(config['data_end_exclusive'])
    f0,f1=app.parse_time(config['formal_start_inclusive']),app.parse_time(config['formal_end_inclusive'])
    state=get(db,'group_state',dict(cursor=0,active={},first_segment=True,segment=0,reason='',seen=[]))
    for key,default in [('global_segment',state['segment']),('segments',{}),('reasons',{}),('left_closed',[])]:
        state.setdefault(key,default)
    log.info('CSV1_GROUP_START',cursor=state['cursor'])
    def load(gid):
        return json.loads(db.execute('SELECT body FROM groups WHERE id=?',(gid,)).fetchone()[0])
    def save(gid,g):
        db.execute('INSERT OR REPLACE INTO groups(id,body) VALUES (?,?)',(gid,text(g)))
    for item in db.execute('SELECT * FROM ingest WHERE seq>? ORDER BY seq',(state['cursor'],)):
        with db, localcontext() as ctx:
            ctx.prec=app.CALC_PRECISION
            value=json.loads(item['body'])
            if item['kind']=='issue':
                scope=text([value['affected_coin'],value['affected_direction']]) if value.get('affected_coin') and value.get('affected_direction') else None
                affected=[state['active'][scope]] if scope in state['active'] else [] if scope else list(state['active'].values())
                for gid in affected:
                    g=load(gid);g['split_after']=value['issue_id'];save(gid,g)
                state['segment']+=1
                if scope:
                    state['active'].pop(scope,None)
                    state['segments'][scope]=state['segment'];state['reasons'][scope]=value['issue_id']
                    if scope in state['seen']:state['seen'].remove(scope)
                    if scope not in state['left_closed']:state['left_closed'].append(scope)
                else:
                    state.update(active={},first_segment=False,global_segment=state['segment'],
                                 reason=value['issue_id'],seen=[],segments={},reasons={},left_closed=[])
            else:
                key=text([value['coin'],value['direction']]);gid=state['active'].get(key)
                tm=app.parse_time(value['event_time'])
                g=load(gid) if gid is not None else None
                if g is not None and tm-g['last_ms']>app.GAP_MS:
                    g=None
                if g is None:
                    gid=item['seq']
                    g=dict(coin=value['coin'],direction=value['direction'],segment_id=state['segments'].get(key,state['global_segment']),
                        episode_start=value['event_time'],episode_end=value['event_time'],
                        start_block=value['block_number'],end_block=value['block_number'],first_seq=item['seq'],
                        first_ms=tm,last_ms=tm,event_count=0,formal_event_count=0,buffer_event_count=0,
                        total_qty='0',entry_notional='0',partial_episode=False,partial_reason='',
                        split_before=state['reasons'].get(key,state['reason']) if key not in state['seen'] else '',split_after='')
                    if state['first_segment'] and key not in state['left_closed'] and tm-start<app.GAP_MS:
                        g.update(partial_episode=True,partial_reason='data_left_boundary')
                    state['active'][key]=gid
                    if key not in state['seen']:state['seen'].append(key)
                g['last_ms']=tm;g['episode_end']=value['event_time'];g['end_block']=value['block_number']
                g['event_count']+=1
                formal=f0<=tm<=f1
                g['formal_event_count']+=int(formal);g['buffer_event_count']+=int(not formal)
                q=Decimal(value['qty']);p=Decimal(value['price'])
                g['total_qty']=app.dec_text(Decimal(g['total_qty'])+q)
                g['entry_notional']=app.dec_text(Decimal(g['entry_notional'])+p*q)
                g['entry_vwap']=app.dec_text(Decimal(g['entry_notional'])/Decimal(g['total_qty']))
                g['duration_ms']=tm-g['first_ms']
                save(gid,g)
                db.execute('INSERT INTO assignment VALUES (?,?)',(item['seq'],gid))
            state['cursor']=item['seq'];put(db,'group_state',state)
        store.fault('group_committed',cursor=state['cursor'])
    with db:
        for gid in state['active'].values():
            g=load(gid)
            if end-g['last_ms']<=app.GAP_MS:
                g['partial_episode']=True
                g['partial_reason']=';'.join(filter(None,[g['partial_reason'],'data_right_boundary']))
                save(gid,g)
        groups=[(row['id'],json.loads(row['body'])) for row in db.execute('SELECT * FROM groups')]
        selected=sorted([(gid,g) for gid,g in groups if g['formal_event_count']],
                        key=lambda item:(item[1]['first_ms'],item[1]['start_block'],item[1]['coin'],item[1]['direction'],item[1]['first_seq']))
        for number,(gid,g) in enumerate(selected,1):
            db.execute('UPDATE groups SET selected=1,episode_id=? WHERE id=?',(f'EP{number:06d}',gid))
        put(db,'selected_episodes',len(selected));put(db,'group_done',True)
    log.info('CSV1_GROUP_COMPLETE',episodes=len(selected),all_groups=len(groups))


def generate_csv1(config, out, db, log):
    with closing(AppendCSV(out,1,CSV1_FIELDS,db,log)) as output:
        if output.done:return
        ingest(config,db,log)
        group(config,db,log)
        cursor=get(db,'csv1_emit_index',0)
        log.info('CSV1_EMIT_START',cursor=cursor)
        def rows():
            for record in db.execute('SELECT * FROM groups WHERE selected=1 ORDER BY episode_id'):
                g=json.loads(record['body']);base=common(config,'fill','grouped',dict(episode_id=record['episode_id']))
                base.update({key:g[key] for key in SUMMARY});base.update({key:config[key] for key in RANGE})
                batch=[]
                for source in db.execute('SELECT i.body FROM ingest i JOIN assignment a ON a.seq=i.seq WHERE a.group_id=? ORDER BY i.seq',(record['id'],)):
                    row={**base,**json.loads(source[0])}
                    tm=app.parse_time(row['event_time'])
                    row['is_formal_event']=app.parse_time(config['formal_start_inclusive'])<=tm<=app.parse_time(config['formal_end_inclusive'])
                    batch.append(row)
                key=episode_hash(batch)
                for row in batch:
                    row['episode_input_hash']=key
                    yield row
            for source in db.execute("SELECT body FROM ingest WHERE kind='issue' ORDER BY seq"):
                yield issue_row(config,json.loads(source[0]))
        for index,row in enumerate(rows()):
            if index<cursor:continue
            output.write(row);output.commit('csv1_emit_index',index+1)
            store.fault('emit_committed',index=index)
        output.commit(complete=True)
    log.info('CSV1_COMPLETE',episodes=get(db,'selected_episodes',0))


def read_csv(path, fields):
    with Path(path).open(encoding='utf-8',newline='') as stream:
        reader=csv.DictReader(stream,strict=True)
        if reader.fieldnames!=fields:
            raise CheckpointError(f'Unexpected header in {path}')
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise CheckpointError(f'Unlocatable CSV structure damage in {path}')
            yield row


def load_csv1(config, out, db):
    if not completed(db,1):
        raise CheckpointError('Complete CSV1 before running CSV2/CSV3')
    groups={}
    for row in read_csv(Path(out)/'csv1.csv',CSV1_FIELDS):
        if row['record_type']=='issue':continue
        if row['record_type']!='fill' or row['status']!='grouped' or not row['episode_id']:
            raise CheckpointError('CSV1 contains an invalid business row')
        groups.setdefault(row['episode_id'],[]).append(row)
    episodes=[]
    for name,rows in groups.items():
        first=rows[0]
        if any(any(r[k]!=first[k] for k in SUMMARY+['episode_input_hash']) for r in rows):
            raise CheckpointError(f'Conflicting CSV1 episode summary: {name}')
        if episode_hash(rows)!=first['episode_input_hash']:
            raise CheckpointError(f'CSV1 episode hash mismatch: {name}')
        with localcontext() as ctx:
            ctx.prec=app.CALC_PRECISION
            q=sum((Decimal(r['qty']) for r in rows),Decimal(0))
            n=sum((Decimal(r['price'])*Decimal(r['qty']) for r in rows),Decimal(0))
            if q!=Decimal(first['total_qty']) or n!=Decimal(first['entry_notional']) or n/q!=Decimal(first['entry_vwap']):
                raise CheckpointError(f'CSV1 episode arithmetic mismatch: {name}')
        if len(rows)!=int(first['event_count']):raise CheckpointError('CSV1 missing fill rows')
        episodes.append({**{k:first[k] for k in SUMMARY},'episode_id':name,
                         'episode_input_hash':first['episode_input_hash'],
                         'tn':rows[-1]['event_time'],'tn_ms':app.parse_time(rows[-1]['event_time']),
                         'target_block':int(rows[-1]['block_number'])})
    if len(episodes)!=get(db,'selected_episodes',len(episodes)):
        raise CheckpointError('CSV1 episode coverage differs from committed progress')
    return episodes


def replay_state(book):
    return [dict(coin=book.coin,oid=o.oid,side=o.side,limitPx=app.dec_text(o.price),sz=app.dec_text(o.qty))
            for o in sorted(book.orders.values(),key=lambda order:order.oid)]


def book_metrics(book, side):
    orders=[dict(oid=o.oid,price=app.dec_text(o.price),qty=app.dec_text(o.qty)) for o in book.side_orders(side)]
    with localcontext() as ctx:
        ctx.prec=app.CALC_PRECISION
        depth=sum((Decimal(o['qty']) for o in orders),Decimal(0))
        amount=sum((Decimal(o['price'])*Decimal(o['qty']) for o in orders),Decimal(0))
    return dict(order_count=len(orders),price_level_count=len({o['price'] for o in orders}),
                total_depth=app.dec_text(depth),side_notional=app.dec_text(amount),
                best_price=orders[0]['price'] if orders else '',worst_price=orders[-1]['price'] if orders else '',
                book_is_empty=not orders,orders_json=text(orders),book_hash=digest(orders))


class BookService:
    def __init__(self, config, cache, episodes, log, all_episodes=None):
        self.config,self.cache,self.log=config,cache,log
        self.catalog=SnapshotCatalog(config,cache,log)
        self.extractor=ExtractionCache(cache,log)
        self.plans,self.errors,self.demands={},{},{}
        for ep in episodes:
            try:
                ref,status=self.catalog.choose(ep)
                self.plans[ep['episode_id']]=(ref,status)
                self.demands.setdefault(ref,set()).add(ep['coin'])
                log.info('EPISODE_SNAPSHOT_PLAN',episode=ep['episode_id'],coin=ep['coin'],
                         snapshot=ref.path,snapshot_block=ref.block_number,target=ep['target_block'],selection=status)
            except app.BookError as exc:
                self.errors[ep['episode_id']]=exc
        full=all_episodes if all_episodes is not None else episodes
        self.diff=DiffCache(config,cache,{e['coin'] for e in full},max([e['target_block'] for e in full]+[0]),log)
        self.extracted={}

    def get(self, ep):
        if ep['episode_id'] in self.errors:
            raise self.errors[ep['episode_id']]
        ref,status=self.plans[ep['episode_id']]
        try:
            ident=identity(ref)
        except OSError as exc:
            raise app.BookError('SNAPSHOT_READ_ERROR',str(exc),ref.path) from exc
        source_id=digest(ident)
        state_key=digest([ep['coin'],source_id,CONVERSION_VERSION])
        state=self.cache.read('replay',state_key)
        last=ref.block_number
        book=None
        if state is not None and state.get('coin')==ep['coin'] and state.get('source_id')==source_id and ref.block_number<=state.get('height',-1)<=ep['target_block']:
            try:
                last=state['height']
                book=app._book_from_payload(dict(block_number=last,coin=ep['coin'],orders=state['orders']),
                    app.SnapshotRef(ref.path,last,ref.time_ms),ep['coin'])
                self.cache.count('REPLAY_CACHE_HIT',coin=ep['coin'],source=ref.path,height=last,target=ep['target_block'])
            except (app.BookError,KeyError,TypeError,ValueError) as exc:
                self.log.warning('REPLAY_CACHE_INVALID',coin=ep['coin'],detail=str(exc));book=None
        if book is None:
            self.cache.count('REPLAY_CACHE_MISS',coin=ep['coin'],source=ref.path)
            last=ref.block_number
            if is_rmp(ref.path):
                if ref not in self.extracted:
                    self.extracted[ref]=self.extractor.ensure(ref,self.demands[ref])
                entry=self.extracted[ref][ep['coin']]
                if isinstance(entry,ExtractionError):
                    raise app.BookError(entry.code,str(entry),ref.path)
                payload=self.cache.read('extract',entry)
                if payload is None:
                    # A single invalid extraction is rebuilt locally, never using another snapshot.
                    entry=self.extractor.ensure(ref,{ep['coin']})[ep['coin']]
                    if isinstance(entry,ExtractionError):raise app.BookError(entry.code,str(entry),ref.path)
                    payload=self.cache.read('extract',entry)
                book=app._book_from_payload(payload,ref,ep['coin'])
            else:
                book=app.load_snapshot(ref,ep['coin'])
        last_applied=last
        def progress(height,count):
            nonlocal last_applied
            last_applied=height
            store.fault('replay_block_applied',episode=ep['episode_id'],block=height)
            if height%self.config.get('progress_every',1000)==0:
                self.log.info('REPLAY_PROGRESS',episode=ep['episode_id'],block=height,target=ep['target_block'],events=count)
            self.log.heartbeat('replay',ref.path,height)
        if last<ep['target_block']:
            try:
                app.replay_blocks(book,self.diff.between(app.SnapshotRef(ref.path,last,ref.time_ms),ep['target_block'],ep['tn_ms']),
                                  last,ep['target_block'],dense=True,progress=progress)
            except app.BookError as exc:
                exc.last_applied_block=last_applied
                if exc.code=='MISSING_DIFF_BLOCK':
                    missing=[p['source_file'] for p in self.diff.issues if p['code']=='MISSING_DIFF_FILE']
                    source=exc.source or getattr(self.diff,'last_attempted','')
                    if missing:source+='; missing sources: '+', '.join(missing)
                    wrapped=app.BookError(exc.code,exc.message,source)
                    wrapped.last_applied_block=last_applied
                    raise wrapped from exc
                raise
        else:
            self.diff.used=[]
        if identity(ref)!=ident:
            raise app.BookError('SOURCE_CHANGED','Snapshot changed during replay; no successful cache commit',ref.path)
        with self.cache.db:
            self.cache.write('replay',state_key,dict(coin=ep['coin'],source_id=source_id,height=ep['target_block'],orders=replay_state(book)))
        store.fault('replay_cache_committed',episode=ep['episode_id'])
        self.log.info('REPLAY_CACHE_COMMIT',episode=ep['episode_id'],coin=ep['coin'],height=ep['target_block'])
        evidence=dict(snapshot_file=ref.path,snapshot_member=member_name(ref.path),snapshot_block=ref.block_number,
            snapshot_time=app.time_text(ref.time_ms),snapshot_source_id=source_id,snapshot_selection_status=status,
            replay_from_block_exclusive=ref.block_number,replay_to_block_inclusive=ep['target_block'],
            last_applied_block=ep['target_block'],replay_coverage_mode='dense',diff_sources_json=text(self.diff.used))
        return book,evidence


def generate_csv2(config, out, db, log, episodes):
    if completed(db,2):
        log.info('CSV2_ALREADY_COMPLETE');return
    done=set(get(db,'csv2_done',[]))
    pending=[e for e in sorted(episodes,key=lambda e:(e['target_block'],e['episode_id'])) if e['episode_id'] not in done]
    with closing(store.Cache(Path(out)/'cache.sqlite',log)) as cache, closing(AppendCSV(out,2,CSV2_FIELDS,db,log)) as output:
        service=BookService(config,cache,pending,log,all_episodes=episodes) if pending else None
        for ep in pending:
            row=common(config,'episode','book_ready',ep)
            row.update({k:ep[k] for k in ['episode_input_hash','coin','direction','total_qty','entry_notional','entry_vwap',
                'partial_episode','partial_reason','split_before','split_after','tn','target_block']})
            side='bid' if ep['direction']=='long' else 'ask'
            row.update(book_state_point='block_end',book_side=side)
            log.info('CSV2_EPISODE_START',episode=ep['episode_id'],coin=ep['coin'],target=ep['target_block'])
            book=None
            try:
                book,evidence=service.get(ep)
                row.update(evidence);row.update(book_metrics(book,side))
                if service.catalog.issues:
                    row['issues_json']=text(service.catalog.issues)
            except app.BookError as exc:
                failed(row,issue('csv2',exc.code,exc.message,exc.source,block_number=ep['target_block']))
                if ep['episode_id'] in service.plans:
                    ref,status=service.plans[ep['episode_id']]
                    row.update(snapshot_file=ref.path,snapshot_block=ref.block_number,snapshot_time=app.time_text(ref.time_ms),
                               snapshot_selection_status=status,replay_from_block_exclusive=ref.block_number,
                               replay_to_block_inclusive=ep['target_block'],replay_coverage_mode='dense',
                               diff_sources_json=text(service.diff.used))
                if hasattr(exc,'last_applied_block'):row['last_applied_block']=exc.last_applied_block
            observed={p['issue_id']:p for p in get(db,'csv2_observed_issues',[])}
            observed.update({p['issue_id']:p for p in service.catalog.issues+service.diff.issues})
            with db:put(db,'csv2_observed_issues',list(observed.values()))
            output.write(row)
            done.add(ep['episode_id']);output.commit('csv2_done',sorted(done))
            log.info('CSV2_EPISODE_COMPLETE',episode=ep['episode_id'],status=row['status'],reason=row['reason_code'])
            # Results are released only after CSV fsync and committed progress.
            del book,row
        seen=set(get(db,'csv2_issues',[]))
        problems=get(db,'csv2_observed_issues',[])
        for problem in problems:
            if problem['issue_id'] in seen:continue
            output.write(issue_row(config,problem));seen.add(problem['issue_id'])
            output.commit('csv2_issues',sorted(seen))
        output.commit(complete=True)
        log.info('CSV2_CACHE_SUMMARY',session=cache.stats,
                 lifetime={r['key']:json.loads(r['value']) for r in cache.db.execute("SELECT * FROM meta WHERE key LIKE 'count:%'")})
    log.info('CSV2_COMPLETE',episode_rows=len(done),issue_rows=len(seen))


def book_from_orders(coin, orders, side):
    book=app.OrderBook(coin)
    if not isinstance(orders,list):raise ValueError('orders_json must be an array')
    for raw in orders:
        if set(raw)!= {'oid','price','qty'} or any(not isinstance(v,str) for v in raw.values()):
            raise ValueError('Order fields must be exact oid/price/qty strings')
        book._insert_snapshot_order(dict(coin=coin,oid=raw['oid'],side=side,price=raw['price'],qty=raw['qty']))
        if Decimal(raw['qty'])<=0:raise ValueError('CSV2 contains a non-active order')
    return book


def csv2_rows(out, db):
    rows,errors={},{}
    for row in read_csv(Path(out)/'csv2.csv',CSV2_FIELDS):
        if row['record_type']=='issue':continue
        name=row['episode_id']
        if row['record_type']!='episode' or not name:
            raise CheckpointError('Cannot locate a malformed CSV2 business row')
        if name in rows:
            errors[name]=issue('csv3','DUPLICATE_CSV2_EPISODE','Duplicate CSV2 episode',Path(out)/'csv2.csv')
        rows[name]=row
        expected=db.execute("SELECT sha FROM row_hashes WHERE name='csv2' AND row_key=?",(name,)).fetchone()
        if expected is None or expected[0]!=digest(row):
            errors[name]=issue('csv3','CSV2_ROW_CHANGED','CSV2 row differs from its committed row',Path(out)/'csv2.csv')
    return rows,errors


def generate_csv3(config, out, db, log, episodes):
    if not completed(db,2):raise CheckpointError('Complete CSV2 before running CSV3')
    # No raw directory discovery, raw reader or cache database is opened in this stage.
    with closing(AppendCSV(out,3,CSV3_FIELDS,db,log)) as output:
        if output.done:return
        books,errors=csv2_rows(out,db)
        done=set(get(db,'csv3_done',[]))
        for ep in episodes:
            if ep['episode_id'] in done:continue
            row=common(config,'episode','skipped',ep)
            row.update({k:ep[k] for k in ['episode_input_hash','coin','direction','episode_start','tn','target_block',
                'event_count','total_qty','entry_notional','entry_vwap','partial_episode','partial_reason','split_before','split_after']})
            source=books.get(ep['episode_id'])
            book=None
            try:
                if ep['episode_id'] in errors:raise ValueError(text(errors[ep['episode_id']]))
                if source is None:raise ValueError('Missing CSV2 episode row')
                row['csv2_row_hash']=digest(source)
                for key in ['episode_input_hash','coin','direction','total_qty','entry_vwap','target_block','tn']:
                    if str(ep[key])!=source[key]:raise ValueError(f'CSV2 association mismatch: {key}')
                if source['status']=='skipped':
                    row.update({k:source[k] for k in ['reason_code','reason_message','error_file','error_member','error_line','error_block','issues_json']})
                else:
                    if source['status']!='book_ready':raise ValueError('Unknown CSV2 status')
                    side='bid' if ep['direction']=='long' else 'ask'
                    if source['book_side']!=side:raise ValueError('Wrong close side')
                    orders=json.loads(source['orders_json'])
                    book=book_from_orders(ep['coin'],orders,side)
                    metrics=book_metrics(book,side)
                    for key in ['book_hash','order_count','price_level_count','total_depth','side_notional','best_price','worst_price']:
                        if value_text(metrics[key])!=source[key]:raise ValueError('CSV2 depth/hash mismatch: '+key)
                    if metrics['orders_json']!=source['orders_json']:
                        raise ValueError('CSV2 orders are not in deterministic price order')
                    for key in ['book_hash','book_side','order_count','price_level_count','total_depth','best_price','worst_price','issues_json']:
                        row[key]=source[key]
                    result=app.simulate_close(book,ep['direction'],Decimal(ep['total_qty']),Decimal(ep['entry_vwap']))
                    with localcontext() as ctx:
                        ctx.prec=app.CALC_PRECISION
                        amount=Decimal(0)
                        for trade in result['fill_trace']:
                            trade['notional']=app.dec_text(Decimal(trade['price'])*Decimal(trade['filled_qty']))
                            amount+=Decimal(trade['notional'])
                        filled_qty=result['filled_qty']
                        row.update(status='calculated_full' if not result['unfilled_qty'] else 'calculated_partial' if filled_qty else 'empty_book',
                            filled_qty=app.dec_text(filled_qty),unfilled_qty=app.dec_text(result['unfilled_qty']),
                            fill_ratio=app.dec_text(filled_qty/Decimal(ep['total_qty'])),close_notional=app.dec_text(amount),
                            exit_vwap=app.dec_text(result['exit_vwap']) if result['exit_vwap'] is not None else '',
                            entry_cost_of_filled_qty=app.dec_text(Decimal(ep['entry_vwap'])*filled_qty),
                            executed_order_count=len(result['fill_trace']),
                            executed_price_level_count=len({t['price'] for t in result['fill_trace']}),
                            last_execution_price=result['fill_trace'][-1]['price'] if result['fill_trace'] else '',
                            la=app.dec_text(result['la']),depth_insufficient=result['depth_insufficient'],
                            fill_trace_json=text(result['fill_trace']))
                    if config.get('detailed_log',True):
                        for trade in result['fill_trace']:log.info('SIMULATED_FILL',episode=ep['episode_id'],**trade)
            except (ValueError,TypeError,KeyError,ArithmeticError,app.BookError) as exc:
                failed(row,issue('csv3','CSV2_EPISODE_INVALID',str(exc),Path(out)/'csv2.csv',block_number=ep['target_block']))
            output.write(row);done.add(ep['episode_id']);output.commit('csv3_done',sorted(done))
            log.info('CSV3_EPISODE_COMPLETE',episode=ep['episode_id'],status=row['status'],la=row.get('la'),reason=row['reason_code'])
            del book,row
        output.commit(complete=True)
    log.info('CSV3_COMPLETE',episode_rows=len(done))


def run_pipeline(config, out, log, stage='all', db=None):
    owns=db is None
    if owns:
        db=store.connect(Path(out)/'progress.sqlite');store.init_progress(db)
    try:
        if stage in ('all','csv1'):
            generate_csv1(config,out,db,log)
        episodes=load_csv1(config,out,db)
        if stage in ('all','csv2'):
            generate_csv2(config,out,db,log,episodes)
        if stage in ('all','csv3'):
            generate_csv3(config,out,db,log,episodes)
        summary=dict(stage=stage,episodes=len(episodes),fills=sum(int(e['event_count']) for e in episodes),
                     raw_records=get(db,'raw_record_count',0),recognized_fills=get(db,'recognized_fills',0),
                     partial_episodes=sum(e['partial_episode']=='true' for e in episodes))
        issue_count=0
        for number,fields in [(1,CSV1_FIELDS),(2,CSV2_FIELDS),(3,CSV3_FIELDS)]:
            if not completed(db,number):continue
            counts={};issues=0
            for row in read_csv(Path(out)/f'csv{number}.csv',fields):
                if row['record_type']=='issue':issues+=1
                else:counts[row['status']]=counts.get(row['status'],0)+1
            summary[f'csv{number}']=dict(statuses=counts,issue_rows=issues)
            issue_count+=issues
        if completed(db,3):
            counts=summary['csv3']['statuses']
            if sum(counts.values())!=len(episodes):raise CheckpointError('CSV3 episode coverage mismatch')
            summary.update(calculated=sum(counts.get(k,0) for k in ['calculated_full','calculated_partial','empty_book']),
                skipped=counts.get('skipped',0),depth_insufficient=counts.get('calculated_partial',0)+counts.get('empty_book',0))
        summary['completion']='completed_with_input_issues_or_skips' if issue_count or summary.get('skipped',0) else 'completed'
        with db:put(db,'summary',summary)
        log.info('RUN_STAGE_COMPLETE',**summary)
        return summary
    finally:
        if owns:db.close()
