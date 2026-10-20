from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fakes import InMemoryUsageStore, make_settings

from uva.brain.vault import Vault
from uva.index.service import Hit, SearchIndex
from uva.index.vectors import OpenAIEmbedder, VecHit, VectorIndex, build_vectors, chunks, rank
from uva.recall import recall
from uva.storage.sqlite import Database
from uva.text import normalize

SP = ZoneInfo("America/Sao_Paulo")
# Words that mean the same land on the same dimension (a toy embedding model).
CONCEPTS = [("carro", "automovel", "veiculo"), ("praia", "litoral", "mar"), ("bolo", "torta", "receita"),
            ("python", "codigo", "programa")]


class FakeEmbedder:
    model = "fake-embed"

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def embed(self, texts):
        if self.fail:
            raise RuntimeError("endpoint down")
        self.calls.append(list(texts))
        out = []
        for text in texts:
            words = normalize(text).split()
            out.append([sum(1.0 for w in words for v in group if w.startswith(v)) + 0.01 for group in CONCEPTS])
        return out


@pytest.fixture
def hv(services, tmp_path):
    db = Database(tmp_path / "uva.db")
    db.migrate()
    services.sqlite = db
    services.vault = Vault(tmp_path / "brain", SP)
    services.vault.ensure_structure()
    services.index = SearchIndex(db, services.vault)
    services.vault.on_change.append(services.index.note_changed)
    return services


def notes(vault):
    vault.create_note("Revisão do carro", "Levar o carro na oficina para trocar o óleo.", folder="areas")
    vault.create_note("Férias", "Uma semana na praia em janeiro.", folder="projetos")
    vault.create_note("Doces", "Receita de bolo de cenoura da vó.", folder="recursos")


@pytest.mark.parametrize("native", [True, False])
async def test_a_paraphrase_is_found_that_full_text_misses(hv, native):
    notes(hv.vault)
    hv.vectors = VectorIndex(hv.sqlite, FakeEmbedder())
    hv.vectors.native = None if native else False
    result = await hv.vectors.sync()
    assert result["embedded"] == 3 and await hv.vectors.count() == 3
    assert await hv.index.search(["automovel"], kinds=("note",)) == []  # FTS alone misses it
    found = await recall(hv, "quando é a manutenção do automóvel?")
    assert found.notes and found.notes[0] == "02-areas/revisao_do_carro.md"
    again = await hv.vectors.sync()  # nothing changed: nothing embedded again
    assert again == {"embedded": 0, "removed": 0}


async def test_changes_and_removals_follow_the_index(hv):
    notes(hv.vault)
    hv.vectors = VectorIndex(hv.sqlite, FakeEmbedder())
    await hv.vectors.sync()
    hv.vault.update_note("03-recursos/doces.md", body="Receita de torta de limão.")
    (hv.vault.root / "01-projetos" / "ferias.md").unlink()
    await hv.index.sync_notes()
    result = await hv.vectors.sync()
    assert result["removed"] >= 1 and result["embedded"] >= 1
    rebuilt = await hv.vectors.rebuild()
    assert rebuilt["embedded"] == await hv.vectors.count()


async def test_failures_fall_back_to_full_text(hv):
    notes(hv.vault)
    hv.vectors = VectorIndex(hv.sqlite, FakeEmbedder(fail=True))
    found = await recall(hv, "receita de bolo de cenoura")
    assert found.notes == ["03-recursos/doces.md"]  # plain FTS still works


def test_ranking_mixes_scores_and_spreads_results():
    now = datetime(2026, 10, 19, tzinfo=UTC)
    fts = [Hit("note", "a.md", "A", "", "", -5.0), Hit("note", "b.md", "B", "", "", -1.0)]
    vec = [VecHit("note", "c.md", 0.9, [1.0, 0.0]), VecHit("note", "c.md", 0.8, [1.0, 0.0]),
           VecHit("note", "a.md", 0.2, [0.0, 1.0]), VecHit("note", "z.md", 0.1, [0.0, 1.0])]
    ranked = rank(fts, vec, limit=5, now=now)
    refs = [r.ref for r in ranked]
    assert refs[0] == "a.md" and "c.md" in refs and refs.count("c.md") == 1  # one entry per note
    assert "z.md" not in refs  # too far to count on meaning alone
    old = (now - timedelta(days=60)).isoformat()
    memory = rank([], [VecHit("memory", "old", 0.9, [1.0], old), VecHit("memory", "new", 0.8, [1.0], now.isoformat())],
                  limit=2, now=now)
    assert memory[0].ref == "new"  # 30-day half-life for memory


def test_chunks():
    text = ("Parágrafo um. " * 40 + "\n\n") * 3
    pieces = chunks(text)
    assert len(pieces) > 1 and all(len(p) <= 800 for p in pieces)


async def test_cost_is_counted_under_embedding():
    usage = InMemoryUsageStore()
    embedder = OpenAIEmbedder(make_settings(embedding_api_key="sk-test", search_embeddings=True), usage)

    async def create(model, input):
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000),
                               data=[SimpleNamespace(embedding=[0.1, 0.2]) for _ in input])

    embedder.client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
    vectors = await embedder.embed(["a", "b"])
    assert vectors == [[0.1, 0.2], [0.1, 0.2]]
    record = usage.records[0]
    assert record.purpose == "embedding" and record.cost_usd == pytest.approx(0.00002)


def test_off_by_default(tmp_path):
    db = Database(tmp_path / "x.db")
    assert build_vectors(make_settings(), db) is None
    assert build_vectors(make_settings(search_embeddings=True), db) is None  # no OpenAI-compatible key
    assert build_vectors(make_settings(search_embeddings=True, embedding_api_key="sk"), db) is not None
