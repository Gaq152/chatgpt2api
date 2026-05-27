from __future__ import annotations

import base64
import hashlib
import json
import random
import secrets
import string
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests
import urllib3
from curl_cffi import requests as curl_requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from services.account_service import account_service
from services.register import mail_provider

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class AccountDeactivatedError(RuntimeError):
    """OpenAI 返回 account_deactivated，账号已被永久封禁。"""


_blocked_domains: dict[str, dict] = {}
_blocked_domains_lock = threading.Lock()
_blocked_domains_loaded = False

_DOMAIN_BLOCK_KEYWORDS = ("unsupported_email", "not supported", "邮箱域名很可能因滥用被封禁")


def _get_storage():
    from services.config import config as app_config
    return app_config.get_storage_backend()


def _load_blocked_domains() -> None:
    global _blocked_domains_loaded
    if _blocked_domains_loaded:
        return
    try:
        storage = _get_storage()
        items = storage.load_blocked_domains()
        with _blocked_domains_lock:
            for item in items:
                domain = str(item.get("domain") or "").lower().strip()
                if domain:
                    _blocked_domains[domain] = item
            _blocked_domains_loaded = True
        if _blocked_domains:
            log(f"已从存储加载 {len(_blocked_domains)} 个被封禁域名", "yellow")
    except Exception as exc:
        print(f"[blocked_domains] 加载失败: {exc}")


def _persist_blocked_domains() -> None:
    try:
        with _blocked_domains_lock:
            items = list(_blocked_domains.values())
        _get_storage().save_blocked_domains(items)
    except Exception as exc:
        print(f"[blocked_domains] 持久化失败: {exc}")


def _block_domain(email: str, reason: str) -> None:
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    if not domain:
        return
    with _blocked_domains_lock:
        if domain in _blocked_domains:
            return
        _blocked_domains[domain] = {
            "domain": domain,
            "reason": reason,
            "blocked_at": datetime.now(timezone.utc).isoformat(),
        }
    log(f"域名 {domain} 已加入黑名单: {reason}", "yellow")
    _persist_blocked_domains()


def _is_domain_blocked(email: str) -> bool:
    _load_blocked_domains()
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    if not domain:
        return False
    with _blocked_domains_lock:
        return domain in _blocked_domains


def remove_blocked_domain(domain: str) -> bool:
    domain = domain.lower().strip()
    with _blocked_domains_lock:
        if domain not in _blocked_domains:
            return False
        del _blocked_domains[domain]
    _persist_blocked_domains()
    log(f"域名 {domain} 已从黑名单移除", "green")
    return True


def list_blocked_domains() -> list[dict]:
    _load_blocked_domains()
    with _blocked_domains_lock:
        return list(_blocked_domains.values())


base_dir = Path(__file__).resolve().parent
config = {
    "mail": {
        "request_timeout": 30,
        "wait_timeout": 30,
        "wait_interval": 2,
        "providers": [],
    },
    "proxy": "",
    "total": 10,
    "threads": 3,
}
register_config_file = base_dir.parents[1] / "data" / "register.json"
try:
    saved_config = json.loads(register_config_file.read_text(encoding="utf-8"))
    config.update({key: saved_config[key] for key in ("mail", "proxy", "total", "threads") if key in saved_config})
except Exception:
    pass

auth_base = "https://auth.openai.com"
platform_base = "https://platform.openai.com"
platform_oauth_client_id = "app_2SKx67EdpoN0G6j64rFvigXD"
platform_oauth_redirect_uri = f"{platform_base}/auth/callback"
# Codex CLI / cliproxyapi 导出的账号用的是另一套 OAuth client（azp=app_EMoamEEZ73f0CkXaXp7hrann）。
# refresh_token 颁发时绑了哪个 client_id，刷新时就得用那个，否则上游会 invalid_grant。
codex_oauth_client_id = "app_EMoamEEZ73f0CkXaXp7hrann"
codex_oauth_redirect_uri = "http://localhost:1455/auth/callback"
platform_oauth_audience = "https://api.openai.com/v1"
platform_auth0_client = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
user_agent = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
sec_ch_ua = '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"'
sec_ch_ua_full_version_list = '"Chromium";v="145.0.0.0", "Not:A-Brand";v="99.0.0.0", "Google Chrome";v="145.0.0.0"'
default_timeout = 30
print_lock = threading.Lock()
stats_lock = threading.Lock()
stats = {"done": 0, "success": 0, "fail": 0, "start_time": 0.0}
register_log_sink = None

