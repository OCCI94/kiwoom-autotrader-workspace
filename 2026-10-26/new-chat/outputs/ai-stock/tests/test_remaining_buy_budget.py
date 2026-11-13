"""Offline command sizing against the automatic paper position cap."""
from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from kiwoom_autotrader.api.orders import OrderReceipt
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.telegram.commands import orders
from kiwoom_autotrader.trading.order_context import automated_orders


class RemainingBudgetCommandTests(TestCase):
    def setUp(self):
        self.now=datetime(2026,10,27,10,0,tzinfo=KST)
        self.session=BrokerSession(now=lambda:self.now)
        self.session.mode='paper';self.session._issued=IssuedToken('offline-budget-token','20300101000000')
        self.tracker=Mock()
        self.capacity=Mock(return_value=280000)
        self.session.paper_automation_reconcile=SimpleNamespace(available_buy_principal=self.capacity)
        self.guard=Mock()
        self.settings={'paper_automation_only':True,'paper_automation_buy_cap':1000000}
        self.quote=Mock(return_value={'stk_cd':'005930','cur_prc':'70000'})
        self.submit=Mock(side_effect=lambda session,side,symbol,qty,price,**kw:
            OrderReceipt('paper',side,symbol,qty,price,'0000001','https://mockapi.kiwoom.com'))
        for target,replacement in (('kiwoom_autotrader.trading.paper_policy.load_settings',lambda:self.settings),
                                   ('kiwoom_autotrader.telegram.commands.orders.fetch_stock_info',self.quote),
                                   ('kiwoom_autotrader.telegram.commands.orders.submit_order',self.submit)):
            patcher=patch(target,side_effect=replacement);patcher.start();self.addCleanup(patcher.stop)
        network=patch('socket.socket.connect',side_effect=AssertionError('external transport forbidden'))
        self.network=network.start();self.addCleanup(network.stop)

    def run_auto(self):
        with automated_orders(self.tracker,'gdcrs'):
            return orders.handle_buy('buy 005930 max 1000000',self.session,before_send=self.guard)

    def test_resizes_before_quote_and_reports_reduced_exposure(self):
        replies=self.run_auto()
        self.capacity.assert_called_once_with('005930')
        self.quote.assert_called_once_with(self.session,'005930')
        self.submit.assert_called_once_with(self.session,'buy','005930','4','70000',before_send=self.guard)
        self.assertIn('요청 예산 1,000,000원 → 적용 예산 280,000원',replies[0])
        self.assertIn('최대 매수대금 280,000원',replies[0])
        self.assertGreaterEqual(self.guard.call_count,3)
        self.network.assert_not_called()

    def test_zero_remaining_blocks_before_quote_and_order(self):
        self.capacity.return_value=0
        replies=self.run_auto()
        self.assertEqual(replies,[])
        self.assertEqual(replies.skipped_reason,'symbol_buy_limit_exhausted')
        self.assertFalse(replies.uncertain)
        self.quote.assert_not_called();self.submit.assert_not_called()

    def test_remaining_below_one_share_silently_skips_without_order(self):
        self.capacity.return_value=69999
        replies=self.run_auto()
        self.assertEqual(replies,[])
        self.assertEqual(replies.skipped_reason,'budget_below_one_share')
        self.assertFalse(replies.uncertain)
        self.quote.assert_called_once();self.submit.assert_not_called()

    def test_manual_budget_below_one_share_still_gives_feedback(self):
        replies=orders.handle_buy('buy 005930 max 69999',self.session)
        self.assertIn('1주도',replies[0])
        self.assertIsNone(replies.skipped_reason)
        self.capacity.assert_not_called();self.submit.assert_not_called()

    def test_bad_quote_does_not_become_silent_skip(self):
        for quote in ('0', 'bad', '-1.5', None):
            with self.subTest(quote=quote):
                self.quote.return_value={'stk_cd':'005930','cur_prc':quote}
                replies=self.run_auto()
                self.assertTrue(replies)
                self.assertIsNone(replies.skipped_reason)
                self.assertEqual(replies.decision_reason, 'quote_unavailable')
                self.submit.assert_not_called()

    def test_unaffordable_quote_after_session_or_strategy_change_is_not_silent(self):
        for change in ('session', 'strategy'):
            with self.subTest(change=change):
                self.session.mode='paper'
                self.session._issued=IssuedToken('offline-budget-token','20300101000000')
                self.guard.reset_mock(side_effect=True)
                def quote(*args):
                    if change == 'session':
                        self.session._issued=IssuedToken('other-offline-token','20300101000000')
                    else:
                        self.guard.side_effect=RuntimeError('private-strategy-change')
                    return {'stk_cd':'005930','cur_prc':'1000001'}
                self.quote.side_effect=quote
                replies=self.run_auto()
                self.assertTrue(replies)
                self.assertIsNone(replies.skipped_reason)
                self.submit.assert_not_called()

    def test_account_failure_is_safe_and_does_not_quote_or_order(self):
        self.capacity.side_effect=RuntimeError('private-raw-provider-token')
        replies=self.run_auto()
        self.assertIn('전송하지 않았습니다',replies[0])
        self.assertNotIn('private-raw-provider-token',replies[0])
        self.assertFalse(replies.uncertain)
        self.quote.assert_not_called();self.submit.assert_not_called()

    def test_session_change_during_capacity_read_blocks_quote_and_order(self):
        def change(_):
            self.session._issued=IssuedToken('different-offline-token','20300101000000')
            return 280000
        self.capacity.side_effect=change
        replies=self.run_auto()
        self.assertIn('변경',replies[0]);self.assertIn('전송하지 않았습니다',replies[0])
        self.quote.assert_not_called();self.submit.assert_not_called()

    def test_strategy_change_during_capacity_read_blocks_quote_and_order(self):
        valid={'enabled':True}
        def check():
            if not valid['enabled']:raise RuntimeError('private-strategy-detail')
        self.guard.side_effect=check
        def change(_):
            valid['enabled']=False
            return 280000
        self.capacity.side_effect=change
        replies=self.run_auto()
        self.assertIn('전송하지 않았습니다',replies[0])
        self.assertNotIn('private-strategy-detail',replies[0])
        self.quote.assert_not_called();self.submit.assert_not_called()

    def test_session_change_in_post_capacity_guard_blocks_even_quote(self):
        def change():
            if self.guard.call_count == 2:self.session.mode='real'
        self.guard.side_effect=change
        replies=self.run_auto()
        self.assertIn('로그인 상태가 변경',replies[0])
        self.quote.assert_not_called();self.submit.assert_not_called()

    def test_invalid_capacity_helper_cannot_expand_requested_budget(self):
        with patch.object(orders,'limit_automatic_buy_budget',return_value=1000001):
            replies=self.run_auto()
        self.assertIn('요청 범위를 벗어났습니다',replies[0])
        self.quote.assert_not_called();self.submit.assert_not_called()

    def test_manual_budget_command_keeps_original_sizing_and_no_account_read(self):
        replies=orders.handle_buy('buy 005930 max 1000000',self.session)
        self.capacity.assert_not_called()
        self.submit.assert_called_once_with(self.session,'buy','005930','14','70000')
        self.assertIn('예산 1,000,000원',replies[0])
        self.assertNotIn('안전 한도',replies[0])

    def test_automatic_budget_respects_lower_per_buy_limit(self):
        self.settings['paper_automation_buy_cap']=200000
        self.capacity.return_value=900000
        replies=self.run_auto()
        self.submit.assert_called_once_with(self.session,'buy','005930','2','70000',before_send=self.guard)
        self.assertIn('적용 예산 200,000원',replies[0])

    def test_paper_default_false_still_limits_to_remaining_position_capacity(self):
        self.settings['paper_automation_only']=False
        self.run_auto()
        self.capacity.assert_called_once_with('005930')
        self.submit.assert_called_once_with(self.session,'buy','005930','4','70000',before_send=self.guard)

    def test_policy_or_cap_change_while_fetching_capacity_blocks_buy(self):
        for change in ({'paper_automation_only':False},{'paper_automation_buy_cap':500000}):
            with self.subTest(change=change):
                self.settings={'paper_automation_only':True,'paper_automation_buy_cap':1000000}
                def mutate(_):
                    self.settings.update(change)
                    return 280000
                self.capacity.side_effect=mutate
                replies=self.run_auto()
                self.assertIn('전송하지 않았습니다',replies[0])
                self.quote.assert_not_called();self.submit.assert_not_called()
