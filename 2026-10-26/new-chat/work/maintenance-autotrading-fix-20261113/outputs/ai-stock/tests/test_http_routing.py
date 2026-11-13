"""All owned HTTP transports use the common queue, and orders guard at dispatch."""
import ast
from datetime import datetime
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from kiwoom_autotrader.api import orders
from kiwoom_autotrader.api.session import BrokerSession, IssuedToken, KST
from kiwoom_autotrader.utils.keychain import Credentials
from kiwoom_autotrader.utils.http_queue import HTTPNotSentError


class HTTPRoutingTests(unittest.TestCase):
    def test_no_direct_http_open_outside_common_queue(self):
        root = Path(orders.__file__).parents[1]
        violations = []
        for file in root.rglob('*.py'):
            if file.name == 'http_queue.py':
                continue
            for node in ast.walk(ast.parse(file.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if isinstance(fn, ast.Attribute) and fn.attr == 'open' and 'opener' in ast.unparse(fn.value):
                    violations.append(str(file.relative_to(root)))
                if isinstance(fn, ast.Attribute) and fn.attr == 'urlopen':
                    violations.append(str(file.relative_to(root)))
        self.assertEqual(violations, [])

    def test_order_session_expiry_or_change_is_checked_at_actual_dispatch(self):
        now = [datetime(2026, 10, 26, 9, tzinfo=KST)]
        session = BrokerSession(now=lambda: now[0], credential_loader=Mock(return_value=Credentials('fixture-key','fixture-secret')),
                                token_issuer=Mock(return_value=IssuedToken('fixture-token', '20261026100000')))
        session.login('paper')
        for mutate in (lambda: now.__setitem__(0, now[0].replace(hour=10)), session.clear):
            now[0] = now[0].replace(hour=9)
            session.login('paper')
            def open_queued(*args, before_send, **kwargs):
                mutate()
                with self.assertRaises(Exception):
                    before_send()
                raise HTTPNotSentError('not sent')
            with patch.object(orders, 'queued_open', side_effect=open_queued) as queued:
                with self.assertRaises(orders.OrderError) as caught:
                    orders.submit_order(session, 'buy', '005930', '1')
            self.assertFalse(caught.exception.uncertain)
            self.assertIn('전송하지 않았습니다', str(caught.exception))
            queued.assert_called_once()

if __name__ == '__main__':
    unittest.main()
