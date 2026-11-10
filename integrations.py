"""Per-user integration settings (local AI connection)."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

AI_PROVIDERS = {"ollama"}
DEFAULT_AI_PROVIDER = "ollama"
DEFAULT_AI_TIMEOUT = 30
MIN_AI_TIMEOUT = 5
MAX_AI_TIMEOUT = 120
OCR_TIMEOUT = 180
# Keep models resident between Telegram messages: a cold load of an 8B model
# costs ~10s on CPU, a warm reply ~1s.
OLLAMA_KEEP_ALIVE = "60m"
_MODEL_CACHE_TTL = 300
_model_cache: dict[tuple[str, str], tuple[float, str]] = {}
_model_cache_lock = threading.Lock()

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
            ai_ocr_model TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(user_integrations)").fetchall()}
    if "ai_ocr_model" not in cols:
        conn.execute("ALTER TABLE user_integrations ADD COLUMN ai_ocr_model TEXT NOT NULL DEFAULT ''")


def default_integration_settings() -> dict:
    return {
        "ai_enabled": False,
        "ai_provider": DEFAULT_AI_PROVIDER,
        "ai_base_url": "",
        "ai_model": "",
        "ai_timeout": DEFAULT_AI_TIMEOUT,
        "ai_ocr_model": "",
    }


def get_user_integrations(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute(
        """
        SELECT ai_enabled, ai_provider, ai_base_url, ai_model, ai_timeout, ai_ocr_model
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
        "ai_ocr_model": row["ai_ocr_model"] or "",
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
    ocr_model = (form.get("ai_ocr_model") or "").strip()
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
        "ai_ocr_model": ocr_model,
    }


def save_user_integrations(conn: sqlite3.Connection, user_id: int, settings: dict) -> None:
    uid = int(user_id)
    conn.execute(
        """
        INSERT INTO user_integrations (
            user_id, ai_enabled, ai_provider, ai_base_url, ai_model, ai_timeout, ai_ocr_model, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            ai_enabled = excluded.ai_enabled,
            ai_provider = excluded.ai_provider,
            ai_base_url = excluded.ai_base_url,
            ai_model = excluded.ai_model,
            ai_timeout = excluded.ai_timeout,
            ai_ocr_model = excluded.ai_ocr_model,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            uid,
            1 if settings["ai_enabled"] else 0,
            settings["ai_provider"],
            settings["ai_base_url"],
            settings["ai_model"],
            settings["ai_timeout"],
            settings.get("ai_ocr_model", ""),
        ),
    )
    conn.commit()


def _model_base(name: str) -> str:
    return name.strip().lower().split(":", 1)[0]


def model_matches(requested: str, available: str) -> bool:
    req = requested.strip().lower()
    avail = available.strip().lower()
    if not req or not avail:
        return False
    if req == avail:
        return True
    if avail.startswith(req + ":"):
        return True
    return _model_base(req) == _model_base(avail)


def resolve_model_name(requested: str, models: list[str]) -> str | None:
    needle = requested.strip()
    if not needle or not models:
        return None
    for name in models:
        if model_matches(needle, name):
            return name
    return None


def fetch_ollama_models(base_url: str, *, timeout: int) -> list[str]:
    url = base_url.rstrip("/") + "/api/tags"
    data = _fetch_json(url, timeout=timeout)
    return [m["name"] for m in data.get("models", []) if m.get("name")]


def parse_json_text(raw: str) -> dict | None:
    text = raw.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _fetch_json(url: str, *, timeout: int, method: str = "GET", payload: dict | None = None) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _resolve_model_cached(base: str, model: str, timeout: int) -> str:
    """Map a short name like 'qwen3' to the installed tag, cached for a few
    minutes so each Telegram message does not pay for an /api/tags round trip."""
    key = (base, model)
    now = time.monotonic()
    with _model_cache_lock:
        hit = _model_cache.get(key)
        if hit and now - hit[0] < _MODEL_CACHE_TTL:
            return hit[1]
    resolved = model
    try:
        picked = resolve_model_name(model, fetch_ollama_models(base, timeout=min(timeout, 10)))
        if picked:
            resolved = picked
    except (HTTPError, URLError, json.JSONDecodeError, OSError, TimeoutError):
        return model
    with _model_cache_lock:
        _model_cache[key] = (now, resolved)
    return resolved


def ollama_chat(
    base_url: str,
    model: str,
    prompt: str,
    *,
    timeout: int,
    json_mode: bool = False,
    images: list[str] | None = None,
    options: dict | None = None,
) -> str | None:
    """One non-streaming chat turn. images are base64 strings (vision/OCR models).
    Thinking is switched off: for short extraction tasks it only adds latency
    (qwen3:8b: ~6s thinking vs ~1s without)."""
    base = base_url.rstrip("/")
    resolved = _resolve_model_cached(base, model.strip(), timeout)
    message: dict = {"role": "user", "content": prompt}
    if images:
        message["images"] = images
    payload: dict = {
        "model": resolved,
        "messages": [message],
        "stream": False,
        "think": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
    }
    if json_mode:
        payload["format"] = "json"
    if options:
        payload["options"] = options
    url = base + "/api/chat"
    try:
        data = _fetch_json(url, timeout=timeout, method="POST", payload=payload)
    except HTTPError as exc:
        if exc.code != 400:
            return None
        # Older Ollama builds reject the think flag; retry without it.
        payload.pop("think", None)
        try:
            data = _fetch_json(url, timeout=timeout, method="POST", payload=payload)
        except (HTTPError, URLError, json.JSONDecodeError, OSError, TimeoutError):
            return None
    except (URLError, json.JSONDecodeError, OSError, TimeoutError):
        return None
    content = data.get("message", {}).get("content")
    return str(content).strip() if content else None


def ollama_ocr(base_url: str, model: str, image_b64: str, *, timeout: int = OCR_TIMEOUT) -> str | None:
    """Plain text of an image via an OCR model such as glm-ocr. The output cap
    and the code-fence stop guard against the model looping after the text."""
    text = ollama_chat(
        base_url,
        model,
        "Text Recognition:",
        timeout=timeout,
        images=[image_b64],
        options={"num_predict": 1200, "stop": ["```"], "temperature": 0},
    )
    return text.strip() if text else None


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

    resolved = resolve_model_name(model, models) if model else None
    if model and not resolved:
        preview = ", ".join(models[:5])
        extra = f" (and {len(models) - 5} more)" if len(models) > 5 else ""
        return False, f"Connected, but model '{model}' was not found. Available: {preview}{extra}"
    if resolved:
        model = resolved

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
