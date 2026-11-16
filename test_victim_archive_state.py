import copy,unittest
from victim_archive_state import verify_pre_fill_state
from victim_quantity_precision import QuantityTailPolicy

T=1760130000123
def fixture():
    state={'http_status':200,'requested_asof_ms':T,'sha256':'fixture','positions':[{'coin':'BTC','size':'1','entry_price':'10','quality':'complete'}],
        'meta':{'as_of':T,'source':'reconstructed','quality':'partial','built_through':T+10000,'finalized_through':T+10000}}
    native={'coin':'BTC','time':T,'tid':30,'oid':40,'side':'A','px':'8','sz':'1','startPosition':'1','fee':'0','closedPnl':'-2'}
    row={'coin':'BTC','timestamp':T,'trade_id':30,'order_id':40,'seq':0,'side':'A','price':'8','size':'1','start_position':'1','end_position':'0','entry_price_after':None,'continuity':'ok','finalized':True}
    return state,{'complete':True,'rows':[row]},native
class ArchiveStateTests(unittest.TestCase):
    def test_valid_before_fill_state_and_native_close(self):
        s,c,n=fixture();self.assertEqual(verify_pre_fill_state(s,c,n,QuantityTailPolicy(5))['entry'],'10')
    def test_future_state_is_rejected(self):
        s,c,n=fixture();s['meta']['as_of']=T+1
        self.assertEqual(verify_pre_fill_state(s,c,n,QuantityTailPolicy(5))['reason'],'archive_state_time_or_source_mismatch')
    def test_partial_unknown_entry_is_not_zero(self):
        s,c,n=fixture();s['positions'][0].update(quality='partial',entry_price=None)
        self.assertIsNone(verify_pre_fill_state(s,c,n,QuantityTailPolicy(5))['entry'])
    def test_small_quantity_tail_is_accepted(self):
        s,c,n=fixture();s['positions'][0]['size']='1.0000000001'
        r=verify_pre_fill_state(s,c,n,QuantityTailPolicy(5));self.assertEqual(r['entry'],'10');self.assertTrue(r['proof']['quantity_tail_adjustments'])
    def test_real_quantity_mismatch_is_rejected(self):
        s,c,n=fixture();s['positions'][0]['size']='1.001'
        self.assertEqual(verify_pre_fill_state(s,c,n,QuantityTailPolicy(5))['reason'],'archive_pre_target_quantity_mismatch')
    def test_pnl_disagreement_is_not_hidden_by_price_rounding(self):
        s,c,n=fixture();n['closedPnl']='-2.01'
        self.assertEqual(verify_pre_fill_state(s,c,n,QuantityTailPolicy(5))['reason'],'archive_entry_disagrees_with_native_close_pnl')
    def test_native_pnl_serialization_tail_is_recorded(self):
        s,c,n=fixture();n['closedPnl']='-2.0000000000000004'
        self.assertEqual(verify_pre_fill_state(s,c,n,QuantityTailPolicy(5))['entry'],'10')
    def test_same_millisecond_net_zero_changes_entry_before_close(self):
        s,c,n=fixture();target=c['rows'][0];target['seq']=2;n['closedPnl']='-3'
        before={'coin':'BTC','timestamp':T,'trade_id':1,'order_id':2,'seq':0,'side':'A','price':'9','size':'0.5','start_position':'1','end_position':'0.5','entry_price_after':'10','continuity':'ok','finalized':True}
        after={**before,'trade_id':3,'order_id':4,'seq':1,'side':'B','price':'12','start_position':'0.5','end_position':'1','entry_price_after':'11'}
        c['rows']=[target,after,before]
        r=verify_pre_fill_state(s,c,n,QuantityTailPolicy(5));self.assertEqual(r['entry'],'11');self.assertEqual(r['proof']['prior_same_millisecond_legs'],2)
    def test_incomplete_page_is_rejected(self):
        s,c,n=fixture();c['complete']=False
        self.assertEqual(verify_pre_fill_state(s,c,n,QuantityTailPolicy(5))['reason'],'archive_target_millisecond_not_complete')
if __name__=='__main__':unittest.main()
