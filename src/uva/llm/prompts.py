"""System prompts (in Portuguese, since the user talks to the bot in Portuguese)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import tzinfo

from uva.actions.registry import Action
from uva.clock import format_local, utcnow
from uva.commands.registry import CommandRegistry

WEEKDAYS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")

PLAN_FORMAT = '{"actions": [{"type": "<nome da ação>", "params": {...}}], "reply": "<texto>"}'


def describe_actions(actions: Iterable[Action]) -> str:
    lines = []
    for action in actions:
        schema = action.params_model.model_json_schema()
        params = json.dumps(schema.get("properties", {}), ensure_ascii=False)
        required = schema.get("required", [])
        lines.append(f"- {action.name}: {action.description}\n  params: {params}; obrigatórios: {required}")
    return "\n".join(lines)


def describe_commands(registry: CommandRegistry) -> str:
    return "\n".join(
        f"- /{c.name} {c.usage}".rstrip() + f": {c.description}" for c in registry
    )


def assistant_system_prompt(
    *,
    actions: Iterable[Action],
    registry: CommandRegistry,
    tz: tzinfo,
    working_memory: str = "",
) -> str:
    now_local = utcnow().astimezone(tz)
    now = f"{WEEKDAYS[now_local.weekday()]}, {format_local(now_local, tz, '%d/%m/%Y %H:%M')}"
    memory = f"\nO que você sabe sobre o usuário (memória):\n{working_memory}\n" if working_memory else ""
    return f"""Você é a UVA, assistente pessoal de um único usuário, conversando pelo Telegram.
Responda sempre em português do Brasil, de forma direta e cordial.

Responda SEMPRE e SOMENTE com um objeto JSON neste formato:
{PLAN_FORMAT}

Ações disponíveis (use só estas, com os params no formato indicado):
{describe_actions(actions)}

Comandos do bot (podem ser executados com a ação run_command):
{describe_commands(registry)}

Regras:
- Se você sabe responder, responda direto em "reply" com "actions": [].
- Use search quando o usuário pedir uma pesquisa ou quando a resposta depender de
  informação atual. Se ele pedir Wikipédia, use "source": "wikipedia".
- Quando uma ação vai produzir o resultado (ex.: search), deixe "reply" curto
  (ex.: "Vou pesquisar sobre X.") — o resultado é anexado depois. Nunca invente
  o resultado de uma ação.
- Use run_command só quando o usuário claramente quiser algo que um comando faz.
- Não repita o texto de "reply" numa ação reply.
- Quando o usuário contar algo duradouro sobre si (nome, cidade, preferências,
  rotina, pessoas próximas), guarde com memory_save. Use uma chave curta e estável;
  se a chave já existe na memória abaixo, use memory_update. Não guarde coisas triviais.
{memory}
Agora são {now}."""


def summary_system_prompt() -> str:
    return (
        "Você resume resultados de pesquisa para um assistente pessoal. Escreva em "
        "português do Brasil um resumo claro e objetivo (até ~200 palavras), usando apenas "
        "as informações dos resultados fornecidos. Não liste as fontes no texto; elas são "
        "anexadas separadamente. Se os resultados não responderem à pergunta, diga isso."
    )
