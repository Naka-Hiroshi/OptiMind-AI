"""
API keys on disk, encrypted for your Windows account (DPAPI).

The Settings page saves GEMINI_API_KEY and GROQ_API_KEY as "dpapi:<base64>":
Windows' Data Protection API encrypts them with a key tied to your Windows
sign-in, so a copy of the settings file (a backup, a synced folder, a
screenshot) doesn't give them away. Only your account on this PC can read
them back. Keys typed into the file by hand stay plain text and keep working.
Off Windows, keys are stored as they are.
"""
from __future__ import annotations

import base64
import binascii
import logging
import sys

logger = logging.getLogger("optimind.dpapi")

PREFIX = "dpapi:"
_DESCRIPTION = "OptiMind AI API key"
_UI_FORBIDDEN = 0x1  # CRYPTPROTECT_UI_FORBIDDEN: never show a prompt


def protect(text: str) -> str:
    """Encrypts `text` for the current Windows account ("" stays "")."""
    if not text or sys.platform != "win32":
        return text
    import win32crypt

    blob = win32crypt.CryptProtectData(text.encode("utf-8"), _DESCRIPTION, None, None, None, _UI_FORBIDDEN)
    return PREFIX + base64.b64encode(blob).decode("ascii")


def unprotect(value: str) -> str:
    """The plain text of a value saved by protect(); any other value is returned as it is.
    A key saved by another Windows account or PC can't be read: it comes back empty."""
    if not value or not value.startswith(PREFIX):
        return value
    if sys.platform != "win32":
        logger.warning("An encrypted API key can only be read on the Windows PC that saved it")
        return ""
    import pywintypes
    import win32crypt

    try:
        blob = base64.b64decode(value[len(PREFIX):], validate=True)
        _, data = win32crypt.CryptUnprotectData(blob, None, None, None, _UI_FORBIDDEN)
        return data.decode("utf-8")
    except (binascii.Error, ValueError, pywintypes.error):
        logger.warning("An API key in the settings file can't be read (saved by another Windows account "
                       "or PC): enter it again in Settings")
        return ""
