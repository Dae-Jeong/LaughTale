from chat_service.exceptions.application import ApplicationError


class InvalidMessageTextError(ApplicationError):
    """보존·저장 가능한 메시지 본문 규칙을 만족하지 않습니다."""


class IdempotencyConflictError(ApplicationError):
    """같은 범위의 멱등 키에 다른 메시지 내용이 들어왔습니다."""
