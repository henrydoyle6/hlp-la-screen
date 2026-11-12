"""Offline verification of the delivered single-episode CoinAPI pilot.

This reads committed results, not raw data, credentials or network endpoints.
It does not claim to repeat the historical replay performed for acceptance.
"""
import csv
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import official_l2 as layout


def require(value, message):
    if not value:
        raise ValueError(message)


def verify():
    evidence = json.loads((ROOT/'validation/coinapi_ep000410_20261113.json').read_text('utf-8'))
    source = ROOT/evidence['csv2_file']
    require(hashlib.sha256(source.read_bytes()).hexdigest() == evidence['csv2_sha256'], 'CSV2 checksum')
    with source.open(encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        require(reader.fieldnames == layout.CSV2_FIELDS, 'CSV2 must retain the 47 delivery columns')
        rows = list(reader)
    require(len(rows) == 1, 'Pilot must contain exactly one episode')
    row = rows[0]
    require(row['episode_id'] == 'EP000410' and row['coin'] == 'TST', 'Pilot scope')
    require(row['book_state_point'] == 'tn_time_approximate_l4', 'Do not promote timestamp inference to block_end')
    require(row['status'] == 'book_ready' and row['book_side'] == 'bid', 'Candidate state')
    require(all(not row[k] for k in ('snapshot_block','last_applied_block','replay_from_block_exclusive','replay_to_block_inclusive')), 'Inferred block labels')
    require(row['episode_input_hash'] == evidence['episode']['episode_input_hash'], 'Input hash')

    csv1 = ROOT/'results/csv1_full_20250322_20261031/csv1_20250322_20261031_zh.csv'
    require(hashlib.sha256(csv1.read_bytes()).hexdigest() == evidence['episode']['source_csv1_sha256'], 'Authoritative CSV1 checksum')
    selected = []
    with csv1.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f)
        require(next(reader) == layout.CHINESE_CSV1, 'Authoritative CSV1 layout')
        for values in reader:
            fill = dict(zip(layout.CSV1_FIELDS, values))
            if fill['episode_id'] == 'EP000410':
                selected.append(fill)
    require(len(selected) == 1, 'Full first episode membership')
    for key in ('episode_input_hash','coin','direction','total_qty','entry_notional','entry_vwap'):
        require(row[key] == selected[0][key], 'Changed CSV1 field: ' + key)
    require(row['tn'] == selected[0]['episode_end'] and row['target_block'] == selected[0]['end_block'], 'Tn/target association')

    orders = json.loads(row['orders_json'])
    require(all(set(o) == {'oid','price','qty'} and all(isinstance(v,str) for v in o.values()) for o in orders), 'Order representation')
    require(len({o['oid'] for o in orders}) == len(orders), 'Duplicate active OID')
    require(all(Decimal(o['price']) > 0 and Decimal(o['qty']) > 0 for o in orders), 'Non-executable order')
    expected_order = sorted(sorted(orders,key=lambda o:o['oid']), key=lambda o:Decimal(o['price']), reverse=True)
    require(orders == expected_order, 'Bid price/OID ordering')
    with localcontext() as context:
        context.prec = 60
        depth = sum((Decimal(o['qty']) for o in orders), Decimal(0))
        amount = sum((Decimal(o['price'])*Decimal(o['qty']) for o in orders), Decimal(0))
    require(depth == Decimal(row['total_depth']) and amount == Decimal(row['side_notional']), 'Independent Decimal totals')
    require(len(orders) == int(row['order_count']), 'Order count')
    require(len({Decimal(o['price']) for o in orders}) == int(row['price_level_count']), 'Price levels')
    require(orders[0]['price'] == row['best_price'] and orders[-1]['price'] == row['worst_price'], 'Price bounds')
    require(layout.digest(orders) == row['book_hash'] == evidence['native_reference']['book_hash'], 'Canonical book hash')

    with (ROOT/evidence['native_reference']['path']).open(encoding='utf-8-sig', newline='') as f:
        reference = next(r for r in csv.DictReader(f) if r['episode_id'] == 'EP000410')
    require(row['orders_json'] == reference['orders_json'], 'Independent native order-by-order reference')
    for key in ('coin','direction','tn','target_block','total_qty','entry_notional','entry_vwap', 'order_count','price_level_count','total_depth','side_notional','best_price','worst_price'):
        require(row[key] == reference[key], 'Native reference mismatch: ' + key)
    require(Decimal(row['snapshot_lag_ms']) == Decimal(layout.parse_time_ns(row['snapshot_time'])-layout.parse_time_ns(row['tn']))/1_000_000, 'Signed state time difference')
    require(layout.parse_time_ns(row['snapshot_time']) <= layout.parse_time_ns(row['tn']), 'Post-Tn data in candidate')
    sources = json.loads(row['diff_sources_json'])
    require(layout.parse_time_ns(sources[0]['seed_time']) < layout.parse_time_ns(row['tn']), 'Future seed')
    require(sources[0]['cutoff'] == row['tn'], 'Replay cutoff')
    require(evidence['scope']['remaining_episodes_processed'] == 0, 'User review boundary')
    return {'status':'PASS','episodes':1,'columns':47,'native_order_differences':0,
            'book_state_point':row['book_state_point'],'snapshot_lag_ms':row['snapshot_lag_ms']}


if __name__ == '__main__':
    print(json.dumps(verify(), ensure_ascii=False, indent=2))
