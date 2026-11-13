"""Pure selection from bounded, receiver-owned market observations.

This module does not fetch, authenticate or order. Membership is established
by ka10099 normal KOSPI/KOSDAQ rows AND ka10023's documented ETF/ETN/SPAC
exclusion, never by the user supplied pro6e taxonomy. Candidate enrollment is
not a strategy signal; every entry still needs fresh bars, quote and risk gates.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import re
import stat
import unicodedata

TELEGRAM_FEED_MAX_BYTES = 256 * 1024
TELEGRAM_FEED_TTL_SECONDS = 15 * 60
TELEGRAM_MESSAGE_MAX_AGE_SECONDS = 90 * 60
TELEGRAM_RESERVED_SLOTS = 5


def _same_name(left, right):
    return (isinstance(left, str) and isinstance(right, str)
            and ''.join(unicodedata.normalize('NFKC', left).split())
            == ''.join(unicodedata.normalize('NFKC', right).split()))


def read_telegram_candidates(path, now):
    """Read a bounded local watch feed; never treat its contents as an order.

    Only current-day messages proven poll_live by the research publisher are
    admitted, for at most 90 minutes. A publisher refresh cannot extend message
    freshness. Ranking, normal-equity, bars, quote and account guards remain
    independent. Claimed disclosure grades here never activate the S4 branch.
    """
    info = {'status':'unavailable', 'reason':'feed_missing_or_unreadable',
            'candidates':[], 'exclusions':[], 'source_sha256':None,
            'watch_only':True, 's4_disclosure_connected':False,
            'feed_max_age_seconds':TELEGRAM_FEED_TTL_SECONDS,
            'message_max_age_seconds':TELEGRAM_MESSAGE_MAX_AGE_SECONDS}
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            item = os.fstat(fd)
            if (not stat.S_ISREG(item.st_mode) or item.st_size > TELEGRAM_FEED_MAX_BYTES
                    or item.st_mode & 0o022
                    or hasattr(os, 'getuid') and item.st_uid != os.getuid()):
                info['reason'] = 'feed_file_invalid'
                return info
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                raw = stream.read(TELEGRAM_FEED_MAX_BYTES + 1)
        finally:
            os.close(fd)
        if len(raw) > TELEGRAM_FEED_MAX_BYTES:
            info['reason'] = 'feed_too_large'
            return info
        info['source_sha256'] = hashlib.sha256(raw).hexdigest()
        def unique(pairs):
            value = {}
            for key, entry in pairs:
                if key in value:
                    raise ValueError('duplicate JSON field')
                value[key] = entry
            return value
        feed = json.loads(raw, object_pairs_hook=unique)
        if (not isinstance(feed, dict) or feed.get('schema') != 'telegram-watch-candidates-v1'
                or feed.get('research_only') is not True or feed.get('tier_1_verified') is not False
                or feed.get('no_order_signal') is not True):
            info['reason'] = 'feed_schema_invalid'
            return info
        if feed.get('status') == 'revoked':
            info.update(status='revoked', reason='feed_revoked')
            return info
        if feed.get('status') != 'ready':
            info['reason'] = 'feed_not_ready'
            return info
        generated, cutoff, expiry = (_time(feed[key]).astimezone(now.tzinfo)
                                      for key in ('generated_at', 'as_of', 'expires_at'))
        if (not cutoff <= generated <= now < expiry
                or expiry-generated > timedelta(seconds=TELEGRAM_FEED_TTL_SECONDS)
                or now-generated > timedelta(seconds=TELEGRAM_FEED_TTL_SECONDS)
                or generated.date() != now.date() or cutoff.date() != now.date()):
            info['reason'] = 'feed_stale_or_future'
            return info
        rows = feed.get('candidates')
        if not isinstance(rows, list) or len(rows) > 100:
            info['reason'] = 'feed_candidate_bound_invalid'
            return info
        seen = set(); duplicates = set()
        for row in rows:
            code = row.get('code') if isinstance(row, dict) else None
            if code in seen:
                duplicates.add(code)
            seen.add(code)
        accepted, excluded = [], []
        for row in rows:
            code = row.get('code') if isinstance(row, dict) else None
            reason = None
            try:
                if not isinstance(code, str) or re.fullmatch(r'[0-9]{6}', code) is None:
                    raise ValueError('code')
                if code in duplicates:
                    reason = 'telegram_duplicate_symbol'
                evidence = row['evidence']; name = row['name']; hashes = row['source_hashes']
                if (not isinstance(name, str) or not name.strip() or len(name) > 100
                        or any(ord(char) < 32 or ord(char) == 127 for char in name)
                        or not isinstance(evidence, dict) or evidence.get('origin') != 'telegram'
                        or evidence.get('ingestion_modes') != ['poll_live']
                        or evidence.get('verification_grade') not in
                            ('provisional_message_context','matched_source','partially_verified')
                        or type(evidence.get('eligible_mentions')) is not int
                        or not 1 <= evidence['eligible_mentions'] <= 100000
                        or evidence.get('stance') not in ('bullish','neutral','unclear')
                        or evidence.get('negative_or_correction_or_cancel') is not False
                        or not isinstance(hashes, dict) or not 1 <= len(hashes) <= 16
                        or any(not isinstance(k,str) or len(k)>100 or not isinstance(v,str)
                               or re.fullmatch('[0-9a-f]{64}',v) is None for k,v in hashes.items())
                        or re.fullmatch('[0-9a-f]{64}', evidence.get('master_snapshot_sha256','')) is None):
                    raise ValueError('provenance')
                available = _time(row['available_at']).astimezone(now.tzinfo)
                observed = _time(row['last_message_observed_at']).astimezone(now.tzinfo)
                published = _time(evidence['source_published_at']).astimezone(now.tzinfo)
                extracted = _time(evidence['source_extracted_at']).astimezone(now.tzinfo)
                edited = evidence.get('source_edited_at')
                edited = _time(edited).astimezone(now.tzinfo) if edited is not None else None
                if (not published <= observed <= extracted <= available <= cutoff
                        or edited is not None and not published <= edited <= observed
                        or published.date() != now.date() or observed.date() != now.date()
                        or now-observed > timedelta(seconds=TELEGRAM_MESSAGE_MAX_AGE_SECONDS)):
                    reason = 'telegram_message_stale_or_future'
                if reason is None:
                    accepted.append({'code':code, 'name':name, 'available_at':available.isoformat(),
                        'last_message_observed_at':observed.isoformat(), 'source_hashes':hashes,
                        'evidence':{key:evidence[key] for key in ('origin','ingestion_modes',
                            'verification_grade','eligible_mentions','stance',
                            'negative_or_correction_or_cancel','source_published_at',
                            'source_extracted_at','master_snapshot_sha256')} | {'source_edited_at':evidence.get('source_edited_at')},
                        'watch_only':True, 's4_disclosure_connected':False})
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                reason = reason or 'telegram_provenance_invalid'
            if reason:
                excluded.append({'symbol':code if isinstance(code,str) and re.fullmatch(r'[0-9]{6}',code) else None,
                                 'reason':reason})
        info.update(status='ready', reason=None, candidates=accepted, exclusions=excluded,
                    generated_at=generated.isoformat(), as_of=cutoff.isoformat(),
                    expires_at=expiry.isoformat(), input_candidate_count=len(rows))
        return info
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        info['reason'] = 'feed_missing_or_invalid'
        return info


def _time(value):
    value = datetime.fromisoformat(value)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive timestamp")
    return value


def _fresh(snapshot, now, *, age, same_minute=False):
    try:
        start, received = (_time(snapshot[k]) for k in ("request_started_at", "available_at"))
        return (start.date() == now.date() and start <= received <= now
                and received - start <= timedelta(seconds=8)
                and now - received <= timedelta(seconds=age)
                and (not same_minute or start.replace(second=0, microsecond=0)
                     == now.replace(second=0, microsecond=0)))
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _normal_member(row, market):
    if not isinstance(row, dict):
        return "membership_conflict"
    if row.get("marketCode") != market or market not in ("0", "10"):
        return "market_membership_unverified"
    name = row.get("name")
    if (not isinstance(name, str) or not name.strip() or len(name) > 100
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        return "name_unverified"
    if (row.get("auditInfo") != "정상" or row.get("orderWarning") != "0"
            or not isinstance(row.get("state"), str) or not row["state"].strip()):
        return "management_status_unverified_or_restricted"
    if any(word in row["state"] for word in ("관리", "정지", "정리", "과열", "주의", "경고", "위험")):
        return "management_status_unverified_or_restricted"
    if row.get("upSizeName") not in ("소형주", "중형주", "대형주"):
        return "ordinary_equity_unverified"
    if any(word in (name + " " + str(row.get("companyClassName", ""))).upper()
           for word in ("ETF", "ETN", "SPAC", "스팩", "기업인수목적")):
        return "excluded_instrument"
    return None


def _positive_quantity(value):
    if not isinstance(value,str) or re.fullmatch(r"\+?[0-9]{1,20}",value) is None:
        return None
    quantity=int(value)
    return quantity if quantity>0 else None


def _liquidity(surge, turnover, code):
    """Current cumulative shares, not completed-bar RVOL or execution proof."""
    first=surge.get("liquidity_rows",{}).get(code)
    second=turnover.get("liquidity_rows",{}).get(code)
    if not isinstance(first,dict) or not isinstance(second,dict):
        return None, "current_volume_unverified"
    one,two=(_positive_quantity(row.get("now_trde_qty")) for row in (first,second))
    if one is None or two is None:
        return None, "current_volume_unverified"
    try:
        current_change=Decimal(second.get("flu_rt"))
    except (InvalidOperation,TypeError,ValueError):
        return None, "ranking_direction_unverified"
    if (not current_change.is_finite() or current_change<=0
            or first.get("pred_pre_sig") not in ("1","2")
            or second.get("pred_pre_sig") not in ("1","2")):
        return None, "ranking_direction_conflict_or_unverified"
    return {"surge_now_trde_qty":one, "turnover_now_trde_qty":two,
            "turnover_change_pct":str(current_change), "volume_unit":"shares",
            "turnover_unit_source":"million_krw", "basis":"current_day_cumulative",
            "same_time_rvol20":None}, None


def read_taxonomy(path, now):
    """Bounded local metadata only; invalid/future taxonomy contributes nothing."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            return {}, None
        data = json.loads(raw)
        if (data.get("schema") != "pro6e-sector-taxonomy-v1" or data.get("metadata_only") is not True
                or _time(data["created_at"]) > now or not isinstance(data.get("kr_stocks"), dict)):
            return {}, None
        return data["kr_stocks"], {"source_sha256":hashlib.sha256(raw).hexdigest(),
            "available_at":data["created_at"], "metadata_only":True}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}, None


