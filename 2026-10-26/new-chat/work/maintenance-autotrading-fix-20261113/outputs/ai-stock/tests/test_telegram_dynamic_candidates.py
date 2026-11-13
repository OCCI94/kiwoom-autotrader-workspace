import json
from datetime import timedelta
from pathlib import Path
import unittest

from kiwoom_autotrader.trading.dynamic_candidates import read_telegram_candidates
from tests import test_dynamic_candidates as dynamic_fixture


class TelegramDynamicTests(unittest.TestCase):
    def setUp(self):
        self.case = dynamic_fixture.DynamicCandidateTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.now = self.case.now
        self.path = Path(self.case.tmp.name) / 'telegram-candidates.json'
        self.case.provider._telegram_candidates_path = self.path

    def candidate(self, code='005930'):
        stamp = (self.now-timedelta(seconds=30)).isoformat()
        return {'code':code, 'name':'검증기업', 'available_at':self.now.isoformat(),
            'last_message_observed_at':stamp,
            'source_hashes':{'message_0':'e'*64, 'master_snapshot':'f'*64},
            'evidence':{'origin':'telegram', 'ingestion_modes':['poll_live'],
                'verification_grade':'provisional_message_context', 'eligible_mentions':1,
                'stance':'unclear', 'negative_or_correction_or_cancel':False,
                'source_published_at':stamp, 'source_edited_at':None,
                'source_extracted_at':self.now.isoformat(), 'master_snapshot_sha256':'f'*64}}

    def feed(self, rows=None):
        return {'schema':'telegram-watch-candidates-v1', 'status':'ready',
            'generated_at':self.now.isoformat(), 'as_of':self.now.isoformat(),
            'expires_at':(self.now+timedelta(minutes=15)).isoformat(),
            'research_only':True, 'tier_1_verified':False, 'no_order_signal':True,
            'candidates':[self.candidate()] if rows is None else rows}

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding='utf-8')
        self.path.chmod(0o600)

    def read(self):
        return read_telegram_candidates(self.path, self.now)

    def test_missing_feed_preserves_market_candidates(self):
        result = self.case.discover(limit=15)
        self.assertEqual([r['symbol'] for r in result['candidates']], ['005930'])
        self.assertEqual(result['telegram_discovery']['status'], 'unavailable')
        self.assertNotIn('telegram_context', result['candidates'][0])

    def test_independent_feed_enrolls_candidates_beyond_market_top_with_same_calls(self):
        codes = [f'{100000+i:06d}' for i in range(40)]
        self.case.surge_rows = [{'stk_cd':c,'flu_rt':'8','sdnin_rt':'150'} for c in codes]
        self.case.turnover_rows = [{'stk_cd':c,'trde_prica':str(100-i)} for i,c in enumerate(codes)]
        self.case.members['0'] = [self.case.member(c) for c in codes]
        self.write(self.feed([self.candidate(c) for c in codes[-6:]]))
        result = self.case.discover(limit=15)
        selected = [r['symbol'] for r in result['candidates']]
        self.assertEqual(selected[:5], codes[-6:-1])
        self.assertEqual(len(selected), 15)
        self.assertEqual(result['telegram_discovery']['newly_enrolled_beyond_market_limit'], 5)
        self.assertEqual(result['telegram_discovery']['eligible_market_candidate_count'], 6)
        self.assertIn({'symbol':codes[-1], 'reason':'telegram_candidate_capacity_deferred'},
                      result['telegram_discovery']['exclusions'])
        self.case.discover(limit=15)
        self.assertEqual(self.case.calls, {'surge':1,'turnover':1,'membership':['0']})

    def test_message_only_missing_market_row_is_explicitly_deferred(self):
        self.write(self.feed([self.candidate('123456')]))
        result = self.case.discover(limit=15)
        self.assertNotIn('123456', [r['symbol'] for r in result['candidates']])
        self.assertIn({'symbol':'123456','reason':'telegram_current_rank_evidence_missing'},
                      result['telegram_discovery']['exclusions'])

    def test_feed_is_context_only_never_s4_disclosure_or_signal(self):
        value = self.feed()
        value['candidates'][0]['evidence']['verification_grade'] = 'matched_source'
        self.write(value)
        row = self.case.discover()['candidates'][0]
        self.assertFalse(row['signal_eligible'])
        self.assertTrue(row['telegram_context']['watch_only'])
        self.assertFalse(row['telegram_context']['s4_disclosure_connected'])
        self.assertEqual(row['candidate_origins'], ['market_ranking','telegram_research'])

    def test_excluded_direction_volume_and_management_cannot_be_promoted(self):
        changes = [('surge','flu_rt','-10'),('surge','sdnin_rt','0'),
                   ('surge','now_trde_qty','0'),('turnover','trde_prica','0'),
                   ('membership','auditInfo','관리종목')]
        for kind,key,value in changes:
            with self.subTest(kind=kind,key=key):
                self.setUp()
                self.write(self.feed())
                target = {'surge':self.case.surge_rows[0], 'turnover':self.case.turnover_rows[0],
                          'membership':self.case.members['0'][0]}[kind]
                target[key] = value
                result = self.case.discover()
                self.assertNotIn('005930', [r['symbol'] for r in result['candidates']])
                self.assertEqual(result['telegram_discovery']['selected_count'], 0)
                self.assertTrue(result['telegram_discovery']['exclusions'])

    def test_name_mismatch_does_not_grant_telegram_priority(self):
        row = self.candidate(); row['name'] = '다른회사'
        self.write(self.feed([row]))
        result = self.case.discover()
        self.assertNotIn('telegram_context', result['candidates'][0])
        self.assertIn({'symbol':'005930','reason':'telegram_current_identity_mismatch'},
                      result['telegram_discovery']['exclusions'])

    def test_current_rank_missing_or_old_gives_no_tg_entry(self):
        self.write(self.feed())
        self.case.provider._surge_fetcher = lambda: {}
        result = self.case.discover()
        self.assertEqual(result['candidates'], [])
        self.assertIn({'symbol':'005930','reason':'telegram_fresh_market_evidence_unavailable'},
                      result['telegram_discovery']['exclusions'])

    def test_feed_future_expired_cross_day_and_excessive_ttl_fail_closed(self):
        mutations = [('generated_at', (self.now+timedelta(seconds=1)).isoformat()),
            ('as_of',(self.now+timedelta(seconds=1)).isoformat()),
            ('expires_at',self.now.isoformat()),
            ('expires_at',(self.now+timedelta(minutes=16)).isoformat()),
            ('generated_at',(self.now-timedelta(days=1)).isoformat())]
        for key,value in mutations:
            with self.subTest(key=key,value=value):
                feed = self.feed(); feed[key] = value; self.write(feed)
                self.assertEqual(self.read()['candidates'], [])
                self.assertEqual(self.read()['reason'], 'feed_stale_or_future')

    def test_backfill_unclear_provenance_and_adverse_messages_are_not_enrolled(self):
        mutations = [('ingestion_modes',['backfill']),('ingestion_modes',['unknown']),
            ('origin','local_unverified'),('negative_or_correction_or_cancel',True),
            ('negative_or_correction_or_cancel',None),('stance','bearish'),
            ('eligible_mentions',True),('eligible_mentions',0),
            ('verification_grade','tier_1_verified'),('master_snapshot_sha256','bad')]
        for key,value in mutations:
            with self.subTest(key=key,value=value):
                row = self.candidate(); row['evidence'][key] = value
                self.write(self.feed([row]))
                self.assertEqual(self.read()['candidates'], [])
                self.assertEqual(self.read()['exclusions'][0]['reason'], 'telegram_provenance_invalid')

    def test_refresh_cannot_extend_stale_message_age_or_replay_yesterday(self):
        for ago in (timedelta(minutes=90,seconds=1),timedelta(days=3)):
            with self.subTest(ago=ago):
                row = self.candidate(); old = (self.now-ago).isoformat()
                row['last_message_observed_at'] = old
                row['evidence']['source_published_at'] = old
                self.write(self.feed([row]))
                self.assertEqual(self.read()['candidates'], [])
                self.assertEqual(self.read()['exclusions'][0]['reason'], 'telegram_message_stale_or_future')

    def test_future_edit_extraction_and_invalid_hash_do_not_pass(self):
        for location,key,value in [('evidence','source_edited_at',(self.now+timedelta(seconds=1)).isoformat()),
                ('evidence','source_extracted_at',(self.now+timedelta(seconds=1)).isoformat()),
                ('root','available_at',(self.now+timedelta(seconds=1)).isoformat()),
                ('root','source_hashes',{'m':'not-a-hash'})]:
            with self.subTest(key=key):
                row = self.candidate(); target = row['evidence'] if location=='evidence' else row
                target[key] = value; self.write(self.feed([row]))
                self.assertEqual(self.read()['candidates'], [])

    def test_duplicate_symbol_or_duplicate_json_key_is_never_chosen(self):
        self.write(self.feed([self.candidate(), self.candidate()]))
        self.assertEqual(self.read()['candidates'], [])
        self.assertEqual(self.read()['exclusions'][0]['reason'], 'telegram_duplicate_symbol')
        self.path.write_text('{"schema":"a","schema":"b"}')
        self.assertEqual(self.read()['candidates'], [])

    def test_revocation_and_wrong_safety_flags(self):
        feed = self.feed(); feed['status'] = 'revoked'; self.write(feed)
        self.assertEqual(self.read()['status'], 'revoked')
        for key in ('research_only','tier_1_verified','no_order_signal'):
            feed = self.feed(); feed[key] = not feed[key]; self.write(feed)
            self.assertEqual(self.read()['reason'], 'feed_schema_invalid')

    def test_bounded_file_and_symlink_checks(self):
        self.path.write_bytes(b' '* (256*1024+1)); self.path.chmod(0o600)
        self.assertEqual(self.read()['candidates'], [])
        self.path.unlink(); other = self.path.with_name('other.json'); other.write_text('{}')
        self.path.symlink_to(other)
        self.assertEqual(self.read()['candidates'], [])


if __name__ == '__main__':
    unittest.main()
