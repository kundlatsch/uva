from datetime import UTC, datetime, timedelta

import pytest
from fakes import InMemoryUsageStore, make_settings, with_llm

from uva.llm.budget import Budget, Scope, UsageRecord
from uva.phrases import Phrases
from uva.router import Router


class InMemoryExtraStore:
    def __init__(self) -> None:
        self.days: dict[str, float] = {}

    async def get(self, day):
        return self.days.get(day, 0.0)

    async def add(self, day, amount):
        self.days[day] = self.days.get(day, 0.0) + amount
        return self.days[day]


def spent(usage, cost, ts=None):
    usage.records.append(
        UsageRecord(provider="p", model="m", input_tokens=0, output_tokens=0, purpose="t",
                    cost_usd=cost, scope=Scope.INTERACTIVE, **({"ts": ts} if ts else {}))
    )


@pytest.fixture
def budget_services(services):
    provider, usage = with_llm(services, '{"actions": [], "reply": "Voltei!"}')
    services.budget = Budget(usage, services.settings, services.tz, InMemoryExtraStore())
    return services, usage, provider


async def test_liberar_unblocks_the_llm_today(budget_services):
    services, usage, provider = budget_services
    spent(usage, services.settings.llm_daily_budget_usd)  # daily budget used up
    router = Router(services)
    assert "orçamento diário" in (await router.handle_text("me conta uma piada")).text

    reply = (await router.handle_text("/liberar 2")).text
    assert reply.startswith("✅ Liberei US$ 2.00 a mais para hoje.")
    assert "Limite de hoje: US$ 3.00" in reply and "mensal continua valendo" in reply
    assert (await router.handle_text("me conta uma piada")).text == "Voltei!"


async def test_liberar_without_value_adds_a_full_daily_budget(budget_services):
    services, _, _ = budget_services
    await Router(services).handle_text("/liberar")
    spend = await services.budget.spend()
    assert spend.extra_today == services.settings.llm_daily_budget_usd
    assert spend.daily_limit(services.settings) == 2 * services.settings.llm_daily_budget_usd


async def test_extra_expires_at_midnight(budget_services):
    services, _, _ = budget_services
    today = datetime(2026, 10, 11, 20, 0, tzinfo=UTC)  # 17:00 in São Paulo
    await services.budget.grant_extra(5, now=today)
    assert (await services.budget.spend(now=today)).extra_today == 5
    tomorrow = today + timedelta(hours=8)  # 01:00 local next day
    assert (await services.budget.spend(now=tomorrow)).extra_today == 0


async def test_monthly_budget_still_applies(budget_services):
    services, usage, _ = budget_services
    spent(usage, services.settings.llm_monthly_budget_usd)
    await services.budget.grant_extra(10)
    status = await services.budget.check(Scope.INTERACTIVE)
    assert not status.allowed and "mensal" in status.reason


@pytest.mark.parametrize("arg", ["muito", "0", "-1", "999"])
async def test_invalid_amounts(budget_services, arg):
    services, _, _ = budget_services
    reply = (await Router(services).handle_text(f"/liberar {arg}")).text
    assert reply.startswith(("Uso:", "O valor precisa"))
    assert (await services.budget.spend()).extra_today == 0


async def test_status_shows_the_extra(budget_services):
    services, _, _ = budget_services
    await services.budget.grant_extra(0.5)
    text = (await Router(services).handle_text("/status")).text
    assert "inclui US$ 0.50 liberados hoje" in text


def test_transcribing_phrases_exist():
    phrases = Phrases(make_settings().phrases_file)
    assert len(phrases.categories["transcrevendo"]) >= 10
    assert phrases.pick("transcrevendo").startswith("🎙️")
