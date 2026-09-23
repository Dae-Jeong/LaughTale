from chat_service.exceptions.application import ApplicationError


class InvalidMessageTextError(ApplicationError):
    """보존·저장 가능한 메시지 본문 규칙을 만족하지 않습니다."""


class IdempotencyConflictError(ApplicationError):
    """같은 범위의 멱등 키에 다른 메시지 내용이 들어왔습니다."""


class ConversationNotFoundError(ApplicationError):
    """없는 방과 권한 없는 방을 구분해 공개하지 않습니다."""


class UnauthenticatedError(ApplicationError):
    """유효한 서버 세션이 없습니다."""


class InvalidCursorError(ApplicationError):
    """현재 committed head에 맞지 않는 cursor입니다."""
