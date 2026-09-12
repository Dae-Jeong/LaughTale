# Laughtale catalog-hub — Step 3 실험용 이미지
#
# D2에 따라 워커 1개 고정입니다. phub Dockerfile이 --workers 없이 uvicorn을 띄우므로
# 단일 프로세스가 모델링 대상입니다. Pod 다중화는 워커가 아니라 replica로 합니다.
FROM python:3.14-slim

WORKDIR /app

# 실험 의존성만 설치합니다. 기존 서비스의 lockfile을 건드리지 않습니다.
RUN pip install --no-cache-dir \
    "fastapi>=0.141.1" \
    "uvicorn>=0.34" \
    "pydantic-settings>=2.15.0" \
    "sqlalchemy[asyncio]>=2.0.52,<2.1" \
    "asyncpg>=0.31.0"

COPY src/catalog_hub /app/catalog_hub

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=8000

EXPOSE 8000

# create_app을 factory로 띄웁니다. 워커 1개입니다.
CMD ["python", "-m", "catalog_hub.run"]
