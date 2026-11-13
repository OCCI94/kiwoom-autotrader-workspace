import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from kiwoom_autotrader.telegram import collector
from kiwoom_autotrader import main


class CollectorDiagnosticsTests(unittest.TestCase):
    def test_original_failure_phase_is_saved_without_private_exception(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, patch.object(collector,'load_account_config',side_effect=RuntimeError('SECRET_SESSION_MUST_NOT_LEAK')), patch('sys.stdout',out):
            with self.assertRaises(collector.CollectorError): asyncio.run(collector.run_collector(Path(tmp),max_cycles=1))
            raw=(Path(tmp)/'collector-status.json').read_text();value=json.loads(raw)
            self.assertEqual(value['phase'],'account_configuration');self.assertEqual(value['error_code'],'unexpected_failure')
            self.assertRegex(out.getvalue(), rf'^\[\d{{4}}-\d\d-\d\d \d\d:\d\d:\d\d KST\] pid={os.getpid()} ')
            self.assertIn('phase=account_configuration code=unexpected_failure', out.getvalue())
            self.assertNotIn('SECRET_SESSION_MUST_NOT_LEAK',raw+out.getvalue())

    def test_pre_status_lock_failure_logs_without_overwriting_current_owner(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            status = directory / 'collector-status.json'
            status.write_bytes(b'{"pid":123}')
            with patch.object(collector.fcntl, 'flock', side_effect=BlockingIOError('PRIVATE_LOCK')), patch('sys.stdout', out):
                with self.assertRaises(collector.CollectorError):
                    asyncio.run(collector.run_collector(directory, max_cycles=1))
            self.assertEqual(status.read_bytes(), b'{"pid":123}')
        self.assertIn(f'pid={os.getpid()}', out.getvalue())
        self.assertIn('phase=acquire_lock code=collector_validation_failed', out.getvalue())
        self.assertNotIn('PRIVATE_LOCK', out.getvalue())

    def test_worker_dependency_failure_is_logged_with_pid_without_exception_text(self):
        out = io.StringIO()
        args = SimpleNamespace(command='telegram-account-listen', state_dir=Path('/unused'))
        with patch('sys.stderr', out), patch('kiwoom_autotrader.utils.dependencies.enable_account_dependencies', side_effect=ImportError('PRIVATE_MODULE')):
            self.assertEqual(main._telegram_account_command(args), 1)
        self.assertIn(f'pid={os.getpid()}', out.getvalue())
        self.assertIn('phase=load_dependencies code=dependency_import_failure', out.getvalue())
        self.assertNotIn('PRIVATE_MODULE', out.getvalue())

    def test_worker_loop_uses_safe_background_exception_handler(self):
        async def run(_state_dir):
            loop = asyncio.get_running_loop()
            loop.call_exception_handler({'message': 'PRIVATE_CONTEXT', 'exception': TimeoutError('PRIVATE_EXCEPTION')})
            return 0

        out = io.StringIO()
        args = SimpleNamespace(command='telegram-account-listen', state_dir=Path('/unused'))
        with patch('sys.stdout', out), patch('kiwoom_autotrader.utils.dependencies.enable_account_dependencies'), patch.object(collector, 'run_collector', run):
            self.assertEqual(main._telegram_account_command(args), 0)
        self.assertIn(f'pid={os.getpid()}', out.getvalue())
        self.assertIn('phase=asyncio_background code=connection_timeout', out.getvalue())
        self.assertNotIn('PRIVATE_', out.getvalue())

    def test_asyncio_does_not_fall_back_to_raw_context_if_diagnostics_fail(self):
        async def run():
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(collector.log_background_failure)
            with patch.object(loop, 'default_exception_handler') as fallback, patch.object(
                collector, '_collector_log', side_effect=OSError('diagnostic sink failed')
            ):
                loop.call_exception_handler({'exception': TimeoutError('PRIVATE_CONTEXT')})
                fallback.assert_not_called()

        asyncio.run(run())

    def test_status_file_write_failure_is_attributed_to_storage_write(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            collector.tempfile, 'mkstemp', side_effect=PermissionError('PRIVATE_PATH')
        ), patch('sys.stdout', out):
            with self.assertRaises(collector.CollectorError):
                asyncio.run(collector.run_collector(Path(tmp), max_cycles=1))
        self.assertIn('phase=write_status code=local_permission_denied', out.getvalue())
        self.assertNotIn('PRIVATE_PATH', out.getvalue())

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
