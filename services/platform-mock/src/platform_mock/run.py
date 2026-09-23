import uvicorn
from pydantic import ValidationError

from platform_mock.app import create_app
from platform_mock.settings import Settings


def main() -> None:
    try:
        settings = Settings()
    except ValidationError:
        raise SystemExit("MOCK_SETTINGS_INVALID") from None
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0" if settings.network_profile == "isolated-lab" else "127.0.0.1",
        port=settings.server_port,
        workers=1,
        proxy_headers=False,
        access_log=False,
        timeout_graceful_shutdown=15,
    )


if __name__ == "__main__":
    main()