def select_dynamic_candidates(surge, turnover, memberships, taxonomy, taxonomy_evidence,
                              *, now, session_binding, limit=30, configured_symbols=(),
                              telegram_feed=None, funnel=False, tiered=False):
    """Return watch candidates, with explicit exclusions and partial-page scope.

    New surge candidates require 4..28% gain; core/configured observations may
    be below 4% but must still be rising with current positive surge and actual
    positive turnover. No minimum count is filled with unverified candidates.
    """
    if type(funnel) is not bool:
        raise ValueError('funnel must be boolean')
    if type(limit) is not int or not 15 <= limit <= 30:
        raise ValueError("candidate limit must be between 15 and 30")
    if (not isinstance(configured_symbols, (list, tuple)) or len(configured_symbols) > (200 if funnel else 100)
            or any(not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None
                   for code in configured_symbols)):
        raise ValueError("configured symbols must be bounded KRX identifiers")
    result = {"schema":"paper-dynamic-candidates-v1", "status":"unavailable",
        "available_at":now.isoformat(), "session_binding":session_binding,
        "candidates":[], "base_candidates":[], "exclusions":[],
        "source_evidence":{}, "truncated":True, "signal_eligible":False,
        "scope":"intersection_of_first_krx_ranking_pages", "membership_max_age_seconds":300}
    feed = telegram_feed if isinstance(telegram_feed, dict) else {}
    telegram = {row['code']:row for row in feed.get('candidates', [])}
    tg_info = {key:value for key,value in feed.items() if key not in ('candidates','exclusions')}
    tg_info.update(reserved_slot_limit=TELEGRAM_RESERVED_SLOTS,
                   eligible_market_candidate_count=0, selected_count=0,
                   newly_enrolled_beyond_market_limit=0,
                   exclusions=list(feed.get('exclusions', [])))
    result['telegram_discovery'] = tg_info
    def defer_telegram(reason):
        tg_info['exclusions'].extend({'symbol':code, 'reason':reason} for code in telegram)
    if (not _fresh(surge, now, age=60, same_minute=True)
            or not _fresh(turnover, now, age=60, same_minute=True)):
        result["reason"] = "fresh_ranking_pages_unavailable"
        defer_telegram('telegram_fresh_market_evidence_unavailable')
        return result
    if surge.get("instrument_filter") != "ka10023_stk_cnd_20":
        result["reason"] = "instrument_exclusion_unverified"
        defer_telegram('telegram_instrument_exclusion_unverified')
        return result
    fresh_members = [item for item in memberships if _fresh(item, now, age=300)]
    index = {}
    for page in fresh_members:
        for code, row in page["rows"].items():
            index[code] = None if code in index else (row, page)
    def meta(page):
        return {key:value for key,value in page.items() if key not in ("rows", "values", "liquidity_rows",'row_source_sha256','row_market')}
    result["source_evidence"] = {"surge":meta(surge), "turnover":meta(turnover),
        "membership":[meta(page) for page in fresh_members], "taxonomy":taxonomy_evidence}
    result["truncated"] = bool(surge.get("truncated") or turnover.get("truncated")
        or len(fresh_members) < 2 or any(page.get("truncated") for page in fresh_members))
    base, found, excluded = set(configured_symbols), [], []
    for code, entry in surge["rows"].items():
        reason = None
        try:
            if not entry or entry[0] is None or entry[1] is None:
                raise ValueError("unknown ratio")
            change, ratio = Decimal(entry[0]), Decimal(entry[1])
            if not change.is_finite() or not ratio.is_finite():
                raise ValueError("nonfinite ratio")
        except (TypeError, ValueError, InvalidOperation):
            excluded.append({"symbol":code,"reason":"ranking_row_unverified"})
            continue
        item = taxonomy.get(code, {})
        core = isinstance(item, dict) and item.get("is_core_leader") is True
        if change <= 0 or ratio <= 0:
            reason = "not_rising_volume_surge"
        elif tiered and not 3<=change<=15 and code not in base:
            # Lower-gain T1 needs two independent source-bound precursor
            # families; the later Toss merge supplies that evidence explicitly.
            reason = "outside_t1_t2_verified_ranking_window"
        elif not tiered and not 4 <= change <= 28 and (funnel or not (core or code in base)):
            reason = "outside_dynamic_gain_window"
        elif change > (15 if tiered else 28):
            reason = "above_dynamic_gain_window"
        amount = turnover["values"].get(code)
        if reason is None and (type(amount) is not int or amount <= 0):
            reason = "current_turnover_unverified"
        liquidity = None
        if reason is None:
            liquidity, reason = _liquidity(surge,turnover,code)
        current_price=None
        if reason is None and tiered:
            prices=[]
            for snapshot in (surge,turnover):
                value=snapshot.get('liquidity_rows',{}).get(code,{}).get('cur_prc')
                if isinstance(value,str) and re.fullmatch('[+-]?[0-9]{1,12}',value):prices.append(abs(int(value)))
            if len(prices)!=2 or any(not 0<price<=500_000 for price in prices):reason='initial_half_budget_below_one_share_or_price_unverified'
            else:current_price=prices[-1]
        member = index.get(code)
        if reason is None and member is None:
            reason = "membership_missing_or_conflicting"
        if reason is None:
            reason = _normal_member(member[0], member[1]["market"])
        if (reason is None and (surge.get('row_market',{}).get(code)=='101'
                or turnover.get('row_market',{}).get(code)=='101') and member[1]['market']!='10'):
            reason='ranking_market_membership_conflict'
        if reason:
            excluded.append({"symbol":code,"reason":reason})
            continue
        row, page = member
        themes = item.get("sub_themes", []) if isinstance(item,dict) else []
        evidence = {key:row.get(key) for key in ("marketCode","auditInfo","orderWarning","state","upSizeName")}
        evidence.update(source="ka10099", available_at=page["available_at"],
                        source_sha256=page["source_sha256"], instrument_filter=surge["instrument_filter"])
        found.append({"symbol":code, "name":row["name"], "change_pct":str(change),
            "market":"KOSDAQ" if row['marketCode']=='10' else 'KOSPI',
            **({'current_price':current_price,'screening_pending':['intraday_bullish_open_requires_fresh_ka10001']} if tiered else {}),
            "surge_rate_pct":str(ratio), "turnover_krw":amount, "is_core_leader":core,
            "liquidity_evidence":liquidity,
            "sub_themes":[value for value in themes if isinstance(value,str)][:50],
            "available_at":max(surge["available_at"],turnover["available_at"],page["available_at"]),
            "source_hashes":{"surge":surge.get('row_source_sha256',{}).get(code,surge["source_sha256"]),
                             "turnover":turnover.get('row_source_sha256',{}).get(code,turnover["source_sha256"]),
                             "membership":page["source_sha256"]},
            "membership_evidence":evidence, "candidate_basis":"configured" if code in base else
                "core_leader_with_current_liquidity" if core else "rising_volume_surge",
            "signal_eligible":False})
    # Deterministic observed liquidity ordering; core status breaks equal-turnover ties.
    found.sort(key=lambda row:(-row["turnover_krw"], -int(row["is_core_leader"]), row["symbol"]))
    by_symbol = {row['symbol']:row for row in found}
    rejected = {row['symbol']:row['reason'] for row in excluded}
    for code, context in telegram.items():
        row = by_symbol.get(code)
        reason = None
        if row is None:
            reason = ('telegram_' + rejected[code] if code in rejected
                      else 'telegram_current_rank_evidence_missing')
        elif not _same_name(context['name'], row['name']):
            reason = 'telegram_current_identity_mismatch'
        elif not Decimal('4') <= Decimal(row['change_pct']) <= Decimal('28'):
            reason = 'telegram_outside_dynamic_gain_window'
        if reason:
            tg_info['exclusions'].append({'symbol':code, 'reason':reason})
            continue
        row['telegram_context'] = context
        row['candidate_origins'] = ['market_ranking', 'telegram_research']
        tg_info['eligible_market_candidate_count'] += 1
    if tiered:
        result.update(candidates=[row for row in found if row['symbol'] not in base],
            base_candidates=[row for row in found if row['symbol'] in base],
            status='ready' if fresh_members else 'unavailable',
            reason=None if fresh_members else 'membership_unavailable',
            exclusions=excluded,excluded_candidate_count=len(excluded),
            candidate_limit_truncated=False,selection_pending_union=True)
        if funnel:
            result.update(schema='paper-funnel-candidates-v1',candidates=found,base_candidates=[])
        return result
    if funnel:
        # No taxonomy membership assumption, hard top-N or Telegram allocation
        # precedes the portfolio prefilter. All verified rows stay in memory.
        result.update(schema='paper-funnel-candidates-v1', candidates=found,
            base_candidates=[], status='ready' if fresh_members else 'unavailable',
            reason=None if fresh_members else 'membership_unavailable',
            candidate_limit_truncated=False, excluded_candidate_count=len(excluded),
            exclusions=excluded, exclusions_truncated=False,
            rank_observed_count=len(surge['rows']), memory_candidate_count=len(found),
            priority_watch_symbols=sorted(base), ranking_order='turnover_krw_desc_core_symbol',
            market_gate='4_to_28_pct_positive_surge_actual_volume_and_turnover',
            taxonomy_is_membership_proof=False)
        return result
    result["base_candidates"] = [row for row in found if row["symbol"] in base]
    dynamic = [row for row in found if row["symbol"] not in base]
    original_top = {row['symbol'] for row in dynamic[:limit]}
    # Reserve real queue places: a valid Telegram candidate beyond the original
    # liquidity top-N can enter. Every such row already passed all market gates.
    reserved = [row for row in dynamic if 'telegram_context' in row]
    reserved.sort(key=lambda row:(-_time(row['telegram_context']['last_message_observed_at']).timestamp(),
                                 -row['turnover_krw'], row['symbol']))
    reserved = reserved[:TELEGRAM_RESERVED_SLOTS]
    priority = {row['symbol'] for row in reserved}
    result['candidates'] = reserved + [row for row in dynamic if row['symbol'] not in priority][:limit-len(reserved)]
    for row in reserved:
        row['telegram_reserved_slot'] = True
        row['telegram_newly_enrolled_beyond_market_limit'] = row['symbol'] not in original_top
    selected_tg = [row for row in result['candidates'] + result['base_candidates'] if 'telegram_context' in row]
    tg_info['selected_count'] = len(selected_tg)
    tg_info['newly_enrolled_beyond_market_limit'] = sum(row['symbol'] not in original_top for row in reserved)
    selected_codes = {row['symbol'] for row in selected_tg}
    tg_info['exclusions'].extend({'symbol':row['symbol'], 'reason':'telegram_candidate_capacity_deferred'}
        for row in found if 'telegram_context' in row and row['symbol'] not in selected_codes)
    result["excluded_candidate_count"] = len(excluded)
    result["exclusions"] = excluded[:100]
    result["exclusions_truncated"] = len(excluded) > 100
    result["candidate_limit_truncated"] = len(dynamic) > limit
    result["status"] = "ready" if fresh_members else "unavailable"
    result["reason"] = None if fresh_members else "membership_unavailable"
    return result
