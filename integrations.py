"""Per-user integration settings (local AI connection)."""

from __future__ import annotations

import json
import re
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

AI_PROVIDERS = {"ollama"}
DEFAULT_AI_PROVIDER = "ollama"
DEFAULT_AI_TIMEOUT = 30
MIN_AI_TIMEOUT = 5
MAX_AI_TIMEOUT = 120

_BASE_URL_RE = re.compile(r"^https?://", re.I)


def migrate_user_integrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_integrations (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            ai_enabled INTEGER NOT NULL DEFAULT 0 CHECK (ai_enabled IN (0, 1)),
            ai_provider TEXT NOT NULL DEFAULT 'ollama',
            ai_base_url TEXT NOT NULL DEFAULT '',
            ai_model TEXT NOT NULL DEFAULT '',
            ai_timeout INTEGER NOT NULL DEFAULT 30 CHECK (ai_timeout BETWEEN 5 AND 120),
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def default_integration_settings() -> dict:
    return {
        "ai_enabled": False,
        "ai_provider": DEFAULT_AI_PROVIDER,
        "ai_base_url": "",
        "ai_model": "",
        "ai_timeout": DEFAULT_AI_TIMEOUT,
    }


def get_user_integrations(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute(
        """
        SELECT ai_enabled, ai_provider, ai_base_url, ai_model, ai_timeout
        FROM user_integrations
        WHERE user_id = ?
        """,
        (int(user_id),),
    ).fetchone()
    if not row:
        return default_integration_settings()
    return {
        "ai_enabled": bool(row["ai_enabled"]),
        "ai_provider": row["ai_provider"] or DEFAULT_AI_PROVIDER,
        "ai_base_url": row["ai_base_url"] or "",
        "ai_model": row["ai_model"] or "",
        "ai_timeout": int(row["ai_timeout"] or DEFAULT_AI_TIMEOUT),
    }


def _normalize_base_url(raw: str) -> str:
    url = raw.strip().rstrip("/")
    if not url:
        return ""
    if not _BASE_URL_RE.match(url):
        raise ValueError("Base URL must start with http:// or https://")
    return url


def _normalize_timeout(raw) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError("Timeout must be a number of seconds") from None
    if value < MIN_AI_TIMEOUT or value > MAX_AI_TIMEOUT:
        raise ValueError(f"Timeout must be between {MIN_AI_TIMEOUT} and {MAX_AI_TIMEOUT} seconds")
    return value


def parse_integration_form(form) -> dict:
    provider = (form.get("ai_provider") or DEFAULT_AI_PROVIDER).strip().lower()
    if provider not in AI_PROVIDERS:
        raise ValueError("Unsupported AI provider")

    enabled = form.get("ai_enabled") == "1"
    base_url = _normalize_base_url(form.get("ai_base_url", ""))
    model = (form.get("ai_model") or "").strip()
    timeout = _normalize_timeout(form.get("ai_timeout", DEFAULT_AI_TIMEOUT))

    if enabled:
        if not base_url:
            raise ValueError("Base URL is required when AI is enabled")
        if not model:
            raise ValueError("Model name is required when AI is enabled")

    return {
        "ai_enabled": enabled,
        "ai_provider": provider,
        "ai_base_url": base_url,
        "ai_model": model,
        "ai_timeout": timeout,
    }


def save_user_integrations(conn: sqlite3.Connection, user_id: int, settings: dict) -> None:
    uid = int(user_id)
    conn.execute(
        """
        INSERT INTO user_integrations (
            user_id, ai_enabled, ai_provider, ai_base_url, ai_model, ai_timeout, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            ai_enabled = excluded.ai_enabled,
            ai_provider = excluded.ai_provider,
            ai_base_url = excluded.ai_base_url,
            ai_model = excluded.ai_model,
            ai_timeout = excluded.ai_timeout,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            uid,
            1 if settings["ai_enabled"] else 0,
            settings["ai_provider"],
            settings["ai_base_url"],
            settings["ai_model"],
            settings["ai_timeout"],
        ),
    )
    conn.commit()


def _fetch_json(url: str, *, timeout: int, method: str = "GET", payload: dict | None = None) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def ollama_chat(base_url: str, model: str, prompt: str, *, timeout: int, json_mode: bool = False) -> str | None:
    url = base_url.rstrip("/") + "/api/chat"
    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    if json_mode:
        payload["format"] = "json"
    try:
        data = _fetch_json(url, timeout=timeout, method="POST", payload=payload)
    except (HTTPError, URLError, json.JSONDecodeError, OSError, TimeoutError):
        return None
    content = data.get("message", {}).get("content")
    return str(content).strip() if content else None


def test_ai_connection(settings: dict) -> tuple[bool, str]:
    provider = settings.get("ai_provider") or DEFAULT_AI_PROVIDER
    base_url = (settings.get("ai_base_url") or "").strip().rstrip("/")
    model = (settings.get("ai_model") or "").strip()
    timeout = int(settings.get("ai_timeout") or DEFAULT_AI_TIMEOUT)

    if not base_url:
        return False, "Set a Base URL first."
    if provider != "ollama":
        return False, f"Provider '{provider}' is not supported yet."

    try:
        tags_data = _fetch_json(f"{base_url}/api/tags", timeout=timeout)
    except HTTPError as exc:
        return False, f"Ollama responded with HTTP {exc.code}."
    except URLError as exc:
        return False, f"Could not reach Ollama: {exc.reason}"
    except (json.JSONDecodeError, OSError, TimeoutError) as exc:
        return False, f"Connection failed: {exc}"

    models = [m.get("name", "") for m in tags_data.get("models", []) if m.get("name")]
    if not models:
        return False, "Connected, but no models are installed on this Ollama instance."

    if model and model not in models:
        preview = ", ".join(models[:5])
        extra = f" (and {len(models) - 5} more)" if len(models) > 5 else ""
        return False, f"Connected, but model '{model}' was not found. Available: {preview}{extra}"

    if model:
        try:
            reply = ollama_chat(base_url, model, "Reply with OK only.", timeout=timeout)
            if reply:
                return True, f"Connected. Model '{model}' replied: {reply[:80]}"
        except (HTTPError, URLError, json.JSONDecodeError, OSError, TimeoutError) as exc:
            return False, f"Tags OK, but chat test failed: {exc}"

    preview = ", ".join(models[:5])
    extra = f" (+{len(models) - 5} more)" if len(models) > 5 else ""
    return True, f"Connected. Models available: {preview}{extra}"