common_headers = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": auth_base,
    "priority": "u=1, i",
    "user-agent": user_agent,
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

navigate_headers = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": user_agent,
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}


def log(text: str, color: str = "") -> None:
    colors = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m"}
    if register_log_sink:
        try:
            register_log_sink(text, color)
        except Exception:
            pass
    with print_lock:
        prefix = colors.get(color, "")
        suffix = "\033[0m" if prefix else ""
        print(f"{prefix}{datetime.now().strftime('%H:%M:%S')} {text}{suffix}")


def step(index: int, text: str, color: str = "") -> None:
    log(f"[任务{index}] {text}", color)


def _make_trace_headers() -> dict[str, str]:
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    return {
        "traceparent": f"00-{uuid.uuid4().hex}-{format(int(parent_id), '016x')}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def _generate_pkce() -> tuple[str, str]:
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    value = list(
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%")
        + "".join(secrets.choice(chars) for _ in range(max(0, length - 4)))
    )
    random.shuffle(value)
    return "".join(value)


def _random_name() -> tuple[str, str]:
    return random.choice(["James", "Robert", "John", "Michael", "David", "Mary", "Emma", "Olivia"]), random.choice(
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
    )


def _random_birthdate() -> str:
    return f"{random.randint(1996, 2006):04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


def _response_json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _decode_jwt_payload(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def create_mailbox(username: str | None = None) -> dict:
    return mail_provider.create_mailbox(config["mail"], username)


def wait_for_code(mailbox: dict) -> str | None:
    return mail_provider.wait_for_code(config["mail"], mailbox)


class SentinelTokenGenerator:
    MAX_ATTEMPTS = 500000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id: str, ua: str):
        self.device_id = device_id
        self.user_agent = ua
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self) -> list:
        perf_now = random.uniform(1000, 50000)
        return [
            "1920x1080",
            time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),
            4294705152,
            random.random(),
            self.user_agent,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None,
            None,
            "en-US",
            random.random(),
            random.choice(["vendorSub-undefined", "plugins-undefined", "mimeTypes-undefined", "hardwareConcurrency-undefined"]),
            random.choice(["location", "implementation", "URL", "documentURI", "compatMode"]),
            random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"]),
            perf_now,
            self.sid,
            "",
            random.choice([4, 8, 12, 16]),
            time.time() * 1000 - perf_now,
        ]

    @staticmethod
    def _b64(data) -> str:
        return base64.b64encode(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).decode("ascii")

    def generate_requirements_token(self) -> str:
        data = self._get_config()
        data[3] = 1
        data[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._b64(data)

    def generate_token(self, seed: str, difficulty: str) -> str:
        start = time.time()
        data = self._get_config()
        difficulty = str(difficulty or "0")
        for i in range(self.MAX_ATTEMPTS):
            data[3] = i
            data[9] = round((time.time() - start) * 1000)
            payload = self._b64(data)
            if self._fnv1a_32(seed + payload)[: len(difficulty)] <= difficulty:
                return "gAAAAAB" + payload + "~S"
        return "gAAAAAB" + self.ERROR_PREFIX + self._b64(str(None))


def build_sentinel_token(session: requests.Session, device_id: str, flow: str) -> str:
    generator = SentinelTokenGenerator(device_id, user_agent)
    resp = session.post(
        "https://sentinel.openai.com/backend-api/sentinel/req",
        data=json.dumps({"p": generator.generate_requirements_token(), "id": device_id, "flow": flow}),
        headers={
            "Content-Type": "text/plain;charset=UTF-8",
            "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
            "Origin": "https://sentinel.openai.com",
            "User-Agent": user_agent,
            "sec-ch-ua": sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
        timeout=20,
        verify=False,
    )
    data = _response_json(resp)
    token = str(data.get("token") or "").strip()
    if resp.status_code != 200 or not token:
        raise RuntimeError(f"sentinel_req_failed_{resp.status_code}")
    pow_data = data.get("proofofwork") or {}
    p_value = (
        generator.generate_token(str(pow_data.get("seed") or ""), str(pow_data.get("difficulty") or "0"))
        if pow_data.get("required") and pow_data.get("seed")
        else generator.generate_requirements_token()
    )
    return json.dumps({"p": p_value, "t": "", "c": token, "id": device_id, "flow": flow}, separators=(",", ":"))


def _is_socks_proxy(proxy: str) -> bool:
    candidate = str(proxy or "").strip().lower()
    return candidate.startswith("socks5://") or candidate.startswith("socks5h://")


def create_session(proxy: str = "") -> Any:
    if _is_socks_proxy(proxy):
        return curl_requests.Session(impersonate="chrome", verify=False, proxy=proxy)
    session = requests.Session()
    retry = Retry(total=2, connect=2, read=2, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.verify = False
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def request_with_local_retry(session: requests.Session, method: str, url: str, retry_attempts: int = 3, **kwargs):
    last_error = ""
    for _ in range(max(1, retry_attempts)):
        try:
            return session.request(method.upper(), url, timeout=default_timeout, **kwargs), ""
        except Exception as error:
            last_error = str(error)
            time.sleep(1)
    return None, last_error


def validate_otp(session: requests.Session, device_id: str, code: str):
    headers = dict(common_headers)
    headers["referer"] = f"{auth_base}/email-verification"
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    if resp is not None and resp.status_code == 200:
        return resp, ""
    headers["openai-sentinel-token"] = build_sentinel_token(session, device_id, "authorize_continue")
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=False)
    return resp, error


def extract_oauth_callback_params_from_url(url: str) -> dict[str, str] | None:
    if not url:
        return None
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:
        return None
    code = str((params.get("code") or [""])[0]).strip()
    if not code:
        return None
    return {"code": code, "state": str((params.get("state") or [""])[0]).strip(), "scope": str((params.get("scope") or [""])[0]).strip()}


def extract_oauth_callback_params_from_consent_session(session: requests.Session, consent_url: str, device_id: str) -> dict[str, str] | None:
    if consent_url.startswith("/"):
        consent_url = f"{auth_base}{consent_url}"
    current_url = consent_url
    for _ in range(10):
        response = session.get(current_url, headers=navigate_headers, verify=False, timeout=30, allow_redirects=False)
        callback_params = extract_oauth_callback_params_from_url(str(response.url)) or extract_oauth_callback_params_from_url(str(response.headers.get("Location") or "").strip())
        if callback_params:
            return callback_params
        location = str(response.headers.get("Location") or "").strip()
        if response.status_code not in (301, 302, 303, 307, 308) or not location:
            break
        current_url = f"{auth_base}{location}" if location.startswith("/") else location
    raw = session.cookies.get("oai-client-auth-session", domain=".auth.openai.com") or session.cookies.get("oai-client-auth-session")
    if not raw:
        return None
    try:
        first_part = raw.split(".")[0]
        padding = 4 - len(first_part) % 4
        if padding != 4:
            first_part += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(first_part))
        workspace_id = payload["workspaces"][0]["id"]
    except Exception:
        return None
    headers = dict(common_headers)
    headers["referer"] = consent_url
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    ws_resp = session.post(f"{auth_base}/api/accounts/workspace/select", json={"workspace_id": workspace_id}, headers=headers, verify=False, timeout=30, allow_redirects=False)
    callback_params = extract_oauth_callback_params_from_url(str(ws_resp.headers.get("Location") or "").strip())
    if callback_params:
        return callback_params
    ws_data = _response_json(ws_resp)
    orgs = ((ws_data.get("data") or {}).get("orgs") or []) if isinstance(ws_data, dict) else []
    if not orgs:
        return None
    org_id = str((orgs[0] or {}).get("id") or "").strip()
    project_id = str(((orgs[0] or {}).get("projects") or [{}])[0].get("id") or "").strip()
    if not org_id:
        return None
    org_headers = dict(common_headers)
    org_headers["referer"] = str(ws_data.get("continue_url") or consent_url)
    org_headers["oai-device-id"] = device_id
    org_headers.update(_make_trace_headers())
    body = {"org_id": org_id}
    if project_id:
        body["project_id"] = project_id
    org_resp = session.post(f"{auth_base}/api/accounts/organization/select", json=body, headers=org_headers, verify=False, timeout=30, allow_redirects=False)
    return extract_oauth_callback_params_from_url(str(org_resp.headers.get("Location") or "").strip())


def exchange_platform_tokens(session: requests.Session, device_id: str, code_verifier: str, consent_url: str) -> dict | None:
    callback_params = extract_oauth_callback_params_from_consent_session(session, consent_url, device_id)
    if not callback_params:
        # 回退方案：直接导航 consent URL（allow_redirects=True），从最终 URL 提取 code
        print(f"[exchange_platform_tokens] 主方案失败，尝试回退方案, continue_url={consent_url[:120]}")
        try:
            r = session.get(consent_url, headers=navigate_headers, allow_redirects=True, verify=False, timeout=30)
            final_url = str(r.url)
            print(f"[exchange_platform_tokens] 回退 final_url={final_url[:120]}")
            callback_params = extract_oauth_callback_params_from_url(final_url)
            if not callback_params:
                for hist in getattr(r, "history", []) or []:
                    loc = str(hist.headers.get("Location") or "")
                    callback_params = extract_oauth_callback_params_from_url(loc)
                    if callback_params:
                        break
        except Exception as e:
            print(f"[exchange_platform_tokens] 回退方案异常: {e}")
    if not callback_params:
        print("[exchange_platform_tokens] 所有方案均无法提取 OAuth code")
        return None
    code = str(callback_params.get("code") or "").strip()
    if not code:
        return None
    resp = create_session(config["proxy"]).post(
        f"{auth_base}/oauth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": platform_oauth_redirect_uri,
            "client_id": platform_oauth_client_id,
            "code_verifier": code_verifier,
        },
        verify=False,
        timeout=60,
    )
    data = _response_json(resp)
    if resp.status_code != 200 or not data.get("access_token") or not data.get("refresh_token") or not data.get("id_token"):
        return None
    payload = _decode_jwt_payload(str(data.get("id_token") or "")) or _decode_jwt_payload(str(data.get("access_token") or ""))
    return {
        "email": str(payload.get("email") or "").strip(),
        "access_token": str(data.get("access_token") or "").strip(),
        "refresh_token": str(data.get("refresh_token") or "").strip(),
        "id_token": str(data.get("id_token") or "").strip(),
    }


