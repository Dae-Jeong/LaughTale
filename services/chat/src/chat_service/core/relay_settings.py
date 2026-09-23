"""외부 배포에 재사용하지 않는 plaintext 실험 전용 설정입니다."""

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class RelaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)
    app_environment: Literal["isolated-lab"]
    network_profile: Literal["isolated-lab"]
    db_primary_url: str = Field(repr=False)
    kafka_bootstrap_servers: Literal["kafka:9092", "127.0.0.1:19092"]

    @model_validator(mode="after")
    def lab_database(self) -> RelaySettings:
        url = make_url(self.db_primary_url)
        if (
            url.drivername != "postgresql+asyncpg"
            or (url.host, url.port) not in {("chat-primary", 5432), ("127.0.0.1", 5440)}
            or url.database != "laughtale_chat"
            or url.username != "chat_writer"
            or not url.password
            or url.query
        ):
            raise ValueError("Relay requires the dedicated lab primary")
        return self
