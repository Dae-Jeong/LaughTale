"""실행 진입점 (LAUGH-KNOWLEDGE-READ-001).

    python -m theme_catalog.run

프로세스 설정은 여기서 소유하고 앱 조립은 `create_app`이 담당합니다.
이 진입점은 로컬 loopback 실험용이며 공개 노출·다중 worker는 범위 밖입니다.
"""

import sys

import uvicorn
from pydantic import ValidationError

from theme_catalog.bootstrap.app import create_app
from theme_catalog.core.settings import Settings


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
