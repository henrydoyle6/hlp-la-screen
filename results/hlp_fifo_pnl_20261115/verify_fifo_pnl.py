"""Independent cumulative-quantity integration, not the FIFO queue implementation."""
import argparse
import bisect
import csv
import hashlib
import json
from collections import defaultdict
from decimal import Decimal as D, getcontext
from pathlib import Path

getcontext().prec = 80


def read(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        yield from csv.DictReader(f)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--ledger', type=Path, required=True)
    p.add_argument('--csv1', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    info = json.loads((a.output / 'validation.json').read_text(encoding='utf-8'))
    for path, expected in [(a.ledger, info['source_ledger_sha256']), (a.csv1, info['source_csv1_sha256'])]:
        with path.open('rb') as f:
            assert hashlib.file_digest(f, 'sha256').hexdigest() == expected
    sources = {}
    cycles = defaultdict(lambda: {'entries': [], 'ends': [D(0)], 'costs': [D(0)], 'prices': []})
    for line, r in enumerate(read(a.ledger), 2):
        if r['记录类型'] != '成交':
            continue
        q, sz, px = [D(r[k]) for k in ['规范化startPosition', '规范化数量', '价格']]
        delta = sz if r['买卖侧'] == 'B' else -sz
        cl = min(abs(q), sz) if q * delta < 0 else D(0)
        op = sz - cl
        uid = r['记录编号']
        assert uid not in sources
        sources[uid] = (r['HLP地址'], r['币种'], px, cl, op, line, r['原始closedPnl'], r['前仓位周期编号'], r['后仓位周期编号'])
        if op:
            cycles[r['后仓位周期编号']]['entries'].append((uid, op, px, 1 if delta > 0 else -1))
        if cl:
            c = cycles[r['前仓位周期编号']]
            c['ends'].append(c['ends'][-1] + cl)
            c['costs'].append(c['costs'][-1] + cl * px)
            c['prices'].append(px)
    expected_pnl = {}
    for cid, c in cycles.items():
        def integral(x):
            i = bisect.bisect_right(c['ends'], x) - 1
            return c['costs'][i] + ((x - c['ends'][i]) * c['prices'][i] if i < len(c['prices']) else D(0))
        cursor = D(0)
        for uid, qty, px, sign in c['entries']:
            end = cursor + qty
            expected_pnl[uid] = (integral(end) - integral(cursor) - qty * px) * sign
            cursor = end
        assert cursor == c['ends'][-1]
    byentry = defaultdict(lambda: [D(0), D(0)])
    byexit = defaultdict(D)
    pairs = set()
    for n, r in enumerate(read(a.output / 'FIFO逐笔退出配对.csv'), 1):
        assert r['配对编号'] == f'FM{n:09d}'
        u, v = r['开仓接管事件编号'], r['退出成交记录编号']
        assert (u, v) not in pairs
        pairs.add((u, v))
        x, y = sources[u], sources[v]
        assert x[:2] == y[:2] == (r['HLP地址'], r['币种'])
        assert x[5] < y[5]
        assert (x[5], y[5]) == (int(r['开仓台账行号']), int(r['退出台账行号']))
        assert x[8] == y[7] == r['仓位周期编号']
        qty, pnl, entrypx, exitpx = [D(r[k]) for k in ['FIFO配对数量', 'FIFO价差盈亏_USDC', '接管价格', '退出价格']]
        assert entrypx == x[2] and exitpx == y[2] and qty > 0
        assert pnl == qty * (exitpx - entrypx) * (1 if r['多空方向'] == '多头' else -1)
        byentry[u][0] += qty; byentry[u][1] += pnl; byexit[v] += qty
    assert len(pairs) == info['matches']
    for uid, s in sources.items():
        assert byexit[uid] == s[3]
        assert byentry[uid][0] == s[4]
    events = list(read(a.output / '每笔接管HLP盈亏_FIFO.csv'))
    assert len(events) == len({r['接管事件编号'] for r in events}) == 22044
    total = D(0)
    for r in events:
        uid = r['接管事件编号']; s = sources[uid]
        assert D(r['FIFO最终价差盈亏_USDC']) == expected_pnl.get(uid, D(0)) == byentry[uid][1]
        assert r['接管当笔原始closedPnl'] == s[6]
        assert D(r['FIFO已退出数量']) == D(r['实际新增仓位数量']) == s[4]
        assert D(r['FIFO剩余数量']) == 0
        total += D(r['FIFO最终价差盈亏_USDC'])
    enriched = list(read(a.output / 'csv1_含每笔HLP_FIFO盈亏.csv'))
    originals = list(read(a.csv1))
    assert len(enriched) == len(originals) == 22048
    for original, new in zip(originals, enriched):
        assert all(original[k] == new[k] for k in original)
    assert total == D(info['fifo_price_pnl'])
    assert total == sum((D(r['FIFO价差盈亏_USDC']) for r in read(a.output / '接管组HLP盈亏_FIFO.csv')), D(0))
    assert total == sum((D(r['FIFO价差盈亏_USDC']) for r in read(a.output / '仓位周期盈亏核对.csv')), D(0))
    result = dict(status='PASS', method='Independent cumulative exit-value integration over opening quantity intervals',
                  events=22044, unique_source_fills=len(sources), matches=len(pairs), cycles=len(cycles),
                  csv1_original_columns_preserved=len(originals[0]), enriched_rows=len(enriched),
                  fifo_price_pnl=str(total), per_entry_and_exit_quantity_conserved=True,
                  no_future_or_cross_account_matches=True, official_pnl_not_reallocated=True)
    (a.output / 'independent_verification.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
