import unittest
import json
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken
from kiwoom_autotrader.trading.paper_strategy_context import PaperStrategyContextProvider, ContextUnavailable

KST = ZoneInfo('Asia/Seoul')

class Clock:
    def __init__(self): self.value = datetime(2026,10,30,10,10,10,tzinfo=KST)
    def __call__(self): return self.value

class ContextTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.session = BrokerSession(now=self.clock)
        self.session.mode = 'paper'
        self.session._issued = IssuedToken('test', '20261030235959')
        self.data = {'stk_cd':'005930', 'cur_prc':'+1000','high_pric':'1100',
                     'upl_pric':'1300','flu_rt':'-1.25','trde_pre':'210.50'}
        self.calls = 0
        self.provider = PaperStrategyContextProvider(self.session, now=self.clock, fetcher=self.fetcher)
    def fetcher(self, session, symbol):
        self.calls += 1
        return dict(self.data)
    def test_basic_cache_precision_and_provenance(self):
        first = self.provider.fetch('005930')
        self.assertEqual(first['current_price'],1000)
        self.assertEqual(first['change_pct'],'-1.25')
        self.assertEqual(first['previous_day_volume_ratio_pct'],'210.50')
        self.assertIsNone(first['same_time_rvol20'])
        self.assertIsNone(first['theme_evidence'])
        self.assertIsNone(first['source_market_timestamp'])
        self.assertFalse(first['source_freshness_verified'])
        self.assertEqual(len(first['source_sha256']),64)
        first['current_price'] = 1
        self.assertEqual(self.provider.fetch('005930')['current_price'],1000)
        self.assertEqual(self.calls,1)
        self.clock.value += timedelta(seconds=5, microseconds=1)
        self.assertFalse(self.provider.is_fresh(first))
        self.provider.fetch('005930')
        self.assertEqual(self.calls,2)
    def test_session_rotation_invalidates_cache_and_old_context(self):
        first = self.provider.fetch('005930')
        self.session._issued = IssuedToken('new', '20261030235959')
        self.assertFalse(self.provider.is_fresh(first))
        second = self.provider.fetch('005930')
        self.assertNotEqual(first['session_binding'], second['session_binding'])
        self.assertEqual(self.calls,2)
    def test_real_session_never_fetches(self):
        self.session.mode = 'real'
        with self.assertRaises(ContextUnavailable): self.provider.fetch('005930')
        self.assertEqual(self.calls,0)
    def test_session_changes_during_fetch(self):
        def change(session, symbol):
            session._issued = IssuedToken('new', '20261030235959')
            return self.data
        self.provider._fetcher = change
        with self.assertRaises(ContextUnavailable): self.provider.fetch('005930')
    def test_response_conflicts_and_missing_fail_closed(self):
        for key, value in [('stk_cd','000000'), ('cur_prc',''), ('high_pric','999'),
                           ('upl_pric','1099'), ('trde_pre','NaN'), ('flu_rt','NaN')]:
            original = self.data[key]
            self.data[key] = value
            with self.subTest(key=key), self.assertRaises(ContextUnavailable): self.provider.fetch('005930')
            self.data[key] = original
    def test_signed_previous_volume_comparison_is_metadata_only(self):
        import hashlib, json
        self.data['trde_pre'] = '-12.34'
        value = self.provider.fetch('005930')
        self.assertEqual(value['previous_day_volume_ratio_pct'], '-12.34')
        self.assertIsNone(value['rising_volume_surge'])
        self.assertIsNone(value['same_time_rvol20'])
        fields=('stk_cd','cur_prc','open_pric','high_pric','upl_pric','flu_rt','trde_pre')
        normalized={key:self.data.get(key,'') for key in fields}
        digest=hashlib.sha256(json.dumps(normalized,sort_keys=True,ensure_ascii=True,
                                        separators=(',',':')).encode()).hexdigest()
        self.assertEqual(value['source_sha256'],digest)

    def test_missing_optional_metrics_are_unknown(self):
        self.data['trde_pre'] = ''
        self.data['flu_rt'] = ''
        result = self.provider.fetch('005930')
        self.assertIsNone(result['change_pct'])
        self.assertIsNone(result['previous_day_volume_ratio_pct'])
    def test_slow_and_minute_boundary_fail_closed(self):
        def slow(session, symbol):
            self.clock.value += timedelta(seconds=6)
            return self.data
        self.provider._fetcher = slow
        with self.assertRaises(ContextUnavailable): self.provider.fetch('005930')
        self.clock.value = self.clock.value.replace(second=59)
        def rollover(session, symbol):
            self.clock.value += timedelta(seconds=1)
            return self.data
        self.provider._fetcher = rollover
        with self.assertRaises(ContextUnavailable): self.provider.fetch('005930')
    def test_outside_market_or_invalid_requested_time_fails(self):
        self.clock.value = self.clock.value.replace(hour=8)
        with self.assertRaises(ContextUnavailable): self.provider.fetch('005930')
        self.clock.value = self.clock.value.replace(hour=10)
        with self.assertRaises(ContextUnavailable): self.provider.fetch('005930', self.clock.value+timedelta(seconds=1))
        with self.assertRaises(ContextUnavailable): self.provider.fetch('005930', datetime(2026,10,30))
        self.assertEqual(self.calls,0)
    def test_freshness_rejects_future_receipt_or_bad_binding(self):
        data = self.provider.fetch('005930')
        self.assertTrue(self.provider.is_fresh(data))
        self.clock.value -= timedelta(seconds=1)
        self.assertFalse(self.provider.is_fresh(data))
        self.clock.value += timedelta(seconds=1)
        data['session_binding'] = 'other'
        self.assertFalse(self.provider.is_fresh(data))
    def test_unknown_or_suffix_symbol_rejected(self):
        for symbol in ['005930_NX','wrong','']:
            with self.assertRaises(ContextUnavailable): self.provider.fetch(symbol)
        self.assertEqual(self.calls,0)

