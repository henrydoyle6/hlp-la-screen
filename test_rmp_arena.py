"""Arena storage compatibility through the original shared extraction/comparison paths."""
import copy
import json
from rmp_book_layout import normalize_book
import rmp_l4
import state_store as store
import hlp_la_screen as app
from test_support import Fixture,Log
from test_rmp_layout import locus_fixture


def arena_book(book):
    entries=[];mapping=[];levels={'B':{},'A':{}}
    for order in book['book_orders'].values():
        slot=len(entries);order=copy.deepcopy(order)
        entries.extend([{'o':order},{'v':None}]);mapping.append([order['o'],slot])
        levels[order['c']['s']].setdefault(order['c']['l'],[]).append(slot)
    halfs=[]
    for side in ('B','A'):
        pairs=[]
        for price,slots in levels[side].items():
            for previous,current,nxt in zip([None]+slots[:-1],slots,slots[1:]+[None]):
                if previous is not None:entries[current]['o']['p']=previous
                if nxt is not None:entries[current]['o']['n']=nxt
            pairs.append([-price if side=='B' else price,dict(f=slots[0],l=slots[-1])])
        halfs.append(dict(levels=sorted(pairs)))
    return dict(asset=book['asset'],halfs=halfs,oid_to_key=mapping,bod=dict(e=entries,l=len(mapping),n=None))


def arena_fixture():
    value=locus_fixture(nameless=True)
    for dex in value['exchange']['perp_dexs']:
        dex['books']=[arena_book(b) for b in dex['books']]
    value['exchange']['spot_books']=[arena_book(b) for b in value['exchange']['spot_books']]
    return value


