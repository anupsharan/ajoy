"""
Live hot-reload config endpoint.

GET  /api/config  — returns all non-secret settings (current in-memory values)
PATCH /api/config — updates one or more settings in the live singleton AND
                    rewrites the matching lines in .env so changes survive a restart.

No restart required. The scheduler reads settings.x on every tick, so changes
take effect on the very next scan cycle (~60 s).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from app.config import Settings, settings

router = APIRouter(prefix="/api/config", tags=["config"])

# .env lives at the project root — two levels above this file
_ENV_PATH = Path(__file__).parent.parent.parent / ".env"

# Fields that must never be changed via the API (credentials, infra)
_READONLY = {
    "tradier_api_token",
    "tradier_api_token_sandbox",
    "tradier_account_id",
    "tradier_account_id_sandbox",
    "tradier_base_url",
    "tradier_base_url_sandbox",
    "use_sandbox",
    "database_url",
    "scheduler_enabled",
    "scan_interval_seconds",
    "manage_interval_seconds",
}


def _cast(raw: str, annotation: Any) -> Any:
    """Cast a string value to the declared pydantic field type."""
    # Unwrap Optional[T] → T
    args = getattr(annotation, "__args__", None)
    if args and type(None) in args:
        inner = next(a for a in args if a is not type(None))
        return _cast(raw, inner)
    if annotation is bool:
        return raw.lower() in ("1", "true", "yes", "on")
    if annotation is int:
        return int(raw)
    if annotation is float:
        return float(raw)
    return str(raw)


@router.get("")
def get_config() -> dict:
    """Return all current (in-memory) settings, excluding secrets."""
    data = settings.model_dump()
    for k in _READONLY:
        data.pop(k, None)
    return data


@router.patch("")
def update_config(payload: dict[str, str]) -> dict:
    """
    Update one or more settings.

    Payload keys must be Python field names (snake_case).
    All values are sent as strings — the endpoint casts them to the correct type.

    Returns { updated: {field: new_value, …}, errors: […] }
    """
    if not payload:
        raise HTTPException(status_code=400, detail="Empty payload")

    updated: dict[str, Any] = {}
    errors: list[str] = []

    for key, raw in payload.items():
        key = key.lower().strip()

        if key in _READONLY:
            errors.append(f"'{key}' is read-only and cannot be changed at runtime")
            continue

        field_info = Settings.model_fields.get(key)
        if field_info is None:
            errors.append(f"Unknown setting: '{key}'")
            continue

        try:
            value = _cast(raw, field_info.annotation)
        except (ValueError, TypeError) as exc:
            errors.append(f"Invalid value for '{key}': {exc}")
            continue

        # ── 1. Update the live settings singleton ──────────────────────────
        # All modules import the same `settings` object — updating it in-place
        # means every subsequent call to settings.x uses the new value.
        # No restart required; the scheduler picks it up on the next tick.
        object.__setattr__(settings, key, value)

        # ── 2. Rewrite the matching line in .env ───────────────────────────
        # Ensures the change survives a restart.
        if _ENV_PATH.exists():
            text = _ENV_PATH.read_text(encoding="utf-8")
            env_key = key.upper()
            # Match "KEY=anything_to_end_of_line" (handles inline comments)
            pattern = rf'^({re.escape(env_key)}\s*=)[^\n]*'
            if re.search(pattern, text, flags=re.MULTILINE):
                text = re.sub(pattern, rf'\g<1>{raw}', text, flags=re.MULTILINE)
            else:
                # Key not present in .env — append it
                text = text.rstrip() + f'\n{env_key}={raw}\n'
            _ENV_PATH.write_text(text, encoding="utf-8")

        updated[key] = value

    if errors and not updated:
        raise HTTPException(status_code=422, detail=errors)

    return {"updated": updated, "errors": errors}
