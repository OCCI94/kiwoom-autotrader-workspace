"""Independent, offline review of OF source/time and denominator boundaries."""
from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import unittest
from kiwoom_autotrader.trading.legacy_order_flow import evaluate_order_flow

KST=ZoneInfo('Asia/Seoul')

def packet_at(now=None):
    now=now or datetime(2026,10,30,10,0,0,tzinfo=KST)
    identity={'symbol':'005930','mode':'paper','exchange':'KRX','session_id':'fixture-session',
              'login_id':'fixture-login','generation':'fixture-generation'}
    def event(kind,offset,values):
        at=now+timedelta(seconds=offset)
        values=dict(values);values['20' if kind=='0B' else '21']=at.strftime('%H%M%S')
        return dict(identity,kind=kind,source_at=at.isoformat(),available_at=at.isoformat(),
                    source_sha256='a'*64,values=values)
    events=[event('0B',-91,{'10':'10000','15':'+1','13':'100'})]
    cumulative=100
    for offset in list(range(-89,-65,2))+list(range(-59,-35,2))+list(range(-29,-5,2))+[-1]:
        recent=offset>-30; size=4 if recent and offset not in (-7,-1) else 1
        side='-' if recent and offset in (-7,-1) else '+'
        cumulative+=size
        events.append(event('0B',offset,{'10':'10020' if offset==-1 else '10000','15':side+str(size),'13':str(cumulative)}))
    for offset in (-35,-20,-10,-1):
        values={}
        for level in range(10):
            values[str(51+level)]=str(10000-level*10);values[str(71+level)]='120'
            values[str(41+level)]=str(10010+level*10);values[str(61+level)]='70'
        events.append(event('0D',offset,values))
    events.sort(key=lambda x:(x['available_at'],x['kind']))
    for sequence,row in enumerate(events):row['sequence']=sequence
    return {'as_of':now.isoformat(),'symbol':'005930','mode':'paper','session_id':'fixture-session',
            'login_id':'fixture-login','generation':'fixture-generation',
            'calendar':{'market':'KRX','is_trading_day':True,'session_date':now.strftime('%Y-%m-%d'),
                        'source_sha256':'c'*64,'available_at':(now-timedelta(seconds=100)).isoformat()},
            'coverage':{'from':(now-timedelta(seconds=100)).isoformat(),'available_at':now.isoformat(),
                        'gap_free':True,'source_sha256':'b'*64},'events':events}

