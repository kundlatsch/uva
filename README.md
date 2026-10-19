# UVA

A personal AI assistant that lives in Telegram. UVA serves a **single user**, runs in Docker on your own PC, and talks back with a personality of its own, in English or Portuguese (`BOT_LANGUAGE`). Every command works in both languages.

Full documentation: **[docs/DOCUMENTATION.md](docs/DOCUMENTATION.md)**.

## What it does

- **Understands free text.** Explicit commands run directly; anything else is matched against known commands by similarity, and only then sent to an LLM (Anthropic or any OpenAI-compatible provider) that can only trigger registered, validated actions.
- **Remembers.** Short-term memory of the current conversation (Redis), long-term facts about you (MongoDB), skills for how you want things done, and a permanent conversation log; a full-text index (SQLite FTS5) lets recall search all of it before calling the LLM, and a background review keeps what matters on its own.
- **Keeps a second brain.** An Obsidian-compatible Markdown vault for notes, research results and images. Notes are never deleted.
- **Works on its own.** Goals it pursues in the background (with a judge that checks they are really done), recurring routines (silent when there is nothing to say, script-only or chained), and proactive messages, all within daily budgets and quiet hours.
- **Extends itself.** `/feature` makes UVA write, test and merge a new command as a plugin; `/projeto` has Claude Code build a new, separate project in an isolated container.
- **Has its own sandbox.** `/sh` and `/py` run in an isolated container with snapshots and undo.
- **Handles more than text.** Voice messages are transcribed, images are understood, stickers can carry meanings and actions, and calibrated yes/no, choice and score answers come from OpenAI Decisions.
- **Shows its work.** Every step is logged with a trace id (`/log`), and a local dashboard shows costs, logs and a debug chat.

## Quick start

1. Create a bot with **@BotFather** and get your numeric user id from **@userinfobot**.
2. Copy the example settings and fill in at least `TELEGRAM_BOT_TOKEN` and `ALLOWED_TELEGRAM_USER_ID` (add `LLM_API_KEY` to enable the LLM, and `BOT_LANGUAGE=pt` for Portuguese replies):

   ```powershell
   Copy-Item .env.example .env
   ```

3. Start everything with Docker:

   ```powershell
   docker compose up -d --build
   ```

4. Send `/ping` to your bot. It should answer `pong`. Send `/help` (or `/ajuda`) to see every command.

The dashboard is at http://localhost:8080 (local only).

## Commands at a glance

| PT | EN | What it does |
|---|---|---|
| `/ajuda` | `/help` | list every command |
| `/nota`, `/buscar` | `/note`, `/search` | save and search notes |
| `/memoria`, `/lembrar`, `/esquecer` | `/memory`, `/remember`, `/forget` | long-term memory |
| `/objetivos`, `/rotina`, `/proativo` | `/goals`, `/routine`, `/proactive` | background work |
| `/gasto`, `/gastos` | `/expense`, `/expenses` | expense tracking |
| `/feature`, `/projeto` | `/feature`, `/project` | self-coding and new projects |
| `/sh`, `/py` | `/terminal`, `/python` | sandboxed shell and Python |
| `/habilidades`, `/historico`, `/perfil` | `/skills`, `/history`, `/profile` | skills, past conversations, profile size |
| `/parar`, `/refazer`, `/modo` | `/stop`, `/retry`, `/mode` | conversation controls |
| `/log`, `/status`, `/llm` | `/log`, `/status`, `/model` | diagnostics and spending |

See the [full command list](docs/DOCUMENTATION.md#commands).

## Development

Requires Python 3.12+.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

The code lives in `src/uva/`; commands are registered in `src/uva/commands/` and plugins in `src/uva/plugins/`. See [Local development](docs/DOCUMENTATION.md#local-development) for how to add a command.

## Security

Only messages from `ALLOWED_TELEGRAM_USER_ID` in a private chat are processed; everything else is ignored and logged. Secrets stay in `.env`, which is never committed. See [Security](docs/DOCUMENTATION.md#security).
