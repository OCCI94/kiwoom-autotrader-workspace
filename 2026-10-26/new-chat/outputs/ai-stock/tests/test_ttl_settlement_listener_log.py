"""Sanitized local log cadence; mock broker/Telegram and private clocks only."""
from datetime import datetime
from io import StringIO
import json,unittest
from types import SimpleNamespace
from unittest.mock import Mock
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.telegram import listener
from kiwoom_autotrader.utils.kst_logging import KSTLineWriter

def pending(number='219494',reason='cancellation_rest_original_identity_conflict'):
 return {'status':'settlement_unconfirmed','reason':reason,'original_order_number':number,'principal_reservation_released':False}

class LocalLogTests(unittest.TestCase):
 def setUp(self):
  self.at=0.0;self.output=[];self.log=listener._TTLSettlementLocalLog(clock=lambda:self.at,emit=self.output.append)
 def test_monotonic_ten_minutes_does_not_reset_for_order_rotation_reason_or_brief_clear(self):
  self.assertTrue(self.log.observe(pending()))
  for at,number in [(30,'219709'),(599.999,'219731')]:
   self.at=at;self.assertFalse(self.log.observe(pending(number)))
  self.at=600;self.assertTrue(self.log.observe(pending('219860')))
  self.at=601;self.assertFalse(self.log.observe({'status':'already_settled'}))
  self.at=602;self.assertFalse(self.log.observe(pending('219880','cancellation_ack_unconfirmed')))
  self.at=1200;self.assertTrue(self.log.observe(pending('219443')));self.assertEqual(len(self.output),3)
 def test_nested_status_and_malformed_private_fields_are_sanitized_single_line(self):
  self.log.observe({'status':'cancel_input_ack_unconfirmed_settlement','settlement_status':pending('account-private\n','token=private-exception-secret')})
  text=self.output[0];body=json.loads(text)
  self.assertEqual(body['event'],'ttl_cancellation_settlement_retry');self.assertEqual(body['reason_code'],'settlement_reason_unclassified');self.assertIsNone(body['original_order_number'])
  self.assertNotIn('private',text);self.assertNotIn('\n',text);self.assertFalse(body['principal_reservation_released'])
 def test_invalid_clock_and_failed_log_sink_do_not_raise_or_touch_a_transport(self):
  for clock in (True,float('nan'),float('inf'),-1):
   self.log.clock=lambda:clock;self.assertFalse(self.log.observe(pending()))
  self.assertEqual(self.output,[]);self.log.clock=lambda:0;self.log.emit=Mock(side_effect=OSError('private'))
  self.assertFalse(self.log.observe(pending()));self.assertEqual(self.log.last_at,0)
 def test_existing_kst_stream_formats_log_without_new_sink(self):
  output=StringIO();stream=KSTLineWriter(output,now=lambda:datetime(2026,11,3,17,45,tzinfo=KST))
  self.log.emit=lambda text:(stream.write(text+'\n'),stream.flush())
  self.log.observe(pending());self.assertTrue(output.getvalue().startswith('[2026-11-03 17:45:00 KST] {'))

class SupervisorLogTests(unittest.TestCase):
 def setUp(self):
  self.at=0.0;self.lines=[];self.local=listener._TTLSettlementLocalLog(clock=lambda:self.at,emit=self.lines.append)
  self.session=SimpleNamespace(mode='paper',execution_recorder=None);self.notify=Mock();self.tracker=Mock()
  self.monitor=Mock();self.monitor.status.return_value={'quarantined':{},'symbol_diagnostics':{},'symbol_errors':{},'resume_requested':False}
  self.ttl=SimpleNamespace(tick=Mock(),last_status=pending(),last_sell_status=None,next_at=None)
 def experiment(self):
  supervisor=listener._ExperimentSupervisor(self.monitor,ttl=self.ttl,settings=lambda:{'paper_experiment_enabled':True},
   clock=lambda:self.at,launcher=Mock(),prior_day_scheduler=Mock(),ttl_settlement_log=self.local)
  supervisor.startup_pending=False;return supervisor
 def legacy(self):
  supervisor=listener._LegacyRecoverySupervisor(self.monitor,ttl=self.ttl,settings=lambda:{'gdcrs_unfilled_buy_ttl_seconds':180},
   clock=lambda:self.at,launcher=Mock(),ttl_settlement_log=self.local)
  supervisor.startup=False;return supervisor
 def exercise(self,supervisor):
  for at,number in [(0,'219494'),(30,'219709'),(599,'219731'),(600,'219860')]:
   self.at=at;self.ttl.last_status={'status':'cancel_input_ack_unconfirmed_settlement','settlement_status':pending(number)}
   supervisor.tick(self.session,self.tracker,self.notify,lambda:False)
  self.assertEqual(len(self.lines),2);self.notify.assert_called_once()
 def test_experiment_nested_retries_log_locally_ten_minutes_not_repeated_telegram(self):self.exercise(self.experiment())
 def test_legacy_nested_retries_log_locally_ten_minutes_not_repeated_telegram(self):self.exercise(self.legacy())
 def test_mode_change_shares_one_cadence_across_both_supervisors(self):
  experiment=self.experiment();legacy=self.legacy();experiment.tick(self.session,self.tracker,self.notify,lambda:False)
  self.at=300;legacy.tick(self.session,self.tracker,self.notify,lambda:False);self.assertEqual(len(self.lines),1)
  self.at=600;legacy.tick(self.session,self.tracker,self.notify,lambda:False);self.assertEqual(len(self.lines),2)
