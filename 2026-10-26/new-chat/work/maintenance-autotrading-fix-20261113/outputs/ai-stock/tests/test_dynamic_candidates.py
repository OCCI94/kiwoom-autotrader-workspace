import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from kiwoom_autotrader.api.session import BrokerSession, IssuedToken
from kiwoom_autotrader.trading.dynamic_candidates import select_dynamic_candidates
from kiwoom_autotrader.trading.paper_strategy_context import PaperStrategyContextProvider, ContextUnavailable

KST = ZoneInfo("Asia/Seoul")


class DynamicCandidateTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026,10,30,10,10,15,tzinfo=KST)
        self.session = BrokerSession(now=lambda:self.now)
        self.session.mode = "paper"
        self.session._issued = IssuedToken("test-not-a-real-token", "20261030235959")
        self.surge_rows = [{"stk_cd":"005930", "flu_rt":"8", "sdnin_rt":"100"},
                           {"stk_cd":"000660", "flu_rt":"6", "sdnin_rt":"300"}]
        self.turnover_rows = [{"stk_cd":"005930", "trde_prica":"20"},
                              {"stk_cd":"000660", "trde_prica":"10"}]
        self.members = {"0":[self.member("005930")], "10":[self.member("000660", "10")]}
        self.calls = {"surge":0,"turnover":0,"membership":[]}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.taxonomy = Path(self.tmp.name) / "taxonomy.json"
        self.taxonomy.write_text(json.dumps({"schema":"pro6e-sector-taxonomy-v1", "metadata_only":True,
            "created_at":"2026-10-29T09:00:00+09:00", "kr_stocks":{
                "005930":{"is_core_leader":True,"sub_themes":["반도체"]}}}))
        self.provider = PaperStrategyContextProvider(self.session, now=lambda:self.now, enable_surge=True,
            surge_fetcher=self.surge, turnover_fetcher=self.turnover, membership_fetcher=self.membership,
            taxonomy_path=self.taxonomy, telegram_candidates_path=Path(self.tmp.name) / 'no-telegram-feed.json')

    @staticmethod
    def member(code, market="0"):
        return {"code":code,"name":"검증기업","marketCode":market,"auditInfo":"정상",
            "orderWarning":"0","state":"증거금100%","upSizeName":"대형주","companyClassName":""}

    @staticmethod
    def page(rows, output, digest):
        return {"ok":True,"body":{output:copy.deepcopy(rows)},"wire_sha256":digest*64,
                "headers":{"cont-yn":"Y"}}

    def surge(self):
        self.calls["surge"] += 1
        rows=[{"now_trde_qty":"10000","prev_trde_qty":"5000","sdnin_qty":"+5000",
               "pred_pre_sig":"2"}|row for row in self.surge_rows]
        return self.page(rows,"trde_qty_sdnin","a") | {"instrument_filter":"ka10023_stk_cnd_20"}

    def turnover(self):
        self.calls["turnover"] += 1
        rows=[{"now_trde_qty":"10010","pred_trde_qty":"5000","flu_rt":"+8.00",
               "pred_pre_sig":"2"}|row for row in self.turnover_rows]
        return self.page(rows,"trde_prica_upper","b")

    def membership(self, market):
        self.calls["membership"].append(market)
        return self.page(self.members[market],"list","c" if market=="0" else "d")

    def discover(self, **kwargs):
        return self.provider.discover_dynamic_candidates(self.now, **kwargs)

    def test_bounded_current_evidence_catalog_staggers_and_deepcopy(self):
        result = self.discover(limit=15)
        self.assertEqual(result["status"], "ready")
        self.assertEqual([c["symbol"] for c in result["candidates"]], ["005930"])
        row = result["candidates"][0]
        self.assertEqual(row["turnover_krw"],20_000_000)
        self.assertEqual(row["liquidity_evidence"]["surge_now_trde_qty"],10000)
        self.assertIsNone(row["liquidity_evidence"]["same_time_rvol20"])
        self.assertTrue(row["is_core_leader"])
        self.assertFalse(row["signal_eligible"])
        self.assertTrue(result["truncated"])
        self.assertEqual(row["membership_evidence"]["source"],"ka10099")
        self.assertEqual(self.calls, {"surge":1,"turnover":1,"membership":["0"]})
        row["name"] = "modified"
        self.assertNotEqual(self.discover()["candidates"][0]["name"],"modified")
        self.assertEqual(self.calls["surge"],1)
        self.now += timedelta(minutes=1)
        second=self.discover()
        self.assertEqual({x["symbol"] for x in second["candidates"]},{"005930","000660"})
        self.assertEqual(self.calls["membership"],["0","10"])

    def test_shared_theme_cache_does_not_refetch_rankings(self):
        self.discover()
        identity=self.provider._session_identity()
        self.assertIsNotNone(self.provider._surge("005930",identity))
        self.provider._theme("005930",identity)
        self.assertEqual((self.calls["surge"],self.calls["turnover"]),(1,1))

    def test_new_four_percent_boundary_is_discoverable(self):
        data=json.loads(self.taxonomy.read_text())
        data['kr_stocks']['005930']['is_core_leader']=False
        self.taxonomy.write_text(json.dumps(data))
        self.surge_rows[0]['flu_rt']='4'
        result=self.discover()
        self.assertIn('005930',{r['symbol'] for r in result['candidates']})

    def test_static_core_is_not_independent_membership(self):
        self.members["0"] = []
        result=self.discover()
        self.assertEqual(result["candidates"],[])
        self.assertIn("membership_missing_or_conflicting",{x["reason"] for x in result["exclusions"]})

    def test_invalid_management_and_instrument_classification_fail_closed(self):
        for field,value in [("auditInfo","관리종목"),("auditInfo",""),("orderWarning","3"),
                ("orderWarning",None),("state","거래정지"),("upSizeName",""),
                ("name","검증스팩"),("marketCode","8"),("companyClassName","기업인수목적회사")]:
            with self.subTest(field=field,value=value):
                self.provider._membership_snapshots.clear()
                self.provider._membership_minute=None
                self.members["0"]=[self.member("005930")|{field:value}]
                self.assertEqual(self.discover()["candidates"],[])

    def test_duplicate_membership_or_rank_is_not_accepted(self):
        self.members["0"].append(self.member("005930"))
        self.assertEqual(self.discover()["candidates"],[])
        self.now += timedelta(minutes=1)
        self.surge_rows.append(dict(self.surge_rows[1]))
        self.assertEqual(self.discover()["candidates"],[])

    def test_falling_distribution_and_above_limit_are_separate(self):
        for change in ("-29.93","0","29"):
            with self.subTest(change=change):
                self.now += timedelta(minutes=1)
                self.surge_rows[0]["flu_rt"]=change
                self.assertNotIn("005930",[x["symbol"] for x in self.discover()["candidates"]])

    def test_core_and_configured_below4_require_current_liquidity(self):
        self.surge_rows[0]["flu_rt"]="2"
        result=self.discover(configured_symbols=["005930"])
        self.assertEqual(result["candidates"],[])
        self.assertEqual(result["base_candidates"][0]["symbol"],"005930")
        self.now += timedelta(minutes=1)
        self.turnover_rows=[]
        self.assertEqual(self.discover(configured_symbols=["005930"])["base_candidates"],[])

    def test_instrument_request_evidence_is_required(self):
        self.provider._surge_fetcher=lambda:self.page(self.surge_rows,"trde_qty_sdnin","a")
        result=self.discover()
        self.assertEqual(result["status"],"unavailable")
        self.assertEqual(result["reason"],"instrument_exclusion_unverified")

    def test_membership_failure_has_no_same_minute_retry_or_inferred_permission(self):
        def fail(market):
            self.calls["membership"].append(market)
            raise OSError("simulated")
        self.provider._membership_fetcher=fail
        self.assertEqual(self.discover()["status"],"unavailable")
        self.discover()
        self.assertEqual(self.calls["membership"],["0"])
        self.assertEqual(self.calls["surge"],1)

    def test_session_change_stops_before_more_requests(self):
        def rotate(market):
            self.session._issued=IssuedToken("different","20261030235959")
            return self.page(self.members[market],"list","c")
        self.provider._membership_fetcher=rotate
        with self.assertRaises(ContextUnavailable): self.discover()
        self.assertEqual(self.calls["surge"],0)

    def test_stale_membership_and_future_ranking_are_rejected(self):
        self.discover()
        surge=copy.deepcopy(self.provider._surge_snapshot)
        turnover=copy.deepcopy(self.provider._turnover_snapshot)
        pages=copy.deepcopy(list(self.provider._membership_snapshots.values()))
        pages[0]["available_at"]=(self.now-timedelta(minutes=6)).isoformat()
        pages[0]["request_started_at"]=pages[0]["available_at"]
        result=select_dynamic_candidates(surge,turnover,pages,{},None,now=self.now,session_binding="test")
        self.assertEqual(result["status"],"unavailable")
        surge["available_at"]=(self.now+timedelta(seconds=1)).isoformat()
        result=select_dynamic_candidates(surge,turnover,[],{},None,now=self.now,session_binding="test")
        self.assertEqual(result["reason"],"fresh_ranking_pages_unavailable")

    def test_no_requests_when_disabled_real_outside_hours_or_invalid_parameters(self):
        with self.assertRaises(ValueError): self.discover(limit=31)
        with self.assertRaises(ValueError): self.discover(configured_symbols=["invalid"])
        self.provider._enable_surge=False
        with self.assertRaises(ContextUnavailable): self.discover()
        self.provider._enable_surge=True
        self.now=self.now.replace(hour=16)
        with self.assertRaises(ContextUnavailable): self.discover()
        self.session.mode="real"
        with self.assertRaises(ContextUnavailable): self.discover()
        self.assertEqual(self.calls,{"surge":0,"turnover":0,"membership":[]})

    def test_candidate_limit_preserves_denominator_without_filling_unknown(self):
        codes=[f"{100000+i:06d}" for i in range(40)]
        self.surge_rows=[{"stk_cd":code,"flu_rt":"8","sdnin_rt":"100"} for code in codes]
        self.turnover_rows=[{"stk_cd":code,"trde_prica":str(50-i)} for i,code in enumerate(codes)]
        self.members["0"]=[self.member(code) for code in codes]
        result=self.discover(limit=15)
        self.assertEqual(len(result["candidates"]),15)
        self.assertTrue(result["candidate_limit_truncated"])
        self.assertEqual([x["symbol"] for x in result["candidates"]],codes[:15])

    def test_default_requests_use_documented_instrument_filter_and_membership_endpoint(self):
        calls=[]
        def capture(api, body, output, **kwargs):
            calls.append((api,body,output,kwargs))
            return {}
        self.provider._ranking_page=capture
        response=self.provider._fetch_surge_page()
        self.assertEqual(response["instrument_filter"],"ka10023_stk_cnd_20")
        self.assertEqual(calls[0][1]["stk_cnd"],"20")
        self.assertEqual(calls[0][1]["stex_tp"],"1")
        self.provider._fetch_membership_page("10")
        self.assertEqual(calls[1],("ka10099",{"mrkt_tp":"10"},"list",{"endpoint":"/api/dostk/stkinfo"}))

    def test_rotated_session_drops_previous_membership_and_rank_caches(self):
        first=self.discover()
        self.session._issued=IssuedToken("new","20261030235959")
        self.members["0"]=[]
        second=self.discover()
        self.assertNotEqual(first["session_binding"],second["session_binding"])
        self.assertEqual(second["candidates"],[])
        self.assertEqual(self.calls["surge"],2)
        self.assertEqual(self.calls["membership"],["0","0"])

    def test_future_taxonomy_cannot_promote_below_window_candidate(self):
        data=json.loads(self.taxonomy.read_text())
        data["created_at"]="2026-10-31T09:00:00+09:00"
        self.taxonomy.write_text(json.dumps(data))
        self.surge_rows[0]["flu_rt"]="2"
        result=self.discover()
        self.assertEqual(result["candidates"],[])
        self.assertIsNone(result["source_evidence"]["taxonomy"])

    def test_current_volume_and_direction_conflicts_never_qualify_as_liquid_risers(self):
        original_surge=copy.deepcopy(self.surge_rows)
        original_turnover=copy.deepcopy(self.turnover_rows)
        for side,key,value in [("surge","now_trde_qty",None),("surge","now_trde_qty","0"),
                ("surge","now_trde_qty","-1"),("turnover","now_trde_qty",""),
                ("turnover","flu_rt","-8"),("turnover","flu_rt","NaN"),
                ("turnover","pred_pre_sig","5"),("surge","pred_pre_sig",None)]:
            with self.subTest(side=side,key=key,value=value):
                self.now+=timedelta(minutes=1)
                self.surge_rows=copy.deepcopy(original_surge)
                self.turnover_rows=copy.deepcopy(original_turnover)
                (self.surge_rows if side=="surge" else self.turnover_rows)[0][key]=value
                self.assertNotIn("005930",[row["symbol"] for row in self.discover()["candidates"]])

    def test_actual_sdnin_field_and_signed_percentage_preserved(self):
        self.surge_rows[0].update(flu_rt="+8.50",sdnin_rt="+38.04")
        result=self.discover()
        self.assertEqual(result["candidates"][0]["change_pct"],"8.50")
        self.assertEqual(result["candidates"][0]["surge_rate_pct"],"38.04")
        self.now+=timedelta(minutes=1)
        del self.surge_rows[0]["sdnin_rt"]
        self.surge_rows[0]["sdinin_rt"]="+38.04"
        self.assertNotIn("005930",[row["symbol"] for row in self.discover()["candidates"]])


if __name__=="__main__":
    unittest.main()
