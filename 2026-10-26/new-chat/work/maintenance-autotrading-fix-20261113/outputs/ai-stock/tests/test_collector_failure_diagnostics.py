import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from kiwoom_autotrader.telegram import collector
from kiwoom_autotrader import main


class CollectorDiagnosticsTests(unittest.TestCase):
    def test_original_failure_phase_is_saved_without_private_exception(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(collector,'load_account_config',side_effect=RuntimeError('SECRET_SESSION_MUST_NOT_LEAK')), patch('builtins.print') as printer:
            with self.assertRaises(collector.CollectorError): asyncio.run(collector.run_collector(Path(tmp),max_cycles=1))
            raw=(Path(tmp)/'collector-status.json').read_text();value=json.loads(raw)
            self.assertEqual(value['phase'],'account_configuration');self.assertEqual(value['error_code'],'unexpected_failure')
            self.assertNotIn('SECRET_SESSION_MUST_NOT_LEAK',raw+str(printer.call_args_list))

    def test_account_command_errors_are_kst_stamped_and_sanitized(self):
        out=io.StringIO()
        with patch('sys.stderr',out), patch('kiwoom_autotrader.utils.dependencies.enable_account_dependencies'), patch('kiwoom_autotrader.telegram.account_credentials.load_account_config',side_effect=PermissionError('SECRET_KEY')):
            result=main._telegram_account_command(SimpleNamespace(command='telegram-account-status'))
        self.assertEqual(result,1);self.assertRegex(out.getvalue(),r'^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d KST\] ')
        self.assertNotIn('SECRET_KEY',out.getvalue())

    def test_safe_failure_categories_do_not_serialize_remote_content(self):
        self.assertEqual(collector.failure_code(TimeoutError('remote token')), 'connection_timeout')
        self.assertEqual(collector.failure_code(PermissionError('account')), 'local_permission_denied')

if __name__=='__main__':unittest.main()
