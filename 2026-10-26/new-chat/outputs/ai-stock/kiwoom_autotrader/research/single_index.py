"""Offline single-index excess-return attribution; never a trading signal.

Caller supplies matched, decimal (1%=0.01) period returns and availability
metadata. No prices, market calendars, risk-free rates or missing values are
invented. The reserved future period is evaluated against its *realized*
market return, so the result is conditional explanatory fit, not a forecast
available at the beginning of that period. This module does not access a
broker, submit orders, load account settings or change strategies.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import fsum, isfinite, sqrt
from typing import Any

from .contracts import aware

MAX_ROWS = 100_000
_RETURN_FIELDS = ('asset_return', 'market_return', 'risk_free_return')
_AVAIL_FIELDS = ('asset_available_at', 'market_available_at', 'risk_free_available_at')
_FIELDS = frozenset(('period_start', 'period_end', *_RETURN_FIELDS, *_AVAIL_FIELDS))
_CONTEXT_FIELDS = frozenset(('asset', 'benchmark', 'currency', 'frequency', 'session',
                             'return_basis', 'asset_source', 'benchmark_source',
                             'risk_free_source', 'universe_selection', 'benchmark_role'))


class RegressionInputError(ValueError):
    """Input semantics are ambiguous or cannot be handled without guessing."""


def _moment(value: Any, field: str) -> datetime:
    try:
        return aware(value).astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise RegressionInputError(f'{field}: timezone-aware ISO timestamp required') from None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if isfinite(result) and abs(result) <= 1_000_000 else None


def _context(context: Any) -> dict:
    if not isinstance(context, dict) or set(context) != _CONTEXT_FIELDS:
        raise RegressionInputError('explicit asset, benchmark, currency, frequency, session, return basis, sources and selection required')
    for key, value in context.items():
        if not isinstance(value, str) or not value.strip() or len(value) > 512 or not value.isprintable():
            raise RegressionInputError(f'{key}: nonempty bounded text required')
    if context['currency'] not in ('KRW', 'USD'):
        raise RegressionInputError('currency must be KRW or USD')
    if context['benchmark_role'] not in ('market_index', 'peer_proxy'):
        raise RegressionInputError('benchmark_role must be market_index or peer_proxy')
    if context['frequency'] not in ('daily', 'monthly'):
        raise RegressionInputError('frequency must be daily or monthly')
    return dict(context)


def _fit(rows: list[dict]) -> dict:
    n = len(rows)
    xs = [r['_market'] - r['_risk_free'] for r in rows]
    ys = [r['_asset'] - r['_risk_free'] for r in rows]
    xm, ym = fsum(xs) / n, fsum(ys) / n
    dx, dy = [x-xm for x in xs], [y-ym for y in ys]
    sxx = fsum(x*x for x in dx)
    if not isfinite(sxx) or sxx <= 0:
        return {'status': 'unavailable', 'reason': 'market_excess_return_zero_variance', 'observations': n}
    beta = fsum(x*y for x, y in zip(dx, dy)) / sxx
    alpha = ym - beta*xm
    errors = [y - (alpha + beta*x) for x, y in zip(xs, ys)]
    sse = fsum(e*e for e in errors)
    sst = fsum(y*y for y in dy)
    if not all(isfinite(x) for x in (beta, alpha, sse, sst)):
        return {'status': 'unavailable', 'reason': 'nonfinite_arithmetic', 'observations': n}
    r_squared = None if sst <= 0 else max(0.0, min(1.0, 1.0-sse/sst))
    resid_se = sqrt(max(0.0, sse/(n-2)))
    beta_se = resid_se / sqrt(sxx)
    alpha_se = resid_se * sqrt(1/n + xm*xm/sxx)
    return {
        'status': 'fitted', 'observations': n,
        'alpha_per_period': alpha, 'beta': beta, 'r_squared': r_squared,
        'residual_standard_error': resid_se, 'alpha_standard_error': alpha_se,
        'beta_standard_error': beta_se,
        'alpha_t_statistic': alpha/alpha_se if alpha_se > 0 else None,
        'beta_t_statistic': beta/beta_se if beta_se > 0 else None,
        'training_mean_asset_excess_return': ym,
        'standard_error_assumptions': 'classical OLS iid/homoskedastic; not tested, not HAC or causal',
        'p_values': None, 'confidence_intervals': None,
    }


def fit_single_index(rows: list[dict], *, as_of: datetime | str,
                     training_end: datetime | str, context: dict,
                     min_observations: int = 24, analysis_mode: str = 'point_in_time') -> dict:
    """Fit historical beta/alpha and evaluate later conditional explanatory fit.

    Each row must contain identical-period asset/market/RF returns and their
    individual availability timestamps. Periods must be chronological and
    nonoverlapping; returns are not calculated from unverified prices here.
    The dataset's source identity/quality is recorded, not independently
    certified. Rows whose values or availability are missing remain in the
    denominator and are reported as excluded, never imputed as zero.
    """
    context = _context(context)
    if analysis_mode not in ('point_in_time', 'retrospective_descriptive'):
        raise RegressionInputError('unsupported analysis mode')
    cutoff, train_cutoff = _moment(as_of, 'as_of'), _moment(training_end, 'training_end')
    if train_cutoff > cutoff:
        raise RegressionInputError('training_end must not exceed as_of')
    if type(min_observations) is not int or not 3 <= min_observations <= MAX_ROWS:
        raise RegressionInputError('min_observations must be an integer between 3 and 100000')
    if not isinstance(rows, (list, tuple)) or len(rows) > MAX_ROWS:
        raise RegressionInputError('bounded list of rows required')
    training, evaluation, excluded = [], [], []
    previous_end = None
    for number, original in enumerate(rows, 1):
        if not isinstance(original, dict) or set(original) != _FIELDS:
            raise RegressionInputError(f'row {number}: fields missing or unknown')
        start = _moment(original['period_start'], 'period_start')
        end = _moment(original['period_end'], 'period_end')
        if start >= end or (previous_end is not None and start < previous_end):
            raise RegressionInputError(f'row {number}: unordered, duplicated or overlapping periods')
        previous_end = end
        row = {'number': number, 'period_start': start, 'period_end': end}
        reason = None
        if end > cutoff:
            reason = 'period_not_complete_as_of'
        vals = [_number(original[key]) for key in _RETURN_FIELDS]
        if any(value is None for value in vals):
            reason = reason or 'missing_or_invalid_return'
        elif context['risk_free_source'] == 'assumed_zero_not_observed' and vals[2] != 0:
            reason = reason or 'risk_free_assumption_conflicts_with_value'
        elif vals[0] < -1 or vals[1] < -1 or vals[2] < -1:
            reason = reason or 'return_below_minus_100_percent'
        avail = []
        for field in _AVAIL_FIELDS:
            try:
                avail.append(_moment(original[field], field))
            except RegressionInputError:
                avail.append(None)
        if any(value is None for value in avail):
            reason = reason or 'availability_missing_or_invalid'
        elif avail[0] < end or avail[1] < end:
            reason = reason or 'asset_or_market_available_before_period_end'
        elif any(value > cutoff for value in avail):
            reason = reason or 'not_available_as_of'
        if start < train_cutoff < end:
            reason = reason or 'period_crosses_split_boundary'
        if end <= train_cutoff:
            bucket = 'training'
            if analysis_mode == 'point_in_time' and all(value is not None for value in avail) and any(value > train_cutoff for value in avail):
                reason = reason or 'not_available_at_training_end'
        else:
            bucket = 'evaluation'
        if reason:
            excluded.append({'row': number, 'period_end': end.isoformat(), 'intended_partition': bucket, 'reason': reason})
            continue
        row.update(_asset=vals[0], _market=vals[1], _risk_free=vals[2])
        (training if bucket == 'training' else evaluation).append(row)

    result = {
        'schema': 'ai-stock-single-index-v1', 'context': context,
        'as_of': cutoff.isoformat(), 'training_end': train_cutoff.isoformat(),
        'analysis_mode': analysis_mode,
        'retrospective': analysis_mode == 'retrospective_descriptive',
        'risk_free_assumed_zero': context['risk_free_source'] == 'assumed_zero_not_observed',
        'benchmark_is_market_index': context['benchmark_role'] == 'market_index',
        'return_unit': 'decimal', 'formula': 'asset_return - risk_free_return = alpha + beta * (market_return - risk_free_return) + error',
        'counts': {'input_rows': len(rows), 'training_rows': len(training),
                   'evaluation_rows': len(evaluation), 'excluded_rows': len(excluded),
                   'minimum_training_rows': min_observations},
        'excluded': excluded, 'fit': None, 'evaluation': None,
        'availability_timestamps_independently_verified': False,
        'data_quality_independently_verified': False,
        'sample_selection_bias_independently_ruled_out': False,
        'out_of_sample_performance_verified': False,
        'forecast_or_trading_signal': False, 'automatic_orders': False,
        'limitations': [
            'Inputs are precomputed observations; no source, calendar, corporate action or market session is independently certified.',
            ('Training uses only rows declared available by training_end; the caller must substantiate the timestamps.'
             if analysis_mode == 'point_in_time' else
             'Retrospective mode uses data only known at the later as_of; the holdout was not observable at historical training_end and is not a prospective OOS test.'),
            ('Risk-free return is assumed zero, not observed. Alpha is not a verified risk-adjusted or Jensen alpha.'
             if context['risk_free_source'] == 'assumed_zero_not_observed' else
             'Risk-free source and timing are supplied by the caller and not independently verified.'),
            ('Peer basket is not a market index; beta is sensitivity to this basket, not market beta or a CAPM estimate.'
             if context['benchmark_role'] == 'peer_proxy' else
             'The benchmark is caller-selected, not independently verified as the market portfolio.'),
            'Later evaluation conditions on realized contemporaneous market return: it is not an ex-ante prediction or trading return.',
            'Selection after observing outcomes is retrospective even when period splitting is chronological.',
            'No transaction costs, spreads, market impact, fills or portfolio return are evaluated.',
            'Classical standard errors require assumptions and are not adjusted for serial correlation or heteroskedasticity.',
        ],
    }
    if len(training) < min_observations:
        result['fit'] = {'status': 'insufficient', 'reason': 'insufficient_training_rows',
                         'observations': len(training), 'required': min_observations}
        result['evaluation'] = {'status': 'not_evaluated', 'reason': 'model_not_fitted', 'observations': len(evaluation)}
        return result
    fit = _fit(training)
    result['fit'] = fit
    if fit['status'] != 'fitted' or not evaluation:
        result['evaluation'] = {'status': 'not_evaluated', 'reason': 'model_not_fitted' if fit['status'] != 'fitted' else 'no_eligible_later_rows', 'observations': len(evaluation)}
        return result
    alpha, beta = fit['alpha_per_period'], fit['beta']
    try:
        actual = [r['_asset']-r['_risk_free'] for r in evaluation]
        expected = [alpha + beta*(r['_market']-r['_risk_free']) for r in evaluation]
        errors = [y-yhat for y,yhat in zip(actual, expected)]
        mse = fsum(e*e for e in errors)/len(errors)
        baseline = fsum((y-fit['training_mean_asset_excess_return'])**2 for y in actual)
        sse = fsum(e*e for e in errors)
        if not all(isfinite(x) for x in (*expected, *errors, mse, baseline, sse)):
            raise ValueError
    except (OverflowError, ValueError):
        result['evaluation'] = {'status': 'not_evaluated', 'reason': 'nonfinite_arithmetic', 'observations': len(evaluation)}
        return result
    result['evaluation'] = {
        'status': 'retrospective_descriptive' if analysis_mode == 'retrospective_descriptive' else 'descriptive_unverified', 'observations': len(evaluation),
        'method': 'later observations with realized contemporaneous market excess return; not an ex-ante forecast',
        'root_mean_squared_error': sqrt(mse),
        'mean_absolute_error': fsum(abs(e) for e in errors)/len(errors),
        'r_squared_vs_training_mean': None if baseline <= 0 else 1-sse/baseline,
        'return_unit': 'decimal_per_period',
    }
    return result
