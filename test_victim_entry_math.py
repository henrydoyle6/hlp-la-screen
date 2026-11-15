import unittest
from decimal import Decimal as D
from victim_entry_math import replay,implied,order_same_time,covers_interval,recent_history_suffix

def record(ident,t,sp,size,px,side,**kwargs):
    f={'time':t,'startPosition':str(sp),'sz':str(size),'px':str(px),'side':side,**kwargs}
    return {'id':ident,'f':f,'source':'synthetic_only','source_line':t,'conflict':0}

class CostReconstructionTests(unittest.TestCase):
    def test_position_continuity_does_not_prove_source_time_coverage(self):
        rs=[record('open',1,0,2,100,'B'),record('target',10,2,2,90,'A')]
        self.assertEqual(replay(rs)['target']['entry'],'100')
        self.assertFalse(covers_interval([(1,2),(9,10)],1,10))
        self.assertTrue(covers_interval([(1,5),(6,10)],1,10))
    def test_history_suffix_preserves_complete_anchor_timestamp(self):
        rs=[record('old',0,0,1,70,'B'),record('close',1,1,1,80,'A'),
            record('open',2,0,2,100,'B'),record('target',3,2,2,90,'A')]
        selected=recent_history_suffix(iter(reversed(rs)),3)
        self.assertEqual(replay(selected)['target']['entry'],replay(rs)['target']['entry'])
        self.assertEqual([r['id'] for r in selected],['open','target'])
    def test_same_millisecond_post_target_anchor_cannot_truncate_cost_history(self):
        rs=[record('open',1,0,2,100,'B'),record('target',2,2,2,90,'A'),record('reopen',2,0,1,110,'B')]
        for i,r in enumerate(rs):r['f']['native_order']=['native',1,i]
        selected=recent_history_suffix(iter(reversed(rs)),2)
        self.assertEqual(replay(selected)['target']['entry'],'100')
    def test_research_precision_does_not_change_callers_context(self):
        from decimal import getcontext,localcontext,ROUND_FLOOR,ROUND_HALF_EVEN
        original=getcontext().prec
        original_rounding=getcontext().rounding
        getcontext().prec=37
        getcontext().rounding=ROUND_FLOOR
        try:
            value=implied({'startPosition':'3','sz':'3','px':'90','side':'A','closedPnl':'-20','fee':'0'})
            self.assertEqual(getcontext().prec,37)
            self.assertEqual(getcontext().rounding,ROUND_FLOOR)
            with localcontext() as context:
                context.prec=60;context.rounding=ROUND_HALF_EVEN
                self.assertEqual(value,D(90)+D(20)/D(3))
        finally:
            getcontext().prec=original;getcontext().rounding=original_rounding
    def test_long_weighted_add_and_partial_close(self):
        rs=[record('open',1,0,2,100,'B'),record('add',2,2,1,130,'B'),record('reduce',3,3,1,120,'A'),record('target',4,2,2,90,'A')]
        self.assertEqual(replay(rs)['target']['entry'],'110')
    def test_short_weighted_add_and_partial_close(self):
        rs=[record('open',1,0,2,100,'A'),record('add',2,-2,1,130,'A'),record('reduce',3,-3,1,120,'B'),record('target',4,-2,2,140,'B')]
        self.assertEqual(replay(rs)['target']['entry'],'110')
    def test_reversal_remainder_is_new_anchor(self):
        rs=[record('flip',1,3,5,120,'A'),record('target',2,-2,2,140,'B')]
        self.assertEqual(replay(rs)['target']['entry'],'120')
    def test_missing_position_leg_invalidates_until_new_anchor(self):
        rs=[record('open',1,0,3,100,'B'),record('missing',2,2,1,90,'A'),record('target',3,1,1,80,'A')]
        self.assertIsNone(replay(rs)['target']['entry'])
    def test_same_millisecond_unique_chain(self):
        rs=[record('target',2,2,2,90,'A'),record('open',1,0,1,100,'B'),record('add',2,1,1,120,'B')]
        self.assertEqual(replay(rs)['target']['entry'],'110')
    def test_ambiguous_same_millisecond_never_guess(self):
        rs=[record('a',1,0,1,100,'B'),record('b',1,0,2,120,'B')]
        self.assertEqual(replay(rs)['a']['reason'],'ambiguous_same_millisecond_order')
    def test_conflicting_anchor_never_used(self):
        a=record('open',1,0,1,100,'B');a['conflict']=1
        self.assertIsNone(replay([a,record('target',2,1,1,90,'A')])['target']['entry'])
    def test_native_stream_order_resolves_multiple_flat_anchors(self):
        rs=[record('open',1,0,1,100,'B'),record('close',1,1,1,90,'A'),
            record('reopen',1,0,1,120,'B'),record('target',2,1,1,110,'A')]
        for index,r in enumerate(rs):r['f']['native_order']=['native_hour',1,index]
        self.assertEqual(replay(list(reversed(rs)))['target']['entry'],'120')
    def test_native_blocks_resolve_same_millisecond_across_files(self):
        rs=[record('open',1,0,1,100,'B'),record('close',1,1,1,90,'A'),
            record('reopen',1,0,1,120,'B'),record('target',2,1,1,110,'A')]
        for i,r in enumerate(rs):r['f'].update(block_number=10+i//2,event_index=i%2,native_order=['file'+str(i),1,0])
        self.assertEqual(replay(list(reversed(rs)))['target']['entry'],'120')
    def test_windows_native_source_path_alias_does_not_make_order_ambiguous(self):
        rs=[record('open',1,0,1,100,'B'),record('close',1,1,1,90,'A'),
            record('reopen',1,0,1,120,'B'),record('target',2,1,1,110,'A')]
        for index,r in enumerate(rs):
            r['f']['native_order']=[r'E:\native\1.zip' if index%2 else r'E:\\native\\1.zip',1,index]
        self.assertEqual(replay(list(reversed(rs)))['target']['entry'],'120')
    def test_victim_pnl_long_and_short(self):
        self.assertEqual(implied({'startPosition':'2','sz':'2','px':'90','side':'A','closedPnl':'-20','fee':'0'}),D(100))
        self.assertEqual(implied({'startPosition':'-2','sz':'2','px':'110','side':'B','closedPnl':'-20','fee':'0'}),D(100))
    def test_nonzero_fee_not_silently_inverted(self):
        with self.assertRaisesRegex(ValueError,'nonzero_fee'):
            implied({'startPosition':'2','sz':'2','px':'90','side':'A','closedPnl':'-20','fee':'0.1'})
    def test_legacy_trade_missing_pnl_never_filled_with_zero(self):
        with self.assertRaisesRegex(ValueError,'missing_victim_closedPnl'):
            implied({'startPosition':'2','sz':'2','px':'90','side':'A','fee':None,'closedPnl':None})
    def test_reject_wrong_side_and_partial_close(self):
        for f in [{'startPosition':'2','sz':'2','px':'90','side':'B','closedPnl':'-20','fee':'0'},
                  {'startPosition':'3','sz':'2','px':'90','side':'A','closedPnl':'-20','fee':'0'}]:
            with self.assertRaises(ValueError):implied(f)

if __name__=='__main__':unittest.main(verbosity=2)
