import json
from datetime import UTC, datetime, timedelta

import pytest
from fakes import with_llm
from test_initiatives import NOON, InMemoryInitiativeStore, decision, init_services  # noqa: F401
from test_review import rv  # noqa: F401
from test_routines import InMemoryRoutineStore, Inbox

from uva import feedback, sent
from uva.clock import start_of_day
from uva.initiatives import InitiativeRecord, initiative_tick
from uva.logging.events import EventType, emit
from uva.review import LEARNED_SECTION, journal_digest, transcript
from uva.routines import Every, Routine, run_due
from uva.storage.sqlite import Database


@pytest.fixture
def fb(init_services, tmp_path):  # noqa: F811
    db = Database(tmp_path / "uva.db")
    db.migrate()
    init_services.sqlite = db
    return init_services


async def dislike(services, record_id, message_id=50, at=NOON):
    await sent.record(services.sqlite, [message_id], "initiative", record_id, now=at)
    return await feedback.record(services, message_id, ["👎"], now=at)


async def test_reactions_map_to_what_the_message_was(fb):
    await fb.initiatives.add(InitiativeRecord(id="i1", kind="research", topic="Rust async runtimes", at=NOON))
    kept = await dislike(fb, "i1")
    assert [(r.kind, r.ref, r.topic) for r in kept] == [("initiative", "i1", "rust async runtimes")]
    unknown = await feedback.record(fb, 999, ["❤️"], now=NOON)
    assert unknown[0].kind == "" and unknown[0].topic == ""
    assert await feedback.blocked_topics(fb, NOON) == ["rust async runtimes"]
    assert await feedback.blocked_topics(fb, NOON + timedelta(days=31)) == []  # INITIATIVE_BLOCK_DAYS


async def test_a_disliked_topic_is_refused_then_another_is_sent(fb):
    old = NOON - timedelta(days=20)
    await fb.initiatives.add(InitiativeRecord(id="i1", kind="research", topic="Rust async runtimes", at=old))
    await dislike(fb, "i1", at=old)
    provider, _ = with_llm(
        fb,
        decision(kind="research", topic="rust async runtimes"),
        decision(kind="message", message="Como foi a corrida de domingo?"),
    )
    status, text = await initiative_tick(fb, NOON, force=True)
    assert status == "sent" and "corrida" in text
    second = provider.calls[1]["messages"][0].content
    # The backfill gave the old initiative a subject, so the whole subject is blocked.
    assert "Not this one" in second and 'subject "rust" (blocked subject)' in second
    assert "Topics he disliked" in provider.calls[0]["messages"][0].content


async def test_refused_twice_sends_nothing(fb):
    await fb.initiatives.add(InitiativeRecord(id="i2", kind="message", topic="Python 3.15 news",
                                              message="💬 x", at=NOON - timedelta(days=3)))
    with_llm(fb, decision(kind="research", topic="python 3.15 news"), decision(kind="research", topic="Python 3.15 News!"))
    assert await initiative_tick(fb, NOON, force=True) == ("nothing", None)  # a repeat of 3 days ago


async def test_liked_topics_go_into_the_prompt(fb):
    await fb.initiatives.add(InitiativeRecord(id="i3", kind="research", topic="Pokémon TCG", at=NOON - timedelta(days=1)))
    await sent.record(fb.sqlite, [60], "initiative", "i3", now=NOON)
    await feedback.record(fb, 60, ["❤️"], now=NOON)
    provider, _ = with_llm(fb, decision())
    await initiative_tick(fb, NOON, force=True)
    assert "Topics he liked" in provider.calls[0]["messages"][0].content and "pokemon tcg" in provider.calls[0]["messages"][0].content


async def test_a_disliked_routine_report_goes_to_the_next_run(fb):
    fb.routines = InMemoryRoutineStore()
    routine = Routine(request="resume as notícias", every=Every.HOURS, hours=1, next_run_at=NOON,
                      last_run_at=NOON - timedelta(hours=1))
    await fb.routines.add(routine)
    await sent.record(fb.sqlite, [70], "routine", routine.id, now=NOON - timedelta(minutes=50))
    await feedback.record(fb, 70, ["👎"], now=NOON - timedelta(minutes=40))
    provider, _ = with_llm(fb, json.dumps({"actions": [], "reply": "Notícias."}),
                           json.dumps({"actions": [], "reply": "Notícias."}))
    await run_due(fb, Inbox(), NOON)
    assert "it was not liked" in provider.calls[0]["messages"][-1].content
    await fb.routines.update(routine.id, next_run_at=NOON + timedelta(hours=1))
    await run_due(fb, Inbox(), NOON + timedelta(hours=1))
    assert "it was not liked" not in provider.calls[1]["messages"][-1].content  # only the run after it


def test_review_transcript_shows_reactions():
    from uva.index.service import TurnRecord
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Sao_Paulo")
    turns = [TurnRecord(1, NOON, "user", "oi", "telegram"), TurnRecord(2, NOON + timedelta(minutes=2), "bot", "olá", "telegram")]
    reaction = feedback.Reaction(9, "👎", NOON + timedelta(minutes=1), "routine", "r1a2", "")
    lines = transcript(turns, tz, [reaction]).splitlines()
    assert lines[1].endswith("User reacted 👎 to UVA's routine r1a2") and lines[2].endswith("UVA: olá")


async def test_journal_digest_lists_what_was_learned(rv, event_store):  # noqa: F811
    rv.events = event_store
    yesterday = start_of_day(rv.tz) - timedelta(hours=6)
    for text in ("o Avaí ganhou", "previsão do tempo amanhã"):
        await rv.index.add_turn("user", text, at=yesterday)
    await emit(EventType.MEMORY, op="review", wrote=["memory:time", "skill:match-recap"], skipped=["profile:"],
               details=[{"target": "memory:time", "what": "Torce pro Avaí"},
                        {"target": "skill:match-recap", "what": "1. Placar primeiro"}])
    event_store.events[-1] = event_store.events[-1].model_copy(update={"ts": yesterday})
    with_llm(rv, "- Falou do Avaí.")
    await journal_digest(rv)
    day = yesterday.astimezone(rv.tz).date().isoformat()
    body = rv.vault.read(f"diario/{day}.md").body
    assert f"## {LEARNED_SECTION}" in body
    assert "- memória time: Torce pro Avaí (trace" in body and "- habilidade match-recap: 1. Placar primeiro" in body
    assert "- não guardei: perfil" in body
