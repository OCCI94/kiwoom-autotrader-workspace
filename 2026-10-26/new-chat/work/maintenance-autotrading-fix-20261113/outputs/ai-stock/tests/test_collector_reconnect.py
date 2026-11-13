"""Transient network failure keeps the same PID/client and durable cursors."""
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch
from tests import test_telegram_collector as fixtures
from kiwoom_autotrader.telegram import collector


class CollectorReconnectTests(IsolatedAsyncioTestCase):
    setUp=fixtures.CollectorTests.setUp
    patch=fixtures.CollectorTests.patch
    status=fixtures.CollectorTests.status
    async def test_connect_timeouts_reconnect_same_client_with_exponential_wait(self):
        self.client.connect.side_effect=[TimeoutError('PRIVATE'),TimeoutError('PRIVATE'),None]
        with patch.object(collector,'_wait_until',new=AsyncMock(return_value=True)) as wait, \
                patch.object(collector.asyncio,'wait_for',wraps=collector.asyncio.wait_for) as timeout:
            self.assertEqual(await collector.run_collector(self.directory,max_cycles=3),0)
        status=self.status()
        self.assertEqual(self.client.connect.await_count,3)
        self.assertEqual(status['cycle_count'],1);self.assertEqual(status['reconnect_count'],2)
        self.assertEqual([c.args[0] for c in wait.await_args_list],[1005,1010])
        self.assertEqual(status['last_connection_failure']['retry_delay_seconds'],10)
        self.assertTrue(status['last_connection_failure']['same_process'])
        self.assertNotIn('PRIVATE',str(status)+str(self.printer.call_args_list))
        self.assertEqual(sum(c.kwargs.get('timeout')==60 for c in timeout.call_args_list),4)

    async def test_connection_loss_after_commit_reuses_existing_cursor_without_relogin(self):
        original=collector.collect_once;calls=[]
        async def failing_once(*args,**kwargs):
            result=await original(*args,**kwargs);calls.append(result)
            if len(calls)==1:raise ConnectionResetError('private session')
            return result
        with patch.object(collector,'collect_once',side_effect=failing_once), \
                patch.object(collector,'_wait_until',new=AsyncMock(return_value=True)):
            self.assertEqual(await collector.run_collector(self.directory,max_cycles=2),0)
        self.assertEqual(self.client.connect.await_count,2)
        self.assertEqual(self.store.channel['latest_id'],250)
        self.assertEqual([c[2]['collection_kind'] for c in self.store.commits],['latest_page','backfill_page'])
        self.assertNotIn('private session',str(self.status()))

    async def test_stop_during_backoff_stays_stopped_no_reconnect(self):
        self.client.connect.side_effect=TimeoutError('private')
        with patch.object(collector,'_wait_until',new=AsyncMock(return_value=False)):
            self.assertEqual(await collector.run_collector(self.directory),0)
        self.assertEqual(self.client.connect.await_count,1)
        self.assertEqual(self.status()['status'],'stopped')

    async def test_local_storage_error_never_enters_network_retry(self):
        self.store.check_storage_health=Mock(side_effect=PermissionError('private path'))
        with patch.object(collector,'_wait_until',new=AsyncMock()) as wait:
            with self.assertRaises(collector.CollectorError):
                await collector.run_collector(self.directory,max_cycles=2)
        self.client.connect.assert_not_awaited();wait.assert_not_awaited()

