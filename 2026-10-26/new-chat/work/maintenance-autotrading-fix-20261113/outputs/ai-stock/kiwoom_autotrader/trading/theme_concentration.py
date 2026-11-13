"""Deterministic paper portfolio theme families; no prices or network access.

All memberships count. The representative family is the exact text before
the taxonomy's middle dot: 보안·사이버 / 보안·인증 / 보안·클라우드 -> 보안.
Missing metadata is unknown, never a new independent sector slot. The optional
shared bucket reserves at most two distinct unknown symbols across strategies.
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .dynamic_candidates import read_taxonomy
from kiwoom_autotrader.utils.paths import app_data_root


UNKNOWN_POOL = 'unclassified_pool'
SUPPLEMENT_FILENAME = 'theme-sector-supplement.json'
SUPPLEMENT_SCHEMA = 'official-business-theme-supplement-v1'


def _families(code, stocks, policy):
    if policy not in ('block', 'shared_bucket', 'allow_unknown'):
        raise ValueError('theme_unclassified_policy_invalid')
    if not isinstance(code, str) or re.fullmatch(r'[0-9]{6}', code) is None:
        raise ValueError('theme_membership_unavailable')
    row = stocks.get(code)
    if code not in stocks or (isinstance(row, dict) and row.get('sub_themes') == []):
        if policy in ('shared_bucket','allow_unknown'):
            return {UNKNOWN_POOL}
        raise ValueError('theme_membership_unavailable')
    labels = row.get('sub_themes') if isinstance(row, dict) else None
    if (not isinstance(labels, list) or not 1 <= len(labels) <= 100
            or any(not isinstance(label, str) or not label.strip() or len(label) > 120
                   or not label.isprintable() for label in labels)):
        raise ValueError('theme_membership_unavailable')
    groups = {label.split('·', 1)[0].strip() for label in labels}
    if not all(groups) or UNKNOWN_POOL in groups:
        raise ValueError('theme_membership_unavailable')
    return groups


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate supplement key')
        result[key] = value
    return result


def _aware_time(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('supplement time must be timezone aware')
    return stamp


def _theme_metadata(path, now):
    """Overlay reviewed missing memberships only; never modify pro6e metadata.

    The optional sibling file is local risk metadata, not an order universe or
    price signal. A present but invalid/future supplement blocks admission.
    Existing malformed pro6e rows remain malformed instead of being healed.
    """
    stocks, evidence = read_taxonomy(path, now)
    if not evidence:
        raise ValueError('theme_taxonomy_unavailable')
    supplement_path = path.with_name(SUPPLEMENT_FILENAME)
    try:
        with supplement_path.open('rb') as stream:
            raw = stream.read(64 * 1024 + 1)
    except FileNotFoundError:
        return stocks, evidence, None
    except OSError as exc:
        raise ValueError('theme_supplement_unavailable') from exc
    try:
        if len(raw) > 64 * 1024:
            raise ValueError('oversized supplement')
        data = json.loads(raw, object_pairs_hook=_unique_object)
        created = _aware_time(data['created_at'])
        if (data['schema'] != SUPPLEMENT_SCHEMA or data['metadata_only'] is not True
                or data['scope'] != 'theme_concentration_only'
                or data['order_universe_authorization'] is not False
                or created > now or not isinstance(data['stocks'], dict)
                or not 1 <= len(data['stocks']) <= 100):
            raise ValueError('invalid supplement envelope')
        merged, applied, retained, provenance = dict(stocks), [], [], {}
        for code, row in data['stocks'].items():
            _families(code, {code: row}, 'block')
            if (not isinstance(row.get('name'), str) or not row['name'].strip()
                    or row.get('review_method') != 'official_source_review'
                    or not isinstance(row.get('business_basis'), str)
                    or not row['business_basis'].strip()
                    or not isinstance(row.get('sources'), list)
                    or not 1 <= len(row['sources']) <= 5):
                raise ValueError('missing supplement provenance')
            for source in row['sources']:
                url = urlsplit(source['url'])
                if (url.scheme != 'https' or not url.hostname or url.username or url.password
                        or source['authority'] not in ('official_disclosure', 'official_company')
                        or not isinstance(source['title'], str) or not source['title'].strip()
                        or _aware_time(source['verified_at']) > created):
                    raise ValueError('invalid supplement source')
                if source['authority'] == 'official_disclosure' and url.hostname not in (
                        'kind.krx.co.kr', 'dart.fss.or.kr', 'englishdart.fss.or.kr'):
                    raise ValueError('unrecognized official disclosure source')
            previous = stocks.get(code)
            missing = code not in stocks or (isinstance(previous, dict)
                                              and previous.get('sub_themes') == [])
            if missing:
                merged[code] = {'sub_themes': list(row['sub_themes'])}
                applied.append(code)
                provenance[code] = row
            else:
                retained.append(code)
        supplement = {'schema': SUPPLEMENT_SCHEMA, 'metadata_only': True,
                      'available_at': data['created_at'],
                      'source_sha256': hashlib.sha256(raw).hexdigest(),
                      'applied_symbols': sorted(applied),
                      'original_taxonomy_retained_symbols': sorted(retained),
                      'provenance_by_symbol': provenance,
                      'scope': 'theme_concentration_only'}
        return merged, evidence, supplement
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        raise ValueError('theme_supplement_unavailable') from exc


def _metadata_with_policy(path, now, policy):
    try:
        stocks,evidence,supplement=_theme_metadata(path,now)
        return stocks,evidence,supplement,'available'
    except (ValueError,OSError,TypeError):
        if policy!='allow_unknown':raise
        return {},None,None,'unavailable_treated_as_unknown'


def _families_with_policy(code, stocks, policy):
    try:return _families(code,stocks,policy)
    except ValueError:
        if policy=='allow_unknown' and isinstance(code,str) and re.fullmatch(r'[0-9]{6}',code):
            return {UNKNOWN_POOL}
        raise


def theme_family_snapshot(occupied_symbols, now, *, path=None, unclassified_policy='block'):
    """One metadata read for the whole radar; malformed entries stay blocked."""
    path = Path(path) if path is not None else app_data_root()/"config/research/pro6e_sector_taxonomy.json"
    stocks, evidence, supplement,metadata_status = _metadata_with_policy(path, now, unclassified_policy)
    families, invalid = {}, []
    for code in set(stocks) | set(occupied_symbols):
        try:
            families[code] = sorted(_families_with_policy(code, stocks, unclassified_policy))
        except ValueError:
            if code in occupied_symbols:
                raise
            invalid.append(code)
    return {'themes_by_symbol': families, 'invalid_theme_symbols': sorted(invalid),
            'taxonomy_evidence': evidence, 'supplement_evidence': supplement,'metadata_status':metadata_status,
            'unclassified_policy': unclassified_policy,
            'family_rule': 'text_before_middle_dot_all_memberships'}


def theme_occupancy(symbol, occupied_symbols, now, *, path=None, unclassified_policy='block'):
    path = Path(path) if path is not None else app_data_root()/"config/research/pro6e_sector_taxonomy.json"
    stocks, evidence, supplement,metadata_status = _metadata_with_policy(path, now, unclassified_policy)
    families = {code: _families_with_policy(code, stocks, unclassified_policy)
                for code in set(occupied_symbols) | {symbol}}
    counts = {family: sorted(code for code in set(occupied_symbols) if family in families[code])
              for family in sorted(families[symbol])}
    return {'theme_families': sorted(families[symbol]), 'occupied_by_theme': counts,
            'taxonomy_evidence': evidence, 'supplement_evidence': supplement,'metadata_status':metadata_status,
            'unclassified_policy': unclassified_policy,
            'family_rule': 'text_before_middle_dot_all_memberships'}


def funnel_theme_reason(symbol, snapshot):
    """Pure early admission diagnostic, never authorization to send an order."""
    if not isinstance(snapshot, dict):
        return 'theme_membership_unavailable'
    try:
        occupied = set(snapshot['occupied_symbols'])
        limit = snapshot['theme_limit']
        if type(limit) is not int or not 0 <= limit <= 10:
            raise ValueError
        if symbol in occupied or limit == 0:
            return None
        if symbol in snapshot.get('invalid_theme_symbols', ()):
            raise ValueError
        families = snapshot['themes_by_symbol']
        policy = snapshot['unclassified_policy']
        def groups(code):
            value = families.get(code)
            if value is None and policy in ('shared_bucket','allow_unknown'):
                return {UNKNOWN_POOL}
            if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x for x in value):
                raise ValueError
            return set(value)
        for family in groups(symbol):
            if family == UNKNOWN_POOL and policy == 'allow_unknown':
                continue
            cap = min(limit, 2) if family == UNKNOWN_POOL else limit
            if sum(family in groups(code) for code in occupied) >= cap:
                return 'theme_concentration_limit_reached'
    except (KeyError, ValueError, TypeError):
        return 'theme_membership_unavailable'
    return None
