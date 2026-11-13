"""Paper multi-strategy scale-in, with unchanged strategy ownership and exits."""
from datetime import datetime

from .b3_entry import B3EvidenceUnavailable, price_above_basis, post_fill_trend_is_nonnegative
from .decision_evidence import _serialize_bar
from .observed_bar_window import actual_window, timeline_rows


def initial_config(settings, symbol, initial_budget):
    from kiwoom_autotrader.config.settings import effective_b3_guards, effective_paper_scan
    pct, profit = effective_b3_guards(settings, 'paper')
    if not 0 < pct < 100 or profit <= 0:
        return None
    budgets = {row['code']:row['budget'] for row in settings.get('gdcrs_stocks',[])}
    budgets.update(dict(effective_paper_scan(settings,'paper')['stocks']))
    total = min(1_000_000,budgets.get(symbol,1_000_000))
    if initial_budget != total*pct//100:
        return None
    return {'multi_b3_stage':'initial','multi_b3_initial_intent_id':None,
            'multi_b3_initial_entry_pct':pct,'multi_b3_add_on_profit_bps':profit,
            'multi_b3_total_budget_krw':total,'max_daily_entries_per_symbol':2}


def evaluate_add_on(candidate, *, bars, market, position, now, symbol, settings):
    """No order, no extra query. A missing post-fill path remains ineligible."""
    def skipped(reason):
        return {'eligible':False,'reason':reason}
    if not isinstance(candidate,dict):
        return skipped('add_on_initial_evidence_unavailable')
    try:
        original = dict(candidate['config'])
        current = initial_config(settings,symbol,
            original['multi_b3_total_budget_krw']*original['multi_b3_initial_entry_pct']//100)
        if (current is None or any(original.get(k)!=v for k,v in current.items())
                or position.get('overnight_unobserved_interval') is not False or position.get('half_take_done') is not False):
            return skipped('add_on_policy_or_position_changed')
        cfg = dict(current)
        if (position['generation_id'] != candidate['initial_intent_id']
                or position['owned_qty'] != candidate['initial_filled_qty']
                or position['basis_krw'] != candidate['initial_filled_principal_krw']):
            return skipped('add_on_basis_unconfirmed')
        fill = datetime.fromisoformat(candidate['initial_fill_observed_at'])
        recent = actual_window(bars,symbol,now,21,require_ohlcv=True)
        # Indicators tolerate proven row omissions, but no-dead-cross evidence
        # for scale-in is stricter: the whole post-fill path must be real and
        # calendar-contiguous. Unknown post-fill gaps never authorize an add.
        safe, trend = post_fill_trend_is_nonnegative(bars,now=now,symbol=symbol,
            initial_fill_observed_at=fill,short=5,long=20)
        if not safe:
            return skipped('add_on_negative_sma_observed')
        if not price_above_basis(market.get('current_price'),position['owned_qty'],position['basis_krw'],cfg['multi_b3_add_on_profit_bps']):
            return skipped('add_on_below_profit_threshold')
        prior = sum(b.volume for b in recent[:-1])
        if prior<=0 or 40*recent[-1].volume<3*prior:
            return skipped('add_on_volume_insufficient')
        budget = min(cfg['multi_b3_total_budget_krw']-position['basis_krw'],
                     cfg['multi_b3_total_budget_krw']*(100-cfg['multi_b3_initial_entry_pct'])//100)
        if budget < market['current_price']:
            return skipped('budget_below_one_share')
        cfg.update(multi_b3_stage='add',multi_b3_initial_intent_id=candidate['initial_intent_id'])
        return {'eligible':True,'reason':'add_on_strength','budget':budget,'config':cfg,
                'addition':{'initial_fill_observed_at':fill.isoformat(),
                            'trend_bars':[_serialize_bar(b) for b in trend],
                            'trend_timeline':timeline_rows(trend)}}
    except (B3EvidenceUnavailable,ValueError,TypeError,KeyError,AttributeError,OverflowError):
        return skipped('add_on_path_or_volume_unavailable')
