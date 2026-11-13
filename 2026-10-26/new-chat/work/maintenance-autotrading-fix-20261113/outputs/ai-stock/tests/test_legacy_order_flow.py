"""Pure synthetic 0B/0D; no I/O, session, network, or orders."""
import copy
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import unittest
from kiwoom_autotrader.trading.legacy_order_flow import evaluate_order_flow

KST=ZoneInfo('Asia/Seoul')
NOW=datetime(2026,10,30,10,2,tzinfo=KST)

def packet():
    identity={'symbol':'005930','mode':'paper','exchange':'KRX','session_id':'session',
              'login_id':'login','generation':'generation','source_sha256':'a'*64}
    events=[];cum=1000
    def event(kind, seconds, values):
        at=NOW+timedelta(seconds=seconds)
        return dict(identity,kind=kind,source_at=at.isoformat(),available_at=at.isoformat(),values=values)
    events.append(event('0B',-92,{'20':(NOW-timedelta(seconds=92)).strftime('%H%M%S'),
                                 '10':'1000','15':'+1','13':str(cum)}))
    for n,sec in enumerate(range(-88,0,2)):
        size=3 if sec>-30 else 1
        cum+=size
        price=1000+(10 if sec>=-4 else 0)
        values={'20':(NOW+timedelta(seconds=sec)).strftime('%H%M%S'), '10':str(price),
                '15':('+' if n%4!=0 else '-')+str(size),'13':str(cum)}
        events.append(event('0B',sec,values))
    for sec in (-45,-25,-15,-5,-1):
        values={'21':(NOW+timedelta(seconds=sec)).strftime('%H%M%S')}
        for i in range(10):
            values[str(51+i)]=str(999-i);values[str(71+i)]=str(300 if sec>=-25 else 250)
            values[str(41+i)]=str(1001+i);values[str(61+i)]='100'
        events.append(event('0D',sec,values))
    events.sort(key=lambda e:e['source_at'])
    for seq,e in enumerate(events):e['sequence']=seq
    return {'as_of':NOW.isoformat(),'symbol':'005930','mode':'paper','session_id':'session',
            'login_id':'login','generation':'generation','coverage':{'from':(NOW-timedelta(seconds=93)).isoformat(),
            'available_at':NOW.isoformat(),'gap_free':True,'source_sha256':'c'*64},
            'calendar':{'market':'KRX','session_date':'2026-10-30','is_trading_day':True,
                        'available_at':NOW.isoformat(),'source_sha256':'e'*64},'events':events}

def ranking(symbol='005930'):
    names=[symbol]+[f'{i:06}' for i in range(1,10)]
    return {'source':'TOSS_MARKET_TRADING_AMOUNT_KR_REALTIME','ranked_at':NOW.isoformat(),
            'available_at':NOW.isoformat(),'source_sha256':'d'*64,
            'items':[{'rank':i,'symbol':code,'currency':'KRW','trading_amount':1000000}
                     for i,code in enumerate(names,1)]}

