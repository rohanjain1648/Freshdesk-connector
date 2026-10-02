from __future__ import annotations

import pytest

from freshdesk_connector.config import MOCK_API_KEY, MOCK_DOMAIN, Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(domain=MOCK_DOMAIN, api_key=MOCK_API_KEY, mock=True, rate_limit_per_minute=6000)


class FakeSleep:
    """Records requested sleeps and advances a virtual clock, so retry tests run instantly."""

    def __init__(self) -> None:
        self.calls: list[float] = []
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_sleep() -> FakeSleep:
    return FakeSleep()
