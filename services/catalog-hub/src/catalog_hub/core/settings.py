"""서비스 설정 (Laughtale 캐싱 실험).

원본 DB는 기존 5433 `thready-postgres` 인스턴스 안의 **합성** DB입니다.
새 PostgreSQL 인스턴스를 띄우지 않습니다 (`/Users/marin/AGENTS.md` 제1원칙).
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 합성 실험의 지원 언어입니다. 조립 후 i18n 치환 단계를 재현하기 위한 값입니다.
SUPPORTED_LANGS: tuple[str, ...] = ("ko", "en", "ja", "zh", "yue", "th", "vi")
DEFAULT_LANG = "ko"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_prefix="", frozen=True
    )

    app_name: str = Field(default="Laughtale Catalog Hub", min_length=1)
    service_version: str = Field(default="0.1.0", min_length=1)
    app_environment: str = Field(default="local", min_length=1, max_length=64)
    server_host: str = Field(default="127.0.0.1", min_length=1)
    server_port: int = Field(default=18093, ge=1, le=65535)
    shutdown_timeout_seconds: int = Field(default=15, ge=1, le=300)
    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"

    # 합성 DB입니다. 회사 DB(procedure_hub)를 가리키지 않습니다.
    db_url: str = Field(
        default="postgresql+asyncpg://thready:thready@127.0.0.1:5433/catalog_hub_lab",
        repr=False,
    )
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_pool_max_overflow: int = Field(default=0, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=5, gt=0, le=60)
    db_connect_timeout_seconds: float = Field(default=5, gt=0, le=60)
    db_statement_timeout_ms: int = Field(default=30_000, ge=1, le=120_000)

    # Step 0은 캐시 없음이 기준선입니다. Step 2에서 켭니다.
    response_cache_enabled: bool = False

    # 구간 타이머입니다. 타이머 없는 회차와 비교해 비용을 보정합니다.
    stage_timing_enabled: bool = False
