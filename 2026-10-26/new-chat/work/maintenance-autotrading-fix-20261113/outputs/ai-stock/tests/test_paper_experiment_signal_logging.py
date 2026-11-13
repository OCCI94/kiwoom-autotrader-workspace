from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from zoneinfo import ZoneInfo
from kiwoom_autotrader.trading.strategy_signal_log import append_signal_event

class ExperimentSignalLogTests(TestCase):
    def test_six_extended_arms_are_persisted_without_becoming_fills(self):
        with TemporaryDirectory() as root:
            at=datetime(2026,11,2,10,tzinfo=ZoneInfo('Asia/Seoul'))
            arms=('EXP_ORB_A','EXP_ORB_B','EXP_GDR','EXP_VSS','EXP_LDR','EXP_ETF_B')
            for arm in arms:
                event={'mode':'paper','strategy':arm,'symbol':'005930','side':'buy',
                       'event_kind':'decision_outcome','observed_at':at.isoformat(),
                       'execution_status':'unfillable_observed','vi_observation':{'vi_active':True},
                       'skip_reason':'vi_active_unfillable'}
                append_signal_event(root,event)
            lines=Path(root,'strategy-signal-events-20261102.jsonl').read_text().splitlines()
            records=[json.loads(line)['event'] for line in lines]
            self.assertEqual([x['strategy'] for x in records],list(arms))
            self.assertTrue(all(x['research_only'] and not x['broker_fill'] for x in records))
            self.assertTrue(all(x['vi_observation']['vi_active'] for x in records))
