"""Import a validated CSV1 into a new six-file run; never open raw fills or regroup."""
from contextlib import closing
import csv
import json
from pathlib import Path
import state_store as store
from state_store import CheckpointError,get,put,text,completed,AppendCSV
from rmp_l4 import source_identity,file_hash


def import_csv1(config,out,db,log):
    import csv_pipeline as pipe
    if completed(db,1):return
    source=Path(config['csv1_import']['path'])
    if source.resolve()==(Path(out)/'csv1.csv').resolve():raise CheckpointError('Cannot import a run CSV1 into itself')
    if not get(db,'csv1_import_read_done',False):
        ident=source_identity(source);sha=file_hash(source)
        if source_identity(source)!=ident:raise CheckpointError('CSV1 import source changed during fingerprinting')
        origin=get(db,'csv1_import_source')
        current=dict(identity=ident,sha256=sha)
        if origin and origin!=current:raise CheckpointError('CSV1 import source changed; use RESUME=False for a new run')
        with db:put(db,'csv1_import_source',current)
        cursor=get(db,'csv1_import_read_cursor',0)
        source_run=get(db,'csv1_import_source_run_id')
        log.info('CSV1_IMPORT_START',source=str(source),sha256=sha,resume_row=cursor,
                 note='Validates/copies CSV1 only; no fills read and no episode regrouping')
        try:
            with source.open(encoding='utf-8-sig',newline='') as stream:
                reader=csv.DictReader(stream,strict=True)
                if reader.fieldnames!=pipe.CSV1_FIELDS:raise CheckpointError('Unsupported imported CSV1 header; use the documented hlp-csv-v3 export')
                for position,row in enumerate(reader,1):
                    if None in row or any(v is None for v in row.values()):raise CheckpointError('Truncated/malformed imported CSV1 record')
                    if position<=cursor:continue
                    if source_run is None:source_run=row['run_id']
                    if row['run_id']!=source_run:raise CheckpointError('Imported CSV1 mixes source run identities')
                    row['run_id']=config['run_id']
                    db.execute('INSERT INTO csv1_import_rows VALUES (?,?)',(position,text(row)))
                    store.fault('csv1_import_row_staged',position=position)
                    if position%256==0:
                        put(db,'csv1_import_read_cursor',position);put(db,'csv1_import_source_run_id',source_run);db.commit()
                        store.fault('csv1_import_read_committed',position=position)
                if source_identity(source)!=ident:raise CheckpointError('CSV1 import source changed while reading')
                put(db,'csv1_import_source_run_id',source_run)
                put(db,'csv1_import_read_done',True);db.commit()
        except BaseException:
            db.rollback();raise
    if not get(db,'csv1_import_validated',False):
        def staged():
            for row in db.execute('SELECT body FROM csv1_import_rows ORDER BY position'):yield json.loads(row[0])
        episodes=pipe.validate_csv1_rows(config,staged())
        with db:
            put(db,'selected_episodes',len(episodes));put(db,'csv1_import_validated',True)
            put(db,'raw_record_count',None);put(db,'recognized_fills',None);put(db,'csv1_origin','import')
        store.fault('csv1_import_validated',episodes=len(episodes))
        log.info('CSV1_IMPORT_VALIDATED',episodes=len(episodes),fills=sum(int(e['event_count']) for e in episodes))
    with closing(AppendCSV(out,1,pipe.CSV1_FIELDS,db,log)) as output:
        cursor=get(db,'csv1_import_emit_cursor',0)
        for staged in db.execute('SELECT position,body FROM csv1_import_rows WHERE position>? ORDER BY position',(cursor,)):
            output.write(json.loads(staged['body']));output.commit('csv1_import_emit_cursor',staged['position'])
            store.fault('csv1_import_emit_committed',position=staged['position'])
        output.commit(complete=True)
    with db:db.execute('DELETE FROM csv1_import_rows')
    log.info('CSV1_IMPORT_COMPLETE',episodes=get(db,'selected_episodes'),source_sha256=get(db,'csv1_import_source')['sha256'])
