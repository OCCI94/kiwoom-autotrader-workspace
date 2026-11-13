"""Local, paper-only read-only broker proxy. Tokens never leave this process.

A single background worker accepts one bounded request at a time. It cannot
submit orders or log in, and uses the receiver's existing BrokerSession and
shared HTTP queue. This socket is not an account/session administration API.
"""
from __future__ import annotations

import ctypes
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import sys
import threading
import time
import urllib.request

from kiwoom_autotrader.api.session import BrokerSession
from kiwoom_autotrader.utils.http_queue import queued_open

MAX_REQUEST = 16384
MAX_WIRE = 2 * 1024 * 1024
MAX_RESPONSE = 3 * 1024 * 1024
MAX_ROWS = 10000
_TIMEOUT = 8
_LISTEN_BACKLOG = 4
SOCKET_NAME = "research.sock"

ENDPOINTS = {"ka10017": "/api/dostk/stkinfo", "ka10023": "/api/dostk/rkinfo",
             "ka10081": "/api/dostk/chart", "ka10099": "/api/dostk/stkinfo",
             "ka10004": "/api/dostk/mrkcond", "ka10080": "/api/dostk/chart"}
OUTPUTS = {"ka10017": "updown_pric", "ka10023": "trde_qty_sdnin",
           "ka10081": "stk_dt_pole_chart_qry", "ka10099": "list",
           "ka10004": None, "ka10080": "stk_min_pole_chart_qry"}
FIELDS = {
    "ka10017": "stk_cd stk_infr stk_nm cur_prc pred_pre_sig pred_pre flu_rt trde_qty pred_trde_qty sel_req sel_bid buy_bid buy_req cnt".split(),
    "ka10023": "stk_cd stk_nm cur_prc pred_pre_sig pred_pre flu_rt prev_trde_qty now_trde_qty sdnin_qty sdnin_rt".split(),
    "ka10081": "cur_prc trde_qty trde_prica dt open_pric high_pric low_pric pred_pre pred_pre_sig trde_tern_rt upd_stkpc_tp upd_rt".split(),
    "ka10099": "code name listCount auditInfo regDay lastPrice state marketCode marketName upName upSizeName orderWarning companyClassName nxtEnable".split(),
    "ka10004": "bid_req_base_tm sel_fpr_req sel_fpr_bid buy_fpr_bid buy_fpr_req".split(),
    "ka10080": "cur_prc trde_qty cntr_tm open_pric high_pric low_pric acc_trde_qty pred_pre pred_pre_sig upd_stkpc_tp upd_rt".split(),
}
UPPER = {"mrkt_tp":"000", "updown_tp":"1", "sort_tp":"1", "stk_cnd":"0", "trde_qty_tp":"00000", "crd_cnd":"0", "trde_gold_tp":"0", "stex_tp":"1"}
SURGE = {"mrkt_tp":"000", "sort_tp":"2", "tm_tp":"2", "trde_qty_tp":"5", "tm":"", "stk_cnd":"0", "pric_tp":"0", "stex_tp":"1"}
MASTER_CODES = frozenset(("0", "10", "8", "60", "70", "90", "3", "6", "2", "4", "5", "7", "9", "30", "50", "80"))

