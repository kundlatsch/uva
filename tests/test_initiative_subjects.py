"""Initiative subjects: saturation, 👎 on a subject, the 48h exception, the
backfill, and what the chooser's context leaves out."""

import sqlite3
from datetime import timedelta

import pytest
from fakes import FakeSearch, with_llm
from test_initiatives import NOON, InMemoryInitiativeStore, decision, init_services  # noqa: F401
from test_stm import InMemorySessionBackend

from uva import feedback, sent
from uva.actions.search import Research, save_research
from uva.initiatives import (
    BACKFILL_KEY,
    Decision,
    InitiativeRecord,
    _context,
    backfill_subjects,
    guess_subject,
    initiative_tick,
    refusal,
    same_subject,
    saturated_subjects,
    saturation,
)
from uva.search.base import SearchResult
from uva.stm.service import ShortTermMemory
from uva.storage.sqlite import MIGRATIONS, Database


@pytest.fixture
def subj(init_services, tmp_path):  # noqa: F811
    db = Database(tmp_path / "uva.db")
    db.migrate()
    init_services.sqlite = db
    return init_services


def add(services, rid, subject, days_ago, topic=""):
    record = InitiativeRecord(id=rid, kind="research", subject=subject, topic=topic or f"{subject} angle {rid}",
                              at=NOON - timedelta(days=days_ago))
    services.initiatives.records.append(record)
    return record


async def user_said(services, text, at):
    await services.sqlite.run(lambda conn: conn.execute(
        "INSERT INTO conversation_turns (at, role, text, source) VALUES (?, 'user', ?, 'telegram')",
        (at.isoformat(), text),
    ))


async def react(services, rid, emoji, message_id, at=NOON, subject=""):
    await sent.record(services.sqlite, [message_id], "initiative", rid, now=at, subject=subject)
    return await feedback.record(services, message_id, [emoji], now=at)


def score(found, subject):
    return next(s.score for s in found if s.subject == subject)


# --- saturation -----------------------------------------------------------------------


async def test_saturation_decays_with_a_one_week_half_life(init_services):  # noqa: F811
    services = init_services  # no SQLite: no engagement data, every send weighs 1
    add(services, "a", "mastodon", 0)
    add(services, "b", "mastodon", 7)
    add(services, "c", "mastodon", 14)
    add(services, "d", "rust", 7)
    found = await saturation(services, NOON)
    assert score(found, "mastodon") == pytest.approx(1 + 0.5 + 0.25)
    assert score(found, "rust") == pytest.approx(0.5)
    assert [s.subject for s in found] == ["mastodon", "rust"]
    assert next(s for s in found if s.subject == "mastodon").count == 3
    add(services, "e", "old", 40)  # outside the 30-day window
    assert "old" not in {s.subject for s in await saturation(services, NOON)}


async def test_engagement_weights(subj):
    add(subj, "ignored", "ignored", 6)
    add(subj, "replied", "replied", 7)
    add(subj, "liked", "liked", 8)
    add(subj, "reacted", "reacted", 9)
    await react(subj, "liked", "❤️", 1)
    await react(subj, "reacted", "🤔", 2)
    await user_said(subj, "que legal isso", NOON - timedelta(days=7) + timedelta(minutes=10))
    found = await saturation(subj, NOON)
    assert score(found, "ignored") == pytest.approx(0.5 ** (6 / 7) * 1.5)
    assert score(found, "replied") == pytest.approx(0.5)
    assert score(found, "liked") == pytest.approx(0.5 ** (8 / 7) * 0.5)
    assert score(found, "reacted") == pytest.approx(0.5 ** (9 / 7))


async def test_an_ignored_subject_saturates_and_is_refused(subj):
    for i in range(3):
        add(subj, f"m{i}", "mastodon", i * 2)
    saturated = await saturated_subjects(subj, NOON)
    assert [s.subject for s in saturated] == ["mastodon"]
    assert (saturated[0].count, saturated[0].days) == (3, 4)
    assert await refusal(subj, Decision(kind="research", subject="mastodon api", topic="x"), NOON) == "saturated"
    assert await refusal(subj, Decision(kind="research", subject="pokemon tcg", topic="x"), NOON) is None


