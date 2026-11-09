import sys,json,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent/'离线原型'))
from service import read_input,run_data,export
class EvidenceExportTest(unittest.TestCase):
 def test_each_result_links_both_inputs_and_sources(self):
  data=read_input(Path(__file__).parent/'离线原型/样例/综合模拟样例.json'); result=run_data(data)
  with tempfile.TemporaryDirectory() as tmp:
   log=export(result,tmp,data); bundle=json.loads((Path(tmp)/'核验来源与输入.json').read_text(encoding='utf8'))
   self.assertEqual(len(bundle['event_claim_evidence']),len(result['event_claim_results']))
   row=next(x for x in bundle['event_claim_evidence'] if x['claim']['id']=='claim-A05' and x['event']['id']=='evt-A05-deposit')
   self.assertTrue(row['claim_sources']);self.assertTrue(row['event_sources']);self.assertIn('raw_sha256',row['claim_sources'][0]);self.assertIn('amount',row['event']);self.assertIn('核验来源与输入.json',log['output_sha256'])
 def test_mismatched_input_is_rejected_before_writing(self):
  data=read_input(Path(__file__).parent/'离线原型/样例/综合模拟样例.json');result=run_data(data);data['claims'][0]['amount']='999'
  with tempfile.TemporaryDirectory() as tmp:
   with self.assertRaises(ValueError):export(result,tmp,data)
   self.assertFalse(list(Path(tmp).iterdir()))
 def test_sources_are_exact_input_records_and_same_case(self):
  data=read_input(Path(__file__).parent/'离线原型/样例/综合模拟样例.json');result=run_data(data)
  with tempfile.TemporaryDirectory() as tmp:
   export(result,tmp,data);bundle=json.loads((Path(tmp)/'核验来源与输入.json').read_text(encoding='utf8'))
   for row in bundle['event_claim_evidence']:
    for key in ('claim','event'):
     obj=row[key];sources=row[key+'_sources']
     self.assertEqual([s['id'] for s in sources],obj['source_ids'])
     self.assertTrue(all(s['case_id']==obj['case_id'] and s in data['records'] for s in sources))
   self.assertEqual(len(bundle['event_reconciliation_evidence']),len(result['event_reconciliation']))
   self.assertEqual(len(bundle['claims_without_output_evidence']),len(result['claims_without_output']))
 def test_tampered_source_hash_is_rejected_before_writing(self):
  data=read_input(Path(__file__).parent/'离线原型/样例/综合模拟样例.json');result=run_data(data);data['records'][0]['payload']['tag']='altered'
  with tempfile.TemporaryDirectory() as tmp:
   with self.assertRaises(ValueError):export(result,tmp,data)
   self.assertFalse(list(Path(tmp).iterdir()))
if __name__=='__main__':unittest.main()
