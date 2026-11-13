"""Offline boundary, IPC peer and lifecycle tests; no real broker access."""
from datetime import datetime
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.research.shared_server import PaperResearchServer, ResearchBridgeError, _peer_uid, UPPER, MAX_REQUEST
from kiwoom_autotrader.utils.keychain import Credentials

TOKEN='offline-broker-secret-token-123456'
class Response(io.BytesIO):
 def __init__(self,payload):
  super().__init__(json.dumps(payload).encode()); self.headers={'api-id':'ka10017','cont-yn':'N','next-key':''}; self.status=200
 def __enter__(self):return self
 def __exit__(self,*args):self.close()

class SharedResearchServerTests(unittest.TestCase):
 def setUp(self):
  self.dir=tempfile.TemporaryDirectory(prefix='ipc-',dir='/tmp');self.addCleanup(self.dir.cleanup)
  self.session=BrokerSession(now=lambda:datetime(2026,10,27,10,tzinfo=KST));self.session.mode='paper';self.session._issued=IssuedToken(TOKEN,'20300101000000')
  self.calls=[]; self.body={'return_code':0,'updown_pric':[{'stk_cd':'005930','stk_nm':'안전','cur_prc':'70000','irrelevant_secret':'TOP_SECRET'}]}
  def transport(opener,request,**kw):
   self.calls.append(request);kw['before_send']();return Response(self.body)
  self.server=PaperResearchServer(self.session,Path(self.dir.name),open_http=transport);self.addCleanup(self.server.stop);self.server.start()
 def call(self,api='ka10017',body=None,session_id=None,cont='N',key='',extra=None):
  s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.addCleanup(s.close);s.settimeout(4);s.connect(str(self.server.path))
  request={'api_id':api,'body':UPPER if body is None else body,'cont_yn':cont,'next_key':key,'session_id':session_id,'deadline_ns':time.monotonic_ns()+8_000_000_000}; request.update(extra or {}); raw=json.dumps(request).encode();s.sendall(struct.pack('!I',len(raw))+raw)
  def exact(n):
   data=b''
   while len(data)<n:
    chunk=s.recv(n-len(data));self.assertTrue(chunk);data+=chunk
   return data
  response=json.loads(exact(struct.unpack('!I',exact(4))[0]));return response
 def test_real_socket_peer_uid(self):
  one,two=socket.socketpair()
  try:self.assertEqual(_peer_uid(one),os.getuid())
  finally:one.close();two.close()
 def test_paper_broker_proxy_no_secrets_and_bounded_fields(self):
  self.body['updown_pric'][0]['stk_nm']=TOKEN
  out=self.call();self.assertTrue(out['ok']);self.assertEqual(out['body']['updown_pric'][0]['stk_nm'],None)
  self.assertNotIn(TOKEN,json.dumps(out));self.assertNotIn('TOP_SECRET',json.dumps(out));self.assertEqual(out['body']['updown_pric'][0]['cur_prc'],'70000')
  self.assertRegex(out['session_id'],r'^[0-9a-f]{32}$');self.assertEqual(len(out['wire_sha256']),64);self.assertGreater(out['wire_bytes'],0)
  self.assertEqual(self.calls[0].full_url,'https://mockapi.kiwoom.com/api/dostk/stkinfo')
 def test_broker_error_is_terminal_and_sensitive_error_text_not_returned(self):
  self.body={'return_code':3,'return_msg':TOKEN+' TOP_SECRET'}
  out=self.call();self.assertEqual(out,{'ok':False,'error':'broker_rejected'})
  self.assertNotIn(TOKEN,json.dumps(out));self.assertEqual(len(self.calls),1)
 def test_live_existing_socket_is_not_unlinked(self):
  path=self.server.path; before=path.stat().st_ino
  second=PaperResearchServer(self.session,Path(self.dir.name),open_http=self.server._open)
  with self.assertRaises(ResearchBridgeError):second.start()
  self.assertEqual(path.stat().st_ino,before);self.assertTrue(self.call()['ok'])
 def test_liveness_probe_does_not_exhaust_queue_before_accept_worker_runs(self):
  # Hold accept deterministically: the failed second startup connects/closes
  # its liveness probe, which remains queued until the original worker runs.
  self.server.stop();entered,release=threading.Event(),threading.Event()
  self.server=PaperResearchServer(self.session,Path(self.dir.name),open_http=self.server._open)
  original_serve=self.server._serve
  def paused():
   entered.set()
   if release.wait(4):original_serve()
  self.server._serve=paused
  self.addCleanup(self.server.stop);self.addCleanup(release.set)
  self.server.start();self.assertTrue(entered.wait(2))
  path=self.server.path;before=path.stat().st_ino
  second=PaperResearchServer(self.session,Path(self.dir.name),open_http=self.server._open)
  with self.assertRaises(ResearchBridgeError):second.start()
  self.assertEqual(path.stat().st_ino,before)
  client=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.addCleanup(client.close);client.settimeout(4)
  try:
   client.connect(str(path))  # must queue before any accept is permitted
   request={'api_id':'ka10017','body':UPPER,'cont_yn':'N','next_key':'','session_id':None,
       'deadline_ns':time.monotonic_ns()+8_000_000_000}
   data=json.dumps(request).encode();client.sendall(struct.pack('!I',len(data))+data)
  finally:release.set()
  def exact(size):
   parts=b''
   while len(parts)<size:
    part=client.recv(size-len(parts));self.assertTrue(part);parts+=part
   return parts
  response=json.loads(exact(struct.unpack('!I',exact(4))[0]))
  self.assertTrue(response['ok']);self.assertEqual(len(self.calls),1)
  self.assertEqual(path.stat().st_ino,before)
 def test_pacing_occurs_before_queuing_another_same_tr(self):
  self.assertTrue(self.call()['ok']); before=time.monotonic();self.assertTrue(self.call()['ok']); self.assertGreaterEqual(time.monotonic()-before,1.05)
 def test_non_paper_and_order_requests_are_blocked(self):
  self.assertFalse(self.call('kt10000',{})['ok']);self.assertEqual(len(self.calls),0)
  self.session.mode='real';self.assertFalse(self.call()['ok']);self.assertEqual(len(self.calls),0)
 def test_session_change_invalidates_prior_epoch(self):
  first=self.call(); self.assertTrue(first['ok']);self.session._issued=IssuedToken('another-test-token','20300101000000')
  out=self.call(session_id=first['session_id']);self.assertEqual(out['error'],'session_changed');self.assertEqual(len(self.calls),1)
 def test_continuation_requires_original_session_id(self):
  out=self.call(cont='Y',key='cursor',session_id=None)
  self.assertEqual(out['error'],'invalid_request');self.assertEqual(self.calls,[])
 def test_real_to_paper_transition_refuses_during_token_issuance(self):
  entered,release=threading.Event(),threading.Event()
  self.session.mode='real';self.session._issued=IssuedToken('offline-real-token','20300101000000')
  self.session._credential_loader=lambda mode:Credentials('offline-app','offline-secret')
  def issue(mode,credentials):
   entered.set();release.wait(4);return IssuedToken('offline-new-paper-token','20300101000000')
  self.session._token_issuer=issue
  worker=threading.Thread(target=lambda:self.session.login('paper'));worker.start();self.assertTrue(entered.wait(2))
  before=time.monotonic();out=self.call();self.assertLess(time.monotonic()-before,1);self.assertEqual(out['error'],'paper_session_unavailable');self.assertEqual(self.calls,[])
  release.set();worker.join(3);self.assertFalse(worker.is_alive());self.assertTrue(self.call()['ok'])
  self.assertNotIn('offline-real-token',json.dumps(self.body));self.assertEqual(self.calls[-1].get_header('Authorization'),'Bearer offline-new-paper-token')
 def test_renewal_transition_refuses_ipc_until_new_token(self):
  first=self.call();entered,release=threading.Event(),threading.Event()
  self.session._credential_loader=lambda mode:Credentials('offline-app','offline-secret')
  def issue(mode,credentials):entered.set();release.wait(4);return IssuedToken('offline-renewed-paper','20300101000000')
  self.session._token_issuer=issue;worker=threading.Thread(target=lambda:self.session.login('paper'));worker.start();self.assertTrue(entered.wait(2))
  self.assertEqual(self.call()['error'],'paper_session_unavailable');release.set();worker.join(3)
  self.assertEqual(self.call(session_id=first['session_id'])['error'],'session_changed')
 def test_clear_between_calls_invalidates_session(self):
  first=self.call();self.assertTrue(first['ok']);self.session.clear()
  self.assertEqual(self.call(session_id=first['session_id'])['error'],'paper_session_unavailable');self.assertEqual(len(self.calls),1)
 def test_session_change_during_network_does_not_return_data(self):
  def changed(opener,request,**kw):
   kw['before_send']();self.session._issued=IssuedToken('another-test-token','20300101000000');return Response(self.body)
  self.server._open=changed;out=self.call();self.assertFalse(out['ok']);self.assertNotIn('updown_pric',out)
 def test_invalid_request_and_cursor_never_reach_broker(self):
  for api,body,cont,key in [('ka10017',{**UPPER,'trade':'yes'},'N',''),('ka10081',{'stk_cd':'070300','base_dt':'20261027','upd_stkpc_tp':'1'},'N',''),('ka10099',{'mrkt_tp':'999'},'N',''),('ka10017',UPPER,'Y','')]:
   with self.subTest(api=api,body=body):self.assertFalse(self.call(api,body,cont=cont,key=key)['ok'])
  self.assertEqual(self.calls,[])
 def test_book_and_minute_allow_only_fixed_readonly_fields(self):
  payloads={
   'ka10004':{'return_code':0,'bid_req_base_tm':'101500','sel_fpr_bid':'+9400','sel_fpr_req':'5','buy_fpr_bid':'-9390','buy_fpr_req':'8','acct_no':'private-account','token':TOKEN},
   'ka10080':{'return_code':0,'stk_cd':'005930','stk_min_pole_chart_qry':[{'cntr_tm':'20261028101400','open_pric':'-9400','high_pric':'-9410','low_pric':'-9390','cur_prc':'-9400','trde_qty':'20','acc_trde_qty':'100','acct_no':'private-account'}]}}
  calls=[]
  def request(opener,req,**kw):
   api=req.get_header('Api-id');self.assertEqual(kw['priority'],'research');kw['before_send']();calls.append(req)
   response=Response(payloads[api]);response.headers={'api-id':api,'cont-yn':'N','next-key':''};return response
  self.server._open=request
  book=self.call('ka10004',{'stk_cd':'005930'});self.assertTrue(book['ok']);self.assertEqual(book['body']['sel_fpr_req'],'5');self.assertEqual(book['body']['requested_stk_cd'],'005930')
  self.assertEqual(calls[0].full_url,'https://mockapi.kiwoom.com/api/dostk/mrkcond')
  chart=self.call('ka10080',{'stk_cd':'005930','tic_scope':'1','upd_stkpc_tp':'0'});self.assertTrue(chart['ok']);self.assertEqual(chart['body']['stk_min_pole_chart_qry'][0]['acc_trde_qty'],'100')
  self.assertNotIn('private-account',json.dumps([book,chart]));self.assertNotIn(TOKEN,json.dumps([book,chart]))
  self.assertEqual(calls[1].full_url,'https://mockapi.kiwoom.com/api/dostk/chart')
 def test_new_tr_validation_rejects_extra_options_and_wrong_symbol(self):
  for api,body,cont,key in [('ka10004',{'stk_cd':'005930_NX'},'N',''),('ka10004',{'stk_cd':'005930','account':'secret'},'N',''),('ka10004',{'stk_cd':'005930'},'Y','cursor'),('ka10080',{'stk_cd':'005930','tic_scope':'5','upd_stkpc_tp':'0'},'N',''),('ka10080',{'stk_cd':'005930','tic_scope':'1','upd_stkpc_tp':'1'},'N',''),('ka10080',{'stk_cd':'005930','tic_scope':'1','upd_stkpc_tp':'0','base_dt':'20261028'},'N','')]:
   with self.subTest(api=api,body=body):self.assertFalse(self.call(api,body,cont=cont,key=key,session_id='a'*32)['ok'])
  self.assertEqual(self.calls,[])
 def test_minute_pagination_uses_shared_queue_and_session(self):
  requests=[]
  def transport(opener,request,**kw):
   kw['before_send']();self.assertEqual(kw['priority'],'research');requests.append(request)
   i=len(requests); p={'return_code':0,'stk_cd':'005930','stk_min_pole_chart_qry':[{'cntr_tm':f'20261028090{2-i}00','cur_prc':'100'}]}
   out=Response(p);out.headers={'api-id':'ka10080','cont-yn':'Y' if i==1 else 'N','next-key':'cursor' if i==1 else ''};return out
  self.server._open=transport
  result=self.call('ka10080',{'stk_cd':'005930','tic_scope':'1','upd_stkpc_tp':'0'},extra={'max_pages':2})
  self.assertTrue(result['ok']);self.assertEqual(result['pages_fetched'],2);self.assertEqual(len(result['body']['stk_min_pole_chart_qry']),2)
  self.assertEqual(requests[1].get_header('Cont-yn'),'Y');self.assertEqual(requests[1].get_header('Next-key'),'cursor')
 def test_minute_response_symbol_mismatch_is_rejected(self):
  def request(opener,req,**kw):
   kw['before_send']();r=Response({'return_code':0,'stk_cd':'000660','stk_min_pole_chart_qry':[]});r.headers={'api-id':'ka10080','cont-yn':'N','next-key':''};return r
  self.server._open=request
  self.assertEqual(self.call('ka10080',{'stk_cd':'005930','tic_scope':'1','upd_stkpc_tp':'0'})['error'],'broker_response_invalid')
 def test_unsafe_existing_socket_path_is_never_deleted(self):
  other=Path(self.dir.name)/'unsafe';other.mkdir(mode=0o700);target=other/'research.sock';target.write_text('keep')
  obj=PaperResearchServer(self.session,other,open_http=self.server._open)
  with self.assertRaises(ResearchBridgeError):obj.start()
  self.assertEqual(target.read_text(),'keep')
 def test_wrong_peer_rejected_without_http(self):
  self.server._peer_uid=lambda _:os.getuid()+1
  self.assertEqual(self.call()['error'],'peer_rejected');self.assertEqual(self.calls,[])
 def test_oversized_request_rejected_without_http(self):
  s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.addCleanup(s.close);s.settimeout(4);s.connect(str(self.server.path));s.sendall(struct.pack('!I',MAX_REQUEST+1));hdr=s.recv(4);size=struct.unpack('!I',hdr)[0];out=json.loads(s.recv(size));self.assertFalse(out['ok']);self.assertEqual(self.calls,[])
 def test_delay_guard_prevents_late_send(self):
  def late(opener,request,**kw):
   with patch('kiwoom_autotrader.research.shared_server.time.monotonic_ns',return_value=time.monotonic_ns()+100_000_000_000):kw['before_send']()
   self.fail('expired request must not be sent')
  self.server._open=late;self.assertFalse(self.call()['ok']);self.assertEqual(self.calls,[])
 def test_expired_client_queued_behind_slow_request_never_reaches_broker(self):
  entered=threading.Event();release=threading.Event();sent=[]
  def blocked(opener,request,**kw):
   kw['before_send']();sent.append(request);entered.set();release.wait(4);return Response(self.body)
  self.server._open=blocked;result=[]
  worker=threading.Thread(target=lambda:result.append(self.call()));worker.start();self.assertTrue(entered.wait(2))
  queued=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.addCleanup(queued.close);queued.settimeout(4);queued.connect(str(self.server.path))
  req={'api_id':'ka10017','body':UPPER,'cont_yn':'N','next_key':'','session_id':None,'deadline_ns':time.monotonic_ns()+100_000_000}
  data=json.dumps(req).encode();queued.sendall(struct.pack('!I',len(data))+data)
  release.wait(0.25);release.set();worker.join(3)
  hdr=queued.recv(4);size=struct.unpack('!I',hdr)[0];received=b''
  while len(received)<size:received+=queued.recv(size-len(received))
  self.assertEqual(json.loads(received)['error'],'request_expired');self.assertEqual(len(sent),1)
 def test_stop_while_network_inflight_discards_response(self):
  entered=threading.Event();release=threading.Event()
  def blocked(opener,request,**kw):
   kw['before_send']();entered.set();release.wait(4);return Response(self.body)
  self.server._open=blocked;out=[]
  def run():
   try:out.append(self.call())
   except Exception:out.append(None)
  worker=threading.Thread(target=run);worker.start();self.assertTrue(entered.wait(2));self.server.stop();release.set();worker.join(4);self.assertFalse(worker.is_alive());self.assertTrue(not out or out[0] is None or out[0].get('ok') is False)
if __name__=='__main__':unittest.main()
