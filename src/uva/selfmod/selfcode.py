"""Self-coding: the bot writes a new plugin command for itself.

Runs as a Celery task (see uva.tasks.jobs.selfcode):
  1. clone the mounted repository into a temp dir, on a new branch;
  2. ask the heavy model for a plugin + tests (JSON), following uva.plugins.api;
  3. reject code that fails the static check (uva.selfmod.codecheck);
  4. write the files and run the full test suite; on failure, feed the output
     back and retry (SELFCODE_MAX_ATTEMPTS);
  5. when green, commit (UVA-Self-Code trailer) and merge into main;
  6. ask the running bot to hot-reload plugins; if the new plugin doesn't
     load there, revert the merge;
  7. tell Gustavo what was built (or why not).
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from uva.clock import utcnow
from uva.i18n import language_name, t
from uva.llm.base import LLMError, LLMMessage
from uva.llm.budget import Scope
from uva.llm.client import metered
from uva.logging.events import EventType, emit, trace
from uva.replies import Reply
from uva.selfmod import codecheck
from uva.selfmod.features import FeatureRecord, FeatureStatus
from uva.selfmod.git import SELF_CODE, Commit, GitError, GitRepo
from uva.selfmod.plugins import load_plugins, plugin_files
from uva.selfmod.testrun import SuiteResult, run_pytest
from uva.services import Services

Notifier = Callable[[Reply], Awaitable[None]]
TestRunner = Callable[[Path], Awaitable[SuiteResult]]
Reloader = Callable[[], Awaitable[dict | None]]

PLUGINS_REL = Path("src/uva/plugins")
TESTS_REL = Path("tests/plugins")
EXAMPLE = "moeda"


class GeneratedPlugin(BaseModel):
    name: str = Field(description="Nome do módulo (a-z, 0-9, _), ex.: dado")
    command: str = Field(description="Nome do comando principal, sem barra")
    summary: str = Field(description="Uma frase dizendo o que o comando faz")
    usage: str = Field(description="Exemplo de uso, ex.: /dado 20")
    plugin_code: str = Field(description="Conteúdo completo de src/uva/plugins/<name>.py")
    test_code: str = Field(description="Conteúdo completo de tests/plugins/test_<name>.py")


GENERATED_SCHEMA = GeneratedPlugin.model_json_schema()


class SelfCodeError(RuntimeError):
    pass


@dataclass(slots=True)
class Attempt:
    plugin: GeneratedPlugin | None
    feedback: str  # what went wrong, for the next attempt ("" when green)
    tests: SuiteResult | None = None


async def build_feature(
    services: Services,
    feature_id: str,
    notify: Notifier,
    *,
    request_reload: Reloader,
    run_tests: TestRunner | None = None,
) -> FeatureRecord:
    """Build one requested feature end to end. Never raises; the record says how it went."""
    store = services.features.store  # type: ignore[union-attr]
    record = await store.get(feature_id)
    if record is None:
        raise SelfCodeError(t("selfmod.selfcode.errors.missing", id=feature_id))
    run_tests = run_tests or _default_runner(services)

    with trace(record.trace_id):
        await store.update(feature_id, status=FeatureStatus.RUNNING)
        await _feature_ref(services, feature_id, "rodando")
        await emit(EventType.SELFCODE, op="start", feature_id=feature_id, description=record.description)
        busy_key = f"selfcode:{feature_id}"
        if services.busy is not None:
            await services.busy.mark(busy_key, services.settings.selfcode_time_limit_seconds)
        try:
            with metered() as meter:
                try:
                    fields = await _build(services, record, request_reload, run_tests)
                except SelfCodeError as exc:
                    fields = {"status": FeatureStatus.FAILED, "error": str(exc)}
            fields.update(cost_usd=round(meter.cost_usd, 4), finished_at=utcnow())
            await store.update(feature_id, **fields)
        finally:
            if services.busy is not None:
                await services.busy.clear(busy_key)

        final = await store.get(feature_id)
        assert final is not None
        ok = final.status is FeatureStatus.MERGED
        await _feature_ref(services, feature_id, "ok" if ok else "falhou", None if ok else final.error)
        completed_goal = None
        if ok and final.goal_id:
            from uva.goals.runner import complete_goal_for_feature  # avoid an import cycle

            completed_goal = await complete_goal_for_feature(services, final.goal_id, final.command)
        await emit(
            EventType.SELFCODE,
            op="finish",
            feature_id=feature_id,
            status=final.status.value,
            command=final.command,
            attempts=final.attempts,
            cost_usd=final.cost_usd,
            commit=final.commit,
            error=final.error,
        )
        announcement = _announcement(final)
        if completed_goal is not None:
            announcement += "\n" + t("selfmod.selfcode.goal_done", title=completed_goal.title)
        await notify(Reply(announcement))
        return final


async def _feature_ref(services: Services, feature_id: str, status, error=None) -> None:
    if services.stm is None:
        return
    try:
        await services.stm.update_ref(feature_id, status, error)
    except Exception:
        pass


async def _build(
    services: Services, record: FeatureRecord, request_reload: Reloader, run_tests: TestRunner
) -> dict:
    settings = services.settings
    repo = services.repo
    if repo is None or services.llm is None:
        raise SelfCodeError(t("selfmod.selfcode.errors.not_configured"))
    if services.budget is not None:
        status = await services.budget.check(Scope.SELFCODE)
        if not status.allowed:
            raise SelfCodeError(status.reason)

    # The worker's view of the commands may be stale: reload from disk first.
    load_plugins(services.registry, settings.plugins_dir)
    branch = f"uva/feature-{record.id}"
    workdir = Path(tempfile.mkdtemp(prefix=f"uva-selfcode-{record.id}-"))
    try:
        await repo.arun("clone", "--quiet", "--no-hardlinks", str(repo.path), str(workdir / "w"))
        work = workdir / "w"
        await repo.arun("checkout", "--quiet", "-b", branch, cwd=work)

        # Modules that exist before we start (later attempts may overwrite our own file).
        taken = {p.stem for p in plugin_files(work / PLUGINS_REL)} | {"api", "testing"}
        attempt = Attempt(None, "")
        for number in range(1, settings.selfcode_max_attempts + 1):
            await services.features.store.update(record.id, attempts=number)  # type: ignore[union-attr]
            attempt = await _attempt(services, record, work, taken, attempt.feedback, run_tests)
            await emit(
                EventType.SELFCODE,
                op="attempt",
                feature_id=record.id,
                attempt=number,
                ok=not attempt.feedback,
                plugin=attempt.plugin.name if attempt.plugin else None,
                tests=attempt.tests.summary if attempt.tests else None,
                feedback=attempt.feedback[-1500:],
            )
            if not attempt.feedback:
                break
            if services.budget is not None and not (await services.budget.check(Scope.SELFCODE)).allowed:
                raise SelfCodeError(t("selfmod.selfcode.errors.budget_ran_out"))
        else:
            raise SelfCodeError(t(
                "selfmod.selfcode.errors.not_green", attempts=settings.selfcode_max_attempts,
                problem=attempt.feedback[-600:],
            ))

        plugin = attempt.plugin
        assert plugin is not None and attempt.tests is not None
        sha = await _commit_and_merge(repo, work, branch, record, plugin)
        fields = {
            "plugin": plugin.name,
            "command": plugin.command,
            "summary": plugin.summary,
            "usage": plugin.usage,
            "tests": attempt.tests.summary,
            "commit": sha,
        }

        report = await request_reload()
        if report is not None and plugin.name in report.get("errors", {}):
            error = report["errors"][plugin.name]
            await _revert(repo, sha, request_reload)
            return {**fields, "status": FeatureStatus.REVERTED, "error": t("selfmod.selfcode.errors.load_failed", error=error)}
        if report is None:
            fields["error"] = t("selfmod.selfcode.errors.no_reload")
        return {**fields, "status": FeatureStatus.MERGED}
    finally:
        await asyncio.to_thread(shutil.rmtree, workdir, True)


async def _attempt(
    services: Services,
    record: FeatureRecord,
    work: Path,
    taken_modules: set[str],
    feedback: str,
    run_tests: TestRunner,
) -> Attempt:
    assert services.llm is not None
    try:
        response = await services.llm.complete(
            [LLMMessage("user", _request_prompt(record.description, feedback))],
            purpose="selfcode",
            scope=Scope.SELFCODE,
            model=services.llm.heavy_model,
            system=_system_prompt(services, work),
            json_schema=GENERATED_SCHEMA,
            max_tokens=12000,
            use_soul=False,  # code must come out exact
        )
    except LLMError as exc:
        raise SelfCodeError(t("selfmod.selfcode.errors.llm", error=exc)) from exc

    try:
        plugin = GeneratedPlugin.model_validate_json(_strip_fence(response.text))
    except (ValidationError, ValueError) as exc:
        return Attempt(None, f"A resposta não era o JSON pedido: {exc}")

    problems = (
        codecheck.check_name(plugin.name, taken_modules)
        + codecheck.check_plugin(plugin.plugin_code)
        + codecheck.check_test(plugin.test_code, plugin.name)
    )
    if services.registry.get(plugin.command) is not None:
        problems.append(f"o comando /{plugin.command} já existe; escolha outro nome")
    if problems:
        return Attempt(plugin, "O código foi recusado pela checagem estática:\n- " + "\n- ".join(problems))

    plugin_path = work / PLUGINS_REL / f"{plugin.name}.py"
    test_path = work / TESTS_REL / f"test_{plugin.name}.py"
    test_path.parent.mkdir(parents=True, exist_ok=True)
    plugin_path.write_text(plugin.plugin_code, encoding="utf-8")
    test_path.write_text(plugin.test_code, encoding="utf-8")

    result = await run_tests(work)
    if not result.ok:
        return Attempt(plugin, f"Os testes falharam ({result.summary}):\n{result.output}", result)
    return Attempt(plugin, "", result)


async def _commit_and_merge(
    repo: GitRepo, work: Path, branch: str, record: FeatureRecord, plugin: GeneratedPlugin
) -> str:
    files = [f"{PLUGINS_REL.as_posix()}/{plugin.name}.py", f"{TESTS_REL.as_posix()}/test_{plugin.name}.py"]
    message = (
        f"Self-code: /{plugin.command} ({plugin.name})\n\n"
        f"Pedido: {record.description}\n\n{plugin.summary}\n\n"
        f"{SELF_CODE}: true\nUVA-Feature-Id: {record.id}"
    )
    await repo.arun("add", "--", *files, cwd=work)
    await repo.arun("commit", "-q", "-m", message, cwd=work)

    try:
        current = (await repo.arun("rev-parse", "--abbrev-ref", "HEAD")).strip()
        if current != "main":
            raise SelfCodeError(t("selfmod.selfcode.errors.branch", branch=repr(current)))
        await repo.arun("fetch", "--quiet", str(work), f"{branch}:{branch}")
        try:
            await repo.arun("merge", "--ff-only", "--quiet", branch)
        except GitError:
            # main moved since the clone: a merge commit, marked as bot-made too.
            await repo.arun(
                "merge", "--no-ff", "--quiet", "-m",
                f"Merge self-code /{plugin.command}\n\n{SELF_CODE}: true", branch,
            )
        await repo.arun("branch", "-q", "-D", branch, check=False)
    except GitError as exc:
        await repo.arun("merge", "--abort", check=False)
        raise SelfCodeError(t("selfmod.selfcode.errors.merge", error=exc)) from exc
    return await repo.head()


async def _revert(repo: GitRepo, sha: str, request_reload: Reloader) -> None:
    commit = next((c for c in await repo.history(20) if c.sha == sha), None)
    if commit is None:
        commit = Commit(sha, "self-code", f"{SELF_CODE}: true", 1)
    await repo.revert(commit)
    await request_reload()


def _announcement(record: FeatureRecord) -> str:
    cost = f"US$ {record.cost_usd:.3f}"
    if record.status is FeatureStatus.MERGED:
        text = t(
            "selfmod.selfcode.report.merged", command=record.command, summary=record.summary,
            usage=record.usage, tests=record.tests, attempts=record.attempts, cost=cost,
            commit=record.commit[:7] if record.commit else "?",
        )
        if record.error:
            text += "\n" + t("selfmod.selfcode.report.note", error=record.error)
        return text
    if record.status is FeatureStatus.REVERTED:
        return t("selfmod.selfcode.report.reverted", command=record.command, error=record.error)
    return t(
        "selfmod.selfcode.report.failed", description=record.description[:80],
        attempts=record.attempts, cost=cost, error=record.error,
    )


def _default_runner(services: Services) -> TestRunner:
    timeout = services.settings.selfcode_test_timeout_seconds

    async def runner(work: Path) -> SuiteResult:
        return await asyncio.to_thread(run_pytest, work, timeout)

    return runner


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text


def _system_prompt(services: Services, work: Path) -> str:
    api = (work / PLUGINS_REL / "api.py").read_text(encoding="utf-8")
    example = (work / PLUGINS_REL / f"{EXAMPLE}.py").read_text(encoding="utf-8")
    example_test = (work / TESTS_REL / f"test_{EXAMPLE}.py").read_text(encoding="utf-8")
    taken = sorted({n for c in services.registry for n in c.all_names})
    allowed = ", ".join(sorted(codecheck.PLUGIN_IMPORTS))
    return f"""Você escreve plugins Python 3.12 para a UVA, um bot de Telegram.
