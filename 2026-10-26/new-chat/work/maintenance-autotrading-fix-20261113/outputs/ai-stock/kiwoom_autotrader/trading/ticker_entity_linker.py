"""Offline, exact Korean ticker links with explicit retrospective identity timing.

No network, database writes, aliases, fuzzy matches or historical verification
backdating. Spelling is exact and case-sensitive: no Unicode compatibility,
corporate-prefix stripping, spacing correction or guessed old-name aliases.
㈜ before a name is punctuation; glued 주식회사NAME is not silently rewritten.  This is a research entity interpretation, not a live trading gate.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import re

SCHEMA = 'high-precision-ticker-links-v1'
# These words also have common non-company meanings. Financial context alone
# ("분석 대상 종목의 주가") is never adequate for them.
AMBIGUOUS_NAMES = frozenset({
    '대상', '신성', '가비아', '신원', '한신', '동양', '진양', '한일', '우리',
    '대한', '대원', '대동', '남선', '세방', '성신', '한샘', '보락', '모나미',
    '신세계', '대교', '모비스', '유니온', '인지', '비상', '한창', '신흥', '진로', '신도',
})
PARTICLES = ('으로부터', '에서부터', '으로는', '에서는', '에게는', '으로', '에서',
             '에게', '까지', '부터', '보다', '처럼', '조차', '마저', '하고', '이랑',
             '은', '는', '이', '가', '을', '를', '의', '와', '과', '도', '에', '로', '만')
WORD = r'가-힣A-Za-z0-9_'
PARTICLE = '|'.join(PARTICLES)
FINANCE = re.compile(r'주가|상한가|하한가|특징주|공시|수주|돌파|테마|매수|매도|거래량|거래대금|영업이익|실적|목표가|급등|급락|증권|지분|유상증자|무상증자|시가총액')
URL = re.compile(r'(?:https?://|www\.)\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', re.I)
FEATURE_HEADER = re.compile(r'^.*?특징주\s*종목\s*[:：]\s*(?P<name>[^\n()]+?)\s*\((?P<code>[0-9]{6})\)\s*$', re.M)
CODE = re.compile(r'(?<![\w])(?P<code>[0-9]{6})(?![\w])')


def digest(value):
    return hashlib.sha256(value.encode('utf-8') if isinstance(value, str) else value).hexdigest()


def timestamp(value):
    """Only timezone-aware ISO or explicit Unix timestamps are evidence."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc)
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed if parsed.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def content_availability(message, source):
    """Confirm the exact archived version; publication alone is never enough.

    source.observed_at/recorded_at denote original raw-version preservation,
    not the date this research output was generated. Edited text requires a
    recorded observation of that edit. The source DB remains unchanged.
    """
    for record, fields in ((message, ('published_at', 'collected_at', 'edited_at', 'edit_observed_at', 'available_at')),
                           (source, ('published_at', 'observed_at', 'recorded_at', 'edited_at'))):
        if any(record.get(field) is not None and timestamp(record[field]) is None for field in fields):
            return {'historical_content_available': False, 'reason': 'invalid_or_naive_source_timestamp', 'available_at': None}
    if not isinstance(source.get('text'), str) or digest(source['text']) != message.get('raw_text_sha256'):
        return {'historical_content_available': False, 'reason': 'raw_version_hash_mismatch_or_missing', 'available_at': None}
    published = timestamp(message.get('published_at'))
    raw_published = timestamp(source.get('published_at'))
    collected = timestamp(message.get('collected_at'))
    observed = timestamp(source.get('observed_at'))
    recorded = timestamp(source.get('recorded_at'))
    if not all((published, raw_published, collected, observed)) or published != raw_published:
        return {'historical_content_available': False, 'reason': 'original_version_timestamps_unverified', 'available_at': None}
    if source.get('clock_valid') is False:
        return {'historical_content_available': False, 'reason': 'source_clock_invalid', 'available_at': None}
    edited = timestamp(message.get('edited_at'))
    raw_edited = timestamp(source.get('edited_at'))
    edit_observed = timestamp(message.get('edit_observed_at'))
    if edited != raw_edited:
        return {'historical_content_available': False, 'reason': 'edit_version_mismatch', 'available_at': None}
    if edited and (not edit_observed or edit_observed < edited or observed < edited):
        return {'historical_content_available': False, 'reason': 'edited_content_observation_missing', 'available_at': None}
    if collected < published or observed < published or recorded and recorded < published:
        return {'historical_content_available': False, 'reason': 'source_clock_inconsistent', 'available_at': None}
    preserved = max(x for x in (observed, recorded) if x)
    times = [published, collected, preserved]
    times.extend(x for x in (edited, edit_observed, timestamp(message.get('available_at'))) if x)
    result = max(times)
    return {
        'historical_content_available': True, 'reason': 'exact_archived_version_and_collection_times',
        'published_at': published.isoformat(), 'first_collected_at': collected.isoformat(),
        'edited_at': edited.isoformat() if edited else None,
        'edit_observed_at': edit_observed.isoformat() if edit_observed else None,
        'edited_observed_at': edit_observed.isoformat() if edit_observed else None,
        'source_preserved_at': preserved.isoformat(),
        'original_content_available_at': result.isoformat(), 'available_at': result.isoformat(),
    }


