#!/usr/bin/env python3
"""
Local setup preflight for Pipecat Hermes Skill.

This deliberately avoids loading STT/TTS models. It checks dependency presence,
config files, and model files so setup failures show up before a live call.
Use --online to also probe the configured Hermes endpoint.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "config.yaml"
PIPER_MODEL = ROOT / "models" / "en_US-joe-medium.onnx"
PIPER_CONFIG = ROOT / "models" / "en_US-joe-medium.onnx.json"
SESSIONS_DIR = ROOT / "sessions"


def _ok(message: str) -> None:
    print(f"OK   {message}")


def _warn(message: str) -> None:
    print(f"WARN {message}")


def _fail(message: str) -> None:
    print(f"FAIL {message}")


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def check_python() -> bool:
    version = sys.version_info
    if version < (3, 11):
        _fail(f"Python {version.major}.{version.minor} detected; Python 3.11+ is required")
        return False
    _ok(f"Python {version.major}.{version.minor}.{version.micro}")
    return True


def check_dependencies() -> bool:
    required = [
        ("yaml", "PyYAML"),
        ("pydantic", "pydantic"),
        ("requests", "requests"),
        ("websocket", "websocket-client"),
        ("faster_whisper", "faster-whisper"),
        ("piper", "piper-tts"),
        ("onnxruntime", "onnxruntime"),
    ]
    success = True
    for module, package in required:
        if _has_module(module):
            _ok(f"dependency available: {package}")
        else:
            _fail(f"missing dependency: {package}")
            success = False

    if _has_module("audioop"):
        _ok("audioop available")
    elif sys.version_info >= (3, 13):
        _fail("audioop unavailable; install audioop-lts via requirements.txt")
        success = False
    else:
        _fail("audioop unavailable")
        success = False

    return success


def check_files() -> bool:
    success = True
    if CONFIG_PATH.exists():
        _ok("config/config.yaml exists")
    else:
        _fail("config/config.yaml missing; copy config/config.example.yaml first")
        success = False

    if PIPER_MODEL.exists() and PIPER_CONFIG.exists():
        _ok("default Piper voice files exist")
    else:
        _fail("default Piper voice missing; run scripts/download_piper_voice.sh")
        success = False

    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        probe = SESSIONS_DIR / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        _ok("sessions directory is writable")
    except Exception as exc:
        _fail(f"sessions directory is not writable: {exc}")
        success = False

    return success


def check_config() -> tuple[bool, str | None]:
    if not CONFIG_PATH.exists() or not _has_module("yaml"):
        return False, None

    import yaml

    try:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        _fail(f"config/config.yaml is not valid YAML: {exc}")
        return False, None

    hermes = data.get("hermes") or {}
    endpoint = str(hermes.get("endpoint") or "").strip()
    backend = str(hermes.get("backend") or "").strip()
    if endpoint:
        _ok(f"Hermes endpoint configured: {endpoint}")
    else:
        _warn("Hermes endpoint is empty")
    if backend:
        _ok(f"Hermes backend configured: {backend}")
    else:
        _warn("Hermes backend is empty")
    return True, endpoint or None


def check_online(endpoint: str | None) -> bool:
    if not endpoint:
        _fail("cannot probe Hermes endpoint because it is not configured")
        return False
    if not _has_module("requests"):
        _fail("cannot probe Hermes endpoint because requests is missing")
        return False

    import requests

    url = endpoint.rstrip("/")
    for path in ("/health", "/"):
        try:
            response = requests.get(f"{url}{path}", timeout=3)
        except Exception as exc:
            _warn(f"Hermes probe {path} failed: {exc}")
            continue
        if response.status_code < 500:
            _ok(f"Hermes endpoint responded on {path} with HTTP {response.status_code}")
            return True
        _warn(f"Hermes probe {path} returned HTTP {response.status_code}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Check local Pipecat Hermes setup")
    parser.add_argument(
        "--online",
        action="store_true",
        help="also probe the configured Hermes HTTP endpoint",
    )
    args = parser.parse_args()

    print(f"Preflight root: {ROOT}")
    checks_ok = [
        check_python(),
        check_dependencies(),
        check_files(),
    ]
    config_ok, endpoint = check_config()
    checks_ok.append(config_ok)
    if args.online:
        checks_ok.append(check_online(endpoint))

    if all(checks_ok):
        print("Preflight passed.")
        return 0
    print("Preflight found issues.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
