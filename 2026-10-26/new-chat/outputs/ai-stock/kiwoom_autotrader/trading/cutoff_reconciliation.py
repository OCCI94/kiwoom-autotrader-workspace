"""Compare observed unit executions with the time span of a profit query.

Broker execution times are only second-resolution. An observation arriving
after a query does not by itself prove that the trade happened after it. A
partial or ambiguous query interval, incomplete unit sequence or lost stream
must remain unconfirmed; this is evidence about a subset, never final P&L.
"""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import re
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')


def _moment(value):
    if not isinstance(value, str): raise ValueError('missing time')
    value = datetime.fromisoformat(value)
    if value.tzinfo is None or value.utcoffset() is None: raise ValueError('naive time')
    return value.astimezone(KST)


def _amount(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)): raise ValueError('invalid amount')
    value = Decimal(value)
    if not value.is_finite() or value < 0: raise ValueError('invalid amount')
    return value


def classify_cost_cutoff(day, symbol, accounting_orders, cost_pages, *, broker_quantity, broker_amount):
    """Distinguish a completed historical subset from later observed fills.

    ``accounting_orders`` come from the validated 00-derived accounting view;
    ``cost_pages`` are all pages of one raw ka10072 query. The cost snapshot's
    true broker effective time is unknown within [first request, last response].
    A rounded timestamp on its boundary is never treated as unambiguous.
    """
    result = {'status': 'unconfirmed', 'reason': 'invalid_or_incomplete_evidence',
              'query_started_at': None, 'query_observed_at': None,
              'pre_completed_order_ids': [], 'post_order_ids': [], 'ambiguous_order_ids': [],
              'pre_quantity': None, 'pre_unit_amount': None,
              'post_quantity': None, 'post_unit_amount': None,
              'broker_quantity': None, 'broker_amount': None,
              'pre_quantity_matches_broker': None, 'pre_amount_matches_broker': None,
              'net_profit_status': 'unconfirmed'}
    try:
        if re.fullmatch(r'\d{8}', day or '') is None or re.fullmatch(r'[0-9A-Z]{6}', symbol or '') is None:
            return result
        if not isinstance(cost_pages, (tuple, list)) or not cost_pages or not isinstance(accounting_orders, (tuple, list)):
            return result
        first = cost_pages[0]; query_id = first.get('query_id')
        if not query_id: return result
        starts, ends = [], []
        for number, page in enumerate(cost_pages, 1):
            request=page.get('request_body', {}); body=page.get('response_body', {}); cont=page.get('continuation', {})
            if (page.get('query_id') != query_id or page.get('source_api') != 'ka10072'
                or page.get('mode') != 'paper' or page.get('day') != day or page.get('page') != number
                or page.get('status') != 'success' or page.get('http_status') != 200
                or body.get('return_code') != 0 or request.get('stk_cd', '').removeprefix('A') != symbol
                or request.get('strt_dt') != day or not isinstance(cont, dict)):
                return result
            if number == 1 and cont.get('request_cont_yn') not in ('N', '', None): return result
            if number > 1 and cont.get('request_cont_yn') != 'Y': return result
            if number < len(cost_pages) and cont.get('response_cont_yn') != 'Y': return result
            if number == len(cost_pages) and cont.get('response_cont_yn') not in ('N', '', None): return result
            start, end = _moment(page.get('request_started_at')), _moment(page.get('observed_at'))
            if start > end or start.date() != datetime.strptime(day, '%Y%m%d').date() or end.date() != start.date(): return result
            if ends and start < ends[-1]: return result
            starts.append(start); ends.append(end)
        start, end = starts[0], ends[-1]
        bqty = int(_amount(broker_quantity)); bamt = _amount(broker_amount)
        if Decimal(bqty) != _amount(broker_quantity) or bqty <= 0: return result
        result.update(query_started_at=start.isoformat(), query_observed_at=end.isoformat(),
                      broker_quantity=bqty, broker_amount=str(bamt))
        pre_qty=post_qty=0; pre_amt=post_amt=Decimal(0)
        relevant=[o for o in accounting_orders if o.get('mode') == 'paper' and o.get('symbol') == symbol and o.get('side') == 'sell']
        if not relevant: return result
        for o in relevant:
            order_id=str(o.get('order_number') or '')
            units=o.get('unit_executions')
            if (not re.fullmatch(r'\d{1,20}',order_id) or not isinstance(units,list) or not units
                or o.get('verified_amount_source') != '00_unit_sum_reconciled_to_cumulative_quantity'
                or o.get('unit_coverage') != 'quantity_reconciled' or o.get('stream_gap_observed') is not False
                or o.get('issues') or type(o.get('ordered_quantity')) is not int
                or type(o.get('verified_quantity')) is not int or o['verified_quantity'] <= 0
                or o['verified_quantity'] > o['ordered_quantity']
                or o.get('verified_quantity') != o.get('rest_projection_quantity')):
                result['ambiguous_order_ids'].append(order_id); continue
            pieces=[]
            for unit in units:
                clock=unit.get('broker_time'); qty=unit.get('quantity'); costobs=unit.get('reported_cost_observations')
                if (not isinstance(clock,str) or not re.fullmatch(r'(?:[01]\d|2[0-3])[0-5]\d[0-5]\d',clock)
                    or type(qty) is not int or qty <= 0 or not isinstance(costobs,list) or not costobs):
                    raise ValueError('invalid unit')
                event_time=datetime.strptime(day+clock, '%Y%m%d%H%M%S').replace(tzinfo=KST)
                first_seen=min(_moment(c['observed_at']) for c in costobs)
                if first_seen < event_time: raise ValueError('observation predates execution')
                amount=_amount(unit.get('amount'))
                if event_time + timedelta(seconds=1) <= start and first_seen < start: side='pre'
                elif event_time > end and first_seen > end: side='post'
                else: side='ambiguous'
                pieces.append((side,qty,amount))
            if sum(x[1] for x in pieces) != o['verified_quantity'] or sum((x[2] for x in pieces),Decimal(0)) != _amount(o.get('verified_amount')):
                result['ambiguous_order_ids'].append(order_id); continue
            categories={x[0] for x in pieces}
            if categories == {'pre'} and o['verified_quantity'] == o['ordered_quantity']:
                result['pre_completed_order_ids'].append(order_id); pre_qty+=sum(x[1] for x in pieces); pre_amt+=sum((x[2] for x in pieces),Decimal(0))
            elif categories == {'post'}:
                result['post_order_ids'].append(order_id); post_qty+=sum(x[1] for x in pieces); post_amt+=sum((x[2] for x in pieces),Decimal(0))
            else: result['ambiguous_order_ids'].append(order_id)
        result.update(pre_quantity=pre_qty, pre_unit_amount=str(pre_amt), post_quantity=post_qty, post_unit_amount=str(post_amt),
                      pre_quantity_matches_broker=pre_qty==bqty, pre_amount_matches_broker=pre_amt==bamt)
        if result['ambiguous_order_ids']:
            result.update(status='unconfirmed',reason='mixed_partial_gap_or_time_ambiguity')
        elif result['post_order_ids'] and pre_qty==bqty and pre_amt==bamt:
            result.update(status='post_cost_query_fills_present',reason='observed_completed_pre_query_orders_match_broker_totals')
        elif result['post_order_ids']:
            result.update(status='post_cost_query_fills_present',reason='pre_query_scope_or_totals_do_not_match')
        else:
            result.update(status='no_proven_post_query_complete_orders',reason='no_unambiguous_post_query_complete_order')
    except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation, OverflowError):
        result.update(status='unconfirmed',reason='invalid_or_incomplete_evidence')
    return result
