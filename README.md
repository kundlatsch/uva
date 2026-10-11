# UVA

Assistente pessoal com IA que conversa pelo Telegram. Atende **um único usuário** (você) e roda em Docker no seu PC.

O plano completo, fase a fase, está em [docs/plano.md](docs/plano.md).

## Estrutura

```
src/uva/
  main.py         # ponto de entrada do bot (long polling)
  bootstrap.py    # monta os serviços (Mongo, Redis, LLM, vault, SQLite) para bot e worker
  config.py       # configurações via .env (pydantic-settings)
  security.py     # filtro de usuário único, roda antes de qualquer handler
  router.py       # texto -> resposta: comando, similaridade ou LLM
  matching.py     # fallback 2 (similaridade com rapidfuzz)
  commands/       # comandos do bot (/help é gerado a partir do registro)
  actions/        # ações que a LLM pode pedir (registro + validação Pydantic)
  llm/            # provedores (Anthropic, OpenAI-compatível), agente, prompts, orçamento
  search/         # backends de pesquisa (Anthropic, Wikipédia, DuckDuckGo)
  brain/          # second brain em Markdown (Obsidian)
  memory/         # memória de longo prazo
  goals/          # objetivos e loop proativo
  tasks/          # Celery: app, tarefas, fila, lock de "ocupado"
  storage/        # Mongo, SQLite (gastos)
  logging/        # logs JSON e eventos com trace_id
tests/            # pytest (Telegram e LLM simulados)
scripts/          # testes de integração e inicialização no Windows
docs/plano.md     # plano por fases
```

## Comandos

| Comando | O que faz |
|---|---|
| `/help` | lista tudo (gerado automaticamente) |
| `/ping`, `/start` | teste e apresentação |
| `/nota <texto>`, `/buscar <termo>` | second brain |
| `/memoria [chave]`, `/lembrar <chave> <valor>`, `/esquecer <chave>` | memória |
| `/objetivos`, `/pausar <id>`, `/retomar <id>`, `/remover <id>` | objetivos do modo proativo |
| `/gasto <valor> <categoria> [descrição]`, `/gastos [hoje\|semana\|mes\|ano\|tudo]` | gastos (SQLite) |
| `/log [período] [tipo]`, `/log trace <id>` | o que aconteceu |
| `/status`, `/llm` | saúde dos serviços, modelos e gastos com API |
| `/tarefas`, `/cancelar <id>` | tarefas em segundo plano |

Fora dos comandos, é só escrever normalmente.

## Configuração

### 1. Criar o bot no Telegram

1. No Telegram, abra uma conversa com **@BotFather**.
2. Envie `/newbot`, escolha um nome e um username (precisa terminar em `bot`).
3. Copie o **token** que ele devolver (formato `123456789:AA...`). Guarde-o em segredo.

### 2. Descobrir o seu user id

O bot só responde ao seu id numérico. Para descobrir:

- Envie qualquer mensagem para **@userinfobot** (ou **@RawDataBot**); ele responde com o seu `Id`.

### 3. Preencher o `.env`

```powershell
Copy-Item .env.example .env
```

Edite o `.env` e preencha pelo menos:

```
TELEGRAM_BOT_TOKEN=123456789:AA...
ALLOWED_TELEGRAM_USER_ID=123456789
```

As demais variáveis são opcionais nesta fase. O `.env` está no `.gitignore` — nunca faça commit dele.

> O bot se recusa a iniciar se `TELEGRAM_BOT_TOKEN` ou `ALLOWED_TELEGRAM_USER_ID` estiverem vazios.

### 4. Subir com Docker

