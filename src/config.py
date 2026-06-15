"""
Configuration loader for the Pipecat Hermes Skill.

Loads settings from config/config.yaml and provides a validated
configuration object.
"""

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class STTConfig(BaseModel):
    engine: str = "faster-whisper"
    model: str = "base"


class TTSConfig(BaseModel):
    engine: str = "piper"


class HermesConfig(BaseModel):
    endpoint: str = "http://localhost:8080"
    # "openai" = Hermes API server (/v1/chat/completions)
    # "hermes" = generic HTTP agent (/message, /chat, ...)
    # "ollama" = Ollama /api/chat
    backend: str = "hermes"
    model: str = "hermes-agent"
    api_key: str = ""
    timeout_seconds: int = 120


class SessionConfig(BaseModel):
    timeout_seconds: int = 300
    persist: bool = True
    persist_dir: str = "sessions"


class LoggingConfig(BaseModel):
    level: str = "INFO"


class PipecatHermesConfig(BaseModel):
    hermes: HermesConfig = Field(default_factory=HermesConfig)
    pipecat: dict = Field(default_factory=lambda: {
        "stt": STTConfig().dict(),
        "tts": TTSConfig().dict()
    })
    session: SessionConfig = Field(default_factory=SessionConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_config(config_path: Optional[str] = None) -> PipecatHermesConfig:
    """
    Load configuration from YAML file.

    If no path is provided, defaults to config/config.yaml
    relative to the project root.
    """
    if config_path is None:
        config_path = Path(__file__).parent.parent / "config" / "config.yaml"

    with open(config_path, "r") as f:
        raw_config = yaml.safe_load(f)

    return PipecatHermesConfig(**raw_config)


# Global config instance (can be overridden in tests)
config: PipecatHermesConfig = load_config()