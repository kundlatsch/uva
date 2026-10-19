# UVA: full documentation

UVA is a personal AI assistant that talks to you on Telegram. It serves **a single user** (you) and runs in Docker on your own PC.

This is the complete reference. For a quick overview, see the [README](../README.md). The original phase-by-phase plan (in Portuguese) is in [plano.md](plano.md). How the memory layers fit together is described in [MEMORY_ARCHITECTURE.md](MEMORY_ARCHITECTURE.md).

## Contents

- [Project layout](#project-layout)
- [Commands](#commands)
- [Setup](#setup)
- [Automatic startup on Windows](#automatic-startup-on-windows)
- [Local development](#local-development)
- [How UVA understands messages](#how-uva-understands-messages)
- [LLM, search and budget](#llm-search-and-budget)
- [Background tasks](#background-tasks)
- [Second brain (Markdown notes)](#second-brain-markdown-notes)
- [Short-term memory](#short-term-memory)
- [Long-term memory](#long-term-memory)
- [Goals and proactive mode](#goals-and-proactive-mode)
- [Routines (recurring tasks)](#routines-recurring-tasks)
- [SQLite (structured data)](#sqlite-structured-data)
- [Personality (soul.md) and profile (profile.md)](#personality-soulmd-and-profile-profilemd)
- [Self-editing](#self-editing)
- [Self-coding: UVA writes new commands](#self-coding-uva-writes-new-commands)
- [New projects with Claude Code (/projeto)](#new-projects-with-claude-code-projeto)
- [UVA's own folder: /uva (/sh and /py)](#uvas-own-folder-uva-sh-and-py)
- [Stickers](#stickers)
- [Audio](#audio)
- [Images](#images)
- [Decisions (OpenAI Decisions)](#decisions-openai-decisions)
- [Ready-made phrases](#ready-made-phrases)
- [Local dashboard](#local-dashboard)
- [Instant feedback](#instant-feedback)
- [Language and formatting](#language-and-formatting)
- [Logs and diagnostics](#logs-and-diagnostics)
- [Security](#security)

## Project layout

```
src/uva/
  main.py         # bot entry point (long polling)
  bootstrap.py    # builds the services (Mongo, Redis, LLM, vault, SQLite) for bot and worker
  config.py       # settings from .env (pydantic-settings)
  i18n.py         # translations: t("key") in the language set by BOT_LANGUAGE
  locales/        # en/ and pt/ JSON files with every text the user reads
  security.py     # single-user filter, runs before any handler
  router.py       # text -> reply: command, similarity or LLM
  matching.py     # fallback tier 2 (similarity with rapidfuzz), small talk
  recall.py       # searches every store before calling the LLM
  commands/       # bot commands (/help is generated from the registry)
  actions/        # actions the LLM may request (registry + Pydantic validation)
  llm/            # providers (Anthropic, OpenAI-compatible), agent, prompts, budget, pricing
  search/         # search backends (Anthropic, Wikipedia, DuckDuckGo)
  brain/          # second brain in Markdown (Obsidian)
  memory/         # long-term memory
  stm/            # short-term memory (Redis session)
  goals/          # goals and the proactive loop
  initiatives.py  # messages UVA sends on its own
  routines.py     # recurring requests on a schedule
  tasks/          # Celery: app, tasks, queue, "busy" lock
  storage/        # Mongo, SQLite (expenses, stickers)
  selfmod/        # self-editing, self-coding, plugin loading, git
  plugins/        # commands added as plugins (by hand or by self-coding)
  projects/       # /projeto: new projects built by Claude Code
  shell/          # /sh and /py client for the isolated shell container
  dashboard/      # local web dashboard (FastAPI)
  logging/        # JSON logs and events with trace_id
data/             # soul.md, profile.md (gitignored), fallback_phrases.json (pt) and .en.json
tests/            # pytest (Telegram and LLM are faked)
scripts/          # integration tests, latency and ask tools, Windows startup
docs/             # this documentation, the original plan, short-term memory notes
```

## Commands

The bot replies in the language set by `BOT_LANGUAGE` (English by default; see [Language](#language-and-formatting)), and **every command answers to a Portuguese and an English name** whatever the language. Common subcommand words also work in both languages (`/log hoje` or `/log today`, `/gastos mes` or `/expenses month`). `/help` (or `/ajuda`) lists everything with its aliases, generated from the registry, and `/comandos` (or `/commands`) gives the full debug catalog.

| Command (PT / EN) | What it does |
|---|---|
| `/start`, `/inicio` | introduction |
| `/help`, `/ajuda` | lists every command (generated automatically) |
| `/comandos`, `/commands` | debug catalog: commands (core/plugin, aliases, examples) and LLM actions with their parameters |
| `/ping` | health check, replies `pong` |
| `/brain`, `/cerebro` `[folder \| note]` | inspects the second brain (also `/ver`, `/abrir`, `/view`, `/open`); `/brain renomear` (`rename`) |
| `/nota`, `/note` `<text>` | saves a quick note to the inbox |
| `/buscar`, `/search` `<term>` | searches the second brain (search index) |
| `/reindexar`, `/reindex` | rebuilds the search index (notes, memory, goals, conversations) |
| `/memoria`, `/memory` `[key]` | shows long-term memory |
| `/lembrar`, `/remember` `<key> <value>` | saves a memory |
| `/esquecer`, `/forget` `<key>` | deletes a memory |
| `/contexto`, `/context` `[limpar\|clear]` | short-term memory of the current conversation |
| `/historico`, `/history` `<term>` | searches every past conversation (the conversation log) |
| `/objetivos`, `/goals` | goals UVA works on by itself |
| `/pausar`, `/pause` `<id>` · `/retomar`, `/resume` `<id>` · `/remover`, `/remove` `<id>` | manage a goal |
| `/proativo`, `/proactive` `[on\|off\|agora\|now]` | proactive mode status and controls |
| `/rotina`, `/routine` `<when> <request>` | creates a recurring task; `/rotina <id> pausar\|retomar\|remover\|agora` (or `pause\|resume\|delete\|now`); `/rotina <id> habilidade <name>\|nenhuma` (`skill`) |
| `/rotinas`, `/routines` | lists routines |
| `/gasto`, `/expense` `<amount> <category> [description]` | records an expense |
| `/gastos`, `/expenses` `[hoje\|semana\|mes\|ano\|tudo]` | expenses by category (also `today\|week\|month\|year\|all`) |
| `/decidir`, `/decide` `...` | probability, choice or score (OpenAI Decisions) |
| `/log`, `/eventos` `[period] [type]` · `/log trace <id>` | what happened |
| `/status`, `/estado` | services, uptime, queue and spending |
| `/llm`, `/modelo`, `/model` | provider, models and LLM spending |
| `/liberar`, `/unlock` `[amount in US$]` | extra LLM budget for today |
| `/tarefas`, `/tasks` | recent background tasks |
| `/cancelar`, `/cancel` `<id>` | cancels a task or project |
| `/frases`, `/phrases` `[category]` | ready-made phrases |
| `/skills`, `/habilidades` | skills UVA has, with uses and last use |
| `/skill`, `/habilidade` `<name> [request]` | shows a skill, or does a request following it |
| `/soul`, `/alma` | shows the personality (soul.md) |
| `/perfil`, `/profile` `[consolidar\|consolidate]` | size of profile.md and soul.md against their limits; `consolidar` shrinks the profile (keeps a full copy) |
| `/reverter`, `/undo` `[brain]` | reverts the bot's last change (also `/revert`, `/desfazer`) |
| `/feature`, `/funcionalidade` `<description>` | asks UVA to code a new command |
| `/features`, `/funcionalidades` | self-coding attempts |
| `/projeto`, `/project` `<description>` | creates a new project with Claude Code |
| `/projetos`, `/projects` | lists projects |
| `/sh`, `/terminal` `<command> \| desfazer \| historico` | runs a Linux command in `/uva` (also `undo`, `history`) |
| `/py`, `/python` `<code>` | runs Python in `/uva` |
| `/figurinhas`, `/stickers` | stickers UVA knows |
| `/figurinha`, `/sticker` `<n> ...` | edits a sticker |

Plugins (in `src/uva/plugins/`, shown as `[plugin]` in `/comandos`): `/dado` (`/dice`), `/moeda` (`/coin`), `/maiusculo` (`/uppercase`), `/palindromo` (`/palindrome`), `/painel` (`/dashboard`), `/pokemon` (`/pokedex`), `/baixar_video` (`/download_video`).

Outside commands, just write normally.

## Setup

### 1. Create the Telegram bot

1. In Telegram, open a chat with **@BotFather**.
2. Send `/newbot` and pick a name and a username (it must end in `bot`).
3. Copy the **token** it returns (format `123456789:AA...`). Keep it secret.

### 2. Find your user id

The bot only answers your numeric id. To find it, send any message to **@userinfobot** (or **@RawDataBot**); it replies with your `Id`.

### 3. Fill in `.env`

```powershell
Copy-Item .env.example .env
```

Edit `.env` and set at least:

```
TELEGRAM_BOT_TOKEN=123456789:AA...
ALLOWED_TELEGRAM_USER_ID=123456789
```

Everything else is optional and has a default (see `.env.example` and `src/uva/config.py`). The bot speaks English by default; set `BOT_LANGUAGE=pt` for Portuguese. Without `LLM_API_KEY`, commands and similarity matching still work, but free text is not sent to an LLM. `.env` is in `.gitignore`: never commit it.

> The bot refuses to start if `TELEGRAM_BOT_TOKEN` or `ALLOWED_TELEGRAM_USER_ID` is empty.

### 4. Start with Docker

Requires [Docker Desktop](https://www.docker.com/products/docker-desktop/).

```powershell
docker compose up -d --build
docker compose logs -f bot
```

Open the chat with your bot in Telegram and send `/ping`; it should reply `pong`.

The compose file starts `bot`, `worker`, `beat`, `dashboard`, `projects`, `shell`, `mongo` and `redis`. Data lives in named volumes (`mongo-data`, `redis-data`, `sqlite-data`, `uva-home`, `uva-snapshots`), which survive `docker compose down` and are only deleted by `docker compose down -v`.

## Automatic startup on Windows

At logon, Docker Desktop opens by itself and a script waits for it to be ready before starting UVA.

1. **Docker Desktop at sign-in:** Docker Desktop → *Settings* → *General* → check **"Start Docker Desktop when you sign in to your computer"**.
2. **Register the scheduled task** (once, in a normal PowerShell in the repository folder):

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\windows\register-autostart.ps1
   ```

   This creates the **"UVA Bot"** task in Task Scheduler, which runs `scripts\windows\start-uva.ps1` at every logon. The script polls `docker info` until Docker answers (up to 10 min) and then runs `docker compose up -d`.

3. Startup log: `%LOCALAPPDATA%\uva\start-uva.log`.

To remove it: `register-autostart.ps1 -Unregister`. To test without rebooting: `powershell -ExecutionPolicy Bypass -File scripts\windows\start-uva.ps1`.

Since the services use `restart: unless-stopped`, Docker also restarts them after a crash; the script only makes sure the stack comes up even if it was stopped.

## Local development

Requires Python 3.12+.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

To run the bot outside Docker: `python -m uva.main` (reads `.env` from the current folder). Some tests read settings from the environment, so run the suite without your real secrets loaded, as self-coding does.

### Adding a command

Commands are async functions registered in the `CommandRegistry` with a name, a description and aliases. `/help` and `/comandos` are generated from the registry. Give each command a Portuguese and an English name, and put every text the user reads in the locale files (`src/uva/locales/en/<namespace>.json` and `pt/<namespace>.json`, see [Language](#language-and-formatting)):

```python
from uva.i18n import t

@registry.command("hora", t("commands.time.hora.description"), aliases=("time", "horas"))
async def hora(ctx: CommandContext) -> str:
    return t("commands.time.now", time=datetime.now().strftime("%H:%M"))
```

Names and aliases must be unique; the registry refuses duplicates. Simple commands can also be plugins (see [Self-coding](#self-coding-uva-writes-new-commands)).

## How UVA understands messages

1. **Explicit command** (`/log 2h`): runs directly, by name or alias.
2. **Small talk and status questions**: "oi", "valeu", "falou" are answered from the phrases file; "como tá aquela pesquisa?" is answered with the real status of the item in focus. No LLM involved.
3. **Similarity**: free text is normalized (lowercase, no accents or punctuation, no filler words such as "me mostra" or "por favor") and compared with each command's name, aliases and example phrases (rapidfuzz). A score ≥ `SIMILARITY_EXECUTE_THRESHOLD` (90) runs the command; ≥ `SIMILARITY_CONFIRM_THRESHOLD` (75) asks "Você quis dizer /x?" with Yes/No buttons; below that, it goes to the next tier. Simple arguments are extracted from the text: "log de erros das últimas 2 horas" becomes `/log 2h erros`.
4. **LLM**: the text goes to the fast model (`LLM_MODEL_ROUTER`), which **always** answers with JSON `{"actions": [...], "reply": "..."}`. The possible actions live in a registry (`uva/actions`): Python functions with a name, a description and a Pydantic schema. The answer is validated; invalid JSON or an unknown action runs nothing (and goes to `/log erros`). The LLM never runs free code.

**Before the LLM, UVA searches every store** (`uva/recall.py`, without spending on the LLM): the words of the message are looked up in the search index (long-term memory beyond the working set, and brain notes by title and content; the best 2 notes go with their text, versions in `04-arquivo` are left out), in goals and what they found, and, if the message is about money, in expenses. When the message points to the past ("ontem", "semana passada", "mês passado", "last week", "o que eu te falei sobre..."), the conversation log is searched too, in that period. Matches go into the message's context block (see below), so "me fala sobre o livro X que eu li" is answered in one call, with your note and the research on the author. If it still needs to read something (`recall`, `note_search`, `note_read`), the result goes back to the LLM and the answer comes in the same message (up to 2 extra lookups), instead of "want me to open the note?".

**Prompt layout (cache-friendly).** The system prompt holds only what rarely changes: profile, soul, rules, the action list, the command list and the working memory. What changes on every message (current time, the session's recent context, what recall found) goes in a `[Context for this message]` block at the start of the last user message. The system prompt is therefore byte-identical from one message to the next and the provider's prompt cache covers it. The most used actions are described with their full parameter schema; the rest get one line with their parameter names, and the LLM calls `action_help` (a lookup) to see a full schema before using one. Every action is still accepted. Commands are listed as name + up to two aliases (the usage is in `/help`). Goal runs work the same way: the goal, its previous results and the time go in the first user message.

Every decision is logged with its score, so the thresholds can be tuned by looking at `/log`.

## LLM, search and budget

- **Providers**: `LLM_PROVIDER=anthropic` (default; models `claude-haiku-5-5` and `claude-opus-5-5` if not configured) or `openai_compatible` (OpenAI, Ollama, OpenRouter... with `LLM_BASE_URL`). `LLM_MODEL_ROUTER` is the fast model, `LLM_MODEL_HEAVY` the strong one (summaries, goals, self-coding), `LLM_MODEL_VISION` the one for images.
- **Actions**: `reply`, `run_command` (runs a bot command), `search`, `recall`, notes (`note_create`, `note_append`, `note_update`, `note_search`, `note_read`, `folder_create`), memory (`memory_save`, `memory_update`, `memory_forget`), `goal_create`, `routine_create`, `expense_add`, files (`file_read`, `file_write`, `file_append`), `feature_request`, `project_create`, `shell_run`, `python_run`, `task_status`, `retry`, `cancel`, `decide`, `image_save`, `profile_patch` (replace one piece of the profile, or append), `history_search` (the conversation log), skills (`skill_read`, `skill_create`, `skill_patch`, `skill_write_file`) and `action_help` (full params of an action listed in one line). `/comandos` lists them with their parameters.
- **Search**: with Anthropic it uses Claude's own web search; with other providers it uses Wikipedia + DuckDuckGo (`SEARCH_BACKEND` changes this). "Pesquisa sobre X na Wikipédia" forces Wikipedia. The summary is written by the heavy model and includes the sources.
- **Cost**: every call records model, tokens, estimated cost and duration (`/log 1d llm`). The price table knows the Anthropic models; for others, set `LLM_PRICES` (US$ per 1M tokens). Without a price, `LLM_DEFAULT_PRICE_*` is used (pessimistic). OpenAI cached prompt tokens are billed at 10% of the input price.
- **Budget**: `LLM_DAILY_BUDGET_USD` (1) and `LLM_MONTHLY_BUDGET_USD` (20) for conversation, `PROACTIVE_DAILY_BUDGET_USD` (0.5) for proactive mode. When a limit is hit, the LLM tier turns off until the next day/month and the bot says so; commands keep working.
- `/llm` shows provider, models, search backends and spending.
- `/liberar [amount]` grants extra LLM budget **for today only** (until midnight): `/liberar 2` adds US$ 2; without an amount, one more full daily limit. It can be repeated (it adds up). The monthly limit still applies; `/status` and `/llm` show what was granted.
- The last `LLM_HISTORY_MESSAGES` (10) messages of the conversation go along as context.

## Background tasks

The compose file also starts `redis`, `worker` (Celery) and `beat` (scheduler). Slow actions such as `search` become tasks: the bot answers right away with "🔎 Pesquisando… (tarefa abc123)" and the worker sends the result when done. Meanwhile the bot keeps answering normally.

- `/tarefas` (`/tasks`): recent tasks and their state.
- `/cancelar <id>` (`/cancel`): cancels a queued or running task.
- Temporary failures (network, API) are retried with growing waits (`TASK_MAX_RETRIES`).
- Redis keeps a "busy" flag while you are chatting and while tasks run; proactive mode only acts when nothing is flagged.
- Beat sends a heartbeat every minute; `/status` shows 🔴 on "Worker + agendador" if it stops arriving.

## Second brain (Markdown notes)

UVA keeps a note vault compatible with **Obsidian**, in the repository's `brain/` folder (outside git). To use another folder, set `SECOND_BRAIN_HOST_PATH` in `.env` (e.g. `D:/Obsidian/UVA`) and run `docker compose up -d`. On first start it creates a PARA structure and a `LEIA-ME.md`:

```
00-inbox/  01-projetos/  02-areas/  03-recursos/  04-arquivo/  05-anexos/  diario/  LEIA-ME.md
```

- Notes have a YAML header (`title`, `created`, `tags`, `source: uva-bot`) and `[[...]]` links.
- `/nota <text>` saves to the inbox; `/buscar <term>` searches titles and content (accent-insensitive, every word must be in the note) through the search index.
- **Search index.** Notes, memory items, goals and conversation turns are indexed in SQLite (FTS5, `search_index`, accents ignored, title weighs more than body). Every note UVA writes is indexed right away; notes you edit in Obsidian are picked up within 10 minutes (beat compares modification times) and deleted ones drop out. The index is derived: the files and Mongo stay the source of truth, and `/reindexar` rebuilds it from scratch. It is built automatically on the first start.
- `/brain` shows the folder and note tree; `/brain <folder>` (e.g. `recursos` or `02-areas/Saúde`) lists a folder with size and date; `/brain <title or path>` shows the raw note, YAML header included. Long content is paginated with ◀ / ▶ buttons that change the page in the same message. Read only, `.md` only, and nothing hidden (`.obsidian`, `.trash`...).
- Every search becomes a note in `03-recursos/` ("Pesquisa - ...") with its sources, and every created note is logged in the day's journal (`diario/`).
- The LLM can also use the `note_create`, `note_append`, `note_update`, `note_search`, `note_read` and `folder_create` actions.
- New folder: ask "cria uma pasta de X" and UVA picks the place by PARA (projects, areas, resources, archive) unless you say where, and creates the folder with an empty `.md` of the same name, like an `__init__.py`, so Obsidian shows it (`folder_create`). If you give content, the note goes inside it (`note_create` with `subfolder`). Up to 3 levels; hidden names (`.something`) and `..` are refused; existing folders are left alone.
- UVA **never deletes** notes. To fix or rename a note it uses `note_update`, which first keeps a copy of the previous version in `04-arquivo/Versões/`. Texts with placeholders instead of content (e.g. `<texto anterior mantido aqui>`) are refused.

To open it in Obsidian: *Open folder as vault* → pick the `brain` folder.

### Names in the second brain

The brain is your content, so names stay in Portuguese even though everything internal is in English:

- every action that writes to the brain (`note_create`, `note_update` when it renames, `image_save`, `search`) receives `title_pt`, a Portuguese title;
- the file name is that title in snake_case, without accents: "Marmitas Frias e Requentadas" becomes `marmitas_frias_e_requentadas.md`. The accented title goes in the front matter and the `# heading`, and links are `[[marmitas_frias_e_requentadas|Marmitas Frias e Requentadas]]`. Attachments follow the same pattern;
- if the title is missing or in English, a cheap LLM call suggests a Portuguese title from the note's text;
- `/brain renomear` (`/brain rename`) brings old notes to this pattern and updates every link. The brain has its own git repository, and `/reverter brain` (`/undo brain`) undoes its last change.

## Short-term memory

See [MEMORY_ARCHITECTURE.md](MEMORY_ARCHITECTURE.md) for the technical view of short-term memory, long-term memory, the background review and recall together.

UVA remembers what happened in the current conversation, including what **it** did: a Redis session (`stm:<your id>`), shared by bot and worker, with the last turns (summarized command outputs and messages it sent on its own), references to everything it started or created (searches, tasks, notes, goals, features, expenses) with the status kept up to date by the worker, and which of them is "in focus". Details (in Portuguese) in [memoria-curto-prazo.md](memoria-curto-prazo.md).

- "como tá aquela pesquisa?", "e a tarefa?", "deu certo?" are answered directly, without the LLM, with the real status of the reference in focus.
- The LLM receives a "recent context" block and the `task_status`, `retry` and `cancel` actions (they accept the ref, e.g. `t3`, or "aquela").
- `/contexto` shows the session; `/contexto limpar` (`clear`) wipes it.
- **Conversation log.** Besides the 12h session, every turn (your message, the reply, and every notice UVA sends) is appended to SQLite (`conversation_turns`, never trimmed) with its source (`telegram`, `dashboard`, `notice`, `routine`, `goal`, `initiative`). `/historico <term>` searches it, the LLM has the `history_search` action (term and/or period), and recall uses it for questions about the past.
- Lifetime `STM_TTL_HOURS` (12h), with at most `STM_MAX_TURNS` (30) turns and `STM_MAX_REFS` (15) references; what is still running does not expire. After `STM_PROMOTE_IDLE_HOURS` (3h) idle the session is dropped (the conversation log keeps every turn, and the background review below has already kept what mattered).

## Background review and journal digest

UVA keeps what matters from the conversation on its own, in the background:

- **When**: after `REVIEW_EVERY_TURNS` (8) new messages from you, or after `REVIEW_MIN_TURNS` (4) when you have been quiet for `REVIEW_IDLE_MINUTES` (10). Beat checks every 2 minutes; the worker runs it with the cheap model, under the proactive budget.
- **What it reads**: the new turns of the conversation log since the last review, the keys in working memory, the skill index and the size of the profile.
- **What it may do** (nothing else): `memory_save` / `memory_update` / `memory_forget` for facts about you, `skill_create` / `skill_patch` / `skill_write_file` (and `skill_read`) for how you want a kind of task done, and `profile_patch` for core profile facts within `PROFILE_MAX_CHARS`. Each kind of knowledge goes to one place only; usually it keeps nothing.
- **Safety**: every write is scanned for prompt injection first (see [Security](#security)); a hit is skipped and logged.
- **Telling you**: when something was kept you get one line, e.g. "💾 anotei: memória time, habilidade match-recap". `REVIEW_NOTIFY=off` turns it off, `verbose` adds the reason. The writes are in `/log memoria` and `/log skills`.
- **Journal digest**: every night (at `MEMORY_MAINTENANCE_HOUR`) the day's conversation is summarized, in Portuguese, into a "Resumo do dia" section of that day's journal note (`diario/AAAA-MM-DD.md`), so the episodes can be read in Obsidian too. Quiet days get none.

## Long-term memory

UVA keeps what is worth remembering about you in Mongo's `memory` collection: key, value, category (`perfil`, `preferencia`, `fato`, `contexto`), relevance (0 to 1), creation and last-use dates, use count and optional expiry.

- The LLM decides by itself when to save (`memory_save`, `memory_update`, `memory_forget`). Example: "meu gato se chama Mingau" becomes `nome_do_gato = Mingau`.
- **Working memory**: each item has a score = relevance × recency × frequency. The best `MEMORY_WORKING_SIZE` (20) go into the prompt of every conversation. The list is recomputed at startup, once a day and whenever memory changes, in any process: a Redis counter (`memory:working_version`) tells the bot when the worker changed it.
- **Use counts**: an item counts as used when it is saved or updated, when recall shows it for a message, and when you read it with `/memoria <key>`.
- **Daily cleanup** (beat, `MEMORY_MAINTENANCE_HOUR`): expired items disappear (TTL index); low-scoring items unused for `MEMORY_ARCHIVE_AFTER_DAYS` (90) days move to `memory_archive`.
- `/memoria` lists everything (★ = working memory), `/memoria <key>` shows one item, `/lembrar <key> <value>` saves, `/esquecer <key>` deletes.

## Goals and proactive mode

**Goals.** Ask in natural language ("cria um objetivo de acompanhar notícias de energia solar", "descobre por qual livro o Ricardo Terto foi semifinalista do Jabuti"). The LLM creates them with `goal_create`:

- `unico` (default): a task with an end. It is **completed** when the LLM says it is done, or when the feature it asked for is ready.
- `recorrente`: keep track of something; runs every `min_interval_hours` (default 6h; news 6–12h, prices 2–6h...).

Beat checks every `PROACTIVE_POLL_MINUTES` (5) and runs at most one goal every `PROACTIVE_INTERVAL_MINUTES` (15). Each run uses only safe actions (search, notes, memory), has a limit of rounds (`PROACTIVE_MAX_STEPS`) and tokens (`PROACTIVE_MAX_TOKENS_PER_RUN`), writes to a note titled "Objetivo - <title>" in `01-projetos/` and **always sends a report** on Telegram (`PROACTIVE_NOTIFY=true`). If a goal needs a new command, it asks self-coding for one (`PROACTIVE_ALLOW_SELFCODE`).

**Initiatives.** Besides goals, UVA shows up on its own. At most every `PROACTIVE_DECIDE_EVERY_MINUTES` (40), the cheap model looks at long-term memory, the recent conversation, the brain notes and the goals, and picks:

- `research`: researches a topic you care about and sends the summary (saved in `03-recursos/`);
- `message`: a reminder, a follow-up question ("voltou pro livro do Terto?") or a suggestion;
- `nothing`: when it is not worth interrupting. After `PROACTIVE_FORCE_AFTER_HOURS` (3h) without an initiative, "nothing" is no longer an option.

Limits: at most `PROACTIVE_MAX_MESSAGES_PER_DAY` (8) per day, with `PROACTIVE_MIN_GAP_MINUTES` (60) between them; nothing during quiet hours (`PROACTIVE_QUIET_START_HOUR`–`PROACTIVE_QUIET_END_HOUR`, 23h–8h); nothing while you are chatting; everything within `PROACTIVE_DAILY_BUDGET_USD`.

- `/proativo` (`/proactive`): status (on?, messages today, quiet hours, next initiative, goals, last initiatives).
- `/proativo on` / `/proativo off`: turns initiatives on/off (goals keep running).
- `/proativo agora` (`now`): sends an initiative right away (only the budget applies).
- `/objetivos`, `/pausar <id>`, `/retomar <id>`, `/remover <id>`; `/log 1d goals` and `/log 1d iniciativas`.

## Routines (recurring tasks)

Requests that run by themselves on a schedule, through the normal router (same budget and limits as a typed message). The result arrives on Telegram. The schedule comes first, in English or Portuguese, whatever `BOT_LANGUAGE` is:

- `/routine every day 08:00 check whether skin X is in the LoL store` (also `daily at 9pm`, `everyday 7:30am`)
- `/routine monday and thursday 9am remind me to water the plants` (also `every tuesday, saturday at 18:15`, `on wednesdays 10h`)
- `/routine every 6 hours check the dollar price` (also `every 30 minutes`, `every hour`; at least 15 minutes)
- In Portuguese: `/rotina todo dia 08:00 ...`, `/rotina segunda e quinta 9h ...`, `/rotina a cada 6 horas ...`
- In natural language too ("every day at 8 check whether...", "todo dia às 8 vê se..."): the LLM uses `routine_create`.
- `/rotinas` (`/routines`) lists them; `/rotina <id> pausar|retomar|remover|agora` (or `pause|resume|delete|now`).

**PC turned off.** Beat checks every minute. A routine whose time passed while the PC was off runs **once** when it comes back, however many times it missed, and the message says "late: it was due at 10/10 08:00" ("atrasada: era para ..." in Portuguese). The next run is always the next scheduled time after now. The schedule and the last run live in Mongo (`routines`).

## SQLite (structured data)

Data with a fixed shape lives in SQLite at `SQLITE_PATH` (volume `sqlite-data`), in WAL mode. Migrations are in `src/uva/storage/sqlite.py` (`MIGRATIONS` list; the applied number is in `PRAGMA user_version`). To change the schema, append a new migration at the end; never edit old ones.

Tables: expenses, stickers, the conversation log (`conversation_turns`), the search index (`search_index`, FTS5) and its note state (`index_state`).

Example: expenses.

- `/gasto 25,90 mercado pão e leite` records one (accepts `25`, `25,90`, `R$ 1.234,56`).
- `/gastos mes` sums by category (also `hoje`, `semana`, `ano`, `tudo`, or `today`, `week`, `month`, `year`, `all`).
- Free text works too: "gastei 30 no mercado" (`expense_add`).

## Personality (soul.md) and profile (profile.md)

UVA's personality lives in [data/soul.md](../data/soul.md): ironic, playful, a bit rude, swearing when it fits, in a partner dynamic, but always useful and precise. Exact outputs (JSON, errors, `/log`, command results) stay neutral.

`data/profile.md` is your profile: context about you, compiled from past conversations. It is personal, so it is **gitignored**.

- Both files go at the very start of the system prompt of every LLM call (conversation, search summaries, goals): profile first, then soul, then the rest of the system prompt. Nothing in the system prompt changes per message (the per-message context goes in the user message), so the provider's prompt cache covers all of it.
- **Size budget.** Both files go into every call, so they have a cap: `PROFILE_MAX_CHARS` (4000) and `SOUL_MAX_CHARS` (3000). A self-edit (`file_write` / `file_append`) that would take either file over its cap is refused with the size and the limit, asking to rewrite it shorter instead of growing it. `/perfil` (`/profile`) shows both sizes against their limits (also in `/status` and on the dashboard).
- **Consolidation.** `/perfil consolidar` (`/profile consolidate`) asks the heavy model to rewrite the profile into a core that fits the cap (who you are, people, work, lasting preferences; merging repetitions and dropping what is outdated). The full previous text is kept in `data/.versions/` and in the brain note `02-areas/Gustavo/perfil_detalhado.md`, which is indexed, so recall still finds the details when a message is about them; the new profile ends with a link to that note. If the rewrite doesn't fit, nothing changes. It **never runs on its own**: you trigger it. Until then the current file is loaded as it is (up to 30000 characters).
- Cost of the profile (~19 KB, about 4.3k tokens), measured on the router model: about US$ 0.0045 more per message while the cache is warm (cached tokens are billed at 10%), and about US$ 0.027 more on the first message after the cache expires (a few idle minutes).
- Edit them freely: they are re-read when they change, without restarting anything.
- `/soul` (`/alma`) shows the current personality.

## Skills (how you want things done)

Skills are UVA's procedural memory: how you want a kind of task done (how to record a book you read, where reflective thoughts go, how to deliver research). Facts about you stay in memory and the profile; skills hold procedures.

- Each skill is a folder `data/skills/<name>/` with a `SKILL.md`: YAML front matter (`name`, `description` up to 120 characters, optional `triggers` with example phrases, `version`) and the sections **When to Use**, **Procedure**, **Pitfalls**, **Verification**; long details can go to `references/*.md`. They are written in English (only the LLM reads them).
- The system prompt carries only the **index** (name + description, right after the soul); the body is loaded when needed:
  - the LLM reads it with `skill_read` when a request fits a skill;
  - a message that contains a skill's name or one of its trigger phrases (fuzzy match) gets the skill attached to the request automatically;
  - `/skill <name> <request>` (`/habilidade`) does a request following that skill; `/skill <name>` shows it;
  - a routine or goal can name a skill (`routine_create` / `goal_create` with `skill`, or `/rotina <id> habilidade <name>`), followed on every run.
- UVA writes them itself: when you correct how it did something or say how you want a kind of task done, it creates a skill (`skill_create`) or fixes the existing one in place (`skill_patch`, which needs a `skill_read` of the same skill first). The prompt asks for lessons, not logs: no one-off details, no failed attempts as advice, no "tool X is broken".
- Every change is a commit with `UVA-Self-Edit`, so `/reverter` undoes it. Usage (uses, last use) is counted in SQLite (`skill_usage`); `/skills` (`/habilidades`) lists them.
- Three skills come with the repository: `reading-notes`, `reflective-thoughts` and `research-summary`.

## Self-editing

UVA can read and change some of its own files through the `file_read`, `file_write` and `file_append` actions, **only** in these paths:

- `data/...` (e.g. `data/soul.md`): read, rewrite and append. Each change becomes a commit in the repository (author `GIT_AUTHOR_NAME`, trailer `UVA-Self-Edit: true`) containing only that file. `data/profile.md` is not in git, so instead of a commit the previous version is copied to `data/.versions/`.
- `brain/...` (second brain): read, create new notes and append; existing notes are never overwritten.

Any other path (`src/`, `security.py`, `.env`, `docker-compose.yml`, hidden files, `..`) is refused.

`/reverter` (`/undo`) undoes the last commit made by the bot (only its own, never yours) with a revert commit tagged `UVA-Reverts: <sha>`; running it again undoes the previous one. Changes show up in `/log 1d selfcode`.

## Self-coding: UVA writes new commands

Ask with `/feature <description>` (e.g. `/feature /dado rola um dado de 6 lados`) or in free text ("cria um comando que..."; `feature_request` action). A worker task then:

1. clones the mounted repository into a temporary folder, on a `uva/feature-<id>` branch;
2. asks the heavy model for a **plugin** (`src/uva/plugins/<name>.py`) and its tests (`tests/plugins/test_<name>.py`), following the interface documented in [src/uva/plugins/api.py](../src/uva/plugins/api.py) (example: [moeda.py](../src/uva/plugins/moeda.py));
3. refuses, without running it, code that imports anything outside an allow list (no `os`, `subprocess`, `pathlib`, config, security), uses `open`/`eval`/`exec`/`getattr`, private attributes, etc.;
4. runs **the whole** pytest suite in a process without the secret variables; on failure it gives the output back to the LLM and tries again (up to `SELFCODE_MAX_ATTEMPTS`, 3);
5. with everything green, commits (`UVA-Self-Code: true`), merges into `main` and asks the bot (via Redis) to reload plugins without restarting; if the plugin does not load in the bot, it reverts by itself;
6. reports on Telegram what it created, how to use it, how many attempts and the cost, or why it failed.

- `/features` lists the attempts; `/reverter` also undoes self-coding commits (and reloads the plugins).
- Its own budget: `SELFCODE_DAILY_BUDGET_USD` (2; does not use the general daily budget, but counts toward the monthly one).
- Plugins are bilingual: the description is a dict `{"en": "...", "pt": "..."}` and replies follow `ctx.lang` (`"en"` or `"pt"`). Plugins can't import `uva.i18n`, so each one keeps its own small translation table.
- Plugins can also be written by hand: create the file in `src/uva/plugins/`, the tests in `tests/plugins/` and restart the bot (or use `/reverter`/`/feature` to trigger a reload). They show up as `[plugin]` in `/comandos`.
- Requires the repository on the `main` branch; your uncommitted changes are not part of the clone.

## New projects with Claude Code (/projeto)

UVA creates **new software projects, separate from itself**:

- `/projeto <description>` (`/project`, or "cria um projeto novo de ..." in natural language): creates the folder `PROJECTS_HOST_PATH/<name>` (default `../uva-projects`, next to this repository), runs `git init` with the author `GIT_AUTHOR_NAME <GIT_AUTHOR_EMAIL>` and runs Claude Code non-interactively (`claude -p`). It writes the code, the README and the tests, runs the tests and commits. At the end UVA sends the summary, path, cost, turns and commits on Telegram, or the reason it failed.
- `/projetos` (`/projects`): list with status (queued, building, done, failed, cancelled).
- `/cancelar <id>`: stops a queued or running project.
- `/log 1d projetos`: history.

**New projects only.** Requests to clone, download or run third-party repositories are refused, because that code would run next to Claude Code's token. Claude Code's git also has no `clone`, `fetch`, `pull`, `submodule` or `remote`. It works in English (code, README, commits) and sends a short summary in the bot language.

One project at a time. Limits: `PROJECT_TIMEOUT_MINUTES` (30), `PROJECT_MAX_TURNS` (80) and `PROJECT_MAX_BUDGET_USD` (3) per project; `PROJECT_DAILY_BUDGET_USD` (10) per day.

**Credential.** Put **one** in `.env`: `CLAUDE_CODE_OAUTH_TOKEN` (run `claude setup-token` on your PC; uses your Claude subscription) or `PROJECTS_ANTHROPIC_API_KEY` (billed separately). Then run `docker compose up -d`.

**Permissions (closed list).** Claude Code runs in `dontAsk` mode with `--allowedTools`. It can read and edit files only inside the project folder (`./**`), but not its `.claude` folder. In the shell it can only run `git`, `python`/`python3`, `pip`, `node`, `npm`, `npx` and `pytest`. Anything else is refused without asking. It never runs with full permissions.

**Isolation.** Everything runs in the `projects` container, which mounts **only** the projects folder: nothing from UVA's repository or the second brain. Claude Code runs as the unprivileged user `builder`, with a clean environment (only its credential and the git author), so it cannot read the bot's tokens. Note: python and node, which are on the list, run code, and the container reaches the internal network (Mongo and Redis, without passwords). Ask for projects, not for tasks about the infrastructure.

## UVA's own folder: /uva (/sh and /py)

UVA has a folder of its own, `/uva`, where it runs Linux commands and Python freely. It uses it by itself through the `shell_run` and `python_run` actions ("calcula isso", "baixa e conta as linhas desse CSV"), and you can use it directly too:

- `/sh <command>` (`/terminal`): runs in bash inside `/uva` and returns the output, paginated if long.
- `/py <code>` (`/python`): runs Python 3 (multiple lines allowed). Packages go into a venv at `/uva/.venv`: `/sh pip install requests`.
- `/sh desfazer` (`undo`): restores `/uva` to how it was before the last command that changed files. Repeat to go further back.
- `/sh historico` (`history`): lists the snapshots. `/log 1d shell` shows the history.

**Deleting asks for permission.** Commands that delete or overwrite files (`rm`, `rmdir`, `mv`, `truncate`, `find -delete`, `git clean`, `sed -i`, `>` or `cp` over an existing file; in Python, `os.remove`, `shutil.rmtree`, `open(..., "w")`...) only run after you tap **Sim, rodar** on Telegram. This also applies when the LLM asks. Since no detection is perfect, every command that changes something first takes a snapshot of `/uva` (git, stored out of the command's reach), and `/sh desfazer` restores it. `.venv`, `.cache` and `node_modules` are left out of snapshots.

**Real isolation, not just a prompt rule.** The `shell` container:

- mounts only the `/uva` and `/snapshots` volumes, with no `.env`, repository, brain, projects or `docker.sock`;
- has a read-only filesystem (only `/uva` and `/tmp` are writable);
- runs every command as the `uva` user, without sudo or capabilities (`no-new-privileges`), so it cannot read the main process's environment or the snapshots;
- limits: `SHELL_MEMORY` (1g), `SHELL_CPUS` (1), 256 processes and `SHELL_TIMEOUT_SECONDS` (60 s) per command;
- network: it sits on an internal network (`shellnet`) that only the bot and the worker reach, so it cannot reach Mongo or Redis. It has internet access for pip and curl, which can be cut with `SHELL_NO_INTERNET=true`.

## Stickers

UVA learns what your stickers mean. These are two different things, both stored in SQLite's `stickers` table, keyed by the sticker's `file_unique_id`, along with its emoji and pack:

1. **Meaning.** The first time you send a sticker on its own, it asks "o que ela significa?", and your next message (text or audio) becomes its meaning (`/pular` leaves it empty). After that, the sticker reaches the LLM as `[figurinha 😴: quando eu tô morto de cansaço]`, along with the conversation context, without interpreting the image.
2. **Action.** "toda vez que eu mandar essa figurinha, me deseja boa noite e me lembra de beber água", right after sending the sticker, attaches a request to it. On every send, the request goes through the normal router, as if you had typed it, with the same budget and limits. It can be a command too (`/figurinha 3 acao /log 1h`).

- `/figurinhas` (`/stickers`): list with each one's meaning, action and usage.
- `/figurinha <n> significado <text>`, `/figurinha <n> acao <request>`, `/figurinha <n> sem-acao`, `/figurinha <n> remover` (or `meaning`, `action`, `no-action`, `remove`).

## Audio

Send audio (voice message, audio file or round video) and UVA transcribes it with OpenAI (`TRANSCRIBE_MODEL`, default `gpt-4o-mini-transcribe`, with `whisper-1` as fallback), replies "🎙️ entendi: ..." and treats the text **exactly as if you had typed it**: commands, similarity and LLM.

- As soon as the audio arrives, it says it is transcribing (phrases from the `transcrevendo` category in `data/fallback_phrases.json`).
- ffmpeg converts Telegram audio (OGG/Opus) to mono MP3 before sending it.
- 20 MB limit per file (Telegram's limit for bots).
- Cost per audio minute counts toward the budget and shows in `/log 1d llm` (`kind=transcribe`); the transcript stays in short-term memory as `[voz] ...`.
- The key is `TRANSCRIBE_API_KEY`, or `LLM_API_KEY` when the main provider is already OpenAI. `TRANSCRIBE_LANGUAGE` defaults to `pt`.

## Images

UVA sees images (photos and images sent as files) with the `LLM_MODEL_VISION` model (default: the router model). Works with OpenAI and Anthropic.

- **Photo with caption**: caption and image go to the LLM together ("o que é isso?", "traduz o cardápio"). A caption starting with `/` is still a command (e.g. `/decidir ...` uses the photo).
- **Photo without caption**: it describes it in a few sentences (transcribing any text) and keeps the image for 30 minutes. Follow-ups such as "o que tá escrito aí?" or "traduz isso" show the image to the LLM again.
- **Saving**: "salva essa foto" uses the `image_save` action, which stores the image in the second brain's `05-anexos/` and creates a note with `![[...]]` and the description. Files are never overwritten.
- Image calls count toward the normal budget (`purpose` `plan_vision` / `vision_describe` in `/log 1d llm`).

## Decisions (OpenAI Decisions)

For questions with a numeric, calibrated answer, UVA uses OpenAI's Decisions API (beta, `DECISIONS_MODEL` default `gpt-6-luna`, US$ 0.10 per 1M input tokens, free output). The key is `DECISIONS_API_KEY`, or `LLM_API_KEY` when the main provider is already OpenAI.

- `/decidir <statement>[: text]` → probability that it is true. E.g. `/decidir esse email é spam: Ganhe um iPhone grátis`
- `/decidir <question> | op1, op2, op3 [:: text]` → picks an option, with confidence and probabilities. E.g. `/decidir o que jantar | pizza, sushi, salada :: tô sem grana`
- `/decidir nota <criterion> | level0, level1, ... :: text` → score from 0 to N (levels from lowest to highest). E.g. `/decidir nota urgência | baixa, média, alta :: o servidor caiu`
- A photo captioned `/decidir ...` evaluates the image. A photo without caption is kept for 30 min for questions like "tem gato nessa foto?".
- In free text, the LLM uses the `decide` action (up to 8 questions) for "qual a chance de...", choosing among fixed options or scoring, instead of guessing.
- Every call counts toward the budget and shows in `/log 1d llm` (`kind=decisions`).

## Ready-made phrases

[data/fallback_phrases.json](../data/fallback_phrases.json) (Portuguese) and [data/fallback_phrases.en.json](../data/fallback_phrases.en.json) (English) have 200+ phrases each in the tone of soul.md; the bot uses the one for `BOT_LANGUAGE`. Both have the same 22 categories (`sem_credito`, `llm_erro`, `nao_entendi`, `ocupado`, `tarefa_falhou`, `tarefa_ok`, `pesquisando`, `saudacao`, `agradecimento`, `despedida`, `confirmacao`, `cancelado`, `sem_resultado`, `erro_generico`, `transcrevendo`, `pensando`, `executando`, `programando`, `procurando`, `olhando`, `salvando`, `comando_desconhecido`). Placeholders such as `{id}` and `{termo}` are documented in the file itself.

- Used when the answer does not need the LLM: budget exceeded or provider out of credit (the phrase + the exact reason), LLM error, "didn't understand", unknown command, start of a search, task notices, cancellations, empty searches, and the progress messages described in [Instant feedback](#instant-feedback).
- "oi", "valeu", "falou" and similar get answered from here, without spending on the LLM.
- Random pick without repeating the last 3 of each category; the file is validated and re-read when it changes. If it is missing or broken, built-in neutral phrases are used.
- `/frases` (`/phrases`) lists the categories; `/frases <category>` shows examples. UVA itself can edit the file (`file_write`), and invalid JSON is refused before writing.

## Local dashboard

The `dashboard` service serves a dashboard at **http://localhost:8080**, only on this computer (`127.0.0.1`; change the port with `DASHBOARD_PORT`). For a password, set `DASHBOARD_PASSWORD` (HTTP Basic, any user name). It has:

- **Overview**: services up, queue, events and errors in the last 24h, counts (memories, goals, notes, stickers, projects).
- **Costs and limits**: LLM spending today, this month, proactive, self-coding and projects, each against its limit. Also a per-day chart (split into interactive, proactive and self-coding), totals by purpose and by model, the `/liberar` button and the personal expenses from SQLite.
- **Chat**: a full channel with UVA, through the same pipeline as Telegram (commands, similarity, LLM, actions and the same short-term memory). The Telegram conversation and the goal and initiative notices show up there too. Confirmation buttons work. Next to it, the **debug** panel of each message shows:
  - which path it took (which tier resolved it);
  - the recall;
  - the actions JSON and the result of each action;
  - every LLM call, with the system prompt (rules, actions, memory), the messages sent (the last one starts with the per-message context: time, recent context, recall), the raw answer, tokens, cost and time.
- **Logs**: events filterable by period, type, trace and text.
- **Environment**: the settings in use, read only, with keys, tokens and passwords masked.

## Instant feedback

Every input gets something back in under 1 second (`uva/progress.py`):

- "typing…" goes out right away, for text, photo, audio, sticker and buttons, and is renewed while the work lasts;
- if the answer is not ready in 0.7 s, a ready-made phrase for the kind of work appears (`pensando`, `executando`, `procurando`, `pesquisando`, `olhando`, `salvando`, `transcrevendo`...). It is drawn from the ready-made phrases file, without repeating the last ones, and **that same message is edited** with the result. If the work changes phase (for example, the LLM decides to read a note), the phrase follows;
- when the LLM decides to run something slow (`/uva`, an unqueued search, `decide`...), its reply goes out first and the result comes after, in place of a "⚙️ Rodando…";
- buttons answer right away with a short notice ("⚙️ Rodando…", "Ok");
- the dashboard chat does the same, streaming: "typing…", then the phrases, then the answer.

To measure the time to first response on each path: `docker compose exec bot python /repo/scripts/latency.py`.

## Language and formatting

Everything internal is in English: system prompts, code, commits, READMEs and technical notes. What you read comes in the **bot language**, set by `BOT_LANGUAGE` in `.env`: `en` (default) or `pt` (`pt-BR` also works). Restart the services after changing it (`docker compose up -d`).

- **Fixed texts** (command replies, `/help`, usage hints, notices, buttons, errors) live in JSON files, one per module: `src/uva/locales/en/<namespace>.json` and `src/uva/locales/pt/<namespace>.json`. The file name is the namespace, named after the module (`commands.tasks.json` for `src/uva/commands/tasks.py`), and nested keys are joined with dots. Code reads them with `t("commands.tasks.none")` or, with placeholders, `t("commands.tasks.cancelar.not_found", id=task_id)` from `uva/i18n.py`. A key missing in Portuguese falls back to English; a key missing everywhere shows the key itself and is logged.
- `tests/test_i18n.py` checks that every key exists in both languages with the same placeholders. Most tests run in Portuguese (`tests/conftest.py`), and each area has English tests too.
- **LLM replies** are written in the bot language (the system prompts say which), keeping technical terms in English. The personality (`data/soul.md`) still applies.
- **Ready-made phrases** come from `data/fallback_phrases.json` (pt) or `data/fallback_phrases.en.json` (en).
- **Input** works in both languages: commands have Portuguese and English names (see [Commands](#commands)), and small talk like "oi"/"hi" or "valeu"/"thanks" is understood either way.
- **Stays in Portuguese by design:** second-brain note titles and file names (your content, see [Names in the second brain](#names-in-the-second-brain)), and `TRANSCRIBE_LANGUAGE`/`WIKIPEDIA_LANG`, which are separate settings.
- **Text only the LLM reads** (system prompts, action descriptions, context blocks, retry feedback) and the bot's commit messages are always in English.

To add a language, create `src/uva/locales/<code>/` with the same files and a `data/fallback_phrases.<code>.json`, then add the code to `SUPPORTED` and `NAMES` in `uva/i18n.py`.

Replies reach Telegram as HTML (`uva/tgformat.py`). Common Markdown coming from the LLM and Claude Code (**bold**, `code`, blocks, headings, lists, links) is converted, and the rest is escaped. If Telegram rejects the formatting, the message is sent as plain text.

## Logs and diagnostics

Every message gets a *trace id*, and every step (chosen route, command, action, LLM call, task, error) becomes an event in Mongo's `events` collection (deleted after `EVENTS_TTL_DAYS`, default 90) and in the console as JSON.

- `/log`: last hour, one line per message showing the path it took.
- `/log 30m`, `/log 6h`, `/log 2d`, `/log hoje` (`today`), `/log ontem` (`yesterday`): other periods.
- `/log 1d llm`: filter by type: `llm`, `goals`, `iniciativas`, `projetos`, `shell`, `erros`, `tarefas`, `memoria`, `acoes`, `selfcode` (English: `errors`, `tasks`, `memory`, `actions`, `initiatives`, `projects`).
- `/log trace <id>`: all events of one message, with full data.
- `/status`: services up, uptime, queue and spending.

Long replies arrive as a `.md` file.

To send a question through the real router without Telegram (it does not go into short-term memory; use `--record` to keep it):

```bash
docker compose exec bot python /repo/scripts/ask.py "me fala sobre o livro X que eu li"
```

Integration tests (real Mongo/Redis) run in a throwaway container on the compose network:

```bash
sh scripts/run-integration-tests.sh
```

## Security

- A global filter (`security.py`) runs before any handler and only lets through messages from `ALLOWED_TELEGRAM_USER_ID` in a **private chat**.
- Messages from other people or from groups are silently ignored and logged (`unauthorized_update_ignored`).
- The LLM only triggers registered, validated actions; it never runs free code. Code it writes (self-coding) is statically checked and tested before being merged, `/sh` and `/py` run in an isolated container, and Claude Code projects run in a separate container with a closed tool list.
- **Prompt-injection scanner** (`uva/safety.py`): text that comes from outside or is about to be stored is checked for instruction overrides ("ignore previous instructions", "ignore as instruções"), role overrides ("you are now...", "a partir de agora você é..."), instructions hidden in HTML comments, requests to read secrets (`.env`, tokens, credentials), `curl`/`wget` sending secrets out, and invisible or bidirectional Unicode. The background review skips any write with a hit.
