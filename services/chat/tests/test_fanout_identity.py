import asyncio
from unittest.mock import Mock, patch
from uuid import UUID

import pytest

from chat_service.core.realtime_settings import RealtimeSettings
from chat_service.fanout import run


@pytest.mark.parametrize(
    "pod_uid", [None, UUID("00000000-0000-4000-8000-000000000001")]
)
def test_fanout_client_id_uses_validated_pod_uid(pod_uid):
    settings = RealtimeSettings(
        _env_file=None,
        app_environment="isolated-lab",
        network_profile="isolated-lab",
        redis_url="redis://default:synthetic@redis:6379/0",
        gateway_delivery_token="test-only-gateway-token-with-distinct-characters",
        lab_pod_uid=pod_uid,
    )
    factory = Mock(side_effect=RuntimeError("stop_before_external_io"))
    with patch("chat_service.fanout.AIOKafkaConsumer", factory):
        with pytest.raises(RuntimeError, match="stop_before_external_io"):
            asyncio.run(run(settings))
    kwargs = factory.call_args.kwargs
    if pod_uid:
        assert kwargs["client_id"] == f"chat-fanout-{pod_uid}"
    else:
        assert "client_id" not in kwargs
