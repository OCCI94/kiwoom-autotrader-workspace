import copy
import unittest

from kiwoom_autotrader.trading.profit_decomposition import (decompose_profit, decompose_profit_reports,
                                                            verified_gross_from_closed_scope)


class ProfitDecompositionTests(unittest.TestCase):
    def test_separate_observations_and_never_assert_a_cause_or_gross(self):
        guard = {'status': 'reports_current', 'reports': {
            'ka10072': {'observed_at': '2026-10-29T19:56:51+09:00', 'quantity': 683,
                        'net': '-28748.99', 'fee': '76310', 'tax': '21939', 'basis': '100'},
            'ka10073': {'observed_at': '2026-10-29T19:57:00+09:00', 'quantity': 683,
                        'net': '-28898.99', 'fee': '76460', 'tax': '21939', 'basis': '100'}}}
        out = decompose_profit_reports(guard, account_report={'reported_net_krw': '-218229'})
        self.assertEqual(out['fee_delta_73_minus_72_krw'], '150')
        self.assertEqual(out['net_delta_73_minus_72_krw'], '-150.00')
        self.assertEqual(out['delta_interpretation'], 'observed_difference_not_proven_cause')
        self.assertIsNone(out['verified_gross_pnl_krw'])
        self.assertIsNone(out['verified_net_pnl_krw'])
        self.assertIn('account_day', out['account_74_scope'])

    def test_incomplete_guard_does_not_compare_or_invent_costs(self):
        out = decompose_profit_reports({'status': 'post_sell_report_pending', 'reports': {
            'ka10072': {'net':'1','fee':'3'}, 'ka10073': {'net':'2','fee':'4'}}})
        self.assertIsNone(out['fee_delta_73_minus_72_krw'])
        self.assertIsNone(out['verified_gross_pnl_krw'])

    def test_accounting_scope_incomplete_and_quantity_mismatch_do_not_compare(self):
        rows = []
        for api, quantity in [('ka10072', 3), ('ka10073', 4)]:
            rows.append({'mode':'paper','source_api':api,'symbol':'005930','complete_query':True,
                         'cash_symbol_scope_verified':True,'totals':{'quantity':quantity,'commission':'10'}})
        out = decompose_profit({'schema':'execution-accounting-v3','cost_reports':rows},mode='paper',symbol='005930')
        self.assertIsNone(out['fee_delta_73_minus_72_krw'])
        self.assertEqual(out['report_currentness'], 'post_sell_not_independently_verified')
        self.assertIsNone(out['verified_gross_pnl_krw'])

    @staticmethod
    def closed_fixture():
        def order(number, side, price, tm):
            return {'order_number':number,'mode':'paper','symbol':'005930','side':side,
                    'ordered_quantity':10,'verified_quantity':10,'verified_amount':str(price*10),
                    'issues':[],'stream_gap_observed':False,'unit_coverage':'quantity_reconciled',
                    'verified_amount_source':'00_unit_sum_reconciled_to_cumulative_quantity',
                    'latest_cumulative':{'quantity':10,'amount':str(price*10),'observed_at':'2026-10-29T09:02:30+09:00'},
                    'unit_executions':[{'execution_id':'1','broker_time':tm,'quantity':10,
                                        'price':str(price),'amount':str(price*10),'conflicting_observation_ids':[]}]}
        accounting={'schema':'execution-accounting-v3','day':'20261029','orders':[
            order('1','buy',10000,'090100'),order('2','sell',11000,'090200')], 'cost_reports':[]}
        proof={'mode':'paper','symbol':'005930','all_account_orders_included':True,
               'external_activity_excluded':True,'ending_flat_verified':True,'stream_gap_unresolved':False,
               'opening':{'source_api':'kt00018','source_verified':True,'evidence_id':'balance-1',
                          'quantity':0,'cost_krw':0,'observed_at':'2026-10-29T09:00:00+09:00'},
               'verified_at':'2026-10-29T09:03:00+09:00','order_numbers':['1','2']}
        return accounting,proof

    def test_verified_full_liquidation_of_reconciled_unit_fills(self):
        accounting,proof=self.closed_fixture()
        out=decompose_profit(accounting,mode='paper',symbol='005930',closed_scope_proof=proof)
        self.assertEqual(out['verified_gross_pnl_krw'],'10000')
        self.assertIsNone(out['verified_net_pnl_krw'])
        self.assertIn('not independently authenticated',out['gross_provenance'])

    def test_verified_known_opening_inventory_requires_full_liquidation(self):
        accounting,proof=self.closed_fixture()
        proof['opening'].update(quantity=5,cost_krw=45000)
        sell=accounting['orders'][1];sell['unit_executions'][0].update(quantity=15,amount='165000')
        sell.update(ordered_quantity=15,verified_quantity=15,verified_amount='165000')
        sell['latest_cumulative'].update(quantity=15,amount='165000')
        self.assertEqual(verified_gross_from_closed_scope(accounting,mode='paper',symbol='005930',proof=proof),'20000')

    def test_no_gross_for_partial_gap_unknown_opening_duplicate_or_basis_drift(self):
        original,base=self.closed_fixture()
        fixtures=[]
        accounting,proof=copy.deepcopy((original,base));accounting['orders'][1]['ordered_quantity']=11;fixtures.append((accounting,proof))
        accounting,proof=copy.deepcopy((original,base));accounting['orders'][0]['stream_gap_observed']=True;fixtures.append((accounting,proof))
        accounting,proof=copy.deepcopy((original,base));proof['opening']['source_verified']=False;fixtures.append((accounting,proof))
        accounting,proof=copy.deepcopy((original,base));accounting['orders'][0]['unit_executions']*=2;fixtures.append((accounting,proof))
        accounting,proof=copy.deepcopy((original,base));accounting['orders'][1]['verified_amount']='110001';fixtures.append((accounting,proof))
        accounting,proof=copy.deepcopy((original,base));proof['external_activity_excluded']=False;fixtures.append((accounting,proof))
        accounting,proof=copy.deepcopy((original,base));proof['opening']['observed_at']='2026-10-29T09:02:30+09:00';fixtures.append((accounting,proof))
        for accounting,proof in fixtures:
            self.assertIsNone(verified_gross_from_closed_scope(accounting,mode='paper',symbol='005930',proof=proof))
