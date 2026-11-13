"""Loopback-only personal-account setup; credentials never enter access logs."""

import asyncio
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from kiwoom_autotrader.utils.paths import project_root
import queue
import re
import secrets
import threading
import time
import webbrowser


_HTML = r'''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>텔레그램 개인 계정 연결</title>
<style>
*{box-sizing:border-box}body{background:#f3f6fb;color:#17263d;font:16px/1.6 -apple-system,BlinkMacSystemFont,sans-serif;margin:0;padding:40px 20px}main{max-width:660px;margin:auto;background:white;border:1px solid #dbe4ef;border-radius:20px;padding:36px;box-shadow:0 15px 50px #19334d0b}h1{font-size:27px;line-height:1.3;margin:12px 0}p{color:#53627a}.tag{font-size:12px;font-weight:700;color:#167696;letter-spacing:1px}label{display:block;margin-top:18px;font-weight:600}input:not([type=checkbox]){width:100%;padding:12px;border:1px solid #becbdd;border-radius:9px;font:inherit;margin-top:6px}button{border:0;border-radius:10px;background:#147fa8;color:white;padding:13px 20px;font:inherit;font-weight:600;cursor:pointer;margin-top:24px}button.secondary{background:#eaf0f8;color:#253957}button:disabled{opacity:.5;cursor:wait}.note{font-size:13px;color:#607087}.channels label{font-weight:400;background:#f5f8fc;border-radius:8px;padding:12px;margin-top:8px}.channels input{margin-right:10px}a{color:#087aa7}[hidden]{display:none!important}#message{padding:12px 16px;background:#eef6fb;border-radius:10px;white-space:pre-wrap}footer{font-size:12px;color:#718198;margin-top:26px}
</style><main><div class="tag">KIWOOM · TELEGRAM</div><h1>텔레그램 개인 계정 연결</h1>
<p>개인 계정으로 접근 가능한 채널을 선택해 과거 글과 새 글을 저장합니다.</p><div id="message">연결 준비 중…</div>
<form id="credentials" hidden><p class="note"><a href="https://my.telegram.org/apps" target="_blank" rel="noopener noreferrer">Telegram API development tools</a>에서 발급한 값을 입력하세요. 기존 봇 토큰과는 다른 값입니다.</p>
<label>API ID<input name="api_id" inputmode="numeric" pattern="[0-9]+" required autocomplete="off"></label>
<label>API Hash<input name="api_hash" type="password" required autocomplete="off"></label>
<label>텔레그램 전화번호 · 코드 로그인 시 입력<input name="phone" type="tel" placeholder="+821012345678" required autocomplete="off"></label>
<button id="qrStart" type="button">QR 코드로 연결</button> <button class="secondary">전화번호로 인증코드 받기</button> <button id="saved" class="secondary" type="button" hidden>저장된 계정으로 계속</button></form>
<div id="qrPanel" hidden><p>휴대폰 Telegram → 설정 → 기기 → 데스크톱 기기 연결에서 아래 QR 코드를 스캔하세요.</p><img id="qrImage" alt="텔레그램 개인 계정 연결 QR 코드" width="280" height="280"></div>
<form id="verification" hidden><label id="verificationLabel"></label><input id="secret" type="password" autocomplete="off" required><button>확인</button></form>
<form id="selection" hidden><p>수집할 채널을 선택하세요. 선택한 채널의 과거 글부터 이어서 저장하고 새 글은 주기적으로 확인합니다.</p><div id="channelList" class="channels"></div><button>선택한 채널 수집 시작</button></form>
<button id="retry" class="secondary" hidden>다시 시도</button><footer>입력값은 이 Mac의 연결 프로그램으로만 전달됩니다. 로그인 세션은 macOS 키체인에 저장합니다. 채널의 글을 매매 명령으로 실행하지 않습니다.</footer></main>
<script>
const base=location.pathname.replace(/\/$/,''); let current='', channelVersion='', qrVersion=0;
async function send(action,data={}){try{const r=await fetch(base,{method:'POST',headers:{'Content-Type':'application/json','X-Setup-Token':base.slice(1)},body:JSON.stringify({action,...data})});if(!r.ok)throw Error();await refresh()}catch{document.querySelector('#message').textContent='연결 화면 요청을 처리하지 못했습니다. 잠시 후 다시 시도하세요.'}}
async function refresh(){try{const r=await fetch(base+'/state',{cache:'no-store'});if(!r.ok)return;const s=await r.json();current=s.step;document.querySelector('#message').textContent=s.message;document.querySelector('#credentials').hidden=current!=='credentials';document.querySelector('#saved').hidden=!s.has_saved;document.querySelector('#verification').hidden=!['code','password'].includes(current);document.querySelector('#selection').hidden=current!=='channels';document.querySelector('#retry').hidden=current!=='error';document.querySelector('#verificationLabel').textContent=current==='password'?'텔레그램 2단계 인증 비밀번호':'텔레그램으로 받은 인증코드';if(current==='channels'&&channelVersion!==JSON.stringify(s.channels)){channelVersion=JSON.stringify(s.channels);const list=document.querySelector('#channelList');list.replaceChildren();s.channels.forEach((c,i)=>{const l=document.createElement('label'),v=document.createElement('input');v.type='checkbox';v.value=i;v.checked=c.selected;l.append(v,document.createTextNode(c.title+(c.username?' (@'+c.username+')':'')));list.append(l)})}}catch{document.querySelector('#message').textContent='설정 프로그램과 연결이 끊겼습니다. 계정 연결 실행 파일을 다시 열어 주세요.'}}
const originalRefresh=refresh;refresh=async()=>{await originalRefresh();try{const r=await fetch(base+'/state',{cache:'no-store'}),s=await r.json();document.querySelector('#qrPanel').hidden=s.step!=='qr';if(s.step==='qr'&&s.qr_version!==qrVersion){qrVersion=s.qr_version;document.querySelector('#qrImage').src=base+'/qr?v='+qrVersion}}catch{}};
document.querySelector('#credentials').onsubmit=e=>{e.preventDefault();const f=new FormData(e.target);send('credentials',Object.fromEntries(f));e.target.reset()};
document.querySelector('#qrStart').onclick=()=>{const form=document.querySelector('#credentials'),f=new FormData(form);send('qr',{api_id:f.get('api_id'),api_hash:f.get('api_hash')});form.reset()};
document.querySelector('#verification').onsubmit=e=>{e.preventDefault();const input=document.querySelector('#secret');send(current,{value:input.value});input.value=''};
document.querySelector('#selection').onsubmit=e=>{e.preventDefault();send('select',{indices:[...document.querySelectorAll('#channelList input:checked')].map(n=>Number(n.value))})};
document.querySelector('#saved').onclick=()=>send('saved');document.querySelector('#retry').onclick=()=>send('retry');refresh();setInterval(refresh,1500);
</script></html>'''


