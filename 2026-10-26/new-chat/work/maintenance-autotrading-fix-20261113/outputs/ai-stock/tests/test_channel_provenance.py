"""Atomic additive collector provenance on an isolated temporary archive."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from kiwoom_autotrader.telegram.channel_store import ChannelStore, ChannelStoreError

class ChannelProvenanceTests(unittest.TestCase):
    def test_atomic_forward_backfill_and_immutability(self):
        with tempfile.TemporaryDirectory() as d:
            with ChannelStore(Path(d),101) as store:
                store.register_channel(11,12,'channel','test')
                post=lambda i:{'message_id':i,'published_at':1000,'edited_at':None,'text':'body'}
                with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=1100):
                    store.commit_page(11,[post(1)],latest_id=1,collection_kind='forward_page',page_index=1,batch_id='a'*32)
                    store.commit_page(11,[post(2)],history_cursor=2,collection_kind='backfill_page',page_index=1,batch_id='b'*32)
                    store.commit_page(11,[post(1)],latest_id=1,collection_kind='forward_page',page_index=1,batch_id='c'*32)
                with sqlite3.connect(store.path) as db:
                    rows=db.execute('SELECT batch_id,batch_kind,page_index,first_seen_at,recorded_at,observed_delay_seconds FROM event_collection_provenance ORDER BY version_id').fetchall()
                    self.assertEqual(rows,[('a'*32,'forward_page',1,1100,1100,100),('b'*32,'backfill_page',1,1100,1100,100)])
                    with self.assertRaises(sqlite3.IntegrityError): db.execute("UPDATE event_collection_provenance SET batch_kind='forward_page'")
                with self.assertRaises(ChannelStoreError):
                    store.commit_page(11,[post(3)],collection_kind='forward_page',page_index=1,batch_id='not-a-batch')
                with sqlite3.connect(store.path) as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM post_versions').fetchone()[0],2)

    def test_clock_rollback_cannot_be_claimed_as_timely(self):
        with tempfile.TemporaryDirectory() as d:
            with ChannelStore(Path(d),101) as store:
                store.register_channel(11,12,'channel','test')
                with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=1100):
                    store.commit_page(11,[{'message_id':1,'published_at':1000,'text':'one'}])
                with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=1050):
                    store.commit_page(11,[{'message_id':2,'published_at':1000,'text':'two'}],
                                      collection_kind='forward_page',page_index=1,batch_id='a'*32)
                with sqlite3.connect(store.path) as db:
                    self.assertEqual(db.execute('SELECT clock_valid FROM event_collection_provenance').fetchone()[0],0)

    def test_second_version_in_same_second_is_not_first(self):
        with tempfile.TemporaryDirectory() as d:
            with ChannelStore(Path(d),101) as store:
                store.register_channel(11,12,'channel','test')
                with patch('kiwoom_autotrader.telegram.channel_store.time.time',return_value=1100):
                    store.commit_page(11,[{'message_id':1,'published_at':1000,'text':'one'}],collection_kind='forward_page',page_index=1,batch_id='a'*32)
                    store.commit_page(11,[{'message_id':1,'published_at':1000,'text':'changed'}],collection_kind='forward_page',page_index=1,batch_id='b'*32)
                with sqlite3.connect(store.path) as db:
                    self.assertEqual(db.execute('SELECT first_version FROM event_collection_provenance ORDER BY version_id').fetchall(),[(1,),(0,)])

if __name__ == '__main__': unittest.main()