def _decode_jwt_payload(token: str) -> dict:
    """解 JWT 第二段 payload，失败返回空 dict。仅用于读 azp / iat / exp 等公开字段。"""
    try:
        import base64
        payload = str(token or "").split(".")[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii"))) or {}
    except Exception:
        return {}


def _resolve_oauth_client_id(access_token: str) -> str:
    """按 access_token JWT 选刷新用的 client_id，解不出兜底 platform。

    OpenAI 不同来源 token 里携带的字段不一样：
    - codex CLI 颁发的 token 用 OIDC 标准的 azp = app_EMoamEEZ73f0CkXaXp7hrann
    - platform 注册机颁发的没有 azp，而是用 client_id = app_2SKx67EdpoN0G6j64rFvigXD
    两个都看，都不命中走 platform 兜底（自家注册机就走这个）。
    """
    payload = _decode_jwt_payload(access_token)
    candidate = str(payload.get("azp") or payload.get("client_id") or "").strip()
    if candidate == codex_oauth_client_id:
        return codex_oauth_client_id
    return platform_oauth_client_id


def refresh_platform_tokens(refresh_token: str, access_token: str = "") -> dict | None:
    """用 refresh_token 走 OAuth 端点换一组新的 access/refresh/id token。

    refresh 端点会轮换 refresh_token，调用方需要把响应里的新值写回。
    某些情况下 id_token 不会重新返回，这种时候由调用方决定是否保留旧值。

    access_token 用来解 azp 选 client_id（platform / codex 两套）。refresh_token 是
    不透明字符串无法识别来源；JWT 颁发时绑的 client_id 必须和刷新时一致，否则上游
    返回 invalid_grant。不传 access_token 时默认走 platform。
    """
    candidate = str(refresh_token or "").strip()
    if not candidate:
        return None
    client_id = _resolve_oauth_client_id(access_token)
    try:
        resp = create_session(config["proxy"]).post(
            f"{auth_base}/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": candidate,
                "scope": "openid profile email offline_access",
            },
            verify=False,
            timeout=60,
        )
    except Exception as exc:
        print(f"[refresh_platform_tokens] request failed (client_id={client_id}): {exc}")
        return None
    data = _response_json(resp)
    if resp.status_code != 200 or not data.get("access_token"):
        print(f"[refresh_platform_tokens] http {resp.status_code} client_id={client_id} body={str(data)[:200]}")
        return None
    return {
        "access_token": str(data.get("access_token") or "").strip(),
        "refresh_token": str(data.get("refresh_token") or "").strip(),
        "id_token": str(data.get("id_token") or "").strip(),
        "expires_in": int(data.get("expires_in") or 0),
    }


