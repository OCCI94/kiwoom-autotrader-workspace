from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from kiwoom_autotrader.trading.catalyst_pre_scoring import (
    CatalystPreScorer, TelegramArchiveTail, DartOriginalQueueTail, load_linker)
from kiwoom_autotrader.trading.ticker_entity_linker import TickerLinker

NOW = datetime(2026, 11, 2, 0, 30, tzinfo=timezone.utc)


def linker():
    return TickerLinker([{'symbol': '042510', 'name': '라온시큐어'},
                         {'symbol': '053800', 'name': '안랩'},
                         {'symbol': '001680', 'name': '대상'}],
                       master_available_at=(NOW-timedelta(days=2)).isoformat(),
                       master_sha256='a'*64, source='fixture_verified_master')


def row(**kwargs):
    at = int((NOW-timedelta(seconds=2)).timestamp())
    result = {'version_id': 1, 'channel_id': 10, 'message_id': 20,
              'published_at': at, 'edited_at': None, 'text': '라온시큐어 공급계약 공시',
              'observed_at': at+1, 'recorded_at': at+1, 'origin': 'observed',
              'clock_valid': 1, 'batch_kind': 'live'}
    result.update(kwargs)
    return result


def dart(**kwargs):
    text = '라온시큐어 공급계약 공시'
    result = {'symbol': '042510', 'receipt_no': '20261102000001',
              'symbol_identity_verified': True, 'original_text_complete': True,
              'new_receipt_observed': True, 'text': text,
              'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
              'first_observed_at': NOW-timedelta(seconds=2),
              'available_at': NOW-timedelta(seconds=1),
              'published_at': NOW-timedelta(seconds=3)}
    result.update(kwargs)
    return result


class CatalystTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.bridge = CatalystPreScorer(linker(), lambda **kw: self.calls.append(kw) or True,
                                       session_id='paper-fixture-session')

    def test_exact_name_submits_untrusted_advisory_and_no_identifiers(self):
        self.assertEqual(self.bridge.consume_telegram([row()], now=NOW), 1)
        call = self.calls[0]
        self.assertEqual(call['kind'], 'news')
        self.assertEqual(call['symbol'], '042510')
        self.assertTrue(call['state']['text_is_untrusted_data'])
        self.assertFalse(call['state']['risk_approval'])
        self.assertNotIn('channel_id', call['state'])
        self.assertNotIn('message_id', call['state'])
        latest = self.bridge.latest_for('042510', now=NOW)
        self.assertEqual(latest['snapshot_id'], call['snapshot_id'])
        self.assertNotIn('text', latest)
        self.assertTrue(latest['advisory_only'])

    def test_old_content_collected_now_is_not_a_live_catalyst(self):
        old = int((NOW-timedelta(days=3)).timestamp())
        self.assertEqual(self.bridge.consume_telegram([row(published_at=old)], now=NOW), 0)
        self.assertFalse(self.calls)

    def test_future_master_and_source_do_not_enter_cache(self):
        self.bridge.linker.master_available_at = (NOW+timedelta(seconds=1)).isoformat()
        self.assertEqual(self.bridge.consume_telegram([row()], now=NOW), 0)
        self.bridge.linker.master_available_at = (NOW-timedelta(days=1)).isoformat()
        self.assertEqual(self.bridge.consume_telegram([row(version_id=2, observed_at=int(NOW.timestamp())+5)], now=NOW), 0)

    def test_edits_remove_prior_entity_and_unknown_new_version_revokes(self):
        self.bridge.consume_telegram([row()], now=NOW)
        self.assertIsNotNone(self.bridge.latest_for('042510', now=NOW))
        edited = row(version_id=2, edited_at=int(NOW.timestamp())-1, text='정정: 해당 종목 내용 삭제')
        self.bridge.consume_telegram([edited], now=NOW)
        self.assertIsNone(self.bridge.latest_for('042510', now=NOW))

    def test_fresh_edit_is_new_version_and_dedupe_does_not_revoke(self):
        self.bridge.consume_telegram([row()], now=NOW)
        old = self.bridge.latest_for('042510', now=NOW)['snapshot_id']
        edited = row(version_id=2, edited_at=int(NOW.timestamp())-1, text='라온시큐어 계약 정정 공시')
        self.bridge.consume_telegram([edited], now=NOW)
        new = self.bridge.latest_for('042510', now=NOW)['snapshot_id']
        self.assertNotEqual(old, new)
        self.assertEqual(self.bridge.consume_telegram([edited, row()], now=NOW), 0)
        self.assertEqual(self.bridge.latest_for('042510', now=NOW)['snapshot_id'], new)
        self.assertEqual(len(self.calls), 2)

    def test_immutable_version_conflict_revokes_without_rescoring(self):
        self.bridge.consume_telegram([row()], now=NOW)
        self.bridge.consume_telegram([row(text='라온시큐어 취소 공시')], now=NOW)
        self.assertIsNone(self.bridge.latest_for('042510', now=NOW))
        self.assertEqual(len(self.calls), 1)

    def test_ambiguous_name_requires_explicit_identity(self):
        self.assertEqual(self.bridge.consume_telegram([row(text='분석 대상 주가 확인')], now=NOW), 0)
        self.assertEqual(self.bridge.consume_telegram([row(version_id=2, text='#대상 공시')], now=NOW), 1)
        self.assertEqual(self.calls[0]['symbol'], '001680')

    def test_missing_clock_or_recorded_proof_and_legacy_never_score(self):
        for changes in ({'clock_valid': 0}, {'recorded_at': None}, {'origin': 'legacy_snapshot'}):
            self.assertEqual(self.bridge.consume_telegram([row(**changes)], now=NOW), 0)

    def test_missing_key_or_queue_rejection_not_replaced_with_old_probability(self):
        self.bridge.consume_telegram([row()], now=NOW)
        old = self.bridge.latest_for('042510', now=NOW)['snapshot_id']
        self.bridge.submit = lambda **kw: False
        self.bridge.consume_telegram([row(version_id=2, text='라온시큐어 공급계약 정정')], now=NOW)
        self.assertNotEqual(self.bridge.latest_for('042510', now=NOW)['snapshot_id'], old)
        self.assertNotIn('probability', self.bridge.latest_for('042510', now=NOW))

    def test_expiry_anchored_to_source_not_repeated_poll(self):
        self.bridge.consume_telegram([row()], now=NOW)
        self.assertIsNone(self.bridge.latest_for('042510', now=NOW+timedelta(seconds=89)))
        self.bridge.consume_telegram([row()], now=NOW+timedelta(seconds=89))
        self.assertEqual(len(self.calls), 1)

    def test_secret_shaped_text_is_redacted_before_submission(self):
        self.bridge.consume_telegram([row(text='라온시큐어 공시 client_secret=fixture_sensitive_value')], now=NOW)
        self.assertNotIn('fixture_sensitive_value', self.calls[0]['state']['text'])
        self.assertTrue(self.calls[0]['state']['redacted_sensitive_pattern'])

    def test_payload_size_bound_and_truncation_explicit(self):
        self.bridge.consume_telegram([row(text='라온시큐어 공시 '+('내용 '*4000))], now=NOW)
        self.assertLessEqual(len(self.calls[0]['state']['text'].encode()), 4096)
        self.assertFalse(self.calls[0]['state']['text_complete'])
        self.assertEqual(self.bridge.consume_telegram([row(version_id=2, text='x'*32769)], now=NOW), 0)
        self.assertIsNone(self.bridge.latest_for('042510', now=NOW))

    def test_cache_memory_bound_includes_latest_symbols(self):
        self.bridge.max_entries=1
        self.bridge.consume_telegram([row(),row(version_id=2,message_id=21,text='#안랩 공급계약 공시')],now=NOW)
        self.assertEqual(len(self.bridge._latest),1)
        self.assertEqual(len(self.bridge._seen),1)
        self.assertEqual(len(self.bridge._source_symbols),1)
        self.assertIsNotNone(self.bridge.latest_for('053800',now=NOW))

    def test_allowed_symbols_cannot_be_expanded_by_text(self):
        self.bridge.allowed = frozenset({'053800'})
        self.assertEqual(self.bridge.consume_telegram([row()], now=NOW), 0)

    def test_dart_original_hash_times_and_symbol_required(self):
        self.assertTrue(self.bridge.consume_dart(dart(), now=NOW))
        original = self.bridge.latest_for('042510', now=NOW)['snapshot_id']
        self.assertFalse(self.bridge.consume_dart(dart(), now=NOW))
        self.assertEqual(self.bridge.latest_for('042510', now=NOW)['snapshot_id'], original)
        self.assertFalse(self.bridge.consume_dart(dart(receipt_no='20261102000002', text_sha256='b'*64), now=NOW))
        self.assertFalse(self.bridge.consume_dart(dart(receipt_no='20261102000003', original_text_complete=False), now=NOW))

    def test_dart_date_only_new_same_day_retains_imprecise_flag(self):
        self.assertTrue(self.bridge.consume_dart(dart(published_at=None, publication_date='2026-11-02'), now=NOW))
        self.assertFalse(self.calls[0]['state']['publication_time_precise'])
        self.assertFalse(self.bridge.consume_dart(dart(receipt_no='20261030000001', published_at=None, publication_date='2026-10-30'), now=NOW))
        self.assertFalse(self.bridge.consume_dart(dart(receipt_no='20261102000002', new_receipt_observed=False), now=NOW))

    def test_scoring_exception_is_data_status_not_propagated(self):
        def fail(**kwargs):
            raise RuntimeError('private exception content must not escape')
        self.bridge.submit = fail
        self.assertEqual(self.bridge.consume_telegram([row()], now=NOW), 0)
        self.assertEqual(self.bridge.counts['submit_error'], 1)

    def test_master_requires_exact_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'master.json'
            raw = json.dumps({'records': [{'market': 'KR', 'symbol': '042510', 'name': '라온시큐어'}],
                              'observed_at': (NOW-timedelta(days=1)).isoformat()}).encode()
            path.write_bytes(raw)
            loaded = load_linker(path, hashlib.sha256(raw).hexdigest())
            self.assertEqual(loaded.names, {'042510': '라온시큐어'})
            with self.assertRaisesRegex(ValueError, 'HASH_MISMATCH'):
                load_linker(path, 'a'*64)


class ArchiveTailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'archive.sqlite3'
        with sqlite3.connect(self.path) as db:
            db.executescript('''CREATE TABLE post_versions(version_id INTEGER PRIMARY KEY,
               channel_id INTEGER,message_id INTEGER,published_at INTEGER,edited_at INTEGER,
               text TEXT,observed_at INTEGER,origin TEXT);
               CREATE TABLE event_collection_provenance(version_id INTEGER PRIMARY KEY,
               recorded_at INTEGER,first_seen_at INTEGER,clock_valid INTEGER,batch_kind TEXT);''')
            for prefix, table in [('post_versions', 'post_versions'), ('provenance', 'event_collection_provenance')]:
                for action in ('update', 'delete'):
                    db.execute(f"CREATE TRIGGER {prefix}_no_{action} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable');END")
        self.insert(1)

    def insert(self, version):
        r = row(version_id=version)
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO post_versions VALUES(?,?,?,?,?,?,?,?)',
                       tuple(r[k] for k in ('version_id','channel_id','message_id','published_at','edited_at','text','observed_at','origin')))
            db.execute('INSERT INTO event_collection_provenance VALUES(?,?,?,?,?)',
                       (version, r['recorded_at'], r['recorded_at'], 1, 'live'))

    def test_initial_prime_new_cursor_bounded_and_readonly(self):
        tail = TelegramArchiveTail(self.path, max_rows=1)
        before = self.path.read_bytes()
        self.assertEqual(tail.read_new(), [])
        self.assertEqual(before, self.path.read_bytes())
        self.insert(2); self.insert(3)
        before = self.path.read_bytes()
        self.assertEqual([r['version_id'] for r in tail.read_new()], [2])
        self.assertTrue(tail.last_status['pending'])
        self.assertEqual([r['version_id'] for r in tail.read_new()], [3])
        self.assertEqual(tail.read_new(), [])
        self.assertEqual(before, self.path.read_bytes())

    def test_missing_immutable_trigger_fails_closed(self):
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TRIGGER provenance_no_update')
        with self.assertRaisesRegex(ValueError, 'NOT_IMMUTABLE'):
            TelegramArchiveTail(self.path).read_new()

    def test_symlink_archive_rejected(self):
        other = self.path.parent/'linked.sqlite3'
        other.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, 'ARCHIVE_UNSAFE'):
            TelegramArchiveTail(other).read_new()

    def test_replaced_database_primes_instead_of_replaying(self):
        tail = TelegramArchiveTail(self.path)
        tail.read_new()
        clone = self.path.parent/'copy.sqlite3'
        clone.write_bytes(self.path.read_bytes())
        clone.replace(self.path)
        self.assertEqual(tail.read_new(), [])
        self.assertEqual(tail.last_status['stage'], 'primed_without_history_replay')


class _Guard:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def verify(self): pass


class DartQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mount = Path(self.tmp.name)
        self.config = self.mount/'config'; self.config.mkdir()
        self.archive = self.mount/'ai-stock-research-data/s5-full-eligibility/fixture'
        self.original = self.mount/'ai-stock-research-data/s5-dart-original-queues/fixture'
        self.archive.mkdir(parents=True);self.original.mkdir(parents=True)
        self.status_path = self.config/'toss-evidence-refresh-status.json'
        self.status_path.write_text('{}')
        self.tail = DartOriginalQueueTail(self.status_path, archive_mount=self.mount,
                                         mount_guard=_Guard, max_documents=1)
        self.assertEqual(self.tail.read_new(now=NOW), [])
        self.later = NOW+timedelta(seconds=4)

    @staticmethod
    def save(path, value):
        raw=json.dumps(value,ensure_ascii=False,sort_keys=True).encode()
        path.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def publish(self, *, bad_raw=False, old=False, escape=False):
        at = NOW-timedelta(seconds=5) if old else NOW+timedelta(seconds=1)
        text = '라온시큐어 신규 공급계약 공시'
        raw_path = self.original/'document.bin';raw_path.write_bytes(b'original-xml')
        text_path = self.original/'document.txt';text_path.write_text(text)
        doc={'receipt':'20261102000001','observed_at':at.isoformat(),'text_extraction_complete':True,
             'raw_path':str(raw_path),'text_path':str(text_path),
             'sha256':hashlib.sha256(raw_path.read_bytes()).hexdigest(),
             'text_sha256':hashlib.sha256(text.encode()).hexdigest()}
        if bad_raw:raw_path.write_bytes(b'changed-xml')
        if escape:doc['text_path']=str(self.config/'outside.txt');Path(doc['text_path']).write_text(text)
        original={'schema':'toss-dart-original-review-queue-v1','symbol':'042510','corp_code':'00000001',
                  'list_rows':[{'rcept_no':'20261102000001','rcept_dt':'20261102','stock_code':'042510'}],
                  'documents':[doc]}
        original_path=self.original/'queue.json';osh=self.save(original_path, original)
        queue={'created_at':(NOW+timedelta(seconds=2)).isoformat(),'rows':[{'symbol':'042510','corp_code':'00000001',
            'original_queue':{'path':str(original_path),'sha256':osh}}]}
        self.save(self.archive/'review-queue.json',queue)
        proof=self.archive/'042510-dart-1.json';proof.write_text('{}')
        risk={'generated_at':(NOW+timedelta(seconds=2)).isoformat(),'expires_at':(NOW+timedelta(minutes=30)).isoformat(),
              'evaluations':[{'symbol':'042510','dart_list_proofs':[{'path':str(proof)}]}]}
        rsh=self.save(self.config/'toss-risk-evidence.json',risk)
        self.save(self.archive/'result.json',{'status':'published','risk_sha256':rsh})
        self.save(self.status_path,{'schema':'toss-dart-intraday-refresh-status-v1','claim_result_recorded':True,
            'risk_promotions_blocked':False,'observed_at':(NOW+timedelta(seconds=3)).isoformat(),
            'full_eligibility':{'status':'published','risk_sha256':rsh}})

    def test_hash_verified_new_original_is_returned_once(self):
        self.publish()
        events=self.tail.read_new(now=self.later)
        self.assertEqual(len(events),1)
        calls=[];bridge=CatalystPreScorer(linker(),lambda **kw:calls.append(kw)or True,session_id='fixture')
        self.assertTrue(bridge.consume_dart(events[0],now=self.later))
        self.assertFalse(calls[0]['state']['publication_time_precise'])
        self.assertEqual(self.tail.read_new(now=self.later),[])
        self.assertNotIn('text',self.tail.last_status)

    def test_old_queue_on_first_start_not_replayed(self):
        self.publish()
        new=DartOriginalQueueTail(self.status_path,archive_mount=self.mount,mount_guard=_Guard)
        self.assertEqual(new.read_new(now=self.later),[])
        self.assertEqual(new.last_status['stage'],'prior_or_expired_queue_not_replayed')

    def test_old_original_new_status_does_not_refresh_ttl(self):
        self.publish(old=True)
        self.assertEqual(self.tail.read_new(now=self.later),[])
        self.assertEqual(self.tail.last_status['counts']['old_original_not_replayed'],1)

    def test_changed_original_fails_without_sending_content(self):
        self.publish(bad_raw=True)
        self.assertEqual(self.tail.read_new(now=self.later),[])
        self.assertEqual(self.tail.last_status['counts']['original_invalid_or_unavailable'],1)

    def test_path_escape_rejected(self):
        self.publish(escape=True)
        self.assertEqual(self.tail.read_new(now=self.later),[])
        self.assertEqual(self.tail.last_status['counts']['original_invalid_or_unavailable'],1)

    def test_replaced_published_risk_hash_rejected(self):
        self.publish()
        (self.config/'toss-risk-evidence.json').write_text('{}')
        self.assertEqual(self.tail.read_new(now=self.later),[])
        self.assertEqual(self.tail.last_status['stage'],'original_queue_unavailable')


if __name__ == '__main__':
    unittest.main()
