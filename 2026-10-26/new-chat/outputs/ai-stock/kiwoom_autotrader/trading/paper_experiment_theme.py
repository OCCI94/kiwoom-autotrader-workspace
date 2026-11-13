"""Research metadata on future records, using the unchanged concentration map.

This module does not fetch, replace or approve a taxonomy. Missing metadata is
explicit unknown, using the approved paper profile's existing unknown policy.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re

from .theme_concentration import theme_family_snapshot, UNKNOWN_POOL


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=True,sort_keys=True,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()


def theme_context(symbol,now,*,path=None,unclassified_policy='allow_unknown'):
    snapshot=theme_family_snapshot([symbol],now,path=path,unclassified_policy=unclassified_policy)
    families=snapshot['themes_by_symbol'].get(symbol,[UNKNOWN_POOL])
    known=[name for name in families if name!=UNKNOWN_POOL]
    evidence=snapshot.get('taxonomy_evidence');supplement=snapshot.get('supplement_evidence')
    # Bind the same source digest without copying unrelated companies' full
    # review prose into every order's64KB evidence envelope.
    if supplement is not None:
        supplement={k:deepcopy(supplement.get(k)) for k in
            ('schema','available_at','source_sha256','metadata_only','scope')}
    return {'schema':'paper-experiment-theme-context-v1','symbol':symbol,
        'observed_at':now.isoformat(),'theme':known[0] if len(known)==1 else '|'.join(known) if known else 'unknown',
        'theme_families':known,'unknown_reason':None if known else
            'taxonomy_unavailable' if snapshot.get('metadata_status')!='available' else 'symbol_membership_missing_or_invalid',
        'mapping_version':_digest({'taxonomy':evidence,'supplement':supplement,
            'family_rule':snapshot['family_rule']}),
        'taxonomy_evidence':deepcopy(evidence),'supplement_evidence':deepcopy(supplement),
        'metadata_status':snapshot.get('metadata_status'),'unclassified_policy':unclassified_policy,
        'family_rule':snapshot['family_rule'],'order_permission':False}


def validate_theme_context(value,*,symbol,as_of):
    if not isinstance(value,dict):raise ValueError('experiment_theme_context_invalid')
    try:at=datetime.fromisoformat(value['observed_at'])
    except (ValueError,TypeError,KeyError):raise ValueError('experiment_theme_context_invalid') from None
    groups=value.get('theme_families')
    if (value.get('schema')!='paper-experiment-theme-context-v1' or value.get('symbol')!=symbol
            or at.tzinfo is None or not at<=as_of or at.date()!=as_of.date()
            or not isinstance(groups,list)
            or any(not isinstance(g,str) or not g or len(g)>120 for g in groups)
            or groups!=sorted(set(groups))
            or value.get('theme')!=('|'.join(groups) if groups else 'unknown')
            or (value.get('unknown_reason') is not None if groups else not isinstance(value.get('unknown_reason'),str))):
        raise ValueError('experiment_theme_context_invalid')
    if (not isinstance(value.get('mapping_version'),str)
            or re.fullmatch('[0-9a-f]{64}',value['mapping_version']) is None
            or value.get('order_permission') is not False):raise ValueError('experiment_theme_context_invalid')
    expected=_digest({'taxonomy':value.get('taxonomy_evidence'),'supplement':value.get('supplement_evidence'),
        'family_rule':value.get('family_rule')})
    if expected!=value['mapping_version']:raise ValueError('experiment_theme_context_invalid')
    return value


def bind_assignment_theme(value,context):
    result=deepcopy(value)
    validate_theme_context(context,symbol=value['symbol'],as_of=datetime.fromisoformat(value['assigned_at']))
    result['theme_context']=deepcopy(context)
    result['assignment_sha256']=_digest({k:v for k,v in result.items() if k!='assignment_sha256'})
    return result


def signal_theme_features(context):
    return {'theme':context['theme'],'theme_families':deepcopy(context['theme_families']),
        'theme_unknown_reason':context['unknown_reason'],'theme_mapping_version':context['mapping_version'],
        'theme_observed_at':context['observed_at'],'theme_context':deepcopy(context)}
