"""A settings reset must never relax paper position and order limits."""
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch
from tests.paper_reconciliation_fixture import fake_reconciliation

from kiwoom_autotrader.config import settings
from kiwoom_autotrader.telegram.commands.settings import handle_settings
from kiwoom_autotrader.trading.order_context import automated_orders
from kiwoom_autotrader.trading.paper_policy import (
    PaperAutomationPolicyError, capture_automatic_paper_guard,
    limit_automatic_buy_budget,
)


class ResetPaperSafetyTests(TestCase):
    def test_stts_default_preserves_automatic_order_and_position_limits(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(settings, 'settings_path', return_value=Path(directory)/'settings.json'):
            settings.save_settings({'paper_automation_only': True, 'paper_automation_buy_cap': 400000})
            session=Mock(mode='paper')
            session.paper_automation_reconcile=Mock(side_effect=fake_reconciliation)
            session.paper_automation_reconcile.available_buy_principal=Mock(return_value=280000)
            result=handle_settings('stts default', session)
            self.assertIn('기본값', result[0])
            self.assertTrue(settings.load_settings()['paper_automation_only'])
            tracker=Mock()
            tracker.assert_no_pending_order=Mock()
            with automated_orders(tracker, 'gdcrs'):
                self.assertEqual(limit_automatic_buy_budget(session, '005930', 1000000), 280000)
                guard=capture_automatic_paper_guard(session, 'buy', '005930', '11', '100000')
                with self.assertRaises(PaperAutomationPolicyError): guard.before_prepare()
                session.paper_automation_reconcile.assert_not_called()
                guard=capture_automatic_paper_guard(session, 'buy', '005930', '2', '100000')
                self.assertTrue(guard.before_prepare())
                guard()
                session.paper_automation_reconcile.assert_called_once_with('005930', side='buy', quantity='2', price='100000')
                tracker.assert_no_pending_order.assert_called_once_with('paper', '005930')
