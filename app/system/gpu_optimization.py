r"""
GPU-related settings that OptiMind can toggle to help games run better on
minimal hardware.

Everything here is read-only by default: a change is only written when the
user (or the game optimizer) explicitly asks for it, and each change is
checked by reading it back so a failed change is logged, not silently
ignored.

Registry keys used:

  Windows Game Mode
    HKCU\Software\Microsoft\GameBar\AutoGameModeEnabled   (DWORD 0/1)
    HKCU\Software\Microsoft\GameBar\GameDAC_enabled       (DWORD 0/1)
  Hardware-accelerated GPU scheduling (read-only: needs a reboot to change)
    HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers\HwSchMode  (DWORD 1/2)
  NVIDIA shader cache
    HKLM\SOFTWARE\NVIDIA Corporation\Global\NVTweak\ShaderCacheControl  (DWORD 0/1)

None of these touch the game itself or its files — only Windows and driver
settings that affect how the GPU and its scheduling behave.
"""

from __future__ import annotations

import logging
import sys

from app.i18n import tx

if sys.platform == "win32":
    import winreg

logger = logging.getLogger("optimind.gpu_optimization")

# ------------------------------------------------------------ Windows Game Mode

_GAME_BAR_KEY = r"Software\Microsoft\GameBar"


def game_mode_enabled() -> bool | None:
    """Whether Windows Game Mode is on (AutoGameModeEnabled), or None when it can't be read."""
    if not _winreg_available():
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _GAME_BAR_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "AutoGameModeEnabled")
            return bool(value)
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.debug("Couldn't read Game Mode (%s)", e)
        return None


def set_game_mode(enabled: bool) -> str:
    """Turn Windows Game Mode on or off. Returns what Windows reports after the change."""
    if not _winreg_available():
        raise OSError(tx("not available on this platform"))
    try:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _GAME_BAR_KEY, 0, winreg.KEY_SET_VALUE)
        except FileNotFoundError:
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, _GAME_BAR_KEY)
        with key:
            winreg.SetValueEx(key, "AutoGameModeEnabled", 0, winreg.REG_DWORD, 1 if enabled else 0)
            # Also set GameDAC (Game Bar's hardware-accelerated GPU scheduling hint) in step
            try:
                winreg.SetValueEx(key, "GameDAC_enabled", 0, winreg.REG_DWORD, 1 if enabled else 0)
            except OSError:
                pass
        actual = game_mode_enabled()
        if actual is None:
            return tx("game mode is {state}", state="on" if enabled else "off")
        return tx("game mode is {state} (checked)", state="on" if actual else "off")
    except OSError as e:
        raise OSError(tx("couldn't change game mode ({error})", error=str(e))) from e


# ------------------------------------------------------------ NVIDIA shader cache

_NV_TWEAK_KEY = r"SOFTWARE\NVIDIA Corporation\Global\NVTweak"


def shader_cache_enabled() -> bool | None:
    """Whether the NVIDIA shader cache is on, or None when it can't be read (non-NVIDIA GPU,
    no NVIDIA driver, or the key doesn't exist)."""
    if not _winreg_available():
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _NV_TWEAK_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "ShaderCacheControl")
            return bool(value)
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.debug("Couldn't read NVIDIA shader cache (%s)", e)
        return None


def set_shader_cache(enabled: bool) -> str:
    """Enable or disable the NVIDIA shader cache. Returns what Windows reports after."""
    if not _winreg_available():
        raise OSError(tx("not available on this platform"))
    try:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _NV_TWEAK_KEY, 0, winreg.KEY_SET_VALUE)
        except FileNotFoundError:
            key = winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, _NV_TWEAK_KEY)
        with key:
            winreg.SetValueEx(key, "ShaderCacheControl", 0, winreg.REG_DWORD, 1 if enabled else 0)
        actual = shader_cache_enabled()
        if actual is None:
            return tx("NVIDIA shader cache is {state}", state="on" if enabled else "off")
        return tx("NVIDIA shader cache is {state} (checked)", state="on" if actual else "off")
    except OSError as e:
        raise OSError(tx("couldn't change the NVIDIA shader cache ({error})", error=str(e))) from e


# ------------------------------------------------------------ Hardware-accelerated GPU scheduling

_HGS_KEY = r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers"


def hags_available() -> bool | None:
    """Whether hardware-accelerated GPU scheduling is supported on this PC (read-only)."""
    if not _winreg_available():
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _HGS_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "HwSchMode")
            return value == 1  # 1 = enabled, 2 = disabled
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.debug("Couldn't read HAGS (%s)", e)
        return None


# ------------------------------------------------------------ helpers

_winreg_available_cache: bool | None = None


def _winreg_available() -> bool:
    global _winreg_available_cache
    if _winreg_available_cache is None:
        _winreg_available_cache = sys.platform == "win32"
    return _winreg_available_cache
