"""Read-only, source-labelled profit comparisons; no implied reconciliation.

All figures are exact decimal strings in KRW. A broker's reported average
price is not proof of individual acquisition lots or verified gross profit.
"""
from decimal import Decimal, InvalidOperation
from datetime import datetime, time
import re
from zoneinfo import ZoneInfo


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _observed(row):
    if not isinstance(row, dict):
        return None
    return {'observed_at': row.get('observed_at'), 'quantity': row.get('quantity'),
            'reported_net_krw': row.get('net'), 'commission_krw': row.get('fee'),
            'tax_krw': row.get('tax'), 'reported_buy_amount_krw': row.get('basis'),
            'reported_sell_amount_krw': row.get('sell_amount')}


def decompose_profit_reports(guard, *, account_report=None):
    """Compare independently validated journal guard reports, without guessing.

    The guard's caller must validate identity, page coverage, chronology and
    sell quantity. Different report times, fees, and account scope are preserved.
    This function cannot prove the cost basis or independently verify the fills.
    """
    guard = guard if isinstance(guard, dict) else {}
    reports = guard.get('reports') if isinstance(guard.get('reports'), dict) else {}
    a, b = _observed(reports.get('ka10072')), _observed(reports.get('ka10073'))
    out = {'schema': 'profit-source-decomposition-v1', 'source_72': a, 'source_73': b,
           'account_74': account_report, 'account_74_scope': 'account_day_not_proven_equal_to_selected_symbols',
           'verified_gross_pnl_krw': None, 'verified_net_pnl_krw': None,
           'fee_delta_73_minus_72_krw': None, 'net_delta_73_minus_72_krw': None,
           'delta_interpretation': 'observed_difference_not_proven_cause',
           'status': 'evidence_incomplete'}
    if guard.get('status') != 'reports_current' or a is None or b is None:
        return out
    qa, qb = _decimal(a['quantity']), _decimal(b['quantity'])
    if (qa is None or qb is None or qa <= 0 or qa != qb or qa != qa.to_integral_value()
            or not isinstance(a['observed_at'], str) or not isinstance(b['observed_at'], str)):
        return out
    for output, field in [('fee_delta_73_minus_72_krw', 'commission_krw'),
                          ('net_delta_73_minus_72_krw', 'reported_net_krw')]:
        left, right = _decimal(a[field]), _decimal(b[field])
        if left is not None and right is not None:
            out[output] = str(right - left)
    out['status'] = 'source_reports_compared_not_final'
    return out