async def test_threshold_and_half_life_are_settings(init_services):  # noqa: F811
    services = init_services
    add(services, "a", "mastodon", 0)
    assert [s.subject for s in await saturated_subjects(services, NOON)] == ["mastodon"]
    services.settings.initiative_saturation_threshold = 1.5
    assert await saturated_subjects(services, NOON) == []
    services.settings.initiative_saturation_threshold = 0.5
    add(services, "b", "rust", 2)
    services.settings.initiative_subject_half_life_days = 1.0
    assert [s.subject for s in await saturated_subjects(services, NOON)] == ["mastodon"]  # rust: 0.25


def test_same_subject():
    assert same_subject("mastodon", "Mastodon")
    assert same_subject("mastodon", "mastodon api")
    assert not same_subject("mastodon", "rust")
    assert not same_subject("", "")


# --- 👎 and the 48h exception -----------------------------------------------------------


async def test_a_dislike_blocks_the_whole_subject(subj):
    add(subj, "i1", "mastodon", 10, topic="Mastodon instance moderation")
    await react(subj, "i1", "👎", 7, at=NOON - timedelta(days=10), subject="mastodon")
    assert await feedback.blocked_subjects(subj, NOON) == ["mastodon"]
    other_angle = Decision(kind="research", subject="mastodon", topic="Mastodon client apps")
    assert await refusal(subj, other_angle, NOON) == "blocked subject"
    later = NOON + timedelta(days=subj.settings.initiative_block_days)
    assert await feedback.blocked_subjects(subj, later) == []


async def test_a_dislike_finds_the_subject_through_the_sent_message(subj):
    # A reaction kept before the record had a subject: the sent message has it.
    await subj.sqlite.run(lambda conn: conn.execute(
        "INSERT INTO reactions (message_id, emoji, at, kind, ref, topic) VALUES (9, '👎', ?, 'initiative', 'i9', '')",
        (NOON.isoformat(),),
    ))
    await sent.record(subj.sqlite, [9], "initiative", "i9", now=NOON, subject="mastodon")
    assert await feedback.blocked_subjects(subj, NOON) == ["mastodon"]


async def test_his_own_mention_lets_a_saturated_subject_through_once(subj):
    for i in range(3):
        add(subj, f"m{i}", "mastodon", 1 + i)
    await user_said(subj, "Vi que o Mastodon mudou a API, sabe algo?", NOON - timedelta(hours=5))
    assert await saturated_subjects(subj, NOON) == []
    # Once: after an initiative on it, the mention no longer counts.
    add(subj, "m9", "mastodon", 0)
    assert [s.subject for s in await saturated_subjects(subj, NOON)] == ["mastodon"]


async def test_an_old_mention_does_not_count(subj):
    for i in range(3):
        add(subj, f"m{i}", "mastodon", 1 + i)
    await user_said(subj, "mastodon", NOON - timedelta(hours=49))
    assert [s.subject for s in await saturated_subjects(subj, NOON)] == ["mastodon"]


async def test_a_saturated_subject_is_named_in_the_prompt_and_refused(subj):
    for i in range(3):
        add(subj, f"m{i}", "mastodon", 1 + i)
    provider, _ = with_llm(
        subj,
        decision(kind="message", subject="mastodon", message="E o Mastodon?"),
        decision(kind="message", subject="running", message="Como foi a corrida?"),
    )
    status, text = await initiative_tick(subj, NOON, force=True)
    assert status == "sent" and "corrida" in text
    first, second = (call["messages"][0].content for call in provider.calls)
    assert "Saturated subjects (don't pick these): mastodon (3 in 3 days)" in first
    assert '[mastodon]' in first
    assert 'Not this one' in second and 'subject "mastodon" (saturated)' in second
    assert subj.initiatives.records[-1].subject == "running"


async def test_a_missing_subject_is_guessed_from_the_topic(init_services):  # noqa: F811
    services = init_services
    add(services, "a", "mastodon", 20)
    with_llm(services, decision(kind="message", topic="New Mastodon clients", message="Viu os apps novos?"))
    assert (await initiative_tick(services, NOON, force=True))[0] == "sent"
    assert services.initiatives.records[-1].subject == "mastodon"


# --- context ---------------------------------------------------------------------------


