"""Receiver integration gates, independent of external services and account state."""
import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from kiwoom_autotrader.config.settings import (
    _validate, default_settings, effective_toss_hybrid, SettingsError)
from kiwoom_autotrader.trading.multi_strategy import MultiStrategyMonitor


class MondayHybridWiringTests(TestCase):
    def local_toss_fixture(self, codes):
        """Real local admission readers; no broker, credentials or HTTP calls."""
        from tests.test_toss_candidate_feed import NOW, row, feed
        from kiwoom_autotrader.api.session import BrokerSession
        from kiwoom_autotrader.trading.paper_strategy_context import PaperStrategyContextProvider
        from kiwoom_autotrader.trading.toss_candidate_feed import read_toss_candidates
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name).resolve()
        feed_path, risk_path = directory / 'feed.json', directory / 'risk.json'
        document = feed()
        document['candidates'] = [row(code) for code in codes]
        risk = {'schema':'toss-hybrid-risk-evidence-v1', 'generated_at':NOW.isoformat(),
            'expires_at':document['candidates'][0]['risk_review']['expires_at'],
            'rows':[dict(item['risk_review'], symbol=item['symbol'],
                source_sha256=item['source_hashes']['risk_review']) for item in document['candidates']]}
        for path, data in ((feed_path, document), (risk_path, risk)):
            path.write_text(json.dumps(data)); path.chmod(0o600)
        provider = PaperStrategyContextProvider(BrokerSession(now=lambda:NOW), now=lambda:NOW,
            enable_toss_hybrid=True, toss_candidates_path=feed_path, s5_risk_path=risk_path)
        current = read_toss_candidates(feed_path, NOW)
        self.assertEqual(current['status'], 'ready')
        self.assertEqual(len(current['candidates']), len(codes))
        saved = [copy.deepcopy(item) | {'feed_sha256':current['source_sha256'],
            'feed_expires_at':current['expires_at']} for item in current['candidates']]
        return provider, saved, risk_path, risk, feed_path, document

    def test_feed_is_opt_in_and_paper_only(self):
        self.assertFalse(effective_toss_hybrid({}, 'paper'))
        self.assertTrue(effective_toss_hybrid({'paper_toss_hybrid_enabled':True}, 'paper'))
        self.assertFalse(effective_toss_hybrid({'paper_toss_hybrid_enabled':True}, 'real'))
        self.assertFalse(default_settings()['paper_toss_hybrid_enabled'])
        for value in (1, 0, 'true', None):
            with self.subTest(value=value), self.assertRaises(SettingsError):
                _validate({**default_settings(), 'paper_toss_hybrid_enabled':value})

    def test_default_provider_receives_only_opt_in_not_credentials(self):
        with patch('kiwoom_autotrader.trading.multi_strategy.PaperStrategyContextProvider') as make:
            monitor=MultiStrategyMonitor(settings_loader=lambda:{'paper_toss_hybrid_enabled':True})
            session=object();monitor._provider_factory(session)
            make.assert_called_once_with(session, enable_surge=True, enable_toss_hybrid=True,
                                         enable_prearmed_risk=False)

    def test_toss_admission_is_revalidated_at_dispatch_and_not_as_surge(self):
        from tests.test_toss_candidate_feed import NOW
        from kiwoom_autotrader.trading.toss_candidate_feed import validate_candidate
        at=NOW
        monitor=MultiStrategyMonitor();monitor._discovery_binding='fixture-session'
        provider, saved, risk_path, risk, feed_path, document = self.local_toss_fixture(['005930'])
        monitor._provider = provider
        proof={'kind':'verified_toss_watch','available_at':at.isoformat(),
            'session_binding':'fixture-session','toss_evidence':saved[0],
            'toss_session_day':'2026-11-02','toss_previous_session_day':'2026-10-30'}
        monitor._entry_admission={'005930':proof}
        with patch('kiwoom_autotrader.trading.toss_candidate_feed.validate_candidate',wraps=validate_candidate) as check:
            self.assertTrue(monitor._entry_admitted('005930',at))
            self.assertEqual(check.call_args.kwargs['previous_session_day'],'2026-10-30')
            self.assertFalse(monitor._entry_admitted('005930',at+timedelta(minutes=1)))
        # The copied proof is still young, but the latest local risk revokes it.
        risk['rows'][0]['unresolved_major_risk'] = True
        risk_path.write_text(json.dumps(risk))
        self.assertFalse(monitor._entry_admitted('005930',at))
        risk['rows'][0]['unresolved_major_risk'] = False
        risk_path.write_text(json.dumps(risk))
        self.assertTrue(monitor._entry_admitted('005930',at))
        # A newer feed generation cannot be bypassed by keeping the old proof.
        document['generated_at'] = (at + timedelta(seconds=1)).isoformat()
        feed_path.write_text(json.dumps(document))
        self.assertFalse(monitor._entry_admitted('005930',at+timedelta(seconds=1)))
        with patch('kiwoom_autotrader.trading.toss_candidate_feed.validate_candidate',side_effect=ValueError('stale')):
            self.assertFalse(monitor._entry_admitted('005930',at))
        with patch('kiwoom_autotrader.trading.toss_candidate_feed.validate_candidate',return_value={'symbol':'000660'}):
            self.assertFalse(monitor._entry_admitted('005930',at))

    def test_feed_failure_does_not_revoke_existing_kiwoom_admission(self):
        at=datetime(2026,11,2,10,30,1,tzinfo=ZoneInfo('Asia/Seoul'))
        monitor=MultiStrategyMonitor();monitor._discovery_binding='fixture-session'
        monitor._entry_admission={'005930':{'kind':'verified_discovery',
            'available_at':at.isoformat(),'session_binding':'fixture-session'}}
        with patch('kiwoom_autotrader.trading.toss_candidate_feed.validate_candidate',side_effect=AssertionError('must not be called')):
            self.assertTrue(monitor._entry_admitted('005930',at))

    def test_base_twenty_five_preserved_with_fifteen_independent_toss_slots(self):
        from tests.test_toss_candidate_feed import NOW
        base=[f'{n:06d}' for n in range(25)]
        dynamic=[f'{n:06d}' for n in range(100,115)]
        settings={'gdcrs_initial_entry_pct':50,'paper_toss_hybrid_enabled':True}
        monitor=MultiStrategyMonitor(now=lambda:NOW)
        provider, saved, risk_path, risk, _, _ = self.local_toss_fixture(dynamic)
        saved_by_code = {item['symbol']:item for item in saved}
        rows=[{'symbol':code,'available_at':NOW.isoformat(),
            'membership_evidence':{'source':'ka10099'},'source_hashes':{'membership':'b'*64,'toss_feed':'a'*64},
            'candidate_basis':'toss_hybrid_verified_watch','toss_evidence':saved_by_code[code]} for code in dynamic]
        result={'schema':'paper-dynamic-candidates-v1','status':'ready',
            'available_at':NOW.isoformat(),'session_binding':'fixture-session',
            'candidates':rows,'base_candidates':[],
            'toss_discovery':{'session_day':'2026-11-02','previous_session_day':'2026-10-30'}}
        monitor._provider=SimpleNamespace(discover_dynamic_candidates=lambda **kwargs:result,
            toss_candidate_is_current=provider.toss_candidate_is_current,
            watch_candidate_is_current=provider.watch_candidate_is_current)
        monitor._refresh_entries([(code,500000) for code in base[:3]],
            {'enabled':True,'limit':15,'stocks':[(code,1000000) for code in base[3:]],
             'entry_pool_limit':40,'max_positions':8},NOW,NOW.replace(second=0),settings)
        self.assertEqual(set(monitor._entry_budgets),set(base+dynamic))
        self.assertTrue(all(budget==500000 for budget in monitor._entry_budgets.values()))
        self.assertTrue(monitor._entry_admitted(dynamic[0],NOW))
        self.assertEqual(monitor._entry_admission[dynamic[0]]['kind'],'verified_toss_watch')
        self.assertFalse(monitor._entry_admitted(dynamic[0],NOW+timedelta(seconds=76)))
        self.assertEqual(monitor._scan_diagnostics['dynamic_entry_count'],15)
        risk['rows'][0]['unresolved_major_risk'] = True
        risk_path.write_text(json.dumps(risk))
        self.assertFalse(monitor._entry_admitted(dynamic[0],NOW))
        self.assertTrue(monitor._entry_admitted(dynamic[1],NOW))
        self.assertEqual(set(monitor._entry_budgets),set(base+dynamic))
