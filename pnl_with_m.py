"""Join immutable, user-selected M with FIFO PnL. Preserve missingness and signs."""
import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from decimal import Decimal as D, Inexact, localcontext
from fractions import Fraction as F
from pathlib import Path

UM_SHA = '7a35db960d80931d5fa0b3a3c34c09a13c27732c941027aa0858c056484c03a6'
FIFO_SHA = '653a7940b9fa329fb7a94183559518bf87bc38b9373375e97d82848bdffe249b'
EVENT_FILE = 'csv1_22044笔_含U_M及HLP盈亏.csv'
GROUP_FILE = '443个episode_含M事件盈亏.csv'
MISSING_FILE = '5笔M缺失及受影响episode.csv'
EXTRA = ['HLP地址', 'FIFO仓位盈亏_USDC', 'HLP盈亏_含M_USDC', '含M盈亏方向',
         '含M计算状态', 'FIFO退出状态', '实际新增仓位数量', '当场抵消旧仓数量', '关联周期官方PnL核对状态']


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''): h.update(b)
    return h.hexdigest()


def read(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        r = csv.DictReader(f)
        return r.fieldnames, list(r)


def text(x):
    if x is None: return ''
    if isinstance(x, D):
        if x == 0: return '0'
        s = format(x, 'f')
        return s.rstrip('0').rstrip('.') if '.' in s else s
    return str(x)


def sign(value):
    return '未确定' if value is None else '盈利' if value > 0 else '亏损' if value < 0 else '持平'


def combine(pnl, margin):
    if margin == '': return None
    a, b = D(pnl), D(margin)
    assert a.is_finite() and b.is_finite()
    with localcontext() as ctx:
        ctx.prec = max(100, max(a.adjusted(), b.adjusted()) - min(a.as_tuple().exponent, b.as_tuple().exponent) + 4)
        ctx.traps[Inexact] = True
        return a + b


def group_result(rows):
    with localcontext() as ctx:
        ctx.prec = 256; ctx.traps[Inexact] = True
        known = [r for r in rows if r['M'] != '']
        total_fifo = sum((D(r['FIFO仓位盈亏_USDC']) for r in rows), D(0))
        known_m = sum((D(r['M']) for r in known), D(0))
        known_fifo = sum((D(r['FIFO仓位盈亏_USDC']) for r in known), D(0))
        known_total = known_m + known_fifo
        missing = len(rows) - len(known)
        return dict(fifo=total_fifo, known_m=known_m, known_fifo=known_fifo,
                    known_total=known_total, missing=missing, known=len(known),
                    total=None if missing else known_total)


def prepare(um, fifo, output):
    assert sha(um) == UM_SHA and sha(fifo) == FIFO_SHA
    output.mkdir(parents=True, exist_ok=True)
    headers, raw = read(um); _, prices = read(fifo)
    price = {r['接管事件编号']: r for r in prices}
    assert len(price) == len(prices) == 22044
    assert len(headers) == 62 and not set(EXTRA) & set(headers)
    events, missing_rows, groups, seen = [], [], defaultdict(list), set()
    counts = Counter()
    for r in raw:
        v = dict(r)
        if r['记录类型'] != 'fill':
            v.update({k: '' for k in EXTRA}); v['含M计算状态'] = '非接管诊断，不计算'
            events.append(v); counts['diagnostics'] += 1; continue
        uid = r['接管事件编号']; assert uid not in seen; seen.add(uid)
        f = price[uid]
        assert all(r[k] == f[k] for k in ['接管组编号', '币种', '接管方向'])
        assert D(r['接管价格']) == D(f['接管价格'])
        assert D(r['接管数量']) == D(f['原始接管数量'])
        assert D(f['FIFO剩余数量']) == 0
        pnl = f['FIFO最终价差盈亏_USDC']; total = combine(pnl, r['M'])
        v.update(dict(zip(EXTRA, [f['HLP地址'], pnl, text(total), sign(total),
                                 '缺少M，完整盈亏未确定' if total is None else '已计算（指定逐笔M口径）',
                                 f['退出状态'], f['实际新增仓位数量'], f['当场抵消旧仓数量'],
                                 f['关联周期官方PnL核对状态']])))
        events.append(v); groups[r['接管组编号']].append(v); counts[sign(total)] += 1
        if total is None:
            assert r['M未计算原因']
            missing_rows.append({k: v[k] for k in ['接管事件编号', '接管组编号', '币种', '接管时间UTC',
                                                  'accountValue', 'U', 'M', 'FIFO仓位盈亏_USDC', 'M未计算原因']})
        else:
            assert F(text(total)) == F(pnl) + F(r['M'])
    assert seen == price.keys() and len(groups) == 443
    episodes = []
    for gid, rows in sorted(groups.items()):
        z = group_result(rows)
        assert len({r['币种'] for r in rows}) == len({r['接管方向'] for r in rows}) == 1
        episodes.append(dict(zip(['接管组编号', '币种', '接管方向', 'HLP地址', '接管组开始时间UTC', '接管组结束时间UTC',
                                 '接管笔数', '含M已计算笔数', 'M缺失笔数', 'FIFO仓位盈亏合计_USDC',
                                 'M完整合计_USDC', 'HLP完整事件盈亏_含M_USDC', '含M盈亏方向', '计算状态',
                                 '已知M小计_USDC', '已计算部分FIFO盈亏_USDC', '已计算部分含M盈亏_USDC',
                                 '缺M记录FIFO盈亏_USDC', '仅抵消旧仓笔数', '缺M事件编号', 'M缺失原因'],
                                [gid, rows[0]['币种'], rows[0]['接管方向'], '|'.join(sorted({r['HLP地址'] for r in rows})),
                                 rows[0]['接管组开始时间UTC'], rows[0]['接管组结束时间UTC'], len(rows), z['known'], z['missing'],
                                 z['fifo'], None if z['missing'] else z['known_m'], z['total'], sign(z['total']),
                                 '不完整，禁止将已知小计当完整事件盈亏' if z['missing'] else '完整（指定逐笔M口径）',
                                 z['known_m'], z['known_fifo'], z['known_total'],
                                 combine(text(z['fifo']), text(-z['known_fifo'])),
                                 sum(D(r['实际新增仓位数量']) == 0 for r in rows),
                                 '|'.join(r['接管事件编号'] for r in rows if r['M'] == ''),
                                 '；'.join(dict.fromkeys(r['M未计算原因'] for r in rows if r['M'] == ''))])))
    payloads = []
    for filename, fields, rows in [(EVENT_FILE, headers + EXTRA, events),
                                   (GROUP_FILE, list(episodes[0]), episodes),
                                   (MISSING_FILE, list(missing_rows[0]), missing_rows)]:
        payloads.append(dict(name=filename, columns=fields, matrix=[fields] + [[text(r[k]) for k in fields] for r in rows],
                             preserved_columns=62 if filename == EVENT_FILE else 0))
    all_z = group_result([r for r in events if r['记录类型'] == 'fill'])
    summary = dict(status='PREPARED', formula='HLP_pnl_with_M = FIFO_price_pnl + supplied_M',
                   sign='positive=profit; negative=loss; zero=flat; missing_M=unknown',
                   um_source=str(um.resolve()), um_sha256=UM_SHA, fifo_source=str(fifo.resolve()), fifo_sha256=FIFO_SHA,
                   events=22044, episodes=443, counts=dict(counts),
                   episode_signs=dict(Counter(r['含M盈亏方向'] for r in episodes)),
                   missing_episode_ids=[r['接管组编号'] for r in episodes if r['M缺失笔数']],
                   totals={k: text(v) for k, v in all_z.items()},
                   full_total_available=False, preserved_columns=62, extra_headers=EXTRA,
                   duplicate_package_policy='Use supplied per-fill M literally; no package deduplication or economic-transfer claim',
                   aws_requests=0, new_source_requests=0)
    (output / 'payload.json').write_text(json.dumps(payloads, ensure_ascii=False), encoding='utf-8')
    (output / 'prepared.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=True, indent=2))


def verify(output, um=None, fifo_source=None, write_report=True):
    summary = json.loads((output / 'prepared.json').read_text(encoding='utf-8'))
    um = um or Path(summary['um_source'])
    fifo_source = fifo_source or Path(summary['fifo_source'])
    assert sha(um) == UM_SHA and sha(fifo_source) == FIFO_SHA
    original_headers, original = read(um)
    _, fifo = read(fifo_source)
    p = {r['接管事件编号']: r for r in fifo}
    event_headers, events = read(output / EVENT_FILE)
    assert len(events) == len(original) == 22048 and event_headers[:62] == original_headers
    bygroup = defaultdict(list)
    for a, b in zip(original, events):
        assert all(a[k] == b[k] for k in original_headers)
        if a['记录类型'] != 'fill':
            assert b['HLP盈亏_含M_USDC'] == ''
            continue
        f = p[a['接管事件编号']]
        assert b['FIFO仓位盈亏_USDC'] == f['FIFO最终价差盈亏_USDC']
        if a['M'] == '':
            assert b['HLP盈亏_含M_USDC'] == '' and b['含M盈亏方向'] == '未确定'
        else:
            q = F(a['接管数量']) * (1 if a['接管方向'] == 'long' else -1)
            assert F(a['U']) == (F(a['清算标记价']) - F(a['被接管用户平均开仓价'])) * q
            assert F(a['M']) == F(a['accountValue']) - F(a['U'])
            val = F(a['M']) + F(f['FIFO最终价差盈亏_USDC'])
            assert F(b['HLP盈亏_含M_USDC']) == val and b['含M盈亏方向'] == sign(val)
        bygroup[a['接管组编号']].append(b)
    _, episodes = read(output / GROUP_FILE)
    assert len(episodes) == len({r['接管组编号'] for r in episodes}) == 443
    for e in episodes:
        rows = bygroup[e['接管组编号']]; known = [r for r in rows if r['M'] != '']
        assert int(e['接管笔数']) == len(rows) and int(e['M缺失笔数']) == len(rows) - len(known)
        assert F(e['FIFO仓位盈亏合计_USDC']) == sum((F(r['FIFO仓位盈亏_USDC']) for r in rows), F(0))
        assert F(e['已知M小计_USDC']) == sum((F(r['M']) for r in known), F(0))
        z = sum((F(r['HLP盈亏_含M_USDC']) for r in known), F(0))
        assert F(e['已计算部分含M盈亏_USDC']) == z
        if len(known) < len(rows):
            assert e['HLP完整事件盈亏_含M_USDC'] == e['M完整合计_USDC'] == ''
            assert e['含M盈亏方向'] == '未确定'
        else:
            assert F(e['HLP完整事件盈亏_含M_USDC']) == z and e['含M盈亏方向'] == sign(z)
    summary.update(status='PASS', verification='Independent Fraction recomputation, every original cell and event/group join checked',
                   output_files={p.name: {'sha256': sha(p), 'bytes': p.stat().st_size} for p in output.glob('*.csv')})
    if write_report:
        (output / 'validation.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: summary[k] for k in ['status', 'counts', 'episode_signs', 'missing_episode_ids', 'totals']}, ensure_ascii=True, indent=2))


def main():
    a = argparse.ArgumentParser(); a.add_argument('--um', type=Path); a.add_argument('--fifo', type=Path)
    a.add_argument('--output', type=Path, required=True); a.add_argument('--verify', action='store_true')
    args = a.parse_args()
    if args.verify: verify(args.output, args.um, args.fifo)
    else: prepare(args.um, args.fifo, args.output)


if __name__ == '__main__': main()
