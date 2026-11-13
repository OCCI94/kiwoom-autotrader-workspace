"""Single shared queue: research must not hold up normal-order dispatch."""
import multiprocessing
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.request

from kiwoom_autotrader.utils import http_queue
from tests.test_http_queue import FakeOpener, Response


def request(tr, suffix):
    return urllib.request.Request('https://mockapi.kiwoom.com/api/dostk/'+suffix,
                                  data=b'{}', headers={'api-id':tr,'authorization':'Bearer fixture'},
                                  method='POST')


def hold_research_tr_in_process(path, ready, release):
    limiter = http_queue.SharedRateLimit(path)
    lease = limiter.acquire_for_request(request('ka10081', 'external'))
    try:
        ready.set()
        release.wait(8)
    finally:
        lease.release(consumed=False)


class ResearchPriorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'rate.json'
        self.limiter=http_queue.SharedRateLimit(self.path)
        self.queue=http_queue.HTTPQueue(limiter=self.limiter)
        self.addCleanup(self.queue.close)
        self.opener=FakeOpener()

    def test_external_held_tr_does_not_block_normal_and_research_expires(self):
        # A separate open file description mimics another process's long call.
        held=self.limiter.acquire_for_request(request('ka10081','already'))
        self.addCleanup(held.release, consumed=False)
        research=request('ka10081','chart')
        allowed=threading.Event(); allowed.set()
        def guard():
            if not allowed.is_set(): raise ValueError('private guard detail')
        low=self.queue.submit(self.opener,research,priority='research',before_send=guard)
        time.sleep(.12)
        first=self.queue.submit(self.opener,request('kt10001','order1'))
        second=self.queue.submit(self.opener,request('kt10000','order2'))
        first.result(timeout=2);second.result(timeout=2)
        self.assertEqual([r.full_url.rsplit('/',1)[-1] for r,_,_ in self.opener.calls],['order1','order2'])
        self.assertFalse(low.done())
        allowed.clear()
        with self.assertRaises(http_queue.HTTPNotSentError) as caught:low.result(timeout=2)
        self.assertNotIn('private guard detail',str(caught.exception))
        self.assertEqual(len(self.opener.calls),2)

    def test_other_process_holding_research_tr_does_not_block_order(self):
        ctx=multiprocessing.get_context('spawn')
        ready,release=ctx.Event(),ctx.Event()
        process=ctx.Process(target=hold_research_tr_in_process,args=(str(self.path),ready,release))
        process.start()
        try:
            self.assertTrue(ready.wait(4))
            low=self.queue.submit(self.opener,request('ka10081','chart'),priority='research')
            time.sleep(.1)
            order=self.queue.submit(self.opener,request('kt10001','sell'))
            order.result(timeout=2)
            self.assertFalse(low.done())
            release.set();low.result(timeout=4)
            process.join(timeout=2);self.assertEqual(process.exitcode,0)
            self.assertEqual([r.full_url.rsplit('/',1)[-1] for r,_,_ in self.opener.calls],['sell','chart'])
        finally:
            release.set();process.join(timeout=2)
            if process.is_alive():process.terminate();process.join(timeout=2)

    def test_same_tr_cooldown_preserved_for_research(self):
        self.queue.submit(self.opener,request('ka10081','first'),priority='research').result(timeout=3)
        self.queue.submit(self.opener,request('ka10081','second'),priority='research').result(timeout=3)
        self.assertGreaterEqual(self.opener.calls[1][2]-self.opener.calls[0][2],1.0)

    def test_same_inflight_paper_tr_does_not_block_other_tr(self):
        entered,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        def respond(req,timeout):
            if req.full_url.endswith('/first'):
                entered.set()
                if not release.wait(4):raise RuntimeError('fixture timeout')
            return Response()
        opener=FakeOpener(respond)
        first=self.queue.submit(opener,request('ka10081','first'),priority='research')
        self.assertTrue(entered.wait(1))
        second=self.queue.submit(opener,request('ka10081','second'),priority='research')
        time.sleep(.1)
        order=self.queue.submit(opener,request('kt10001','sell'))
        order.result(timeout=2)
        self.assertFalse(second.done())
        release.set();first.result(timeout=2);second.result(timeout=3)
        self.assertEqual([r.full_url.rsplit('/',1)[-1] for r,_,_ in opener.calls],['first','sell','second'])
        self.assertGreaterEqual(opener.calls[2][2]-opener.calls[0][2],1.0)

    def test_close_cancels_busy_research_without_leaking_lease(self):
        held=self.limiter.acquire_for_request(request('ka10081','held'))
        self.addCleanup(held.release,consumed=False)
        low=self.queue.submit(self.opener,request('ka10081','chart'),priority='research')
        time.sleep(.12);self.queue.close()
        with self.assertRaises(OSError):low.result(timeout=2)
        self.queue._worker.join(timeout=2);self.assertFalse(self.queue._worker.is_alive())
        self.assertEqual(self.opener.calls,[])

    def test_research_and_normal_exact_pending_duplicate_keep_first(self):
        held=self.limiter.acquire_for_request(request('ka10081','held'))
        self.addCleanup(held.release,consumed=False)
        query=request('ka10081','chart')
        low=self.queue.submit(self.opener,query,priority='research')
        normal=self.queue.submit(self.opener,query)
        self.assertIs(low,normal)
        held.release(consumed=False)
        low.result(timeout=3)
        self.assertEqual(len(self.opener.calls),1)

    def test_research_must_target_paper_and_real_query_is_not_dispatched(self):
        q=urllib.request.Request('https://api.kiwoom.com/api/dostk/chart',data=b'{}',headers={'api-id':'ka10081'})
        with self.assertRaises(OSError):self.queue.submit(self.opener,q,priority='research').result(timeout=2)
        self.assertEqual(self.opener.calls,[])

    def test_global_four_per_second_applies_to_mixed_priorities(self):
        futures=[]
        for i in range(5):
            futures.append(self.queue.submit(self.opener,request('ka1000'+str(i),'item'+str(i)),
                                             priority='research' if i%2==0 else 'normal'))
        for future in futures:future.result(timeout=5)
        times=sorted(x[2] for x in self.opener.calls)
        self.assertGreaterEqual(times[-1]-times[0],1.0)


if __name__=='__main__':unittest.main()
