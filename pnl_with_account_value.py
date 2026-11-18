"""Replace M-based outputs with FIFO PnL + accountValue; retain five price gaps."""
import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from decimal import Decimal as D, Inexact, localcontext
from fractions import Fraction as F
from pathlib import Path

INPUT_SHA = 'cd5ec689b6e3ff041186ba9b0b565c5f52cf42162d3455f58336964b98a62638'
EPISODE_SHA = '69eef0c5a6a4d33e01071ca5d8be942d512e65d1461ae04e7d0b30db404211ee'
FIFO_SHA = '653a7940b9fa329fb7a94183559518bf87bc38b9373375e97d82848bdffe249b'
EVENT_FILE = 'csv1_22044笔_HLP盈亏加AccountValue.csv'
EPISODE_FILE = '443个episode_HLP盈亏加AccountValue.csv'
REMOVED = {'M', 'M未计算原因', 'HLP盈亏_含M_USDC', '含M盈亏方向', '含M计算状态'}
ADDED = ['HLP盈亏_加AccountValue_USDC', '盈亏方向', '新公式计算状态', '历史价格数据缺失',
         '缺失价格字段', '历史价格缺失说明', '缺失对本次盈亏影响']
EXPECTED_GAPS = {
    'bd5f9795b0ce267c203ad5f3b69495bc7fb7b7923eefd5eb43ea09ef5260053d': ('EP000009', 'TRUMP', '清算标记价'),
    'a9dc9897d8d9f4b9e3bb017af85dcc2280d9707384509719f027a9e7dfef221f': ('EP000013', 'kPEPE', '清算标记价'),
    'b9b21341642d5e4b5d1eb93d9e9fd60b868d1dc5c0e80487b5f4b99c1bac54f3': ('EP000019', 'OM', '清算标记价'),
    '032137a30de78df8ae5d8557193d83e1a103b3eb4a39cd211e67492fcadc23ac': ('EP000151', 'HYPE', '被接管用户平均开仓价'),
    '71cf40dbe14c9845f8f017782eaf77cb11f88d545ada499d6a5ac1ba0be9153d': ('EP000162', 'SPX', '被接管用户平均开仓价'),
}


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''): h.update(b)
    return h.hexdigest()


