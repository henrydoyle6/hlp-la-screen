import json,unittest
from victim_fill_merge import merge_overlay,native_identity
from victim_quantity_precision import QuantityTailPolicy

def fill(tid=1,sp='0',size='1',legacy=False):
    f={'coin':'BTC','time':100,'tid':tid,'oid':7,'side':'B','hash':'0xabc','px':'10','sz':size,'startPosition':sp,'native_order':['raw',2,0]}
    if legacy:f['legacy_source_identity']=True
    return f
def record(f):return {'id':native_identity('u','BTC',100,f),'f':f,'source':'base','source_line':0,'conflict':0}
def added(f,i=1):return (str(i),'u','BTC',100,json.dumps(f),'raw',i,'sha')
class MergeTests(unittest.TestCase):
    def test_same_native_identity_deduplicates(self):
        result,stats=merge_overlay([record(fill())],[added(fill())],QuantityTailPolicy(4))
        self.assertEqual(len(result),1);self.assertEqual(stats['matched_rows_with_native_order_verified'],1)
    def test_distinct_native_id_is_not_fingerprint_deduplicated(self):
        result,_=merge_overlay([record(fill())],[added(fill(2))],QuantityTailPolicy(4))
        self.assertEqual(len(result),2)
    def test_legacy_alias_deduplicates_with_dust_without_overwriting(self):
        old=record(fill(sp='1'));result,stats=merge_overlay([old],[added(fill('legacy_source_row:x',sp='1.0000000001',legacy=True))],QuantityTailPolicy(4))
        self.assertEqual(len(result),1);self.assertEqual(result[0]['f']['startPosition'],'1');self.assertEqual(old['source'],'base')
        self.assertEqual(stats['quantity_alias_tail_comparisons'],1)
    def test_native_conflicting_quantity_is_flagged(self):
        result,_=merge_overlay([record(fill())],[added(fill(size='2'))],QuantityTailPolicy(4))
        self.assertTrue(result[0]['conflict'])
    def test_ambiguous_alias_is_flagged(self):
        result,stats=merge_overlay([record(fill()),record(fill(2))],[added(fill('legacy_source_row:x',legacy=True))],QuantityTailPolicy(4))
        self.assertTrue(all(r['conflict'] for r in result));self.assertEqual(stats['ambiguous_legacy_alias_rows'],1)
    def test_two_legacy_rows_do_not_reuse_one_native_fill(self):
        f=fill('legacy_source_row:x',legacy=True)
        result,_=merge_overlay([record(fill())],[added(f,1),added(f,2)],QuantityTailPolicy(4))
        self.assertEqual(len(result),2)
    def test_repeated_native_alias_does_not_duplicate_legacy_fill(self):
        old=record(fill('legacy_source_row:x',legacy=True))
        result,_=merge_overlay([old],[added(fill(),1),added(fill(),2)],QuantityTailPolicy(4))
        self.assertEqual(len(result),1)
    def test_legacy_alias_cannot_collapse_distinct_native_ids_across_sources(self):
        old=record(fill('legacy_source_row:x',legacy=True))
        first=added(fill(1),1)
        second=list(added(fill(2),2));second[5]='another_source'
        result,_=merge_overlay([old],[first,tuple(second)],QuantityTailPolicy(4))
        self.assertEqual(len(result),2)
    def test_native_binding_survives_a_second_merge_call(self):
        old=record(fill('legacy_source_row:x',legacy=True))
        first,_=merge_overlay([old],[added(fill(1),1)],QuantityTailPolicy(4))
        another=list(added(fill(2),2));another[5]='another_source'
        second,_=merge_overlay(first,[tuple(another)],QuantityTailPolicy(4))
        self.assertEqual(len(second),2)
if __name__=='__main__':unittest.main()