class TickerLinker:
    """Master records must be independently sourced exact code/name pairs.

    Names of <=2 characters and ambiguity-list names require #NAME, a
    parenthesized exact code/name pair, or explicit 종목명: NAME notation.
    Other <=3-character names require financial context in the same sentence.
    Left/right word boundaries include Korean; an explicit finite particle
    list is the only suffix exception. Longer-name overlaps are suppressed.
    """
    def __init__(self, records, *, master_available_at, master_sha256, source):
        if timestamp(master_available_at) is None:
            raise ValueError('timezone-aware master observation required')
        self.master_available_at = master_available_at
        self.master_sha256 = master_sha256
        self.source = source
        pairs = defaultdict(set)
        for record in records:
            code, name = record.get('symbol'), record.get('name')
            if isinstance(code, str) and re.fullmatch('[0-9]{6}', code) and isinstance(name, str) and name.strip() == name and name:
                pairs[code].add(name)
        self.conflicting_codes = {code for code, names in pairs.items() if len(names) != 1}
        self.names = {code: next(iter(names)) for code, names in pairs.items() if len(names) == 1}
        byname = defaultdict(set)
        for code, name in self.names.items():
            byname[name].add(code)
        self.byname = dict(byname)
        # One alternation ensures a protected long company name consumes the
        # token before a shorter constituent company name can be considered.
        alternatives = '|'.join(re.escape(n) for n in sorted(byname, key=lambda n: (-len(n), n)))
        self.name_re = re.compile(r'(?<![' + WORD + r'])(?P<name>' + alternatives + r')(?P<particle>(?:' + PARTICLE + r')?)(?![' + WORD + r'])') if alternatives else None

    @staticmethod
    def _clause(text, start, end):
        lo = max(text.rfind(c, 0, start) for c in ('\n', '.', '!', '?', ';')) + 1
        ends = [x for c in ('\n', '.', '!', '?', ';') if (x := text.find(c, end)) != -1]
        hi = min(ends) if ends else len(text)
        return text[max(lo, start - 80):min(hi, end + 80)]

    def link(self, text):
        if not isinstance(text, str):
            return {'links': [], 'rejected': [{'reason': 'raw_text_missing'}]}
        clean = URL.sub(lambda m: ' ' * len(m.group()), text)
        accepted, rejected = defaultdict(list), []
        # All generated feature-stock headers are masked at once. They can
        # contain upstream substring-match errors or repeat and corroborate
        # each other. Never use a generated header to strengthen a body name.
        headers = list(FEATURE_HEADER.finditer(clean))
        clean = FEATURE_HEADER.sub(lambda m: ' ' * len(m.group()), clean)
        for match in CODE.finditer(clean):
            code = match['code']
            if code not in self.names:
                continue
            before, after = clean[max(0, match.start()-32):match.start()], clean[match.end():match.end()+12]
            # Reject prices/quantities, dates and telephone fragments even if a
            # six-digit number happens to be a valid listed ticker.
            if re.match(r'\s*(?:원|주|년|월|일|%|억|만)(?![' + WORD + r'])', after) or before.endswith(('-', '/', ':')) and not re.search(r'(?:종목코드|코드)\s*:\s*$', before) or re.match(r'[-/][0-9]', after):
                rejected.append({'symbol': code, 'reason': 'numeric_non_ticker_context'}); continue
            explicit = bool(re.search(r'(?:종목코드|티커|코드)\s*[:：]?\s*$', before) or re.search(r'[#(\[]\s*$', before) and re.match(r'\s*[)\]]', after) or before.endswith('#'))
            clause = self._clause(clean, match.start(), match.end())
            standalone = clean.strip() == code
            named = self.names[code] in clause
            if not (explicit or standalone or named or FINANCE.search(clause)):
                rejected.append({'symbol': code, 'reason': 'bare_numeric_ticker_unconfirmed'}); continue
            accepted[code].append({'method': 'exact_code', 'start': match.start(), 'end': match.end(), 'context_rule': 'known_master_code_with_explicit_or_financial_context'})
        if self.name_re:
            for match in self.name_re.finditer(clean):
                name = match['name']; codes = self.byname[name]
                if len(codes) != 1:
                    rejected.append({'name': name, 'reason': 'master_name_collision'}); continue
                code = next(iter(codes))
                before = clean[max(0, match.start()-18):match.start()]
                after = clean[match.start()+len(name):match.start()+len(name)+24]
                # Pairing is local and must refer to this exact company code.
                pair = bool(re.match(r'\s*[\[(]\s*' + re.escape(code) + r'\s*[\])]', after) or re.search(r'[\[(]\s*' + re.escape(code) + r'\s*[\])]\s*$', before))
                explicit_name = before.endswith('#') or bool(re.search(r'(?:종목명|기업명)\s*[:：]\s*$', before)) or pair
                if not re.search('[가-힣]', name):
                    # Latin-only KR names can be US ticker tags (STX=Seagate),
                    # not the identically named Korean issuer. A hashtag or
                    # finance word is insufficient jurisdiction evidence.
                    kr_label = bool(re.search(r'(?:종목명|기업명)\s*[:：]\s*$', before))
                    if not (kr_label or pair or any(p['method']=='exact_code' for p in accepted.get(code, []))):
                        rejected.append({'symbol': code, 'name': name, 'reason': 'latin_name_requires_kr_identity_context'}); continue
                ambiguous = len(name) <= 2 or name in AMBIGUOUS_NAMES
                if ambiguous and not explicit_name:
                    rejected.append({'symbol': code, 'name': name, 'reason': 'ambiguous_name_requires_explicit_entity_marker'}); continue
                if len(name) <= 3 and not explicit_name and not FINANCE.search(self._clause(clean, match.start(), match.end())):
                    rejected.append({'symbol': code, 'name': name, 'reason': 'short_name_missing_local_financial_context'}); continue
                accepted[code].append({'method': 'explicit_ambiguous_name' if ambiguous else 'exact_name', 'start': match.start(), 'end': match.start()+len(name), 'context_rule': 'explicit_entity_marker' if explicit_name else ('short_name_financial_clause' if len(name)<=3 else 'exact_long_company_name')})
        for header in headers:
            rejected.append({'symbol': header['code'], 'name': self.names.get(header['code']),
                             'reason': 'generated_header_masked_body_link_exists' if header['code'] in accepted else 'generated_header_without_independent_body_link'})
        links = []
        for code, proofs in sorted(accepted.items()):
            links.append({'symbol': code, 'name': self.names[code], 'link_status': 'linked',
                          'match_methods': sorted({p['method'] for p in proofs}), 'proofs': proofs,
                          'independent_symbol_link': True, 'identity_mapping_retrospective': True,
                          'master_available_at': self.master_available_at, 'master_source_asof': self.master_available_at,
                          'master_sha256': self.master_sha256, 'master_source': self.source})
        return {'links': links, 'rejected': rejected}


def make_linked_event(link, message, source, *, linked_at, source_message_key):
    if timestamp(linked_at) is None:
        raise ValueError('timezone-aware linkage date required')
    availability = content_availability(message, source)
    # Deliberately omit message body and channel/message identifiers.
    return {k: v for k, v in {**link, **availability,
        'event_id': digest(source_message_key + ':' + message['raw_text_sha256'] + ':' + link['symbol']),
        'message_key_sha256': digest(source_message_key), 'raw_text_sha256': message['raw_text_sha256'],
        'linked_at': linked_at, 'linkage_as_of': linked_at,
        'original_symbol_verified': False, 'original_verification_unchanged': True,
        'strict_historical_binding_verified': False,
        'retrospective_research_only': True,
        'catalyst_direction_verified': False,
        'financial_subject_or_beneficiary_verified': False,
    }.items() if k != 'proofs'}
