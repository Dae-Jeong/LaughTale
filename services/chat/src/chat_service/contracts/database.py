from enum import StrEnum


class AuthPhase(StrEnum):
    POOL = "pool"
    QUERY = "query"


class AuthOutcome(StrEnum):
    SUCCESS = "success"
    CANCELLED = "cancelled"
    FAILED = "failed"


class TransactionOutcome(StrEnum):
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


class AcquisitionOutcome(StrEnum):
    ACQUIRED = "acquired"
    TIMEOUT = "timeout"
    FAILED = "failed"