class LegacyOrderFlowIndependentReview(unittest.TestCase):
    def test_core_volume_signals_do_not_grant_order_permission(self):
        result=evaluate_order_flow(packet_at())
        self.assertEqual(result['patterns']['OF-001']['status'],'PASS')
        self.assertEqual(result['patterns']['OF-004']['status'],'PASS')
        self.assertFalse(result['execution_enabled'])
        self.assertTrue(all(not item['permission_to_trade'] for item in result['patterns'].values()))

    def test_negative_book_quantity_is_never_made_positive(self):
        packet=packet_at();book=next(e for e in packet['events'] if e['kind']=='0D')
        book['values']['71']='-120'
        result=evaluate_order_flow(packet)
        self.assertTrue(all(item['status']=='UNKNOWN' for item in result['patterns'].values()))

    def test_negative_cumulative_trade_volume_is_invalid(self):
        packet=packet_at();trade=next(e for e in packet['events'] if e['kind']=='0B')
        trade['values']['13']='-100'
        result=evaluate_order_flow(packet)
        self.assertTrue(all(item['status']=='UNKNOWN' for item in result['patterns'].values()))

    def test_90_second_anchor_before_regular_open_is_not_regular_session_history(self):
        packet=packet_at(datetime(2026,10,30,9,1,0,tzinfo=KST))
        result=evaluate_order_flow(packet)
        self.assertTrue(all(item['status']=='UNKNOWN' for item in result['patterns'].values()))

    def test_weekday_without_calendar_attestation_is_unknown(self):
        packet=packet_at();packet.pop('calendar')
        result=evaluate_order_flow(packet)
        self.assertTrue(all(item['status']=='UNKNOWN' for item in result['patterns'].values()))

    def test_calendar_future_or_closed_never_authorizes_signals(self):
        for amendment in ({'is_trading_day':False}, {'available_at':'2026-10-30T10:00:01+09:00'}):
            packet=packet_at();packet['calendar'].update(amendment)
            with self.subTest(amendment=amendment):
                result=evaluate_order_flow(packet)
                self.assertTrue(all(item['status']=='UNKNOWN' for item in result['patterns'].values()))

    def test_support_cannot_be_derived_from_residual_bid_size_alone(self):
        result=evaluate_order_flow(packet_at())
        self.assertNotEqual(result['patterns']['OF-002']['status'],'PASS')
        self.assertNotEqual(result['patterns']['OF-005']['status'],'PASS')

    def test_wall_pattern_cannot_pass_without_separate_event_sequence(self):
        result=evaluate_order_flow(packet_at())
        self.assertNotEqual(result['patterns']['OF-006']['status'],'PASS')

    def test_zero_sell_volume_is_unknown_not_infinite_buy_ratio(self):
        packet=packet_at()
        for row in packet['events']:
            if row['kind']=='0B' and row['values']['15'].startswith('-'):
                row['values']['15']='+'+row['values']['15'][1:]
        result=evaluate_order_flow(packet)
        self.assertEqual(result['patterns']['OF-001']['status'],'UNKNOWN')

    def test_exact_two_to_one_is_inclusive_and_uses_shares(self):
        packet=packet_at();trades=[e for e in packet['events'] if e['kind']=='0B']
        cumulative=100
        for i,row in enumerate(trades[1:],1):
            size=6 if i==len(trades)-1 else 1
            side='-' if i==len(trades)-1 else '+'
            row['values']['15']=side+str(size);cumulative+=size;row['values']['13']=str(cumulative)
        result=evaluate_order_flow(packet)['patterns']['OF-001']
        self.assertEqual(result['status'],'PASS')
        self.assertEqual((result['metrics']['buy_qty'],result['metrics']['sell_qty']),(12,6))

    def test_cumulative_gap_and_future_receipt_are_unknown(self):
        for mutation in ('cumulative_gap','future_receipt'):
            packet=packet_at();trade=next(e for e in reversed(packet['events']) if e['kind']=='0B')
            if mutation=='cumulative_gap':trade['values']['13']=str(int(trade['values']['13'])+1)
            else:trade['available_at']='2026-10-30T10:00:01+09:00'
            with self.subTest(mutation=mutation):
                result=evaluate_order_flow(packet)
                self.assertTrue(all(item['status']=='UNKNOWN' for item in result['patterns'].values()))

    def test_five_second_freshness_limit_is_inclusive(self):
        packet=packet_at();packet['as_of']='2026-10-30T10:00:04+09:00'
        within=evaluate_order_flow(packet)
        self.assertNotEqual(within['patterns']['OF-001']['reason'],'latest_trade_or_book_stale')
        packet['as_of']='2026-10-30T10:00:05+09:00'
        stale=evaluate_order_flow(packet)
        self.assertEqual(stale['patterns']['OF-001']['reason'],'latest_trade_or_book_stale')

    def test_wall_book_component_does_not_count_later_price_collapse_as_support(self):
        from kiwoom_autotrader.trading.legacy_order_flow import _wall_clauses
        now=datetime(2026,10,30,10,0,0,tzinfo=KST)
        def at(seconds):return now+timedelta(seconds=seconds)
        def book(seconds,bid,asks):return {'src':at(seconds),'bids':[(bid,100)],'asks':asks}
        books=[book(-20,9990,[(10000,3000),(10010,100),(10020,100)]),
               book(-15,9990,[(10000,1000),(10010,100),(10020,100)]),
               book(-11,9000,[(9010,100),(9020,100),(9030,100)]),
               book(-1,9000,[(9010,100),(9020,100),(9030,100)])]
        trades=[{'src':at(-17),'price':10000,'size':1500,'side':'buy'},
                {'src':at(-12),'price':10010,'size':1,'side':'buy'},
                {'src':at(-10),'price':10010,'size':1,'side':'buy'}]
        candidates=_wall_clauses(books,[[],[],trades],now)
        wall=next(x for x in candidates if x['wall_price']==10000 and x['baseline_at']==at(-20).isoformat())
        self.assertNotEqual(wall['post_break_book_2sec'],'PASS')

    def test_legacy_candle_catalog_is_research_only_for_all_eight_patterns(self):
        from kiwoom_autotrader.trading.legacy_candle_catalog import list_patterns,evaluate_pattern
        entries=list_patterns()
        self.assertEqual(len(entries),8)
        self.assertEqual(len({entry.identifier for entry in entries}),8)
        for entry in entries:
            with self.subTest(pattern=entry.identifier):
                result=evaluate_pattern(entry.identifier,data={'future_bar':'2099-01-01T00:00:00+09:00'})
                self.assertEqual(result['status'],'UNKNOWN')
                self.assertFalse(result['input_evaluated'])
                self.assertFalse(result['eligible_for_order'])
