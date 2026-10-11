import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fakes import with_llm

from uva.router import Router
from uva.storage.expenses import ExpenseRepository, format_brl, normalize_category, parse_amount
from uva.storage.sqlite import Database


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "data" / "uva.db")
    database.migrate()
    return database


@pytest.fixture
def repo(db) -> ExpenseRepository:
    return ExpenseRepository(db)


def test_migrations_run_once_and_enable_wal(tmp_path):
    database = Database(tmp_path / "x.db")
    assert database.migrate() == 1
    assert database.migrate() == 0
    assert database.version() == 1
    with database.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_new_migrations_are_applied_in_order(tmp_path):
    path = tmp_path / "y.db"
    Database(path, ["CREATE TABLE a (x INTEGER);"]).migrate()
    newer = Database(path, ["CREATE TABLE a (x INTEGER);", "ALTER TABLE a ADD COLUMN y TEXT;"])
    assert newer.migrate() == 1
    with newer.connect() as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(a)")]
    assert columns == ["x", "y"]


def test_failed_migration_is_rolled_back(tmp_path):
    database = Database(tmp_path / "z.db", ["CREATE TABLE ok (x);", "CREATE TABLE broken (;"])
    with pytest.raises(sqlite3.OperationalError):
        database.migrate()
    assert database.version() == 1


@pytest.mark.parametrize(
    ("text", "cents"),
    [("25", 2500), ("25,5", 2550), ("25,90", 2590), ("25.9", 2590), ("R$1.234,56", 123456), ("1234.56", 123456)],
)
def test_parse_amount(text, cents):
    assert parse_amount(text) == cents


@pytest.mark.parametrize("text", ["abc", "-5", "1,2,3", ""])
def test_parse_amount_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_amount(text)


def test_format_brl():
    assert format_brl(123456) == "R$ 1.234,56"
    assert format_brl(5) == "R$ 0,05"


def test_normalize_category():
    assert normalize_category("Mercado") == normalize_category("mercado") == "mercado"
    assert normalize_category("Saúde Mental") == "saude_mental"


async def test_totals_by_period(repo):
    now = datetime.now(UTC)
    await repo.add(2500, "mercado", "pão")
    await repo.add(1000, "Mercado")
    await repo.add(5000, "transporte", spent_at=now - timedelta(days=40))
    assert await repo.totals_by_category(since=now - timedelta(days=1)) == [("mercado", 3500)]
    assert await repo.totals_by_category() == [("transporte", 5000), ("mercado", 3500)]
    assert [e.amount_cents for e in await repo.recent(2)] == [1000, 2500]


async def test_gasto_and_gastos_commands(services, repo):
    services.expenses = repo
    router = Router(services)
    reply = (await router.handle_text("/gasto 25 mercado pão e leite")).text
    assert reply.startswith("💸 R$ 25,00 em mercado")
    await router.handle_text("/gasto 10,50 transporte")
    summary = (await router.handle_text("/gastos mes")).text
    assert "Gastos neste mês: R$ 35,50" in summary
    assert "- mercado: R$ 25,00 (70%)" in summary
    assert "pão e leite" in summary
    assert "Não entendi o valor" in (await router.handle_text("/gasto muito mercado")).text


async def test_llm_records_expense(services, repo):
    services.expenses = repo
    with_llm(
        services,
        json.dumps(
            {
                "actions": [{"type": "expense_add", "params": {"amount": 30, "category": "mercado"}}],
                "reply": "Anotado!",
            }
        ),
    )
    reply = await Router(services).handle_text("gastei 30 reais no mercado")
    assert "R$ 30,00 em mercado" in reply.text
    assert await repo.totals_by_category() == [("mercado", 3000)]
