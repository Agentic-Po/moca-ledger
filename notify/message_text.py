"""Telegram display policy: contact handles are plain text, never mentions."""
import re

_HANDLE = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{4,31})(?![\w.])")


def plain_handles(text):
    """Keep contact text readable without creating Telegram mention notifications.

    Email addresses and email-domain annotations are preserved.
    """
    return _HANDLE.sub(r"\1", str(text))


def outbound_fields(data):
    """Apply the policy to text and photo captions at the transport boundary."""
    return {k: plain_handles(v) if k in ("text", "caption") else v
            for k, v in (data or {}).items()}
