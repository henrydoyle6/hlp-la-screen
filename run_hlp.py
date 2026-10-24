#!/usr/bin/env python3
"""Edit ONLY the configuration block below, then run: python run_hlp.py

Dates are inclusive UTC calendar dates. Directory paths must be absolute paths
on the Ubuntu machine. See README.txt for layout, installation and output rules.
"""

# ================== USER CONFIGURATION / 只修改本区 ==================
START_DATE = '2025-11-12'     # UTC 开始日期，包含当天
END_DATE = '2025-11-13'       # UTC 结束日期，包含当天

FILLS_ROOT = '/data/hyperliquid/fills'  # 下一级为 YYYYMMDD/H.zip
DIFFS_ROOT = '/data/hyperliquid/node_raw_book_diffs_by_block'
SNAPSHOTS_ROOT = '/data/hyperliquid/periodic_abci_states'
OUTPUT_ROOT = '/data/hlp_la/output'     # 每次自动创建独立 run_* 目录
CACHE_ROOT = '/data/hlp_la/cache'       # 跨次复用；与三个原始数据目录分开

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
PREPARE_COIN_DIFFS = True    # 自动提取本次所需币种的 diff 输入，缓存复用
# ================== END CONFIGURATION / 以下不用修改 ==================

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
import traceback
import uuid


def settings_from_top():
    return {name: globals()[name] for name in (
        'START_DATE', 'END_DATE', 'FILLS_ROOT', 'DIFFS_ROOT', 'SNAPSHOTS_ROOT',
        'OUTPUT_ROOT', 'CACHE_ROOT', 'HLP_ACCOUNTS', 'DETAILED_LOG', 'CONSOLE_LOG',
        'LOG_BLOCK_INTERVAL', 'PREPARE_COIN_DIFFS')}


def build_config(settings):
    from hlp_la_screen import parse_time, time_text
    def day(name):
        value = settings[name]
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError(f'{name}: use YYYY-MM-DD')
        return date.fromisoformat(value)
    first, last = day('START_DATE'), day('END_DATE')
    if last < first:
        raise ValueError('END_DATE must be on or after START_DATE')
    paths = {}
    for key in ('FILLS_ROOT', 'DIFFS_ROOT', 'SNAPSHOTS_ROOT', 'OUTPUT_ROOT', 'CACHE_ROOT'):
        raw = os.path.expandvars(os.path.expanduser(str(settings[key])))
        p = Path(raw)
        if not p.is_absolute():
            raise ValueError(f'{key}: use an absolute Ubuntu path')
        paths[key] = p.resolve()
    inputs = [paths[k] for k in ('FILLS_ROOT', 'DIFFS_ROOT', 'SNAPSHOTS_ROOT')]
    for key in ('OUTPUT_ROOT', 'CACHE_ROOT'):
        if any(paths[key] == p or p in paths[key].parents for p in inputs):
            raise ValueError(f'{key} must be outside the raw input directories')
    if paths['OUTPUT_ROOT'] == paths['CACHE_ROOT']:
        raise ValueError('OUTPUT_ROOT and CACHE_ROOT must be separate directories')
    for k in ('FILLS_ROOT', 'DIFFS_ROOT', 'SNAPSHOTS_ROOT'):
        if not paths[k].is_dir():
            raise ValueError(f'{k}: directory does not exist: {paths[k]}')
    accounts = [str(a).lower() for a in settings['HLP_ACCOUNTS']]
    if not accounts or any(not re.fullmatch(r'0x[0-9a-f]{40}', a) for a in accounts):
        raise ValueError('HLP_ACCOUNTS must contain valid 0x + 40 hex addresses')
    interval = settings['LOG_BLOCK_INTERVAL']
    if type(interval) is not int or interval <= 0:
        raise ValueError('LOG_BLOCK_INTERVAL must be a positive integer')
    core_start = parse_time(first.isoformat() + 'T00:00:00Z')
    exclusive_end = parse_time((last + timedelta(days=1)).isoformat() + 'T00:00:00Z')
    cache = paths['CACHE_ROOT']
    config = dict(data_start=time_text(core_start - 3_600_000),
        data_end=time_text(exclusive_end + 3_600_000 - 1),
        fills=dict(format='node', hlp_addresses=sorted(set(accounts))),
        snapshots=[], diffs=dict(coverage_mode='dense', chunks=[]),
        rmp_cache_dir=str(cache / 'rmp_l4'),
        auto_input_discovery=dict(fills_root=str(paths['FILLS_ROOT']),
            diffs_root=str(paths['DIFFS_ROOT']), snapshots_root=str(paths['SNAPSHOTS_ROOT']),
            index_dir=str(cache / 'headers')),
        detailed_log=bool(settings['DETAILED_LOG']), console_log=bool(settings['CONSOLE_LOG']),
        log_block_interval=interval, append_run_log=True, overwrite=True)
    if settings['PREPARE_COIN_DIFFS']:
        config['local_diff_preparation'] = dict(cache_dir=str(cache / 'diff_inputs'))
    return config, paths


