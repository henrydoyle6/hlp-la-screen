import json,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from decimal import Decimal
from prepare_local_diffs import prepare_diff_inputs,loads_exact_selected
from hlp_la_screen import SnapshotRef
from test_ondemand import Log

class ProjectionTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
 def run_rows(self,rows):
  source=self.root/'0.zip'
  with zipfile.ZipFile(source,'w') as z:z.writestr('hourly/0',''.join(r if isinstance(r,str) else json.dumps(r)+'\n' for r in rows))
  config={'diffs':{'coverage_mode':'dense','chunks':[{'path':str(source),'start_block':101,'end_block':103}]},'local_diff_preparation':{'cache_dir':str(self.root/'cache')}}
  manager=SimpleNamespace(planning_errors={},extraction_errors={})
  ep=SimpleNamespace(snapshot=SnapshotRef('before.rmp',100,0),episode_id='e',coin='BTC',last=SimpleNamespace(block_number=103))
  return prepare_diff_inputs(config,[ep],manager,Log()),config,ep,manager
 def test_empty_rows_order_and_decimal_tails_preserved(self):
  raw='{"block_number":102,"events":[{"coin":"ETH","oid":2},{"coin":"BTC","oid":1,"raw_book_diff":{"new":{"sz":0.123456789012345678901}}}]}\n'
  sections,_,_,_=self.run_rows([{'block_number':101,'events':[]},raw,{'block_number':103,'events':[]}])
  p=Path(sections['BTC']['chunks'][0]['path']);rows=[json.loads(s) for s in p.read_text().splitlines()]
  self.assertEqual([r['block_number'] for r in rows],[101,102,103])
  self.assertEqual(rows[1]['events'][0]['raw_book_diff']['new']['sz'],'0.123456789012345678901')
  self.assertEqual(rows[1]['source_event_indices'],[1])
 def test_missing_block_is_not_synthesized(self):
  sections,_,_,_=self.run_rows([{'block_number':101,'events':[]},{'block_number':103,'events':[]}])
  rows=[json.loads(s) for s in Path(sections['BTC']['chunks'][0]['path']).read_text().splitlines()]
  self.assertEqual([r['block_number'] for r in rows],[101,103])
 def test_global_invalid_event_is_retained(self):
  sections,_,_,_=self.run_rows([{'block_number':101,'events':[{'coin':'ETH'},17,{'oid':1}]},{'block_number':103,'events':[]}])
  row=json.loads(Path(sections['BTC']['chunks'][0]['path']).read_text().splitlines()[0])
  self.assertEqual(row['events'],[17,{'oid':1}]);self.assertEqual(row['source_event_indices'],[1,2])
 def test_conflicting_duplicate_falls_back_to_original_file(self):
  sections,config,_,_=self.run_rows([{'block_number':101,'events':[]},{'block_number':101,'events':[{'coin':'ETH'}]}])
  self.assertEqual(sections['BTC']['chunks'][0]['path'],config['diffs']['chunks'][0]['path'])
  self.assertFalse(list((self.root/'cache').rglob('success.json')))
 def test_warm_projection_cache_reuses_complete_inputs(self):
  sections,config,ep,manager=self.run_rows([{'block_number':101,'events':[]},{'block_number':103,'events':[]}])
  second=prepare_diff_inputs(config,[ep],manager,Log());self.assertEqual(sections,second)
 def test_large_integer_is_not_float_rounded(self):
  data=loads_exact_selected(b'{"events":[{"coin":"BTC","oid":123456789012345678901234567890}]}',{'BTC'})
  self.assertEqual(data['events'][0]['oid'],123456789012345678901234567890)

if __name__=='__main__':unittest.main(verbosity=2)
