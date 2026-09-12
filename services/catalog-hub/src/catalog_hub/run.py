"""실행 진입점 (Laughtale 캐싱 실험).

    python -m catalog_hub.run

D2: 워커 1개 고정입니다. phub Dockerfile이 --workers 없이 uvicorn을 띄우므로
단일 프로세스가 모델링 대상입니다. 이 진입점은 로컬 loopback 실험용이며
공개 노출은 범위 밖입니다.
"""

import sys

import uvicorn
from pydantic import ValidationError

from catalog_hub.bootstrap.app import create_app
from catalog_hub.core.settings import Settings


def main() -> None:
    try:
        settings = Settings()
    except ValidationError as error:
        # 환경값과 알 수 없는 키 이름은 출력하지 않습니다.
        for detail in error.errors(include_input=False, include_context=False):
            field = detail["loc"][0] if detail["loc"] else "settings"
            if field not in Settings.model_fields:
                field = "settings"
            print(f"{field}: {detail['type']}", file=sys.stderr)
        raise SystemExit(1) from None

    uvicorn.run(
        create_app(settings),
        host=settings.server_host,
        port=settings.server_port,
        log_level=settings.log_level,
        access_log=False,
        workers=1,
        proxy_headers=False,
        timeout_graceful_shutdown=settings.shutdown_timeout_seconds,
    )


if __name__ == "__main__":
    main()