def run(settings=None):
    from hlp_la_screen import RunLog, run_screen
    from input_discovery import atomic_json, discover_fills
    settings = settings_from_top() if settings is None else settings
    config, paths = build_config(settings)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    name = f"run_{settings['START_DATE']}_{settings['END_DATE']}_{stamp}_{uuid.uuid4().hex[:8]}"
    run_dir = paths['OUTPUT_ROOT'] / name
    result_dir = run_dir / 'result'
    result_dir.mkdir(parents=True, exist_ok=False)
    config['auto_input_discovery'].update(run_dir=str(run_dir), missing_root=str(run_dir / 'unavailable_inputs'))
    print(f'Run directory: {run_dir}', flush=True)
    print(f'RUN.LOG: {result_dir / "run.log"}', flush=True)
    atomic_json(run_dir / 'user_config.json', settings)
    code_dir = run_dir / 'source'
    code_dir.mkdir()
    hashes = {}
    for source in Path(__file__).resolve().parent.iterdir():
        if source.is_file() and (source.suffix == '.py' or source.name in ('README.txt', 'requirements.txt')):
            data = source.read_bytes()
            (code_dir / source.name).write_bytes(data)
            hashes[source.name] = hashlib.sha256(data).hexdigest()
    atomic_json(run_dir / 'source_sha256.json', hashes)
    log = RunLog(result_dir / 'run.log', console=config['console_log'])
    try:
        log.info('AUTO_RUN_START', settings=settings, platform=platform.platform(), python=sys.version,
                 formal_start=settings['START_DATE'], formal_end_inclusive=settings['END_DATE'],
                 timezone='UTC', source_sha256=hashes)
        discover_fills(config, log)
        atomic_json(run_dir / 'resolved_config.json', config)
    except Exception as exc:
        log.warning('AUTO_RUN_FAILED', phase='fills_discovery', reason=type(exc).__name__, detail=str(exc))
        atomic_json(run_dir / 'process_exit.json', dict(status='FAILED', reason=str(exc)))
        raise
    finally:
        log.close()
    try:
        # Unique owned directory: overwrite only permits appending the discovery
        # log just created here. It never overwrites a previous run's CSV files.
        if any((result_dir / f'csv{i}.csv').exists() for i in (1, 2, 3)):
            raise FileExistsError('Unexpected core output in new run directory')
        summary = run_screen(config, result_dir)
        atomic_json(run_dir / 'run_summary.json', summary)
        atomic_json(run_dir / 'process_exit.json', dict(status='COMPLETED', summary=summary))
        print(json.dumps(dict(output=str(result_dir), summary=summary), ensure_ascii=False, indent=2), flush=True)
        return run_dir, summary
    except Exception as exc:
        atomic_json(run_dir / 'process_exit.json', dict(status='FAILED', reason=str(exc)))
        raise


def main():
    if sys.version_info < (3, 10):
        print('Python 3.10 or later is required', file=sys.stderr)
        return 2
    try:
        run()
        return 0
    except KeyboardInterrupt:
        print('Interrupted; keep run.log and rerun into a new run directory. Completed extraction caches remain reusable.', file=sys.stderr)
        return 130
    except Exception:
        traceback.print_exc()
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
