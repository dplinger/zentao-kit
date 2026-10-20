"""在用户自己的浏览器或终端登录；密码仅以本机密文留在个人状态文件。"""

import html
from http.cookiejar import Cookie, CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shlex
import shutil
from string import Template
import secrets
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser

from common import SkillError, private_write, read_json, state_dir
from local_cipher import decrypt, encrypt, protection

# 登录成功后如实说明账密存在哪里——保护方式不同，用户该知道差别。
PROTECTION_LABEL = {"windows-dpapi": "密钥由 Windows 系统凭据保护",
                    "macos-keychain": "密钥由 macOS 登录钥匙串保护",
                    "file": "密钥保存在本机状态目录内"}

# v2 把失败写成 HTTP 200 + status=fail。调用方据此识别，避免各处硬编码字面量。
BUSINESS_FAILURE = "业务失败"

# 不内置任何站点默认值：地址由用户提供并保存到个人 preferences.json。

# 未配置站点时返回给宿主的状态；宿主据此向用户索取地址后重新发起 auth。
NEED_BASE_URL = ("NEED_BASE_URL：尚未配置禅道站点。请向用户索取站点地址（例如 https://zentao.example.com，"
                 "站点部署在子目录时为 https://example.com/zentao；仅内网 HTTP 部署可直接给 http:// 地址），"
                 "再执行 auth --base-url <地址> 打开登录页。")

# 站点走明文 HTTP 时的固定告警：配置结果与登录结果共用同一句话，避免口径不一。
HTTP_NOTICE = ("当前站点以 HTTP（明文）访问，登录账密与令牌会在网络上明文传输；"
               "若站点同时支持 HTTPS 请改用 https:// 地址，否则请仅在可信内网使用。")

# 地址形态不合法时的统一措辞。
BAD_BASE = ("禅道地址无法识别：需要以 http:// 或 https:// 开头、且不含账号密码与查询参数的地址，"
            "例如 https://zentao.example.com 或 http://10.0.0.5/zentao。")

def write_auth_status(value):
    """登录进度更新时保留已保存的密文，避免终端或浏览器登录覆盖它。"""
    path = state_dir() / "auth-status.json"
    old = read_json(path) if path.exists() else {}
    if old.get("saved_login") and "saved_login" not in value:
        value = {**value, "saved_login": old["saved_login"]}
    private_write(path, value)


def setting(name, fallback=None):
    """环境变量、个人配置、历史登录站点依次覆盖内置默认值。"""
    env = os.environ.get('ZENTAO_BASE_URL' if name == 'base_url' else 'ZENTAO_UTC_OFFSET')
    if env:
        return env
    path = state_dir() / 'preferences.json'
    value = read_json(path).get(name) if path.exists() else None
    if value:
        return value
    if name == 'base_url':
        session = state_dir() / 'auth-status.json'
        value = read_json(session).get('base_url') if session.exists() else None
        # 没有内置站点：未配置时返回 None，由调用方向用户索取地址。
        return value or None
    return fallback


def base_url(value):
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password \
            or parts.query or parts.fragment:
        raise SkillError("禅道地址必须为无凭据、无查询参数的 HTTP(S) 地址。")
    return value.rstrip("/")


def explicit_scheme(value):
    """取出用户显式写出的协议；没写或写法不规范时返回 None。"""
    text = " ".join(str(value or "").split()).strip("\"'").lower()
    return next((item for item in ("https", "http") if text.startswith(item + "://")), None)


def prefers_http(host):
    """判断是否内网地址：这类站点普遍只监听 HTTP。

    公网站点先试 HTTPS 是为了自动升级到加密连接，但内网 IP、单段主机名、
    localhost 上多半根本没有 TLS 监听；遇到防火墙丢包时，每次探测都要等满
    超时，等于让用户白等几十秒。故这类地址把 HTTP 排到前面，HTTPS 仍保留兜底。
    """
    try:
        name = (urllib.parse.urlsplit("//" + host).hostname or "").lower()
    except ValueError:
        return False
    if not name:
        return False
    return (name in ("localhost", "::1")
            or name.startswith("127.") or name.startswith("10.") or name.startswith("192.168.")
            or re.match(r"172\.(?:1[6-9]|2\d|3[01])\.", name) is not None
            or "." not in name)          # 单段主机名按内网 DNS 处理