def read(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        r = csv.DictReader(f)
        return r.fieldnames, list(r)


def text(value):
    if value is None: return ''
    if not isinstance(value, D): return str(value)
    if value == 0: return '0'
    s = format(value, 'f')
    return s.rstrip('0').rstrip('.') if '.' in s else s


def sign(value):
    return '未确定' if value is None else '盈利' if value > 0 else '亏损' if value < 0 else '持平'


def add_account_value(fifo, account):
    if fifo == '' or account == '': return None
    a, b = D(fifo), D(account)
    assert a.is_finite() and b.is_finite()
    with localcontext() as ctx:
        ctx.prec = max(100, max(a.adjusted(), b.adjusted()) - min(a.as_tuple().exponent, b.as_tuple().exponent) + 4)
        ctx.traps[Inexact] = True
        return a + b


def price_gap(row):
    fields, reasons = [], []
    for name, reason in [('被接管用户平均开仓价', '平均开仓价未补全原因'), ('清算标记价', '清算标记价未补全原因')]:
        if row[name] == '':
            fields.append(name); reasons.append('缺少' + name + '：' + row[reason])
    return '、'.join(fields), '；'.join(reasons)


def prepare(source, old_episodes, fifo_source, output):
    assert sha(source) == INPUT_SHA and sha(old_episodes) == EPISODE_SHA and sha(fifo_source) == FIFO_SHA
    headers, old = read(source); _, previous_groups = read(old_episodes); _, fifo_rows = read(fifo_source)
    fifo = {r['接管事件编号']: r for r in fifo_rows}
    assert len(fifo) == len(fifo_rows) == 22044
    retained = [k for k in headers if k not in REMOVED]
    new_headers = retained + ADDED
    assert len(retained) == 66 and not any('M' in k for k in new_headers)
    rows, groups, missing, seen = [], defaultdict(list), [], set()
    for r in old:
        row = {k: r[k] for k in retained}; row.update({k: '' for k in ADDED})
        if r['记录类型'] != 'fill':
            row['新公式计算状态'] = '非接管诊断，不计算'; row['历史价格数据缺失'] = '不适用（非接管）'
            rows.append(row); continue
        uid = r['接管事件编号']; assert uid not in seen; seen.add(uid)
        f = fifo[uid]
        assert all(r[k] == f[k] for k in ['接管组编号', '币种', '接管方向', 'HLP地址'])
        assert r['FIFO仓位盈亏_USDC'] == f['FIFO最终价差盈亏_USDC']
        result = add_account_value(r['FIFO仓位盈亏_USDC'], r['accountValue'])
        # Deliberately never read the old HLP盈亏_含M_USDC value.
        fields, reason = price_gap(r)
        if fields:
            assert (r['接管组编号'], r['币种'], fields) == EXPECTED_GAPS[uid]
            missing.append({'事件编号': uid, 'episode': r['接管组编号'], '币种': r['币种'], '缺失字段': fields})
        row.update(dict(zip(ADDED, [text(result), sign(result), '已计算' if result is not None else '缺公式必需字段，未计算',
                                   '是（原5笔缺价记录）' if fields else '否', fields, reason,
                                   '不影响新公式：FIFO盈亏和accountValue齐全；原始价格仍缺失' if fields and result is not None else
                                   '公式必需字段缺失，无法计算' if result is None else '无历史缺价标记'])))
        rows.append(row); groups[r['接管组编号']].append(row)
    assert seen == fifo.keys() and {r['事件编号'] for r in missing} == EXPECTED_GAPS.keys()
    assert set(groups) == {r['接管组编号'] for r in previous_groups} and len(groups) == 443
    episodes = []
    with localcontext() as ctx:
        ctx.prec = 256; ctx.traps[Inexact] = True
        for old_group in previous_groups:
            gid = old_group['接管组编号']; members = groups[gid]
            assert len(members) == int(old_group['接管笔数'])
            fifo_total = sum((D(r['FIFO仓位盈亏_USDC']) for r in members), D(0))
            account_total = sum((D(r['accountValue']) for r in members), D(0))
            totals = [D(r[ADDED[0]]) for r in members if r[ADDED[0]] != '']
            assert len(totals) == len(members), 'Fixed source has complete formula inputs'
            total = sum(totals, D(0)); assert total == fifo_total + account_total
            flagged = [r for r in members if r['缺失价格字段']]
            e = {k: old_group[k] for k in ['接管组编号', '币种', '接管方向', 'HLP地址', '接管组开始时间UTC', '接管组结束时间UTC', '接管笔数', '仅抵消旧仓笔数']}
            e.update({'FIFO仓位盈亏合计_USDC': text(fifo_total), 'AccountValue逐笔合计_USDC': text(account_total),
                      'HLP事件盈亏_加AccountValue_USDC': text(total), '盈亏方向': sign(total),
                      '已计算笔数': str(len(totals)), '公式必需字段缺失笔数': '0', '新公式计算状态': '完整（指定逐笔AccountValue口径）',
                      '历史缺价接管笔数': str(len(flagged)), '历史价格数据缺失': '是（保留标记，不影响本次公式）' if flagged else '否',
                      '历史缺价事件编号': '|'.join(r['接管事件编号'] for r in flagged),
                      '历史缺价字段及说明': '；'.join(r['币种'] + ' / ' + r['接管事件编号'] + ' / ' + r['历史价格缺失说明'] for r in flagged)})
            episodes.append(e)
        fills = [r for r in rows if r['记录类型'] == 'fill']
        totals = {k: text(sum((D(r[k]) for r in fills), D(0))) for k in ['FIFO仓位盈亏_USDC', 'accountValue', ADDED[0]]}
    payload = []
    for name, fields, data, prefix in [(EVENT_FILE, new_headers, rows, 66), (EPISODE_FILE, list(episodes[0]), episodes, 0)]:
        assert not any('M' in k for k in fields)
        payload.append({'name': name, 'columns': fields, 'matrix': [fields] + [[r[k] for k in fields] for r in data], 'preserved_columns': prefix})
    output.mkdir(parents=True, exist_ok=True)
    summary = {'status': 'PREPARED', 'source_sha256': INPUT_SHA, 'old_episode_sha256': EPISODE_SHA, 'fifo_sha256': FIFO_SHA,
               'source': str(source.resolve()), 'old_episodes': str(old_episodes.resolve()), 'fifo_source': str(fifo_source.resolve()),
               'events': len(fills), 'diagnostics': len(rows) - len(fills), 'episodes': len(episodes), 'event_signs': dict(Counter(r['盈亏方向'] for r in fills)),
               'episode_signs': dict(Counter(r['盈亏方向'] for r in episodes)), 'formula_missing_events': sum(r[ADDED[0]] == '' for r in fills),
               'historical_price_gaps': missing, 'totals': totals, 'removed_columns': sorted(REMOVED), 'preserved_columns': 66,
               'rule': 'FIFO price PnL + supplied per-row accountValue; no M and no extra direction sign',
               'scope': 'User-defined per-row sum; repeated liquidation-package accountValue is not deduplicated', 'aws_requests': 0}
    (output / 'payload.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    (output / 'prepared.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=True, indent=2))