def relogin_for_tokens(email: str, password: str, stored_mailbox: dict | None = None) -> dict | None:
    """用邮箱+密码重新登录换取全新三件套。

    refresh_token 作废后的自动恢复手段。登录可能触发 OTP 验证码，
    此时优先使用注册时存储的 mailbox 数据（含 provider token）接码，
    其次尝试用配置的邮件服务为同一地址重建邮箱接码。
    """
    if not email or not password:
        return None
    mailbox: dict | None = None
    if isinstance(stored_mailbox, dict) and stored_mailbox.get("address"):
        mailbox = dict(stored_mailbox)
    if not mailbox:
        local_part = email.split("@")[0] if "@" in email else None
        try:
            mailbox = create_mailbox(local_part)
        except Exception:
            mailbox = {"address": email}
    provider_type = str(mailbox.get("provider") or "").strip()
    if provider_type:
        try:
            mail_provider._create_provider(config["mail"], provider_type, str(mailbox.get("provider_ref") or ""))
        except RuntimeError as exc:
            log(f"[relogin] {email} 跳过重登: {exc}", "yellow")
            return None
    registrar = PlatformRegistrar(config["proxy"])
    try:
        tokens = registrar._login_and_exchange_tokens(email, password, mailbox, 0)
        log(f"[relogin] {email} 密码重登成功", "green")
        return tokens
    except Exception as exc:
        msg = str(exc)
        log(f"[relogin] {email} 密码重登失败: {msg}", "red")
        if "account_deactivated" in msg:
            raise AccountDeactivatedError(msg) from exc
        return None
    finally:
        registrar.close()


