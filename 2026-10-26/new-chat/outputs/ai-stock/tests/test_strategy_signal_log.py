from datetime import datetime
import hashlib,json
from pathlib import Path
from types import SimpleNamespace
import tempfile
from unittest import TestCase
from unittest.mock import patch

from kiwoom_autotrader.trading.strategy_signal_log import append_signal_event,record_signal_safely


class StrategySignalLogTests(TestCase):
    def event(self):
        return {'mode':'paper','strategy':'S2_surge_high_breakout','symbol':'005930','side':'buy',
                'event_kind':'eligible_signal','observed_at':'2026-10-30T11:00:01+09:00',
                'reason':'shallow_pullback_high_breakout','research_only':False,'broker_fill':True}

    def test_append_preserves_prior_bytes_and_fixed_research_contract(self):
        with tempfile.TemporaryDirectory() as root:
            digest=append_signal_event(root,self.event())
            path=Path(root)/'strategy-signal-events-20261030.jsonl';before=path.read_bytes()
            row=json.loads(before)
            self.assertTrue(row['event']['research_only']);self.assertFalse(row['event']['broker_fill'])
            raw=json.dumps(row['event'],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
            self.assertEqual(hashlib.sha256(raw).hexdigest(),digest)
            append_signal_event(root,{**self.event(),'event_kind':'decision_outcome','reason':'insufficient_cash'})
            self.assertTrue(path.read_bytes().startswith(before));self.assertEqual(len(path.read_text().splitlines()),2)

    def test_busy_or_failed_research_sink_is_visible_without_raising(self):
        tracker=SimpleNamespace(record_strategy_signal=lambda event: (_ for _ in ()).throw(OSError('not persisted')))
        self.assertFalse(record_signal_safely(tracker,self.event())['ok'])
        self.assertEqual(tracker.research_signal_log_status['failure_count'],1)

    def test_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as root:
            target=Path(root)/'existing';target.write_text('untouched')
            (Path(root)/'strategy-signal-events-20261030.jsonl').symlink_to(target)
            with self.assertRaises(ValueError):append_signal_event(root,self.event())
            self.assertEqual(target.read_text(),'untouched')
