"""Сессии MAX: парсинг web-сессий, автопоиск эндпоинта, конвертация."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from config import config

FOUND_FILE = Path(__file__).parent / "found_endpoint.json"


@dataclass
class WebSession:
    token: str
    viewer_id: int | None = None
    device_id: str | None = None

    @property
    def short(self) -> str:
        t = self.token
        return f"{t[:12]}...{t[-8:]}" if len(t) > 30 else t


# ------------------------------ парсинг ----------------------------------- #

AUTH_RE = re.compile(
    r"localStorage\.setItem\(\s*['\"]__oneme_auth['\"]\s*,\s*['\"](.*?)['\"]\s*\)",
    re.DOTALL,
)
DEVICE_RE = re.compile(
    r"localStorage\.setItem\(\s*['\"]__oneme_device_id['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)",
)
JSON_AUTH_RE = re.compile(
    r'\{\s*"token"\s*:\s*"([^"]{40,})"\s*(?:,\s*"viewerId"\s*:\s*(\d+)\s*)?\}'
)


def parse_sessions(text: str) -> list[WebSession]:
    sessions: list[WebSession] = []
    seen: set[str] = set()

    device_ids = DEVICE_RE.findall(text or "")
    default_device = device_ids[0] if device_ids else None

    for m in AUTH_RE.finditer(text or ""):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        token = data.get("token")
        if not token or token in seen:
            continue
        seen.add(token)
        sessions.append(WebSession(
            token=token,
            viewer_id=data.get("viewerId"),
            device_id=data.get("device_id") or default_device,
        ))

    for m in JSON_AUTH_RE.finditer(text or ""):
        token = m.group(1)
        if token in seen:
            continue
        seen.add(token)
        sessions.append(WebSession(
            token=token,
            viewer_id=int(m.group(2)) if m.group(2) else None,
            device_id=default_device,
        ))

    if not sessions:
        for raw in re.findall(r"[A-Za-z0-9_-]{40,}", text or ""):
            if raw in seen:
                continue
            seen.add(raw)
            sessions.append(WebSession(token=raw, device_id=default_device))

    return sessions


def session_info(s: WebSession) -> str:
    return (
        f"токен: {s.short}\n"
        f"viewerId: {s.viewer_id if s.viewer_id is not None else '?'}\n"
        f"device_id: {s.device_id or '?'}\n"
        f"длина токена: {len(s.token)}"
    )


# --------------------------- сохранение находки ---------------------------- #

def save_found_endpoint(url: str) -> None:
    config.max_auth_url = url
    FOUND_FILE.write_text(json.dumps({"max_auth_url": url}), "utf-8")


def load_found_endpoint() -> None:
    if FOUND_FILE.exists():
        try:
            data = json.loads(FOUND_FILE.read_text("utf-8"))
            url = data.get("max_auth_url")
            if url:
                config.max_auth_url = url
        except Exception:  # noqa: BLE001
            pass


# --------------------------- запросы --------------------------------------- #

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _android_headers() -> dict[str, str]:
    return {
        "User-Agent": config.android_user_agent,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _body_for(s: WebSession, device_id: str | None = None) -> dict:
    return {
        "token": s.token,
        "viewerId": s.viewer_id,
        "deviceId": device_id or s.device_id or str(uuid.uuid4()),
        "client": config.android_client_id,
        "platform": "android",
        "appVersion": config.android_app_version,
    }


def _extract_token(data: dict) -> str | None:
    for key in ("token", "access_token", "sessionToken", "authToken",
                "session_token"):
        if data.get(key):
            return str(data[key])
    for sub in data.values():
        if isinstance(sub, dict):
            for key in ("token", "access_token"):
                if sub.get(key):
                    return str(sub[key])
    return None


async def convert_via_server(s: WebSession) -> tuple[bool, str]:
    """Конвертация web -> android через настроенный эндпоинт."""
    if not config.max_auth_url:
        return False, "Эндпоинт не найден. Сначала запусти автопоиск."

    body = _body_for(s)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(config.max_auth_url, json=body,
                                     headers=_android_headers(),
                                     follow_redirects=True)
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                return False, f"200, но не JSON: {resp.text[:150]}"
            token = _extract_token(data)
            if token:
                return True, token
            return False, f"200, но токена нет: {str(data)[:200]}"
        return False, f"{resp.status_code}: {resp.text[:200]}"
    except httpx.HTTPError as exc:
        return False, f"Сетевая ошибка: {exc}"


# ------------------------- автопоиск эндпоинта ------------------------------ #

APP_DOMAINS = [
    "https://maxmessenger.ru",
    "https://web.maxmessenger.ru",
    "https://api.maxmessenger.ru",
]

STATIC_PATHS = [
    "/api/auth", "/api/login", "/api/session", "/api/v1/auth",
    "/auth", "/login", "/session", "/api/sessions",
]

# ВСЕ http(s) URL из JS -- фильтруем по ключевым словам потом
JS_HTTP_URL_RE = re.compile(r'https?://[A-Za-z0-9._~:/?#\[\]@!$&\'()*+,;=%-]{4,150}')

# любые строки в кавычках, похожие на api-пути
JS_PATH_KEYWORD_RE = re.compile(
    r'["\'](/[A-Za-z0-9_./-]{2,80})["\']'
)

# конфигоподобные присваивания
JS_CONFIG_RE = re.compile(
    r'(?:apiUrl|baseURL|API_URL|API_BASE|apiBase|endpoint|api_host|apiHost)'
    r'["\']?\s*[:=]\s*["\']([^"\']{4,120})["\']',
    re.IGNORECASE,
)

PATH_KEYWORDS = ("api", "auth", "session", "token", "login", "oauth", "user")

SCRIPT_RE = re.compile(r'<script[^>]+src=["\']([^"\']+)["\']')

PAUSE_BETWEEN_REQUESTS = 1.2


def _harvest_js(js_text: str, base_dom: str,
                candidates: set[str], report: list[str]) -> None:
    """Вытаскивает из JS-кода всё, что похоже на адреса API."""
    # 1. конфиги вида apiUrl: "https://..."
    for val in JS_CONFIG_RE.findall(js_text):
        v = val.rstrip("/")
        if v.startswith("http"):
            candidates.add(v)
            candidates.add(v + "/auth")
            candidates.add(v + "/session")
    # 2. полные URL с ключевыми словами
    for url in JS_HTTP_URL_RE.findall(js_text):
        low = url.lower()
        if any(k in low for k in PATH_KEYWORDS) and "cloudflare" not in low:
            candidates.add(url.rstrip("/"))
    # 3. относительные пути с ключевыми словами
    for p in JS_PATH_KEYWORD_RE.findall(js_text):
        if any(k in p.lower() for k in PATH_KEYWORDS) and "." not in p:
            candidates.add(base_dom.rstrip("/") + p)


async def _try_convert(client: httpx.AsyncClient, url: str,
                       s: WebSession) -> tuple[bool, str]:
    try:
        r = await client.post(url, json=_body_for(s),
                              headers=_android_headers(),
                              follow_redirects=True)
    except httpx.HTTPError as exc:
        return False, f"ERR {url} :: {str(exc)[:80]}"
    if r.status_code == 200:
        try:
            token = _extract_token(r.json())
        except ValueError:
            token = None
        if token:
            return True, token
        return False, f"200 {url} :: нет токена: {r.text[:80]}"
    if r.status_code == 429:
        await asyncio.sleep(3)
    return False, f"{r.status_code} {url} :: {r.text[:60]}"


async def auto_discover(s: WebSession) -> str:
    report: list[str] = []
    candidates: set[str] = set()
    live_domains: list[str] = []

    async with httpx.AsyncClient(timeout=15) as client:
        for dom in APP_DOMAINS:
            try:
                r = await client.get(dom, headers=BROWSER_HEADERS,
                                     follow_redirects=True)
            except httpx.HTTPError as exc:
                report.append(f"ERR GET {dom} :: {str(exc)[:60]}")
                continue
            report.append(f"{r.status_code} GET {dom}")
            if r.status_code < 400:
                live_domains.append(dom)

        if not live_domains:
            report.append("Ни один домен не отвечает.")
            return "\n".join(report)

        for dom in live_domains:
            try:
                page = await client.get(dom, headers=BROWSER_HEADERS,
                                        follow_redirects=True)
            except httpx.HTTPError:
                continue
            srcs = SCRIPT_RE.findall(page.text)[:12]
            # также тянем inline-скрипты самой страницы
            _harvest_js(page.text, dom, candidates, report)
            for src in srcs:
                js_url = src if src.startswith("http") else dom + src
                try:
                    jr = await client.get(js_url, headers=BROWSER_HEADERS,
                                          follow_redirects=True)
                except httpx.HTTPError:
                    continue
                if jr.status_code == 200 and len(jr.text) > 500:
                    before = len(candidates)
                    _harvest_js(jr.text, dom, candidates, report)
                    report.append(f"JS ...{js_url[-30:]}: "
                                  f"+{len(candidates) - before} кандидатов")

        candidates.update(dom + p for dom in live_domains
                          for p in STATIC_PATHS)

        report.append(f"Живых доменов: {len(live_domains)}, "
                      f"кандидатов: {len(candidates)}")

        for url in sorted(candidates)[:60]:
            ok, info = await _try_convert(client, url, s)
            report.append(("++ " if ok else "-- ") + info[:110])
            if ok:
                save_found_endpoint(url)
                report.append(f"\nСОХРАНЕНО: {url}")
                return "\n".join(report)
            await asyncio.sleep(PAUSE_BETWEEN_REQUESTS)

    report.append("\nРабочий эндпоинт не найден с этого IP/сети.")
    return "\n".join(report)