class ResearchBridgeError(ValueError):
    pass

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _peer_uid(sock):
    """Use kernel credentials; unrecognized systems fail closed."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.getpeereid
        fn.argtypes = (ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint))
        fn.restype = ctypes.c_int
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        if fn(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            raise ResearchBridgeError("peer_unavailable")
        return uid.value
    if sys.platform.startswith("linux") and hasattr(socket, "SO_PEERCRED"):
        _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        return uid
    raise ResearchBridgeError("peer_unavailable")


def _validate(req):
    if not isinstance(req, dict) or set(req) - {"api_id", "body", "cont_yn", "next_key", "session_id", "deadline_ns", "max_pages", "stop_before_dt"}:
        raise ResearchBridgeError("invalid_request")
    api, body = req.get("api_id"), req.get("body")
    if api not in ENDPOINTS or not isinstance(body, dict) or any(type(k) is not str or type(v) is not str for k,v in body.items()):
        raise ResearchBridgeError("invalid_request")
    if api == "ka10017" and body != UPPER or api == "ka10023" and body != SURGE:
        raise ResearchBridgeError("invalid_request")
    if api == "ka10099" and (set(body) != {"mrkt_tp"} or body.get("mrkt_tp") not in MASTER_CODES):
        raise ResearchBridgeError("invalid_request")
    if api == "ka10004" and (set(body) != {"stk_cd"} or re.fullmatch(r"[0-9A-Z]{6}",body.get("stk_cd","")) is None):
        raise ResearchBridgeError("invalid_request")
    if api == "ka10080" and (set(body) != {"stk_cd","tic_scope","upd_stkpc_tp"}
            or re.fullmatch(r"[0-9A-Z]{6}",body.get("stk_cd","")) is None
            or body.get("tic_scope") != "1" or body.get("upd_stkpc_tp") != "0"):
        raise ResearchBridgeError("invalid_request")
    if api == "ka10081":
        if (set(body) != {"stk_cd","base_dt","upd_stkpc_tp"} or re.fullmatch(r"[0-9A-Z]{6}",body.get("stk_cd","")) is None
                or re.fullmatch(r"[0-9]{8}", body.get("base_dt","")) is None or body.get("upd_stkpc_tp") != "0"):
            raise ResearchBridgeError("invalid_request")
        try:
            dt.datetime.strptime(body["base_dt"], "%Y%m%d")
        except ValueError:
            raise ResearchBridgeError("invalid_request") from None
    pages = req.get("max_pages", 1)
    cutoff = req.get("stop_before_dt")
    if type(pages) is not int or not 1 <= pages <= 5 or (api != "ka10080" and ("max_pages" in req or "stop_before_dt" in req)):
        raise ResearchBridgeError("invalid_request")
    if cutoff is not None:
        if type(cutoff) is not str or re.fullmatch(r"[0-9]{14}", cutoff) is None:
            raise ResearchBridgeError("invalid_request")
        try: dt.datetime.strptime(cutoff, "%Y%m%d%H%M%S")
        except ValueError: raise ResearchBridgeError("invalid_request") from None
    if pages > 1 and req.get("cont_yn") != "N":
        raise ResearchBridgeError("invalid_request")
    cont, key, session_id = req.get("cont_yn"), req.get("next_key"), req.get("session_id")
    deadline_ns = req.get("deadline_ns")
    if type(deadline_ns) is not int or deadline_ns <= 0:
        raise ResearchBridgeError("invalid_request")
    if cont not in ("N","Y") or not isinstance(key,str) or len(key)>50 or not key.isascii() or (key and not key.isprintable()):
        raise ResearchBridgeError("invalid_request")
    if cont == "N" and key or cont == "Y" and (not key or session_id is None):
        raise ResearchBridgeError("invalid_request")
    if api == "ka10004" and (cont != "N" or key):
        raise ResearchBridgeError("invalid_request")
    if session_id is not None and (not isinstance(session_id,str) or re.fullmatch(r"[0-9a-f]{32}",session_id) is None):
        raise ResearchBridgeError("invalid_request")
    return api, body, cont, key, session_id, deadline_ns


def _read_n(sock, size):
    parts = []
    while size:
        block = sock.recv(size)
        if not block:
            raise ResearchBridgeError("truncated_request")
        parts.append(block); size -= len(block)
    return b"".join(parts)


def _recv(sock):
    length = struct.unpack("!I",_read_n(sock,4))[0]
    if not 0 < length <= MAX_REQUEST:
        raise ResearchBridgeError("invalid_request")
    def unique(pairs):
        result={}
        for k,v in pairs:
            if k in result: raise ValueError
            result[k]=v
        return result
    try:
        return json.loads(_read_n(sock,length).decode("utf-8"),object_pairs_hook=unique)
    except (ValueError,UnicodeError,RecursionError):
        raise ResearchBridgeError("invalid_request") from None


def _send(sock, payload):
    raw=json.dumps(payload,ensure_ascii=True,separators=(",",":"),allow_nan=False).encode("utf-8")
    if len(raw)>MAX_RESPONSE: raise ResearchBridgeError("response_too_large")
    sock.sendall(struct.pack("!I",len(raw))+raw)


def _safe_text(value, token):
    if value is None: return None
    if value == "": return ""
    if not isinstance(value,str) or len(value)>256 or not value.isprintable() or token in value:
        return None
    return value


class PaperResearchServer:
    """Background endpoint bound to one receiver and one in-memory session."""
    def __init__(self, session, state_dir, *, open_http=None, peer_uid=None):
        if not isinstance(session,BrokerSession): raise ValueError("broker session required")
        self._session=session; self._dir=Path(state_dir); self.path=self._dir/SOCKET_NAME
        self._open=open_http or queued_open; self._peer_uid=peer_uid or _peer_uid
        self._last_response_at = {}
        self._stop=threading.Event(); self._sock=None; self._thread=None; self._active=None
        self._identity=None; self._issued_ref=None; self._session_id=None; self._socket_identity=None

    def _current(self):
        # Never block the shared queue's dispatcher on a login/renewal. The
        # issued object is retained in the tuple to prevent object-id reuse.
        lock=self._session._auth_lock
        if not lock.acquire(blocking=False):
            raise ResearchBridgeError("paper_session_unavailable")
        try:
            issued=self._session._issued
            token=issued.token if issued is not None else None
            if (self._stop.is_set() or self._session.mode != "paper" or not self._session.authenticated
                    or not self._session.token_usable or not isinstance(token,str) or not token):
                raise ResearchBridgeError("paper_session_unavailable")
            return self._session.mode,id(issued),token,issued
        finally:
            lock.release()

    def _session_for(self, supplied):
        identity=self._current()
        if self._identity != identity:
            self._identity=identity; self._session_id=secrets.token_hex(16)
        if supplied is not None and supplied != self._session_id:
            raise ResearchBridgeError("session_changed")
        return identity,self._session_id

    def _assert_current(self, identity):
        if self._current() != identity: raise ResearchBridgeError("session_changed")

    def _exchange(self, req):
        api, _, _, _, _, _ = _validate(req)
        pages = req.get("max_pages", 1)
        cutoff = req.get("stop_before_dt")
        if api != "ka10080" or (pages == 1 and cutoff is None):
            return self._exchange_single(req)
        current = dict(req)
        current.pop("max_pages", None); current.pop("stop_before_dt", None)
        seen_keys = set(); rows = {}; evidence = []; result = None
        for _ in range(pages):
            result = self._exchange_single(current)
            evidence.append({"sha256": result["wire_sha256"], "bytes": result["wire_bytes"]})
            for row in result["body"][OUTPUTS[api]]:
                key = row.get("cntr_tm")
                if not isinstance(key, str) or re.fullmatch(r"[0-9]{14}", key) is None:
                    raise ResearchBridgeError("broker_response_invalid")
                if key in rows and rows[key] != row:
                    raise ResearchBridgeError("broker_response_invalid")
                rows[key] = row
            if cutoff is not None and rows and min(rows) <= cutoff: break
            if result["headers"]["cont-yn"] != "Y": break
            next_key = result["headers"]["next-key"]
            if not next_key or next_key in seen_keys: raise ResearchBridgeError("broker_response_invalid")
            seen_keys.add(next_key)
            current.update(cont_yn="Y", next_key=next_key, session_id=result["session_id"])
        result["body"][OUTPUTS[api]] = [rows[key] for key in sorted(rows, reverse=True)]
        result["wire_pages"] = evidence
        result["wire_bytes"] = sum(page["bytes"] for page in evidence)
        result["wire_sha256"] = hashlib.sha256("".join(page["sha256"] for page in evidence).encode("ascii")).hexdigest()
        result["pages_fetched"] = len(evidence)
        result["cutoff_reached"] = bool(cutoff is not None and rows and min(rows) <= cutoff)
        return result

    def _exchange_single(self, req):
        api, body, cont, key, session_id, client_deadline_ns = _validate(req)
        now_ns=time.monotonic_ns()
        if client_deadline_ns <= now_ns: raise ResearchBridgeError("request_expired")
        deadline_ns=min(client_deadline_ns, now_ns + _TIMEOUT * 1_000_000_000)
        identity, epoch = self._session_for(session_id)
        token=identity[2]
        if token in key: raise ResearchBridgeError("invalid_request")
        def guard():
            if time.monotonic_ns() >= deadline_ns: raise ResearchBridgeError("request_expired")
            self._assert_current(identity)
        request=urllib.request.Request("https://mockapi.kiwoom.com"+ENDPOINTS[api],
            data=json.dumps(body,ensure_ascii=True,separators=(",",":")).encode(),method="POST",
            headers={"Content-Type":"application/json;charset=UTF-8","authorization":"Bearer "+token,
                     "api-id":api,"cont-yn":cont,"next-key":key})
        # Pace here, before entering the shared FIFO: waiting on our own TR
        # lease inside its dispatcher would delay unrelated trading requests.
        delay = self._last_response_at.get(api, -1e30) + 1.10 - time.monotonic()
        if delay > 0 and self._stop.wait(delay):
            raise ResearchBridgeError("session_changed")
        guard()
        try:
            with self._open(urllib.request.build_opener(_NoRedirect()),request,timeout=_TIMEOUT,
                            max_bytes=MAX_WIRE,before_send=guard,priority="research") as response:
                raw=response.read(MAX_WIRE+1); headers=response.headers; status=getattr(response,"status",200)
        finally:
            self._last_response_at[api]=time.monotonic()
        guard()
        if not isinstance(raw,bytes) or len(raw)>MAX_WIRE or status != 200: raise ResearchBridgeError("broker_response_invalid")
        def unique(pairs):
            out={}
            for k,v in pairs:
                if k in out: raise ValueError
                out[k]=v
            return out
        try: payload=json.loads(raw.decode("utf-8"),object_pairs_hook=unique)
        except (UnicodeError,ValueError,RecursionError): raise ResearchBridgeError("broker_response_invalid") from None
        if not isinstance(payload,dict): raise ResearchBridgeError("broker_response_invalid")
        rc=payload.get("return_code")
        if type(rc) is int and -999999<=rc<=999999: pass
        elif isinstance(rc,str) and re.fullmatch(r"-?[0-9]{1,6}",rc): pass
        else: raise ResearchBridgeError("broker_response_invalid")
        successful=rc in (0,"0")
        if not successful: raise ResearchBridgeError("broker_rejected")
        out={"return_code":rc}
        if successful:
            if api == "ka10004":
                # ka10004's documented response is a top-level object; it has
                # no broker-supplied symbol. Only project explicitly listed
                # quote fields and label the requested code separately.
                out.update({field:_safe_text(payload[field],token) for field in FIELDS[api] if field in payload})
                out["requested_stk_cd"]=body["stk_cd"]
            else:
                rows=payload.get(OUTPUTS[api]);
                if not isinstance(rows,list) or len(rows)>MAX_ROWS or any(not isinstance(row,dict) for row in rows):
                    raise ResearchBridgeError("broker_response_invalid")
                out[OUTPUTS[api]]=[{field:_safe_text(row[field],token) for field in FIELDS[api] if field in row} for row in rows]
            if api in ("ka10081", "ka10080"):
                out["stk_cd"]=_safe_text(payload.get("stk_cd"),token)
                if out["stk_cd"] != body["stk_cd"]: raise ResearchBridgeError("broker_response_invalid")
        api_header=headers.get("api-id");yn=headers.get("cont-yn");next_key=headers.get("next-key","")
        if api_header not in (None,"",api) or yn not in (None,"","Y","N") or not isinstance(next_key,str) or len(next_key)>50 or token in next_key:
            raise ResearchBridgeError("broker_response_invalid")
        if next_key and (not next_key.isascii() or not next_key.isprintable()): raise ResearchBridgeError("broker_response_invalid")
        guard()
        return {"ok":True,"status":200,"headers":{"api-id":api_header or "","cont-yn":yn or "","next-key":next_key},
                "body":out,"session_id":epoch,"wire_sha256":hashlib.sha256(raw).hexdigest(),"wire_bytes":len(raw)}

    def _handle(self, conn):
        conn.settimeout(3)
        if self._peer_uid(conn) != os.getuid(): raise ResearchBridgeError("peer_rejected")
        req=_recv(conn)
        conn.settimeout(_TIMEOUT+3)
        _send(conn,self._exchange(req))

    def _serve(self):
        while not self._stop.is_set():
            listener=self._sock
            if listener is None: break
            try: conn,_=listener.accept()
            except socket.timeout: continue
            except OSError: break
            self._active=conn
            try:
                self._handle(conn)
            except Exception as error:
                code=str(error) if isinstance(error,ResearchBridgeError) else "request_failed"
                if code not in {"invalid_request","truncated_request","peer_unavailable","peer_rejected","paper_session_unavailable","session_changed","broker_response_invalid","response_too_large","request_expired","broker_rejected"}:
                    code="request_failed"
                try:_send(conn,{"ok":False,"error":code})
                except Exception:pass
            finally:
                self._active=None
                try:conn.close()
                except OSError:pass

    def start(self):
        if self._thread is not None: raise ValueError("already started")
        self._dir.mkdir(mode=0o700,parents=True,exist_ok=True)
        info=self._dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
            raise ResearchBridgeError("unsafe_directory")
        try: old=self.path.lstat()
        except FileNotFoundError: old=None
        if old is not None:
            if not stat.S_ISSOCK(old.st_mode) or old.st_uid != os.getuid(): raise ResearchBridgeError("unsafe_socket")
            # Never unlink a live endpoint, even if it belongs to the same uid.
            # Only a verified refused stale socket may be removed.
            probe=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            try:
                probe.settimeout(0.2)
                try:
                    probe.connect(str(self.path))
                except ConnectionRefusedError:
                    check=self.path.lstat()
                    if (check.st_dev,check.st_ino)!=(old.st_dev,old.st_ino): raise ResearchBridgeError("unsafe_socket")
                    self.path.unlink()
                else:
                    raise ResearchBridgeError("unsafe_socket")
            finally:
                probe.close()
        sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        bound=False
        try:
            # A same-uid startup liveness probe connects then closes. Until
            # this serial worker accepts it, that dead probe occupies a queue
            # slot. Keep a small bounded wait queue so one probe cannot refuse
            # the next legitimate client merely because accept is unscheduled.
            sock.bind(str(self.path)); bound=True; os.chmod(self.path,0o600); sock.listen(_LISTEN_BACKLOG);sock.settimeout(0.5)
            info=self.path.lstat();self._socket_identity=(info.st_dev,info.st_ino)
            self._sock=sock; self._thread=threading.Thread(target=self._serve,name="paper-research-ipc",daemon=True);self._thread.start()
        except Exception:
            sock.close()
            if bound:
                try:
                    info=self.path.lstat()
                    if stat.S_ISSOCK(info.st_mode) and info.st_uid==os.getuid():self.path.unlink()
                except OSError:pass
            raise

    def stop(self):
        self._stop.set()
        sock,self._sock=self._sock,None
        if sock is not None:
            try:sock.close()
            except OSError:pass
        conn=self._active
        if conn is not None:
            try:conn.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        if self._thread is not None:self._thread.join(timeout=2)
        try:
            info=self.path.lstat()
            if stat.S_ISSOCK(info.st_mode) and (info.st_dev,info.st_ino)==self._socket_identity:self.path.unlink()
        except FileNotFoundError:pass
