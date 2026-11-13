import io
import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import urllib.request

from kiwoom_autotrader.trading.jev_news_client import (
    ENDPOINT, MODEL, FAST_QUESTIONS, NEWS_QUESTIONS, JevJudgment, JevNewsClient,
    JevUnavailable, TLSKeepAliveOpener)
from kiwoom_autotrader.trading.jev_fast_decision import AsyncJevDecision, compact_fast_state
from kiwoom_autotrader.utils import http_queue

NOW = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)


def payload(kind='fast'):
    questions = FAST_QUESTIONS if kind == 'fast' else NEWS_QUESTIONS
    return {'model': MODEL, 'answers': {key: {'type': 'noul', 'noul': .7} for key in questions},
            'usage': {'input_tokens': 123}}


def response(body=None):
    return http_queue.BufferedResponse(json.dumps(body or payload()).encode(), {}, 200)


class NoWait:
    def acquire(self, *, before_send=None):
        if before_send:
            before_send()


class ClientTests(unittest.TestCase):
    def test_missing_key_no_network_and_repr_redacted(self):
        called = []
        client = JevNewsClient('', queued_transport=lambda *a, **k: called.append(1))
        with self.assertRaisesRegex(JevUnavailable, 'credentials_missing'):
            client.evaluate_fast({})
        self.assertEqual(called, [])
        secret_client = JevNewsClient('test-private-value')
        self.assertNotIn('test-private-value', repr(secret_client))
        client.close(); secret_client.close()

    def test_single_shot_default_wire_contract_and_pinned_model(self):
        called = []
        def transport(opener, request, **kwargs):
            called.append((request, kwargs)); kwargs['before_send']()
            return response()
        client = JevNewsClient('test-private-value', queued_transport=transport)
        result = client.evaluate_fast({'price_vwap_bps': 3})
        request, kwargs = called[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.get_method(), 'POST')
        body = json.loads(request.data)
        self.assertEqual(body['model'], MODEL)
        self.assertEqual(set(body['questions']), set(FAST_QUESTIONS))
        self.assertEqual(request.get_header('Connection'), 'close')
        self.assertIsInstance(kwargs['queue'], http_queue.HTTPQueue)
        self.assertFalse(isinstance(kwargs['queue']._limiter, http_queue.SharedRateLimit))
        self.assertLessEqual(kwargs['timeout'], .3)
        self.assertEqual(result.input_tokens, 123)
        self.assertEqual(dict(result.probabilities)['toxic_flow'], .7)
        client.close()

    def test_news_dimensions_remain_typed_probabilities(self):
        client = JevNewsClient('test-private-value', queued_transport=lambda *a, **k: response(payload('news')))
        result = client.evaluate_news({'text': 'untrusted event', 'symbol': '042510'})
        self.assertEqual(set(dict(result.probabilities)), {'direction', 'relevance', 'evidence', 'importance'})
        client.close()

    def test_malformed_probabilities_and_model_fail_closed(self):
        for alteration, expected in [
            (lambda d: d.update(model='jev-latest'), 'model_mismatch'),
            (lambda d: d['answers']['toxic_flow'].update(noul=True), 'response_invalid'),
            (lambda d: d['answers']['toxic_flow'].update(noul=1.01), 'response_invalid'),
            (lambda d: d['answers']['toxic_flow'].update(type='score'), 'response_invalid'),
            (lambda d: d['answers'].pop('toxic_flow'), 'response_invalid'),
        ]:
            data = payload(); alteration(data)
            client = JevNewsClient('test-private-value', queued_transport=lambda *a, **k: response(data))
            with self.assertRaisesRegex(JevUnavailable, expected):
                client.evaluate_fast({})
            client.close()

    def test_secret_and_exception_body_not_exposed_or_retried(self):
        calls = []
        def transport(*a, **k):
            calls.append(1); raise RuntimeError('test-private-value secret response https://secret')
        client = JevNewsClient('test-private-value', queued_transport=transport)
        with self.assertRaises(JevUnavailable) as caught:
            client.evaluate_fast({})
        self.assertEqual(str(caught.exception), 'transport_unavailable')
        self.assertEqual(len(calls), 1)
        client.close()

    def test_expired_before_send_and_late_response_discard(self):
        clock = [1.0]; calls = []
        def transport(*a, **k):
            calls.append(1); clock[0] += .31; return response()
        client = JevNewsClient('test-private-value', queued_transport=transport, clock=lambda: clock[0])
        with self.assertRaisesRegex(JevUnavailable, 'deadline_exceeded'):
            client.evaluate_fast({}, deadline_monotonic=.9)
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(JevUnavailable, 'deadline_exceeded'):
            client.evaluate_fast({})
        self.assertEqual(len(calls), 1)
        client.close()

    def test_deadline_rechecked_after_queue_thread_delay(self):
        clock=[1.0];calls=[]
        class Opener:
            def open(self,*a,**k):calls.append(k);return response()
        def transport(opener,request,**kwargs):
            kwargs['before_send']();clock[0]=1.301
            return opener.open(request,timeout=kwargs['timeout'])
        client=JevNewsClient('test-private-value',opener=Opener(),queued_transport=transport,clock=lambda:clock[0])
        with self.assertRaisesRegex(JevUnavailable,'deadline_exceeded'):client.evaluate_fast({})
        self.assertEqual(calls,[]);client.close()

    def test_queue_explicit_override_does_not_create_default_queue(self):
        fake = type('Opener', (), {'open': lambda self, request, **kw: response()})()
        queue = http_queue.HTTPQueue(limiter=NoWait(), max_pending=2, max_active=1)
        with patch.object(http_queue, '_default_queue', None):
            request = urllib.request.Request(ENDPOINT, data=b'{}', method='POST')
            with http_queue.queued_open(fake, request, queue=queue) as result:
                self.assertEqual(result.status, 200)
            self.assertIsNone(http_queue._default_queue)
        queue.close()
        with self.assertRaises(ValueError):
            http_queue.queued_open(fake, request, queue=object())
        for host in ('api.kiwoom.com','mockapi.kiwoom.com','kiwoom.com'):
            with self.assertRaises(ValueError):
                http_queue.queued_open(fake,urllib.request.Request('https://'+host+'/api'),queue=queue)


