"""Standalone demanded-coin extraction using the SAME backend as the screener."""
import argparse
import json
import os
from pathlib import Path
import sys
from hlp_la_screen import SnapshotRef, parse_time, RunLog, load_snapshot
from rmp_l4 import ExtractionCache, ExtractionError, CONVERSION_VERSION, save


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True,type=Path)
    parser.add_argument('--block',required=True,type=int)
    parser.add_argument('--time',required=True,help='actual embedded snapshot time, UTC')
    parser.add_argument('--coins',nargs='+',default=[])
    parser.add_argument('--coins-file',type=Path,help='JSON array of explicitly demanded coin names')
    parser.add_argument('--cache-dir',required=True,type=Path)
    parser.add_argument('--manifest',required=True,type=Path)
    args=parser.parse_args(argv)
    coins=set(args.coins)
    if args.coins_file:
        value=json.loads(args.coins_file.read_text(encoding='utf-8-sig'))
        if not isinstance(value,list) or any(not isinstance(c,str) or not c for c in value):
            parser.error('--coins-file must be a JSON array of nonempty strings')
        coins.update(value)
    if not coins:parser.error('supply --coins or --coins-file')
    args.cache_dir.mkdir(parents=True,exist_ok=True)
    args.manifest.parent.mkdir(parents=True,exist_ok=True)
    log=RunLog(args.manifest.with_suffix('.log'),console=True)
    ref=SnapshotRef(os.path.normcase(os.path.realpath(args.source)),args.block,parse_time(args.time))
    cache=ExtractionCache(args.cache_dir,log)
    try:
        def validate(path,coin):load_snapshot(SnapshotRef(str(path),ref.block_number,ref.time_ms),coin)
        result=cache.ensure(ref,coins,validate)
        report={'source':ref.path,'block_number':ref.block_number,'time_ms':ref.time_ms,
            'conversion_version':CONVERSION_VERSION,'stats':cache.stats,
            'coins':[{'coin':coin,'path':str(value)} if isinstance(value,Path) else
                     {'coin':coin,'error':value.code,'message':str(value)} for coin,value in sorted(result.items())]}
        args.manifest.parent.mkdir(parents=True,exist_ok=True)
        save(args.manifest.with_suffix(args.manifest.suffix+'.tmp'),report)
        os.replace(args.manifest.with_suffix(args.manifest.suffix+'.tmp'),args.manifest)
        print(json.dumps({'markets':len(coins),**cache.stats,'manifest':str(args.manifest)},ensure_ascii=False))
        return int(any(isinstance(v,ExtractionError) for v in result.values()))
    finally:log.close()


if __name__=='__main__':raise SystemExit(main())
