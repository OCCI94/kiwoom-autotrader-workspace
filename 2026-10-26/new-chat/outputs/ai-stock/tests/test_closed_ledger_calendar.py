"""Official saved dates only: missing source/hash never creates market days."""
from datetime import datetime,timedelta
from pathlib import Path
import json,tempfile,unittest
from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.trading.closed_ledger_calendar import closed_poll_plan

class ClosedCalendarTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
  self.now=datetime(2026,11,3,16,tzinfo=KST);self.archive=self.root/'archive';self.claims=self.root/'claims';self.claims.mkdir()
 def save(self,day,nextday):
  start=day+'T09:00:00+09:00';end=day+'T15:30:00+09:00';at=day+'T08:50:00+09:00'
  today={'date':day,'integrated':{'regularMarket':{'startTime':start,'endTime':end}}}
  nxt={'date':nextday,'integrated':{'regularMarket':{'startTime':nextday+'T09:00:00+09:00','endTime':nextday+'T15:30:00+09:00'}}}
  body={'schema':'toss-decoded-response-evidence-v1','request':{'endpoint_key':'market_calendar_kr','market_country':'KR'},
   'result':{'status':'ok','endpoint_key':'market_calendar_kr','observed_at':at,'response_sha256':'a'*64,'response':{'result':{'today':today,'nextBusinessDay':nxt}}}}
  path=self.archive/day/'toss'/'0001-calendar-current.json';path.parent.mkdir(parents=True);path.write_text(json.dumps(body))
  claim=self.claims/(day.replace('-','')+'-0850.claim.json')
  claim.write_text(json.dumps({'schema':'toss-dart-refresh-slot-claim-v1','session_day':day,'calendar_source_sha256':'a'*64,
   'claimed_at':at,'regular_start':start,'regular_end':end}));claim.chmod(0o600)
  return claim
 def run_plan(self):return closed_poll_plan(self.now,'20261026',archive_root=self.archive,claim_root=self.claims)
 def test_explicit_next_business_close_and_elapsed_observed_session(self):
  self.save('2026-11-03','2026-11-05')
  result=self.run_plan();self.assertEqual(result['next_close'],datetime(2026,11,5,15,31,tzinfo=KST));self.assertEqual(result['elapsed_sessions'],1)
 def test_no_calendar_or_hash_mismatch_cannot_guess_weekday(self):
  self.assertIsNone(self.run_plan()['next_close']);claim=self.save('2026-11-03','2026-11-04')
  value=json.loads(claim.read_text());value['calendar_source_sha256']='b'*64;claim.write_text(json.dumps(value))
  self.assertIsNone(self.run_plan()['next_close']);self.assertIsNone(self.run_plan()['elapsed_sessions'])
 def test_future_observation_and_unknown_hours_rejected(self):
  self.save('2026-11-03','2026-11-04');self.now=self.now.replace(hour=8,minute=49)
  self.assertIsNone(self.run_plan()['next_close'])

 def test_claim_may_precede_later_same_wire_calendar_observation(self):
  claim=self.save('2026-11-03','2026-11-04')
  path=self.archive/'2026-11-03'/'toss'/'0001-calendar-current.json'
  value=json.loads(path.read_text());value['result']['observed_at']='2026-11-03T08:50:01+09:00';path.write_text(json.dumps(value))
  self.assertEqual(self.run_plan()['next_close'],datetime(2026,11,4,15,31,tzinfo=KST))

 def test_old_matching_claim_cannot_authorize_unbounded_later_observation(self):
  self.save('2026-11-03','2026-11-04')
  path=self.archive/'2026-11-03'/'toss'/'0001-calendar-current.json'
  value=json.loads(path.read_text());value['result']['observed_at']='2026-11-03T09:06:00+09:00';path.write_text(json.dumps(value))
  self.assertIsNone(self.run_plan()['next_close'])