class FakeResponse:
    status = 200
    will_close = False
    def read(self, size): return json.dumps(payload()).encode()
    def close(self): pass
    def getheaders(self): return [('Content-Type', 'application/json')]


class KeepAliveTests(unittest.TestCase):
    def test_connection_reused_and_post_failure_never_replayed(self):
        made = []
        class Connection:
            sock = None
            def __init__(self, host, **kwargs): self.host=host;self.calls=0;self.closed=False;made.append(self)
            def request(self, *args, **kwargs): self.calls += 1
            def getresponse(self): return FakeResponse()
            def close(self): self.closed=True
        opener = TLSKeepAliveOpener(connection_factory=Connection)
        request = urllib.request.Request(ENDPOINT, data=b'{}', method='POST')
        for _ in range(2):
            with opener.open(request, timeout=.3) as r: self.assertEqual(r.status, 200)
        self.assertEqual(len(made), 1);self.assertEqual(made[0].calls, 2)
        def bad(*a, **kw): made[0].calls += 1;raise OSError('private')
        made[0].request=bad
        with self.assertRaisesRegex(OSError, '^transport_unavailable$'):opener.open(request, timeout=.3)
        self.assertEqual(len(made), 1);self.assertEqual(made[0].calls, 3);self.assertTrue(made[0].closed)
        with opener.open(request, timeout=.3):pass
        self.assertEqual(len(made), 2)
        opener.close();self.assertTrue(made[1].closed)

    def test_origin_redirect_and_method_not_allowed(self):
        opener=TLSKeepAliveOpener(connection_factory=lambda *a, **k: self.fail('network'))
        for url,method in [('http://api.typesafe.ai/v1/systemone','POST'),('https://evil.example/v1/systemone','POST'),(ENDPOINT,'GET'),(ENDPOINT+'?x=1','POST')]:
            with self.assertRaises(OSError):
                opener.open(urllib.request.Request(url,data=b'{}',method=method),timeout=.3)


class FakeClient:
    available = True
    def __init__(self): self.enter=threading.Event();self.release=threading.Event();self.calls=0
    def evaluate_fast(self, state, *, deadline_monotonic):
        self.calls+=1;self.enter.set();self.release.wait(2)
        return JevJudgment(MODEL,(('toxic_flow',.2),('quote_environment',.8),('momentum_quality',.9)),1.0)
    evaluate_news=evaluate_fast


