"""Conservative, process-local evidence for reusing unchanged raw partitions.

Not a durable index: a restart, externally changed SQLite connection, replaced
DB, old cutoff, missing manifest, future timestamps or unrecognized store all
force the complete exporter.  No message bodies are cached here.
"""
from __future__ import annotations
import hashlib, json, sqlite3, stat, weakref

_STATES = weakref.WeakKeyDictionary()

def _manifest_digest(manifest):
    return hashlib.sha256(json.dumps(manifest,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def _database_state(store):
    from ..telegram.channel_store import ChannelStore
    if type(store) is not ChannelStore: return None
    conn=store._connection()
    info=store.path.stat()
    if not stat.S_ISREG(info.st_mode):return None
    triggers=conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name IN ('post_versions_no_update','post_versions_no_delete') ORDER BY name").fetchall()
    if len(triggers)!=2:return None
    definitions=tuple((r[0],r[1]) for r in triggers)
    expected={'post_versions_no_update':'BEFORE UPDATE ON POST_VERSIONS',
              'post_versions_no_delete':'BEFORE DELETE ON POST_VERSIONS'}
    for name,sql in definitions:
        normalized=' '.join(str(sql).upper().split())
        if expected.get(name) not in normalized or 'RAISE(ABORT' not in normalized:return None
    n,maximum=conn.execute('SELECT count(*),coalesce(max(version_id),0) FROM post_versions').fetchone()
    return {'connection':id(conn),'device':info.st_dev,'inode':info.st_ino,'data_version':conn.execute('PRAGMA data_version').fetchone()[0],
            'count':n,'maximum':maximum,'triggers':definitions,'account_id':store.account_id}

def snapshot(store):
    try: return _database_state(store)
    except (OSError,sqlite3.Error,ValueError,TypeError): return None

def baseline(store,root,manifest,raw_count,raw_max,revision,source_snapshot):
    """Remember a successfully committed manifest, otherwise disable reuse."""
    try:
        db=_database_state(store)
        if db is None or db!=source_snapshot or db['count']!=raw_count or db['maximum']!=raw_max or store.dataset_revision()!=revision:
            if store in _STATES: _STATES[store]={'invalid':True}
            return
        old=_STATES.get(store)
        if old and (old.get('invalid') or any(old['db'][k]!=db[k] for k in ('connection','device','inode','account_id'))):
            _STATES[store]={'invalid':True};return
        _STATES[store]={'db':db,'root':str(root),'manifest':_manifest_digest(manifest)}
    except (OSError,sqlite3.Error,ValueError,TypeError):_STATES.pop(store,None)

def unchanged_source(store, root, manifest, revision):
    """Verify no external mutation/replacement before reusing the whole export."""
    from ..telegram.channel_store import ChannelStore
    # Test doubles and alternate store implementations retain the preexisting
    # full-manifest validation behavior; production uses exactly ChannelStore.
    if type(store) is not ChannelStore: return True
    try:
        state=_STATES.get(store)
        if state is None or state.get('invalid') or state['root']!=str(root) or state['manifest']!=_manifest_digest(manifest):return False
        current=_database_state(store)
        if current is None:return False
        previous=state['db']
        if any(previous[k]!=current[k] for k in ('connection','device','inode','account_id')):
            _STATES[store]={'invalid':True}
            return False
        return current==previous and manifest.get('dataset_revision')==revision and store.dataset_revision()==revision
    except (OSError,sqlite3.Error,ValueError,TypeError):return False

def reusable(store,root,previous,cutoff,explicit,raw_count,raw_max,revision,source_snapshot):
    """Return watermark if append-only proof holds, otherwise fall back."""
    try:
        state=_STATES.get(store)
        if explicit or state is None or state.get('invalid') or previous is None or previous.get('next_available_at') is not None:return None
        if state['root']!=str(root) or state['manifest']!=_manifest_digest(previous):return None
        db=_database_state(store)
        if db is None or db!=source_snapshot or db['count']!=raw_count or db['maximum']!=raw_max or store.dataset_revision()!=revision:return None
        old=state['db']
        for key in ('connection','device','inode','data_version','triggers','account_id'):
            if db[key]!=old[key]:return None
        if db['count']<old['count'] or db['maximum']<old['maximum']:return None
        conn=store._connection()
        new_count=conn.execute('SELECT count(*) FROM post_versions WHERE version_id>?',(old['maximum'],)).fetchone()[0]
        if db['count']-old['count']!=new_count:return None
        return old['maximum']
    except (OSError,sqlite3.Error,ValueError,TypeError):return None

# Durable body-free index. The index is a rebuildable optimization, never an
# authority for the archive or a substitute for manifest/file validation.
_STATE_NAME = '.incremental-export-state.json'
_STATE_LIMIT = 64 * 1024 * 1024
_STATE_VERSION = 2
_SHARD_LIMIT = 4 * 1024 * 1024
_BUCKET_SIZE = 1024

def _event(row):
    from ..trading.message_factors import available_at, symbol_candidates, _URL_PATTERN
    counts = symbol_candidates(row['text'])
    return {'v':row['version_id'], 'c':row['channel_id'], 'm':row['message_id'],
            'p':row['published_at'], 'o':row['observed_at'], 'a':available_at(row),
            'origin':row['origin'], 'chars':len(row['text']),
            'urls':len(_URL_PATTERN.findall(row['text'])),
            'symbols':[[market,symbol,count] for (market,symbol),count in sorted(counts.items())],
            'raw':False}

def _key(event):
    return str(event['c']) + ':' + str(event['m'])

def _factor_day(day, events, first, cutoff):
    from collections import Counter, defaultdict
    from datetime import datetime
    from ..trading.message_factors import KST, kst_datetime, kst_date
    ordered = sorted(events, key=lambda e:(e['a'],e['o'],e['v']))
    current = {}
    for event in ordered: current[_key(event)] = event
    selected = list(current.values())
    mentions, messages, channels = Counter(), Counter(), defaultdict(set)
    symbol_messages = 0
    for event in selected:
        symbols = {(m,s):n for m,s,n in event['symbols']}
        symbol_messages += bool(symbols)
        mentions.update(symbols); messages.update(symbols.keys())
        for candidate in symbols: channels[candidate].add(event['c'])
    end = int(datetime.fromisoformat(day).replace(hour=23,minute=59,second=59,tzinfo=KST).timestamp())
    cut = min(end, cutoff); partial = cutoff < end
    new = sum(first[k] == day for k in current)
    daily = {'date_kst':day, 'cutoff_at':cut, 'cutoff_at_kst':kst_datetime(cut).isoformat(),
       'is_partial_day':partial, 'observed_message_count':len(selected), 'new_message_count':new,
       'updated_message_count':len(selected)-new, 'observed_revision_count':len(events),
       'channel_count':len({e['c'] for e in selected}),
       'prior_day_backfill_count':sum(first[k]==day and kst_date(e['p'])<day for k,e in current.items()),
       'legacy_snapshot_count':sum(e['origin']=='legacy_snapshot' for e in selected),
       'text_character_count':sum(e['chars'] for e in selected), 'url_count':sum(e['urls'] for e in selected),
       'symbol_candidate_message_count':symbol_messages, 'symbol_candidate_mentions':sum(mentions.values()),
       'distinct_symbol_candidates':len(mentions)}
    symbols = [{'date_kst':day,'cutoff_at':cut,'is_partial_day':partial,'market':m,'symbol':s,
                'message_count':messages[(m,s)],'mention_count':mentions[(m,s)],
                'channel_count':len(channels[(m,s)]),'candidate_only':True} for m,s in sorted(mentions)]
    return daily, symbols

def _db_key(store, db):
    conn = store._connection()
    return {k:db[k] for k in ('device','inode','account_id')} | {
        'triggers':[list(pair) for pair in db['triggers']],
        'schema_version':conn.execute('PRAGMA schema_version').fetchone()[0]}

def _encode_state(state):
    raw=json.dumps(state,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    if len(raw)>_STATE_LIMIT: raise ValueError('incremental state too large')
    result=(json.dumps({'payload':state,'sha256':hashlib.sha256(raw).hexdigest()},ensure_ascii=False,
                       sort_keys=True,separators=(',',':'))+'\n').encode()
    if len(result)>_STATE_LIMIT: raise ValueError('incremental state too large')
    return result

def _read_state(root, anchored, guard):
    from .platform_io import os
    try:
        if anchored:
            raw=anchored._read(_STATE_NAME,_STATE_LIMIT)
        else:
            guard(); fd=os.open(root/_STATE_NAME,os.O_RDONLY|os.O_NOFOLLOW)
            with os.fdopen(fd,'rb') as f:
                st=os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid():return None
                raw=f.read(_STATE_LIMIT+1)
        if raw is None or len(raw)>_STATE_LIMIT:return None
        envelope=json.loads(raw); state=envelope['payload']; expected=envelope['sha256']
        data=json.dumps(state,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
        return state if hashlib.sha256(data).hexdigest()==expected and state.get('schema')==_STATE_VERSION else None
    except (OSError,ValueError,TypeError,KeyError,RecursionError):return None

def _write_state(root, anchored, guard, state):
    import os, sys
    from .message_dataset import _write_changed
    data=_encode_state(state)
    if anchored:
        changed=anchored.write(_STATE_NAME,data)
        if changed and sys.platform != 'win32':
            guard(); os.fsync(anchored.fd)
    else:
        changed=_write_changed(root/_STATE_NAME,data,guard)
        if changed and sys.platform != 'win32':
            guard(); fd=os.open(root,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|os.O_NOFOLLOW)
            try:
                info=os.fstat(fd)
                if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid():raise ValueError('unsafe state directory')
                guard(); os.fsync(fd)
            finally:os.close(fd)



def _shard_name(kind, bucket, day=None):
    import re
    if type(bucket) is not int or not 0 <= bucket <= (1 << 63) // _BUCKET_SIZE:
        raise ValueError('invalid shard bucket')
    if kind == 'day' and isinstance(day,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',day):
        return f'.incremental-day-{day}-{bucket}.json'
    if kind == 'pending' and day is None:
        return f'.incremental-pending-{bucket}.json'
    raise ValueError('invalid shard name')


def _shard_write(root, anchored, guard, kind, bucket, events, day=None):
    from .message_dataset import _write_changed
    name=_shard_name(kind,bucket,day)
    events.sort(key=lambda e:(e['a'],e['o'],e['v']))
    data=(json.dumps({'schema':_STATE_VERSION,'kind':kind,'day':day,'bucket':bucket,'events':events},
                     ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()
    if len(data)>_SHARD_LIMIT:raise ValueError('incremental shard too large')
    if anchored:anchored.write(name,data)
    else:_write_changed(root/name,data,guard)
    descriptor={'name':name,'sha256':hashlib.sha256(data).hexdigest(),'count':len(events)}
    if kind=='pending':
        descriptor['next_available']=min((e['a'] for e in events),default=None)
        descriptor['next_observed']=min((e['o'] for e in events if not e['raw']),default=None)
    return descriptor


def _shard_read(root, anchored, guard, descriptor, kind, bucket, day=None):
    from .platform_io import os
    name=_shard_name(kind,bucket,day)
    if not isinstance(descriptor,dict) or descriptor.get('name')!=name:raise ValueError('bad shard index')
    if anchored:raw=anchored._read(name,_SHARD_LIMIT)
    else:
        guard();fd=os.open(root/name,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid():raise ValueError('bad shard file')
            raw=stream.read(_SHARD_LIMIT+1)
    if raw is None or len(raw)>_SHARD_LIMIT or hashlib.sha256(raw).hexdigest()!=descriptor.get('sha256'):
        raise ValueError('bad shard digest')
    data=json.loads(raw)
    if (not isinstance(data,dict) or data.get('schema')!=_STATE_VERSION or data.get('kind')!=kind
            or data.get('day')!=day or data.get('bucket')!=bucket or not isinstance(data.get('events'),list)
            or len(data['events'])!=descriptor.get('count') or len(data['events'])>_BUCKET_SIZE):
        raise ValueError('bad shard payload')
    return data['events']


def _remove_shard(root, anchored, guard, name):
    from .platform_io import os
    import re
    if not isinstance(name,str) or not re.fullmatch(r'\.incremental-(?:day-\d{4}-\d{2}-\d{2}-|pending-)\d+\.json',name):
        raise ValueError('invalid obsolete shard name')
    if anchored:
        anchored.remove_old(name)
    else:
        guard();path=root/name
        try:info=path.lstat()
        except FileNotFoundError:return
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid():raise ValueError('unsafe obsolete shard')
        guard();path.unlink()


def durable_baseline(store, root, anchored, guard, manifest, all_rows, factors, source_snapshot):
    """Persist a small index plus bounded, body-free day/version shards."""
    from ..trading.message_factors import kst_date
    try:
        if _STATES.get(store, {}).get('invalid'):return
        old_state=_read_state(root,anchored,guard)
        old_names=set()
        if old_state is not None:
            for shards in old_state.get('days',{}).values():
                old_names.update(item['name'] for item in shards.values())
            old_names.update(item['name'] for item in old_state.get('pending',{}).values())
        db=_database_state(store)
        if db is None or db!=source_snapshot or db['count']!=len(all_rows) or store.dataset_revision()!=manifest['dataset_revision']:return
        state={'schema':_STATE_VERSION,'db':_db_key(store,db),'count':db['count'],'watermark':db['maximum'],
               'revision':manifest['dataset_revision'],'manifest':_manifest_digest(manifest),'cutoff':manifest['as_of'],
               'days':{},'pending':{},'daily':{r['date_kst']:r for r in factors['daily']},'symbols':{}}
        for row in factors['symbols']:state['symbols'].setdefault(row['date_kst'],[]).append(row)
        grouped={};pending={};first={};versions=0;messages=set();channels=set()
        for row in sorted(all_rows,key=lambda r:(max(r['observed_at'],r['published_at'],r['edited_at'] or 0),r['observed_at'],r['version_id'])):
            event=_event(row);bucket=event['v']//_BUCKET_SIZE
            if event['o']<=manifest['as_of']:
                event['raw']=True;versions+=1;messages.add(_key(event));channels.add(event['c'])
            if event['a']<=manifest['as_of']:
                day=kst_date(event['a']);event['f']=first.setdefault(_key(event),day)
                grouped.setdefault((day,bucket),[]).append(event)
            else:pending.setdefault(bucket,[]).append(event)
        if (versions!=manifest['version_count'] or len(messages)!=manifest['message_count']
                or sorted(channels)!=manifest['channel_ids']):return
        # Initial export writes each bounded shard once. A failed commit leaves
        # the manifest valid and old shards are never trusted under a new hash.
        for (day,bucket),events in sorted(grouped.items()):
            state['days'].setdefault(day,{})[str(bucket)]=_shard_write(root,anchored,guard,'day',bucket,events,day)
        for bucket,events in sorted(pending.items()):
            state['pending'][str(bucket)]=_shard_write(root,anchored,guard,'pending',bucket,events)
        _write_state(root,anchored,guard,state)
        new_names={item['name'] for shards in state['days'].values() for item in shards.values()}
        new_names.update(item['name'] for item in state['pending'].values())
        for name in old_names-new_names:_remove_shard(root,anchored,guard,name)
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,RecursionError):return


def _first_available_day(conn,event,watermark,cutoff):
    from ..trading.message_factors import kst_date
    row=conn.execute('''SELECT min(max(published_at,observed_at,coalesce(edited_at,0)))
        FROM post_versions WHERE channel_id=? AND message_id=? AND version_id<=?
        AND max(published_at,observed_at,coalesce(edited_at,0))<=?''',
        (event['c'],event['m'],watermark,cutoff)).fetchone()
    if row is None or type(row[0]) is not int:raise ValueError('unknown first availability')
    return kst_date(row[0])

def durable_try(store,root,anchored,guard,previous,cutoff,revision,source_identity):
    """Use only new body versions and changed availability/publication days."""
    from datetime import datetime, timedelta
    from ..telegram.channel_store import ChannelStore, _with_permalink
    from ..trading.message_factors import KST, DAILY_FIELDS, SYMBOL_FIELDS, kst_date, kst_datetime, normalize_versions
    from .message_dataset import (_manifest_files_exist, _private_directory, _write_changed,
                                  _csv_bytes, _summary, MessageDatasetError)
    if type(store) is not ChannelStore or previous is None:return None
    if _STATES.get(store, {}).get('invalid'):
        raise MessageDatasetError('채널 원장 파일이 교체되어 저장소를 다시 열어야 합니다.')
    state=_read_state(root,anchored,guard)
    if state is None:return None
    try:
        db=_database_state(store);live=_STATES.get(store)
        if (db is not None and live and not live.get('invalid')
                and live.get('db',{}).get('connection')==db['connection']
                and any(live['db'].get(k)!=db[k] for k in ('device','inode'))):
            _STATES[store]={'invalid':True}
            raise MessageDatasetError('채널 원장 파일이 교체되어 저장소를 다시 열어야 합니다.')
        if (db is None or _db_key(store,db)!=state['db'] or state['manifest']!=_manifest_digest(previous)
            or previous.get('source_identity')!=source_identity or state['cutoff']!=previous.get('as_of')
            or cutoff<state['cutoff'] or db['count']<state['count'] or db['maximum']<state['watermark']
            or previous.get('dataset_revision')!=state['revision']):return None
        if (live and not live.get('invalid') and live.get('db',{}).get('connection')==db['connection']
                and live['db'].get('data_version')!=db['data_version']):return None
        if not verify_manifest_files(root,anchored,guard,previous):return None
        conn=store._connection()
        if conn.execute('PRAGMA foreign_key_check(post_versions)').fetchone() is not None:return None
        raw=conn.execute('''SELECT v.version_id,v.channel_id,v.message_id,v.published_at,v.edited_at,
                    v.text,v.media_type,v.grouped_id,v.observed_at,v.first_observed_at,v.origin,v.title,v.username
                    FROM post_versions v JOIN channels c ON c.channel_id=v.channel_id
                    WHERE v.version_id>? ORDER BY v.observed_at,v.version_id''',(state['watermark'],)).fetchall()
        if len(raw)!=db['count']-state['count']:return None
        rows=normalize_versions([_with_permalink(row) for row in raw])
        if len(rows)!=len(raw) or any(row['observed_at']<state['cutoff'] for row in rows):return None
        for row in rows:
            earliest=conn.execute('SELECT published_at FROM post_versions WHERE channel_id=? AND message_id=? ORDER BY version_id LIMIT 1',
                                  (row['channel_id'],row['message_id'])).fetchone()
            if earliest is None or earliest[0]!=row['published_at']:return None
        events=[_event(row) for row in rows]
        dirty_raw=set();dirty_factor=set();changed_day={};changed_pending={};obsolete=[]
        versions=previous['version_count']; messages=previous['message_count']; channels=set(previous['channel_ids'])
        checked_messages=set(); old_cutoff=state['cutoff']; old_watermark=state['watermark']
        # Load only pending shards with an event becoming raw/available by the
        # new cutoff, plus shards receiving a newly inserted future event.
        buckets={int(e['v']//_BUCKET_SIZE) for e in events if e['a']>cutoff}
        for bucket,descriptor in state['pending'].items():
            if any(type(t) is int and t<=cutoff for t in (descriptor.get('next_observed'),descriptor.get('next_available'))):
                buckets.add(int(bucket))
        active=[]
        for bucket in sorted(buckets):
            descriptor=state['pending'].get(str(bucket))
            old=(_shard_read(root,anchored,guard,descriptor,'pending',bucket) if descriptor else [])
            changed_pending[bucket]=old
            active.extend(old)
        active.extend(events)
        active.sort(key=lambda e:(e['a'],e['o'],e['v']))
        for event in active:
            bucket=event['v']//_BUCKET_SIZE
            if event['o']<=cutoff and not event['raw']:
                event['raw']=True;dirty_raw.add(kst_date(event['p']));versions+=1;channels.add(event['c'])
                key=_key(event)
                if key not in checked_messages:
                    prior=conn.execute('''SELECT 1 FROM post_versions WHERE channel_id=? AND message_id=?
                        AND version_id<=? AND observed_at<=? LIMIT 1''',
                        (event['c'],event['m'],old_watermark,old_cutoff)).fetchone()
                    if prior is None:messages+=1
                    checked_messages.add(key)
            if event['a']<=cutoff:
                day=kst_date(event['a']);event['f']=_first_available_day(conn,event,db['maximum'],cutoff)
                dirty_factor.add(day)
                key=(day,bucket)
                if key not in changed_day:
                    descriptor=state['days'].get(day,{}).get(str(bucket))
                    changed_day[key]=(_shard_read(root,anchored,guard,descriptor,'day',bucket,day) if descriptor else [])
                changed_day[key].append(event)
            else:
                changed_pending.setdefault(bucket,[])
        # Reconstruct processed pending buckets from active events. Untouched
        # pending shards remain indexed without being loaded or serialized.
        for bucket in buckets:
            remaining=[e for e in active if e['v']//_BUCKET_SIZE==bucket and e['a']>cutoff]
            changed_pending[bucket]=remaining
        if cutoff!=old_cutoff:
            old_day=kst_date(old_cutoff)
            if old_day in state['daily'] and old_day not in dirty_factor:
                # No evidence changed on this day: only the partial-day
                # cutoff fields change. Avoid re-reading a whole busy day.
                end=int(datetime.fromisoformat(old_day).replace(hour=23,minute=59,second=59,tzinfo=KST).timestamp())
                cut=min(end,cutoff); partial=cutoff<end
                state['daily'][old_day].update(cutoff_at=cut,cutoff_at_kst=kst_datetime(cut).isoformat(),is_partial_day=partial)
                for item in state['symbols'].get(old_day,[]):item.update(cutoff_at=cut,is_partial_day=partial)
        for (day,bucket),day_events in sorted(changed_day.items()):
            state['days'].setdefault(day,{})[str(bucket)]=_shard_write(root,anchored,guard,'day',bucket,day_events,day)
        for bucket,items in sorted(changed_pending.items()):
            if items:state['pending'][str(bucket)]=_shard_write(root,anchored,guard,'pending',bucket,items)
            else:
                old=state['pending'].pop(str(bucket),None)
                if old:obsolete.append(old['name'])
        for day in sorted(dirty_factor):
            day_events=[]
            for bucket,descriptor in sorted(state['days'][day].items(),key=lambda x:int(x[0])):
                day_events.extend(_shard_read(root,anchored,guard,descriptor,'day',int(bucket),day))
            first={_key(e):e['f'] for e in day_events}
            daily,symbols=_factor_day(day,day_events,first,cutoff)
            state['daily'][day]=daily
            if symbols:state['symbols'][day]=symbols
            else:state['symbols'].pop(day,None)
        previous_files={x['path']:x for x in previous['files']}
        new_files=[];changed=0
        for day in sorted(set(previous.get('publication_days',[]))|dirty_raw):
            relative=day+'/messages.jsonl'
            if day not in dirty_raw:new_files.append(previous_files[relative]);continue
            start=datetime.fromisoformat(day).replace(tzinfo=KST);end=start+timedelta(days=1)
            selected=conn.execute('''SELECT v.version_id,v.channel_id,v.message_id,v.published_at,v.edited_at,
                   v.text,v.media_type,v.grouped_id,v.observed_at,v.first_observed_at,v.origin,v.title,v.username
                   FROM post_versions v JOIN channels c ON c.channel_id=v.channel_id
                   WHERE v.published_at>=? AND v.published_at<? AND v.observed_at<=?
                   ORDER BY v.observed_at,v.version_id''',(int(start.timestamp()),int(end.timestamp()),cutoff)).fetchall()
            versions_rows=normalize_versions([_with_permalink(row) for row in selected]);ordered=[]
            for row in versions_rows:
                available=max(row['observed_at'],row['published_at'],row['edited_at'] or 0)
                ordered.append({**row,'published_at_kst':kst_datetime(row['published_at']).isoformat(),
                    'observed_at_kst':kst_datetime(row['observed_at']).isoformat(),'available_at':available,
                    'available_at_kst':kst_datetime(available).isoformat(),
                    'publication_date_kst':kst_date(row['published_at']),'availability_date_kst':kst_date(available)})
            ordered.sort(key=lambda row:(row['published_at'],row['channel_id'],row['message_id'],row['observed_at'],row['version_id']))
            data=b''.join((json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n').encode() for row in ordered)
            if anchored:anchored.ensure_day(day);changed+=anchored.write(relative,data)
            else:_private_directory(root/day,guard);changed+=_write_changed(root/relative,data,guard)
            new_files.append({'path':relative,'sha256':hashlib.sha256(data).hexdigest(),'rows':len(ordered)})
        for name,source,fields in [('daily_factors.csv','daily',DAILY_FIELDS),('symbol_factors.csv','symbols',SYMBOL_FIELDS)]:
            data_rows=([state[source][day] for day in sorted(state[source])] if source=='daily' else
                       [row for day in sorted(state[source]) for row in state[source][day]])
            data=_csv_bytes(data_rows,fields)
            changed += anchored.write(name,data) if anchored else _write_changed(root/name,data,guard)
            new_files.append({'path':name,'sha256':hashlib.sha256(data).hexdigest(),'rows':len(data_rows)})
        manifest=dict(previous)
        next_available=min((d['next_available'] for d in state['pending'].values()),default=None)
        manifest.update(as_of=cutoff,as_of_kst=kst_datetime(cutoff).isoformat(),dataset_revision=revision,
            next_available_at=next_available,channel_ids=sorted(channels),version_count=versions,message_count=messages,
            publication_days=sorted(set(previous.get('publication_days',[]))|dirty_raw),
            factor_days=len(state['daily']),files=new_files)
        if _database_state(store)!=db or store.dataset_revision()!=revision:
            raise MessageDatasetError('채널 데이터가 저장 중 변경되어 증분 내보내기를 중단했습니다.')
        data=(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode()
        changed += anchored.write('manifest.json',data) if anchored else _write_changed(root/'manifest.json',data,guard)
        state.update(db=_db_key(store,db),count=db['count'],watermark=db['maximum'],revision=revision,
                     manifest=_manifest_digest(manifest),cutoff=cutoff)
        _write_state(root,anchored,guard,state)
        for name in obsolete:_remove_shard(root,anchored,guard,name)
        baseline(store,root,manifest,db['count'],db['maximum'],revision,db)
        result=_summary(root,manifest,changed,False)
        result.update(reused_raw_partitions=len(manifest['publication_days'])-len(dirty_raw),
                      recomputed_factor_days=len(dirty_factor),incremental=True)
        return result
    except MessageDatasetError:raise
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,IndexError,OverflowError,RecursionError):return None

# Signature-only verification cache: never caches message bodies. Any change
# to inode/device/size/mtime/ctime/owner/mode or a bounded TTL forces SHA-256.
# Each access still invokes the SSD guard and safely opens/stats the target.
import threading as _threading
import time as _time
_FILE_VERIFY_LOCK = _threading.RLock()
_FILE_VERIFY_CACHE = {}
_VERIFY_TTL = 300

def _local_verify_file(root,name,expected,full,guard):
    from .platform_io import os
    from .anchored_dataset import _file_signature
    import sys
    guard();path=root/name; rootfd=None; dayfd=None
    try:
        if sys.platform != 'win32':
            flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|os.O_NOFOLLOW
            rootfd=os.open(root,flags); directory=rootfd
            if '/' in name:
                day,leaf=name.split('/',1)
                dayfd=os.open(day,flags,dir_fd=rootfd); directory=dayfd
            else:leaf=name
            fd=os.open(leaf,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
        else:
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            before=os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid():return None
            if full:
                digest=hashlib.sha256()
                while chunk:=stream.read(1024*1024):digest.update(chunk)
                if digest.hexdigest()!=expected:return None
            after=os.fstat(stream.fileno())
            if _file_signature(before)!=_file_signature(after):return None
            guard()
            if rootfd is not None:
                root_open=os.fstat(rootfd);root_path=root.lstat()
                if (root_open.st_dev,root_open.st_ino)!=(root_path.st_dev,root_path.st_ino):return None
                if dayfd is not None:
                    day_open=os.fstat(dayfd);day_path=os.stat(day,dir_fd=rootfd,follow_symlinks=False)
                    if (not stat.S_ISDIR(day_path.st_mode) or day_path.st_uid!=os.getuid()
                            or (day_open.st_dev,day_open.st_ino)!=(day_path.st_dev,day_path.st_ino)):return None
                entry=os.stat(leaf,dir_fd=directory,follow_symlinks=False)
            else:entry=path.lstat()
            return _file_signature(after) if _file_signature(after)==_file_signature(entry) else None
    except (FileNotFoundError,NotADirectoryError):return None
    finally:
        if dayfd is not None:os.close(dayfd)
        if rootfd is not None:os.close(rootfd)


def verify_manifest_files(root,anchored,guard,manifest):
    """Validate all manifest files while rehashing only changed/expired files."""
    import re
    from .platform_io import os
    files=manifest.get('files')
    if not isinstance(files,list):return False
    pairs=[]
    for item in files:
        if not isinstance(item,dict):return False
        name=item.get('path'); expected=item.get('sha256')
        if name not in ('daily_factors.csv','symbol_factors.csv') and not (isinstance(name,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}/messages\.jsonl',name)):return False
        if not isinstance(expected,str) or not re.fullmatch(r'[0-9a-f]{64}',expected):return False
        pairs.append((name,expected))
    if len({name for name,_ in pairs})!=len(pairs):return False
    guard()
    info=os.fstat(anchored.fd) if anchored else root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid():return False
    rootkey=(info.st_dev,info.st_ino,info.st_uid)
    with _FILE_VERIFY_LOCK:
        if len(_FILE_VERIFY_CACHE)>20000:_FILE_VERIFY_CACHE.clear()
        for name,expected in pairs:
            key=(*rootkey,name,expected);old=_FILE_VERIFY_CACHE.get(key);now=_time.monotonic()
            full=old is None or now-old[0]>=_VERIFY_TTL
            def check(force):
                return anchored._verify_file(name,expected,force) if anchored else _local_verify_file(root,name,expected,force,guard)
            signature=check(full)
            if signature is None:
                _FILE_VERIFY_CACHE.pop(key,None);return False
            if not full and signature!=old[1]:
                signature=check(True);full=True
                if signature is None:
                    _FILE_VERIFY_CACHE.pop(key,None);return False
            if full:_FILE_VERIFY_CACHE[key]=(now,signature)
        guard()
    return True
