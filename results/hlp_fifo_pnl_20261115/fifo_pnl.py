"""FIFO price PnL attribution. Decimal arithmetic; no network or official-PnL edits."""
import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal, getcontext
from pathlib import Path

getcontext().prec = 80
D = Decimal
ZERO = D(0)
AUDITED_SHA = 'e079bce1970a29c59342ef3f07f859cf67a413e85a55cc4d30acc8b0a3f104cb'


def number(x):
    return format(x, 'f') if isinstance(x, D) else x


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


@dataclass
class Lot:
    uid: str
    remaining: Decimal
    price: Decimal | None
    sign: int
    cycle: str
    time: str
    line: int
    origin: str


class Fifo:
    """One queue per account/coin, independent of CSV1 episode boundaries."""
    def __init__(self):
        self.lots = deque()
        self.position = ZERO

    def initial(self, position):
        assert not self.lots and self.position == 0
        self.position = position
        if position:
            self.lots.append(Lot('期初未知成本', abs(position), None,
                                 1 if position > 0 else -1, '', '', 0, '期初旧仓'))

    def trade(self, uid, start, size, side, price, before='', after='', time='', line=0, origin=''):
        assert start == self.position, ('POSITION_GAP', uid, start, self.position)
        assert size > 0 and price >= 0 and side in ('A', 'B')
        delta = size if side == 'B' else -size
        close = min(abs(start), size) if start * delta < 0 else ZERO
        opened = size - close
        end = start + delta
        matches = []
        left = close
        while left:
            assert self.lots, ('CLOSE_WITHOUT_LOT', uid)
            lot = self.lots[0]
            assert lot.sign == (1 if start > 0 else -1)
            assert not before or not lot.cycle or before == lot.cycle
            qty = min(left, lot.remaining)
            pnl = None if lot.price is None else qty * lot.sign * (price - lot.price)
            matches.append((lot, qty, pnl))
            lot.remaining -= qty
            left -= qty
            if lot.remaining == 0:
                self.lots.popleft()
        if opened:
            self.lots.append(Lot(uid, opened, price, 1 if end > 0 else -1,
                                 after, time, line, origin))
        self.position = end
        assert sum((x.remaining * x.sign for x in self.lots), ZERO) == end
        return close, opened, end, matches


MATCH_HEADERS = ['配对编号', '开仓接管事件编号', '退出成交记录编号', 'HLP地址', '币种',
                 '仓位周期编号', '多空方向', '开仓UTC', '退出UTC', '接管价格', '退出价格',
                 'FIFO配对数量', 'FIFO价差盈亏_USDC', '退出动作来源', '退出接管事件编号',
                 '开仓台账行号', '退出台账行号']


def write_csv(path, fields, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: number(r.get(k, '')) for k in fields})