Requer o [Docker Desktop](https://www.docker.com/products/docker-desktop/).

```powershell
docker compose up -d --build
docker compose logs -f bot
```

Abra a conversa com o seu bot no Telegram e envie `/ping` — ele deve responder `pong`.

Os dados do MongoDB ficam no volume nomeado `mongo-data` (sobrevive a `docker compose down`; só é apagado com `docker compose down -v`).

## Inicialização automática no Windows

A ideia: ao fazer logon, o Docker Desktop abre sozinho e um script espera ele ficar pronto para subir a UVA.

1. **Docker Desktop ao entrar:** Docker Desktop → *Settings* → *General* → marque **"Start Docker Desktop when you sign in to your computer"**.
2. **Registrar a tarefa agendada** (uma vez, num PowerShell normal na pasta do repositório):

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\windows\register-autostart.ps1
   ```

   Isso cria a tarefa **"UVA Bot"** no Agendador de Tarefas, que roda `scripts\windows\start-uva.ps1` a cada logon. O script consulta `docker info` até o Docker responder (até 10 min) e então executa `docker compose up -d`.

3. Log da inicialização: `%LOCALAPPDATA%\uva\start-uva.log`.

Para remover: `register-autostart.ps1 -Unregister`. Para testar sem reiniciar: `powershell -ExecutionPolicy Bypass -File scripts\windows\start-uva.ps1`.

Como os serviços usam `restart: unless-stopped`, o Docker também os religa sozinho depois de uma queda — o script só garante que a stack suba mesmo se tiver sido parada.

## Desenvolvimento local

Requer Python 3.12+.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

Para rodar o bot fora do Docker: `python -m uva.main` (lê o `.env` da pasta atual).

### Adicionando um comando

Comandos são funções async registradas no `CommandRegistry` com nome, descrição e apelidos. O `/help` é gerado automaticamente a partir do registro:

```python
@registry.command("hora", "Mostra a hora atual", aliases=("horas",))
async def hora(ctx: CommandContext) -> str:
    return datetime.now().strftime("%H:%M")
```

## Como a UVA entende mensagens

1. **Comando explícito** (`/log 2h`): executado direto, por nome ou apelido.
2. **Similaridade**: texto livre é normalizado (minúsculas, sem acentos e pontuação, sem palavras de enchimento como "me mostra", "por favor") e comparado com nome, apelidos e frases de exemplo de cada comando (rapidfuzz). Nota ≥ `SIMILARITY_EXECUTE_THRESHOLD` (90) executa; ≥ `SIMILARITY_CONFIRM_THRESHOLD` (75) pergunta "Você quis dizer /x?" com botões Sim/Não; abaixo disso segue para o próximo nível. Argumentos simples são extraídos do texto: "log de erros das últimas 2 horas" vira `/log 2h erros`.
3. **LLM**: o texto vai para o modelo rápido (`LLM_MODEL_ROUTER`), que responde **sempre** com um JSON `{"actions": [...], "reply": "..."}`. As ações possíveis ficam num registro (`uva/actions`): funções Python com nome, descrição e schema Pydantic. A resposta é validada; JSON inválido ou ação desconhecida não executa nada (vai para `/log erros`). A LLM nunca roda código livre.

Cada decisão fica no log com a nota, então dá para ajustar os limiares olhando `/log`.

## LLM, pesquisa e orçamento

- **Provedores**: `LLM_PROVIDER=anthropic` (padrão; modelos `claude-haiku-5-5` e `claude-opus-5-5` se não configurar) ou `openai_compatible` (OpenAI, Ollama, OpenRouter... com `LLM_BASE_URL`).
- **Ações iniciais**: `reply`, `run_command` (executa um comando do bot) e `search`.
- **Pesquisa**: com Anthropic usa a busca na web do próprio Claude; com outros provedores usa Wikipédia + DuckDuckGo (`SEARCH_BACKEND` troca). "Pesquisa sobre X na Wikipédia" força a Wikipédia. O resumo é feito pelo modelo pesado (`LLM_MODEL_HEAVY`) e vem com as fontes.
- **Custo**: cada chamada registra modelo, tokens, custo estimado e duração (`/log 1d llm`). A tabela de preços já conhece os modelos Anthropic; para outros, configure `LLM_PRICES` (US$ por 1M tokens). Sem preço, usa `LLM_DEFAULT_PRICE_*` (pessimista).
- **Orçamento**: `LLM_DAILY_BUDGET_USD` e `LLM_MONTHLY_BUDGET_USD` (conversa) e `PROACTIVE_DAILY_BUDGET_USD` (modo proativo). Ao bater o limite, o fallback 3 desliga até virar o dia/mês e o bot avisa; os comandos continuam funcionando.
- `/llm` mostra provedor, modelos, backends de pesquisa e gastos.
- As últimas `LLM_HISTORY_MESSAGES` mensagens da conversa vão como contexto (ficam só na memória do processo).

## Tarefas em segundo plano

O compose sobe também `redis`, `worker` (Celery) e `beat` (agendador). Ações demoradas, como `search`, viram tarefas: o bot responde na hora com "🔎 Pesquisando… (tarefa abc123)" e o worker manda o resultado quando terminar. Enquanto isso o bot continua respondendo normalmente.

- `/tarefas` — tarefas recentes e o estado de cada uma
- `/cancelar <id>` — cancela uma tarefa na fila ou rodando
- Falhas temporárias (rede, API) são tentadas de novo com espera crescente (`TASK_MAX_RETRIES`).
- O Redis guarda um "ocupado" enquanto você conversa e enquanto há tarefas rodando; o modo proativo (fase 8) só age quando nada está marcado.
- O beat dispara um heartbeat a cada minuto; `/status` mostra 🔴 em "Worker + agendador" se ele parar de chegar.

## Second brain (notas em Markdown)

A UVA mantém um cofre de notas compatível com o **Obsidian**, na pasta `brain/` do repositório (fora do git). Para usar outra pasta, defina `SECOND_BRAIN_HOST_PATH` no `.env` (ex.: `D:/Obsidian/UVA`) e rode `docker compose up -d`. Na primeira subida ela cria a estrutura PARA e um `LEIA-ME.md`:

```
00-inbox/  01-projetos/  02-areas/  03-recursos/  04-arquivo/  diario/  LEIA-ME.md
```

- Notas têm cabeçalho YAML (`title`, `created`, `tags`, `source: uva-bot`) e ligações `[[...]]`; o nome do arquivo é o título.
- `/nota <texto>` salva no inbox; `/buscar <termo>` procura em títulos e conteúdo (sem diferenciar acentos).
- Toda pesquisa vira uma nota em `03-recursos/` ("Pesquisa - ...") com as fontes, e cada nota criada é registrada no diário do dia.
- A LLM também pode usar as ações `note_create`, `note_append`, `note_search` e `note_read`.
- A UVA **nunca apaga nem sobrescreve** notas: só cria e acrescenta.

Para abrir no Obsidian: *Open folder as vault* → escolha a pasta `brain`.

## Memória

A UVA guarda o que vale a pena lembrar sobre você na coleção `memory` do Mongo: chave, valor, categoria (`perfil`, `preferencia`, `fato`, `contexto`), relevância (0 a 1), datas de criação e último uso, contagem de uso e validade opcional.

- A LLM decide sozinha quando guardar (ações `memory_save`, `memory_update`, `memory_forget`). Exemplo: "meu gato se chama Mingau" vira `nome_do_gato = Mingau`.
- **Memória de trabalho**: cada item tem uma nota = relevância × recência × frequência. Os `MEMORY_WORKING_SIZE` melhores vão no prompt de toda conversa. A lista é recalculada na subida, uma vez por dia e sempre que a memória muda.
- **Limpeza diária** (beat, `MEMORY_MAINTENANCE_HOUR`): itens com validade vencida somem (índice TTL); itens com nota baixa e sem uso há `MEMORY_ARCHIVE_AFTER_DAYS` dias vão para `memory_archive`.
- `/memoria` lista tudo (★ = memória de trabalho), `/memoria <chave>` mostra um item, `/lembrar <chave> <valor>` guarda, `/esquecer <chave>` apaga.

## Objetivos e modo proativo

Você pode pedir em linguagem natural: *"cria um objetivo de acompanhar notícias sobre energia solar toda semana"*. A LLM usa a ação `goal_create` (título, descrição, `recorrente` ou `unico`, prioridade 1–5, intervalo mínimo em horas) e o objetivo vai para a coleção `goals`.

A cada `PROACTIVE_INTERVAL_MINUTES` o beat dispara o loop proativo:

1. pega o objetivo ativo, vencido, de maior prioridade (se não houver, não faz nada);
2. se o bot estiver ocupado (você conversando ou tarefas rodando), pula a vez;
3. se o orçamento proativo do dia acabou, pula a vez;
4. roda até `PROACTIVE_MAX_STEPS` rodadas com o modelo pesado, usando **só ações seguras** (pesquisa, notas e memória — nada de comandos, nem criar objetivos, nem esquecer memórias), com limite de `PROACTIVE_MAX_TOKENS_PER_RUN` tokens;
5. escreve o resultado em `01-projetos/Objetivo - <título>.md`, registra no diário e reagenda (objetivos `unico` são concluídos quando a LLM diz que terminou).

O modo proativo **nunca manda mensagem no Telegram**: você vê o que ele fez nas notas, em `/objetivos` e em `/log 1d goals`.

- `/objetivos` — lista, com próxima execução e último resultado
- `/pausar <id>`, `/retomar <id>`, `/remover <id>` (as notas ficam)

## SQLite (dados estruturados)

Para dados com formato fixo há um SQLite em `SQLITE_PATH` (volume `sqlite-data`), em modo WAL. As migrações ficam em `src/uva/storage/sqlite.py` (lista `MIGRATIONS`; o número aplicado fica em `PRAGMA user_version`). Para mudar o esquema, acrescente uma migração nova no fim, nunca edite as antigas.

Exemplo incluído: gastos.
- `/gasto 25,90 mercado pão e leite` grava (aceita `25`, `25,90`, `R$ 1.234,56`)
- `/gastos mes` soma por categoria (também `hoje`, `semana`, `ano`, `tudo`)
- Em texto livre também funciona: "gastei 30 no mercado" (ação `expense_add`)

## Log e diagnóstico

Cada mensagem recebe um *trace id*, e cada passo (rota escolhida, comando, ação, chamada à LLM, tarefa, erro) vira um evento na coleção `events` do Mongo (apagado após `EVENTS_TTL_DAYS`, padrão 90) e no console em JSON.

- `/log` — última hora, uma linha por mensagem mostrando o caminho que ela fez
- `/log 30m`, `/log 6h`, `/log 2d`, `/log hoje`, `/log ontem` — outros períodos
- `/log 1d llm` — filtra por tipo: `llm`, `goals`, `erros`, `tarefas`, `memoria`, `acoes`
- `/log trace <id>` — todos os eventos de uma mensagem, com os dados completos
- `/status` — serviços no ar, uptime, fila e gastos

Respostas longas chegam como arquivo `.md`.

Testes de integração (Mongo/Redis reais) rodam num container descartável na rede do compose:

```bash
sh scripts/run-integration-tests.sh
```

## Segurança

- Um filtro global (`security.py`) roda antes de qualquer handler e só deixa passar mensagens do `ALLOWED_TELEGRAM_USER_ID` em **chat privado**.
- Mensagens de outras pessoas ou de grupos são ignoradas em silêncio e registradas no log (`unauthorized_update_ignored`).