def verified_gross_from_closed_scope(accounting, *, mode, symbol, proof):
    """Calculate gross only for a caller-reconciled, fully closed inventory.

    ``proof`` must be backed by the caller's account/holding observations, not
    inferred from 00 alone. It establishes that the starting inventory is known,
    *all* account orders in the interval are included, there were no external
    transactions or unresolved stream gaps, and the ending inventory is flat.
    We independently recheck every 00 unit against order cumulative evidence,
    the requested order set, inventory flow and the anchored times. Incomplete
    or conflicting evidence always returns None. This excludes partial-lot
    matching and does not establish net profit or actual fees.
    """
    try:
        if (not isinstance(accounting, dict) or accounting.get('schema') != 'execution-accounting-v3'
                or not isinstance(proof, dict) or mode != 'paper'
                or proof.get('mode') != mode or proof.get('symbol') != symbol
                or any(proof.get(k) is not True for k in
                       ('all_account_orders_included', 'external_activity_excluded', 'ending_flat_verified'))
                or proof.get('stream_gap_unresolved') is not False):
            return None
        opening = proof['opening']
        if (opening.get('source_api') != 'kt00018' or opening.get('source_verified') is not True
                or not isinstance(opening.get('evidence_id'), str) or not opening['evidence_id']):
            return None
        oq, oc = _decimal(opening['quantity']), _decimal(opening['cost_krw'])
        if (oq is None or oc is None or oq < 0 or oc < 0 or oq != oq.to_integral_value()
                or oc != oc.to_integral_value() or (oq == 0) != (oc == 0)):
            return None
        opening_time = datetime.fromisoformat(opening['observed_at'])
        checked = datetime.fromisoformat(proof['verified_at'])
        if opening_time.tzinfo is None or checked.tzinfo is None or opening_time > checked:
            return None
        day = datetime.strptime(accounting['day'], '%Y%m%d').date()
        orders = [o for o in accounting.get('orders', []) if o.get('mode') == mode and o.get('symbol') == symbol]
        expected = proof['order_numbers']
        if (not isinstance(expected, list) or not expected or len(expected) != len(set(expected))
                or set(expected) != {o['order_number'] for o in orders} or len(orders) != len(expected)):
            return None
        executions, identifiers = [], set()
        for order in orders:
            if (order.get('side') not in ('buy', 'sell') or order.get('issues')
                    or order.get('stream_gap_observed') is not False
                    or order.get('unit_coverage') != 'quantity_reconciled'
                    or order.get('verified_amount_source') != '00_unit_sum_reconciled_to_cumulative_quantity'
                    or not order.get('unit_executions')):
                return None
            amount = Decimal(0); quantity = Decimal(0)
            for unit in order['unit_executions']:
                uid = (order['order_number'], str(unit['execution_id']))
                tm = str(unit['broker_time'])
                if uid in identifiers or not re.fullmatch(r'[0-9]{6}', tm): return None
                identifiers.add(uid)
                if unit.get('conflicting_observation_ids'): return None
                when = datetime.combine(day,time(int(tm[:2]),int(tm[2:4]),int(tm[4:])),ZoneInfo('Asia/Seoul'))
                q,p = _decimal(unit['quantity']), _decimal(unit['price'])
                if (q is None or p is None or q<=0 or p<=0 or q!=q.to_integral_value()
                        or p!=p.to_integral_value() or _decimal(unit['amount'])!=q*p
                        or when < opening_time or when > checked): return None
                quantity += q; amount += q*p
                executions.append((when,order['side'],q))
            if (quantity != _decimal(order['verified_quantity']) or quantity != _decimal(order['ordered_quantity'])
                    or amount != _decimal(order['verified_amount'])): return None
            cumulative = order.get('latest_cumulative')
            if (not isinstance(cumulative, dict) or _decimal(cumulative.get('quantity')) != quantity
                    or (cumulative.get('amount') is not None and _decimal(cumulative['amount']) != amount)
                    or not cumulative.get('observed_at')): return None
            cumulative_at = datetime.fromisoformat(cumulative['observed_at'])
            if cumulative_at.tzinfo is None or not opening_time <= cumulative_at <= checked: return None
        # A same-second cross-side ordering is unknowable from this source.
        by_time = {}
        for when, side, q in executions:
            by_time.setdefault(when, set()).add(side)
        if any(len(sides) > 1 for sides in by_time.values()): return None
        inventory = oq
        for _, side, q in sorted(executions, key=lambda row: row[0]):
            inventory += q if side == 'buy' else -q
            if inventory < 0: return None
        if inventory != 0: return None
        buy = sum((_decimal(o['verified_amount']) for o in orders if o['side']=='buy'),Decimal(0))
        sell = sum((_decimal(o['verified_amount']) for o in orders if o['side']=='sell'),Decimal(0))
        return str(sell-buy-oc)
    except (KeyError, ValueError, TypeError, InvalidOperation, OverflowError):
        return None


def decompose_profit(accounting, *, mode, symbol, closed_scope_proof=None):
    """Read an execution-accounting-v3 view; ignore incomplete cost queries."""
    reports = {}
    if not isinstance(accounting, dict) or accounting.get('schema') != 'execution-accounting-v3':
        return decompose_profit_reports({})
    account = None
    for report in accounting.get('cost_reports', []):
        if not isinstance(report, dict) or report.get('mode') != mode or not report.get('complete_query'):
            continue
        api, totals = report.get('source_api'), report.get('totals')
        if not isinstance(totals, dict):
            continue
        if api == 'ka10074' and report.get('scope') == 'account_day':
            account = {'observed_at': report.get('observed_at'), 'reported_net_krw': totals.get('reported_net'),
                       'commission_krw': totals.get('commission'), 'tax_krw': totals.get('tax')}
        elif api in ('ka10072', 'ka10073') and report.get('symbol') == symbol and report.get('cash_symbol_scope_verified') is True:
            reports[api] = {'observed_at': report.get('observed_at'), 'quantity': totals.get('quantity'),
                            'net': totals.get('reported_net'), 'fee': totals.get('commission'),
                            'tax': totals.get('tax'), 'basis': totals.get('buy_amount'),
                            'sell_amount': totals.get('sell_amount')}
    comparable = (len(reports) == 2 and reports['ka10072']['quantity'] is not None
                  and reports['ka10072']['quantity'] == reports['ka10073']['quantity'])
    result = decompose_profit_reports({'status': 'reports_current' if comparable else 'evidence_incomplete',
                                       'reports': reports}, account_report=account)
    # Accounting reports have not necessarily been queried after the last sell.
    result['report_currentness'] = 'post_sell_not_independently_verified'
    if closed_scope_proof is not None:
        result['verified_gross_pnl_krw'] = verified_gross_from_closed_scope(
            accounting, mode=mode, symbol=symbol, proof=closed_scope_proof)
        result['gross_provenance'] = '00_units_and_caller_reconciled_closed_scope; caller evidence not independently authenticated'
    return result
