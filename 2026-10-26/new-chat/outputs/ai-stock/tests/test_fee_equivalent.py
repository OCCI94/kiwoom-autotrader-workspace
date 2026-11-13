import unittest
from copy import deepcopy
from kiwoom_autotrader.trading.fee_equivalent import realized_profit_tracks, aggregate_profit_tracks, tracks_from_projection

class FeeEquivalentTests(unittest.TestCase):
    def evidence(self):
        return dict(schema='realized-profit-evidence-v1',mode='paper',scope_id='lot-a',symbol='005930',day='20261102',
            sold_lot_basis_verified=True,gross_verified=True,verified_gross_pnl_krw='10000',gross_evidence_refs=['closed-lot-1'],
            matched_buy_amount_krw='1000000',matched_sell_amount_krw='1010000',actual_turnover_verified=True,
            cash_equity_scope_verified=True,turnover_evidence_refs=['00-buy-1','00-sell-1'],
            actual_costs_reconciled=True,actual_commission_krw='7000',actual_tax_krw='2020',ledger_net_krw='980',cost_evidence_refs=['72+73-reconciled'])
    def test_actual_turnovers_three_independent_tracks(self):
        e=self.evidence();before=deepcopy(e);r=realized_profit_tracks(e)
        self.assertEqual(e,before);self.assertEqual(r['gross_verified_krw'],'10000')
        self.assertEqual(r['mock_net_krw'],'980');self.assertEqual(r['real_fee_equivalent_krw'],'7678.50000')
        self.assertEqual(r['reference_tax_krw'],'2020.000');self.assertEqual(r['reference_commission_krw'],'301.50000')
        self.assertFalse(r['policy']['actual_account_cost_verified']);self.assertEqual(r['policy']['additional_slippage_rate'],'0')
    def test_unconfirmed_basis_or_overnight_average_is_not_gross(self):
        for delta in ({'sold_lot_basis_verified':False},{'gross_verified':False},{'gross_evidence_refs':[]},{'mode':'real'}):
            x=realized_profit_tracks({**self.evidence(),**delta});self.assertIsNone(x['gross_verified_krw']);self.assertIsNone(x['mock_net_krw']);self.assertIsNone(x['real_fee_equivalent_krw'])
        self.assertIsNone(tracks_from_projection({'net':100,'buy_amount':1000,'sell_amount':1200,'profit_basis':1000})['gross_verified_krw'])
    def test_cost_conflict_does_not_invalidate_independent_reference(self):
        x=realized_profit_tracks({**self.evidence(),'ledger_net_krw':'981'})
        self.assertIsNone(x['mock_net_krw']);self.assertIsNotNone(x['real_fee_equivalent_krw'])
    def test_reference_requires_matched_turnover_and_cash_classification(self):
        for delta in ({'actual_turnover_verified':False}, {'cash_equity_scope_verified':False}):
            x=realized_profit_tracks({**self.evidence(),**delta});self.assertIsNone(x['real_fee_equivalent_krw']);self.assertEqual(x['mock_net_krw'],'980')
        conflict=realized_profit_tracks({**self.evidence(),'matched_buy_amount_krw':'900000'})
        self.assertIsNone(conflict['gross_verified_krw']);self.assertIsNone(conflict['mock_net_krw']);self.assertEqual(conflict['gross_status'],'verified_inputs_conflict')
    def test_nonfinite_bool_cost_and_unknown_coverage(self):
        for v in ('NaN','Infinity',True,None):
            x=realized_profit_tracks({**self.evidence(),'actual_tax_krw':v});self.assertIsNone(x['mock_net_krw'])
        a=aggregate_profit_tracks([realized_profit_tracks(self.evidence()),realized_profit_tracks()])
        self.assertIsNone(a['tracks']['gross_verified_krw']['value_krw']);self.assertEqual(a['tracks']['gross_verified_krw']['evaluated_scopes'],1)
        self.assertEqual(a['tracks']['gross_verified_krw']['verified_subset_krw'],'10000')
        self.assertIsNone(aggregate_profit_tracks([])['tracks']['mock_net_krw']['value_krw'])
    def test_duplicate_scopes_are_unavailable_not_double_counted(self):
        item=realized_profit_tracks(self.evidence());out=aggregate_profit_tracks([item,item])
        self.assertEqual(out['duplicate_scope_count'],2);self.assertIsNone(out['tracks']['gross_verified_krw']['value_krw'])

if __name__=='__main__':unittest.main()