async def test_context_leaves_out_bot_notes_the_journal_and_notices(init_services):  # noqa: F811
    services = init_services
    vault = services.vault
    found = Research(query="q", source="web", backend="web", summary="Resumo.")
    tagged = save_research(vault, found, "Mastodon moderação", initiative=True)
    untagged = save_research(vault, found, "Mastodon API")  # made before the tag existed
    services.initiatives.records.append(InitiativeRecord(kind="research", topic="x", note=untagged.path,
                                                         at=NOON - timedelta(days=2)))
    mine = vault.create_note("Minha corrida", "5km hoje", folder="areas")
    vault.append_daily("algo")
    services.stm = ShortTermMemory(InMemorySessionBackend(), services.settings)
    await services.stm.add_turn("user", "oi, tudo bem?")
    await services.stm.add_turn("notice", "🧠 Mastodon de novo")
    await services.stm.add_turn("bot", "tudo sim")

    context = await _context(services, NOON)
    notes = context.split("Recent second brain notes:\n")[1].split("\n\n")[0]
    assert mine.path in notes
    assert tagged.path not in notes and untagged.path not in notes and "diario/" not in notes
    conversation = context.split("Recent conversation:\n")[1].split("\n\n")[0]
    assert "oi, tudo bem?" in conversation and "tudo sim" in conversation
    assert "Mastodon de novo" not in conversation


async def test_initiative_research_notes_are_tagged(init_services):  # noqa: F811
    services = init_services
    services.search = {"web": FakeSearch("web", SearchResult("Masto", "https://m.example", "x"))}
    with_llm(services, decision(kind="research", subject="mastodon", topic="Mastodon API"), "Resumo.")
    assert (await initiative_tick(services, NOON))[0] == "sent"
    note = services.initiatives.records[-1].note
    assert "iniciativa" in (services.vault.root / note).read_text(encoding="utf-8")


# --- storage ---------------------------------------------------------------------------


def test_migration_adds_subject_columns_to_existing_rows(tmp_path):
    path = tmp_path / "old.db"
    Database(path, MIGRATIONS[:14]).migrate()
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO sent_messages (message_id, kind, ref, at) VALUES (1, 'initiative', 'i1', 'x')")
    conn.execute("INSERT INTO reactions (message_id, emoji, at) VALUES (1, '👎', 'x')")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.migrate() == len(MIGRATIONS) - 14
    with db.connect() as conn:
        assert conn.execute("SELECT subject FROM sent_messages").fetchone()[0] == ""
        assert conn.execute("SELECT subject FROM reactions").fetchone()[0] == ""


async def test_sent_keeps_subject_and_initiatives_for_60_days(subj):
    db = subj.sqlite
    await sent.record(db, [1], "initiative", "i1", now=NOON - timedelta(days=45), subject="mastodon")
    await sent.record(db, [2], "task", "t1", now=NOON - timedelta(days=45))
    await sent.record(db, [3], "initiative", "i3", now=NOON - timedelta(days=61))
    assert (await sent.lookup(db, 1)).subject == "mastodon"
    assert await sent.purge(db, NOON) == 2
    assert await sent.lookup(db, 1) is not None
    assert await sent.lookup(db, 2) is None and await sent.lookup(db, 3) is None


# --- backfill --------------------------------------------------------------------------


def test_guess_subject_prefers_the_common_word():
    from collections import Counter

    assert guess_subject("Latest news on Fediverse and Mastodon", Counter({"mastodon": 5})) == "mastodon"
    assert guess_subject("Rust async runtimes") == "rust"
    assert guess_subject("the news") == ""
    assert guess_subject("💬 Taguma, teu brain já tem coisa andando", Counter({"brain": 2})) == "brain"


async def test_backfill_gives_old_initiatives_a_subject_once(subj):
    topics = ["Mastodon instance moderation", "Mastodon API changes", "Fediverse and Mastodon growth",
              "Mastodon vs Bluesky", "Rust async runtimes"]
    for i, topic in enumerate(topics):
        subj.initiatives.records.append(InitiativeRecord(id=f"i{i}", kind="research", topic=topic,
                                                         at=NOON - timedelta(days=2 * i)))
    subj.initiatives.records.append(InitiativeRecord(id="old", kind="research", topic="Mastodon",
                                                     at=NOON - timedelta(days=40)))
    await sent.record(subj.sqlite, [10, 11], "initiative", "i1", now=NOON - timedelta(days=2))
    assert await backfill_subjects(subj, NOON) == 5
    subjects = {r.id: r.subject for r in subj.initiatives.records}
    assert subjects == {"i0": "mastodon", "i1": "mastodon", "i2": "mastodon", "i3": "mastodon", "i4": "rust",
                        "old": ""}
    assert (await sent.lookup(subj.sqlite, 11)).subject == "mastodon"
    assert await subj.sqlite.get_kv(BACKFILL_KEY)
    assert await backfill_subjects(subj, NOON) == 0
    assert [s.subject for s in await saturated_subjects(subj, NOON)] == ["mastodon"]
