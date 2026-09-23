"""python -m chat_service.gateway: 별도 Connection Gateway 진입점입니다."""

import logging

import uvicorn

from chat_service.bootstrap.gateway import create_gateway
from chat_service.core.realtime_settings import RealtimeSettings
from chat_service.core.settings import Settings


def main() -> None:
    logging.disable(logging.CRITICAL)
    try:
        settings = Settings()
        app = create_gateway(settings, RealtimeSettings())
    except Exception:
        raise SystemExit("Invalid gateway settings") from None
    uvicorn.run(
        app,
        host=settings.server_host,
        port=18082,
        proxy_headers=False,
        access_log=False,
        log_level="critical",
        timeout_graceful_shutdown=20,
        ws_max_size=16384,
        ws_max_queue=16,
    )


if __name__ == "__main__":
    main()
