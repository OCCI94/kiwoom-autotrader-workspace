"""SSD daily writes anchored to verified directory descriptors.

Path lookup after a disk is detached must never recreate a local /Volumes tree.
"""
from __future__ import annotations
import hashlib, json, os, re, secrets, stat, threading, time
from pathlib import Path

# Per-process cache only. Restart, a changed manifest/root, metadata change,
# or the verification deadline always forces a full content verification.
_VERIFY_INTERVAL_SECONDS = 300
_VERIFY_CACHE = {}
_VERIFY_LOCK = threading.RLock()

def _file_signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_uid, stat.S_IMODE(info.st_mode))

class AnchoredDataset:
    def __init__(self, store, root: Path, guard):
        self.store=store; self.guard=guard; self.fd=None
        archive=store._external_archive
        expected=archive.directory/'daily'
        if root != expected or archive.directory.parent != archive.mount:
            raise ValueError('invalid external daily location')
        guard(); self.guard()
        flags=os.O_RDONLY | getattr(os,'O_DIRECTORY',0) | os.O_NOFOLLOW
        mountfd=os.open(archive.mount,flags)
        try:
            st=os.fstat(mountfd)
            if not stat.S_ISDIR(st.st_mode) or st.st_dev==os.stat(archive.mount.parent).st_dev:
                raise ValueError('unmounted')
            sfd=os.open('.ssd-volume-sentinel',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=mountfd)
            try:
                if os.read(sfd,129).decode('ascii').strip()!=archive.config['volume_uuid']:raise ValueError('sentinel')
            finally:os.close(sfd)
            afd=os.open(archive.directory.name,flags,dir_fd=mountfd)
            try:
                if os.fstat(afd).st_dev!=st.st_dev:raise ValueError('device changed')
                dfd=os.open('daily',flags,dir_fd=afd)
                if os.fstat(dfd).st_dev!=st.st_dev:
                    os.close(dfd);raise ValueError('device changed')
                self.fd=dfd
            finally:os.close(afd)
        finally:os.close(mountfd)
        try:self.guard()
        except Exception:
            self.close()
            raise

    def close(self):
        if self.fd is not None:os.close(self.fd);self.fd=None

    def _parent(self,relative,create=False):
        self.guard()
        p=Path(relative)
        if p.is_absolute() or len(p.parts) not in (1,2) or any(x in ('.','..') for x in p.parts):raise ValueError('invalid path')
        if len(p.parts)==1:return os.dup(self.fd),p.name
        day=p.parts[0]
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day):raise ValueError('invalid day')
        if create:
            self.guard()
            try:os.mkdir(day,0o700,dir_fd=self.fd)
            except FileExistsError:pass
        self.guard();fd=os.open(day,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|os.O_NOFOLLOW,dir_fd=self.fd)
        if os.fstat(fd).st_dev!=os.fstat(self.fd).st_dev:os.close(fd);raise ValueError('device changed')
        return fd,p.name

    def ensure_day(self,day):
        fd,_=self._parent(day+'/messages.jsonl',True);os.close(fd)

    def _read(self,relative,limit=None):
        try:fd,name=self._parent(relative)
        except FileNotFoundError:return None
        try:
            self.guard()
            try:handle=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
            except FileNotFoundError:return None
            with os.fdopen(handle,'rb') as stream:
                st=os.fstat(stream.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid():raise ValueError('unsafe file')
                return stream.read() if limit is None else stream.read(limit+1)
        finally:os.close(fd)

    def read_manifest(self,limit):
        data=self._read('manifest.json',limit)
        if data is None:return None
        if len(data)>limit:raise ValueError('manifest too long')
        value=json.loads(data)
        if not isinstance(value,dict):raise ValueError('invalid manifest')
        return value if value.get('schema_version')==1 else None

    def _verify_file(self, relative, expected, full):
        try: parent,name=self._parent(relative)
        except FileNotFoundError:return None
        try:
            self.guard()
            try:handle=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent)
            except FileNotFoundError:return None
            with os.fdopen(handle,'rb') as stream:
                before=os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid():
                    raise ValueError('unsafe file')
                if full:
                    digest=hashlib.sha256()
                    while chunk:=stream.read(1024*1024):digest.update(chunk)
                    if digest.hexdigest()!=expected:return None
                after=os.fstat(stream.fileno())
                if _file_signature(before)!=_file_signature(after):return None
                # A concurrent replacement can leave an open old inode intact.
                self.guard()
                entry=os.stat(name,dir_fd=parent,follow_symlinks=False)
                if _file_signature(after)!=_file_signature(entry):return None
                return _file_signature(after)
        except FileNotFoundError:return None
        finally:os.close(parent)

    def files_exist(self,manifest):
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
        self.guard()
        root=os.fstat(self.fd)
        scope=(root.st_dev,root.st_ino,os.getuid(),
               hashlib.sha256(json.dumps({'files':pairs,'dataset_revision':manifest.get('dataset_revision'),
                   'source_identity':manifest.get('source_identity'),'as_of':manifest.get('as_of')},
                   separators=(',',':'),sort_keys=True).encode()).hexdigest())
        with _VERIFY_LOCK:
            cached=_VERIFY_CACHE.get(scope)
            full=cached is None or time.monotonic()-cached[0]>=_VERIFY_INTERVAL_SECONDS
            signatures=[]
            for name,expected in pairs:
                sig=self._verify_file(name,expected,full)
                if sig is None:
                    _VERIFY_CACHE.pop(scope,None)
                    return False
                signatures.append(sig)
            if not full and tuple(signatures)!=cached[1]:
                # Metadata changed: verify all content before treating the
                # cache as valid, and repair through the caller on corruption.
                _VERIFY_CACHE.pop(scope,None)
                return self.files_exist(manifest)
            self.guard()
            if full:
                if len(_VERIFY_CACHE)>=8:_VERIFY_CACHE.clear()
                _VERIFY_CACHE[scope]=(time.monotonic(),tuple(signatures))
            return True

    def write(self,relative,data):
        existing=self._read(relative,len(data))
        if existing==data:return False
        fd,name=self._parent(relative,create=False);temporary='.dataset-'+secrets.token_hex(12)
        try:
            self.guard();handle=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600,dir_fd=fd)
            try:
                with os.fdopen(handle,'wb') as stream:
                    self.guard();stream.write(data);stream.flush();os.fsync(stream.fileno())
                self.guard();os.replace(temporary,name,src_dir_fd=fd,dst_dir_fd=fd)
                temporary=None
            finally:
                if temporary is not None:
                    try:os.unlink(temporary,dir_fd=fd)
                    except FileNotFoundError:pass
            return True
        finally:os.close(fd)

    def remove_old(self,relative):
        try:fd,name=self._parent(relative)
        except FileNotFoundError:return False
        try:
            self.guard()
            try:h=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
            except FileNotFoundError:return False
            try:
                st=os.fstat(h)
                if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid():raise ValueError('unsafe file')
            finally:os.close(h)
            self.guard();os.unlink(name,dir_fd=fd)
        finally:os.close(fd)
        try:self.guard();os.rmdir(Path(relative).parts[0],dir_fd=self.fd)
        except OSError:pass
        return True
