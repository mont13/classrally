"""Optional bridge to a central bug collector ("Buggy").

Disabled unless BUGGY_URL, BUGGY_PROJECT_KEY and BUGGY_ORIGIN are set. The
browser never talks to the collector: pages post to /api/bug-report, this
module validates and redacts the report and forwards it server to server
(one attempt, no redirects, short timeout). No collector admin token is used
or needed — creating a report only takes the project key and the registered
origin.

The reporting widget itself is not part of this repository. When
BUGGY_WIDGET_DIR points to a directory with widget.js, widget.css and
capture.css (and optionally build-manifest.json), they are served under
/buggy/ and static/buggy-host.js mounts the widget on every page.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

URL = os.environ.get("BUGGY_URL", "").rstrip("/")
PROJECT_KEY = os.environ.get("BUGGY_PROJECT_KEY", "")
ORIGIN = os.environ.get("BUGGY_ORIGIN", "").rstrip("/")
WIDGET_DIR = Path(os.environ["BUGGY_WIDGET_DIR"]) if os.environ.get("BUGGY_WIDGET_DIR") else None
TIMEOUT_SEC = 8

WIDGET_FILES = {
    "widget.js": "application/javascript; charset=utf-8",
    "widget.css": "text/css; charset=utf-8",
    "capture.css": "text/css; charset=utf-8",
}

# A screenshot is at most 4 MiB decoded (the collector's limit); as base64
# inside JSON that is about 5.6 MB, so the endpoint has its own body limit.
MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024
MAX_BODY_BYTES = 6 * 1024 * 1024
MAX_POPIS = 5000
TYPES = {"CHYBA", "NAMET", "DOTAZ"}
SURFACES = {"page", "dialog"}
PAGES = {"play", "host", "admin", "exam", "profile", "account", "landing", "other"}
ROLES = {"student", "teacher", "anonymous"}

_IMAGE_SIGNATURES = {
    "png": b"\x89PNG\r\n\x1a\n",
    "jpeg": b"\xff\xd8\xff",
    "webp": b"RIFF",
}
_DATA_URL = re.compile(r"^data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)$")
_REQUEST_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")

# Redact what a description must never carry upstream. The collector redacts
# again; doing it here keeps secrets out of its logs and AI triage too.
_REDACTIONS = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[e-mail]"),
    (re.compile(r"(?i)\b(heslo|password|token|secret|api[_ -]?key)\b\s*[:=]\s*\S+"), r"\1: [skryto]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[token]"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "[token]"),
    (re.compile(r"\b[A-Za-z0-9_-]{40,}\b"), "[token]"),
]


def enabled() -> bool:
    return bool(URL and PROJECT_KEY and ORIGIN)


def widget_available() -> bool:
    return enabled() and WIDGET_DIR is not None and (WIDGET_DIR / "widget.js").is_file()


def _asset_version() -> str:
    manifest = WIDGET_DIR / "build-manifest.json" if WIDGET_DIR else None
    try:
        sha = json.loads(manifest.read_text())["assets"]["widget.js"]["sha256"]
        return str(sha)[:12]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "0"


def public_config() -> dict[str, Any]:
    """What the page needs to decide whether to load the widget."""
    if not widget_available():
        return {"enabled": False}
    version = _asset_version()
    return {"enabled": True, "script": f"/buggy/widget.js?v={version}",
            "style": f"/buggy/widget.css?v={version}"}


def widget_file(name: str) -> tuple[bytes, str] | None:
    """Bytes and content type of an allowed widget asset, or None."""
    if not widget_available() or name not in WIDGET_FILES:
        return None
    path = WIDGET_DIR / name
    try:
        return path.read_bytes(), WIDGET_FILES[name]
    except OSError:
        return None


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class BadReport(ValueError):
    """The report is malformed; the message is a short machine code."""


def _screenshot(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise BadReport("screenshot_invalid")
    match = _DATA_URL.match(value)
    if not match:
        raise BadReport("screenshot_invalid")
    kind, payload = match.groups()
    if len(payload) > (MAX_SCREENSHOT_BYTES * 4) // 3 + 4:
        raise BadReport("screenshot_too_large")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise BadReport("screenshot_invalid") from None
    if len(raw) > MAX_SCREENSHOT_BYTES:
        raise BadReport("screenshot_too_large")
    if not raw.startswith(_IMAGE_SIGNATURES[kind]) or (kind == "webp" and raw[8:12] != b"WEBP"):
        raise BadReport("screenshot_invalid")
    return value


def build_report(data: dict[str, Any], *, role: str, reporter: str | None,
                 user_agent: str) -> dict[str, Any]:
    """Validate a page report and build the collector payload."""
    popis = data.get("popis")
    if not isinstance(popis, str) or not popis.strip():
        raise BadReport("popis_required")
    popis = redact(popis.strip())[:MAX_POPIS]
    typ = data.get("typ") if data.get("typ") in TYPES else "CHYBA"
    page = data.get("page") if data.get("page") in PAGES else "other"
    surface = data.get("surface") if data.get("surface") in SURFACES else "page"
    url = data.get("url")
    path = url.split("?", 1)[0].split("#", 1)[0] if isinstance(url, str) and url.startswith("/") \
        and not url.startswith("//") else f"/{page}"
    viewport = data.get("viewport") if isinstance(data.get("viewport"), dict) else {}
    try:
        width, height = int(viewport.get("w", 0)), int(viewport.get("h", 0))
    except (TypeError, ValueError):
        width = height = 0
    phase = data.get("phase")
    payload: dict[str, Any] = {
        "projectKey": PROJECT_KEY,
        "popis": popis,
        "typ": typ,
        "url": path[:500],
        "userAgent": user_agent[:400],
        "technicke_detaily": {
            "aplikace": "ClassRally",
            "stranka": page,
            "faze_hry": phase if isinstance(phase, str) and len(phase) <= 40 else None,
            "role": role if role in ROLES else "anonymous",
            "viewport": {"w": max(0, min(width, 10000)), "h": max(0, min(height, 10000))},
            "surface": surface,
        },
    }
    screenshot = _screenshot(data.get("screenshotDataUrl"))
    if screenshot:
        payload["screenshot_data_url"] = screenshot
    if reporter:
        payload["reporter"] = {"name": reporter[:80]}
    return payload


class _Receipts:
    """clientRequestId -> collector id, so a repeated click is not a second report."""

    def __init__(self, ttl_sec: int = 3600, limit: int = 2000) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, dict[str, Any]]] = {}
        self.ttl_sec, self.limit = ttl_sec, limit

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._items.get(key)
            if item and time.time() - item[0] < self.ttl_sec:
                return item[1]
            return None

    def put(self, key: str, value: dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            if len(self._items) >= self.limit:
                for old in [k for k, (t, _) in self._items.items() if now - t >= self.ttl_sec]:
                    del self._items[old]
                while len(self._items) >= self.limit:
                    del self._items[next(iter(self._items))]
            self._items[key] = (now, value)


RECEIPTS = _Receipts()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class DeliveryError(Exception):
    """The collector did not confirm the report (code: rejected/unavailable)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def forward(payload: dict[str, Any], client_request_id: str | None = None) -> dict[str, Any]:
    """Send one report to the collector; returns {"id", "screenshotSaved"}."""
    key = None
    if client_request_id and _REQUEST_ID.match(client_request_id):
        key = hashlib.sha256(client_request_id.encode()).hexdigest()
        cached = RECEIPTS.get(key)
        if cached:
            return cached
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{URL}/api/reports", data=body, method="POST",
        headers={"Content-Type": "application/json", "Origin": ORIGIN,
                 "User-Agent": "ClassRally-bug-bridge"})
    try:
        with _OPENER.open(request, timeout=TIMEOUT_SEC) as response:
            status = response.status
            answer = json.loads(response.read(65536).decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raise DeliveryError("rejected" if 400 <= exc.code < 500 else "unavailable") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise DeliveryError("unavailable") from None
    report_id = answer.get("id") if isinstance(answer, dict) else None
    if status != 201 or not isinstance(report_id, int) or report_id <= 0:
        raise DeliveryError("unavailable")
    result = {"id": report_id, "screenshotSaved": "screenshot_data_url" in payload}
    if key:
        RECEIPTS.put(key, result)
    return result