class OrderFlowTests(unittest.TestCase):
    def test_observed_independent_pass_and_unknown(self):
        result=evaluate_order_flow(packet())
        self.assertFalse(result['execution_enabled'])
        self.assertEqual(result['calibration'],'mock_initial_unvalidated')
        self.assertEqual(result['patterns']['OF-001']['status'],'PASS')
        self.assertEqual(result['patterns']['OF-004']['status'],'PASS')
        self.assertEqual(result['patterns']['OF-002']['reason'],'price_support_and_spread_definition_unspecified')
        self.assertEqual(result['patterns']['OF-003']['status'],'UNKNOWN')
        self.assertEqual(result['patterns']['OF-006']['status'],'UNKNOWN')
        self.assertTrue(all(not x['permission_to_trade'] for x in result['patterns'].values()))
    def test_sell_zero_unknown_not_infinity(self):
        data=packet()
        for event in data['events']:
            if event['kind']=='0B':event['values']['15']='+'+event['values']['15'].lstrip('+-')
        result=evaluate_order_flow(data)['patterns']['OF-001']
        self.assertEqual((result['status'],result['reason']),('UNKNOWN','sell_volume_zero'))
    def test_quantity_discontinuity_makes_all_unknown(self):
        data=packet();trade=next(x for x in data['events'] if x['kind']=='0B' and x['sequence']>2)
        trade['values']['13']=str(int(trade['values']['13'])+1)
        result=evaluate_order_flow(data)['patterns']
        self.assertEqual({x['reason'] for x in result.values()},{'cumulative_trade_discontinuity'})
    def test_no_anchor_or_coverage(self):
        for mutate in ('anchor','gap'):
            data=packet()
            if mutate=='anchor':data['events']=data['events'][1:]
            else:data['coverage']['gap_free']=False
            self.assertEqual(evaluate_order_flow(data)['patterns']['OF-001']['status'],'UNKNOWN')
    def test_generation_future_naive_duplicate_reject(self):
        for mutate in ('generation','future','naive','duplicate'):
            data=packet(); e=data['events'][-1]
            if mutate=='generation':e['generation']='other'
            elif mutate=='future':e['available_at']=(NOW+timedelta(seconds=1)).isoformat()
            elif mutate=='naive':e['available_at']=NOW.replace(tzinfo=None).isoformat()
            else:e['sequence']=data['events'][-2]['sequence']
            with self.subTest(mutate=mutate):
                self.assertEqual(evaluate_order_flow(data)['patterns']['OF-001']['status'],'UNKNOWN')
    def test_open_session_window_cannot_import_premarket(self):
        data=packet();data['as_of']=NOW.replace(hour=9,minute=0,second=30).isoformat()
        # Regenerate a valid but early-clock packet; all market data later than
        # cutoff is unknown, never implicitly backdated.
        self.assertEqual(evaluate_order_flow(data)['patterns']['OF-001']['status'],'UNKNOWN')
    def test_changed_bid_price_is_unknown(self):
        data=packet();book=[x for x in data['events'] if x['kind']=='0D'][-1]
        book['values']['51']='1000'
        self.assertEqual(evaluate_order_flow(data)['patterns']['OF-002']['reason'],'bid_price_levels_changed')
    def test_stale_latest_book_unknown(self):
        data=packet();data['events']=[x for x in data['events'] if not (x['kind']=='0D' and x['source_at']>(NOW-timedelta(seconds=6)).isoformat())]
        for n,e in enumerate(data['events']):e['sequence']=n
        self.assertEqual(evaluate_order_flow(data)['patterns']['OF-004']['reason'],'latest_trade_or_book_stale')
    def test_toss_uses_original_ranks_and_known_negative_only(self):
        data=packet();data['toss_ranking']=ranking()
        self.assertEqual(evaluate_order_flow(data)['patterns']['OF-005']['reason'],'price_support_formula_unspecified')
        data['toss_ranking']=ranking('900000')
        self.assertEqual(evaluate_order_flow(data)['patterns']['OF-005']['status'],'FAIL')
        data['toss_ranking']['items'].pop()
        self.assertEqual(evaluate_order_flow(data)['patterns']['OF-005']['status'],'UNKNOWN')
    def test_toss_future_and_old_are_unknown(self):
        data=packet();data['toss_ranking']=ranking()
        for value in ((NOW+timedelta(seconds=1)).isoformat(),(NOW-timedelta(seconds=61)).isoformat()):
            data['toss_ranking']['ranked_at']=value
            self.assertEqual(evaluate_order_flow(data)['patterns']['OF-005']['status'],'UNKNOWN')
    def test_recent_price_flat_cannot_pass_volume_surge(self):
        data=packet()
        for e in data['events']:
            if e['kind']=='0B':e['values']['10']='1000'
        self.assertEqual(evaluate_order_flow(data)['patterns']['OF-004']['status'],'FAIL')
    def test_invalid_negative_volume_and_orderbook_quantity_unknown(self):
        for kind, field in (('0B','13'),('0D','71')):
            data=packet(); e=next(x for x in data['events'] if x['kind']==kind)
            e['values'][field]='-123'
            with self.subTest(kind=kind):
                self.assertEqual(evaluate_order_flow(data)['patterns']['OF-001']['reason'],'negative_quantity')
    def test_calendar_proof_is_required_and_not_independently_verified(self):
        data=packet(); data.pop('calendar')
        result=evaluate_order_flow(data)
        self.assertEqual(result['patterns']['OF-001']['reason'],'krx_trading_day_not_proven')
        self.assertFalse(result['source_integrity_independently_verified'])
    def test_ask_price_change_makes_of002_unknown(self):
        data=packet();book=[x for x in data['events'] if x['kind']=='0D'][-1]
        book['values']['41']='1000'
        self.assertEqual(evaluate_order_flow(data)['patterns']['OF-002']['reason'],'bid_price_levels_changed')
    def test_numeric_zero_book_is_not_free_infinity(self):
        data=packet();book=[x for x in data['events'] if x['kind']=='0D'][-1]
        book['values']['61']='0'
        self.assertEqual(evaluate_order_flow(data)['patterns']['OF-001']['status'],'UNKNOWN')
    def test_pullback_rebound_uses_strict_0_1_boundary(self):
        data=packet()
        for event in data['events']:
            if event['kind']=='0B':event['values']['10']='1000'
        last=[e for e in data['events'] if e['kind']=='0B'][-1]
        last['values']['10']='1001'
        clause=evaluate_order_flow(data)['patterns']['OF-003']['metrics']
        self.assertEqual(clause['rebound_strict_0_1pct'],'FAIL')
        last['values']['10']='1002'
        clause=evaluate_order_flow(data)['patterns']['OF-003']['metrics']
        self.assertEqual(clause['rebound_strict_0_1pct'],'PASS')
        self.assertEqual(clause['stable_range_0_4pct'],'UNKNOWN')
    def test_rank_top_three_ratio_at_exact_2x(self):
        data=packet();data['toss_ranking']=ranking()
        for book in [e for e in data['events'] if e['kind']=='0D']:
            for fid in ('71','72','73'): book['values'][fid]='200'
        item=evaluate_order_flow(data)['patterns']['OF-005']
        self.assertEqual(item['metrics']['three_level_bid_over_ask_2x'],'PASS')
        self.assertEqual(item['status'],'UNKNOWN')
        self.assertEqual(item['metrics']['price_support'],'UNKNOWN')
        last=[e for e in data['events'] if e['kind']=='0D'][-1]
        last['values']['71']='199'
        self.assertEqual(evaluate_order_flow(data)['patterns']['OF-005']['status'],'FAIL')
    def test_wall_levels_and_discrete_observations_no_continuity_claim(self):
        data=packet();books=[e for e in data['events'] if e['kind']=='0D']
        books[0]['values']['61']='1200'
        books[1]['values']['61']='600'
        trades=[e for e in data['events'] if e['kind']=='0B']
        for e in trades:
            sec=(datetime.fromisoformat(e['source_at'])-NOW).total_seconds()
            if sec==-40:
                e['values']['15']='+600';e['values']['10']='1001'
            elif sec>-25:e['values']['10']='1003'
        cumulative=1000
        for e in trades[1:]:
            cumulative+=abs(int(e['values']['15']));e['values']['13']=str(cumulative)
        result=evaluate_order_flow(data)['patterns']['OF-006']
        self.assertEqual(result['status'],'UNKNOWN')
        wall=next(x for x in result['metrics']['candidate_walls'] if x['baseline_at']==books[0]['source_at'] and x['wall_price']==1001)
        self.assertEqual(wall['wall_1000_and_3x_other_mean'],'PASS')
        self.assertEqual(wall['same_price_drop_50pct'],'PASS')
        self.assertEqual(wall['same_price_buy_50pct'],'PASS')
        self.assertEqual(wall['breakout_0_1pct_observed_2sec'],'PASS')
        self.assertEqual(wall['post_break_book_2sec'],'UNKNOWN')
        self.assertEqual(wall['post_break_book_reason'],'quote_price_support_definition_unspecified')
        self.assertGreaterEqual(wall['post_break_book_overlap_count'],2)
        books[1]['values']['61']='601'
        wall=next(x for x in evaluate_order_flow(data)['patterns']['OF-006']['metrics']['candidate_walls'] if x['baseline_at']==books[0]['source_at'] and x['wall_price']==1001)
        self.assertEqual(wall['same_price_drop_50pct'],'FAIL')
    def test_wall_out_of_visible_levels_is_unknown(self):
        data=packet();books=[e for e in data['events'] if e['kind']=='0D']
        books[0]['values']['61']='1200'
        # Move the whole ask book, no matching absolute price remains.
        for i in range(10):books[1]['values'][str(41+i)]=str(1010+i)
        for b in books[2:]:
            for i in range(10):b['values'][str(41+i)]=str(1010+i)
        wall=next(x for x in evaluate_order_flow(data)['patterns']['OF-006']['metrics']['candidate_walls'] if x['baseline_at']==books[0]['source_at'])
        self.assertEqual(wall['same_price_drop_50pct'],'UNKNOWN')

    def test_real_mode_never_passes(self):
        data=packet();data['mode']='real'
        self.assertEqual({v['status'] for v in evaluate_order_flow(data)['patterns'].values()},{'UNKNOWN'})

if __name__=='__main__': unittest.main()
