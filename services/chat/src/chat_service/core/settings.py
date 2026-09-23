from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from platform_contracts.wire import Profile
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class ConnectionCredential(BaseModel):
    profile: Profile
    token: SecretStr = Field(min_length=16)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", frozen=True
    )

    app_name: str = Field(default="Laughtale Chat", min_length=1)
    service_version: str = Field(default="0.1.0", min_length=1)
    app_environment: str = Field(default="local", min_length=1, max_length=64)
    network_profile: Literal["local", "isolated-lab"] = "local"
    lab_message_admission_limit: int = Field(default=0, ge=0, le=32)
    session_backend: Literal["local", "postgres"] = "local"
    lab_ingress_enabled: bool = False
    lab_ingress_token: SecretStr = Field(default=SecretStr(""), repr=False)
    lab_pod_uid: UUID | None = None
    server_host: str = Field(default="127.0.0.1", min_length=1)
    server_port: int = Field(default=18082, ge=1, le=65535)
    shutdown_timeout_seconds: int = Field(default=15, ge=1, le=300)
    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"
    db_primary_url: str = Field(default="", repr=False)
    db_pool_size: int = Field(default=4, ge=1, le=100)
    db_pool_max_overflow: int = Field(default=0, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=2, gt=0, le=60)
    db_connect_timeout_seconds: float = Field(default=3, gt=0, le=60)
    db_statement_timeout_ms: int = Field(default=5000, ge=1, le=60000)
    db_lock_timeout_ms: int = Field(default=1000, ge=1, le=60000)
    dev_sessions_enabled: bool = False
    dev_origin: str = "http://127.0.0.1:18083"
    external_enabled: bool = False
    external_control_token: SecretStr = Field(default=SecretStr(""), repr=False)
    external_connection_credentials: dict[str, ConnectionCredential] = Field(
        default_factory=dict, repr=False
    )
    mock_api_url: str = "http://127.0.0.1:18087"
    mock_api_token: SecretStr = Field(default=SecretStr(""), repr=False)

    @field_validator("dev_origin")
    @classmethod
    def loopback_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.port is None
        ):
            raise ValueError("Use an exact loopback HTTP origin with explicit port")
        return value

    @model_validator(mode="after")
    def local_session_safety(self) -> Settings:
        lab = self.network_profile == "isolated-lab"
        if self.session_backend == "postgres" and not (lab and self.db_primary_url):
            raise ValueError("Shared sessions require isolated lab and Primary")
        if lab and (
            self.app_environment != "isolated-lab"
            or self.server_host != "0.0.0.0"
            or self.server_port != 18082
        ):
            raise ValueError(
                "Isolated lab requires explicit environment, bind and port"
            )
        if self.dev_sessions_enabled and not (
            lab
            or (
                self.network_profile == "local"
                and self.app_environment == "local"
                and self.server_host == "127.0.0.1"
            )
        ):
            raise ValueError(
                "Synthetic sessions require local environment and loopback bind"
            )
        return self

    @model_validator(mode="after")
    def lab_ingress_safety(self) -> Settings:
        if not self.lab_ingress_enabled:
            return self
        token = self.lab_ingress_token.get_secret_value()
        other_tokens = [
            self.external_control_token.get_secret_value(),
            self.mock_api_token.get_secret_value(),
            *(
                credential.token.get_secret_value()
                for credential in self.external_connection_credentials.values()
            ),
        ]
        if (
            self.network_profile != "isolated-lab"
            or self.app_environment != "isolated-lab"
            or self.session_backend != "postgres"
            or not self.db_primary_url
            or not self.dev_sessions_enabled
            or self.lab_pod_uid is None
            or not 32 <= len(token) <= 256
            or len(set(token)) < 8
            or not all(33 <= ord(char) <= 126 for char in token)
            or token in other_tokens
        ):
            raise ValueError(
                "Lab ingress requires shared sessions, Pod identity and a separate strong credential"
            )
        return self

    @model_validator(mode="after")
    def external_safety(self) -> Settings:
        if not self.external_enabled:
            return self
        if not self.dev_sessions_enabled or not self.db_primary_url:
            raise ValueError("External lab requires local sessions and explicit DB")
        parsed = urlsplit(self.mock_api_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname
            not in (
                {"127.0.0.1", "platform-mock"}
                if self.network_profile == "isolated-lab"
                else {"127.0.0.1"}
            )
            or parsed.port is None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Mock URL must be an exact loopback HTTP origin")
        from uuid import UUID

        for key in self.external_connection_credentials:
            if str(UUID(key)) != key:
                raise ValueError("Use canonical connection UUIDs")
        secrets = [
            self.external_control_token.get_secret_value(),
            self.mock_api_token.get_secret_value(),
            *(
                value.token.get_secret_value()
                for value in self.external_connection_credentials.values()
            ),
        ]
        if (
            not self.external_connection_credentials
            or len(self.external_connection_credentials) > 1000
            or any(
                not 16 <= len(value) <= 256
                or not all(33 <= ord(char) <= 126 for char in value)
                for value in secrets
            )
            or len(set(secrets)) != len(secrets)
        ):
            raise ValueError(
                "Use separate strong control, outbound and connection credentials"
            )
        return self

    @field_validator("db_primary_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        if not value:
            return value
        try:
            url = make_url(value)
        except ArgumentError as error:
            raise ValueError("Invalid database URL") from error
        if (
            url.drivername != "postgresql+asyncpg"
            or not url.database
            or url.query
            or not url.host
        ):
            raise ValueError(
                "Use a PostgreSQL asyncpg URL with host and database, without query options"
            )
        return value
