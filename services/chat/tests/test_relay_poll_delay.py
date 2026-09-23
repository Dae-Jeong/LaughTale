import pytest

from chat_service.contracts.relay import RelayOutcome
from chat_service.relay import idle_delay_seconds, poll_delay


@pytest.mark.parametrize(("value", "expected"), [("100", 0.1), ("500", 0.5)])
def test_idle_delay(value, expected):
    delay = idle_delay_seconds(value)
    assert poll_delay(RelayOutcome.IDLE, delay) == expected
    assert poll_delay(RelayOutcome.DATABASE_RETRY, delay) == 0.5
    assert poll_delay(RelayOutcome.PUBLISHED, delay) == 0


@pytest.mark.parametrize("value", ["0", "-1", "101", "nan", "", "5000"])
def test_invalid_lab_delay(value):
    with pytest.raises(ValueError):
        idle_delay_seconds(value)
