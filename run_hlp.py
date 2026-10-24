#!/usr/bin/env python3
"""Edit ONLY the configuration block below, then run: python run_hlp.py

Dates describe the prepared data range (inclusive UTC days); its first/last
hours are internal buffers. CSV stages resume by default. Paths must be absolute
on the Ubuntu machine. See README.txt for layout, installation and output rules.
"""

# ================== USER CONFIGURATION / 只修改本区 ==================
START_DATE = '2025-11-12'     # 已准备好数据的 UTC 开始日期，包含当天
END_DATE = '2025-11-13'       # 已准备好数据的 UTC 最后日期，包含当天

FILLS_ROOT = '/data/hyperliquid/fills'  # 下一级为 YYYYMMDD/H.zip
DIFFS_ROOT = '/data/hyperliquid/node_raw_book_diffs_by_block'
SNAPSHOTS_ROOT = '/data/hyperliquid/periodic_abci_states'
OUTPUT_ROOT = '/data/hlp_la/output'     # 每轮目录内只有六个长期文件

# HLP 接收 backstop 成交的账户；按研究口径维护，程序不会联网替换。
HLP_ACCOUNTS = (
    '0x2e3d94f0562703b25c83308a05046ddaf9a8dd14',
    '0xb0a55f13d22f66e6d495ac98113841b2326e9540',
    '0x5e177e5e39c0f4e421f5865a6d8beed8d921cb70',
    '0x2ed5c4484ea3ff8b57d5f2fb152a40d9f2b68308',
)
DETAILED_LOG = True          # 逐笔接管、逐笔模拟成交、回放及缓存统计
CONSOLE_LOG = True           # run.log 始终写入；本项只控制终端同步输出
LOG_BLOCK_INTERVAL = 1000    # 每 N 块报告回放进度；心跳仍每 10 秒记录
RUN_STAGE = 'all'            # all / csv1 / csv2 / csv3；csv3 只读取 csv1、csv2
RESUME = True                # 自动续跑同配置的最近一次运行；False 新建一轮
# ================== END CONFIGURATION / 以下不用修改 ==================

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback
import uuid


def settings_from_top():
    return {name:globals()[name] for name in ('START_DATE','END_DATE','FILLS_ROOT','DIFFS_ROOT',
        'SNAPSHOTS_ROOT','OUTPUT_ROOT','HLP_ACCOUNTS','DETAILED_LOG','CONSOLE_LOG',
        'LOG_BLOCK_INTERVAL','RUN_STAGE','RESUME')}


def build_config(settings):
    from hlp_la_screen import parse_time, time_text
    from state_store import digest
    def day(key):
        value=settings[key]
        if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
            raise ValueError(key+': use YYYY-MM-DD')
        return date.fromisoformat(value)
    first,last=day('START_DATE'),day('END_DATE')
    if last<first:raise ValueError('END_DATE must be on or after START_DATE')
    paths={}
    for key in ('FILLS_ROOT','DIFFS_ROOT','SNAPSHOTS_ROOT','OUTPUT_ROOT'):
        path=Path(os.path.expandvars(os.path.expanduser(str(settings[key]))))
        if not path.is_absolute():raise ValueError(key+': use an absolute local Ubuntu path')
        paths[key]=path.resolve()
    if any(paths[k]==paths['OUTPUT_ROOT'] or paths[k] in paths['OUTPUT_ROOT'].parents
           for k in ('FILLS_ROOT','DIFFS_ROOT','SNAPSHOTS_ROOT')):
        raise ValueError('OUTPUT_ROOT must be outside raw data directories')
    accounts=sorted(set(str(a).lower() for a in settings['HLP_ACCOUNTS']))
    if not accounts or any(not re.fullmatch(r'0x[0-9a-f]{40}',a) for a in accounts):
        raise ValueError('HLP_ACCOUNTS must contain 0x + 40 hex addresses')
    interval=settings.get('LOG_BLOCK_INTERVAL',1000)
    if type(interval) is not int or interval<=0:raise ValueError('LOG_BLOCK_INTERVAL must be positive')
    start=parse_time(first.isoformat()+'T00:00:00Z')
    end=parse_time((last+timedelta(days=1)).isoformat()+'T00:00:00Z')
    config=dict(data_start=time_text(start),data_end_exclusive=time_text(end),
        formal_start_inclusive=time_text(start+3_600_000),formal_end_inclusive=time_text(end-3_600_000),
        fills=dict(format='node',hlp_addresses=accounts),
        roots=dict(fills=str(paths['FILLS_ROOT']),diffs=str(paths['DIFFS_ROOT']),snapshots=str(paths['SNAPSHOTS_ROOT'])),
        detailed_log=settings.get('DETAILED_LOG',True),progress_every=interval)
    config['_semantic_hash']=digest({k:v for k,v in config.items() if k not in ('detailed_log','progress_every')})
    return config,paths


