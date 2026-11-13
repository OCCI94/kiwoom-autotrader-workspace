import time, unittest
from unittest.mock import Mock
from kiwoom_autotrader.research.shared_server import PaperResearchServer, ResearchBridgeError, _validate

B={'stk_cd':'005930','tic_scope':'1','upd_stkpc_tp':'0'}
def req(**extra):return {'api_id':'ka10080','body':B,'cont_yn':'N','next_key':'','session_id':None,'deadline_ns':time.monotonic_ns()+8_000_000_000,**extra}
def page(rows, yn='N', key=''):
 return {'ok':True,'status':200,'headers':{'api-id':'ka10080','cont-yn':yn,'next-key':key},'body':{'return_code':0,'stk_cd':'005930','stk_min_pole_chart_qry':rows},'session_id':'a'*32,'wire_sha256':'b'*64,'wire_bytes':100}
def row(tm, price='1'):return {'cntr_tm':tm,'cur_prc':price}
class PaginationTest(unittest.TestCase):
 def test_invalid_options(self):
  for opts in ({'max_pages':True},{'max_pages':0},{'max_pages':6},{'stop_before_dt':'20260230000000'},{'stop_before_dt':'x'}):
   with self.subTest(opts=opts),self.assertRaises(ResearchBridgeError):_validate(req(**opts))
 def test_chain_dedup_order_and_evidence(self):
  srv=object.__new__(PaperResearchServer);srv._exchange_single=Mock(side_effect=[page([row('20261028100000'),row('20261028095900')],'Y','k'),page([row('20261028095900'),row('20261028095800')])])
  out=srv._exchange(req(max_pages=2));self.assertEqual(out['pages_fetched'],2);self.assertEqual([r['cntr_tm'] for r in out['body']['stk_min_pole_chart_qry']],['20261028100000','20261028095900','20261028095800']);self.assertEqual(srv._exchange_single.call_args_list[1].args[0]['next_key'],'k');self.assertEqual(srv._exchange_single.call_args_list[1].args[0]['session_id'],'a'*32)
 def test_cutoff_prevents_further_queries(self):
  srv=object.__new__(PaperResearchServer);srv._exchange_single=Mock(return_value=page([row('20261028090000')],'Y','k'))
  out=srv._exchange(req(max_pages=5,stop_before_dt='20261028090000'));self.assertEqual(out['pages_fetched'],1);self.assertTrue(out['cutoff_reached']);srv._exchange_single.assert_called_once()
 def test_conflicting_overlap_fails_closed(self):
  srv=object.__new__(PaperResearchServer);srv._exchange_single=Mock(side_effect=[page([row('20261028090000')],'Y','k'),page([row('20261028090000','2')])])
  with self.assertRaises(ResearchBridgeError):srv._exchange(req(max_pages=2))
 def test_repeated_cursor_fails_closed(self):
  srv=object.__new__(PaperResearchServer);srv._exchange_single=Mock(side_effect=[page([],'Y','k'),page([],'Y','k')])
  with self.assertRaises(ResearchBridgeError):srv._exchange(req(max_pages=3))
 def test_one_page_is_compatible(self):
  srv=object.__new__(PaperResearchServer);srv._exchange_single=Mock(return_value=page([]));q=req();self.assertIs(srv._exchange(q),srv._exchange_single.return_value);srv._exchange_single.assert_called_once_with(q)
if __name__=='__main__':unittest.main()
