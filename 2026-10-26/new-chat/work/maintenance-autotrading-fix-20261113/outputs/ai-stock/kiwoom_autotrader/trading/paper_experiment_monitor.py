"""Receiver-owned randomized paper experiment with a read-only discovery worker.

Signals, attempts and broker fills are separate records. Every order still uses
the shared queued transport, paper reconciliation and the atomic owned ledger.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
import uuid

from kiwoom_autotrader.api.session import KST
from kiwoom_autotrader.api.minute_chart import fetch_session_minute_chart, session_identity
from kiwoom_autotrader.api.account_report import fetch_account_report
from kiwoom_autotrader.api.orders import submit_order
from kiwoom_autotrader.api.order_book import fetch_budget_limit
from kiwoom_autotrader.api.stock_info import fetch_stock_info
from kiwoom_autotrader.config.settings import load_settings, effective_paper_experiment
from kiwoom_autotrader.config.paper_hot_policy import DEFAULT_CHANGES
from kiwoom_autotrader.trading.paper_policy import experiment_order_sizing
from kiwoom_autotrader.trading.order_context import automated_orders
from kiwoom_autotrader.trading.decision_evidence import _serialize_bar, strategy_source_hash
from kiwoom_autotrader.trading.paper_experiment_rules import (
    assign_candidate, assign_cohort, evaluate_entry, evaluate_exit, normalize_exit_session,
    select_overnight, validate_assignment)
from kiwoom_autotrader.trading.paper_experiment_store import ExperimentStore, canonical
from kiwoom_autotrader.trading.paper_experiment_calendar import current_session
from kiwoom_autotrader.trading.paper_strategy_context import PaperStrategyContextProvider
from kiwoom_autotrader.trading.multi_strategy import (
    EntryState, entry_signal, next_entry_state, exit_signal, STRATEGIES, S5)
from kiwoom_autotrader.trading.steady_uptrend_vwap import timed_exit
from kiwoom_autotrader.trading.paper_experiment_s5 import (
    boundary as s5_boundary, evaluate_s5_native, build_s5_native_evidence, validate_s5_native_evidence, s5_signal_features,
    s5_observation_diagnostic)
from kiwoom_autotrader.trading.strategy_signal_log import record_signal_safely
from kiwoom_autotrader.utils.paths import app_data_root
from kiwoom_autotrader.trading import paper_experiment_extended_rules as extended
from kiwoom_autotrader.trading.paper_experiment_theme import (
    theme_context, bind_assignment_theme, signal_theme_features)
from kiwoom_autotrader.trading.paper_experiment_inputs import ExperimentInputs,build_admission,common_tick,number
from kiwoom_autotrader.trading.paper_experiment_qls_shadow import observe_qls_shadow
from kiwoom_autotrader.trading.paper_experiment_scheduler import ExtendedScheduler
from kiwoom_autotrader.trading.stability_metrics import TickHealth

OVERNIGHT_ARMS=frozenset({'EXP_D',*extended.OVERNIGHT})
AUCTION_EXIT_ARMS=frozenset({'EXP_ORB_A','EXP_ORB_B',*OVERNIGHT_ARMS})
EXIT_OWNERSHIP_STATES = ('owned', 'exit_only_broker_confirmed')
CANDIDATE_MAX_AGE = timedelta(seconds=60)
CLOSING_UNIVERSE_MAX_AGE = timedelta(minutes=6)


@dataclass(frozen=True)
class CandidateScope:
    epoch: int
    day: str
    start: datetime
    end: datetime
    calendar_sha256: str
    base: tuple
    dynamic_limit: int
    entry_limit: int


@dataclass(frozen=True)
class CandidateSnapshot:
    scope: CandidateScope
    available_at: datetime
    # Entries retain the discovery row's source timestamp and earliest expiry.
    rows: tuple
    sha256: str


def _candidate_time(value):
    at = datetime.fromisoformat(value)
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('candidate_time_unverified')
    return at.astimezone(KST)


def _launch_candidate_discovery(target):
    threading.Thread(target=target, name='paper-candidate-discovery', daemon=True).start()


def _opening_auction(now, calendar):
    start=calendar.get('start') if isinstance(calendar,dict) else None
    return (isinstance(start,datetime) and start.utcoffset()==timedelta(hours=9)
        and now.strftime('%H:%M')=='08:59' and start-timedelta(minutes=1)<=now<start)


def _owned_priority_phase(now, calendar):
    if calendar is not None and calendar['start']<=now<calendar['end']:
        return 'regular'
    if _opening_auction(now,calendar):
        return 'opening_auction'
    return None


def _digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _arm_name(strategy):
    names={'EXP_ORB_A':'ORB-a','EXP_ORB_B':'ORB-b','EXP_ETF_B':'ETF-B'}
    if strategy in names:return names[strategy]
    return strategy.removeprefix('EXP_').replace('B_TP','B-TP') if strategy.startswith('EXP_') else strategy[:2]


class PaperExperimentMonitor:
    def __init__(self, *, settings_loader=None, now=None, store_path=None,
                 chart_fetcher=None, provider_factory=None, account_fetcher=None,
                 submitter=None, calendar_reader=None, stop_requested=None, batch_size=4,extended_inputs_factory=None,
                 exit_provider_factory=None, policy_reloader=None, discovery_provider_factory=None,
                 discovery_launcher=None, discovery_timer_factory=None, qls_shadow_dir=None):
        self.settings = settings_loader or load_settings
        # Optional approved source hook. No implicit sidecar/config activation.
        self.policy_reloader = policy_reloader
        self.hot_policy_snapshot = None
        self.tick_health = TickHealth()
        self.now = now or (lambda: datetime.now(KST))
        self.path = Path(store_path) if store_path else app_data_root()/'telegram/paper-experiment.sqlite3'
        self.chart = chart_fetcher or fetch_session_minute_chart
        # Entry price and S5 inputs keep their own freshness gates. Rank calls
        # must never occur implicitly inside a foreground price lookup.
        self.provider_factory = provider_factory or PaperStrategyContextProvider
        self.discovery_provider_factory = discovery_provider_factory
        if self.discovery_provider_factory is None and provider_factory is None:
            self.discovery_provider_factory = lambda session: PaperStrategyContextProvider(
                session, enable_surge=True, enable_toss_hybrid=True, ranking_allowed=self._candidate_rank_allowed)
        self.discovery_launcher = discovery_launcher or _launch_candidate_discovery
        self.discovery_timer_factory = discovery_timer_factory or (threading.Timer if discovery_launcher is None else None)
        self.exit_provider_factory=exit_provider_factory or (provider_factory if provider_factory is not None else
            lambda session: PaperStrategyContextProvider(session,exit_only=True))
        self.exit_provider=None
        self.account = account_fetcher or fetch_account_report
        self.submit = submitter or submit_order
        self.calendar = calendar_reader or current_session
        self.stop = stop_requested or (lambda: False)
        self.batch_size = batch_size
        self.store = None
        self.provider = None
        self.identity = None
        self.next_at = None
        self._discovery_lock = threading.Lock()
        self._discovery_epoch = 0
        self._discovery_inflight = False
        self._discovery_query_epoch = None
        self._discovery_attempt = None
        self._discovery_provider = None
        self._discovery_session = None
        self._discovery_timer = None
        self._candidate_snapshot = None
        self._candidate_admission = None
        self._closing_universe = None
        self.scan = []
        self.seen = set()
        self.minute = None
        self.states = {}
        self.s5_evaluated = {}
        self.s5_observations = {}
        self.s5_observation_scope = {}
        self._s5_recent_bars = {}
        self._s5_observation_attempted_at = {}
        self._scan_attempted_at = {}
        self._s5_local_retry = {}
        self.quarantined = {}
        self.warnings = set()
        self.diagnostics = {}
        self.run_id = uuid.uuid4().hex
        self.source_hash = strategy_source_hash(__file__)
        self._status = {'enabled':False, 'orders_are_broker_confirmed_only':True}
        self.paused_arms={arm:'existing_multi_order_evidence_adapter_not_integrated' for arm in STRATEGIES}
        self.inputs_factory=extended_inputs_factory or (ExperimentInputs if provider_factory is None else None)
        self.inputs=None;self.extended_scheduler=None
        self.qls_shadow_dir=Path(qls_shadow_dir) if qls_shadow_dir is not None else None

    def seconds_until_next(self):
        if self.next_at is None:
            return None
        return max(0, (self.next_at-self.now()).total_seconds())

    def status(self):
        self._status['tick_health'] = self.tick_health.status()
        if isinstance(self._status.get('s5_integration'),dict):
            self._status['s5_integration']['evaluated_boundary_by_symbol']=dict(self.s5_evaluated)
            self._status['s5_integration']['observation_only_by_symbol']=dict(self.s5_observations)
            self._status['s5_integration']['observation_coverage']=dict(self.s5_observation_scope)
            self._status['s5_integration']['local_retries']=dict(self._s5_local_retry)
        return {**self._status, 'paused_arms':dict(self.paused_arms), 'quarantined':dict(self.quarantined),
                'extended':self.extended_scheduler.status() if self.extended_scheduler else {'status':'dependency_not_provided'},
                'symbol_diagnostics':dict(self.diagnostics),
                'next_check_at':self.next_at.isoformat() if self.next_at else None}

    def reset(self):
        close=getattr(self.provider,'close_prearmed',None)
        if callable(close):close()
        self.identity=None
        self._reset_candidate_discovery()
        self.exit_provider=None
        self.next_at=None
        self.states.clear()

    def _notify(self, symbol, reason, notify):
        key=(self.now().date().isoformat(),symbol,reason)
        if key not in self.warnings:
            self.warnings.add(key)
            notify(f'모의 실험 {symbol}: {reason}. 이 종목만 대사 후 재검사하며 불확실 주문은 재전송하지 않습니다.')

    def _position(self, tracker, symbol, arm, now):
        """A separate broker-confirmed exit path never grants entry ownership."""
        position = tracker.multi_strategy_position('paper', symbol, arm, now)
        if position.get('status') == 'unsupported':
            reader = getattr(tracker, 'paper_exit_only_position', None)
            if callable(reader):
                fallback = reader('paper', symbol, arm, now)
                if fallback.get('status') == 'exit_only_broker_confirmed':
                    return fallback
        return position

    def _protection_unavailable(self, symbol, reason, tracker, notify):
        # A missing quantity stays unknown; it is never derived from stale fills.
        quantity = None
        reader = getattr(tracker, 'paper_exit_only_position', None)
        if callable(reader):
            try:
                observed = reader('paper', symbol, S5, self.now())
                quantity = observed.get('broker_quantity')
            except Exception:
                pass
        if type(quantity) is not int or quantity < 0:
            quantity = None
        text = str(quantity) + '주' if quantity is not None else '미확정'
        self.diagnostics[symbol] = {'status': 'unsupported', 'reason': reason,
            'protection_enabled': False, 'broker_confirmed_quantity': quantity,
            'profit_attribution': 'unconfirmed', 'observed_at': self.now().isoformat()}
        self._notify(symbol, f'{reason}; 보호 청산이 꺼졌으니 수동 매도가 필요합니다. '
            f'브로커 확인 수량 {text}. sell protect {symbol} 명령은 최신 브로커 매도가능 수량만 시장가 청산합니다', notify)

    def _event(self, tracker, assignment, result, *, outcome=None, skip=None, receipt=None):
        at=self.now()
        self._record_theme(result,assignment['symbol'],at)
        event={'mode':'paper','strategy':assignment['arm'],'symbol':assignment['symbol'],
            'side':result.get('action') or 'buy','observed_at':at.isoformat(),
            'event_kind':'eligible_signal' if outcome is None else 'decision_outcome',
            'reason':result['reason'],'signal_at':result.get('signal_at'),
            'signal_key':result.get('signal_key'), 'arm':assignment['arm'],
            'available_at':at.isoformat(),'signal_price':result.get('signal_price'),
            'signal_price_basis':result.get('signal_price_basis'),
            'execution_status':result.get('execution_status'),
            'vi_observation':result.get('vi_observation'),
            'assignment':assignment,'features':result.get('features',{}),
            'outcome':outcome,'skip_reason':skip,'submitted':receipt is not None,
            'order_number':getattr(receipt,'order_number',None),'broker_fill':False,
            'ownership_status':result.get('ownership_status'),'profit_attribution':result.get('profit_attribution')}
        if assignment['arm']==S5 and outcome is not None:
            self.diagnostics.setdefault(assignment['symbol'],{}).update(
                order_outcome=outcome,order_skip_reason=skip,order_number=getattr(receipt,'order_number',None))
        return record_signal_safely(tracker,event)

    def _theme_context(self,symbol,now):
        profile=effective_paper_experiment(self.settings(),'paper')
        return theme_context(symbol,now,unclassified_policy=profile.get('unclassified_theme_policy','allow_unknown'))

    def _record_theme(self,result,symbol,now):
        if not isinstance(result.get('features'),dict):result['features']={}
        if 'theme_context' not in result['features']:
            result['features'].update(signal_theme_features(self._theme_context(symbol,now)))

    def _explain_s5_risk(self,diagnostic,symbol,now):
        if diagnostic.get('reason')!='dart_risk_review_unavailable':return diagnostic
        from .paper_experiment_disclosure import read_s5_risk_diagnostics
        path=getattr(self.provider,'_s5_risk_path',None)
        if path is None:return diagnostic
        detail=read_s5_risk_diagnostics(path,symbol,now)
        diagnostic.update(legacy_reason='dart_risk_review_unavailable',reason=detail['reason'],
            risk_diagnostic=detail,eligibility_unchanged=True)
        diagnostic.setdefault('failure_details',{}).setdefault('risk',{})['reason_codes']=detail.get('reasons') or [detail['reason']]
        return diagnostic

    def _preopen(self, session, settings, now, base):
        day=now.strftime('%Y%m%d')
        existing=self.store.selection(day,'preopen')
        if existing is None:
            existing=self.store.selection(day,'preopen',{'observed_at':now.isoformat(),
                'candidates':[{'symbol':s,'first_seen_at':now.isoformat()} for s in base]})
        # Prior close is recorded before the opening quote, never backdated.
        for row in existing['candidates'][:40]:
            actual,regular=self._fresh_session()
            if regular is None or actual>=regular['start'] or self.stop():return
            symbol=row['symbol']
            if self.store.selection(day,'prior:'+symbol) is not None:
                continue
            try:
                if self.inputs is not None:
                    daily=self.inputs.daily(symbol)
                    actual,regular=self._fresh_session()
                    if regular is None or actual>=regular['start']:return
                    if daily:
                        self.store.selection(day,'prior:'+symbol,{'symbol':symbol,'previous_close':daily['previous_close'],
                            'previous_session_day':daily['rows'][-1]['date'],'previous_close_available_at':daily['available_at'],
                            'source_sha256':daily['source_sha256']})
                    continue
                response=self.provider._fetch_s5_daily_page(symbol,day)
                received,regular=self._fresh_session()
                if regular is None or received>=regular['start'] or not response.get('ok'):
                    return
                body=response['body']
                if body.get('stk_cd')!=symbol:
                    raise ValueError('prior_symbol')
                rows=[r for r in body['stk_dt_pole_chart_qry'] if r.get('dt','')<day]
                prior=max(rows,key=lambda r:r['dt'])
                close=abs(int(prior['cur_prc']))
                if close<=0 or len(response.get('wire_sha256',''))!=64:
                    raise ValueError('prior_price')
                self.store.selection(day,'prior:'+symbol,{'symbol':symbol,'previous_close':close,
                    'previous_session_day':prior['dt'],'previous_close_available_at':received.isoformat(),
                    'source_sha256':response['wire_sha256']})
            except Exception:
                self.store.selection(day,'prior:'+symbol,{'symbol':symbol,'status':'unverified'})
            return  # Bounded one prior-day request per listener cycle.

    def _assignment(self, symbol, now, *, market=None):
        day=now.strftime('%Y%m%d');prior=self.store.assignment(day,symbol)
        if prior is not None:
            return prior
        if symbol in extended.ETF_SYMBOLS:
            value=extended.etf_assignment(day,symbol,now,now=now)
            return self.store.assign(bind_assignment_theme(value,self._theme_context(symbol,now)))
        pre=self.store.selection(day,'preopen') or {'candidates':[]}
        first=next((r['first_seen_at'] for r in pre['candidates'] if r['symbol']==symbol),now.isoformat())
        daily=self.store.selection(day,'prior:'+symbol)
        c=(now.strftime('%H:%M')=='09:00' and market is not None
            and datetime.fromisoformat(first).strftime('%H:%M')<'09:00'
            and daily and type(daily.get('previous_close')) is int
            and type(market.get('day_open')) is int
            and market['day_open']*100>=daily['previous_close']*103)
        if not c and now.strftime('%H:%M')<'09:06':
            return None  # Freeze C/GDR/ORB first; keep the original first_seen.
        seen=self.store.selection(day,'first_seen:'+symbol) or {'first_seen_at':first}
        profile=effective_paper_experiment(self.settings(),'paper').get('assignment_profile','native-five-v1')
        if c:
            value=assign_candidate(day,symbol,datetime.fromisoformat(seen['first_seen_at']),assigned_at=now,
                c_preopen=True,profile=profile)
            return self.store.assign(bind_assignment_theme(value,self._theme_context(symbol,now)))
        return self.store.assign_prospective(day,symbol,datetime.fromisoformat(seen['first_seen_at']),
            assigned_at=now,profile=profile,theme=self._theme_context(symbol,now))

    def _opening_context(self,symbol,market,now):
        prior=self.store.selection(now.strftime('%Y%m%d'),'prior:'+symbol) or {}
        return {**prior, 'symbol':symbol,'mode':'paper','exchange':'KRX',
                'observed_at':market['observed_at'],'request_started_at':market['request_started_at'],
                'source_sha256':market['source_sha256'],'day_open':market.get('day_open')}

    def _reset_candidate_discovery(self, *, reset_provider=True):
        with self._discovery_lock:
            self._discovery_epoch += 1
            self._candidate_snapshot = None
            if self._discovery_timer is not None:
                self._discovery_timer.cancel()
                self._discovery_timer = None
            if reset_provider:
                self._discovery_provider = None
                self._discovery_session = None
        self._candidate_admission = None
        self._closing_universe = None

    def _candidate_scope(self, base, now, calendar):
        regular = normalize_exit_session(calendar, now)
        policy = (self.hot_policy_snapshot or {}).get('changes', DEFAULT_CHANGES)
        return CandidateScope(self._discovery_epoch, regular['session_day'], regular['start'], regular['end'],
            regular['calendar_source_sha256'], tuple(base), policy['dynamic_candidate_limit'],
            policy['entry_pool_limit'])

    def _candidate_discovery_enabled(self):
        try:
            return not self.stop() and effective_paper_experiment(self.settings(), 'paper')['enabled']
        except Exception:
            return False

    def _candidate_rank_allowed(self):
        with self._discovery_lock:
            current = self._discovery_query_epoch == self._discovery_epoch
        return current and self._candidate_discovery_enabled()

    def _configured_candidate_base(self, settings):
        base = list(dict.fromkeys([r['code'] for r in settings.get('gdcrs_stocks', [])] +
            [r['code'] if isinstance(r, dict) else r for r in settings.get('paper_strategy_stocks', [])]))[:40]
        if self.inputs is not None:
            base = list(dict.fromkeys([*base[:38], *sorted(extended.ETF_SYMBOLS)]))[:40]
        return base

    @staticmethod
    def _candidate_window(now, start, end):
        return start + timedelta(minutes=1) <= now < end and now.strftime('%H:%M') < '15:15'

    @staticmethod
    def _freeze_candidates(found, scope, requested, received):
        if (found.get('status') != 'ready' or found.get('signal_eligible') is not False
                or not isinstance(found.get('candidates'), list) or len(found['candidates']) > scope.dynamic_limit):
            return None
        available = _candidate_time(found['available_at'])
        if not requested <= available <= received:
            return None
        frozen = []
        seen = set()
        for row in found['candidates']:
            symbol = row['symbol']
            observed = _candidate_time(row['available_at'])
            sources = row['source_hashes']
            if (not isinstance(symbol, str) or re.fullmatch('[0-9]{6}', symbol) is None
                    or symbol in seen or not scope.start <= observed <= available
                    or received - observed >= CANDIDATE_MAX_AGE or row.get('signal_eligible') is not False
                    or not isinstance(sources, dict) or not 1 <= len(sources) <= 8
                    or any(not isinstance(key, str) or not isinstance(value, str)
                           or re.fullmatch('[a-f0-9]{64}', value) is None for key, value in sources.items())):
                return None
            expires = observed + CANDIDATE_MAX_AGE
            membership = row.get('membership_evidence')
            if isinstance(membership, dict) and 'available_at' in membership:
                member_at = _candidate_time(membership['available_at'])
                if member_at.date() != available.date() or member_at > observed:
                    return None
                expires = min(expires, member_at + timedelta(minutes=5))
            toss = row.get('toss_evidence')
            if toss is not None:
                from .toss_candidate_feed import TTL_SECONDS
                expires = min(expires, _candidate_time(toss['feed_expires_at']),
                              _candidate_time(toss['risk_review']['expires_at']),
                              _candidate_time(toss['price_timestamp']) + timedelta(seconds=TTL_SECONDS))
            if received < expires:
                frozen.append((symbol, observed, expires, tuple(sorted(sources.items()))))
            seen.add(symbol)
        digest = _digest([scope.day, scope.calendar_sha256, available.isoformat(),
                          [(code, at.isoformat(), expiry.isoformat(), hashes)
                           for code, at, expiry, hashes in frozen]])
        return CandidateSnapshot(scope, available, tuple(frozen), digest)

    def _schedule_candidate_discovery(self, session, base, now, calendar, *, expected_epoch=None):
        """One low-priority shared-session rank observation. Never join it here."""
        if self.discovery_provider_factory is None or not self._candidate_discovery_enabled():
            return
        try:
            scope = self._candidate_scope(base, now, calendar)
        except (KeyError, TypeError, ValueError):
            return
        minute = now.replace(second=0, microsecond=0)
        if not self._candidate_window(now, scope.start, scope.end):
            return
        with self._discovery_lock:
            if (scope.epoch != self._discovery_epoch
                    or expected_epoch is not None and expected_epoch != scope.epoch
                    or self._discovery_inflight
                    or self._discovery_attempt is not None and minute <= self._discovery_attempt):
                return
            self._discovery_attempt = minute
            self._discovery_inflight = True
            self._discovery_query_epoch = scope.epoch
            self._discovery_session = session
            provider = self._discovery_provider
        identity = self.identity

        def collect():
            reader = provider
            snapshot = None
            try:
                requested = self.now()
                if (not self._candidate_rank_allowed() or session_identity(session) != identity
                        or not now <= requested < minute + timedelta(minutes=1)):
                    return
                if reader is None:
                    reader = self.discovery_provider_factory(session)
                if reader is self.provider or reader is self.exit_provider:
                    return
                found = reader.discover_dynamic_candidates(requested, limit=scope.dynamic_limit,
                                                           configured_symbols=scope.base)
                received, current_calendar = self._fresh_session()
                if (self._candidate_rank_allowed() and session_identity(session) == identity
                        and requested <= received < minute + timedelta(minutes=1)
                        and self._candidate_window(received, scope.start, scope.end)
                        and self._candidate_scope(scope.base, received, current_calendar) == scope):
                    snapshot = self._freeze_candidates(found, scope, requested, received)
            except Exception:
                pass  # A missing or invalid observation leaves only the configured base.
            finally:
                with self._discovery_lock:
                    if self._discovery_epoch == scope.epoch:
                        if reader is not self.provider and reader is not self.exit_provider:
                            self._discovery_provider = reader
                        if snapshot is not None:
                            self._candidate_snapshot = snapshot
                    self._discovery_inflight = False
                    self._discovery_query_epoch = None
                self._arm_candidate_refresh(session, scope, identity)

        try:
            self.discovery_launcher(collect)
        except Exception:
            with self._discovery_lock:
                self._discovery_inflight = False
                self._discovery_query_epoch = None

    def _arm_candidate_refresh(self, session, scope, identity):
        """An already armed lane stays fresh during a slow foreground cycle."""
        if self.discovery_timer_factory is None or not self._candidate_discovery_enabled():
            return
        now = self.now()
        due = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        if not self._candidate_window(due, scope.start, scope.end):
            return

        def wake():
            with self._discovery_lock:
                if self._discovery_timer is not timer:
                    return
                self._discovery_timer = None
                if self._discovery_epoch != scope.epoch:
                    return
            try:
                if self.stop() or session_identity(session) != identity:
                    return
                settings = self.settings()
                if (not effective_paper_experiment(settings, 'paper')['enabled']
                        or tuple(self._configured_candidate_base(settings)) != scope.base):
                    return
                at, calendar = self._fresh_session()
                if self._candidate_scope(scope.base, at, calendar) != scope:
                    return
                if at < due:
                    self._arm_candidate_refresh(session, scope, identity)
                else:
                    self._schedule_candidate_discovery(session, scope.base, at, calendar,
                                                       expected_epoch=scope.epoch)
            except Exception:
                return

        timer = self.discovery_timer_factory((due - now).total_seconds(), wake)
        timer.daemon = True
        with self._discovery_lock:
            if self._discovery_epoch != scope.epoch or self._discovery_timer is not None:
                return
            self._discovery_timer = timer
        try:
            timer.start()
        except Exception:
            with self._discovery_lock:
                if self._discovery_timer is timer:
                    self._discovery_timer = None

    def _candidate_pool(self, base, now):
        """Admit a completed immutable discovery generation without live rank I/O."""
        calendar = self.calendar(now)
        policy = (self.hot_policy_snapshot or {}).get('changes', DEFAULT_CHANGES)
        dynamic_limit = policy['dynamic_candidate_limit']
        # A reduction cannot evict configured symbols or affect owned exits.
        entry_limit = max(len(base), policy['entry_pool_limit'])
        scope = None
        if calendar is not None:
            try:
                scope = self._candidate_scope(base, now, calendar)
            except (KeyError, TypeError, ValueError):
                pass
        with self._discovery_lock:
            snapshot = self._candidate_snapshot
            session = self._discovery_session
        try:
            session_matches = session is not None and session_identity(session) == self.identity
        except Exception:
            session_matches = False
        if scope is None or not self._candidate_window(now, scope.start, scope.end):
            pool = list(base)
            closing = self._closing_universe
            status = 'outside_discovery_window'
            snapshot_sha = snapshot_at = None
            if (closing is not None and snapshot is not None and snapshot.scope == scope
                    and not snapshot.rows and closing[1] <= snapshot.available_at <= now):
                closing = self._closing_universe = None
            # Closing selection uses the last already admitted universe, not
            # expired rank/quote inputs and not a new late discovery. The
            # actual EXP_D admission and order gates still require fresh data.
            if (session_matches and scope is not None and '15:15' <= now.strftime('%H:%M') < '15:20'
                    and now < scope.end and closing is not None and closing[0] == scope
                    and timedelta(0) <= now - closing[1] <= CLOSING_UNIVERSE_MAX_AGE):
                pool = list(closing[2])
                snapshot_sha, snapshot_at = closing[3], closing[1].isoformat()
                status = 'frozen_closing_universe'
            self._status.update(candidate_symbols=pool, entry_pool_limit=entry_limit,
                dynamic_queue_limit=dynamic_limit, dynamic_observed_count=len(pool) - len(base),
                candidate_snapshot_sha256=snapshot_sha, candidate_snapshot_at=snapshot_at,
                discovery_reason=status)
            if (scope is not None and scope.start <= now < scope.start + timedelta(minutes=1)
                    and now.strftime('%H:%M') >= '09:06'):
                # A delayed opening can already be inside the nominal cohort
                # time. Keep the configured admissions even with ranks off.
                self._record_candidate_admission(scope, now, base, [], pool, None)
            return pool
        if (not session_matches or snapshot is None or snapshot.scope != scope
                or not timedelta(0) <= now - snapshot.available_at < CANDIDATE_MAX_AGE):
            snapshot = None
        codes = [row[0] for row in snapshot.rows if row[1] <= now < row[2]] if snapshot else []
        pool=list(dict.fromkeys([*base,*codes]))[:entry_limit]
        snapshot_sha = snapshot.sha256 if snapshot else None
        if snapshot is not None:
            if codes:
                self._closing_universe = (scope, snapshot.available_at, tuple(pool), snapshot_sha)
            elif not snapshot.rows:
                self._closing_universe = None
        self._status.update(dynamic_observed_count=len(codes), dynamic_queue_limit=dynamic_limit,
            candidate_symbols=pool, entry_pool_limit=entry_limit, candidate_snapshot_sha256=snapshot_sha,
            candidate_snapshot_at=snapshot.available_at.isoformat() if snapshot else None,
            discovery_reason='snapshot_ready' if snapshot else 'candidate_snapshot_unavailable_base_preserved')
        self._record_candidate_admission(scope, now, base, codes, pool, snapshot_sha)
        return pool

    def _record_candidate_admission(self, scope, now, base, codes, pool, snapshot_sha):
        admission = (scope, now.replace(second=0, microsecond=0), snapshot_sha, tuple(codes))
        if admission == self._candidate_admission:
            return
        pre=self.store.selection(now.strftime('%Y%m%d'),'preopen') or {'candidates':[]}
        previous={r['symbol']:r['first_seen_at'] for r in pre['candidates']}
        universe_sha = _digest({'day':now.strftime('%Y%m%d'),'symbols':sorted(pool)})
        for symbol in list(dict.fromkeys([*base,*codes])):
            self.store.selection(now.strftime('%Y%m%d'),'discovered:'+symbol,
                {'symbol':symbol,'discovered_at':self.now().isoformat()})
        for symbol in pool:
            self.store.selection(now.strftime('%Y%m%d'),'first_seen:'+symbol,
                {'symbol':symbol,'first_seen_at':previous.get(symbol,self.now().isoformat()),
                 'selection_universe_sha256':universe_sha,
                 'selection_available_at':self.now().isoformat(),
                 'first_seen_source':'preopen_configured' if symbol in previous else 'live_candidate_discovery'})
        self._candidate_admission = admission

    def _evidence(self, assignment, result, bars, now, *, budget, position=None):
        config={'enabled':True,'experiment_arm':_arm_name(assignment['arm']),
            'experiment_assignment_sha256':assignment['assignment_sha256'],
            'experiment_first_seen_at':assignment['first_seen_at'],
            'experiment_signal_at':result.get('signal_at',now.isoformat()),
            'experiment_signal_sha256':_digest(result),
            'experiment_profile_version':'paper-experiment-v1',
            'reentry_cooldown_minutes':0,'max_daily_entries_per_symbol':0}
        if assignment['arm']==S5:config['experiment_assignment']=assignment
        if result.get('s5_native_proof') is not None:
            config['experiment_s5_signal']=result['s5_native_proof']
            config['experiment_assignment']=assignment
        if result.get('extended_admission') is not None:config['experiment_extended_admission']=result['extended_admission']
        if result.get('order_window_end') is not None:config['experiment_order_window_end_at']=result['order_window_end']
        if type(result.get('signal_price')) is int and result['signal_price']>0:
            config['experiment_signal_price']=result['signal_price']
            config['experiment_signal_price_basis']=result.get('signal_price_basis') or 'observed_current_price'
        selection=assignment if assignment.get('selection_universe_sha256') else (
            self.store.selection(assignment['day'],'first_seen:'+assignment['symbol']) if self.store is not None else {}) or {}
        if selection.get('selection_universe_sha256'):
            config['experiment_selection_universe_sha256']=selection['selection_universe_sha256']
            config['experiment_selection_available_at']=selection['selection_available_at']
        if result.get('action')=='sell':
            config.update(multi_exit_kind='full',multi_position_generation_id=position['generation_id'])
            config['experiment_exit_reason']=result['reason']
            if position.get('status') == 'exit_only_broker_confirmed':
                config.update(ownership_status='exit_only_broker_confirmed', profit_attribution='unconfirmed')
            if assignment['arm']==S5:
                config['experiment_s5_exit']={'first_fill_at':position.get('first_fill_at'),
                    'reason':result['reason'],'observed_at':now.isoformat()}
            if assignment['arm']=='EXP_D':
                config['experiment_assignment']=assignment
                config['experiment_d_exit']=result['experiment_d_exit']
            if result.get('experiment_extended_late_exit') is not None:
                config['experiment_assignment']=assignment
                config['experiment_extended_late_exit']=result['experiment_extended_late_exit']
        evidence={'schema_version':1,'strategy':assignment['arm'],'strategy_version':'paper-experiment-v1',
            'strategy_source_sha256':self.source_hash,'run_id':self.run_id,'config':config,
            'observed_at':now.isoformat(),'evaluated_at':now.isoformat(),'budget':max(1,budget),
            'source_kind':'broker_observation','source_provider':'kiwoom','source_exchange':'KRX'}
        if bars:
            evidence.update(source_api='ka10080',interval_seconds=60,
                candle_start=bars[-1].start.isoformat(),
                candle_end=(bars[-1].start+timedelta(minutes=1)).isoformat(),
                bars=[_serialize_bar(b) for b in bars[-(21 if assignment['arm']==S5 else 61):]])
        else:
            evidence.update(source_api='ka10001',source_sha256=result.get('quote_sha256'))
        return evidence

    def _d_exit_result(self, assignment, position, now):
        """Only original, fully timed ownership may enter this recovery path."""
        if position.get('first_fill_timing_verified') is not True:
            raise ValueError('first_fill_timing_unverified')
        fill=position.get('first_fill_at')
        if not isinstance(fill,str):raise ValueError('first_fill_timing_unverified')
        regular=normalize_exit_session(self.calendar(now),now)
        pos={'arm':'EXP_D','symbol':assignment['symbol'],'quantity':position['owned_qty'],
            'entry_price':int(Decimal(position['basis_krw'])/position['owned_qty']),
            'entry_basis_krw':position['basis_krw'],'entry_at':fill}
        result=evaluate_exit('EXP_D',[],symbol=assignment['symbol'],now=now,position=pos,
            official_session=regular,next_session_day=regular['session_day'])
        if result['signal']:
            result['experiment_d_exit']={'schema':'exp-d-owned-exit-v1','kind':result['reason'],
                'generation_id':position['generation_id'],'first_fill_at':fill,
                'signal_at':result['signal_at'],'session_day':regular['session_day'],
                'regular_start':regular['start'].isoformat(),'regular_end':regular['end'].isoformat(),
                'calendar_source_sha256':regular['calendar_source_sha256'],
                'observed_session_open_at':regular['start'].isoformat()}
        return result

    def _extended_late_exit_result(self, assignment, position, now):
        if position.get('first_fill_timing_verified') is not True or not isinstance(position.get('first_fill_at'),str):
            raise ValueError('first_fill_timing_unverified')
        regular=normalize_exit_session(self.calendar(now),now)
        fill=position['first_fill_at']
        pos={'arm':assignment['arm'],'symbol':assignment['symbol'],'quantity':position['owned_qty'],
            'entry_price':int(Decimal(position['basis_krw'])/position['owned_qty']),
            'entry_basis_krw':position['basis_krw'],'entry_at':fill}
        result=extended.evaluate_exit(assignment['arm'],[],symbol=assignment['symbol'],now=now,
            position=pos,selection=assignment,official_session=regular)
        if result['signal']:
            result['experiment_extended_late_exit']={'schema':'extended-overnight-owned-exit-v1',
                'generation_id':position['generation_id'],'first_fill_at':fill,'signal_at':result['signal_at'],
                'session_day':regular['session_day'],'regular_start':regular['start'].isoformat(),
                'regular_end':regular['end'].isoformat(),'calendar_source_sha256':regular['calendar_source_sha256']}
        return result

    def _owned_exit_matches(self, tracker, assignment, position, now):
        current=self._position(tracker,assignment['symbol'],assignment['arm'],now)
        return (current.get('status') in EXIT_OWNERSHIP_STATES
            and current.get('first_fill_timing_verified') is True
            and all(current.get(k)==position.get(k) for k in
                ('generation_id','owned_qty','basis_krw','first_fill_at'))
            and self._generation_matches(tracker,assignment['symbol'],assignment,position['generation_id']))

    def _extended_late_exit_quote(self, session, symbol, identity):
        # Only this held exit uses the time range from the official session.
        # It needs a quote for the decision audit, without VI, ranks or daily inputs.
        started=self.now()
        regular=normalize_exit_session(self.calendar(started),started)
        if identity[0]!='paper' or not regular['start']<=started<regular['end']:
            raise ValueError('extended_late_exit_quote_session_unverified')
        raw=fetch_stock_info(session,symbol)
        observed=self.now()
        if (session_identity(session)!=identity or raw.get('stk_cd')!=symbol
                or not started<=observed<regular['end'] or observed-started>timedelta(seconds=5)):
            raise ValueError('extended_late_exit_quote_unverified')
        price=number(raw.get('cur_prc'),positive=True)
        return {'current_price':price,'source_sha256':_digest({'stk_cd':symbol,'cur_prc':raw['cur_prc']})}

    def _dispatch(self, session, tracker, assignment, result, bars, market, position, settings):
        decision_policy=self.hot_policy_snapshot
        symbol=assignment['symbol'];side=result['action'];now=self.now()
        d_exit=assignment['arm']=='EXP_D' and side=='sell'
        extended_late=(assignment['arm'] in extended.OPENING_EXIT_ARMS and side=='sell'
            and result['reason']=='late_recovery_exit')
        timed_exit=d_exit or extended_late
        proof_field='experiment_d_exit' if d_exit else 'experiment_extended_late_exit'
        recovery_identity=session_identity(session) if extended_late else None
        if assignment['arm'] in self.paused_arms and not (assignment['arm']==S5 and side=='sell'):
            self._event(tracker,assignment,result,outcome='not_submitted',skip=self.paused_arms[assignment['arm']])
            return
        if side == 'sell' and position.get('status') == 'exit_only_broker_confirmed':
            result.update(ownership_status='exit_only_broker_confirmed', profit_attribution='unconfirmed')
        if timed_exit:
            status=self.store.exp_d_exit_status if d_exit else self.store.extended_exit_status
            attempt=status(assignment,position.get('generation_id'),
                now=now,entry_at=position.get('first_fill_at'))
            key=attempt['key']
            if attempt['status']!='available':
                result['dispatch_status']=attempt['reason'];return
        else:
            self._event(tracker,assignment,result)
            key=_digest([assignment['assignment_sha256'],side,result.get('dedup_at',result.get('signal_at')),
                         result['reason'],position.get('generation_id') if side=='sell' else None])
            if self.store.attempted(key):return
        if position['status'] not in (('flat','flat_candidate') if side=='buy' else EXIT_OWNERSHIP_STATES):
            self._event(tracker,assignment,result,outcome='not_submitted',skip='position_'+position['status'])
            return
        if extended_late and not self._owned_exit_matches(tracker,assignment,position,now):
            result['dispatch_status']='extended_late_exit_ownership_changed'
            return
        if symbol in self.quarantined:
            # Only a positive fresh ledger ownership/pending recheck may clear it.
            if position['status'] in ('flat', *EXIT_OWNERSHIP_STATES):
                self.quarantined.pop(symbol,None)
            else:
                self._event(tracker,assignment,result,outcome='not_submitted',skip='await_reconciliation')
                return
        quantity=position.get('owned_qty') if side=='sell' else None
        price=None;budget=1
        try:
            if side=='sell' and (not bars or assignment['arm']==S5):
                current=(self._extended_late_exit_quote(session,symbol,recovery_identity) if extended_late else
                    self._s5_exit_quote(session,symbol) if assignment['arm']==S5 else
                    self.inputs.market(symbol,force=True) if self.inputs is not None else self.provider.fetch(symbol,self.now()))
                result['quote_sha256']=current['source_sha256']
                result['signal_price']=current['current_price']
                result['signal_price_basis']='current_quoted_price'
            if side=='buy':
                report=self.account(session)
                cash=int(report['cash']['ord_alow_amt'])
                tracker._holdings('paper',report,self.now(),source_kind='broker_observation')
                tracker._save_paper_balance_snapshot('paper',report,self.now())
                current=self.inputs.market(symbol,force=True) if self.inputs is not None else self.provider.fetch(symbol,self.now())
                result['quote_sha256']=current['source_sha256']
                auction=result.get('auction_intent') or assignment['arm']=='EXP_D' and self.now().strftime('%H:%M')>='15:20'
                if auction:
                    price=current.get('indicative_price') or current['current_price']
                    if type(price) is not int or price<=0 or price%common_tick(price):raise ValueError('auction_limit_price_unverified')
                else:price=fetch_budget_limit(session,symbol,current['current_price'])
                if assignment['arm']==S5:
                    risk_inputs=self.provider.fetch_s5_inputs(symbol,self.now())
                    current=self.provider.fetch(symbol,self.now())
                    current.update(risk_inputs)
                    proof=build_s5_native_evidence(bars,current,symbol,self.now())
                    if proof['signal_at']!=result['signal_at']:raise ValueError('s5_boundary_changed')
                    result['s5_native_proof']=proof
                    result['signal_price']=current['current_price']
                    result['signal_price_basis']='current_quoted_price'
                    result['quote_sha256']=current['source_sha256']
                if result.get('signal_price') is None:
                    result['signal_price']=current.get('indicative_price') or current['current_price']
                    result['signal_price_basis']='observed_auction_indicative_price' if auction else 'observed_current_price'
                sizing=experiment_order_sizing(settings,'paper',available_cash_krw=cash,price=price,
                    existing_principal_krw=0,pending_principal_krw=0)
                quantity=sizing['quantity'];budget=max(sizing['budget_krw'],quantity*price)
                if quantity<=0:
                    self._event(tracker,assignment,result,outcome='not_submitted',skip='insufficient_cash')
                    return
                if assignment['arm'] in extended.ARMS-{'EXP_ETF_B'}:
                    if self.inputs is None:raise ValueError('extended_inputs_unavailable')
                    current=self.inputs.market(symbol,force=True)
                    result['extended_admission']=build_admission(symbol,assignment['arm'],current,self.inputs.daily(symbol),self.now())
        except Exception as exc:
            self._event(tracker,assignment,result,outcome='not_submitted',skip=getattr(exc,'reason_code',None) or type(exc).__name__)
            raise
        # Quote/ownership work may cross the opening minute. No claim or order
        # exists yet: re-evaluate at the real current time before freezing it.
        now=self.now()
        if timed_exit:
            if not self._owned_exit_matches(tracker,assignment,position,now):
                result['dispatch_status']='exp_d_exit_ownership_changed' if d_exit else 'extended_late_exit_ownership_changed'
                return
            now=self.now()
            if extended_late and session_identity(session)!=recovery_identity:
                raise ValueError('paper_session_changed')
            reevaluate=self._d_exit_result if d_exit else self._extended_late_exit_result
            refreshed=reevaluate(assignment,position,now)
            if not refreshed['signal']:return
            result.update(refreshed)
            self._event(tracker,assignment,result)
            claim=self.store.claim_exp_d_exit if d_exit else self.store.claim_extended_exit
            claimed=claim(assignment,position['generation_id'],now=now,entry_at=position['first_fill_at'],payload=result)
        else:
            claimed=self.store.claim(key,day=now.strftime('%Y%m%d'),symbol=symbol,arm=assignment['arm'],
                side=side,now=now,payload=result)
        if not claimed:
            return
        identity=recovery_identity if extended_late else session_identity(session)
        def guard(*,check_ownership=False):
            at=self.now()
            if side=='buy' and self.policy_reloader is not None:
                self.policy_reloader.assert_entry_current(decision_policy,at,assignment['arm'])
            if self.stop() or session_identity(session)!=identity or identity[0]!='paper':
                raise ValueError('paper_session_changed')
            if effective_paper_experiment(self.settings(),'paper')!=effective_paper_experiment(settings,'paper'):
                raise ValueError('experiment_profile_changed')
            cal=self.calendar(at)
            preauction=(assignment['arm'] in ('EXP_GDR','EXP_LDR') and at.strftime('%H:%M')=='08:59')
            if cal is None or not (cal['start']<=at<cal['end'] or preauction and cal['start']-timedelta(minutes=1)<=at<cal['start']):
                raise ValueError('official_session_unavailable')
            end=result.get('order_window_end')
            if end and at>=datetime.fromisoformat(end):
                raise ValueError('signal_window_expired')
            if timed_exit:
                regular=normalize_exit_session(cal,at);proof=result[proof_field]
                failure='exp_d_exit' if d_exit else 'extended_late_exit'
                if (regular['session_day']!=proof['session_day'] or any(
                        regular[k].isoformat()!=proof['regular_'+k] for k in ('start','end'))
                        or regular['calendar_source_sha256']!=proof['calendar_source_sha256']):
                    raise ValueError(failure+'_session_changed')
                if not self._generation_matches(tracker,symbol,assignment,position['generation_id']):
                    raise ValueError(failure+'_ownership_changed')
                if check_ownership:
                    if not self._owned_exit_matches(tracker,assignment,position,at):
                        raise ValueError(failure+'_ownership_changed')
                # After submit_order prepares its own sell intent, an ordinary
                # position read correctly reports pending. The transport's
                # assert_multi_exit_dispatch excludes only that exact intent
                # and rechecks current qty/generation/00 immediately to send.
            if side=='buy' and assignment['arm'] not in OVERNIGHT_ARMS and at.strftime('%H:%M')>='15:15':
                raise ValueError('intraday_entry_cutoff')
            if side=='buy' and assignment['arm']==S5:
                validate_s5_native_evidence(result.get('s5_native_proof'),symbol,at,result.get('signal_at'))
                if settings.get('paper_fast_decision_enabled') is True:
                    generation=getattr(self.provider,'s5_risk_generation',None)
                    expected=result['s5_native_proof']['risk_context']['file_sha256']
                    if not callable(generation) or generation()!=expected:
                        raise ValueError('s5_risk_evidence_changed')
            if side=='buy' and assignment['arm'] in extended.ARMS-{'EXP_ETF_B'}:
                from .paper_experiment_admission import validate_admission
                validate_admission(result.get('extended_admission'),symbol,assignment['arm'],at)
        try:
            guard(check_ownership=True)
            if side=='sell' and position.get('status')=='exit_only_broker_confirmed':
                current=tracker.paper_exit_only_position('paper',symbol,assignment['arm'],self.now())
                if (current.get('status')!='exit_only_broker_confirmed' or any(current.get(k)!=position.get(k)
                        for k in ('generation_id','owned_qty','basis_krw'))):
                    raise ValueError('exit_only_broker_evidence_changed')
            evidence=self._evidence(assignment,result,bars,self.now(),budget=budget,position=position)
            with automated_orders(tracker,assignment['arm'],evidence=evidence):
                receipt=self.submit(session,side,symbol,quantity,price,before_send=guard)
            self.store.outcome(key,self.now(),{'status':'accepted','order_number':receipt.order_number})
            self._event(tracker,assignment,result,outcome='accepted',receipt=receipt)
        except Exception as exc:
            uncertain=bool(getattr(exc,'uncertain',False))
            reason=getattr(exc,'reason_code',None) or type(exc).__name__
            self.store.outcome(key,self.now(),{'status':'uncertain' if uncertain else 'not_submitted','reason':reason})
            self._event(tracker,assignment,result,outcome='not_submitted',skip=reason)
            if uncertain:
                self.quarantined[symbol]='uncertain_order'
            raise

    def _generation_matches(self, tracker, symbol, assignment, generation):
        """Bind the saved assignment to this actual accepted buy intent."""
        try:
            path=tracker.path
            if path.is_symlink() or not path.is_file():return False
            with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5) as db:
                db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
                row=db.execute("""SELECT i.*,d.evidence_json,d.evidence_sha256 FROM intents i
                    JOIN order_decisions d ON d.scope=i.scope AND d.intent_id=i.id
                    WHERE i.scope=? AND i.id=?""",(tracker.scope,generation)).fetchone()
                if row is None or (row['mode'],row['side'],row['symbol'],row['strategy'],row['day'],row['state'])!=(
                        'paper','buy',symbol,assignment['arm'],assignment['day'],'accepted') or row['order_number'] is None:
                    return False
                raw=row['evidence_json']
                if not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=row['evidence_sha256']:return False
                evidence=json.loads(raw);config=evidence.get('config')
                return (evidence.get('strategy')==assignment['arm'] and evidence.get('strategy_version')=='paper-experiment-v1'
                        and isinstance(config,dict) and config.get('experiment_assignment_sha256')==assignment['assignment_sha256'])
        except (AttributeError,OSError,sqlite3.Error,ValueError,TypeError,KeyError):
            return False

    def _owned_assignment(self, tracker, symbol, now):
        """Recover only a proven original lot and its saved assignment."""
        try:
            from .execution_log import EXPERIMENT_STRATEGIES
            arms=set(EXPERIMENT_STRATEGIES)|set(STRATEGIES)
            path=getattr(tracker,'path',None)
            if path is not None:
                if path.is_symlink() or not path.is_file():raise ValueError('ledger_unavailable')
                with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5) as db:
                    db.execute('PRAGMA query_only=ON')
                    found=db.execute("SELECT DISTINCT strategy FROM intents WHERE scope=? AND mode='paper' AND symbol=? AND side='buy' AND state='accepted' AND day<=?",
                                     (tracker.scope,symbol,now.strftime('%Y%m%d'))).fetchall()
                arms &= {row[0] for row in found}
            positions={arm:self._position(tracker,symbol,arm,now) for arm in sorted(arms)}
            days=set()
            for pos in positions.values():
                first=pos.get('first_fill_at')
                if pos.get('status') in EXIT_OWNERSHIP_STATES and pos.get('first_fill_timing_verified') and isinstance(first,str):
                    moment=datetime.fromisoformat(first)
                    if moment.tzinfo is not None and moment<=now:days.add(moment.strftime('%Y%m%d'))
            entries=self.store.recent_assignments(symbol,now.strftime('%Y%m%d'),days=days)
            matches=[]
            for assignment in entries:
                arm=assignment['arm']
                if arm not in positions:
                    positions[arm]=self._position(tracker,symbol,arm,now)
                position=positions[arm]
                if position.get('status') not in EXIT_OWNERSHIP_STATES:continue
                ident=position.get('generation_id');first=position.get('first_fill_at')
                if not isinstance(ident,str) or re.fullmatch(r'[0-9a-f]{32}',ident) is None:continue
                if position.get('first_fill_timing_verified') is True and isinstance(first,str):
                    filled=datetime.fromisoformat(first)
                    if (filled.tzinfo is None or filled>now or filled.strftime('%Y%m%d')!=assignment.get('day')
                            or datetime.fromisoformat(assignment['assigned_at'])>filled):continue
                elif position.get('status')!='exit_only_broker_confirmed' or assignment.get('day')!=now.strftime('%Y%m%d'):
                    continue  # No inferred fill time; current-day generation still must match saved order proof.
                validate_assignment(assignment,symbol=symbol,day=assignment['day'],now=now)
                if not self._generation_matches(tracker,symbol,assignment,ident):continue
                matches.append((assignment,ident))
            unique={(a['assignment_sha256'],ident) for a,ident in matches}
            if len(unique)==1:return matches[0][0],None
            if matches:return None,'ownership_ambiguous'
            status=tracker.multi_strategy_position('paper',symbol,'EXP_D',now).get('status')
            if status in ('unsupported','pending'):
                return None,'ownership_'+status
            return None,None
        except Exception:
            return None,'ownership_unavailable'

    def _s5_exit_quote(self,session,symbol):
        if self.exit_provider is None:self.exit_provider=self.exit_provider_factory(session)
        return self.exit_provider.fetch(symbol,self.now())

    def _remember_s5_chart(self,session,symbol):
        # Native strategies still perform their original fetch. Supplemental
        # diagnostics can reuse these exact immutable bars in the same boundary.
        bars=self.chart(session,symbol)
        now=self.now()
        self._s5_recent_bars={s:value for s,value in self._s5_recent_bars.items()
            if timedelta(0)<=now-value[0]<=timedelta(seconds=60)}
        self._s5_recent_bars[symbol]=(now,tuple(bars))
        return bars

    def _observe_s5_one(self, session, tracker, pool, profile):
        """At most one supplemental observation after the normal/exit batch.

        This path cannot assign, reserve, publish an eligible order signal or
        dispatch. The same existing session/provider supplies actual inputs.
        Durable markers are separate from native S5 evaluation/order markers.
        """
        now=self.now();day=now.strftime('%Y%m%d');boundary=s5_boundary(now).isoformat()
        if profile.get('s5_enabled') is not True or not '09:35'<=now.strftime('%H:%M')<'15:15':return
        reader=getattr(self.provider,'s5_eligible_symbols',None)
        if not callable(reader):return
        try:
            values=reader(now)
            if not isinstance(values,(tuple,list,set,frozenset)) or any(
                    not isinstance(s,str) or re.fullmatch('[0-9]{6}',s) is None for s in values):
                raise ValueError('risk_scope_unverified')
            symbols=sorted(set(values)&set(pool))[:40]
            self.s5_observations={s:d for s,d in self.s5_observations.items()
                if s in symbols and d.get('signal_at')==boundary}
            native=[s for s in symbols if self.s5_evaluated.get(s)==boundary]
            pending=[]
            for symbol in symbols:
                if symbol in native:continue
                key='s5_observation_only:'+symbol+':'+boundary
                old=self.store.selection(day,key)
                if old is not None:self.s5_observations[symbol]=old
                else:pending.append(symbol)
            # A slow scan may span five-minute boundaries. Preserve fairness
            # across those boundaries rather than always retrying the first code.
            pending.sort(key=lambda s:(self._s5_observation_attempted_at.get(s,''),s))
            self.s5_observation_scope={'risk_eligible_symbols':symbols,'boundary':boundary,
                'native_evaluated_symbols':native,'observation_attempted_symbols':sorted(self.s5_observations),
                'observation_evaluated_symbols':sorted(s for s,d in self.s5_observations.items() if d.get('status')!='UNOBSERVED'),
                'pending_symbols':pending,'actual_assignments_unchanged':True,
                'last_observation_attempt_at_by_symbol':{s:self._s5_observation_attempted_at.get(s) for s in symbols},
                'maximum_supplemental_symbols_per_cycle':1,'order_permission':False}
            if not pending or self.stop():return
            symbol=pending[0];assignment=self.store.assignment(day,symbol)
            self._s5_observation_attempted_at[symbol]=now.isoformat()
            ownership=None
            try:
                ownership=tracker.multi_strategy_position('paper',symbol,
                    (assignment or {}).get('arm',S5),now).get('status')
            except Exception:ownership='unavailable'
            try:
                inputs=self.provider.fetch_s5_inputs(symbol,self.now())
                cached=self._s5_recent_bars.get(symbol)
                if (cached is not None and s5_boundary(cached[0]).isoformat()==boundary
                        and timedelta(0)<=self.now()-cached[0]<=timedelta(seconds=60)):
                    bars=list(cached[1])
                else:bars=self._remember_s5_chart(session,symbol)
                minute=self.now().replace(second=0,microsecond=0)
                if any(b.start>minute for b in bars):raise ValueError('s5_future_session_bar')
                bars=[b for b in bars if b.start<minute]
                market=self.provider.fetch(symbol,self.now());market.update(inputs)
                actual_now=self.now()
                actual_boundary=s5_boundary(actual_now).isoformat()
                if actual_boundary!=boundary:
                    # Do not consume the new boundary using an older queued
                    # request or label observation time as signal time.
                    return
                diagnostic=s5_observation_diagnostic(bars,market,symbol,actual_now,
                    assignment=assignment,ownership_status=ownership)
                self._explain_s5_risk(diagnostic,symbol,actual_now)
            except Exception as error:
                text=str(error)
                diagnostic={'schema':'s5-assignment-independent-observation-v1','symbol':symbol,
                    'signal_at':boundary,'observed_at':self.now().isoformat(),
                    'evaluation_role':'observation_only','order_permission':False,'assignment_changed':False,
                    'actual_assignment_arm':(assignment or {}).get('arm'),
                    'actual_assignment_sha256':(assignment or {}).get('assignment_sha256'),
                    'ownership_status':ownership,'status':'UNOBSERVED','eligible':None,
                    'reason':text if re.fullmatch('[a-z0-9_]{1,100}',text) else type(error).__name__,
                    'failed_checks':[],'order_block_reason':'observation_only_no_order_authority'}
            key='s5_observation_only:'+symbol+':'+boundary
            self.s5_observations[symbol]=self.store.selection(day,key,diagnostic)
            self.s5_observation_scope['observation_attempted_symbols']=sorted(self.s5_observations)
            self.s5_observation_scope['observation_evaluated_symbols']=sorted(
                s for s,d in self.s5_observations.items() if d.get('status')!='UNOBSERVED')
            self.s5_observation_scope['pending_symbols']=pending[1:]
        except Exception:
            # A diagnostic-only problem must not interrupt the actual strategy
            # batch or owned-position protection and grants no fallback order.
            self.s5_observation_scope={'status':'unavailable','reason':'s5_observation_unavailable',
                'boundary':boundary,'order_permission':False,'actual_assignments_unchanged':True}

    def _schedule_s5_local_retry(self,symbol,assignment):
        """Retry only an unevaluated latest boundary, never an old buy signal."""
        now=self.now();boundary=s5_boundary(now).isoformat()
        if (not assignment or assignment.get('arm')!=S5 or S5 in self.paused_arms
                or not '09:35'<=now.strftime('%H:%M')<'15:15'
                or self.s5_evaluated.get(symbol)==boundary):
            self._s5_local_retry.pop(symbol,None);return
        detail=self.diagnostics.get(symbol,{})
        reason=detail.get('reason','')
        if not (str(reason).startswith('ownership_') or detail.get('status')=='retry_next_cycle'):
            self._s5_local_retry.pop(symbol,None);return
        previous=self._s5_local_retry.get(symbol,{})
        attempts=previous.get('attempts',0) if previous.get('boundary')==boundary else 0
        self._s5_local_retry[symbol]={'boundary':boundary,'attempts':attempts,'max_attempts':12,
            'due_at':(now+timedelta(seconds=10)).isoformat() if attempts<12 else None,
            'reason':reason,'observation_only_until_ownership_revalidated':True}

    def _run_scan_symbol(self,session,tracker,settings,symbol,assignment,notify,*,local_retry=False):
        self.seen.add(symbol);self._scan_attempted_at[symbol]=self.now().isoformat()
        if local_retry:
            state=self._s5_local_retry[symbol]
            state['attempts']+=1;state['due_at']=None
        try:
            self._one(session,tracker,settings,symbol,self.now(),assignment)
            detail=self.diagnostics.get(symbol,{})
            if detail.get('time_protection_enabled') is False:
                self._notify(symbol,f"최초 체결시각이 미확정하여 시간 기반 보호 청산이 꺼졌으니 수동 매도가 필요합니다. 브로커 확인 {detail.get('broker_confirmed_quantity')}주; sell protect {symbol}",notify)
        except Exception as exc:
            reason=getattr(exc,'reason_code',None) or type(exc).__name__
            detail=str(exc) if isinstance(exc,ValueError) and re.fullmatch('[a-z0-9_]{1,100}',str(exc)) else reason
            self.diagnostics[symbol]={**self.diagnostics.get(symbol,{}),'status':'retry_next_cycle',
                'order_or_input_error':detail,'observed_at':self.now().isoformat()}
            self.diagnostics[symbol].setdefault('reason',detail)
            self._notify(symbol,reason,notify)
        saved=assignment or self.store.assignment(self.now().strftime('%Y%m%d'),symbol)
        self._schedule_s5_local_retry(symbol,saved)

    def _due_s5_local_retry(self,pool,processed):
        now=self.now();boundary=s5_boundary(now).isoformat();due=[]
        for symbol,state in list(self._s5_local_retry.items()):
            if (state.get('boundary')!=boundary or self.s5_evaluated.get(symbol)==boundary
                    or symbol not in pool):
                self._s5_local_retry.pop(symbol,None);continue
            if symbol not in processed and state.get('due_at') is not None:
                when=datetime.fromisoformat(state['due_at'])
                if when<=now:due.append((when,symbol))
        return min(due)[1] if due else None

    def _one_s5(self, session, tracker, settings, symbol, assignment, position):
        """Exactly one latest-boundary entry evaluation; owned exits stay urgent."""
        now=self.now();day=now.strftime('%Y%m%d');boundary=s5_boundary(now).isoformat()
        if position.get('status') not in ('flat','flat_candidate',*EXIT_OWNERSHIP_STATES):
            self.diagnostics[symbol]={'arm':S5,'status':position.get('status'),
                'reason':'ownership_'+str(position.get('status')),'failed_checks':[],
                'observed_at':now.isoformat()}
            return
        if position.get('status') in EXIT_OWNERSHIP_STATES:
            # A valid owned lot never waits for risk/DART/entry pattern refresh.
            bars=[];sma_status='not_needed'
            market=self._s5_exit_quote(session,symbol)
            action=exit_signal(S5,qty=position['owned_qty'],basis_krw=position['basis_krw'],
                price=market['current_price'],sma20=None,half_taken=position.get('half_take_done',False))
            if action is None:
                if position.get('status')=='exit_only_broker_confirmed' and self.now().strftime('%H:%M')>='15:19':
                    action=('s5_regular_cutoff_exit',position['owned_qty'])
                elif position.get('status')=='exit_only_broker_confirmed':
                    # The proof retains the independently observed first00 time.
                    # Passing a local view does not change ledger ownership.
                    action=timed_exit({**position,'status':'owned'},self.now())
                else:action=timed_exit(position,self.now())
            if action is None:
                from .multi_strategy import valid_sma_bars
                try:
                    bars=self._remember_s5_chart(session,symbol)
                    bars=[b for b in bars if b.start.date()==now.date() and b.start+timedelta(minutes=1)<=self.now()]
                    valid=valid_sma_bars(bars[-20:],symbol,self.now())
                    sma_status='verified' if valid else 'unavailable'
                except Exception:
                    bars=[];valid=False;sma_status='unavailable'
                if valid:
                    sma=sum(b.close for b in bars[-20:])/20
                    market=self._s5_exit_quote(session,symbol)
                    action=exit_signal(S5,qty=position['owned_qty'],basis_krw=position['basis_krw'],
                        price=market['current_price'],sma20=sma,half_taken=position.get('half_take_done',False))
            reason=action[0] if action else 'owned_protection_monitoring'
            self.diagnostics[symbol]={'arm':S5,'status':position['status'],'ownership_status':position.get('ownership_status',position['status']),'profit_attribution':position.get('profit_attribution'),'reason':reason,
                'failed_checks':[],'sma_status':sma_status,'observed_at':self.now().isoformat()}
            if action:
                result={'signal':True,'action':'sell','reason':reason,'signal_at':self.now().isoformat(),
                    'dedup_at':self.now().replace(second=0,microsecond=0).isoformat(),
                    'quote_sha256':market['source_sha256'],'signal_price':market['current_price'],
                    'order_window_end':now.replace(hour=15,minute=30,second=0,microsecond=0).isoformat()}
                self._dispatch(session,tracker,assignment,result,bars,market,position,settings)
            return
        marker='s5_evaluated:'+symbol+':'+boundary
        previous=self.store.selection(day,marker)
        if previous is not None:
            self.s5_evaluated[symbol]=boundary
            self.diagnostics[symbol]={**previous,'same_boundary_already_evaluated':True}
            return
        if not '09:35'<=now.strftime('%H:%M')<'15:15':
            self.diagnostics[symbol]={'arm':S5,'status':'waiting','reason':'s5_entry_window',
                'failed_checks':[],'signal_at':boundary,'observed_at':now.isoformat()}
            return
        # Slow daily inputs first; then actual session chart and a fresh quote.
        inputs=self.provider.fetch_s5_inputs(symbol,self.now())
        bars=self._remember_s5_chart(session,symbol)
        minute=self.now().replace(second=0,microsecond=0)
        if any(b.start>minute for b in bars):raise ValueError('s5_future_session_bar')
        bars=[b for b in bars if b.start<minute]  # Current unfinished minute is not a completed candle.
        market=self.provider.fetch(symbol,self.now());market.update(inputs)
        now=self.now();boundary=s5_boundary(now).isoformat()
        evaluation=evaluate_s5_native(bars,market,symbol,now)
        diagnostic={'arm':S5,'status':evaluation['status'],'reason':evaluation['reason'],
            'signal_at':evaluation.get('signal_at',boundary),'observed_at':now.isoformat(),
            'ownership_status':position['status'],'checks':evaluation.get('checks',{}),
            'failed_checks':evaluation.get('failed_checks',[]),'check_groups':evaluation.get('check_groups',{}),
            'failure_details':evaluation.get('failure_details',{}),
            'session_coverage':evaluation.get('session_coverage'),'vwap':evaluation.get('vwap'),
            'order_permission':False,'assignment_sha256':assignment['assignment_sha256']}
        self._explain_s5_risk(diagnostic,symbol,now)
        # Persist before order preparation; crashes cannot recreate this signal.
        marker='s5_evaluated:'+symbol+':'+boundary
        self.store.selection(day,marker,diagnostic)
        self.s5_evaluated[symbol]=boundary;self.diagnostics[symbol]=diagnostic
        if evaluation.get('eligible') is not True:return
        proof=build_s5_native_evidence(bars,market,symbol,now)
        result={'signal':True,'action':'buy','reason':'steady_uptrend_vwap',
            'signal_at':evaluation['signal_at'],'signal_key':_digest([assignment['assignment_sha256'],boundary]),
            'checks':evaluation['checks'],'s5_native_proof':proof,
            'signal_price':market['current_price'],'signal_price_basis':'current_quoted_price',
            'features':s5_signal_features(evaluation,market['current_price']),
            'order_window_end':(s5_boundary(now)+timedelta(minutes=5)).isoformat()}
        self._dispatch(session,tracker,assignment,result,bars,market,position,settings)

    def _record_evaluation(self,assignment,result,observed_at,*,role='entry'):
        """Immutable telemetry only; its write failure never changes orders.

        A receipt means the evaluator returned for this boundary. Input/window
        gates remain separate from technical evaluation, and exits never enter
        the entry denominator. Historical missing receipts stay unmeasured.
        """
        try:
            evaluated=result.get('evaluated_at') or observed_at.isoformat()
            at=datetime.fromisoformat(evaluated);boundary=at.replace(second=0,microsecond=0)
            reason=result.get('reason')
            if not isinstance(reason,str) or re.fullmatch('[a-zA-Z0-9_]{1,120}',reason) is None:
                reason='unclassified_evaluation_result'
            stage=('input_gate' if 'unverified' in reason or 'unavailable' in reason else
                'entry_window' if reason.startswith('outside_') or reason.endswith('_window_not_met') else 'technical')
            checks=result.get('checks') or {}
            checks={k:v for k,v in checks.items() if isinstance(k,str) and re.fullmatch('[A-Za-z0-9_]{1,100}',k)
                and type(v) is bool} if isinstance(checks,dict) else {}
            payload={'schema':'paper-arm-evaluation-receipt-v1','day':at.strftime('%Y%m%d'),
                'arm':assignment['arm'],'symbol':assignment['symbol'],'evaluation_role':role,
                'boundary_at':boundary.isoformat(),'evaluated_at':at.isoformat(),
                'observed_at':observed_at.isoformat(),'evaluation_stage':stage,'reason':reason,
                'signal':result.get('signal') is True,'checks':dict(list(checks.items())[:50]),
                'assignment_sha256':assignment['assignment_sha256'],'order_permission':False}
            payload['receipt_sha256']=_digest(payload)
            prefix='evaluated' if role=='entry' else 'evaluated_exit'
            self.store.selection(payload['day'],f"{prefix}:{assignment['arm']}:{assignment['symbol']}:{boundary.isoformat()}:{stage}",payload)
        except Exception as error:
            self._status['evaluation_receipt_error']={'reason':type(error).__name__,
                'observed_at':observed_at.isoformat(),'order_path_unchanged':True}

    def _one(self, session, tracker, settings, symbol, now, assignment=None):
        market=None
        if assignment is None and now.strftime('%H:%M')=='09:00':
            market=self.provider.fetch(symbol,now)
        assignment=assignment or self._assignment(symbol,self.now(),market=market)
        if assignment is None:
            self.diagnostics[symbol]={'status':'waiting','reason':'non_gap_cohort_pending_open_minute_end'}
            return
        arm=assignment['arm']
        position=self._position(tracker,symbol,arm,self.now())
        if self.policy_reloader is not None and position.get('status') not in EXIT_OWNERSHIP_STATES:
            try:self.policy_reloader.assert_entry_current(self.hot_policy_snapshot,self.now(),arm)
            except ValueError as error:
                self.diagnostics[symbol]={'arm':arm,'status':'paused','reason':str(error),
                    'policy_version':(self.hot_policy_snapshot or {}).get('version'),
                    'assignment_preserved':True,'observed_at':self.now().isoformat()}
                return
        if arm in self.paused_arms and not (arm==S5 and position.get('status') in EXIT_OWNERSHIP_STATES):
            self.diagnostics[symbol]={'arm':arm,'status':'paused','reason':self.paused_arms[arm]}
            return
        if arm==S5:
            return self._one_s5(session,tracker,settings,symbol,assignment,position)
        bars=[]
        fill=position.get('first_fill_at') or position.get('initial_fill_at') if position.get('status') in EXIT_OWNERSHIP_STATES else None
        if position.get('status')=='exit_only_broker_confirmed' and not position.get('first_fill_timing_verified'):
            self.diagnostics[symbol]={'arm':arm,'status':'exit_only_broker_confirmed',
                'ownership_status':'exit_only_broker_confirmed','profit_attribution':'unconfirmed',
                'reason':'first_fill_timing_unverified','time_protection_enabled':False,
                'broker_confirmed_quantity':position['owned_qty'],'observed_at':self.now().isoformat()}
            if arm not in OVERNIGHT_ARMS and '15:15'<=now.strftime('%H:%M')<'15:20':
                result={'signal':True,'action':'sell','reason':'intraday_forced_liquidation',
                    'signal_at':now.isoformat(),'dedup_at':now.replace(second=0,microsecond=0).isoformat(),
                    'order_window_end':now.replace(hour=15,minute=20,second=0,microsecond=0).isoformat()}
                self._dispatch(session,tracker,assignment,result,[],None,position,settings)
            return
        unexpected_overnight=(arm not in OVERNIGHT_ARMS and isinstance(fill,str) and datetime.fromisoformat(fill).date()<now.date())
        if (arm in extended.OPENING_EXIT_ARMS and isinstance(fill,str) and datetime.fromisoformat(fill).date()<now.date()
                and not _opening_auction(now,self.calendar(now))):
            try:
                result=self._extended_late_exit_result(assignment,position,self.now())
            except (KeyError,ValueError,TypeError) as error:
                self.diagnostics[symbol]={'arm':arm,'status':position['status'],'reason':str(error),
                    'observed_at':self.now().isoformat()}
                return
            self._record_evaluation(assignment,result,self.now(),role='exit')
            if result['signal']:
                self._dispatch(session,tracker,assignment,result,[],None,position,settings)
            self.diagnostics[symbol]={'arm':arm,'status':position['status'],'reason':result['reason'],
                'dispatch_status':result.get('dispatch_status'),'observed_at':self.now().isoformat()}
            return
        if arm in extended.ARMS and not unexpected_overnight:
            if self.extended_scheduler is None:
                self.diagnostics[symbol]={'arm':arm,'status':'waiting','reason':'extended_inputs_unavailable'};return
            need_bars=(arm.startswith('EXP_ORB_') or arm=='EXP_ETF_B') and now.strftime('%H:%M')<'15:20'
            need_bars|=arm=='EXP_GDR' and position.get('status') in EXIT_OWNERSHIP_STATES and now.strftime('%H:%M')>='15:20'
            if need_bars:
                bars=self._remember_s5_chart(session,symbol)
                # The chart transport includes the current unfinished candle.
                # Keep strict provenance validation, but pass completed rows as
                # the native path already does. Never manufacture a missing row.
                received=self.now()
                if any(b.start>received.replace(second=0,microsecond=0) for b in bars):
                    raise ValueError('extended_future_session_bar')
                bars=[b for b in bars if b.start.date()==received.date()
                    and b.start+timedelta(minutes=1)<=received]
            context=self.extended_scheduler.context(symbol,self.now(),bars=bars,
                market=position.get('status') not in EXIT_OWNERSHIP_STATES and arm!='EXP_ETF_B' or arm=='EXP_GDR')
            if position.get('status') in EXIT_OWNERSHIP_STATES:
                previous_update=self.store.position_updates(position['generation_id'])
                pos={'arm':arm,'symbol':symbol,'quantity':position['owned_qty'],
                    'entry_price':int(Decimal(position['basis_krw'])/position['owned_qty']),
                    'entry_basis_krw':int(Decimal(position['basis_krw'])),'entry_at':fill,
                    **previous_update}
                result=extended.evaluate_exit(arm,bars,symbol=symbol,now=self.now(),position=pos,selection=assignment,context=context)
                if arm.startswith('EXP_ORB_'):
                    vi=self.inputs.vi_state(symbol)
                    result['vi_observation']=vi
                    if result['signal'] and isinstance(vi,dict) and vi.get('vi_active') is True:
                        result['execution_status']='unfillable_observed'
                if result.get('position_update'):
                    update={**previous_update,**result['position_update'],'observed_at':self.now().isoformat()}
                    self.store.selection(now.strftime('%Y%m%d'),'position_update:'+position['generation_id']+':'+self.now().strftime('%H%M%S%f'),update)
            else:
                result=extended.evaluate_entry(arm,bars,symbol=symbol,now=self.now(),selection=assignment,context=context)
            self._record_evaluation(assignment,result,self.now(),
                role='exit' if position.get('status') in EXIT_OWNERSHIP_STATES else 'entry')
            self.diagnostics[symbol]={'arm':arm,'status':position['status'] if position['status'] in (*EXIT_OWNERSHIP_STATES,'pending','unsupported')
                else 'signal' if result['signal'] else 'waiting','reason':result['reason'],'detail':result.get('detail'),
                'checks':result.get('checks'),'observed_at':self.now().isoformat()}
            if result['signal']:
                if result.get('execution_status')=='unfillable_observed':
                    market_vi=context.get('market') or {}
                    if 'vi_observation' not in result:
                        result['vi_observation']={k:market_vi.get(k) for k in ('vi_active','vi_complete',
                            'vi_observed_at','vi_source_sha256','vi_last_trigger_at','vi_last_trigger_basis')}
                    vi=(result.get('vi_observation') or {}).get('vi_active') is True or (result.get('checks') or {}).get('vi_entry_executable') is False
                    self._event(tracker,assignment,result,outcome='not_submitted',skip='vi_active_unfillable' if vi else 'entry_not_executable')
                else:self._dispatch(session,tracker,assignment,result,bars,context.get('market'),position,settings)
            return
        if arm not in ('EXP_C','EXP_D') and now.strftime('%H:%M')<'15:15' and not unexpected_overnight:
            bars=self._remember_s5_chart(session,symbol)
            bars=[b for b in bars if b.start.date()==now.date() and b.start+timedelta(minutes=1)<=self.now()]
        now=self.now()
        if position.get('status') in EXIT_OWNERSHIP_STATES:
            if unexpected_overnight:
                result={'signal':True,'action':'sell','reason':'unexpected_overnight_protective_exit',
                        'signal_at':now.isoformat(),'dedup_at':now.replace(second=0,microsecond=0).isoformat(),
                        'order_window_end':now.replace(hour=15,minute=20,second=0,microsecond=0).isoformat()}
                if now.strftime('%H:%M')<'15:20':self._dispatch(session,tracker,assignment,result,[],None,position,settings)
                self.diagnostics[symbol]={'arm':arm,'status':'owned','reason':'unexpected_overnight_protective_exit',
                                          'observed_at':now.isoformat()}
                return
            pos={'arm':arm,'symbol':symbol,'quantity':position['owned_qty'],
                 'entry_price':int(Decimal(position['basis_krw'])/position['owned_qty']),
                 'entry_basis_krw':position['basis_krw'], 'entry_at':fill}
            if arm=='EXP_D':
                try:result=self._d_exit_result(assignment,position,now)
                except (KeyError,ValueError,TypeError) as error:
                    self.diagnostics[symbol]={'arm':arm,'status':position['status'],
                        'reason':str(error),'observed_at':self.now().isoformat()}
                    return
            else:
                result=evaluate_exit(arm,bars,symbol=symbol,now=now,position=pos,
                    next_session_day=now.strftime('%Y%m%d') if self.calendar(now) else None)
            if arm in STRATEGIES and not result['signal']:
                market=self.provider.fetch(symbol,now)
                check=exit_signal(arm,qty=position['owned_qty'],basis_krw=position['basis_krw'],
                    price=market['current_price'],sma20=sum(b.close for b in bars[-20:])/20 if len(bars)>=20 else None,
                    half_taken=position.get('half_take_done',False))
                age=(now-datetime.fromisoformat(fill)).total_seconds() if fill else None
                if arm==S5 and age is not None and age>=1800:check=('time_stop_30m',position['owned_qty'])
                if check:result.update(signal=True,action='sell',reason=check[0])
            self._record_evaluation(assignment,result,self.now(),role='exit')
            if result['signal']:
                result.setdefault('signal_at',now.replace(second=0,microsecond=0).isoformat())
                if result['reason']=='intraday_forced_liquidation':
                    result['order_window_end']=now.replace(hour=15,minute=20,second=0,microsecond=0).isoformat()
                elif result['reason']=='next_session_open' and arm!='EXP_D':
                    result['order_window_end']=now.replace(hour=9,minute=1,second=0,microsecond=0).isoformat()
                self._dispatch(session,tracker,assignment,result,bars,market,position,settings)
            reason=result['reason']
            if arm=='EXP_D' and not result['signal'] and isinstance(fill,str) and datetime.fromisoformat(fill).date()<now.date() and now.strftime('%H:%M')>='09:01':
                reason='d_morning_exit_overdue'
            self.diagnostics[symbol]={'arm':arm,'status':position['status'],'ownership_status':position.get('ownership_status',position['status']),'profit_attribution':position.get('profit_attribution'),'reason':reason,'dispatch_status':result.get('dispatch_status'),'observed_at':self.now().isoformat()}
            return
        external=None
        if arm in STRATEGIES:
            if market is None:market=self.provider.fetch(symbol,self.now())
            if arm==S5:market.update(self.provider.fetch_s5_inputs(symbol,self.now()))
            state=self.states.get(symbol,EntryState())
            reason=entry_signal(arm,state,bars,market,symbol=symbol,now=self.now())
            if bars:self.states[symbol]=next_entry_state(state,bars[-1],market)
            external={'arm':arm,'symbol':symbol,'eligible':bool(reason),
                'signal_at':self.now().replace(second=0,microsecond=0).isoformat(),'evidence_sha256':_digest(market)}
        opening=self._opening_context(symbol,market,now) if arm=='EXP_C' and market else None
        result=evaluate_entry(arm,bars,symbol=symbol,now=self.now(),assignment=assignment,
            opening_context=opening,external_signal=external)
        self._record_evaluation(assignment,result,self.now())
        if arm=='EXP_C' and result.get('signal') is True:
            from .paper_experiment_scheduler import record_c_signal
            record_c_signal(self.store,assignment,result,opening,self.now())
        if self.inputs is not None:
            daily=self.inputs.daily(symbol)
            if not isinstance(result.get('features'),dict):result['features']={}
            result['features'].update(avg_turnover20_krw=daily.get('avg_turnover20_krw') if daily else None,
                avg_turnover20_status='observed' if daily else 'unobserved',
                market_first30m_return=None,market_first30m_status='unobserved_not_independent_arm')
        self._record_theme(result,symbol,self.now())
        self.diagnostics[symbol]={'arm':arm,'reason':result['reason'],'checks':result.get('checks'),
            'status':position['status'] if position['status'] in ('unsupported','pending') else ('signal' if result['signal'] else 'waiting'),
            'ownership_status':position['status'],'observed_at':self.now().isoformat()}
        if result['signal']:
            self._dispatch(session,tracker,assignment,result,bars,market,position,settings)

    def tick(self, session, notify):
        measured = getattr(session, 'mode', None) == 'paper'
        if measured:self.tick_health.begin(self.now(), time.monotonic())
        success = False
        try:
            if measured and self.policy_reloader is not None:
                self.hot_policy_snapshot=self.policy_reloader.next_cycle(self.now())
                self._status['hot_policy']=self.hot_policy_snapshot
                if getattr(self,'_hot_policy_epoch',None)!=self.hot_policy_snapshot['policy_sha256']:
                    self._reset_candidate_discovery(reset_provider=False)
                self._hot_policy_epoch=self.hot_policy_snapshot['policy_sha256']
            result = self._tick(session,notify)
            success = True
            return result
        finally:
            if measured:self.tick_health.end(self.now(), time.monotonic(), success=success)
            checkpoint=getattr(self.extended_scheduler,'record_checkpoint',None)
            if callable(checkpoint) and getattr(session,'mode',None)=='paper':
                checkpoint(self.now(),symbol_diagnostics=self.diagnostics)

    def _fresh_session(self):
        """Read the clock after work, and reject cross-date calendar values."""
        now=self.now();cal=self.calendar(now)
        try:
            if (not isinstance(cal,dict) or cal['start'].utcoffset()!=timedelta(hours=9)
                    or cal['end'].utcoffset()!=timedelta(hours=9)
                    or not cal['start']<cal['end'] or cal['start'].date()!=now.date()
                    or cal['end'].date()!=now.date()
                    or cal.get('session_day',now.strftime('%Y%m%d'))!=now.strftime('%Y%m%d')):
                return now,None
        except (KeyError,TypeError,AttributeError):return now,None
        return now,cal

    def _owned_priority(self,session,tracker,settings,notify,processed,overrides,blocked,auction_processed):
        """All owned protection precedes discovery; the entry batch is unrelated.

        An unavailable owner blocks its own symbol. The opening auction has a
        separate pass, so a missed 08:59 cannot consume the first regular pass.
        """
        now,cal=self._fresh_session()
        phase=_owned_priority_phase(now,cal)
        if phase is None or self.stop():
            return now,cal,{'status':'not_regular','symbols':[]}
        current_pass=processed if phase=='regular' else auction_processed
        owned=tracker.multi_strategy_open_symbols('paper',now)
        symbols=owned.get('symbols') if isinstance(owned,dict) else None
        if (not isinstance(owned,dict) or owned.get('status')!='available'
                or not isinstance(symbols,list) or len(symbols)>1000
                or any(not isinstance(s,str) or re.fullmatch('[0-9]{6}',s) is None for s in symbols)):
            self._status['ownership_monitoring']='unavailable'
            return *self._fresh_session(),{'status':'unavailable','symbols':[]}
        for symbol in dict.fromkeys(symbols):
            now,cal=self._fresh_session()
            if self.stop() or _owned_priority_phase(now,cal)!=phase:break
            if symbol in current_pass:continue
            # The owner lookup may itself block. _one and the final queued
            # guard independently read current time/session/owned generation.
            try:
                assignment,problem=self._owned_assignment(tracker,symbol,now)
                current_pass.add(symbol)
                if assignment is None:
                    blocked.add(symbol)
                    self._protection_unavailable(symbol,problem or 'ownership_unavailable',tracker,notify)
                    continue
                if phase=='opening_auction' and assignment['arm'] not in extended.OPENING_EXIT_ARMS:
                    continue
                blocked.discard(symbol)
                overrides[symbol]=assignment
                now,cal=self._fresh_session()
                if _owned_priority_phase(now,cal)!=phase:break
                if assignment['arm'] not in OVERNIGHT_ARMS and assignment['day']<now.strftime('%Y%m%d'):
                    self._notify(symbol,'unexpected_overnight_position',notify)
                elif assignment['arm'] not in AUCTION_EXIT_ARMS and now.strftime('%H:%M')>='15:20':
                    self._notify(symbol,'intraday_position_not_closed_by_cutoff',notify)
                self._run_scan_symbol(session,tracker,settings,symbol,assignment,notify)
            except Exception as error:
                current_pass.add(symbol);blocked.add(symbol)
                self._notify(symbol,type(error).__name__,notify)
        self._status['owned_priority']={'processed_symbols':sorted(processed),
            'blocked_symbols':sorted(blocked),'entry_batch_size':self.batch_size,
            'observed_at':self.now().isoformat()}
        return *self._fresh_session(),owned

    def _tick(self, session, notify):
        # In real mode even experiment files/settings are untouched.
        if getattr(session,'mode',None)!='paper':
            self._reset_candidate_discovery()
            self._status={'enabled':False,'reason':'paper_only'}
            return
        settings=self.settings();profile=effective_paper_experiment(settings,'paper')
        if not profile.get('enabled') or self.stop():
            self._reset_candidate_discovery()
            self._status={'enabled':False,'reason':'disabled'}
            return
        now=self.now();self._status.update(enabled=True,profile=profile)
        if profile.get('s5_enabled') is True:self.paused_arms.pop(S5,None)
        else:self.paused_arms[S5]='native_s5_disabled'
        self._status['active_arms']=[arm for arm in ('EXP_R','EXP_A','EXP_B','EXP_B_TP','EXP_E','EXP_C','EXP_D',
            *sorted(extended.ARMS),S5) if arm not in self.paused_arms]
        self._status['s5_integration']={'enabled':profile.get('s5_enabled') is True,
            'status':'ACTIVE' if profile.get('s5_enabled') is True else 'PAUSED',
            'assignment_profile':profile.get('assignment_profile','native-five-v1'),
            'evaluated_boundary_by_symbol':dict(self.s5_evaluated),
            'real_order_path':'automated_orders/queued_open/atomic_ledger',
            'reserved_positions':profile.get('s5_reserved_positions',0)}
        identity=session_identity(session)
        if identity!=self.identity:
            self._reset_candidate_discovery()
            self.identity=identity;self.provider=self.provider_factory(session)
            self.exit_provider=self.exit_provider_factory(session)
            self.inputs=None;self.extended_scheduler=None
            self.states.clear();self.scan=[];self.seen=set();self.minute=None;self.s5_evaluated.clear()
            self.s5_observations.clear();self.s5_observation_scope.clear()
            self._s5_recent_bars.clear()
            self._s5_observation_attempted_at.clear();self._scan_attempted_at.clear();self._s5_local_retry.clear()
        if self.store is None:self.store=ExperimentStore(self.path)
        self._status['qls_shadow']=observe_qls_shadow(self.store,now,checkpoint_dir=self.qls_shadow_dir)
        if self.inputs is None and self.inputs_factory is not None:
            self.inputs=self.inputs_factory(session,self.provider,self.store,clock=self.now)
            if self.inputs is not None:self.extended_scheduler=ExtendedScheduler(self.inputs,self.store,self.chart,self.now,calendar_reader=self.calendar)
        tracker=getattr(getattr(session,'paper_automation_reconcile',None),'_tracker',None)
        if tracker is None:
            self._status['reason']='ledger_unavailable';return
        processed_owned=set();auction_processed=set();overrides={};blocked=set();owned={'status':'not_regular','symbols':[]}
        def checkpoint():
            return self._owned_priority(session,tracker,settings,notify,processed_owned,overrides,blocked,auction_processed)
        now,cal,owned=checkpoint()
        if cal is None:
            self._status['reason']='official_calendar_unverified';self.next_at=now+timedelta(seconds=10);return
        if now>=cal['end']:
            self._status['reason']='outside_session';self.next_at=now+timedelta(minutes=1);return
        base=self._configured_candidate_base(settings)
        preopen_input_work=False
        warm_selection=getattr(self.inputs,'prepare_selection_one',None)
        if callable(warm_selection):
            preopen_input_work=warm_selection(self._status.get('candidate_symbols',base)) is True
            now,cal,owned=checkpoint()
        prepare_preopen=getattr(self.inputs,'prepare_preopen_one',None)
        if callable(prepare_preopen) and '08:30'<=now.strftime('%H:%M')<='08:55':
            preopen_input_work=prepare_preopen(list(dict.fromkeys([*sorted(extended.ETF_SYMBOLS),*base]))) is True
            now,cal,owned=checkpoint()
        if cal is None:
            self._status['reason']='official_calendar_unverified';self.next_at=now+timedelta(seconds=10);return
        if self.inputs is not None and not preopen_input_work and '08:30'<=now.strftime('%H:%M')<'15:15':
            self.inputs.prepare_one(list(dict.fromkeys([*sorted(extended.ETF_SYMBOLS),*base])))
            preopen_input_work=True
            now,cal,owned=checkpoint()
        if cal is None:
            self._status['reason']='official_calendar_unverified';self.next_at=now+timedelta(seconds=10);return
        if now<cal['start']:
            if now.strftime('%H:%M')>='08:50':
                self._preopen(session,settings,now,base)
                now,cal,owned=checkpoint()
            if cal is None:
                self._status['reason']='official_calendar_unverified';self.next_at=now+timedelta(seconds=10);return
            if self.extended_scheduler is not None and now<cal['start'] and now.strftime('%H:%M')=='08:59':
                self.extended_scheduler.run_selection(session,base,tracker,now)
                now,cal,owned=checkpoint()
                if cal is None:
                    self._status['reason']='official_calendar_unverified';self.next_at=now+timedelta(seconds=10);return
                choices=self.extended_scheduler.overrides(now.strftime('%Y%m%d'))
                for symbol,a in choices.items():
                    if self.now()>=cal['start']:break
                    if symbol in auction_processed or symbol in overrides or symbol in blocked:continue
                    try:self._one(session,tracker,settings,symbol,self.now(),a)
                    except Exception as error:self._notify(symbol,type(error).__name__,notify)
            now,cal,owned=checkpoint()
            if cal is None:
                self._status['reason']='official_calendar_unverified';self.next_at=now+timedelta(seconds=10);return
            if now<cal['start']:
                self._status['reason']='preopen';self.next_at=now+timedelta(seconds=1);return
        if now>=cal['end']:
            self._status['reason']='outside_session';self.next_at=now+timedelta(minutes=1);return
        day=now.strftime('%Y%m%d');minute=now.replace(second=0,microsecond=0)
        pool=self._candidate_pool(base,now)
        now,cal,owned=checkpoint()
        if cal is not None:
            self._schedule_candidate_discovery(session,base,now,cal)
        if self.inputs is not None and not preopen_input_work:
            self.inputs.prepare_one(pool)
            now,cal,owned=checkpoint()
        if cal is None or not cal['start']<=now<cal['end']:
            self._status['reason']='official_calendar_unverified' if cal is None else 'outside_session'
            self.next_at=now+timedelta(seconds=10);return
        day=now.strftime('%Y%m%d');minute=now.replace(second=0,microsecond=0)
        urgent=list(owned.get('symbols',[])) if owned.get('status')=='available' else []
        if owned.get('status')!='available':
            self._status['ownership_monitoring']='unavailable'
        if self.extended_scheduler is not None:
            self.extended_scheduler.run_selection(session,pool,tracker,self.now())
            now,cal,owned=checkpoint()
            if cal is None or not cal['start']<=now<cal['end']:
                self._status['reason']='official_calendar_unverified' if cal is None else 'outside_session'
                self.next_at=now+timedelta(seconds=10);return
            for symbol,value in self.extended_scheduler.overrides(day).items():
                if symbol not in overrides and symbol not in blocked:overrides[symbol]=value
        if '09:06'<=now.strftime('%H:%M')<'15:15' and self.store.selection(day,'initial_intraday_cohort') is None:
            rows=[]
            for symbol in pool:
                if (symbol in overrides or symbol in blocked or symbol in extended.ETF_SYMBOLS
                        or self.store.assignment(day,symbol) is not None):
                    continue
                first_seen = self.store.selection(day,'first_seen:'+symbol)
                if first_seen is not None:
                    rows.append({'symbol':symbol,'first_seen_at':first_seen['first_seen_at']})
            if len(rows)>=2:
                cohort=assign_cohort(rows,day=day,snapshot_at=self.now(),profile=profile.get('assignment_profile','native-five-v1'))
                values=cohort['assignments'] if isinstance(cohort,dict) else cohort
                values=[bind_assignment_theme(value,self._theme_context(value['symbol'],
                    datetime.fromisoformat(value['assigned_at']))) for value in values]
                self.store.selection(day,'initial_intraday_cohort',{'observed_at':self.now().isoformat(),'assignments':values})
                for value in values:self.store.assign(value)
        now,cal,owned=checkpoint()
        if cal is None or not cal['start']<=now<cal['end']:
            self._status['reason']='official_calendar_unverified' if cal is None else 'outside_session'
            self.next_at=now+timedelta(seconds=10);return
        closing_selection=self.store.selection(day,'extended:closing')
        if (now.strftime('%H:%M')>='15:18' and now.strftime('%H:%M')<'15:20'
                and self.store.selection(day,'D') is None
                and (closing_selection is None or closing_selection.get('status')=='selection_window_expired')):
            candidates=[]
            for symbol in pool:
                if owned.get('status')!='available' or symbol in overrides or symbol in blocked:continue
                state=tracker.multi_strategy_position('paper',symbol,'EXP_D',now)
                if state.get('status')!='flat':continue
                saved=self._assignment(symbol,now)
                if saved is not None:candidates.append({'symbol':symbol,'first_seen_at':saved['first_seen_at']})
            now,cal,owned=checkpoint()
            # A blocking account lookup cannot turn a late selection into an
            # allegedly frozen pre-auction universe.
            if cal is not None and '15:18'<=now.strftime('%H:%M')<'15:20':
                self.store.selection(day,'D',select_overnight(candidates,day=day,snapshot_at=now))
        if now.strftime('%H:%M')>='15:20':
            selection=self.store.selection(day,'D') or {'assignments':[]}
            for assignment in selection['assignments']:
                symbol=assignment['symbol']
                if owned.get('status')=='available' and symbol not in overrides and symbol not in blocked:
                    state=tracker.multi_strategy_position('paper',symbol,'EXP_D',now)
                    if state.get('status')=='flat':overrides[symbol]=assignment
            pool=list(overrides)
        for selection in self.store.prior_overnight_selections(day):
            for assignment in selection['assignments']:
                symbol=assignment['symbol']
                if symbol in overrides or symbol in blocked:continue
                pos=self._position(tracker,symbol,'EXP_D',now)
                if pos.get('status') in ('pending','unsupported'):
                    blocked.add(symbol);self._protection_unavailable(symbol,'ownership_'+pos['status'],tracker,notify)
                elif pos.get('status') in EXIT_OWNERSHIP_STATES:
                    # _owned_assignment verifies assignment date, actual fill and generation.
                    assignment,problem=self._owned_assignment(tracker,symbol,now)
                    if assignment is None:
                        blocked.add(symbol);self._protection_unavailable(symbol,problem or 'ownership_unavailable',tracker,notify)
                    else:overrides[symbol]=assignment
                    if symbol not in pool:pool.insert(0,symbol)
        observation_pool=list(pool)
        pool=[symbol for symbol in pool if symbol not in blocked and symbol not in processed_owned]
        urgent=[symbol for symbol in urgent if symbol not in blocked and symbol not in processed_owned]
        if self.minute!=minute:
            self.minute=minute;self.seen=set(processed_owned)
            assigned_s5=[s for s in pool if (overrides.get(s) or self.store.assignment(day,s) or {}).get('arm')==S5
                and self.s5_evaluated.get(s)!=s5_boundary(now).isoformat()]
            remainder=[s for s in self.scan if s in pool or s in urgent]
            priority=lambda symbols:sorted(symbols,key=lambda s:(self._scan_attempted_at.get(s,''),s))
            # Carry the unfinished round over a minute boundary. Priority work
            # precedes it, but already-serviced ordinary symbols stay behind it.
            urgent_queue=priority(urgent)
            s5_queue=priority([s for s in assigned_s5 if s not in urgent])
            ordinary=list(dict.fromkeys(s for s in remainder+pool if s not in urgent and s not in s5_queue))
            # Repeated ownership holds must not use every ordinary slot. Owned
            # exits retain absolute priority; reserve one available batch place
            # for the oldest unfinished ordinary symbol when capacity remains.
            s5_quota=max(0,self.batch_size-len(urgent_queue)-1) if ordinary else len(s5_queue)
            self.scan=list(dict.fromkeys(urgent_queue+s5_queue[:s5_quota]+ordinary[:1]
                +s5_queue[s5_quota:]+ordinary[1:]))
        self.scan=[s for s in self.scan if s in pool or s in urgent]
        processed=set(processed_owned)
        for _ in range(self.batch_size):
            if not self.scan or self.stop():break
            now,cal,owned=checkpoint()
            if cal is None or not cal['start']<=now<cal['end']:
                self._status['reason']='official_calendar_unverified' if cal is None else 'outside_session'
                self.next_at=now+timedelta(seconds=10);return
            self.scan=[s for s in self.scan if s not in processed_owned and s not in blocked]
            if not self.scan:break
            symbol=self.scan.pop(0);processed.add(symbol)
            self._run_scan_symbol(session,tracker,settings,symbol,overrides.get(symbol),notify)
        urgent_pending=any(s in self.scan for s in urgent)
        processed.update(processed_owned)
        retry=None if urgent_pending or self.stop() else self._due_s5_local_retry(pool,processed)
        if retry is not None:
            self.scan=[s for s in self.scan if s!=retry]
            self._run_scan_symbol(session,tracker,settings,retry,overrides.get(retry),notify,local_retry=True)
        if not urgent_pending and not self.stop():
            self._observe_s5_one(session,tracker,observation_pool,profile)
        counts={};day_assignments=self.store.assignments(day)
        for a in day_assignments:counts[a['arm']]=counts.get(a['arm'],0)+1
        native_names=('EXP_R','EXP_A','EXP_B','EXP_B_TP','EXP_E')+((S5,) if profile.get('s5_enabled') is True else ())
        native_counts={arm:counts.get(arm,0) for arm in native_names};native_total=sum(native_counts.values())
        self._status['native_allocation']={'target_probability_pct':{arm:100/len(native_names) for arm in native_names},
            'actual_counts':native_counts,'actual_denominator':native_total,
            'actual_control_share_at_least_15pct':None if not native_total else
                all(native_counts[arm]*100>=15*native_total for arm in ('EXP_R','EXP_A')),
            'basis':'fixed_initial_cohort_then_prospective_daily_control_floor_v2',
            'finite_realized_minimum_guaranteed':bool(native_total>=2
                and all(native_counts[arm]*100>=15*native_total for arm in ('EXP_R','EXP_A'))
                and all(a.get('allocation_policy') in ('frozen_cohort_minimum_controls_v1',
                    'prospective_daily_control_floor_v2') for a in day_assignments if a['arm'] in native_names)),
            'new_arrival_allocation_policy':'prospective_daily_control_floor_v2',
            'legacy_assignments_unchanged':True,'day':day,
            'control_share_shortfall_day':bool(native_total and any(native_counts[a]*100<15*native_total for a in ('EXP_R','EXP_A'))),
            'original_seeded_arrivals_are_finite_hash_samples_not_a_quota':True,
            'future_arrivals_are_not_independent_uniform_draws':True}
        control_record={**self._status['native_allocation'],'observed_at':self.now().isoformat()}
        control_key='control_share_day:'+_digest([day,native_counts])
        if getattr(self,'_control_observation_key',None)!=control_key:
            if self._status['native_allocation']['control_share_shortfall_day']:
                self.store.selection(day,'control_share_shortfall_seen',control_record)
            self._status['native_allocation']['day_ever_below_control_floor']=(
                self.store.selection(day,'control_share_shortfall_seen') is not None)
            control_record['day_ever_below_control_floor']=self._status['native_allocation']['day_ever_below_control_floor']
            self.store.selection(day,control_key,control_record)
            self._control_observation_key=control_key
        else:
            self._status['native_allocation']['day_ever_below_control_floor']=getattr(self,'_control_shortfall_seen',False)
        self._control_shortfall_seen=self._status['native_allocation']['day_ever_below_control_floor']
        self._status.update(reason='running',assignment_counts=counts,observed_symbols=list(self.seen),remaining=len(self.scan))
        observation_pending=(profile.get('s5_enabled') is True
            and '09:35'<=self.now().strftime('%H:%M')<'15:15'
            and bool(self.s5_observation_scope.get('pending_symbols')))
        self.next_at=self.now()+timedelta(seconds=1) if self.scan or observation_pending else minute+timedelta(minutes=1)
        retry_times=[datetime.fromisoformat(s['due_at']) for s in self._s5_local_retry.values() if s.get('due_at')]
        if retry_times:self.next_at=min(self.next_at,max(self.now()+timedelta(seconds=1),min(retry_times)))
