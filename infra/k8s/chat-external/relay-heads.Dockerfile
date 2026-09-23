# Local, opt-in experiment: preserve the deployed v5 runtime except this query.
# Verified base image ID: 1ea81f5f1c072dda465d21ed01d66940b7561717b60d403293b088b2aa1f5caa
FROM laughtale-chat:external-lab-v5
COPY services/chat/src/chat_service/repositories/relay.py /app/.venv/lib/python3.14/site-packages/chat_service/repositories/relay.py
