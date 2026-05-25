import asyncio
import logging

import pytest

from src.bridge.errors import BridgeExternalTimeout, BridgeTransientError
from src.runtime.timeouts import with_timeout, with_timeout_or_none


@pytest.mark.asyncio
async def test_with_timeout_raises_typed_external_timeout():
    with pytest.raises(BridgeExternalTimeout) as excinfo:
        await with_timeout(
            asyncio.sleep(3600),
            timeout_seconds=0.01,
            operation="test.operation",
        )

    assert isinstance(excinfo.value, BridgeTransientError)
    assert excinfo.value.operation == "test.operation"
    assert excinfo.value.timeout_seconds == 0.01


@pytest.mark.asyncio
async def test_with_timeout_or_none_logs_and_returns_none(caplog):
    logger = logging.getLogger("tests.timeouts")

    with caplog.at_level(logging.ERROR, logger="tests.timeouts"):
        result = await with_timeout_or_none(
            asyncio.sleep(3600),
            timeout_seconds=0.01,
            logger=logger,
            event="test.timeout",
            operation="test.operation",
        )

    assert result is None
    assert "test.timeout" in caplog.text
    assert caplog.records[-1].event_fields["operation"] == "test.operation"
