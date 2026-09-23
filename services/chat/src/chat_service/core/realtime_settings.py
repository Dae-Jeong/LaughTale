"""격리된 단일 노드 실험만 허용하는 Gateway/Fanout 설정입니다."""

from ipaddress import ip_address, ip_network
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

POD_NETWORK = ip_network("10.42.0.0/24")


def validate_ip(value: str, allow_loopback: bool = False) -> str:
    address = ip_address(value)
    if str(address) != value or not (
        address in POD_NETWORK or (allow_loopback and value == "127.0.0.1")
    ):
        raise ValueError("GATEWAY_ADDRESS_NOT_ALLOWED")
    return value


class RealtimeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)
    app_environment: Literal["isolated-lab"]
    network_profile: Literal["isolated-lab"]
    redis_url: SecretStr = Field(repr=False)
    gateway_delivery_token: SecretStr = Field(min_length=32, max_length=256, repr=False)
    gateway_browser_port: int = Field(default=18082, ge=1024, le=65535)
    gateway_ip: str = "127.0.0.1"
    realtime_allow_loopback: bool = False
    lab_pod_uid: UUID | None = None
    kafka_bootstrap_servers: Literal["kafka:9092", "127.0.0.1:19092"] = "kafka:9092"

    @field_validator("gateway_delivery_token")
    @classmethod
    def strong_token(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not all(33 <= ord(char) <= 126 for char in raw) or len(set(raw)) < 8:
            raise ValueError("USE_DISTINCT_STRONG_INTERNAL_SECRET")
        return value

    @model_validator(mode="after")
    def addresses(self) -> RealtimeSettings:
        parsed = urlsplit(self.redis_url.get_secret_value())
        hosts = {"redis"} | ({"127.0.0.1"} if self.realtime_allow_loopback else set())
        if (
            parsed.scheme != "redis"
            or parsed.hostname not in hosts
            or parsed.port != 6379
            or parsed.path != "/0"
            or parsed.query
            or parsed.fragment
            or not parsed.username
            or not parsed.password
        ):
            raise ValueError("REDIS_REQUIRES_EXPLICIT_LAB_ACL_AND_ADDRESS")
        return self