def candidate_bases(value):
    """把用户随手给的各种写法展开成有序候选地址：HTTPS 优先，HTTP 兜底。

    清理：去引号与首尾空白、去查询串与锚点、**去页面文件名**
    （`user-login-xxx.html`、`bug-view-1.html`、`index.php` 等），只保留到子目录。
    用户最常做的是从浏览器地址栏直接复制，那串链接里一定带页面名和查询串；
    把整串当接口根会把后续请求打到页面上，服务端返回 HTML，最后表现成
    「响应不是有效 JSON」。
    协议：显式 HTTPS 就只用 HTTPS；显式 HTTP 或缺协议时先试 HTTPS 再退回 HTTP，
    站点同时开放两种协议时自动升级，不让账密走明文。
    子目录：站点部署在 /zentao 时一并试探，用户只给域名也能对上。
    """
    text = " ".join(str(value or "").split()).strip("\"'")
    if not text:
        raise SkillError(NEED_BASE_URL)
    scheme = explicit_scheme(text)
    if "://" in text:
        if scheme is None:
            raise SkillError(BAD_BASE)
        host = text.split("://", 1)[1]
    else:
        host = text

    # 丢掉查询串与锚点，再把路径逐段截短。
    # 从浏览器地址栏复制来的链接会带页面路径（/user-login、/user-login-xxx.html、
    # /my-work-bug-assignedTo-0-id_desc-0-20-1.html…），这类名字无穷无尽，不靠猜，
    # 而是把每一级前缀都交给探测：/a/b/c → /a/b → /a → 空，谁先探通就用谁。
    host = re.split(r"[?#]", host, 1)[0]
    head, _, rest = host.partition("/")
    segments = [item for item in rest.split("/") if item]
    paths = ["/" + "/".join(segments[:size]) if size else "" for size in range(len(segments), -1, -1)]

    schemes = (["https"] if scheme == "https"
               else ["http", "https"] if prefers_http(head) else ["https", "http"])
    candidates = []
    for item in schemes:
        for path in paths:
            candidate = base_url(f"{item}://{head}{path}")
            if candidate not in candidates:
                candidates.append(candidate)
        # 站点部署在 /zentao 而用户只给了域名时，补一次子目录候选。
        extra = base_url(f"{item}://{head}/zentao")
        if extra not in candidates:
            candidates.append(extra)
    return candidates


def transport_notice(base):
    """站点走明文 HTTP 时的告警；HTTPS 返回 None。"""
    return HTTP_NOTICE if urllib.parse.urlsplit(base).scheme == "http" else None


def probe(base):
    """探测 REST 接口入口，返回 'ok' | 'tls' | 'gateway' | 'absent' | 'unreachable'。

    401 表示入口存在但需要令牌，与 200 同属可用。两种失败最容易被误判成别的毛病，
    必须单独识别：证书不受信任（被 urllib 包成 URLError）与网关异常（502/503/504，
    网络代理对不可达目标也常回 502）。否则用户只会看到"网络不通"或"没有接口入口"
    而去白查拼写和端口。
    """
    req = urllib.request.Request(base + "/api.php/v1/ping",
                                 headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=10) as response:
            # 禅道对不存在的页面路径同样返回 200 和登录页 HTML，只看状态码会把
            # `/zentao/user-login` 这类页面地址误判成接口入口。必须是 JSON 才算数。
            if response.status != 200:
                return "absent"
            head = response.read(512)
            ctype = (response.headers.get("Content-Type") or "").lower()
            return "ok" if "json" in ctype or head.lstrip().startswith(b"{") else "absent"
    except urllib.error.HTTPError as exc:
        if exc.code in (502, 503, 504):
            return "gateway"
        return "ok" if exc.code == 401 else "absent"
    except urllib.error.URLError as exc:
        return "tls" if isinstance(exc.reason, ssl.SSLCertVerificationError) else "unreachable"
    except (TimeoutError, OSError, ValueError):
        return "unreachable"


