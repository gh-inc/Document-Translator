import pytest

from app.core.errors import ErrorCode, ProviderError
from app.worker.executor import Executor


@pytest.mark.asyncio
async def test_executor_retries_retryable_provider_error() -> None:
    executor = Executor(max_attempts=3, base_wait=0, max_wait=0)
    calls = 0

    async def task() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ProviderError(ErrorCode.PROVIDER_TIMEOUT)
        return "translated"

    assert await executor.execute(task) == "translated"
    assert calls == 3


@pytest.mark.asyncio
async def test_executor_reraises_non_retryable_provider_error_immediately() -> None:
    executor = Executor(max_attempts=4, base_wait=0, max_wait=0)
    calls = 0

    async def task() -> str:
        nonlocal calls
        calls += 1
        raise ProviderError(ErrorCode.PROVIDER_BAD_REQUEST)

    with pytest.raises(ProviderError) as raised:
        await executor.execute(task)

    assert raised.value.error_code is ErrorCode.PROVIDER_BAD_REQUEST
    assert calls == 1


@pytest.mark.asyncio
async def test_executor_reraises_last_error_after_max_attempts() -> None:
    executor = Executor(max_attempts=2, base_wait=0, max_wait=0)
    calls = 0

    async def task() -> str:
        nonlocal calls
        calls += 1
        raise ProviderError(ErrorCode.PROVIDER_TIMEOUT)

    with pytest.raises(ProviderError) as raised:
        await executor.execute(task)

    assert raised.value.error_code is ErrorCode.PROVIDER_TIMEOUT
    assert calls == 2


def test_executor_requires_positive_attempt_count() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        Executor(max_attempts=0)
