"""Per-user integration settings (local AI connection)."""

from __future__ import annotations

import json
import os
import re
import secrets
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
TRANSCRIBE_TIMEOUT = 120
DEFAULT_WHISPER_MODEL = "deepdml/faster-whisper-large-v3-turbo-ct2"
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
    migrate_whisper_columns(conn)


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
    (qwen3:8b: ~6s thinking vs ~1s without). No keep_alive is sent, so the
    Ollama server's own OLLAMA_KEEP_ALIVE decides how long a model stays in
    memory; the server may be shared with other apps and games."""
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


# --- speech to text ---------------------------------------------------------------
# Voice messages go to a Whisper server with an OpenAI-compatible API, such as
# speaches (faster-whisper) running next to Ollama. Each user sets it in
# Settings -> Integrations (user_integrations.whisper_*); the VB_WHISPER_*
# environment variables are the fallback for users who never set it.

WHISPER_COLUMNS = {
    "whisper_enabled": "INTEGER NOT NULL DEFAULT 0 CHECK (whisper_enabled IN (0, 1))",
    "whisper_url": "TEXT NOT NULL DEFAULT ''",
    "whisper_model": "TEXT NOT NULL DEFAULT ''",
    "whisper_language": "TEXT NOT NULL DEFAULT 'el'",
}
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}$")


def migrate_whisper_columns(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(user_integrations)").fetchall()}
    for name, spec in WHISPER_COLUMNS.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE user_integrations ADD COLUMN {name} {spec}")


def whisper_settings() -> dict:
    """The server-wide fallback from the environment."""
    return {
        "url": os.environ.get("VB_WHISPER_URL", "").strip().rstrip("/"),
        "model": os.environ.get("VB_WHISPER_MODEL", "").strip() or DEFAULT_WHISPER_MODEL,
        # Greek by default: on short clips Whisper's own language guess is unreliable.
        "language": os.environ.get("VB_WHISPER_LANGUAGE", "el").strip(),
    }


def default_whisper_user_settings() -> dict:
    return {"whisper_enabled": False, "whisper_url": "", "whisper_model": "", "whisper_language": "el"}


def get_whisper_user_settings(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute(
        "SELECT whisper_enabled, whisper_url, whisper_model, whisper_language FROM user_integrations WHERE user_id = ?",
        (int(user_id),),
    ).fetchone()
    if not row:
        return default_whisper_user_settings()
    return {
        "whisper_enabled": bool(row["whisper_enabled"]),
        "whisper_url": row["whisper_url"] or "",
        "whisper_model": row["whisper_model"] or "",
        "whisper_language": row["whisper_language"] or "",
    }


def parse_whisper_form(form) -> dict:
    enabled = form.get("whisper_enabled") == "1"
    try:
        url = _normalize_base_url(form.get("whisper_url", ""))
    except ValueError:
        raise ValueError("Whisper URL must start with http:// or https://") from None
    model = (form.get("whisper_model") or "").strip()
    language = (form.get("whisper_language") or "").strip().lower()
    if language and not _LANGUAGE_RE.match(language):
        raise ValueError("Language must be a code like el or en, or blank to let Whisper guess")
    if enabled and not url:
        raise ValueError("Whisper URL is required when voice messages are on")
    return {"whisper_enabled": enabled, "whisper_url": url, "whisper_model": model, "whisper_language": language}


def save_whisper_user_settings(conn: sqlite3.Connection, user_id: int, settings: dict) -> None:
    conn.execute(
        """
        INSERT INTO user_integrations (user_id, whisper_enabled, whisper_url, whisper_model, whisper_language, updated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            whisper_enabled = excluded.whisper_enabled,
            whisper_url = excluded.whisper_url,
            whisper_model = excluded.whisper_model,
            whisper_language = excluded.whisper_language,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            int(user_id),
            1 if settings["whisper_enabled"] else 0,
            settings["whisper_url"],
            settings["whisper_model"],
            settings["whisper_language"],
        ),
    )
    conn.commit()


def whisper_config(user_settings: dict) -> dict:
    """{url, model, language} to call Whisper with; url is blank when voice is off."""
    return {
        "url": user_settings["whisper_url"].rstrip("/") if user_settings["whisper_enabled"] else "",
        "model": user_settings["whisper_model"] or DEFAULT_WHISPER_MODEL,
        "language": user_settings["whisper_language"],
    }


def whisper_for_user(conn: sqlite3.Connection, user_id: int) -> dict:
    """The user's own Whisper settings; the environment only for a user who never set any."""
    mine = get_whisper_user_settings(conn, user_id)
    if mine["whisper_enabled"] or mine["whisper_url"]:
        return whisper_config(mine)
    return whisper_settings()


def _multipart(fields: dict[str, str], file_field: str, filename: str, data: bytes) -> tuple[bytes, str]:
    boundary = "vb" + secrets.token_hex(12)
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n".encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def transcribe_audio(
    audio: bytes,
    filename: str,
    *,
    prompt: str = "",
    settings: dict | None = None,
    timeout: int = TRANSCRIBE_TIMEOUT,
) -> str | None:
    """Text of a voice message, or None when the server is unset, down or heard nothing.
    prompt carries words Whisper should spell right (categories, account names)."""
    cfg = settings or whisper_settings()
    if not cfg.get("url"):
        return None
    fields = {"model": cfg["model"], "response_format": "json", "temperature": "0"}
    if cfg.get("language"):
        fields["language"] = cfg["language"]
    if prompt:
        fields["prompt"] = prompt
    body, content_type = _multipart(fields, "file", filename, audio)
    req = Request(
        cfg["url"] + "/v1/audio/transcriptions",
        data=body,
        headers={"Content-Type": content_type, "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except (HTTPError, URLError, json.JSONDecodeError, OSError, TimeoutError, UnicodeDecodeError):
        return None
    text = str(data.get("text") or "").strip() if isinstance(data, dict) else ""
    return text or None


def test_whisper_connection(cfg: dict, *, timeout: int = 10) -> tuple[bool, str]:
    """Is the server up and is the model downloaded? cfg is whisper_config()'s shape."""
    url = (cfg.get("url") or "").rstrip("/")
    model = cfg.get("model") or DEFAULT_WHISPER_MODEL
    if not url:
        return False, "Set the Whisper URL first."
    try:
        data = _fetch_json(f"{url}/v1/models", timeout=timeout)
    except HTTPError as exc:
        return False, f"Whisper responded with HTTP {exc.code}."
    except URLError as exc:
        return False, f"Could not reach Whisper: {exc.reason}"
    except (json.JSONDecodeError, OSError, TimeoutError) as exc:
        return False, f"Connection failed: {exc}"
    ids = [str(m.get("id")) for m in (data.get("data") or []) if isinstance(m, dict) and m.get("id")]
    if model in ids:
        return True, f"Connected. Model '{model}' is ready."
    return False, (
        f"Connected, but model '{model}' is not downloaded. "
        f"Download it once with: curl -X POST {url}/v1/models/{model}"
    )