def verify(output, source=None, old_episodes=None, fifo_source=None, write_report=True):
    summary = json.loads((output / 'prepared.json').read_text(encoding='utf-8'))
    source = source or Path(summary['source']); old_episodes = old_episodes or Path(summary['old_episodes']); fifo_source = fifo_source or Path(summary['fifo_source'])
    assert sha(source) == INPUT_SHA and sha(old_episodes) == EPISODE_SHA and sha(fifo_source) == FIFO_SHA
    oh, old = read(source); eh, events = read(output / EVENT_FILE); gh, eps = read(output / EPISODE_FILE)
    _, fifo_rows = read(fifo_source); fifo = {r['接管事件编号']: r for r in fifo_rows}
    _, old_eps = read(old_episodes)
    assert len(events) == len(old) == 22048 and len(eps) == 443
    assert eh[:66] == [k for k in oh if k not in REMOVED] and not any('M' in k for k in eh + gh)
    groups, seen_gaps, seen = defaultdict(list), set(), set()
    for a, b in zip(old, events):
        assert all(a[k] == b[k] for k in eh[:66])
        if a['记录类型'] != 'fill':
            assert b[ADDED[0]] == '' and b['盈亏方向'] == ''; continue
        uid = b['接管事件编号']; assert uid not in seen; seen.add(uid)
        value = F(fifo[uid]['FIFO最终价差盈亏_USDC']) + F(a['accountValue'])
        assert F(b[ADDED[0]]) == value and b['盈亏方向'] == sign(value)
        if uid in EXPECTED_GAPS:
            seen_gaps.add(uid); assert b['历史价格数据缺失'].startswith('是') and b['缺失价格字段'] == EXPECTED_GAPS[uid][2]
            assert b['历史价格缺失说明'] and a[EXPECTED_GAPS[uid][2]] == '' and b['新公式计算状态'] == '已计算'
        else:
            assert b['历史价格数据缺失'] == '否' and not b['缺失价格字段']
        groups[b['接管组编号']].append(b)
    assert seen == fifo.keys() and seen_gaps == EXPECTED_GAPS.keys()
    assert [e['接管组编号'] for e in eps] == [e['接管组编号'] for e in old_eps]
    for e in eps:
        rows = groups[e['接管组编号']]
        f = sum((F(r['FIFO仓位盈亏_USDC']) for r in rows), F(0)); a = sum((F(r['accountValue']) for r in rows), F(0))
        assert F(e['FIFO仓位盈亏合计_USDC']) == f and F(e['AccountValue逐笔合计_USDC']) == a
        assert F(e['HLP事件盈亏_加AccountValue_USDC']) == f + a == sum((F(r[ADDED[0]]) for r in rows), F(0))
        assert e['盈亏方向'] == sign(f + a) and int(e['已计算笔数']) == int(e['接管笔数']) == len(rows)
        assert int(e['历史缺价接管笔数']) == sum(r['接管事件编号'] in EXPECTED_GAPS for r in rows)
        assert set(filter(None, e['历史缺价事件编号'].split('|'))) == {r['接管事件编号'] for r in rows if r['接管事件编号'] in EXPECTED_GAPS}
    summary.update(status='PASS', verification='Independent Fraction recomputation of all events and episodes; retained cells and all five flags checked',
                   output_files={p.name: {'sha256': sha(p), 'bytes': p.stat().st_size} for p in output.glob('*.csv')})
    if write_report: (output / 'validation.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: summary[k] for k in ['status', 'event_signs', 'episode_signs', 'totals', 'formula_missing_events']}, ensure_ascii=True, indent=2))


def main():
    p = argparse.ArgumentParser(); p.add_argument('--source', type=Path); p.add_argument('--old-episodes', type=Path); p.add_argument('--fifo', type=Path)
    p.add_argument('--output', type=Path, required=True); p.add_argument('--verify', action='store_true'); a = p.parse_args()
    if a.verify: verify(a.output, a.source, a.old_episodes, a.fifo)
    else: prepare(a.source, a.old_episodes, a.fifo, a.output)


if __name__ == '__main__': main()