def engine_hashes():
    root=Path(__file__).resolve().parent
    names=['csv_pipeline.py','hlp_la_screen.py','state_store.py','rmp_l4.py','input_discovery.py','prepare_local_diffs.py']
    result={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}
    runner=Path(__file__).read_text('utf-8-sig').split('# ================== END CONFIGURATION',1)[1]
    result['run_hlp.py:engine']=hashlib.sha256(runner.encode()).hexdigest()
    return result


def run(settings=None):
    import state_store as store
    import csv_pipeline as pipeline
    from hlp_la_screen import RunLog
    settings=settings_from_top() if settings is None else settings
    config,paths=build_config(settings)
    stage=settings.get('RUN_STAGE','all')
    if stage not in ('all','csv1','csv2','csv3'):raise ValueError('RUN_STAGE must be all/csv1/csv2/csv3')
    root=paths['OUTPUT_ROOT'];root.mkdir(parents=True,exist_ok=True)
    prefix=f"run_{settings['START_DATE']}_{settings['END_DATE']}_{config['_semantic_hash'][:12]}_"
    candidates=sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith(prefix) and (p/'progress.sqlite').exists())
    if settings.get('RESUME',True) and candidates:
        directory=candidates[-1]
    else:
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        directory=root/(prefix+stamp+'_'+uuid.uuid4().hex[:8]);directory.mkdir()
    print('Run directory:',directory,flush=True)
    print('RUN.LOG:',directory/'run.log',flush=True)
    with store.run_lock(directory/'progress.sqlite'):
        db=store.connect(directory/'progress.sqlite')
        log=None
        try:
            store.init_progress(db)
            manifest=store.get(db,'manifest')
            hashes=engine_hashes()
            if manifest:
                if manifest['semantic_hash']!=config['_semantic_hash'] or manifest['engine_hashes']!=hashes or manifest['pipeline_version']!=pipeline.VERSION:
                    raise store.CheckpointError('Code/configuration is incompatible with this run; set RESUME=False to create a new run')
                config['run_id']=manifest['run_id']
            else:
                config['run_id']=directory.name.rsplit('_',1)[-1]
                try:
                    commit=subprocess.check_output(['git','-C',str(Path(__file__).parent),'rev-parse','HEAD'],stderr=subprocess.DEVNULL,text=True).strip()
                except (OSError,subprocess.CalledProcessError):commit='source-archive'
                with db:
                    store.put(db,'manifest',dict(run_id=config['run_id'],semantic_hash=config['_semantic_hash'],
                        engine_hashes=hashes,pipeline_version=pipeline.VERSION,version=(Path(__file__).parent/'VERSION').read_text('utf-8').strip(),
                        start_commit=commit,config=config,created_at=datetime.now(timezone.utc).isoformat()))
                # Empty outputs are not stage execution. They give every run the same six-file layout.
                for number,fields in [(1,pipeline.CSV1_FIELDS),(2,pipeline.CSV2_FIELDS),(3,pipeline.CSV3_FIELDS)]:
                    quiet=type('Quiet',(),{'info':lambda *a,**k:None,'warning':lambda *a,**k:None})()
                    output=store.AppendCSV(directory,number,fields,db,quiet);output.close()
                # No raw-data cache is opened by CSV3. A new run cannot execute CSV3 without upstream stages.
                cache=store.Cache(directory/'cache.sqlite',quiet);cache.close()
            log=RunLog(directory/'run.log',console=settings.get('CONSOLE_LOG',True),append=True)
            log.info('RUN_START',run_id=config['run_id'],stage=stage,resume=settings.get('RESUME',True),
                     timezone='UTC',config=config,source_version=hashes)
            summary=pipeline.run_pipeline(config,directory,log,stage,db)
            with db:store.put(db,'last_exit',dict(status='completed',stage=stage,summary=summary))
            print(json.dumps(dict(output=str(directory),summary=summary),ensure_ascii=False),flush=True)
            return directory,summary
        except BaseException as exc:
            if log:
                log.warning('RUN_INTERRUPTED' if isinstance(exc,KeyboardInterrupt) else 'RUN_FAILED',
                            stage=stage,reason=type(exc).__name__,detail=str(exc))
                with db:store.put(db,'last_exit',dict(status='interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed',reason=str(exc),stage=stage))
            raise
        finally:
            if log:log.close()
            db.close()


def main():
    if sys.version_info<(3,10):
        print('Python 3.10+ required',file=sys.stderr);return 2
    if len(sys.argv)>1:
        print('Edit the configuration block at the top of run_hlp.py, then run python run_hlp.py',file=sys.stderr);return 2
    try:
        run();return 0
    except KeyboardInterrupt:
        print('Interrupted; keep the six files and RESUME=True to continue.',file=sys.stderr);return 130
    except Exception:
        traceback.print_exc();return 2


if __name__=='__main__':
    raise SystemExit(main())