class SetupWorkflow:
    def __init__(self):
        self.lock = threading.RLock()
        self.answers = queue.Queue(maxsize=1)
        self.step = "credentials"
        self.message = "API ID와 API Hash를 입력한 뒤 QR 코드 또는 전화번호로 본인 인증을 시작하세요."
        self.config = None
        self.channels = []
        self.selected = set()
        self.qr_svg = b""
        self.qr_version = 0
        from kiwoom_autotrader.telegram.account_credentials import load_account_config
        try:
            self.has_saved = load_account_config() is not None
        except Exception:
            self.has_saved = False

    def state(self):
        with self.lock:
            return {"step": self.step, "message": self.message, "has_saved": self.has_saved,
                    "qr_version": self.qr_version,
                    "channels": [{"title": c["title"], "username": c.get("username"),
                                  "selected": c["channel_id"] in self.selected}
                                 for c in self.channels] if self.step == "channels" else []}

    def _set(self, step, message):
        with self.lock:
            self.step, self.message = step, message
            if step != "qr":
                self.qr_svg = b""

    def qr_image(self):
        with self.lock:
            return self.qr_svg if self.step == "qr" else b""

    def _show_qr(self, url, expires_at):
        import qrcode
        from qrcode.image.svg import SvgPathFillImage
        if not isinstance(url, str) or not re.fullmatch(r"tg://login\?token=[A-Za-z0-9_=-]+", url):
            raise ValueError
        image = qrcode.make(url, image_factory=SvgPathFillImage, box_size=8, border=4)
        stream = io.BytesIO()
        image.save(stream)
        with self.lock:
            self.qr_svg = stream.getvalue()
            self.qr_version += 1
            self._set("qr", "휴대폰 텔레그램에서 QR 코드를 스캔해 본인 계정 연결을 승인하세요.")

    async def _ask(self, step):
        self._set(step, "텔레그램으로 받은 인증코드를 입력하세요." if step == "code"
                  else "텔레그램 2단계 인증 비밀번호를 입력하세요.")
        return await asyncio.to_thread(self.answers.get, True, 600)

    async def _prepare(self, config):
        from kiwoom_autotrader.telegram.channel_store import ChannelStore
        from kiwoom_autotrader.utils.account_service import account_paths
        from kiwoom_autotrader.telegram.collector import list_account_channels
        channels = await list_account_channels(config)
        with ChannelStore(account_paths().state, config.user_id) as store:
            selected = {c["channel_id"] for c in store.channels()}
        with self.lock:
            self.config, self.channels, self.selected = config, channels, selected
            self.has_saved = True
        self._set("channels", "계정 연결 완료. 수집할 채널을 선택하세요." if channels else
                  "연결은 완료됐지만 가입한 방송 채널이 없습니다. 텔레그램에서 채널에 가입한 뒤 연결 화면을 다시 여세요.")

    def _authenticate(self, values=None, use_qr=False):
        async def work():
            from kiwoom_autotrader.telegram.account import connect_account, connect_account_qr
            from kiwoom_autotrader.telegram.account_credentials import load_account_config
            if values is None:
                config = load_account_config()
                if config is None:
                    raise ValueError
            elif use_qr:
                config = await connect_account_qr(values[0], values[1], self._show_qr,
                                                  lambda: self._ask("password"))
            else:
                config = await connect_account(values[0], values[1], values[2],
                                               lambda: self._ask("code"), lambda: self._ask("password"))
            await self._prepare(config)
        try:
            asyncio.run(work())
        except Exception as error:
            from kiwoom_autotrader.telegram.account import AccountConnectionError
            message = str(error) if isinstance(error, AccountConnectionError) else "계정 연결을 완료하지 못했습니다. 입력값과 네트워크를 확인하세요."
            self._set("error", message)

    def _activate(self, channels):
        try:
            from kiwoom_autotrader.telegram.channel_store import ChannelStore
            from kiwoom_autotrader.utils.account_service import account_paths, install_collector_service, stop_collector_service
            paths = account_paths()
            stop_collector_service(paths)
            with ChannelStore(paths.state, self.config.user_id) as store:
                store.set_selected(channels)
            install_collector_service(source=project_root(), paths=paths)
            self._set("done", "채널 수집을 시작했습니다. Mac 로그인 시 자동으로 다시 연결됩니다.\n텔레그램 봇에서 ‘채널’ 또는 ‘채널글’로 수집 상태와 최근 글을 확인할 수 있습니다.")
        except Exception:
            self._set("error", "채널 선택은 저장됐을 수 있지만 자동 수집 시작을 확인하지 못했습니다. 저장된 계정으로 다시 진행하세요.")

    def submit(self, body):
        if not isinstance(body, dict):
            raise ValueError
        action = body.get("action")
        with self.lock:
            if action == "retry" and self.step == "error":
                self._set("credentials", "연결 정보를 확인하고 다시 진행하세요.")
                return
            if action in {"credentials", "saved", "qr"} and self.step == "credentials":
                values = None
                if action in {"credentials", "qr"}:
                    api_id, api_hash, phone = (body.get(k) for k in ("api_id", "api_hash", "phone"))
                    if (not isinstance(api_id, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", api_id)
                            or not isinstance(api_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash)
                            or (action == "credentials" and (not isinstance(phone, str) or not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone)))):
                        self.message = "API ID, 32자리 API Hash, 국가번호를 포함한 전화번호(+8210…)를 확인하세요."
                        return
                    values = int(api_id), api_hash, phone
                while not self.answers.empty():
                    try:
                        self.answers.get_nowait()
                    except queue.Empty:
                        break
                self._set("working", "텔레그램에 연결하고 있습니다…")
                threading.Thread(target=self._authenticate, args=(values, action == "qr"), daemon=True).start()
                return
            if action in {"code", "password"} and self.step == action:
                value = body.get("value")
                if not isinstance(value, str) or not 1 <= len(value) <= 1024:
                    raise ValueError
                self._set("working", "인증 정보를 확인하고 있습니다…")
                self.answers.put_nowait(value)
                return
            if action == "select" and self.step == "channels":
                indices = body.get("indices")
                if (not isinstance(indices, list) or not indices or len(indices) != len(set(indices))
                        or any(type(i) is not int or not 0 <= i < len(self.channels) for i in indices)):
                    self.message = "수집할 채널을 하나 이상 선택하세요."
                    return
                selected = [self.channels[i] for i in indices]
                self._set("working", "채널 수집과 로그인 자동 실행을 설정하고 있습니다…")
                threading.Thread(target=self._activate, args=(selected,), daemon=True).start()
                return
            raise ValueError


