import asyncio
from typing import cast
from unittest.mock import patch

import pytest

from chat_service.contracts.database import AuthPhase
from chat_service.core.database_metrics import create_database_metrics
from chat_service.core.metrics import create_metrics


@pytest.mark.parametrize(
    "error,outcome",
    [(None, "success"), (ValueError, "failed"), (asyncio.CancelledError, "cancelled")],
)
def test_auth_phase_records_and_preserves_outcome(error, outcome):
    owner = create_metrics()
    metrics = create_database_metrics(owner, 2)
    if error is None:
        with metrics.auth_phase(AuthPhase.POOL):
            pass
    else:
        with pytest.raises(error), metrics.auth_phase(AuthPhase.POOL):
            raise error()
    assert (
        owner.registry.get_sample_value(
            "db_auth_phase_seconds_count", {"phase": "pool", "outcome": outcome}
        )
        == 1
    )


def test_metric_failure_does_not_change_business_result():
    owner = create_metrics()
    metrics = create_database_metrics(owner, 2)
    with patch.object(metrics.auth_duration, "labels", side_effect=RuntimeError):
        with metrics.auth_phase(AuthPhase.QUERY):
            pass
    assert owner.failed


def test_unbounded_label_is_rejected():
    metrics = create_database_metrics(create_metrics(), 2)
    with (
        pytest.raises(ValueError),
        metrics.auth_phase(cast(AuthPhase, "user-supplied-label")),
    ):
        pass
