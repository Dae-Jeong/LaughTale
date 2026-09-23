from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MOCK_", env_file=".env", extra="forbid", hide_input_in_errors=True
    )
    server_port: int = Field(default=18087, ge=1024, le=65535)
    network_profile: Literal["local", "isolated-lab"] = "local"
    control_token: SecretStr
    service_token: SecretStr
    chat_base_url: str = "http://127.0.0.1:18082"
    connection_tokens: dict[UUID, SecretStr] = Field(default_factory=dict, repr=False)
    delivery_timeout_seconds: float = Field(default=2, gt=0, le=5)

    @model_validator(mode="after")
    def check_local(self) -> Self:
        target = urlsplit(self.chat_base_url)
        if (
            target.scheme != "http"
            or target.hostname
            not in (
                {"127.0.0.1", "chat"}
                if self.network_profile == "isolated-lab"
                else {"127.0.0.1"}
            )
            or target.port is None
            or target.username
            or target.password
            or target.path
            or target.query
            or target.fragment
        ):
            raise ValueError("Chat target must be an explicit loopback HTTP authority")
        secrets = [
            self.control_token.get_secret_value(),
            self.service_token.get_secret_value(),
        ]
        if any(len(value) < 24 for value in secrets) or secrets[0] == secrets[1]:
            raise ValueError(
                "Distinct control and service tokens of at least 24 characters required"
            )
        if any(
            len(token.get_secret_value()) < 24
            for token in self.connection_tokens.values()
        ):
            raise ValueError("Connection tokens must contain at least 24 characters")
        all_tokens = secrets + [
            token.get_secret_value() for token in self.connection_tokens.values()
        ]
        if any(
            not token.isascii()
            or any(ord(char) <= 32 or ord(char) >= 127 for char in token)
            for token in all_tokens
        ):
            raise ValueError("Tokens must be visible ASCII without whitespace")
        return self
