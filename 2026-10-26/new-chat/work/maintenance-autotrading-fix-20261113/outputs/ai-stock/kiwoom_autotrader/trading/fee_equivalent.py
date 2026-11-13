"""Three separately labelled realized P&L tracks; no ledger writes or rate claims.

The caller must supply reconciled sold-lot evidence. Reported average costs,
same-day buy/sell differences and net+reported-cost reverse calculations do not
constitute such evidence. Decimal strings preserve fractional reference costs.
"""
from decimal import Decimal, InvalidOperation
import re

SCHEMA = 'realized-profit-tracks-v1'
REFERENCE_POLICY = {
    'id': 'actual-turnover-reference-fees-v1',
    'buy_commission_rate': '0.00015', 'sell_commission_rate': '0.00015',
    'sell_tax_rate': '0.002', 'additional_slippage_rate': '0',
    'basis': 'matched_actual_buy_and_sell_consideration',
    'actual_account_cost_verified': False,
    'rounding': 'unrounded_decimal_reference_not_broker_rounding',
    'research_110_41_43bp_models': False,
}


def _d(value):
    if value is None or isinstance(value, bool): return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError): return None


def _refs(value):
    return (isinstance(value, list) and bool(value) and
            all(isinstance(x, str) and 0 < len(x) <= 300 for x in value))


def realized_profit_tracks(evidence=None):
    """Evaluate an explicit caller-reconciled sold-lot scope, fail closed.

    No inferred overnight basis. Acquisition amounts must cover the sold lots,
    even when their actual buys occurred on an earlier date. A partial remaining
    position is permitted only with the same verified matched-lot scope.
    """
    e = evidence if isinstance(evidence, dict) else {}
    out = {'schema': SCHEMA, 'gross_verified_krw': None, 'mock_net_krw': None,
           'real_fee_equivalent_krw': None, 'gross_status': 'unverified_sold_lot_basis',
           'mock_net_status': 'gross_or_actual_costs_unverified',
           'real_fee_status': 'gross_or_actual_turnover_unverified',
           'matched_buy_amount_krw': None, 'matched_sell_amount_krw': None,
           'actual_commission_krw': None, 'actual_tax_krw': None,
           'reference_commission_krw': None, 'reference_tax_krw': None,
           'reference_total_cost_krw': None, 'policy': dict(REFERENCE_POLICY),
           'evidence_refs': [], 'unit': 'KRW', 'scope_id': e.get('scope_id'),
           'symbol': e.get('symbol'), 'day': e.get('day')}
    gross, buy, sell = (_d(e.get(k)) for k in
                       ('verified_gross_pnl_krw', 'matched_buy_amount_krw', 'matched_sell_amount_krw'))
    if (e.get('schema') != 'realized-profit-evidence-v1' or e.get('mode') != 'paper'
        or e.get('sold_lot_basis_verified') is not True or e.get('gross_verified') is not True
        or not isinstance(e.get('scope_id'),str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,160}',e['scope_id'])
        or not re.fullmatch(r'[0-9A-Z]{6}',str(e.get('symbol',''))) or not re.fullmatch(r'[0-9]{8}',str(e.get('day','')))
        or gross is None or not _refs(e.get('gross_evidence_refs'))): return out
    if (e.get('actual_turnover_verified') is True and buy is not None and sell is not None
        and (buy<=0 or sell<0 or gross!=sell-buy)):
        out['gross_status']='verified_inputs_conflict'
        return out
    out.update(gross_verified_krw=str(gross), gross_status='verified_sold_lot_scope',
               evidence_refs=list(e['gross_evidence_refs']))
    if (e.get('actual_turnover_verified') is True and buy is not None and sell is not None
        and buy > 0 and sell >= 0 and gross == sell-buy and _refs(e.get('turnover_evidence_refs'))
        and (e.get('cash_equity_scope_verified') is True or e.get('reference_market_assumption')=='KR_cash_equity_standard_fee_reference')):
        commission = (buy+sell)*Decimal('0.00015'); tax = sell*Decimal('0.002')
        out.update(matched_buy_amount_krw=str(buy), matched_sell_amount_krw=str(sell),
                   real_fee_equivalent_krw=str(gross-commission-tax), real_fee_status='reference_only',
                   reference_commission_krw=str(commission), reference_tax_krw=str(tax),
                   reference_total_cost_krw=str(commission+tax),
                   reference_market_assumption=e.get('reference_market_assumption'),
                   instrument_fee_classification_verified=e.get('cash_equity_scope_verified') is True)
        out['evidence_refs'].extend(e['turnover_evidence_refs'])
    fee, tax, reported = (_d(e.get(k)) for k in
                         ('actual_commission_krw', 'actual_tax_krw', 'ledger_net_krw'))
    if (e.get('actual_costs_reconciled') is True and fee is not None and tax is not None
        and fee >= 0 and tax >= 0 and _refs(e.get('cost_evidence_refs'))):
        net = gross-fee-tax
        if reported is not None and reported != net:
            out['mock_net_status'] = 'ledger_net_cost_conflict'
        elif e.get('ledger_net_krw') is not None and reported is None:
            out['mock_net_status'] = 'ledger_net_invalid'
        else:
            out.update(mock_net_krw=str(net), mock_net_status='actual_costs_reconciled',
                       actual_commission_krw=str(fee), actual_tax_krw=str(tax))
            out['evidence_refs'].extend(e['cost_evidence_refs'])
    out['evidence_refs'] = sorted(set(out['evidence_refs']))
    return out


def aggregate_profit_tracks(rows):
    """Do not turn missing scopes into zero or a partial sum into a full total."""
    items = list(rows); fields = ('gross_verified_krw', 'mock_net_krw', 'real_fee_equivalent_krw')
    result = {'schema': 'realized-profit-tracks-summary-v1', 'scope_count': len(items),
              'policy': dict(REFERENCE_POLICY), 'tracks': {}}
    identities=[(x.get('scope_id'),x.get('day'),x.get('symbol')) if x.get('scope_id') else None for x in items]
    duplicated={key for key in identities if key is not None and identities.count(key)>1}
    result['duplicate_scope_count']=sum(key in duplicated for key in identities)
    for field in fields:
        values = [_d(x.get(field)) if x.get('schema') == SCHEMA and key not in duplicated else None for x,key in zip(items,identities)]
        known = [x for x in values if x is not None]
        result['tracks'][field] = {'value_krw': str(sum(known)) if items and len(known)==len(items) else None,
            'verified_subset_krw': str(sum(known)) if known else None,
            'evaluated_scopes': len(known), 'unavailable_scopes': len(items)-len(known),
            'total_scopes': len(items), 'complete': bool(items) and len(known)==len(items)}
    return result


def tracks_from_projection(row):
    """A public summary never upgrades reported net/broker averages to evidence."""
    row = row if isinstance(row, dict) else {}
    evidence = row.get('realized_profit_evidence')
    if not isinstance(evidence, dict):
        evidence = {'scope_id': row.get('scope_id'), 'symbol': row.get('symbol'), 'day': row.get('day')}
    return realized_profit_tracks(evidence)