def make_handler(workflow, token, origin):
    prefix = "/" + token

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _respond(self, code, content, content_type="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(content)

        def _host_valid(self):
            return self.headers.get("Host") == origin.removeprefix("http://")

        def do_GET(self):
            if not self._host_valid():
                return self._respond(403, b"{}")
            if self.path in {prefix, prefix + "/"}:
                return self._respond(200, _HTML.encode(), "text/html; charset=utf-8")
            if self.path == prefix + "/state":
                return self._respond(200, json.dumps(workflow.state(), ensure_ascii=False).encode())
            if self.path == prefix + "/qr" or re.fullmatch(re.escape(prefix) + r"/qr\?v=[0-9]+", self.path):
                data = workflow.qr_image()
                return self._respond(200 if data else 404, data, "image/svg+xml")
            self._respond(404, b"{}")

        def do_POST(self):
            if (not self._host_valid() or self.path != prefix or self.headers.get("Origin") != origin
                    or self.headers.get("X-Setup-Token") != token
                    or self.headers.get("Content-Type") != "application/json"):
                return self._respond(403, b"{}")
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16384:
                    raise ValueError
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError
                workflow.submit(payload)
            except (ValueError, TypeError, RecursionError, queue.Full):
                return self._respond(400, b"{}")
            self._respond(200, b'{"ok":true}')

    return Handler


def serve_setup(*, open_browser=True):
    from kiwoom_autotrader.utils.dependencies import enable_account_dependencies
    from kiwoom_autotrader.utils.account_service import account_paths
    from kiwoom_autotrader.utils.telegram_service import _private_write
    enable_account_dependencies()
    workflow = SetupWorkflow()
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    origin = f"http://127.0.0.1:{server.server_port}"
    server.RequestHandlerClass = make_handler(workflow, token, origin)
    server.timeout = 1
    url = origin + "/" + token
    state_dir = account_paths().state
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    url_file = state_dir / "setup-url.json"
    _private_write(url_file, json.dumps({"url": url}).encode())
    print("개인 계정 연결 화면: " + url, flush=True)
    if open_browser:
        webbrowser.open(url)
    deadline = time.monotonic() + 1800
    try:
        while time.monotonic() < deadline:
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            if json.loads(url_file.read_text()).get("url") == url:
                url_file.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
    return 0