Cada plugin é um módulo em src/uva/plugins/<name>.py com testes pytest em
tests/plugins/test_<name>.py. Responda SOMENTE com JSON no formato pedido.

## Interface de plugins (src/uva/plugins/api.py)
```python
{api}
```

## Plugin de exemplo (src/uva/plugins/{EXAMPLE}.py)
```python
{example}
```

## Teste de exemplo (tests/plugins/test_{EXAMPLE}.py)
```python
{example_test}
```

## Regras
- Imports permitidos no plugin: {allowed}.
- Nos testes, além desses: pytest, uva.plugins.testing e uva.plugins.<name>.
- Proibido: open, eval, exec, compile, __import__, getattr/setattr, globals, atributos
  começando com "_", import relativo, os, sys, subprocess, pathlib, socket, config, segurança.
- O plugin não acessa arquivos, processos nem variáveis de ambiente.
- Para LER dados do bot (memórias, objetivos, contexto recente, gastos, notas), use só
  `await ctx.data.<método>(...)` (veja DataView na interface); o handler fica `async def`.
  Não há como gravar dados. Nos testes, passe dados falsos com
  `run(handler, data=StaticData(goals=[...], working_memory=[...]))`
  (import: `from uva.plugins.testing import StaticData, run`).
- O bot fala inglês e português: `ctx.lang` é "en" ou "pt" (o idioma do bot). Todo texto que o
  usuário lê (respostas, mensagens de uso, erros) existe nos dois idiomas, em inglês e em
  português do Brasil, dentro do próprio plugin (ex.: `TEXTS = {{"en": {{...}}, "pt": {{...}}}}`),
  e a resposta segue ctx.lang (`TEXTS.get(ctx.lang, TEXTS["en"])`). Identificadores em inglês.
- A descrição do @command vai como dicionário: `{{"en": "...", "pt": "..."}}`.
- Nos testes, cubra os dois idiomas: `run(handler, ..., lang="en")` e `run(handler, ..., lang="pt")`.
- O campo "summary" do JSON em {language_name()}.
- Valide os argumentos e devolva uma mensagem de uso clara quando estiverem errados.
- Os testes devem ser funções síncronas usando uva.plugins.testing.run, determinísticos
  (para aleatoriedade, teste propriedades/intervalos, não valores exatos) e sem rede.
- Comandos e apelidos já usados (não repita): {", ".join(taken)}.
- "name" (módulo) e "command" podem ser iguais; use nomes curtos em português."""


def _request_prompt(description: str, feedback: str) -> str:
    prompt = f"Funcionalidade pedida pelo Gustavo:\n{description}"
    if feedback:
        prompt += (
            "\n\nSua tentativa anterior não passou. Corrija e mande o JSON completo de novo.\n"
            f"{feedback[-5000:]}"
        )
    return prompt