class PlatformRegistrar:
    def __init__(self, proxy: str = "") -> None:
        self.session = create_session(proxy)
        self.device_id = str(uuid.uuid4())
        self._current_email: str = ""
        self._code_verifier: str = ""

    def close(self) -> None:
        self.session.close()

    def _navigate_headers(self, referer: str = "") -> dict[str, str]:
        headers = dict(navigate_headers)
        if referer:
            headers["referer"] = referer
        return headers

    def _json_headers(self, referer: str) -> dict[str, str]:
        headers = dict(common_headers)
        headers["referer"] = referer
        headers["oai-device-id"] = self.device_id
        headers.update(_make_trace_headers())
        return headers

    def _platform_authorize(self, email: str, index: int) -> None:
        step(index, "开始 platform authorize")
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")
        code_verifier, code_challenge = _generate_pkce()
        self._code_verifier = code_verifier
        params = {
            "issuer": auth_base,
            "client_id": platform_oauth_client_id,
            "audience": platform_oauth_audience,
            "redirect_uri": platform_oauth_redirect_uri,
            "device_id": self.device_id,
            "screen_hint": "login_or_signup",
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": platform_auth0_client,
        }
        resp, error = request_with_local_retry(self.session, "get", f"{auth_base}/api/accounts/authorize?{urlencode(params)}", headers=self._navigate_headers(f"{platform_base}/"), allow_redirects=True, verify=False)
        if resp is None or resp.status_code != 200:
            err = _response_json(resp).get("error", {}) if resp is not None else {}
            detail = f": {err.get('code', '')} - {err.get('message', '')}".strip(" -") if err else ""
            raise RuntimeError(error or f"platform_authorize_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "platform authorize 完成")

    def _register_user(self, email: str, password: str, index: int) -> None:
        step(index, "开始提交注册密码")
        headers = self._json_headers(f"{auth_base}/create-account/password")
        headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "username_password_create")
        resp, error = request_with_local_retry(self.session, "post", f"{auth_base}/api/accounts/user/register", json={"username": email, "password": password}, headers=headers, verify=False)
        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            msg = str(data.get("message") or "")
            err_code = ""
            if isinstance(data.get("error"), dict):
                msg = msg or str(data["error"].get("message") or "")
                err_code = str(data["error"].get("code") or "")
            combined = f"{msg} {err_code}"
            if any(kw in combined for kw in _DOMAIN_BLOCK_KEYWORDS) or msg == "Failed to create account. Please try again.":
                _block_domain(email, msg or err_code)
                step(index, "注册失败: 邮箱域名已被封禁，已加入黑名单", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"user_register_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "提交注册密码完成")

    def _send_otp(self, index: int) -> None:
        step(index, "开始发送验证码")
        resp, error = request_with_local_retry(self.session, "get", f"{auth_base}/api/accounts/email-otp/send", headers=self._navigate_headers(f"{auth_base}/create-account/password"), allow_redirects=True, verify=False)
        if resp is None or resp.status_code not in (200, 302):
            raise RuntimeError(error or f"send_otp_http_{getattr(resp, 'status_code', 'unknown')}")
        step(index, "发送验证码完成")

    def _validate_otp(self, code: str, index: int) -> dict:
        step(index, f"开始校验验证码 {code}")
        resp, error = validate_otp(self.session, self.device_id, code)
        if resp is None or resp.status_code != 200:
            body = ""
            try:
                body = (resp.text or "")[:500] if resp is not None else ""
            except Exception:
                pass
            raise RuntimeError(error or f"validate_otp_http_{getattr(resp, 'status_code', 'unknown')}_body={body}")
        step(index, "验证码校验完成")
        return _response_json(resp)

    def _create_account(self, name: str, birthdate: str, index: int) -> dict:
        step(index, "开始创建账号资料")
        headers = self._json_headers(f"{auth_base}/about-you")
        headers["openai-sentinel-token"] = build_sentinel_token(self.session, self.device_id, "oauth_create_account")
        resp, error = request_with_local_retry(self.session, "post", f"{auth_base}/api/accounts/create_account", json={"name": name, "birthdate": birthdate}, headers=headers, verify=False)
        if resp is None or resp.status_code not in (200, 302):
            data = _response_json(resp) if resp is not None else {}
            msg = str(data.get("message") or "")
            err_code = ""
            if isinstance(data.get("error"), dict):
                msg = msg or str(data["error"].get("message") or "")
                err_code = str(data["error"].get("code") or "")
            combined = f"{msg} {err_code}"
            if any(kw in combined for kw in _DOMAIN_BLOCK_KEYWORDS) or msg == "Failed to create account. Please try again.":
                _block_domain(self._current_email, msg or err_code)
                step(index, "创建账号失败: 邮箱域名已被封禁，已加入黑名单", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)}" if data else ""
            raise RuntimeError(error or f"create_account_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "创建账号资料完成")
        return _response_json(resp)

    def _try_exchange_with_register_session(self, continue_url: str, index: int) -> dict | None:
        """优先用注册流程当前 session + _platform_authorize 留下的 code_verifier 换 token。

        注册验证码校验 + create_account 完成后，注册 session 已经具备半登录态，
        理论上 OAuth/PKCE 的 continue_url 这一步可以直接拿到 code，无需再走密码登录。
        失败返回 None，让上层走独立登录回退方案。
        """
        if not self._code_verifier:
            return None
        target_url = (continue_url or "").strip() or f"{auth_base}/sign-in-with-chatgpt/codex/consent"
        try:
            tokens = exchange_platform_tokens(self.session, self.device_id, self._code_verifier, target_url)
        except Exception as exc:
            step(index, f"注册 session 直接换 token 异常，回退独立登录: {exc}", "yellow")
            return None
        if not tokens:
            step(index, "注册 session 未拿到 OAuth code，回退独立登录")
            return None
        step(index, "注册 session 直接换 token 成功")
        return tokens

    def _login_and_exchange_tokens(self, email: str, password: str, mailbox: dict, index: int) -> dict:
        step(index, "开始独立登录换 token")
        login_session = create_session(config["proxy"])
        login_device_id = str(uuid.uuid4())
        login_session.cookies.set("oai-did", login_device_id, domain=".auth.openai.com")
        login_session.cookies.set("oai-did", login_device_id, domain="auth.openai.com")
        code_verifier, code_challenge = _generate_pkce()
        params = {
            "issuer": auth_base,
            "client_id": platform_oauth_client_id,
            "audience": platform_oauth_audience,
            "redirect_uri": platform_oauth_redirect_uri,
            "device_id": login_device_id,
            "screen_hint": "login_or_signup",
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": platform_auth0_client,
        }

        def _login_nav_headers(referer: str = "") -> dict[str, str]:
            h = dict(navigate_headers)
            if referer:
                h["referer"] = referer
            return h

        def _login_json_headers(referer: str) -> dict[str, str]:
            h = dict(common_headers)
            h["referer"] = referer
            h["oai-device-id"] = login_device_id
            h.update(_make_trace_headers())
            return h

        resp, error = request_with_local_retry(
            login_session, "get",
            f"{auth_base}/api/accounts/authorize?{urlencode(params)}",
            headers=_login_nav_headers(f"{platform_base}/"),
            allow_redirects=True, verify=False
        )
        if resp is None:
            raise RuntimeError(error or "platform_login_authorize_failed")
        step(index, "登录 authorize 完成")

        # 提交邮箱（原样，不带 state）
        def _do_authorize_continue():
            h = _login_json_headers(f"{auth_base}/log-in?usernameKind=email")
            h["openai-sentinel-token"] = build_sentinel_token(login_session, login_device_id, "authorize_continue")
            return request_with_local_retry(
                login_session, "post",
                f"{auth_base}/api/accounts/authorize/continue",
                json={"username": {"kind": "email", "value": email}},
                headers=h,
                allow_redirects=False,
                verify=False
            )

        step(index, "开始提交邮箱")
        resp, error = _do_authorize_continue()
        if resp is not None and resp.status_code == 409:
            step(index, "邮箱提交 invalid_state，重新 authorize 后重试")
            # 再次清除 cookie 并重新 authorize
            for cookie in list(login_session.cookies):
                if 'auth.openai.com' in cookie.domain:
                    login_session.cookies.clear(domain=cookie.domain, path=cookie.path, name=cookie.name)
            login_session.cookies.set("oai-did", login_device_id, domain=".auth.openai.com")
            login_session.cookies.set("oai-did", login_device_id, domain="auth.openai.com")
            resp, error = request_with_local_retry(
                login_session, "get",
                f"{auth_base}/api/accounts/authorize?{urlencode(params)}",
                headers=_login_nav_headers(f"{platform_base}/"),
                allow_redirects=True, verify=False
            )
            if resp is None:
                raise RuntimeError(error or "platform_login_authorize_retry_failed")
            resp, error = _do_authorize_continue()

        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            detail = json.dumps(data, ensure_ascii=False) if data else ""
            raise RuntimeError(
                error or f"email_submit_http_{getattr(resp, 'status_code', 'unknown')}"
                + (f": {detail}" if detail else "")
            )
        step(index, "邮箱提交完成")

        # 密码验证
        step(index, "开始密码校验")
        headers = _login_json_headers(f"{auth_base}/log-in/password")
        headers["openai-sentinel-token"] = build_sentinel_token(
            login_session, login_device_id, "password_verify"
        )
        resp, error = request_with_local_retry(
            login_session, "post",
            f"{auth_base}/api/accounts/password/verify",
            json={"password": password},
            headers=headers,
            allow_redirects=False,
            verify=False
        )
        if resp is None or resp.status_code != 200:
            body = ""
            try:
                body = (resp.text or "")[:500] if resp is not None else ""
            except Exception:
                pass
            raise RuntimeError(error or f"password_verify_http_{getattr(resp, 'status_code', '')}_body={body}")
        step(index, "密码校验完成")

        payload = _response_json(resp)
        continue_url = str(payload.get("continue_url") or "").strip()
        page_type = str(((payload.get("page") or {}).get("type")) or "")

        if page_type == "email_otp_verification" or "email-verification" in continue_url or "email-otp" in continue_url:
            step(index, "独立登录需要邮箱验证码")
            code = wait_for_code(mailbox)
            if not code:
                login_session.close()
                raise RuntimeError("独立登录等待验证码超时")
            step(index, f"收到登录验证码: {code}")
            resp, reason = validate_otp(login_session, login_device_id, code)
            if resp is None or resp.status_code != 200:
                print("独立登录验证码校验失败响应:", resp.text if resp is not None else "None")
                data = _response_json(resp) if resp is not None else {}
                message = str((data.get("error") or {}).get("message") or data.get("message") or "").strip()
                login_session.close()
                raise RuntimeError(reason or f"独立登录验证码校验失败{': ' + message if message else ''}")
            otp_payload = _response_json(resp)
            continue_url = str(otp_payload.get("continue_url") or continue_url).strip()
            step(index, "独立登录验证码校验完成")

        if not continue_url:
            continue_url = f"{auth_base}/sign-in-with-chatgpt/codex/consent"
        tokens = exchange_platform_tokens(login_session, login_device_id, code_verifier, continue_url)
        login_session.close()
        if not tokens:
            raise RuntimeError("token换取失败")
        step(index, "token 换取完成")
        return tokens

    def register(self, index: int) -> dict:
        step(index, "开始创建邮箱")
        mailbox = create_mailbox()
        email = str(mailbox.get("address") or "").strip()
        if not email:
            raise RuntimeError("邮箱服务未返回 address")
        self._current_email = email
        if _is_domain_blocked(email):
            domain = email.rsplit("@", 1)[-1]
            raise RuntimeError(f"域名 {domain} 已在黑名单中，跳过注册")
        channel = str(mailbox.get("label") or mailbox.get("provider") or "").strip()
        step(index, f"邮箱创建完成[{channel}]({email})")
        password = _random_password()
        first_name, last_name = _random_name()
        self._platform_authorize(email, index)
        self._register_user(email, password, index)
        self._send_otp(index)
        step(index, "开始等待注册验证码")
        code = wait_for_code(mailbox)
        if not code:
            raise RuntimeError("等待注册验证码超时")
        step(index, f"收到注册验证码: {code}")
        otp_payload = self._validate_otp(code, index)
        create_payload = self._create_account(f"{first_name} {last_name}", _random_birthdate(), index)

        # 优先用注册 session 当前的半登录态 + 最初的 code_verifier 直接换 token，
        # 避免再走独立的密码登录流程触发 password_verify_http_409 (state machine 冲突)。
        continue_url = (
            str(create_payload.get("continue_url") or "").strip()
            or str(otp_payload.get("continue_url") or "").strip()
        )
        tokens = self._try_exchange_with_register_session(continue_url, index)
        if not tokens:
            tokens = self._login_and_exchange_tokens(email, password, mailbox, index)

        mailbox_data = {key: mailbox[key] for key in mailbox if not key.startswith("_")}
        return {
            "email": email,
            "password": password,
            "access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
            "mailbox_data": mailbox_data,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }


def worker(index: int) -> dict:
    start = time.time()
    registrar = PlatformRegistrar(config["proxy"])
    try:
        step(index, "任务启动")
        result = registrar.register(index)
        cost = time.time() - start
        access_token = str(result["access_token"])
        # 兼容旧版 AccountService：没有 add_account_items 则降级用 add_accounts，
        # 避免 token 已经换到、却因为部署版本不匹配落不进号池。
        if hasattr(account_service, "add_account_items"):
            account_service.add_account_items([{**result, "source": "register"}])
        else:
            account_service.add_accounts([access_token])
        # 仅查询账号信息（额度/类型），不轮换 refresh_token。
        # 注册刚拿到的三件套是全新的，立即轮换既浪费又有风险：
        # 如果 HTTP 响应在 proxy/网络中丢失，服务端已轮换但客户端没收到新值，
        # refresh_token 就永久作废。让 watcher 定时刷新来处理后续轮换。
        try:
            account_service.fetch_remote_info(access_token, "register")
        except Exception:
            pass
        with stats_lock:
            stats["done"] += 1
            stats["success"] += 1
            avg = (time.time() - stats["start_time"]) / stats["success"]
        log(f'{result["email"]} 注册成功，本次耗时{cost:.1f}s，全局平均每个号注册耗时{avg:.1f}s', "green")
        return {"ok": True, "index": index, "result": result}
    except Exception as e:
        cost = time.time() - start
        with stats_lock:
            stats["done"] += 1
            stats["fail"] += 1
        log(f"任务{index} 注册失败，本次耗时{cost:.1f}s，原因: {e}", "red")
        return {"ok": False, "index": index, "error": str(e)}
    finally:
        registrar.close()
