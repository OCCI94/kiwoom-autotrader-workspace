"""Line prefixes for the listener's existing stdout/stderr; no new log sink."""
from datetime import datetime
from functools import wraps
import sys
import threading
from zoneinfo import ZoneInfo


class KSTLineWriter:
    def __init__(self, stream, now=None):
        self.stream=stream; self.now=now or (lambda:datetime.now(ZoneInfo('Asia/Seoul')))
        self._line_start=True; self._lock=threading.RLock()

    def write(self,text):
        if not isinstance(text,str):raise TypeError('text required')
        with self._lock:
            for part in text.splitlines(keepends=True):
                if self._line_start:
                    self.stream.write(self.now().astimezone(ZoneInfo('Asia/Seoul')).strftime('[%Y-%m-%d %H:%M:%S KST] '))
                self.stream.write(part)
                self._line_start=part.endswith(('\n','\r'))
        return len(text)

    def flush(self):
        with self._lock:self.stream.flush()

    def __getattr__(self,name):return getattr(self.stream,name)


def kst_listener_logs(function):
    @wraps(function)
    def wrapped(*args,**kwargs):
        stdout,stderr=sys.stdout,sys.stderr
        sys.stdout=stdout if isinstance(stdout,KSTLineWriter) else KSTLineWriter(stdout)
        sys.stderr=stderr if isinstance(stderr,KSTLineWriter) else KSTLineWriter(stderr)
        try:return function(*args,**kwargs)
        finally:
            sys.stdout.flush();sys.stderr.flush()
            sys.stdout,sys.stderr=stdout,stderr
    return wrapped
