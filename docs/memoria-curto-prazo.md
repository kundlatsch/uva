# Memória de curto prazo (Redis)

## Problema

O bot mandava as últimas 10 mensagens para a LLM, mas não sabia o que ele mesmo tinha feito (tarefas disparadas, respostas, avisos). Por isso "e aí, como tá aquela task?" não tinha referência.

## Desenho

Uma sessão por usuário no Redis, na chave `stm:<user_id>`, compartilhada pelo bot e pelo worker.

1. **Conversa recente**: os últimos N turnos (mensagem do usuário, resposta do bot e avisos proativos enviados), com horário. Saídas de comandos ficam resumidas (ex.: `/log 1h → 12 eventos`). Substitui o antigo histórico das últimas 10 mensagens.
2. **Referências recentes**: lista curta de tudo que o bot disparou ou criou:
   `{ref: "t3", tipo: tarefa|pesquisa|nota|objetivo|feature|gasto, id, rotulo, status: na fila|rodando|ok|falhou, erro?, quando}`.
   O worker atualiza o status quando uma tarefa termina (no mesmo caminho das notificações).
3. **Foco atual**: a última referência mencionada ou criada, para resolver "aquela", "ela", "de novo".

## Uso

- O prompt da LLM recebe um bloco "Contexto recente" (turnos recentes + referências com status), além do soul e da memória de longo prazo.
- Novas ações da LLM: `task_status(ref)` (estado real no Celery/Mongo), `retry(ref)` (roda de novo uma tarefa que falhou) e `cancel(ref)`.
- Caminho sem LLM: frases como "como tá a tarefa?", "e a pesquisa?" ou "deu certo?" são respondidas direto com o status da referência em foco, usando as frases prontas quando cabe.
- `/contexto` mostra o que o bot lembra agora; `/contexto limpar` apaga.

## Validade e limites

- `STM_TTL_HOURS=12`. Referências ainda rodando não expiram até terminar.
- `STM_MAX_TURNS=30`, `STM_MAX_REFS=15`.

## Promoção

Quando a sessão fica parada por `STM_PROMOTE_IDLE_HOURS` (padrão 3), uma tarefa do beat pergunta ao modelo barato se algo deve ir para a memória de longo prazo (`memory_save`) e então descarta a sessão. Respeita os orçamentos.

## Testes

Corte do buffer e TTL, atualização de status das referências pelo worker, resolução do foco, bloco do prompt, resposta de status sem LLM e tarefa de promoção com LLM simulada. Um teste real: disparar uma pesquisa e depois perguntar "como tá aquela pesquisa?".
