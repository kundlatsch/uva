"""Memory actions: the LLM decides what is worth remembering about the user."""

from __future__ import annotations

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.i18n import t
from uva.logging.events import EventType, current_trace_id, emit
from uva.memory.store import MemoryCategory


class MemorySaveParams(BaseModel):
    key: str = Field(min_length=1, description="Chave curta e estável (ex.: cidade, nome_do_gato)")
    value: str = Field(min_length=1, description="O que lembrar")
    category: MemoryCategory = Field(
        default=MemoryCategory.FATO, description="perfil | preferencia | fato | contexto"
    )
    relevance: float = Field(default=0.5, ge=0, le=1, description="Importância de 0 a 1")
    ttl_days: int | None = Field(
        default=None, ge=1, description="Dias até esquecer sozinho (para contexto temporário)"
    )


class MemoryUpdateParams(BaseModel):
    key: str = Field(min_length=1)
    value: str | None = None
    relevance: float | None = Field(default=None, ge=0, le=1)


class MemoryForgetParams(BaseModel):
    key: str = Field(min_length=1)


def no_memory() -> ActionResult:
    return ActionResult(text=t("actions.memory.not_configured"))


def register(registry: ActionRegistry) -> None:
    @registry.action(
        "memory_save",
        "Guarda algo duradouro sobre o usuário (perfil, preferência, fato ou contexto)",
        MemorySaveParams,
        proactive_safe=True,
    )
    async def memory_save(params: MemorySaveParams, ctx: ActionContext) -> ActionResult:
        memory = ctx.services.memory
        if memory is None:
            return no_memory()
        item = await memory.save(
            params.key,
            params.value,
            category=params.category,
            relevance=params.relevance,
            ttl_days=params.ttl_days,
            source=current_trace_id(),
        )
        await emit(EventType.MEMORY, op="save", key=item.key, value=item.value)
        # Silent on purpose: the LLM's own reply acknowledges it.
        return ActionResult(data={"key": item.key})

    @registry.action(
        "memory_update",
        "Atualiza uma memória existente",
        MemoryUpdateParams,
        proactive_safe=True,
    )
    async def memory_update(params: MemoryUpdateParams, ctx: ActionContext) -> ActionResult:
        memory = ctx.services.memory
        if memory is None:
            return no_memory()
        item = await memory.update(params.key, value=params.value, relevance=params.relevance)
        await emit(EventType.MEMORY, op="update", key=params.key, found=item is not None)
        return ActionResult(data={"key": params.key, "found": item is not None})

    @registry.action("memory_forget", "Esquece uma memória", MemoryForgetParams)
    async def memory_forget(params: MemoryForgetParams, ctx: ActionContext) -> ActionResult:
        memory = ctx.services.memory
        if memory is None:
            return no_memory()
        removed = await memory.forget(params.key)
        await emit(EventType.MEMORY, op="forget", key=params.key, removed=removed)
        return ActionResult(data={"key": params.key, "removed": removed})
