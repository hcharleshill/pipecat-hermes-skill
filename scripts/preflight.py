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
DEFAULT_ASTERISK_CONFIG_DIR = ROOT / "asterisk-config"


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


def _strip_asterisk_comment(line: str) -> str:
    return line.split(";", 1)[0].strip()


def _find_agent_pin(config_dir: Path) -> tuple[bool, Path | None]:
    if not config_dir.exists():
        return False, None
    for path in sorted(config_dir.glob("*.conf")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for line in lines:
            line = _strip_asterisk_comment(line)
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "HERMES_AGENT_PIN" and value.strip():
                return True, path
    return False, None


def check_asterisk_pin_gate(config_dir: Path, require_agent_pin: bool = False) -> bool:
    """
    Confirm the committed dialplan has the DTMF gate and optionally require a
    real deployment PIN. The PIN itself stays in Asterisk, never Python config.
    """
    hermes_conf = config_dir / "hermes.conf"
    if not hermes_conf.exists():
        _fail(f"Asterisk config missing: {hermes_conf}")
        return False

    try:
        text = hermes_conf.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        _fail(f"cannot read {hermes_conf}: {exc}")
        return False

    gate_markers = [
        "HERMES_AGENT_PIN",
        "Authenticate(${HERMES_AGENT_PIN}",
        "Gosub(hermes-agent-auth,s,1)",
        "Stasis(hermes)",
    ]
    gate_ok = all(marker in text for marker in gate_markers)
    if gate_ok:
        _ok("Asterisk DTMF PIN gate is present before Stasis(hermes)")
    else:
        _fail("Asterisk DTMF PIN gate is incomplete in hermes.conf")

    pin_found, pin_path = _find_agent_pin(config_dir)
    if pin_found and pin_path:
        _ok(f"HERMES_AGENT_PIN appears configured in {pin_path}")
    elif require_agent_pin:
        _fail(
            "HERMES_AGENT_PIN is not configured; set it in Asterisk "
            "or omit --require-agent-pin for local/dev checks"
        )
        return False
    else:
        _warn(
            "HERMES_AGENT_PIN is not configured in the checked Asterisk config; "
            "extension 101 will fail closed until you set it"
        )

    return gate_ok


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
    parser.add_argument(
        "--asterisk-config-dir",
        default=str(DEFAULT_ASTERISK_CONFIG_DIR),
        help="Asterisk config directory to check for hermes.conf and HERMES_AGENT_PIN",
    )
    parser.add_argument(
        "--require-agent-pin",
        action="store_true",
        help="fail if HERMES_AGENT_PIN is not configured in the checked Asterisk config",
    )
    args = parser.parse_args()

    print(f"Preflight root: {ROOT}")
    checks_ok = [
        check_python(),
        check_dependencies(),
        check_files(),
        check_asterisk_pin_gate(
            Path(args.asterisk_config_dir),
            require_agent_pin=args.require_agent_pin,
        ),
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
