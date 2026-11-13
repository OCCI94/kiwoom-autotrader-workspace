"""Owned production-ledger fixtures only; no live session/IPC/transport."""
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
import sqlite3,tempfile,unittest
from unittest.mock import Mock
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.api.cancellations import CancellationError,CancellationReceipt
from kiwoom_autotrader.trading.execution_log import ExecutionTracker
from kiwoom_autotrader.trading.unfilled_buy_ttl import TTLLedger
from tests import test_unfilled_buy_ttl as fixtures

class CloseWindowTests(unittest.TestCase):
 setUp=fixtures.TTLTests.setUp
 make_monitor=fixtures.TTLTests.make_monitor
 own_order=fixtures.TTLTests.own_order
 ack=fixtures.TTLTests.ack
 auction_order=fixtures.AuctionWindowTests.auction_order
 def private_order(self,arm,*,bad_hash=False,end='2026-10-30T15:30:00+09:00'):
  temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
  self.tracker=ExecutionTracker(log_dir=Path(temp.name),now=lambda:self.now)
  self.auction_order(arm,'2026-10-30T15:20:43+09:00',end,bad_hash=bad_hash)
 def test_d_vss_ldr_verified_window_survives_180_seconds_and_expires_at_1530(self):
  for arm in ('EXP_D','EXP_VSS','EXP_LDR'):
   with self.subTest(arm=arm):
    self.private_order(arm);ledger=TTLLedger(self.tracker)
    for clock in ('15:24:00','15:29:59.999999'):
     self.assertEqual(ledger.due(datetime.fromisoformat('2026-10-30T'+clock+'+09:00'),180)[0],[])
    rows,_=ledger.due(datetime(2026,10,30,15,30,tzinfo=KST),180)
    self.assertEqual(len(rows),1);self.assertEqual(rows[0]['due_reason'],'auction_window_end')
    self.assertEqual(rows[0]['expiry_policy'],'verified_auction_window');self.assertEqual(rows[0]['expires_at'],'2026-10-30T15:30:00+09:00')
 def test_missing_bad_hash_or_wrong_day_never_extends_ordinary_180_seconds(self):
  for arm in ('EXP_D','EXP_VSS','EXP_LDR'):
   for end,bad_hash in [('2026-10-30T15:30:00+09:00',True),('2026-10-31T15:30:00+09:00',False),(None,False)]:
    with self.subTest(arm=arm,end=end,bad_hash=bad_hash):
     self.private_order(arm,bad_hash=bad_hash,end=end)
     rows,_=TTLLedger(self.tracker).due(datetime(2026,10,30,15,24,tzinfo=KST),180)
     self.assertEqual(len(rows),1);self.assertEqual(rows[0]['due_reason'],'ttl');self.assertEqual(rows[0]['expires_at'],'2026-10-30T15:23:43+09:00')

class AllDueTests(unittest.TestCase):
 setUp=fixtures.TTLTests.setUp
 make_monitor=fixtures.TTLTests.make_monitor
 own_order=fixtures.TTLTests.own_order
 ack=fixtures.TTLTests.ack
 counts=fixtures.TTLTests.counts
 def add_due(self,number,symbol):
  self.own_order(number=number,strategy='EXP_B')
  with sqlite3.connect(self.tracker.path) as db:
   db.execute('UPDATE orders SET symbol=? WHERE ord_no=?',(symbol,number));db.execute('UPDATE intents SET symbol=? WHERE id=?',(symbol,'intent'+number))
  self.pending.append(dict(self.pending[0],ord_no=number.zfill(7),stk_cd='A'+symbol))
 def test_entire_due_set_fresh_each_order_even_when_batch_exceeds_ten_seconds(self):
  self.add_due('3','000660');self.add_due('4','035420');clock=[0.0];self.monitor.monotonic=lambda:clock[0]
  def cancel(session,symbol,number,*,before_send):
   before_send();self.now+=timedelta(seconds=11);clock[0]+=11
   return CancellationReceipt(number,str(int(number)+100),0)
  self.cancel.side_effect=cancel;self.monitor.tick(self.session,self.tracker,180)
  self.assertEqual(self.cancel.call_count,3);self.assertEqual(self.fetch.call_count,3);self.assertEqual(self.counts(),(3,3))
  self.assertEqual(self.monitor.last_status['batch_due_count'],3);self.assertEqual(self.monitor.last_status['batch_processed_count'],3)
  self.assertEqual(self.monitor.last_status['batch_elapsed_seconds'],33)
  self.assertTrue(all(r['principal_reservation_released'] is False for r in self.monitor.last_status['batch_results']))
 def test_first_unknown_then_proven_not_sent_do_not_starve_other_due_orders(self):
  self.add_due('3','000660');self.add_due('4','035420')
  def cancel(session,symbol,number,*,before_send):
   before_send()
   if number=='1':raise CancellationError('cancel_response_unconfirmed',uncertain=True)
   if number=='3':raise CancellationError('cancel_not_sent')
   return CancellationReceipt(number,'104',0)
  self.cancel.side_effect=cancel;self.monitor.tick(self.session,self.tracker,180)
  self.assertEqual(self.cancel.call_count,3);self.assertEqual(self.fetch.call_count,3);self.assertEqual(self.counts(),(3,3))
  self.assertEqual(self.monitor.last_status['status'],'retry_pending')
  self.now+=timedelta(seconds=31);self.cancel.side_effect=self.ack;self.make_monitor().tick(self.session,self.tracker,180)
  self.assertEqual(self.cancel.call_count,4);self.assertEqual(self.cancel.call_args.args[2],'3')
 def test_first_identity_mismatch_does_not_skip_a_different_valid_order(self):
  self.add_due('3','000660');self.pending[0]['stk_cd']='A035420'
  self.monitor.tick(self.session,self.tracker,180)
  self.cancel.assert_called_once();self.assertEqual(self.cancel.call_args.args[2],'3')
  self.assertEqual(self.monitor.last_status['batch_processed_count'],2)
 def test_stream_loss_after_first_ack_blocks_remaining_transports_without_release(self):
  self.add_due('3','000660');self.add_due('4','035420')
  def cancel(session,symbol,number,*,before_send):
   before_send();self.stream['ready']=False;return CancellationReceipt(number,'101',0)
  self.cancel.side_effect=cancel;self.monitor.tick(self.session,self.tracker,180)
  self.cancel.assert_called_once();self.fetch.assert_called_once();self.assertEqual(self.counts(),(1,1))
  self.assertEqual(self.monitor.last_status['batch_processed_count'],3)
  self.assertTrue(all(r['principal_reservation_released'] is False for r in self.monitor.last_status['batch_results']))
