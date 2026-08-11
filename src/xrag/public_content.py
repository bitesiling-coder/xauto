from __future__ import annotations

import json
import re


_SENSITIVE_KEY_PATTERN = (
    r"(?:[a-z][a-z0-9]*_)*(?:api_key|token|secret|password|passwd|private_key)"
    r"|aws_(?:access_key_id|secret_access_key|session_token)"
    r"|(?:auth|access|refresh)[\s_.-]*token"
    r"|client[\s_.-]*secret"
    r"|(?:[a-z][a-z0-9]*_)*(?:access_key(?:_id)?|secret_access_key|"
    r"session_(?:key|id|credential|credentials))"
    r"|(?:access|session)[\s_.-]*(?:key|credential)(?:[\s_.-]*id)?"
    r"|password|passwd|cookie|authorization|ct0"
)
_SENSITIVE_KEY = re.compile(rf"(?:{_SENSITIVE_KEY_PATTERN})\Z", re.IGNORECASE)
_CREDENTIAL_ASSIGNMENT = re.compile(
    rf"(?<![a-z0-9_])[\"']?(?:{_SENSITIVE_KEY_PATTERN})"
    r"[\"']?(?![a-z0-9_])\s*[:=]",
    re.IGNORECASE,
)
_PRIVATE_PATH = re.compile(
    r"\\{2,}|(?<![a-z])[a-z]:(?:\\+|/(?!/))|"
    r"(?<![a-z0-9.:])/(?:mnt/[a-z]|home|root|users)/",
    re.IGNORECASE,
)


def assert_public_content(content: str) -> None:
    """Reject credentials and machine-local absolute paths from public output."""
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        _assert_safe_text(content)
    else:
        _assert_safe_json_value(parsed)


def _assert_safe_json_value(value: object) -> None:
    if isinstance(value, str):
        _assert_safe_text(value)
        return
    if isinstance(value, list):
        for item in value:
            _assert_safe_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                _assert_safe_text(key)
                if _SENSITIVE_KEY.fullmatch(key.strip()):
                    raise ValueError("unsafe public output")
            _assert_safe_json_value(item)


def _assert_safe_text(content: str) -> None:
    if _CREDENTIAL_ASSIGNMENT.search(content) or _PRIVATE_PATH.search(content):
        raise ValueError("unsafe public output")