def calculate(ledger, csv1, output):
    assert sha(ledger) == AUDITED_SHA, 'Use the independently verified 2026-11-15 ledger'
    output.mkdir(parents=True, exist_ok=True)
    with csv1.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        csv1_headers = reader.fieldnames
        source_rows = list(reader)
    seeds = {r['接管事件编号']: r for r in source_rows if r['记录类型'] == 'fill'}
    assert len(seeds) == 22044
    books = {}
    seen = set()
    events = {}
    cycle = defaultdict(lambda: dict(cash=ZERO, fifo=ZERO, official=ZERO, missing=0,
                                    count=0, open=ZERO, close=ZERO, first='', last='',
                                    owner='', coin='', side='', origins=Counter(), end=ZERO))
    statistics = Counter()
    previous = {}
    diagnostics = []
    match_count = 0
    match_path = output / 'FIFO逐笔退出配对.csv'
    with ledger.open(encoding='utf-8-sig', newline='') as f, match_path.open('w', encoding='utf-8-sig', newline='') as g:
        reader = csv.DictReader(f)
        writer = csv.writer(g)
        writer.writerow(MATCH_HEADERS)
        for line, r in enumerate(reader, 2):
            if r['记录类型'] != '成交':
                diagnostics.append(r)
                continue
            uid = r['记录编号']
            assert uid not in seen
            seen.add(uid)
            assert r['复核后轨迹完整性'].startswith('完整'), ('INCOMPLETE', uid)
            pair = (r['HLP地址'], r['币种'])
            if pair in previous:
                p = previous[pair]
                assert r['UTC时间'] >= p['UTC时间']
                if r['区块号'] and p['区块号']:
                    assert (int(r['区块号']), int(r['原始事件序号'])) >= (int(p['区块号']), int(p['原始事件序号']))
                elif r['UTC时间'] == p['UTC时间'] and r['来源文件'] == p['来源文件']:
                    assert (int(r['来源行号']), int(r['原始事件序号'])) >= (int(p['来源行号']), int(p['原始事件序号']))
            previous[pair] = r
            start, size, px = (D(r[k]) for k in ('规范化startPosition', '规范化数量', '价格'))
            if pair not in books:
                # All 206 observed pairs have a genuine zero baseline. Do not guess otherwise.
                assert start == 0, ('ZERO_BASELINE_MISSING', pair)
                books[pair] = Fifo()
            before, after = r['前仓位周期编号'], r['后仓位周期编号']
            close, opened, end, matches = books[pair].trade(uid, start, size, r['买卖侧'], px,
                                                         before, after, r['UTC时间'], line, r['动作来源'])
            assert end == D(r['规范化成交后仓位'])
            assert close + opened == size
            seed = seeds.get(uid)
            if seed:
                assert r['CSV1接管事件编号'] == uid
                assert seed['接管组编号'] == r['CSV1接管组编号']
                assert D(seed['接管价格']) == px
                assert seed['币种'] == r['币种']
                assert ('long' if r['买卖侧'] == 'B' else 'short') == seed['接管方向']
                assert float(seed['接管数量']) == float(r['数量']) == float(size)
                events[uid] = dict(row=r, line=line, total=size, close=close, opened=opened,
                                   exited=ZERO, pnl=ZERO, exit_value=ZERO, count=0,
                                   first_exit='', last_exit='', exit_origins=set(), old_pnl=ZERO,
                                   first_exit_uid='', last_exit_uid='', cycle=after if opened else '')
                statistics['pure_offset' if not opened else 'flip' if close else 'new_position'] += 1
            if opened:
                assert seed, ('UNINDEXED_OPENING', uid)
            triggered = ZERO
            for lot, qty, pnl in matches:
                assert pnl is not None and lot.uid in events
                event = events[lot.uid]
                event['exited'] += qty
                event['pnl'] += pnl
                event['exit_value'] += qty * px
                event['count'] += 1
                if not event['first_exit']:
                    event['first_exit'] = r['UTC时间']
                    event['first_exit_uid'] = uid
                event['last_exit'] = r['UTC时间']
                event['last_exit_uid'] = uid
                event['exit_origins'].add(r['动作来源'])
                cycle[lot.cycle]['fifo'] += pnl
                triggered += pnl
                match_count += 1
                writer.writerow([f'FM{match_count:09d}', lot.uid, uid, *pair, lot.cycle,
                                 '多头' if lot.sign > 0 else '空头', lot.time, r['UTC时间'],
                                 number(lot.price), r['价格'], number(qty), number(pnl),
                                 r['动作来源'], uid if seed else '', lot.line, line])
            if seed:
                events[uid]['old_pnl'] = triggered
            # Split reversing trades between the old/new cycles; one official PnL only.
            for cid, qty, signed, part in [(before, close, -1 if start > 0 else 1, 'close'),
                                           (after, opened, 1 if end > 0 else -1, 'open')]:
                if qty:
                    assert cid
                    c = cycle[cid]
                    c['cash'] -= qty * signed * px
                    c[part] += qty
                    c['owner'], c['coin'] = pair
                    c['side'] = '多头' if (start if part == 'close' else end) > 0 else '空头'
                    c['first'] = c['first'] or r['UTC时间']
                    c['last'] = r['UTC时间']
                    c['end'] = ZERO if part == 'close' and opened else end
                    c['origins'][r['动作来源']] += 1
            c = cycle[r['PnL所属周期编号']]
            c['count'] += 1
            if r['原始closedPnl'] == '':
                c['missing'] += 1
            else:
                c['official'] += D(r['原始closedPnl'])
            statistics['fills'] += 1
    assert set(events) == set(seeds)
    assert all(b.position == 0 and not b.lots for b in books.values())
    cycle_rows = []
    recon = Counter()
    for cid, c in sorted(cycle.items()):
        assert c['open'] == c['close'] and c['end'] == 0
        assert c['fifo'] == c['cash'], ('FIFO_CASHFLOW_MISMATCH', cid)
        diff = c['fifo'] - c['official'] if not c['missing'] else None
        status = '官方PnL缺失，无法对账' if diff is None else '完全一致' if diff == 0 else '差额不足0.01，原因未确认' if abs(diff) < D('.01') else '差额至少0.01，原因未确认'
        reason = ('原始node_trades缺少HLP侧PnL字段，未补值；价差可独立计算' if diff is None else
                  '' if diff == 0 else '按公开成交价和规范化数量计算；官方成本记账精度或其他口径差异尚未逐笔证实，不强行调平')
        c['status'] = status
        recon[status] += 1
        cycle_rows.append(dict(zip(['仓位周期编号', 'HLP地址', '币种', '多空方向', '开始UTC', '退出UTC',
                                    '新增数量', '退出数量', '剩余数量', 'FIFO价差盈亏_USDC', '独立买卖现金流_USDC',
                                    '官方closedPnl完整合计', '已观察官方closedPnl小计', '缺失官方PnL笔数',
                                    'FIFO减官方PnL差额', '官方PnL核对状态', '未确认原因'],
                                   [cid, c['owner'], c['coin'], c['side'], c['first'], c['last'],
                                    c['open'], c['close'], c['end'], c['fifo'], c['cash'],
                                    c['official'] if diff is not None else '', c['official'], c['missing'],
                                    diff if diff is not None else '', status, reason])))
    write_csv(output / '仓位周期盈亏核对.csv', list(cycle_rows[0]), cycle_rows)
    event_rows = []
    group_rows = defaultdict(list)
    for uid, seed in seeds.items():
        e = events[uid]
        r = e['row']
        remain = e['opened'] - e['exited']
        assert e['total'] == e['close'] + e['opened']
        assert remain == 0
        status = '仅抵消旧仓，无新增仓位' if e['opened'] == 0 else '新增仓位已全部退出'
        recon_cid = e['cycle'] or r['前仓位周期编号']
        row = dict(zip(['接管事件编号', '接管组编号', 'HLP地址', '币种', '接管时间UTC', '接管方向',
                        '原始接管数量', '计算接管数量', '接管价格', '接管前仓位', '当场抵消旧仓数量',
                        '实际新增仓位数量', 'FIFO已退出数量', 'FIFO剩余数量', 'FIFO已实现价差盈亏_USDC',
                        'FIFO最终价差盈亏_USDC', 'FIFO退出均价', '首次退出UTC', '最后退出UTC', 'FIFO配对笔数',
                        '退出动作来源', '退出状态', '新增仓位周期编号', '被抵消旧仓周期编号',
                        '本次触发旧批次FIFO盈亏_勿重复加总', '接管当笔原始closedPnl', '关联周期官方PnL核对状态',
                        '价差计算完整性', '官方PnL缺失笔数_关联周期', '首次退出成交编号', '最后退出成交编号',
                        '接管台账行号', '来源文件', '来源行号', '观察截止UTC'],
                       [uid, seed['接管组编号'], r['HLP地址'], r['币种'], r['UTC时间'], seed['接管方向'],
                        r['数量'], e['total'], r['价格'], r['规范化startPosition'], e['close'],
                        e['opened'], e['exited'], remain, e['pnl'], e['pnl'],
                        e['exit_value'] / e['exited'] if e['exited'] else '', e['first_exit'], e['last_exit'], e['count'],
                        '|'.join(sorted(e['exit_origins'])), status, e['cycle'], r['前仓位周期编号'] if e['close'] else '',
                        e['old_pnl'], r['原始closedPnl'], cycle[recon_cid]['status'],
                        '完整（按给定公开成交价及FIFO规则）', cycle[recon_cid]['missing'],
                        e['first_exit_uid'], e['last_exit_uid'], e['line'], r['来源文件'], r['来源行号'], r['观察截止UTC']]))
        event_rows.append(row)
        group_rows[seed['接管组编号']].append(row)
    write_csv(output / '每笔接管HLP盈亏_FIFO.csv', list(event_rows[0]), event_rows)
    enriched = {r['接管事件编号']: r for r in event_rows}
    extra = [k for k in event_rows[0] if k not in csv1_headers]
    write_csv(output / 'csv1_含每笔HLP_FIFO盈亏.csv', csv1_headers + extra,
              [{**r, **{k: enriched.get(r['接管事件编号'], {}).get(k, '') for k in extra}} for r in source_rows])
    groups = []
    for gid, rows in sorted(group_rows.items()):
        assert len({r['币种'] for r in rows}) == 1
        groups.append({'接管组编号': gid, '币种': rows[0]['币种'], 'HLP地址': '|'.join(sorted({r['HLP地址'] for r in rows})),
                       '接管笔数': len(rows), '仅抵消旧仓笔数': sum(r['实际新增仓位数量'] == 0 for r in rows),
                       '接管总数量': sum((r['计算接管数量'] for r in rows), ZERO),
                       '当场抵消旧仓数量': sum((r['当场抵消旧仓数量'] for r in rows), ZERO),
                       '实际新增仓位数量': sum((r['实际新增仓位数量'] for r in rows), ZERO),
                       'FIFO已退出数量': sum((r['FIFO已退出数量'] for r in rows), ZERO),
                       'FIFO剩余数量': sum((r['FIFO剩余数量'] for r in rows), ZERO),
                       'FIFO价差盈亏_USDC': sum((r['FIFO最终价差盈亏_USDC'] for r in rows), ZERO)})
    write_csv(output / '接管组HLP盈亏_FIFO.csv', list(groups[0]), groups)
    write_csv(output / '来源覆盖诊断.csv', ['记录编号', 'UTC时间', '原因代码', '诊断说明'], diagnostics)
    pnl = sum((e['pnl'] for e in events.values()), ZERO)
    assert pnl == sum((c['cash'] for c in cycle.values()), ZERO)
    result = dict(calculation_status='PASS', official_reconciliation_status='PARTIAL_UNRESOLVED',
                  source_ledger_sha256=sha(ledger), source_csv1_sha256=sha(csv1),
                  source_ledger=str(ledger.resolve()), source_csv1=str(csv1.resolve()),
                  conversation='6af99f15-f1cc-83ea-9b90-417833be94b3',
                  events=len(events), groups=len(groups), cycles=len(cycle), pairs=len(books),
                  counts=dict(statistics), matches=match_count, diagnostic_rows=len(diagnostics),
                  fifo_price_pnl=str(pnl), official_observed_pnl=str(sum((c['official'] for c in cycle.values()), ZERO)),
                  fifo_pnl_official_missing_cycles=str(sum((c['fifo'] for c in cycle.values() if c['missing']), ZERO)),
                  fifo_pnl_official_complete_cycles=str(sum((c['fifo'] for c in cycle.values() if not c['missing']), ZERO)),
                  reconciliation=dict(recon), remaining_lots=0, aws_requests=0, network_requests=0,
                  profitable_events=sum(e['pnl'] > 0 for e in events.values()),
                  loss_events=sum(e['pnl'] < 0 for e in events.values()), zero_events=sum(e['pnl'] == 0 for e in events.values()),
                  output_files={p.name: {'sha256': sha(p), 'bytes': p.stat().st_size} for p in output.glob('*.csv')})
    (output / 'validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True, indent=2))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--ledger', type=Path, required=True)
    p.add_argument('--csv1', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    calculate(a.ledger, a.csv1, a.output)


if __name__ == '__main__':
    main()
