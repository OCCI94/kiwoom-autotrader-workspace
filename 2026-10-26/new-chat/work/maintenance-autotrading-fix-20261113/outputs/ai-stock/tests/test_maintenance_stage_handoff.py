"""Receiver stage handoff keeps commands and strategy work behind both latches."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
import unittest

from kiwoom_autotrader.telegram import listener
from tests.test_maintenance_reconciliation import PhaseFixture


class ReceiverMaintenanceHandoffTests(PhaseFixture, unittest.TestCase):
    def setUp(self):
        self.initialize()

    def test_expiry_stage_does_not_replace_the_bound_readonly_query_batch(self):
        self.phase.tick = Mock(side_effect=AssertionError('bound read batch overwritten'))
        expiry = Mock()
        expiry.requested.return_value = True
        expiry.tick.return_value = {'status': 'complete', 'resolution_type': 'expired'}
        result = listener._maintenance_reconciliation_tick(
            self.session, self.tracker, self.state_dir, self.phase, expiry)
        self.assertEqual(result['resolution_type'], 'expired')
        expiry.tick.assert_called_once_with(self.session, self.tracker, self.state_dir, self.phase)
        self.phase.tick.assert_not_called()

    def test_invalid_second_stage_cannot_fall_back_to_a_new_read_phase(self):
        expiry = Mock()
        expiry.requested.return_value = True
        expiry.tick.side_effect = ValueError('unverified resolution marker')
        self.phase.tick = Mock()
        with self.assertRaises(ValueError):
            listener._maintenance_reconciliation_tick(
                self.session, self.tracker, self.state_dir, self.phase, expiry)
        self.phase.tick.assert_not_called()

    def test_real_second_stage_actor_never_reverts_to_reads_after_marker_replacement(self):
        expiry = listener.MaintenanceExpiryResolution(now=lambda: self.now)
        marker = json.loads(self.marker.read_text())
        marker['prior_expiry_resolution'] = {}
        self.marker.write_text(json.dumps(marker))
        self.phase.tick = Mock(side_effect=AssertionError('stage2 fell back to fresh reads'))
        first = listener._maintenance_reconciliation_tick(
            self.session, self.tracker, self.state_dir, self.phase, expiry)
        self.assertEqual(first['status'], 'failed')
        self.renew_marker()  # The original read-stage shape cannot reset stage2.
        second = listener._maintenance_reconciliation_tick(
            self.session, self.tracker, self.state_dir, self.phase, expiry)
        self.assertEqual(second['status'], 'failed')
        self.assertTrue(expiry.blocked)
        self.phase.tick.assert_not_called()

    def test_order_before_send_checks_second_latch_after_first_is_released(self):
        self.marker.unlink()
        self.phase.blocked = False
        expiry = SimpleNamespace(assert_orders_allowed=Mock(side_effect=ValueError('expiry proof pending')))
        with self.assertRaisesRegex(ValueError, 'expiry proof pending'):
            listener._maintenance_order_guard(self.state_dir, self.phase, expiry)
        expiry.assert_orders_allowed.assert_called_once_with()

    def _receive_once(self, *, paused, complete=False):
        class Expiry:
            blocked = True
            def __init__(self):
                self.tick = Mock(return_value={'status': 'complete' if complete else 'failed'})
                self.release_if_verified = Mock(return_value=False)
            def latch(self, _): return self.blocked
            def requested(self, _): return True
            def status(self): return {'status': 'complete' if complete else 'failed'}

        expiry = Expiry()
        self.session.maintenance_expiry_resolution = expiry
        self.phase.tick = Mock(side_effect=AssertionError('ordinary read batch forbidden in stage2'))
        self.phase.release_if_verified = Mock(return_value=True)
        self.session.websocket_engine = Mock()
        self.session.websocket_engine.snapshot.return_value = {}
        self.tracker.tick = Mock(side_effect=AssertionError('ordinary tracker forbidden'))
        config = SimpleNamespace(bot_token='private-fixture', bot_id=123, bot_username='fixture')
        strategy = Mock(); strategy.status.return_value = {}
        supervisor = Mock(); supervisor.status.return_value = {}
        scheduler = Mock(); legacy = Mock(); legacy.status.return_value = {}
        client = Mock()
        client.get_me.return_value = {'id': 123, 'username': 'fixture'}
        client.get_webhook_info.return_value = {'url': ''}
        status = {'paused': paused, 'reason': 'maintenance_active' if paused else 'no_marker',
                  'reconciliation_required': paused}
        if not paused:
            self.marker.unlink()  # Match the real filesystem, not only mocked status.
        self.phase.blocked = paused
        with patch.object(listener, 'MaintenanceExpiryResolution', Expiry), \
             patch.object(listener, 'maintenance_status', return_value=status), \
             patch.object(listener, '_maintenance_paused', return_value=paused), \
             patch.object(self.session, 'login', return_value=SimpleNamespace(message='fixture')), \
             patch.object(listener, 'terminal_report'), \
             patch.object(listener, 'TelegramClient', return_value=client), \
             patch.object(listener, '_LegacyRecoverySupervisor', return_value=legacy), \
             patch.object(listener, 'ReceiverStabilityAudit') as audit, \
             patch.object(listener, 'initialize_control_audit'), \
             patch.object(listener, '_send_notification'), \
             patch.object(listener.time, 'sleep'):
            audit.return_value.pulse.return_value = {}
            state = {}
            # A removed marker cannot authorize the ordinary initial ticker.
            self.phase.blocked = True
            self.assertEqual(listener._receive(
                self.state_dir, state, config, 1, self.session, scheduler,
                strategy, self.tracker, strategy, strategy, supervisor), 0)
        self.tracker.tick.assert_not_called()
        self.phase.tick.assert_not_called()
        scheduler.claim_due.assert_not_called()
        client.get_updates.assert_not_called()
        supervisor.tick.assert_not_called()
        legacy.tick.assert_not_called()
        strategy.tick.assert_not_called()
        self.assertTrue(state['maintenance_paused'])
        return expiry

    def test_receive_during_resolution_skips_reserved_commands_ttl_and_normal_monitor(self):
        expiry = self._receive_once(paused=True)
        expiry.tick.assert_called_once()
        expiry.release_if_verified.assert_not_called()

    def test_deleted_marker_does_not_release_failed_or_unfresh_expiry_proof(self):
        expiry = self._receive_once(paused=False, complete=True)
        expiry.tick.assert_not_called()
        expiry.release_if_verified.assert_called_once_with(self.session, self.tracker)
        self.phase.release_if_verified.assert_not_called()

    def test_restart_discards_resolution_diagnostics_without_restoring_readiness(self):
        config = SimpleNamespace(bot_id=123, chat_id=456)
        saved = listener._new_state(config, 1000)
        saved.update(next_offset=42, maintenance_expiry_resolution={'status': 'complete'})
        path = self.state_dir / 'state.json'
        listener._save_state(path, saved)
        loaded = listener._load_state(path, config, 1001)
        self.assertNotIn('maintenance_expiry_resolution', loaded)
        self.assertEqual(loaded['next_offset'], 42)


if __name__ == '__main__':
    unittest.main()
