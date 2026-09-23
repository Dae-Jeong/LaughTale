# Opt-in experiment; A/B/A share this image and only LAB_RELAY_IDLE_MS changes.
# Base ID: sha256:543d96de28ed63e972c2bc04c2725563cc7d3dd178d0420676c5ed62cf0aaf1d
FROM laughtale-chat:relay-heads-isolated-v1
COPY services/chat/src/chat_service/relay.py /app/.venv/lib/python3.14/site-packages/chat_service/relay.py