class ArenaTests(Fixture):
    def extract(self,value,coins=('BTC','EMPTY','xyz:ALPHA')):
        path=self.snapshot(value=value)
        cache=store.Cache(self.root/'arena.sqlite',Log());self.addCleanup(cache.close)
        ref=app.SnapshotRef(str(path),100,app.parse_time('2026-01-01T00:00:00Z'))
        result=rmp_l4.ExtractionCache(cache,Log()).ensure(ref,set(coins))
        return result,cache

    def test_original_orders_and_free_slots_and_empty_market(self):
        result,cache=self.extract(arena_fixture())
        self.assertTrue(all(isinstance(v,str) for v in result.values()))
        btc=cache.read('extract',result['BTC'])['orders']
        self.assertEqual([(r['oid'],r['side'],r['limitPx'],r['sz']) for r in btc],
                         [('111','B','12.345','2.5'),('112','B','12.345','2.5'),('113','A','12.4','0.5')])
        self.assertEqual(cache.read('extract',result['EMPTY'])['orders'],[])

    def test_large_real_oid_is_never_replaced_by_arena_slot(self):
        value=arena_fixture();book=value['exchange']['perp_dexs'][0]['books'][0]
        book['bod']['e'][0]['o']['o']=9007199254740993;book['oid_to_key'][0][0]=9007199254740993
        result,cache=self.extract(value)
        self.assertEqual(cache.read('extract',result['BTC'])['orders'][0]['oid'],'9007199254740993')

    def test_zero_remaining_is_kept_for_later_remove(self):
        value=arena_fixture();value['exchange']['perp_dexs'][0]['books'][0]['bod']['e'][0]['o']['r']=0
        result,cache=self.extract(value)
        self.assertEqual(cache.read('extract',result['BTC'])['orders'][0]['sz'],'0')

    def test_mixed_legacy_and_arena_markets_supported(self):
        value=locus_fixture(nameless=True)
        value['exchange']['perp_dexs'][0]['books'][0]=arena_book(value['exchange']['perp_dexs'][0]['books'][0])
        result,_=self.extract(value)
        self.assertTrue(all(isinstance(v,str) for v in result.values()))

    def test_malformed_selected_market_local_and_not_success_cached(self):
        value=arena_fixture();value['exchange']['perp_dexs'][0]['books'][0]['oid_to_key'][0][0]=999
        result,cache=self.extract(value)
        self.assertEqual(result['BTC'].code,'RMP_COIN_CONVERSION_FAILED')
        self.assertIsInstance(result['xyz:ALPHA'],str)
        self.assertEqual(cache.db.execute("SELECT count(*) FROM entries WHERE kind='rmp_staging'").fetchone()[0],0)

    def test_cache_reuses_a_successful_arena_extraction(self):
        from unittest.mock import patch
        value=arena_fixture();result,cache=self.extract(value)
        ref=app.SnapshotRef(str(self.root/'snapshots'/'100.rmp'),100,app.parse_time('2026-01-01T00:00:00Z'))
        with patch.object(rmp_l4,'scan_rmp',side_effect=AssertionError('must reuse')):
            again=rmp_l4.ExtractionCache(cache,Log()).ensure(ref,{'BTC'})
        self.assertEqual(again['BTC'],result['BTC'])

    def test_reject_bad_index_count_and_slot_variants(self):
        book=arena_fixture()['exchange']['perp_dexs'][0]['books'][0]
        for kind in ('duplicate_oid','duplicate_slot','vacant_slot','orphan','count','slot_variant','ambiguous'):
            bad=copy.deepcopy(book)
            if kind=='duplicate_oid':bad['oid_to_key'][1][0]=111
            elif kind=='duplicate_slot':bad['oid_to_key'][1][1]=0
            elif kind=='vacant_slot':bad['oid_to_key'][0][1]=1
            elif kind=='orphan':bad['oid_to_key'].pop()
            elif kind=='count':bad['bod']['l']=99
            elif kind=='slot_variant':bad['bod']['e'][1]={'unknown':None}
            else:bad['book_orders']={}
            with self.subTest(kind=kind),self.assertRaises(ValueError):normalize_book(bad)

    def test_reject_bad_chain_side_price_and_links(self):
        book=arena_fixture()['exchange']['perp_dexs'][0]['books'][0]
        for kind in ('cycle','previous','side','price','head','tail','unlinked'):
            bad=copy.deepcopy(book)
            if kind=='cycle':bad['bod']['e'][0]['o']['n']=0
            elif kind=='previous':bad['bod']['e'][2]['o']['p']=4
            elif kind=='side':bad['bod']['e'][0]['o']['c']['s']='A'
            elif kind=='price':bad['bod']['e'][0]['o']['c']['l']=123451
            elif kind=='head':bad['halfs'][0]['levels'][0][1]['f']=1
            elif kind=='tail':bad['bod']['e'][2]['o']['n']=4
            else:bad['halfs'][0]['levels']=[]
            with self.subTest(kind=kind),self.assertRaises(ValueError):normalize_book(bad)

    def test_no_same_price_merge_and_arena_conversion_is_pure(self):
        raw=arena_fixture()['exchange']['perp_dexs'][0]['books'][0];before=copy.deepcopy(raw)
        normal,kind=normalize_book(raw)
        self.assertEqual(kind,'arena');self.assertEqual(raw,before)
        self.assertEqual(len(normal['book_orders']),3)
        self.assertEqual([o['o'] for o in normal['book_orders'].values()],[111,112,113])

    def test_original_compare_entry_accepts_arena_and_rejects_wrong_remaining(self):
        from test_order_book_replay import compare_multiple_snapshots
        before=self.snapshot(value=arena_fixture())
        answer=arena_fixture();answer['exchange']['locus']['ctx'].update(height=102,time='2026-01-01T01:00:00Z')
        after=self.snapshot('102.rmp',value=answer)
        diff=self.hour('diffs',1,[self.block(101,'2026-01-01T00:30:00Z'),self.block(102,'2026-01-01T01:00:00Z')])
        args=dict(before_state=before,after_state=after,before_block=100,before_time='2026-01-01T00:00:00Z',
                  target_block=102,coins=['BTC','xyz:ALPHA'],diffs=[diff])
        self.assertEqual(compare_multiple_snapshots(**args)['status'],'PASS')
        answer['exchange']['perp_dexs'][0]['books'][0]['bod']['e'][0]['o']['r']=251
        self.snapshot('102.rmp',value=answer)
        self.assertEqual(compare_multiple_snapshots(**args)['status'],'FAIL')