def probe_failure(seen):
    """把各候选的探测结果汇总成一句可执行的报错：具体原因优先于笼统原因。"""
    tried = " 与 ".join(address for address, _ in seen)
    states = {state for _, state in seen}
    if "tls" in states:
        return (f"站点证书不受信任，无法建立 HTTPS 连接（自签名证书常见，已尝试 {tried}）。"
                "请把站点 CA 证书加入系统信任库，或改用站点提供的 HTTP 地址后重试。")
    if "gateway" in states:
        return (f"禅道网关或上游服务异常（已尝试 {tried}）；若本机使用网络代理，"
                "也可能是代理无法访问该地址。请稍后重试、核对地址与端口，"
                "或把该地址加入代理的直连（绕过）列表。")
    if states == {"absent"}:
        return (f"地址可达但没有禅道接口入口（已尝试 {tried}）。"
                "请核对站点是否部署在子目录（如 /zentao），以及该地址是否确为禅道站点。")
    return (f"无法访问禅道接口（已尝试 {tried}）。请核对地址拼写、端口与网络可达性。")


def locate_base(value, probe_entry=True):
    """规范地址、按需探测接口入口，返回 (站点地址, 说明)。

    说明用于告知协议被自动升级，或提示正在降级使用明文 HTTP；无特殊情况时为 None。
    探测只用于尽早发现写错的地址，不锁定站点：探测通过才返回，失败则明确报错并
    指出具体原因（证书 / 入口不存在 / 网络不通），避免用户白输一遍账号密码。
    """
    candidates = candidate_bases(value)
    if not probe_entry:
        return candidates[0], transport_notice(candidates[0])
    seen = []
    for candidate in candidates:
        state = probe(candidate)
        if state == "ok":
            if urllib.parse.urlsplit(candidate).scheme == "https" and explicit_scheme(value) == "http":
                return candidate, "站点支持 HTTPS，已自动改用加密连接，未使用你给出的 http:// 明文地址。"
            return candidate, transport_notice(candidate)
        seen.append((candidate, state))
    raise SkillError(probe_failure(seen))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """拒绝跳转，避免账号密码或 Token 被转发到另一地址。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def clean_detail(value, limit=200):
    """清洗服务端说明：去标签、压空白、截断，并过滤 `error`、`404 Not found` 这类占位词。"""
    text = re.sub(r'<[^>]+>', ' ', str(value))
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) < 3 or text.lower() in ('error', 'success') or text.startswith('404'):
        return ''
    return text[:limit]


def failure_detail(exc, limit=200):
    """提取 HTTP 错误响应里的业务说明。

    禅道对字段类错误只回 `{"error": "『开始日期』不能为空。"}` 这样的短语，原先一律被压成
    「HTTP 400」，用户无从定位。这里只取 error/message 字段，不把整段响应带出来。
    """
    try:
        body = exc.read(8192).decode('utf-8', 'replace')
        value = json.loads(body)
    except (OSError, ValueError):
        return ''
    if isinstance(value, dict):
        value = value.get('error') or value.get('message') or ''
    return clean_detail(value, limit)


def result_failure(result, limit=200):
    """提取业务失败响应里的说明。

    v2 把「记录不存在」表达成 HTTP 200 + `{"status":"fail","message":"Bug does not exist."}`，
    原先被压成「请核对权限、版本和字段」，把明确的「不存在」误导成权限问题。这里把说明带出来。
    """
    if not isinstance(result, dict):
        return ''
    return clean_detail(result.get('message') or result.get('error') or '', limit)


def http_failure(code, writing=False, detail=''):
    """区分重新鉴权与服务不可用；不泄露响应正文，不对写操作盲目重试。"""
    if code == 401:
        return SkillError('AUTH_REQUIRED：认证失败或已过期，请在外部登录窗口重新登录。')
    if code in (502, 503, 504):
        message = f'SERVICE_UNAVAILABLE：HTTP {code}，禅道网关或上游服务异常，请等待管理员恢复；无需反复登录。'
    elif code == 403:
        message = 'PERMISSION_DENIED：HTTP 403，当前账号无权访问，请核对权限。'
    else:
        # 400/404/409 这类多半是字段或关联问题，把服务端说明如实转达，用户才知道要改什么。
        message = f'禅道接口返回 HTTP {code}。' + (f'服务端说明：{detail} ' if detail else '')
    if writing:
        message += '写入结果需回查，禁止盲目重发。'
    return SkillError(message)


def request(base, method, path, token=None, body=None, opener=None, allow_failure=False,
            raw=None, content_type=None):
    """发送接口请求；body 为 JSON，raw 用于 multipart 等其它请求体。"""
    headers = {"Accept": "application/json", "Content-Type": content_type or "application/json"}
    if token:
        headers["Token"] = token
    if raw is not None:
        data = raw
    else:
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(base_url(base) + "/api.php/" + path, data=data, headers=headers, method=method)
    try:
        with (opener or urllib.request.build_opener(NoRedirect())).open(req, timeout=30) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise SkillError("接口响应过大，已停止；请缩小查询范围。")
            result = json.loads(raw)
    except urllib.error.HTTPError as exc:
        # 不输出请求对象或整段响应，只带出服务端给用户的业务说明。
        raise http_failure(exc.code, method not in ('GET', 'HEAD') and path != 'v2/users/login',
                           failure_detail(exc)) from None
    except ValueError:
        # 响应不是 JSON：最常见的原因是地址指到了网页而不是接口根
        # （例如把 /zentao/user-login 当成站点地址），服务端回的是登录页 HTML。
        # 这种情况说成「网络异常」会让用户以为技能坏了，必须直指地址。
        raise SkillError("站点地址可能有误，请检查后重新提供。") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SkillError("网络异常，没能连上站点；"
                         + ("写入结果需回查，不能盲目重试。" if method not in ("GET", "HEAD")
                            else "请确认地址、网络和代理设置后重试。")) from None
    if (isinstance(result, dict) and result.get("status") in ("fail", "failed", "error")
            and not allow_failure):
        detail = result_failure(result)
        raise SkillError(f"禅道返回{BUSINESS_FAILURE}；请核对权限、字段或编号"
                         + (f"。服务端说明：{detail}" if detail
                            else "，未输出可能包含敏感信息的原始响应。"))
    return result


def upload(base, path, token, field, filename, content, content_type="application/octet-stream", extra=None):
    """以 multipart/form-data 上传单个文件；禅道附件接口的字段名是 imgFile。"""
    boundary = "----zentao-worklog-" + uuid.uuid4().hex
    parts = []
    for name, value in (extra or {}).items():
        parts.append(f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                 f"Content-Type: {content_type}\r\n\r\n".encode())
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return request(base, "POST", path, token, raw=b"".join(parts),
                   content_type="multipart/form-data; boundary=" + boundary)


def remember_message():
    """账密保存完毕后的提示；密钥由系统代管还是落文件，如实告知。"""
    return f"已记住账密（{PROTECTION_LABEL.get(protection(), '本机加密保存')}），令牌过期后会自动重登。"


def fetch_page(session, path):
    """用登录保存的 Cookie 读取站点网页或页面 JSON；仅令牌会得到不正确的页面内容。"""
    if not session.get('cookies'):
        raise SkillError('AUTH_REQUIRED：缺少网页会话，请重新登录。')
    jar = CookieJar()
    try:
        for saved in session['cookies']:
            fields = dict(saved)
            fields['rest'] = fields.pop('_rest', {})
            jar.set_cookie(Cookie(**fields))
    except (TypeError, ValueError):
        raise SkillError('网页会话缓存不可用，请重新登录。') from None
    base = base_url(session['base_url'])
    if not path.startswith('/') or path.startswith('//') or '..' in path:
        raise SkillError('页面路径不合法。')
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(jar))
    try:
        with opener.open(base + path, timeout=30) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise SkillError('网页响应超出读取上限，已停止。')
        return raw.decode('utf-8')
    except urllib.error.HTTPError as exc:
        raise http_failure(exc.code) from None
    except (urllib.error.URLError, OSError, UnicodeError):
        raise SkillError('网页读取失败或会话失效，请核对网络与登录状态；不能视为列表为空。') from None


def save_session(value, password):
    """始终保存令牌；并用本机密钥加密记住账密，供令牌过期后自动重登。"""
    fields = ("base_url", "token", "cookies", "id", "account", "realname")
    credentials = json.dumps({"account": value["account"], "password": password}, ensure_ascii=False).encode()
    # 明文站点在每次登录成功时都要提醒：配置时说过，真正输账密时可能已经忘了。
    notice = transport_notice(value["base_url"])
    saved = {"status": "authenticated", **{key: value[key] for key in fields if key in value},
             "saved_login": {"base_url": base_url(value["base_url"]), "ciphertext": encrypt(credentials)},
             "message": f"{notice} {remember_message()}" if notice else remember_message()}
    private_write(state_dir() / "auth-status.json", saved)
    (state_dir() / "session.json").unlink(missing_ok=True)
    # 登录成功后保留站点配置，退出登录或凭据过期不会丢失公司地址。
    path = state_dir() / 'preferences.json'
    prefs = read_json(path) if path.exists() else {}
    prefs['base_url'] = base_url(value['base_url'])
    private_write(path, prefs)
    return saved.get("message")


def local_login_ready(url):
    """只探测本机随机登录页是否仍在监听，不读取或提交表单。"""
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def auth_status():
    """只返回白名单摘要，绝不将令牌和 Cookie 输出给宿主。"""
    path = state_dir() / "auth-status.json"
    value = read_json(path) if path.exists() else {"status": "not_started"}
    # 页面暂时忙碌不能当作进程退出；连通性由 auth-wait 连续观察后判断。
    summary = {k: value[k] for k in ("status", "url", "expires_at", "message") if k in value}
    if value.get("status") == "authenticated":
        if not value.get("token"):
            return {"status": "auth_required", "message": "旧版登录缓存需要重新授权。"}
        session = load_session()
        summary.update({k: session[k] for k in ("id", "account", "realname", "base_url") if k in session})
    return summary


def load_session():
    path = state_dir() / "auth-status.json"
    if not path.exists():
        raise SkillError("AUTH_REQUIRED：请先执行 auth，在 AI 之外输入账号密码。")
    try:
        value = read_json(path)
        if value.get("status") != "authenticated" or not value.get("token"):
            raise SkillError("AUTH_REQUIRED：旧缓存不可用，请重新登录以建立网页会话。")
        return value
    except (KeyError, ValueError, OSError):
        raise SkillError("登录缓存不可用，请重新登录。") from None


def profile(base, token):
    value = request(base, "GET", "v1/user", token)
    value = value.get("profile", value) if isinstance(value, dict) else {}
    if not isinstance(value, dict) or not value.get("account"):
        raise SkillError("个人信息响应缺少 account，需核对公司禅道接口格式。")
    return {"account": value["account"], "realname": value.get("realname", "")}


def login(base, account, password):
    # V2 一次取得身份和令牌；CookieJar 保留网页查询所需的登录会话。
    cookies = CookieJar()
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(cookies))
    # 登录失败时读取经过筛选的服务端说明，避免只显示笼统的“业务失败”。
    value = request(base, "POST", "v2/users/login", body={"account": account, "password": password},
                    opener=opener, allow_failure=True)
    if not isinstance(value, dict) or not isinstance(value.get("token"), str) or not value["token"]:
        raise SkillError(login_failure_reason(value, account, password))
    user = value.get("user", {})
    if not isinstance(user, dict) or user.get("account") != account:
        raise SkillError("登录身份不一致，已停止。")
    who = {key: user.get(key, "") for key in ("id", "account", "realname")}
    message = save_session({"base_url": base, "token": value["token"], **who,
                            "cookies": [vars(cookie) for cookie in cookies]}, password)
    if message:
        who["message"] = message
    return who


def renew_session(session):
    """仅在旧令牌已被确认过期时尝试一次本地重登。"""
    saved = session.get("saved_login") or {}
    if saved.get("base_url") != base_url(session["base_url"]) or not saved.get("ciphertext"):
        raise SkillError("AUTH_REQUIRED：登录已过期，请在外部登录窗口重新登录。")
    try:
        credentials = json.loads(decrypt(saved["ciphertext"]))
        if credentials["account"] != session["account"] or not credentials["password"]:
            raise ValueError("登录身份不一致")
    except (ValueError, KeyError, TypeError):
        # 密文被改动、本机密钥更换、或旧版密文都走这里：清掉密文，要求重新登录一次。
        session.pop("saved_login", None)
        private_write(state_dir() / "auth-status.json", session)
        raise SkillError("AUTH_REQUIRED：本地登录信息不可用，请重新输入账号密码。") from None
    try:
        login(session["base_url"], credentials["account"], credentials["password"])
    except SkillError as exc:
        if str(exc).startswith(("登录失败", "AUTH_REQUIRED")):
            session.pop("saved_login", None)
            private_write(state_dir() / "auth-status.json", session)
            raise SkillError("AUTH_REQUIRED：自动登录失败，请重新输入账号密码。") from None
        raise
    return load_session()


def login_failure_reason(value, account, password):
    """返回可读的登录失败原因，同时拒绝回显账号、密码或多行响应。"""
    reason = value.get("reason") if isinstance(value, dict) else None
    if not isinstance(reason, str) or not reason.strip():
        return "登录失败：账号或密码不正确，请重新登录。"
    reason = " ".join(reason.split())[:200]
    if (account and account in reason) or (password and password in reason):
        return "登录失败：账号或密码不正确，请重新登录。"
    return f"登录失败：{reason}"


def login_page(base, nonce, message="", form=True, status=200):
    """读取独立页面模板，用标准库填充已转义的数据和表单展示状态。"""
    template = Template((Path(__file__).resolve().parents[1] / "assets/login.html").read_text(encoding="utf-8"))
    success = not form and status == 200
    values = {
        "base": base, "nonce": nonce, "message": message,
        "title": "连接您的禅道账号" if form else "授权已完成" if success else "授权暂未完成",
        "notice_class": "success" if success else "error",
        "notice_hidden": "" if message else "hidden",
        "form_hidden": "" if form else "hidden", "disabled": "" if form else "disabled",
        "next_hidden": "hidden" if form else "",
        "auto_close": "true" if success else "false",
        "next_message": "即将自动关闭页面 4 秒" if success else
                        "请返回对话重新发起授权，使用最新打开的页面。",
    }
    # 单次模板替换，不把用户文本再次解释为模板；所有动态值都进行 HTML 转义。
    return template.substitute({key: html.escape(str(value), quote=True) for key, value in values.items()}).encode("utf-8")


def browser_login(base, seconds=600):
    """只监听本机随机端口，表单带随机路径和防跨站令牌，默认 10 分钟后退出。

    `seconds` 是等待上限。宿主若会回收命令产生的子进程，登录服务会在用户提交表单前
    被掐断——这种环境应让调用方传 `--wait`，把服务放在当前进程里阻塞等待。
    返回最终状态，便于调用方直接转述结果。
    """
    nonce = secrets.token_urlsafe(32)
    finished = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def page(self, message="", form=True, status=200):
            page = login_page(base, nonce, message, form, status)
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            # no-referrer 可使浏览器表单 POST 的 Origin 变为 null，误触下面的同源校验。
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Content-Security-Policy", f"default-src 'none'; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(page)

        def allowed(self):
            host = f"127.0.0.1:{self.server.server_port}"
            return (self.path == f"/{nonce}" and self.headers.get("Host") == host
                    and self.headers.get("Origin", f"http://{host}") == f"http://{host}")

        def do_GET(self):
            if not self.allowed():
                self.page("无效登录地址。", False, 403)
            else:
                self.page()

        def do_POST(self):
            nonlocal finished
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not self.allowed() or not 0 < length <= 8192:
                    self.page("无效请求。", False, 403)
                    return
                fields = urllib.parse.parse_qs(self.rfile.read(length).decode())
                if not secrets.compare_digest(fields.get("csrf", [""])[0], nonce):
                    self.page("登录页面已失效。", False, 403)
                    return
                account = fields.get("account", [""])[0].strip()
                password = fields.get("password", [""])[0]
                if not account or not password:
                    raise SkillError("账号和密码不能为空。")
                who = login(base, account, password)
                # login 已原子写入统一状态文件，不能再用状态摘要覆盖凭据。
                finished = True
                self.page("登录成功，请返回 AI 对话继续。" + (" " + who["message"] if who.get("message") else ""), False)
            except SkillError as exc:
                # SkillError 只包含经过筛选的状态说明，不含密码、令牌或接口正文。
                self.page(f"登录未完成：{exc}")
            except (ValueError, OSError):
                self.page("登录未完成，请核对账号密码、网络和权限后重试。")

    # 登录 POST 可能等待禅道响应；独立线程仍可回答 auth-wait 的页面探测。
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 1
        url = f"http://127.0.0.1:{server.server_port}/{nonce}"
        write_auth_status({"status": "waiting", "url": url, "expires_at": time.time() + 600,
                           "message": "登录页已就绪。"})
        try:
            opened = webbrowser.open(url)
        except Exception:
            opened = False
        if not opened:
            # macOS 等环境可能不响应默认浏览器调用；链接仍可安全地由对话展示给用户点击。
            write_auth_status({"status": "waiting", "url": url, "expires_at": time.time() + 600,
                               "message": "登录页未自动打开，请手动点击打开登录页。"})
        deadline = time.monotonic() + max(1, min(int(seconds), 600))
        while not finished and time.monotonic() < deadline:
            server.handle_request()
    if not finished:
        write_auth_status({"status": "expired"})
        return {"status": "expired", "message": "登录页已过期；请重新执行登录。"}
    return auth_status()


def wait_login(seconds=20):
    """有界等待授权完成，仅返回非敏感状态，宿主可连续调用保持任务运行。"""
    deadline = time.monotonic() + max(0, min(seconds, 30))
    unreachable_since = None
    while True:
        value = auth_status()
        if value.get("status") in ("waiting", "waiting_terminal") and value.get("expires_at", 0) <= time.time():
            return {"status": "expired"}
        if value.get("status") not in ("starting", "waiting", "waiting_terminal") or time.monotonic() >= deadline:
            return value
        if value.get("status") == "waiting" and value.get("url"):
            if local_login_ready(value["url"]):
                unreachable_since = None
            elif unreachable_since is None:
                unreachable_since = time.monotonic()
            elif time.monotonic() - unreachable_since >= 15:
                # 连续不可达才兜底；最后重读一次，避免覆盖刚完成的登录。
                latest = auth_status()
                if latest.get("status") != "waiting":
                    return latest
                return {"status": "failed", "message": "本机登录页持续不可达，可改用独立终端登录。"}
        time.sleep(1)


def terminal_login_command(base):
    """生成不含账号密码的交互式终端登录命令。"""
    script = Path(__file__).resolve().with_name("zentao.py")
    return [sys.executable, str(script), "auth", "--base-url", base_url(base), "--terminal"]


def macos_terminal_launcher(command):
    """创建可重复覆盖的 macOS 启动文件，避免依赖 AppleScript 自动化权限。"""
    launcher = state_dir() / "zentao-login.command"
    content = "\n".join([
        "#!/bin/sh",
        "# 账号和密码由用户在终端输入，本文件只保存无敏感信息的启动命令。",
        shlex.join(command),
        "code=$?",
        "printf '\\n登录流程已结束，按回车关闭此窗口。'",
        "read -r _",
        "exit \"$code\"",
        "",
    ])
    private_write(launcher, content)
    launcher.chmod(0o700)
    return launcher


def open_terminal_login(base):
    """打开可见的独立终端并运行登录命令，凭据仍由用户本人输入。"""
    command = terminal_login_command(base)
    write_auth_status({
        "status": "waiting_terminal",
        "expires_at": time.time() + 600,
        "message": "已打开独立终端，请在终端中输入禅道账号和密码。",
    })
    try:
        if sys.platform == "darwin":
            # 打开本机生成的 .command 文件即可由 Terminal 执行，不需要宿主获得
            # “自动化控制 Terminal”的 Apple Event 权限，也支持中文和空格路径。
            launcher = macos_terminal_launcher(command)
            subprocess.run(["open", "-a", "Terminal", str(launcher)], check=True, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif os.name == "nt":
            windows_terminal = shutil.which("wt.exe")
            if windows_terminal:
                subprocess.Popen(
                    [windows_terminal, "new-tab", "--title", "禅道登录", *command],
                    cwd=str(Path(__file__).resolve().parent),
                    close_fds=True,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                subprocess.Popen(
                    command,
                    cwd=str(Path(__file__).resolve().parent),
                    close_fds=True,
                    creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP,
                )
        else:
            terminals = [
                ("x-terminal-emulator", ["-e"]),
                ("gnome-terminal", ["--"]),
                ("konsole", ["-e"]),
            ]
            for name, prefix in terminals:
                executable = shutil.which(name)
                if executable:
                    subprocess.Popen([executable, *prefix, *command], close_fds=True, start_new_session=True)
                    break
            else:
                raise OSError("未找到可用终端")
    except (OSError, subprocess.SubprocessError):
        write_auth_status({
            "status": "failed",
            "message": "无法自动打开独立终端，请在自己的终端执行 auth --terminal。",
        })
        return auth_status()
    return auth_status()


def start_login(base, terminal=False, terminal_window=False, wait=False, seconds=600):
    # 未配置且未传入站点时，交回宿主索取地址；其余情况先规范并探测再开登录页。
    if not base:
        base = setting('base_url')
    if not base:
        raise SkillError(NEED_BASE_URL)
    base, notice = locate_base(base)

    def answered(value):
        """把站点说明附在结果上，让宿主能转述「已自动升级」或「正在用明文 HTTP」。"""
        return {**value, "notice": notice} if notice and isinstance(value, dict) else value

    if terminal and terminal_window:
        raise SkillError("--terminal 与 --terminal-window 不能同时使用。")
    if terminal_window:
        return answered(open_terminal_login(base))
    if terminal:
        if not sys.stdin.isatty():
            raise SkillError("终端登录必须由用户在自己的交互终端执行，禁止管道传入凭据。")
        # 用户终端由本人操作，智能体不得使用工具 stdin 代填。
        account = input("禅道账号：").strip()
        # 独立终端中明文显示输入，便于用户在回车前核对；不经过智能体 stdin。
        password = input("禅道密码（输入可见，回车确认）：")
        try:
            return answered({"status": "authenticated", **login(base, account, password)})
        except SkillError as exc:
            write_auth_status({"status": "failed", "message": str(exc)})
            raise
    if wait:
        # 在当前进程内等待登录完成。宿主若会把 detached 子进程一并回收，
        # 只有这条通路能把服务留到用户提交表单的那一刻。
        return answered(browser_login(base, seconds))
    pending = state_dir() / "auth-status.json"
    if pending.exists():
        value = auth_status()
        # 重开授权时只复用仍可访问的旧页面，避免返回已失效的浏览器标签。
        if (value.get("status") == "waiting" and value.get("expires_at", 0) > time.time()
                and value.get("url") and local_login_ready(value["url"])):
            return answered(value)
    write_auth_status({"status": "starting"})
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
              "close_fds": True, "cwd": str(Path(__file__).resolve().parent)}
    if os.name == "nt":
        # 某些宿主会在命令结束时关闭作业；允许时让网页登录服务脱离该作业。
        kwargs["creationflags"] = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
                                    | subprocess.CREATE_NEW_PROCESS_GROUP)
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        try:
            subprocess.Popen([sys.executable, os.path.abspath(__file__), base],
                             **{**kwargs, "creationflags": kwargs["creationflags"] | breakaway})
        except OSError:
            if not breakaway:
                raise
            # 系统禁止脱离作业时仍按原方式启动，再由持续探测判断页面可用性。
            subprocess.Popen([sys.executable, os.path.abspath(__file__), base], **kwargs)
    else:
        kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, os.path.abspath(__file__), base], **kwargs)
    for _ in range(60):
        value = auth_status()
        if value.get("status") == "waiting" and value.get("url") and local_login_ready(value["url"]):
            return answered(value)
        if value.get("status") not in ("starting", "waiting"):
            return answered(value)
        time.sleep(0.1)
    return answered({
        "status": "failed",
        "message": "本机登录页未能在 6 秒内就绪，可改用独立终端登录。",
    })


if __name__ == "__main__":
    try:
        browser_login(base_url(sys.argv[1]))
    except Exception:
        # 后台登录进程不打印异常上下文；仅留下不含凭据的状态。
        write_auth_status({"status": "failed", "message": "无法启动浏览器登录，请使用本人终端登录。"})
