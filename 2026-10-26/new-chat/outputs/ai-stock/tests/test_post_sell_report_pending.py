"""Latest sell, latest query attempts and full quantities must agree offline."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import sqlite3
import unittest

from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.trading.execution_log import _post_sell_report_state
from tests import test_execution_accounting as fixture


class PostSellReportStateTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 26, 10, 0, tzinfo=KST)
        self.events = []
        self.sell(1, 2)
        self.cost('ka10072', 2)
        self.cost('ka10073', 2)

    def add(self, event):
        self.events.append({'id': len(self.events) + 1, 'payload_json': json.dumps(event)})

    def sell(self, unit, quantity, **updates):
        event = {'day': '20261026', 'mode': 'paper', 'source_api': '00',
                 'event_kind': 'execution_stream', 'observed_at': self.now.isoformat(),
                 'values': {'9001': '097520', '907': '1', '913': '체결', '9203': '24',
                            '909': str(unit), '914': '1000', '915': str(quantity)}}
        event['values'].update(updates)
        self.add(event)

    def cost(self, api, quantity, *, query=None, started=None, updates=None):
        field = 'dt_stk_div_rlzt_pl' if api == 'ka10072' else 'dt_stk_rlzt_pl'
        row = {'stk_cd': '097520', 'crd_tp': '현금잔고', 'cntr_qty': str(quantity),
               'buy_uv': '900', 'cntr_pric': '1000', 'tdy_trde_cmsn': '1', 'tdy_trde_tax': '1', 'tdy_sel_pl': str(quantity*100-2)}
        if api == 'ka10073':
            row['dt'] = '20261026'
        request = {'stk_cd': '097520', 'strt_dt': '20261026'}
        if api == 'ka10073':
            request['end_dt'] = '20261026'
        event = {'schema': 'kiwoom-broker-page-v1', 'mode': 'paper', 'day': '20261026',
                 'source_api': api, 'query_id': query or str(len(self.events)) * 32, 'page': 1,
                 'request_started_at': (started or self.now).isoformat(), 'observed_at': self.now.isoformat(),
                 'request_body': request, 'response_body': {'return_code': 0, field: [row]},
                 'continuation': {'request_cont_yn': 'N', 'response_cont_yn': 'N', 'next_key_present': False},
                 'http_status': 200, 'status': 'success'}
        event.update(updates or {})
        self.add(event)

    def state(self):
        return _post_sell_report_state('20261026', 'paper', '097520', self.events)[0]

    def test_requests_at_exact_latest_sell_boundary_are_valid(self):
        self.assertEqual(self.state()['status'], 'reports_current')

    def test_new_sell_invalidates_two_preexisting_queries(self):
        self.now += timedelta(seconds=1)
        self.sell(2, 1)
        state = self.state()
        self.assertEqual(state['status'], 'post_sell_report_pending')
        self.assertEqual(state['observed_sell_quantity'], 3)
        self.assertIn('ka10072:query_started_before_latest_sell', state['reason_codes'])
        self.cost('ka10072', 3)
        self.assertEqual(self.state()['status'], 'post_sell_report_pending')
        self.cost('ka10073', 3)
        self.assertEqual(self.state()['status'], 'reports_current')

    def test_response_after_sell_is_not_enough_when_request_was_before_it(self):
        self.cost('ka10073', 2, started=self.now-timedelta(seconds=1), query='f'*32)
        self.assertEqual(self.state()['status'], 'post_sell_report_pending')

    def test_latest_partial_or_failed_query_never_falls_back(self):
        for change in ({'status': 'transport_error'},
                       {'continuation': {'request_cont_yn': 'N', 'response_cont_yn': 'Y', 'next_key_present': True}},
                       {'http_status': 503}):
            with self.subTest(change=change):
                original = deepcopy(self.events)
                self.cost('ka10073', 2, query='f'*32, updates=change)
                self.assertEqual(self.state()['status'], 'post_sell_report_pending')
                self.events = original

    def test_complete_both_still_require_actual_unit_quantity(self):
        self.cost('ka10073', 1, query='f'*32)
        self.assertEqual(self.state()['status'], 'post_sell_report_pending')
        self.assertIn('ka10073:sell_quantity_not_reconciled', self.state()['reason_codes'])

    def test_amount_difference_is_preserved_for_existing_reconciler_not_freshness_gate(self):
        raw = json.loads(self.events[-1]['payload_json'])
        raw['response_body']['dt_stk_rlzt_pl'][0]['cntr_pric'] = '1000.01'
        self.events[-1]['payload_json'] = json.dumps(raw)
        state = self.state()
        self.assertEqual(state['status'], 'reports_current')
        self.assertEqual(state['reports']['ka10073']['sell_amount'], '2000.02')
        self.assertEqual(state['observed_sell_amount'], '2000')

    def test_identical_unit_repeat_is_not_summed_twice_but_conflict_blocks(self):
        self.sell(1, 2)
        self.assertEqual(self.state()['observed_sell_quantity'], 2)
        self.sell(1, 3)
        self.assertEqual(self.state()['status'], 'post_sell_report_pending')
        self.assertIn('sell_unit_conflict', self.state()['reason_codes'])

    def test_missing_unit_fields_are_not_reconstructed_from_cumulative(self):
        self.sell(2, 1, **{'915': '', '911': '3'})
        self.assertEqual(self.state()['status'], 'post_sell_report_pending')
        self.assertIsNone(self.state()['observed_sell_quantity'])

    def test_required_cost_fields_and_bool_success_fail_closed(self):
        raw = json.loads(self.events[-1]['payload_json'])
        del raw['response_body']['dt_stk_rlzt_pl'][0]['tdy_trde_tax']
        self.events[-1]['payload_json'] = json.dumps(raw)
        self.assertEqual(self.state()['status'], 'post_sell_report_pending')
        raw['response_body']['return_code'] = False
        self.events[-1]['payload_json'] = json.dumps(raw)
        self.assertEqual(self.state()['status'], 'post_sell_report_pending')

    def test_multi_page_query_must_finish_with_full_quantity(self):
        self.events = self.events[:2]
        self.cost('ka10073', 1, query='e'*32,
                  updates={'continuation': {'request_cont_yn': 'N', 'response_cont_yn': 'Y', 'next_key_present': True}})
        self.cost('ka10073', 1, query='e'*32, updates={'page': 2,
                  'continuation': {'request_cont_yn': 'Y', 'response_cont_yn': 'N', 'next_key_present': False}})
        self.assertEqual(self.state()['status'], 'reports_current')


class ProjectionGateTests(unittest.TestCase):
    def test_fresh_73_reprojects_production_72_cache_without_second_fetch(self):
        from tests.test_profit_period_reprojection import ProfitPeriodReprojectionTests
        from unittest.mock import Mock
        fx = ProfitPeriodReprojectionTests(methodName='runTest')
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        fx.tracker._profit_source_kind = 'broker_observation'
        raw = {'stk_cd': '097520', 'crd_tp': '현금잔고', 'cntr_qty': '49',
               'buy_uv': '20150', 'cntr_pric': '20392', 'tdy_trde_cmsn': '100', 'tdy_trde_tax': '50', 'tdy_sel_pl': '11708'}
        def fetch(session, symbol, day):
            fx.fx.cost('ka10072', [raw], query='e'*32)
            return fx.rows
        fx.tracker.profit_fetcher = Mock(side_effect=fetch)
        fx.tracker.period_profit_fetcher = fx.period(100, 11708)
        fx.tracker.tick(fx.session, Mock())
        self.assertIsNone(fx.net())
        fx.tracker.tick(fx.session, Mock())
        self.assertEqual(fx.net(), '11708')
        fx.tracker.profit_fetcher.assert_called_once()

    def test_production_source_cannot_confirm_missing_or_stale_raw_queries(self):
        fx = fixture.ExecutionAccountingTests(methodName='runTest')
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        fx.unit(1, 49, 49, 20392, 999208, 100)
        from decimal import Decimal
        from kiwoom_autotrader.api.executions import RealizedProfit
        rows = (RealizedProfit('097520', 49, Decimal(20392), Decimal(20150), Decimal(100), Decimal(50), reported_net=Decimal(11708)),)
        fx.tracker._profit_source_kind = 'broker_observation'
        fx.tracker._profit('paper', '20261026', '097520', rows)
        with sqlite3.connect(fx.tracker.path) as db:
            self.assertIsNone(db.execute('SELECT net FROM profits').fetchone()[0])
            payload = json.loads(db.execute('SELECT payload_json FROM profit_observations ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        self.assertEqual(payload['post_sell_report_state']['status'], 'post_sell_report_pending')
        self.assertNotIn('conservative_estimate', payload.get('amount_reconciliation') or {})
        row = {'stk_cd': '097520', 'dt': '20261026', 'crd_tp': '현금잔고', 'cntr_qty': '49',
               'buy_uv': '20150', 'cntr_pric': '20392', 'tdy_trde_cmsn': '100', 'tdy_trde_tax': '50', 'tdy_sel_pl': '11708'}
        fx.cost('ka10072', [{key: value for key, value in row.items() if key != 'dt'}], query='1'*32)
        fx.cost('ka10073', [row], query='2'*32)
        fx.tracker._profit('paper', '20261026', '097520', rows)
        with sqlite3.connect(fx.tracker.path) as db:
            self.assertEqual(db.execute('SELECT net FROM profits').fetchone()[0], '11708')


if __name__ == '__main__':
    unittest.main()
