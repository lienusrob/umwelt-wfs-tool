"""
HTTP-Helper für das Umwelt WFS Tool.
Verwendet urllib (kein Qt-Event-Loop erforderlich, funktioniert aus QThread heraus).

Performance-Features:
  - Gzip/Deflate-Komprimierung (Accept-Encoding)
  - In-Memory-Cache mit konfigurierbarer TTL
  - Persistenter Disk-Cache für Capabilities (überlebt QGIS-Neustart)
"""

import gzip as _gzip
import hashlib
import json
import os
import threading
import time
import urllib.request
import urllib.error
import zlib
from typing import Optional

from qgis.core import QgsMessageLog
from .compat import MSG_WARNING

_USER_AGENT = "QGIS Umwelt WFS Tool/0.2"
_CAPS_TTL   = 3600   # In-Memory Capabilities: 1 Stunde
_FEAT_TTL   = 900    # In-Memory Feature-Daten: 15 Minuten
_DISK_TTL   = 86400  # Disk-Cache Capabilities: 24 Stunden

_mem_cache: dict  = {}   # url → (timestamp_float, data_bytes)
_cache_lock       = threading.Lock()
_disk_dir: str    = ""   # lazy-initialized


# ── Disk-Cache ────────────────────────────────────────────────────────────────

def _get_disk_dir() -> str:
    global _disk_dir
    if _disk_dir:
        return _disk_dir
    try:
        from qgis.core import QgsApplication
        d = os.path.join(QgsApplication.qgisSettingsDirPath(), "cache", "umwelt_wfs")
        os.makedirs(d, exist_ok=True)
        _disk_dir = d
    except Exception:
        _disk_dir = ""
    return _disk_dir


def _disk_path(url: str) -> str:
    key = hashlib.md5(url.encode()).hexdigest()
    return os.path.join(_get_disk_dir(), key)


def _load_disk(url: str) -> Optional[bytes]:
    d = _get_disk_dir()
    if not d:
        return None
    path = _disk_path(url)
    meta = path + ".meta"
    try:
        with open(meta) as f:
            t = json.load(f)["t"]
        if time.time() - t > _DISK_TTL:
            return None
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None


def _save_disk(url: str, data: bytes) -> None:
    d = _get_disk_dir()
    if not d:
        return
    try:
        path = _disk_path(url)
        with open(path, "wb") as f:
            f.write(data)
        with open(path + ".meta", "w") as f:
            json.dump({"t": time.time()}, f)
    except Exception:
        pass


# ── Cache-Verwaltung ──────────────────────────────────────────────────────────

def clear_cache() -> None:
    """Leert den In-Memory-Cache (z.B. bei Plugin-Reload)."""
    with _cache_lock:
        _mem_cache.clear()


# ── HTTP-Hilfsfunktion ────────────────────────────────────────────────────────

def http_get(url: str, timeout_ms: int = 15000, use_cache: bool = False,
             ttl: int = None, disk_cache: bool = False) -> bytes:
    """
    Synchroner HTTP-GET mit Gzip-Unterstützung, In-Memory- und optionalem Disk-Cache.
    Funktioniert aus jedem Thread heraus (kein Qt-Event-Loop nötig).

    use_cache=True  : In-Memory-Cache mit ttl Sekunden (Standard: _CAPS_TTL).
    disk_cache=True : Zusätzlich Disk-Cache (nur sinnvoll für Capabilities).
    """
    effective_ttl = ttl if ttl is not None else _CAPS_TTL

    # 1. In-Memory-Cache
    if use_cache:
        with _cache_lock:
            entry = _mem_cache.get(url)
            if entry and time.time() - entry[0] < effective_ttl:
                return entry[1]

    # 2. Disk-Cache (nur für Capabilities)
    if disk_cache:
        data = _load_disk(url)
        if data:
            with _cache_lock:
                _mem_cache[url] = (time.time(), data)
            return data

    # 3. HTTP-Request mit Gzip-Unterstützung
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent":      _USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
        },
    )
    last_exc = None

    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout_ms / 1000) as resp:
                raw = resp.read()
                encoding = resp.headers.get("Content-Encoding", "")

            # Dekomprimierung
            if encoding == "gzip":
                try:
                    data = _gzip.decompress(raw)
                except Exception:
                    data = raw
            elif encoding == "deflate":
                try:
                    data = zlib.decompress(raw)
                except Exception:
                    try:
                        data = zlib.decompress(raw, -zlib.MAX_WBITS)
                    except Exception:
                        data = raw
            else:
                data = raw

            if use_cache:
                with _cache_lock:
                    _mem_cache[url] = (time.time(), data)
            if disk_cache:
                _save_disk(url, data)
            return data

        except urllib.error.HTTPError as exc:
            QgsMessageLog.logMessage(
                f"HTTP {exc.code} [{url}]: {exc.reason}",
                "Umwelt WFS Tool", MSG_WARNING,
            )
            return b""
        except (urllib.error.URLError, OSError) as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(attempt + 1)
        except Exception as exc:
            QgsMessageLog.logMessage(
                f"HTTP-Fehler [{url}]: {exc}",
                "Umwelt WFS Tool", MSG_WARNING,
            )
            return b""

    QgsMessageLog.logMessage(
        f"HTTP-Fehler nach 3 Versuchen [{url}]: {last_exc}",
        "Umwelt WFS Tool", MSG_WARNING,
    )
    return b""
