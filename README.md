# UVA

Assistente pessoal com IA que conversa pelo Telegram. Atende **um único usuário** (você) e roda em Docker no seu PC.

O plano completo, fase a fase, está em [docs/plano.md](docs/plano.md).

## Estrutura

```
src/uva/
  main.py        # ponto de entrada: monta o bot e inicia o long polling
  config.py      # configurações via .env (pydantic-settings)
  security.py    # filtro de usuário único, roda antes de qualquer handler
  router.py      # texto -> resposta (comandos; fallbacks 2 e 3 marcados como TODO)
  commands/      # registro de comandos + comandos embutidos
  logging/       # logs estruturados em JSON (structlog)
  actions/ llm/ memory/ goals/ brain/ tasks/ storage/   # reservados para fases futuras
tests/           # pytest, sem acessar o Telegram
scripts/windows/ # inicialização automática no Windows
```

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
