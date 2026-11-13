"""Private temporary checkpoints only; no runtime state/credentials/API access."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from kiwoom_autotrader.telegram import listener
from kiwoom_autotrader.telegram.bot_credentials import TelegramConfig


CONFIG = TelegramConfig('123456789:fixture_token_abcdefghijklmnopqrstuvwxyz', 778899001,
                        123456789, 'fixture_bot')


def encoded(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode('utf-8')


class CheckpointBoundTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'state.json'
        self.base = listener._new_state(CONFIG, 1000)
        self.base.update(next_offset=123456789, poll_count=160000, received_count=23,
                         replied_count=104, last_command='status', last_reply_at=999)

    def diagnostics(self, count):
        rows = {}
        for i in range(count):
            rows[f'{i:06d}'] = {
                'schema':'prearmed-account-advisory-v1', 'status':'observed_advisory',
                'reason':'requires_fresh_reconciliation', 'observed_at':'2026-11-01T19:00:00+09:00',
                'prepared_at':'2026-11-01T19:00:01+09:00', 'expires_at':'2026-11-01T19:00:03.500000+09:00',
                'theme_remaining_slots':2, 'position_remaining_slots':8, 'observed_held_qty':0,
                'observed_held_principal_krw':0, 'reserved_principal_krw':0,
                'remaining_principal_krw':1000000, 'available_cash_krw':6676039,
                'estimated_initial_qty':50, 'reservation_status':'no_pending_at_observation',
                'order_authorized':False, 'requires_fresh_reconciliation':True,
                'snapshot_is_atomic_broker_balance':False,
                'scope_sha256':'a'*64, 'account_source_sha256':'b'*64,
                'theme_families':['security', 'software'],
                'quantity_kind':'observed_upper_bound_before_fees_and_final_reconciliation',
                'source_kind':'broker_observation',
                'estimated_remaining_qty':100,
                'observed_deposit_krw':6676039, 'observed_orderable_cash_krw':9533906,
            }
        return {'paper_multi_strategy':{
            'symbol_input_diagnostics':{code:{'stage':'dormant_waiting_volume',
                'observed_at':'2026-11-01T19:00:01+09:00', 'reason':'awaiting_regular_session',
                'order_permission':False, 'interpolation_applied':False} for code in rows},
            'fast_decision_pipeline':{
                'stage':'running', 'credentials_present':True, 'account_preparation':rows}}}

    def test_25_and_40_diagnostics_over_old_limit_restart_preserves_offsets(self):
        for count in (25, 40):
            with self.subTest(symbols=count):
                state = dict(self.base, automation_status=self.diagnostics(count),
                             execution_recording={'ready':True}, maintenance_paused=True)
                self.assertGreater(len(encoded(state)), 30_000)
                self.assertLess(len(encoded(state)), listener._STATE_LIMIT)
                listener._save_state(self.path, state)
                loaded = listener._load_state(self.path, CONFIG, 2000)
                for key in ('next_offset', 'poll_count', 'received_count', 'replied_count',
                            'last_command', 'last_reply_at'):
                    self.assertEqual(loaded[key], self.base[key])
                self.assertEqual(loaded['started_at'], 2000)
                self.assertEqual(loaded['pid'], os.getpid())
                self.assertEqual(loaded['status'], 'running')
                for key in ('execution_recording', 'automation_status', 'maintenance_paused'):
                    self.assertNotIn(key, loaded)
                self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_exact_256k_checkpoint_roundtrip_and_one_byte_over_rejected(self):
        self.assertEqual(listener._STATE_LIMIT, 256 * 1024)
        state = dict(self.base, automation_status={'diagnostic':''})
        state['automation_status']['diagnostic'] = 'x' * (listener._STATE_LIMIT - len(encoded(state)))
        self.assertEqual(len(encoded(state)), listener._STATE_LIMIT)
        listener._save_state(self.path, state)
        self.assertEqual(listener._load_state(self.path, CONFIG, 2000)['next_offset'], self.base['next_offset'])
        prior = self.path.read_bytes(); metadata = self.path.stat()
        state['automation_status']['diagnostic'] += 'x'
        with patch.object(listener.tempfile, 'mkstemp') as create, patch.object(listener.os, 'replace') as replace:
            with self.assertRaises(listener.TelegramListenerError) as caught:
                listener._save_state(self.path, state)
            self.assertEqual(listener.listener_error_code(caught.exception), 'state_storage_failure')
            create.assert_not_called(); replace.assert_not_called()
        self.assertEqual(self.path.read_bytes(), prior)
        self.assertEqual(self.path.stat().st_ino, metadata.st_ino)
        self.assertEqual(list(self.path.parent.glob('.state-*.tmp')), [])

    def test_reader_still_rejects_above_shared_bound(self):
        self.path.write_bytes(b' ' * (listener._STATE_LIMIT + 1))
        with self.assertRaises(listener.TelegramListenerError) as caught:
            listener._load_state(self.path, CONFIG, 2000)
        self.assertEqual(listener.listener_error_code(caught.exception), 'state_storage_failure')

    def test_unserializable_value_never_creates_partial_checkpoint(self):
        listener._save_state(self.path, self.base); prior = self.path.read_bytes()
        with patch.object(listener.tempfile, 'mkstemp') as create:
            with self.assertRaises(listener.TelegramListenerError):
                listener._save_state(self.path, dict(self.base, automation_status=object()))
            create.assert_not_called()
        self.assertEqual(self.path.read_bytes(), prior)

    def test_replace_failure_preserves_checkpoint_and_removes_temporary(self):
        listener._save_state(self.path, self.base); prior = self.path.read_bytes()
        with patch.object(listener.os, 'replace', side_effect=OSError('fixture disk failure')):
            with self.assertRaises(listener.TelegramListenerError):
                listener._save_state(self.path, dict(self.base, next_offset=999))
        self.assertEqual(self.path.read_bytes(), prior)
        self.assertEqual(list(self.path.parent.glob('.state-*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
