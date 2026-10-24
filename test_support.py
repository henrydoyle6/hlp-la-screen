"""Small native fixtures and a harness for the production pipeline (no copied algorithms)."""
import contextlib
import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
import msgpack
import hlp_la_screen as app
import run_hlp
import state_store as store
import csv_pipeline as pipe

class Log:
    def __init__(self):self.events=[];self.lines=0
    def info(self,event,**kw):self.events.append((event,kw))
    warning=info
    def heartbeat(self,*args):pass

def raw_order(oid,side='B',price=123450,qty=250):
    return {'o':oid,'c':{'s':side,'l':price},'r':qty,'O':999999,'u':'0xowner','t':0}

def fixture(height=100,time='2026-01-01T00:00:00Z',reversed_meta=False):
    main={'clearinghouse':{'meta':{'pdi':0,'universe':[{'name':'BTC','szDecimals':2},{'name':'EMPTY','szDecimals':0}]}},
          'books':[{'book_orders':{91:raw_order(111),92:raw_order(112),93:raw_order(113,'A',124000,50)},'asset':0},
                   {'asset':1,'book_orders':{}}]}
    hip={'clearinghouse':{'meta':{'pdi':1,'universe':[{'name':'xyz:ALPHA','szDecimals':3}]}},
         'books':[{'asset':10000,'book_orders':{1:raw_order(211,price=5000,qty=10)}}]}
    if reversed_meta:
        main={'books':main['books'],'clearinghouse':main['clearinghouse']}
        hip={'books':hip['books'],'clearinghouse':hip['clearinghouse']}
    return {'exchange':{'context':{'height':height,'time':time},'perp_dexs':[main,hip],
        'spot_books':[{'book_orders':{1:raw_order(311,price=123456,qty=125)},'asset':0}],
        'spot_clearinghouse':{'meta':{'spot_infos':[{'name':'@TEST','tokens':[0,1]}],
            'token_infos':[{'spec':{'szDecimals':1}},{'spec':{'szDecimals':6}}]}},
        'user_states':{'must_not_be_read_as_book':{'untriggered_orders':[raw_order(999)]}}}}


def run_fixture(config, output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    c=dict(config)
    end=app.parse_time(c.get('data_end_exclusive',c['data_end']))
    start=app.parse_time(c['data_start'])
    c.update(data_end_exclusive=app.time_text(end),formal_start_inclusive=app.time_text(start+app.BUFFER_MS),
        formal_end_inclusive=app.time_text(end-app.BUFFER_MS),run_id='synthetic',detailed_log=True)
    log=app.RunLog(output/'run.log',console=False,append=True)
    db=store.connect(output/'progress.sqlite');store.init_progress(db)
    cache=store.Cache(output/'cache.sqlite',log);cache.close()
    try:return pipe.run_pipeline(c,output,log,'all',db)
    finally:db.close();log.close()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='hlp_vnext_');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name in ['fills','diffs','snapshots']:(self.root/name).mkdir()
        self.settings=run_hlp.settings_from_top()
        self.settings.update(START_DATE='2026-01-01',END_DATE='2026-01-01',
            FILLS_ROOT=str(self.root/'fills'),DIFFS_ROOT=str(self.root/'diffs'),
            SNAPSHOTS_ROOT=str(self.root/'snapshots'),OUTPUT_ROOT=str(self.root/'output'),CONSOLE_LOG=False,RESUME=True)
        self.owner=self.settings['HLP_ACCOUNTS'][0]
        for h in range(24):self.hour('fills',h,[])
        self.snapshot()
        self.fill(1,102)
        self.hour('diffs',0,[self.block(101,'2026-01-01T00:30:00Z')])
        self.hour('diffs',1,[self.block(102,'2026-01-01T01:00:00Z')])
        self.directory=None

    @staticmethod
    def block(height,time,events=None):
        return dict(block_number=height,block_time=time,events=events or [])

    def hour(self,kind,hour,rows,day='20260101',suffix='.zip'):
        path=self.root/kind/day/(str(hour)+suffix);path.parent.mkdir(parents=True,exist_ok=True)
        content=''.join(r if isinstance(r,str) else json.dumps(r)+'\n' for r in rows)
        if suffix.endswith('.zip'):
            with zipfile.ZipFile(path,'w') as z:z.writestr(f'hourly/{day}/{hour}',content)
        else:path.write_text(content,encoding='utf-8')
        return path

    def snapshot(self,name='100.rmp',height=100,time='2026-01-01T00:00:00Z',value=None,zipped=False):
        path=self.root/'snapshots'/name
        data=msgpack.packb(fixture(height,time) if value is None else value,use_bin_type=True)
        if zipped:
            with zipfile.ZipFile(path,'w') as z:z.writestr(name.removesuffix('.zip'),data)
        else:path.write_bytes(data)
        return path

    def fill(self,hour,height,side='B',qty='1',price='12',coin='BTC',time=None,tid=None):
        stamp=time or f'2026-01-01T{hour:02d}:00:00Z'
        event=[self.owner,dict(coin=coin,side=side,px=price,sz=qty,time=app.parse_time(stamp),
            tid=tid if tid is not None else height,liquidation=dict(method='backstop',liquidatedUser='other'))]
        return self.hour('fills',hour,[self.block(height,stamp,[event])])

    def run_stage(self,stage='all',**updates):
        self.settings.update(updates,RUN_STAGE=stage)
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            try:
                self.directory,self.summary=run_hlp.run(self.settings)
            except BaseException:
                directories=sorted(Path(self.settings['OUTPUT_ROOT']).glob('run_*'))
                if directories:self.directory=directories[-1]
                raise
        return self.directory,self.summary

    def rows(self,number,business=True):
        with (self.directory/f'csv{number}.csv').open(encoding='utf-8',newline='') as stream:
            rows=list(csv.DictReader(stream))
        return [r for r in rows if r['record_type']!='issue'] if business else rows