class AsyncTests(unittest.TestCase):
    def args(self, **extra):
        return dict(kind='fast',symbol='042510',session_id='session1',snapshot_id='snap1',
                    state={'price_vwap_bps':1},observed_at=NOW,expires_at=NOW+timedelta(seconds=10),**extra)
    def lookup(self, cache, **changes):
        args=self.args();args.pop('state');args.pop('observed_at');args.pop('expires_at');args.update(changes)
        return cache.lookup(**args,now=NOW)
    def wait_result(self, cache, **changes):
        for _ in range(200):
            r=self.lookup(cache,**changes)
            if r['status']!='pending':return r
            time.sleep(.001)
        self.fail('worker did not complete')

    def test_lookup_does_not_start_work_or_wait_for_slow_network(self):
        client=FakeClient();cache=AsyncJevDecision(client,now=lambda:NOW);self.addCleanup(cache.close)
        self.assertEqual(self.lookup(cache)['source'],'deterministic_fallback');self.assertEqual(client.calls,0)
        self.assertTrue(cache.submit(**self.args()));self.assertTrue(client.enter.wait(1))
        values=[self.lookup(cache) for _ in range(1000)]
        self.assertTrue(all(r['status']=='pending' for r in values))
        self.assertFalse(client.release.is_set());self.assertEqual(client.calls,1)
        client.release.set();self.assertEqual(self.wait_result(cache)['source'],'jev_cache')

    def test_writer_lock_contention_never_blocks_lookup_or_submit(self):
        client=FakeClient();cache=AsyncJevDecision(client,now=lambda:NOW);self.addCleanup(cache.close)
        cache._lock.acquire()
        try:
            self.assertFalse(cache.submit(**self.args()))
            self.assertEqual(self.lookup(cache)['source'],'deterministic_fallback')
        finally:cache._lock.release();client.release.set()

    def test_expired_future_session_and_changed_generation_rejected(self):
        client=FakeClient();cache=AsyncJevDecision(client,now=lambda:NOW);self.addCleanup(cache.close)
        args=self.args();args['observed_at']=NOW+timedelta(seconds=1);self.assertFalse(cache.submit(**args))
        args=self.args();args['expires_at']=NOW;self.assertFalse(cache.submit(**args))
        self.assertTrue(cache.submit(**self.args()));self.assertTrue(client.enter.wait(1))
        self.assertFalse(cache.submit(**self.args()))
        args=self.args();args['snapshot_id']='snap2';self.assertTrue(cache.submit(**args))
        client.release.set();self.wait_result(cache,snapshot_id='snap2')
        self.assertEqual(self.lookup(cache)['source'],'deterministic_fallback')
        self.assertEqual(self.lookup(cache,snapshot_id='snap2',session_id='other')['source'],'deterministic_fallback')
        a=self.args();a['snapshot_id']='snap2';a['state']={'price_vwap_bps':999};self.assertFalse(cache.submit(**a))
        lookup={k:a[k] for k in ('kind','symbol','session_id','snapshot_id')}
        self.assertEqual(cache.lookup(**lookup,now=NOW+timedelta(seconds=11))['status'],'stale_or_late')

    def test_late_result_discard_and_invalidation(self):
        clock=[1.0];client=FakeClient();cache=AsyncJevDecision(client,now=lambda:NOW,clock=lambda:clock[0]);self.addCleanup(cache.close)
        self.assertTrue(cache.submit(**self.args()));self.assertTrue(client.enter.wait(1))
        clock[0]=1.301;client.release.set();self.assertEqual(self.wait_result(cache)['source'],'deterministic_fallback')
        self.assertTrue(cache.invalidate());self.assertEqual(self.lookup(cache)['status'],'cache_miss')
        cache.close();self.assertEqual(self.lookup(cache)['status'],'closed')

    def test_no_key_no_worker_and_state_byte_contract(self):
        cache=AsyncJevDecision(JevNewsClient(''),now=lambda:NOW)
        self.assertIsNone(cache._worker);self.assertFalse(cache.submit(**self.args()))
        self.assertEqual(self.lookup(cache)['status'],'credentials_missing')
        state=compact_fast_state({'price_vwap_bps':1.5,'short_amount_share':.1})
        self.assertIsNone(state['spread_bps']);self.assertLess(len(json.dumps(state,separators=(',',':')).encode()),400)
        for value in [float('nan'),True,'instruct model']:
            with self.assertRaises(ValueError):compact_fast_state({'spread_bps':value})
        cache.close()

    def test_pending_bound_and_displaced_identity_stays_missing(self):
        client=FakeClient();cache=AsyncJevDecision(client,now=lambda:NOW,max_pending=1,max_entries=1);self.addCleanup(cache.close)
        self.assertTrue(cache.submit(**self.args()));self.assertTrue(client.enter.wait(1))
        args=self.args();args['symbol']='053800';args['snapshot_id']='snap2';self.assertTrue(cache.submit(**args))
        third=self.args();third['snapshot_id']='snap3';self.assertFalse(cache.submit(**third))
        client.release.set();self.wait_result(cache,symbol='053800',snapshot_id='snap2')
        self.assertEqual(self.lookup(cache)['status'],'cache_miss')
        self.assertLessEqual(len(cache._view[0]),1)

    def test_monotonic_ttl_and_news_symbol_integrity(self):
        clock=[1.0];client=FakeClient();client.release.set()
        cache=AsyncJevDecision(client,now=lambda:NOW,clock=lambda:clock[0]);self.addCleanup(cache.close)
        self.assertTrue(cache.submit(**self.args()));self.wait_result(cache)
        clock[0]=12.0
        self.assertEqual(self.lookup(cache)['status'],'stale_or_late')
        news=self.args();news.update(kind='news',state={'symbol':'053800','text':'source text'})
        self.assertFalse(cache.submit(**news))
        news['state']['symbol']='042510';self.assertTrue(cache.submit(**news))

    def test_older_generation_and_malformed_identity_cannot_replace_current(self):
        client=FakeClient();cache=AsyncJevDecision(client,now=lambda:NOW);self.addCleanup(cache.close)
        self.assertTrue(cache.submit(**self.args()))
        older=self.args();older.update(snapshot_id='old',observed_at=NOW-timedelta(seconds=1))
        self.assertFalse(cache.submit(**older))
        self.assertEqual(self.lookup(cache,snapshot_id={})['status'],'invalid_identity')
        client.release.set();self.assertEqual(self.wait_result(cache)['source'],'jev_cache')


if __name__=='__main__':unittest.main()