class RankingContextTests(unittest.TestCase):
    def setUp(self):
        ContextTests.setUp(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.taxonomy = Path(self.tmp.name) / 'taxonomy.json'
        self.taxonomy.write_text(json.dumps({'schema':'pro6e-sector-taxonomy-v1','metadata_only':True,
          'created_at':'2026-10-29T10:00:00+09:00','kr_stocks':{
          '005930':{'sub_themes':['반도체']},'000660':{'sub_themes':['반도체']}}}))
        self.data['flu_rt'] = '8.5'
        self.surge_calls = self.turnover_calls = 0
        self.provider = PaperStrategyContextProvider(self.session, now=self.clock, fetcher=self.fetcher,
             enable_surge=True, surge_fetcher=self.surge, turnover_fetcher=self.turnover,
             taxonomy_path=self.taxonomy)
    def fetcher(self, session, symbol):
        self.calls += 1
        return dict(self.data)
    def surge(self):
        self.surge_calls += 1
        return {'ok':True,'wire_sha256':'a'*64,'headers':{'cont-yn':'Y'},'body':{'trde_qty_sdnin':[
          {'stk_cd':'005930','flu_rt':'8.5','sdnin_rt':'320'},
          {'stk_cd':'000660','flu_rt':'5.0','sdnin_rt':'200'}]}}
    def turnover(self):
        self.turnover_calls += 1
        return {'ok':True,'wire_sha256':'b'*64,'headers':{'cont-yn':'Y'},'body':{'trde_prica_upper':[
          {'stk_cd':'005930','trde_prica':'20'}, {'stk_cd':'000660','trde_prica':'10'}]}}
    def test_surge_and_theme_known_candidate_and_cache(self):
        from kiwoom_autotrader.trading.multi_strategy import _theme_or_disclosure
        result = self.provider.fetch('005930')
        self.assertTrue(result['rising_volume_surge'])
        self.assertTrue(result['surge_evidence']['truncated'])
        self.assertEqual(result['theme_evidence']['members'][0]['turnover_krw'],20_000_000)
        self.assertFalse(result['theme_evidence']['full_theme_leader_verified'])
        self.assertTrue(_theme_or_disclosure(result, '005930', self.clock()))
        self.provider.fetch('005930')
        self.assertEqual((self.surge_calls,self.turnover_calls), (1,1))
    def test_future_taxonomy_blocks_s4_only(self):
        data=json.loads(self.taxonomy.read_text())
        data['created_at']='2026-10-31T10:00:00+09:00'
        self.taxonomy.write_text(json.dumps(data))
        result=self.provider.fetch('005930')
        self.assertTrue(result['rising_volume_surge'])
        self.assertIsNone(result['theme_evidence'])
        self.assertEqual(self.turnover_calls,0)
    def test_not_in_first_surge_page_unknown_and_no_turnover(self):
        self.provider._surge_fetcher=lambda: {'ok':True,'wire_sha256':'a'*64,
          'headers':{'cont-yn':'Y'},'body':{'trde_qty_sdnin':[]}}
        result=self.provider.fetch('005930')
        self.assertIsNone(result['rising_volume_surge'])
        self.assertEqual(self.turnover_calls,0)
    def test_surge_failure_only_one_try_per_minute(self):
        def fail():
            self.surge_calls+=1
            raise OSError('simulated')
        self.provider._surge_fetcher=fail
        self.provider.fetch('005930')
        self.clock.value += timedelta(seconds=6)
        self.provider.fetch('005930')
        self.assertEqual(self.surge_calls,1)
        self.clock.value += timedelta(minutes=1)
        self.provider.fetch('005930')
        self.assertEqual(self.surge_calls,2)
    def test_disagreeing_current_direction_blocks(self):
        self.data['flu_rt']='-1'
        result=self.provider.fetch('005930')
        self.assertIsNone(result['rising_volume_surge'])
        self.assertIsNone(result['theme_evidence'])
    def test_incomplete_peer_intersection_blocks_theme(self):
        self.provider._turnover_fetcher=lambda:{'ok':True,'wire_sha256':'b'*64,
          'headers':{'cont-yn':'Y'},'body':{'trde_prica_upper':[{'stk_cd':'005930','trde_prica':'20'}]}}
        result=self.provider.fetch('005930')
        self.assertTrue(result['rising_volume_surge'])
        self.assertIsNone(result['theme_evidence'])

class ExitContextTests(unittest.TestCase):
    def setUp(self):
        ContextTests.setUp(self)
        self.provider = PaperStrategyContextProvider(self.session, now=self.clock,
            fetcher=self.fetcher, exit_only=True)
    def fetcher(self, session, symbol):
        self.calls += 1
        return dict(self.data)
    def test_exit_only_requires_price_not_high_limit_or_ranking(self):
        self.data.update(high_pric='', upl_pric='', flu_rt='', trde_pre='')
        value = self.provider.fetch('005930')
        self.assertTrue(value['exit_only'])
        self.assertEqual(value['current_price'], 1000)
        self.assertIsNone(value['upper_limit_price'])
        self.assertIsNone(value['day_high'])
        self.assertIsNone(value['rising_volume_surge'])
        self.assertTrue(self.provider.is_fresh(value))
    def test_exit_only_rejects_missing_price_and_wrong_code(self):
        for key, val in [('cur_prc',''), ('stk_cd','000660')]:
            original = self.data[key]
            self.data[key] = val
            with self.assertRaises(ContextUnavailable): self.provider.fetch('005930')
            self.data[key] = original
    def test_exit_cannot_enable_ranking(self):
        with self.assertRaises(ValueError):
            PaperStrategyContextProvider(self.session, exit_only=True, enable_surge=True)

if __name__ == '__main__': unittest.main()
