"""Useful diagnostics without credentials, headers or complete server responses."""
from __future__ import annotations

import re


def safe_text(value, secrets=()):
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'sk-[A-Za-z0-9_-]+', '[REDACTED]', text)
    text = re.sub(r'(?i)Bearer\s+\S+', 'Bearer [REDACTED]', text)
    return ' '.join(text.split())[:600]


def error_detail(exc, secrets=()):
    status = getattr(exc, 'status_code', None)
    body = getattr(exc, 'body', None)
    error = body.get('error', body) if isinstance(body, dict) else None
    parts = [type(exc).__name__]
    if status:
        parts.append(f'HTTP {status}')
    if isinstance(error, dict):
        parts += [f'{key}={error[key]}' for key in ('code', 'param', 'message') if error.get(key)]
    elif isinstance(exc, (ImportError, UnicodeError, TypeError, ValueError)):
        parts.append(str(exc))
    if getattr(exc, 'smtp_code', None):
        parts.append(f'SMTP {exc.smtp_code}')
        message = getattr(exc, 'smtp_error', b'')
        parts.append(message.decode('utf-8', errors='replace') if isinstance(message, bytes) else str(message))
    return safe_text(' | '.join(parts), secrets)


def known_unbilled_failure(exc):
    # Local construction/import errors occurred before a request was sent. Explicit
    # API rejection is not a completed generation; timeouts/5xx remain uncertain.
    if isinstance(exc, (ImportError, UnicodeEncodeError, TypeError)):
        return True
    return getattr(exc, 'status_code', None) in (400, 401, 403, 404, 405, 413, 415, 422, 429)
