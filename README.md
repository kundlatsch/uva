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
