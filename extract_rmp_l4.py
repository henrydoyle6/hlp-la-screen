"""Explicit standalone L4 export using the same native SQLite extraction backend."""
import argparse
from pathlib import Path
from hlp_la_screen import SnapshotRef,RunLog
from input_discovery import rmp_header
from rmp_l4 import ExtractionCache,ExtractionError,digest,save
from state_store import Cache


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True,type=Path)
    parser.add_argument('--coins',required=True,nargs='+')
    parser.add_argument('--output-dir',required=True,type=Path)
    args=parser.parse_args(argv)
    args.output_dir.mkdir(parents=True,exist_ok=True)
    log=RunLog(args.output_dir/'extract.log',console=True)
    cache=Cache(args.output_dir/'cache.sqlite',log)
    try:
        header=rmp_header(args.source)
        ref=SnapshotRef(str(args.source),header['block_number'],header['time_ms'])
        result=ExtractionCache(cache,log).ensure(ref,set(args.coins))
        failed=False
        for coin,entry in sorted(result.items()):
            if isinstance(entry,ExtractionError):
                log.warning('COIN_EXTRACTION_FAILED',coin=coin,reason=str(entry));failed=True
            else:
                path=args.output_dir/(digest(coin)+'.json')
                save(path,cache.read('extract',entry));log.info('L4_EXPORTED',coin=coin,path=str(path))
        return int(failed)
    finally:cache.close();log.close()


if __name__=='__main__':raise SystemExit(main())
