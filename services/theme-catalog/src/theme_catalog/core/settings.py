"""서비스 설정 (LAUGH-KNOWLEDGE-READ-001).

독립 실험 서비스이므로 DB·외부 client·인증 설정이 없습니다.
loopback 기본값만 사용하며 공개 노출은 범위 밖입니다.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from theme_catalog.core.fixture import DEFAULT_FIXTURE_PATH


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_prefix="", frozen=True
    )

    app_name: str = Field(default="Laughtale Theme Catalog", min_length=1)
    service_version: str = Field(default="0.1.0", min_length=1)
    app_environment: str = Field(default="local", min_length=1, max_length=64)
    server_host: str = Field(default="127.0.0.1", min_length=1)
    server_port: int = Field(default=18092, ge=1, le=65535)
    shutdown_timeout_seconds: int = Field(default=15, ge=1, le=300)
    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"

    fixture_path: Path = Field(default=DEFAULT_FIXTURE_PATH)
    cache_enabled: bool = False
    cache_capacity: int = Field(default=16, ge=1, le=4096)
