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
| `/comandos` | catálogo completo para debug: comandos (core/plugin, apelidos, exemplos) e ações da LLM com parâmetros |
| `/ping`, `/start` | teste e apresentação |
| `/nota <texto>`, `/buscar <termo>` | second brain |
| `/memoria [chave]`, `/lembrar <chave> <valor>`, `/esquecer <chave>` | memória |
| `/objetivos`, `/pausar <id>`, `/retomar <id>`, `/remover <id>` | objetivos do modo proativo |
| `/gasto <valor> <categoria> [descrição]`, `/gastos [hoje\|semana\|mes\|ano\|tudo]` | gastos (SQLite) |
| `/log [período] [tipo]`, `/log trace <id>` | o que aconteceu |
| `/status`, `/llm` | saúde dos serviços, modelos e gastos com API |
| `/tarefas`, `/cancelar <id>` | tarefas em segundo plano |
| `/frases [categoria]` | frases prontas do bot |
| `/decidir ...` | probabilidade, escolha ou nota (OpenAI Decisions) |
| `/contexto [limpar]` | memória de curto prazo da conversa |

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

**Antes da LLM, a UVA procura em todos os sistemas** (`uva/recall.py`, sem gastar LLM): as palavras da mensagem são buscadas na memória longa inteira (não só na de trabalho), nas notas do brain (título e conteúdo; as 2 melhores vão com o texto, versões em `04-arquivo` ficam de fora), nos objetivos e no que eles descobriram e, se a mensagem fala de dinheiro, nos gastos. O que bate entra no prompt, então "me fala sobre o livro X que eu li" é respondido numa chamada só, com a sua nota e as pesquisas sobre o autor. Se ela ainda precisar ler algo (`recall`, `note_search`, `note_read`), o resultado volta para ela e a resposta sai na mesma mensagem (até 2 consultas extras), em vez de "quer que eu abra a nota?".

Cada decisão fica no log com a nota, então dá para ajustar os limiares olhando `/log`.

## LLM, pesquisa e orçamento

- **Provedores**: `LLM_PROVIDER=anthropic` (padrão; modelos `claude-haiku-5-5` e `claude-opus-5-5` se não configurar) ou `openai_compatible` (OpenAI, Ollama, OpenRouter... com `LLM_BASE_URL`).
- **Ações iniciais**: `reply`, `run_command` (executa um comando do bot) e `search`.
- **Pesquisa**: com Anthropic usa a busca na web do próprio Claude; com outros provedores usa Wikipédia + DuckDuckGo (`SEARCH_BACKEND` troca). "Pesquisa sobre X na Wikipédia" força a Wikipédia. O resumo é feito pelo modelo pesado (`LLM_MODEL_HEAVY`) e vem com as fontes.
- **Custo**: cada chamada registra modelo, tokens, custo estimado e duração (`/log 1d llm`). A tabela de preços já conhece os modelos Anthropic; para outros, configure `LLM_PRICES` (US$ por 1M tokens). Sem preço, usa `LLM_DEFAULT_PRICE_*` (pessimista).
- **Orçamento**: `LLM_DAILY_BUDGET_USD` e `LLM_MONTHLY_BUDGET_USD` (conversa) e `PROACTIVE_DAILY_BUDGET_USD` (modo proativo). Ao bater o limite, o fallback 3 desliga até virar o dia/mês e o bot avisa; os comandos continuam funcionando.
- `/llm` mostra provedor, modelos, backends de pesquisa e gastos.
- `/liberar [valor]` libera gasto extra de LLM **só hoje** (até a meia-noite): `/liberar 2` dá US$ 2 a mais; sem valor, mais um limite diário inteiro. Pode repetir (soma). O limite mensal continua valendo; `/status` e `/llm` mostram o que foi liberado.
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
- `/brain` mostra a árvore de pastas e notas; `/brain <pasta>` (ex.: `recursos` ou `02-areas/Saúde`) lista uma pasta com tamanho e data; `/brain <título ou caminho>` mostra o conteúdo bruto da nota, com o cabeçalho YAML; conteúdo longo vem paginado, com botões ◀ Anterior / Próxima ▶ que trocam a página na mesma mensagem. Só leitura, só `.md`, e nada oculto (`.obsidian`, `.trash`...). Também: `/ver`, `/abrir`.
- Toda pesquisa vira uma nota em `03-recursos/` ("Pesquisa - ...") com as fontes, e cada nota criada é registrada no diário do dia.
- A LLM também pode usar as ações `note_create`, `note_append`, `note_search` e `note_read`.
- Pasta nova: peça "cria uma pasta de X" e a UVA escolhe o lugar pelo PARA (projetos, áreas, recursos, arquivo), a menos que você diga onde, e cria a pasta com um arquivo `.md` vazio de mesmo nome, tipo um `__init__.py`, para o Obsidian mostrar (ação `folder_create`). Se você der conteúdo, a nota vai dentro dela (`note_create` com `subfolder`). Até 3 níveis; nomes ocultos (`.algo`) e `..` são recusados; pastas existentes não são mexidas.
- A UVA **nunca apaga** notas. Para corrigir ou renomear uma nota ela usa `note_update`, que guarda antes uma cópia da versão anterior em `04-arquivo/Versões/`. Textos com marcadores no lugar do conteúdo (ex.: `<texto anterior mantido aqui>`) são recusados.

Para abrir no Obsidian: *Open folder as vault* → escolha a pasta `brain`.

## Memória de curto prazo

A UVA lembra o que aconteceu na conversa de agora, inclusive o que **ela mesma** fez: uma sessão no Redis (`stm:<seu id>`), compartilhada por bot e worker, com os últimos turnos (saídas de comando resumidas e avisos que ela mandou sozinha), referências a tudo que disparou ou criou (pesquisas, tarefas, notas, objetivos, features, gastos) com o status atualizado pelo worker, e qual delas está "em foco". Detalhes em [docs/memoria-curto-prazo.md](docs/memoria-curto-prazo.md).

- "como tá aquela pesquisa?", "e a tarefa?", "deu certo?" são respondidos direto, sem LLM, com o status real da referência em foco.
- A LLM recebe um bloco "Contexto recente" e as ações `task_status`, `retry` e `cancel` (aceitam a ref, ex.: `t3`, ou "aquela").
- `/contexto` mostra a sessão; `/contexto limpar` apaga.
- Validade `STM_TTL_HOURS` (12h), com no máximo `STM_MAX_TURNS` turnos e `STM_MAX_REFS` referências; o que ainda está rodando não expira. Depois de `STM_PROMOTE_IDLE_HOURS` (3h) parada, o modelo barato decide o que vale ir para a memória longa e a sessão é descartada.

## Memória

A UVA guarda o que vale a pena lembrar sobre você na coleção `memory` do Mongo: chave, valor, categoria (`perfil`, `preferencia`, `fato`, `contexto`), relevância (0 a 1), datas de criação e último uso, contagem de uso e validade opcional.

- A LLM decide sozinha quando guardar (ações `memory_save`, `memory_update`, `memory_forget`). Exemplo: "meu gato se chama Mingau" vira `nome_do_gato = Mingau`.
- **Memória de trabalho**: cada item tem uma nota = relevância × recência × frequência. Os `MEMORY_WORKING_SIZE` melhores vão no prompt de toda conversa. A lista é recalculada na subida, uma vez por dia e sempre que a memória muda.
- **Limpeza diária** (beat, `MEMORY_MAINTENANCE_HOUR`): itens com validade vencida somem (índice TTL); itens com nota baixa e sem uso há `MEMORY_ARCHIVE_AFTER_DAYS` dias vão para `memory_archive`.
- `/memoria` lista tudo (★ = memória de trabalho), `/memoria <chave>` mostra um item, `/lembrar <chave> <valor>` guarda, `/esquecer <chave>` apaga.

## Objetivos e modo proativo

**Objetivos.** Peça em linguagem natural ("cria um objetivo de acompanhar notícias de energia solar", "descobre por qual livro o Ricardo Terto foi semifinalista do Jabuti"). A LLM cria com `goal_create`:
- `unico` (padrão): tarefa com fim. É **concluída** quando a LLM diz que terminou, ou quando a feature que ela pediu fica pronta.
- `recorrente`: acompanhar algo; roda a cada `min_interval_hours` (padrão 6h; notícias 6–12h, preços 2–6h...).

O beat verifica a cada `PROACTIVE_POLL_MINUTES` (5) e roda no máximo um objetivo a cada `PROACTIVE_INTERVAL_MINUTES` (15). Cada execução usa só ações seguras (pesquisa, notas, memória), tem limite de rodadas e tokens, grava em `01-projetos/Objetivo - <título>.md` e **sempre manda relatório** no Telegram (`PROACTIVE_NOTIFY=true`). Se o objetivo precisa de um comando novo, ela pede ao auto-código (`PROACTIVE_ALLOW_SELFCODE`).

**Iniciativas.** Além dos objetivos, a UVA aparece por conta própria. A cada ciclo, o modelo barato olha a memória longa, a conversa recente, as notas do brain e os objetivos, e escolhe:
- `research`: pesquisa um tema do seu interesse e manda o resumo (salvo em `03-recursos/`);
- `message`: um lembrete, uma pergunta de acompanhamento ("voltou pro livro do Terto?") ou uma sugestão;
- `nothing`: quando não vale interromper.

Travas: no máximo `PROACTIVE_MAX_MESSAGES_PER_DAY` (4) por dia, com `PROACTIVE_MIN_GAP_MINUTES` (120) entre elas; nada no horário de silêncio (`PROACTIVE_QUIET_START_HOUR`–`_END_HOUR`, 23h–8h); nada enquanto você conversa; tudo dentro de `PROACTIVE_DAILY_BUDGET_USD`.

- `/proativo` — status (ligado?, mensagens hoje, silêncio, próxima iniciativa, objetivos, últimas iniciativas)
- `/proativo on` / `/proativo off` — liga/desliga as iniciativas (os objetivos continuam)
- `/proativo agora` — manda uma iniciativa já (respeita só o orçamento)
- `/objetivos`, `/pausar <id>`, `/retomar <id>`, `/remover <id>`; `/log 1d goals` e `/log 1d iniciativas`

## Rotinas (tarefas recorrentes)

Pedidos que rodam sozinhos num horário, passando pelo roteador normal (mesmo orçamento e travas de uma mensagem digitada). O resultado chega no Telegram.

- `/rotina todo dia 08:00 pesquisa se a skin X saiu na loja do LoL`
- `/rotina segunda e quinta 9h me lembra de regar as plantas`
- `/rotina a cada 6 horas confere o preço do dólar`
- Em linguagem natural também ("todo dia às 8 vê se..."): a LLM usa `routine_create`.
- `/rotinas` lista; `/rotina <id> pausar|retomar|remover|agora`.

**PC desligado.** O beat confere a cada minuto. Uma rotina cujo horário passou com o PC desligado roda **uma vez** quando ele liga, por mais horários que tenha perdido, e o aviso diz "atrasada: era para 10/10 08:00". A próxima execução é sempre o próximo horário depois de agora. A agenda e a última execução ficam no Mongo (`routines`).

## SQLite (dados estruturados)

Para dados com formato fixo há um SQLite em `SQLITE_PATH` (volume `sqlite-data`), em modo WAL. As migrações ficam em `src/uva/storage/sqlite.py` (lista `MIGRATIONS`; o número aplicado fica em `PRAGMA user_version`). Para mudar o esquema, acrescente uma migração nova no fim, nunca edite as antigas.

Exemplo incluído: gastos.
- `/gasto 25,90 mercado pão e leite` grava (aceita `25`, `25,90`, `R$ 1.234,56`)
- `/gastos mes` soma por categoria (também `hoje`, `semana`, `ano`, `tudo`)
- Em texto livre também funciona: "gastei 30 no mercado" (ação `expense_add`)

## Personalidade (soul.md)

A personalidade da UVA fica em [data/soul.md](data/soul.md): irônica, brincalhona, meio grossa, com palavrão quando cabe, numa dinâmica de parceira, mas sempre útil e precisa. O arquivo entra no começo do system prompt de toda chamada à LLM (conversa, resumos de pesquisa, objetivos). Saídas exatas (JSON, erros, `/log`, resultados de comandos) continuam neutras.

- Edite o arquivo à vontade: ele é relido quando muda, sem reiniciar nada.
- `/soul` mostra o conteúdo atual.

## Auto-edição

A UVA pode ler e mudar alguns arquivos dela mesma pelas ações `file_read`, `file_write` e `file_append`, **só** nestes caminhos:

- `data/...` (ex.: `data/soul.md`): leitura, reescrita e acréscimo. Cada mudança vira um commit no repositório (autor `GIT_AUTHOR_NAME`, trailer `UVA-Self-Edit: true`) contendo só aquele arquivo.
- `brain/...` (second brain): leitura, criação de notas novas e acréscimo; notas existentes nunca são sobrescritas.

Qualquer outro caminho (`src/`, `security.py`, `.env`, `docker-compose.yml`, arquivos ocultos, `..`) é recusado.

`/reverter` desfaz o último commit feito pelo bot (só os dele, nunca os seus) com um commit de revert marcado `UVA-Reverts: <sha>`; rodar de novo desfaz o anterior. Mudanças aparecem em `/log 1d selfcode`.

## Auto-código: a UVA programa comandos novos

Peça com `/feature <descrição>` (ex.: `/feature /dado rola um dado de 6 lados`) ou em texto livre ("cria um comando que..."; ação `feature_request`). Uma tarefa no worker então:

1. clona o repositório montado numa pasta temporária, num branch `uva/feature-<id>`;
2. pede ao modelo pesado um **plugin** (`src/uva/plugins/<nome>.py`) e seus testes (`tests/plugins/test_<nome>.py`), seguindo a interface documentada em [src/uva/plugins/api.py](src/uva/plugins/api.py) (exemplo: [moeda.py](src/uva/plugins/moeda.py));
3. recusa sem executar código que use imports fora de uma lista permitida (nada de `os`, `subprocess`, `pathlib`, config, segurança), `open`/`eval`/`exec`/`getattr`, atributos privados etc.;
4. roda **a suíte inteira** do pytest num processo sem as variáveis secretas; se falhar, devolve a saída para a LLM e tenta de novo (até `SELFCODE_MAX_ATTEMPTS`);
5. com tudo verde, faz commit (`UVA-Self-Code: true`), merge na `main` e pede ao bot (Redis) para recarregar os plugins sem reiniciar; se o plugin não carregar no bot, reverte sozinha;
6. avisa no Telegram o que criou, como usar, quantas tentativas e o custo, ou por que falhou.

- `/features` lista as tentativas; `/reverter` também desfaz commits de auto-código (e recarrega os plugins).
- Orçamento próprio: `SELFCODE_DAILY_BUDGET_USD` (não consome o orçamento diário geral, mas conta no mensal).
- Plugins também podem ser escritos à mão: crie o arquivo em `src/uva/plugins/`, os testes em `tests/plugins/` e reinicie o bot (ou use `/reverter`/`/feature` para disparar um reload). Eles aparecem como `[plugin]` em `/comandos`.
- Requer o repositório na branch `main`; mudanças suas ainda não commitadas não entram no clone.

## Projetos novos com o Claude Code (/projeto)

A UVA cria projetos de software **novos e separados** dela:

- `/projeto <descrição>` (ou "cria um projeto novo de ..." em linguagem natural): cria a pasta `PROJECTS_HOST_PATH/<nome>` (padrão `../uva-projects`, ao lado deste repositório), faz `git init` com o autor `GIT_AUTHOR_NAME <GIT_AUTHOR_EMAIL>` e roda o Claude Code sem interação (`claude -p`). Ele escreve o código, o README e os testes, roda os testes e faz commits. No fim, a UVA manda no Telegram o resumo, o caminho, o custo, os turnos e os commits, ou o motivo da falha.
- `/projetos`: lista com o status (na fila, construindo, pronto, falhou, cancelado).
- `/cancelar <id>`: para um projeto que está na fila ou em construção.
- `/log 1d projetos`: histórico.

**Só projetos novos.** Pedidos para clonar, baixar ou rodar repositórios de terceiros são recusados, porque esse código rodaria ao lado do token do Claude Code. Isso vai ser papel do `/rodar`, num container sem token. O git do Claude Code também não tem `clone`, `fetch`, `pull`, `submodule` nem `remote`. Ele trabalha em inglês (código, README, commits, sem traduzir nada) e manda um resumo curto em português.

Um projeto por vez. Limites: `PROJECT_TIMEOUT_MINUTES` (30), `PROJECT_MAX_TURNS` (80) e `PROJECT_MAX_BUDGET_USD` (3) por projeto; `PROJECT_DAILY_BUDGET_USD` (10) por dia.

**Credencial.** Ponha **uma** no `.env`: `CLAUDE_CODE_OAUTH_TOKEN` (rode `claude setup-token` no seu PC; usa a sua assinatura do Claude) ou `PROJECTS_ANTHROPIC_API_KEY` (cobrada à parte). Depois, rode `docker compose up -d`.

**Permissões (lista fechada).** O Claude Code roda no modo `dontAsk` com `--allowedTools`. Ele pode ler e editar arquivos só dentro da pasta do projeto (`./**`), mas não a pasta `.claude` dele. No shell, só pode rodar `git`, `python`/`python3`, `pip`, `node`, `npm`, `npx` e `pytest`. Qualquer outra coisa é recusada, sem pergunta. Nunca roda com permissão total.

**Isolamento.** Tudo roda no container `projects`, que monta **só** a pasta de projetos: nada do repositório da UVA nem do second brain. O Claude Code roda como o usuário sem privilégios `builder`, com um ambiente limpo (só a credencial dele e o autor do git), então não lê os tokens do bot. Atenção: python e node, que estão na lista, executam código, e o container alcança a rede interna (Mongo e Redis, sem senha). Peça projetos, não tarefas sobre a infraestrutura.

## A pasta da UVA: /uva (/sh e /py)

A UVA tem uma pasta só dela, `/uva`, onde roda comandos Linux e Python livremente. Ela usa isso sozinha, com as ações `shell_run` e `python_run` ("calcula isso", "baixa e conta as linhas desse CSV"), e você também pode usar direto:

- `/sh <comando>`: roda no bash dentro de `/uva` e devolve a saída, paginada se for longa.
- `/py <código>`: roda Python 3 (pode ter várias linhas). Os pacotes vão para um venv em `/uva/.venv`: `/sh pip install requests`.
- `/sh desfazer`: volta `/uva` para como estava antes do último comando que mudou arquivos. Repita para voltar mais.
- `/sh historico`: lista os snapshots. `/log 1d shell` mostra o histórico.

**Apagar pede permissão.** Comandos que apagam ou sobrescrevem arquivos (`rm`, `rmdir`, `mv`, `truncate`, `find -delete`, `git clean`, `sed -i`, `>` ou `cp` em cima de um arquivo que já existe; em Python, `os.remove`, `shutil.rmtree`, `open(..., "w")`...) só rodam depois de você tocar em **Sim, rodar** no Telegram. Isso vale também quando é a LLM que pede. Como nenhuma detecção é perfeita, todo comando que muda algo deixa antes um snapshot de `/uva` (git, guardado fora do alcance do comando), e `/sh desfazer` restaura. Ficam de fora dos snapshots `.venv`, `.cache` e `node_modules`.

**Isolamento de verdade, não só regra no prompt.** O container `shell` funciona assim:
- monta só os volumes `/uva` e `/snapshots`, sem `.env`, repositório, brain, projetos ou `docker.sock`;
- o sistema de arquivos é somente leitura (só `/uva` e `/tmp` aceitam escrita);
- todo comando roda como o usuário `uva`, sem sudo e sem nenhuma capability (`no-new-privileges`). Ele não lê o ambiente do processo principal nem os snapshots;
- limites: `SHELL_MEMORY` (1g), `SHELL_CPUS` (1), 256 processos e `SHELL_TIMEOUT_SECONDS` (60 s) por comando;
- rede: fica numa rede interna (`shellnet`) que só o bot e o worker alcançam, então não chega no Mongo nem no Redis. Tem internet para pip e curl, que dá para cortar com `SHELL_NO_INTERNET=true`.

## Figurinhas

A UVA aprende o que suas figurinhas querem dizer. São duas coisas diferentes, e as duas ficam na tabela `stickers` do SQLite, com o `file_unique_id` da figurinha como chave, mais o emoji e o pacote:

1. **Significado.** Na primeira vez que você manda uma figurinha sozinha, ela pergunta "o que ela significa?", e a sua próxima mensagem (texto ou áudio) vira o significado (`/pular` deixa sem). Depois disso, a figurinha chega para a LLM como `[figurinha 😴: quando eu tô morto de cansaço]`, junto com o contexto da conversa, sem interpretar a imagem.
2. **Ação.** "toda vez que eu mandar essa figurinha, me deseja boa noite e me lembra de beber água", logo depois de mandar a figurinha, prende um pedido a ela. A cada envio, o pedido passa pelo roteador normal, como se você tivesse digitado, com o mesmo orçamento e as mesmas travas. Pode ser um comando também (`/figurinha 3 acao /log 1h`).

- `/figurinhas`: lista com o significado, a ação e o uso de cada uma.
- `/figurinha <n> significado <texto>`, `/figurinha <n> acao <pedido>`, `/figurinha <n> sem-acao`, `/figurinha <n> remover`.

## Áudio

Mande um áudio (mensagem de voz, arquivo de áudio ou vídeo redondo) e a UVA transcreve com a OpenAI (`TRANSCRIBE_MODEL`, padrão `gpt-4o-mini-transcribe`, com `whisper-1` de reserva), responde "🎙️ entendi: ..." e trata o texto **exatamente como se você tivesse digitado**: comandos, similaridade e LLM.

- Assim que o áudio chega, ela avisa que está transcrevendo (frases da categoria `transcrevendo` em `data/fallback_phrases.json`).
- O ffmpeg converte o áudio do Telegram (OGG/Opus) para MP3 mono antes de enviar.
- Limite de 20 MB por arquivo (limite do Telegram para bots).
- Custo por minuto de áudio entra no orçamento e em `/log 1d llm` (`kind=transcribe`); a transcrição fica na memória de curto prazo como `[voz] ...`.
- A chave é `TRANSCRIBE_API_KEY`, ou a `LLM_API_KEY` quando o provedor principal já é a OpenAI.
- Transcrição local (faster-whisper na CPU) ficou para depois: deixaria a imagem bem maior e lenta no PC.

## Imagens

A UVA enxerga imagens (fotos e imagens mandadas como arquivo) com o modelo de `LLM_MODEL_VISION` (padrão: o mesmo do roteador). Funciona com OpenAI e Anthropic.

- **Foto com legenda**: a legenda e a imagem vão juntas para a LLM ("o que é isso?", "traduz o cardápio"). Legenda começando com `/` continua sendo comando (ex.: `/decidir ...` usa a foto).
- **Foto sem legenda**: ela descreve em poucas frases (transcrevendo o texto que houver) e guarda a imagem por 30 minutos. Perguntas seguintes como "o que tá escrito aí?" ou "traduz isso" mostram a imagem de novo para a LLM.
- **Guardar**: "salva essa foto" usa a ação `image_save`, que grava a imagem em `05-anexos/` do second brain e cria uma nota com `![[...]]` e a descrição. Arquivos nunca são sobrescritos.
- O custo das chamadas com imagem entra no orçamento normal (`purpose` `plan_vision` / `vision_describe` no `/log 1d llm`).

## Decisões (OpenAI Decisions)

Para perguntas com resposta numérica e calibrada, a UVA usa a API Decisions da OpenAI (beta, modelo `gpt-6-luna`, US$ 0,10 por 1M tokens de entrada, saída grátis). A chave é `DECISIONS_API_KEY`, ou a `LLM_API_KEY` quando o provedor principal já é a OpenAI.

- `/decidir <afirmação>[: texto]` → chance de ser verdade. Ex.: `/decidir esse email é spam: Ganhe um iPhone grátis`
- `/decidir <pergunta> | op1, op2, op3 [:: texto]` → escolhe uma opção, com confiança e probabilidades. Ex.: `/decidir o que jantar | pizza, sushi, salada :: tô sem grana`
- `/decidir nota <critério> | nível0, nível1, ... :: texto` → nota de 0 a N (níveis do mais baixo ao mais alto). Ex.: `/decidir nota urgência | baixa, média, alta :: o servidor caiu`
- Foto com a legenda `/decidir ...` avalia a imagem. Foto sem legenda fica guardada 30 min para perguntas como "tem gato nessa foto?".
- Em texto livre, a LLM usa a ação `decide` (até 8 perguntas) para "qual a chance de...", escolher entre opções fixas ou dar nota, em vez de chutar.
- Cada chamada entra no orçamento e no `/log 1d llm` (`kind=decisions`).

## Frases prontas

[data/fallback_phrases.json](data/fallback_phrases.json) tem 140+ frases no tom do soul.md, por categoria (`sem_credito`, `llm_erro`, `nao_entendi`, `ocupado`, `tarefa_falhou`, `tarefa_ok`, `pesquisando`, `saudacao`, `agradecimento`, `despedida`, `confirmacao`, `cancelado`, `sem_resultado`, `erro_generico`). Placeholders como `{id}` e `{termo}` ficam documentados no próprio arquivo.

- Usadas quando a resposta não precisa da LLM: orçamento estourado ou provedor sem crédito (a frase + o motivo exato), erro da LLM, "não entendi", início de pesquisa, avisos de tarefa, cancelamentos, buscas vazias.
- "oi", "valeu", "falou" e parecidos são respondidos direto daqui, sem gastar LLM.
- Sorteio sem repetir as últimas 3 de cada categoria; o arquivo é validado e relido quando muda. Se estiver ausente ou quebrado, entram frases neutras embutidas.
- `/frases` lista as categorias; `/frases <categoria>` mostra exemplos. A própria UVA pode editar o arquivo (`file_write`), e JSON inválido é recusado antes de gravar.

## Painel local (dashboard)

O serviço `dashboard` publica um painel em **http://localhost:8080**, só neste computador (`127.0.0.1`; a porta muda com `DASHBOARD_PORT`). Se quiser senha, defina `DASHBOARD_PASSWORD` (HTTP Basic, com qualquer usuário). Ele tem:

- **Visão geral**: serviços no ar, fila, eventos e erros das últimas 24h, contagens (memórias, objetivos, notas, figurinhas, projetos).
- **Custos e limites**: o gasto com LLM de hoje, do mês, do proativo, do auto-código e dos projetos, cada um contra o seu limite. Também tem um gráfico por dia (separado em interativo, proativo e auto-código), totais por propósito e por modelo, o botão de `/liberar` e os gastos pessoais do SQLite.
- **Chat**: um canal completo com a UVA, pelo mesmo pipeline do Telegram (comandos, similaridade, LLM, ações e a mesma memória curta). A conversa do Telegram e os avisos dos objetivos e iniciativas aparecem ali também. Botões de confirmação funcionam. Ao lado, o **debug** de cada mensagem mostra:
  - por onde ela passou (qual fallback resolveu);
  - o recall;
  - o JSON de ações e o resultado de cada ação;
  - cada chamada à LLM, com o prompt de sistema (memória, recall, contexto), as mensagens enviadas, a resposta bruta, os tokens, o custo e o tempo.
- **Logs**: eventos filtráveis por período, tipo, trace e texto.
- **Ambiente**: as configurações em uso, somente leitura, com chaves, tokens e senhas mascarados.

## Resposta imediata

Toda entrada recebe algo em menos de 1 segundo (`uva/progress.py`):
- "digitando…" sai na hora, para texto, foto, áudio, figurinha e botões, e é renovado enquanto o trabalho dura;
- se a resposta não ficar pronta em 0,7 s, aparece uma frase pronta do tipo da tarefa (`pensando`, `executando`, `procurando`, `pesquisando`, `olhando`, `salvando`, `transcrevendo`...). Ela é sorteada de `data/fallback_phrases.json`, sem repetir as últimas, e essa **mesma mensagem é editada** com o resultado. Se o trabalho muda de fase (por exemplo, a LLM decide ler uma nota), a frase acompanha;
- quando a LLM decide rodar algo lento (`/uva`, pesquisa sem fila, `decide`...), a resposta dela sai antes, e o resultado vem depois, no lugar de um "⚙️ Rodando…";
- botões respondem na hora com um aviso curto ("⚙️ Rodando…", "Ok");
- o chat do painel faz o mesmo, em streaming: "digitando…", depois as frases, depois a resposta.

Para medir o tempo até a primeira resposta em cada caminho: `docker compose exec bot python /repo/scripts/latency.py`.

## Idioma e formatação

Toda a operação interna é em inglês: prompts de sistema, código, commits, READMEs e notas técnicas. Nada é traduzido. Só o que você lê vem em português, com os termos técnicos em inglês. A personalidade (`data/soul.md`) continua valendo nas respostas.

As respostas chegam no Telegram em HTML (`uva/tgformat.py`). O Markdown comum que vem da LLM e do Claude Code (**negrito**, `código`, blocos, títulos, listas, links) é convertido, e o resto é escapado. Se o Telegram recusar a formatação, a mensagem vai como texto puro.

## Log e diagnóstico

Cada mensagem recebe um *trace id*, e cada passo (rota escolhida, comando, ação, chamada à LLM, tarefa, erro) vira um evento na coleção `events` do Mongo (apagado após `EVENTS_TTL_DAYS`, padrão 90) e no console em JSON.

- `/log` — última hora, uma linha por mensagem mostrando o caminho que ela fez
- `/log 30m`, `/log 6h`, `/log 2d`, `/log hoje`, `/log ontem` — outros períodos
- `/log 1d llm` — filtra por tipo: `llm`, `goals`, `iniciativas`, `projetos`, `shell`, `erros`, `tarefas`, `memoria`, `acoes`
- `/log trace <id>` — todos os eventos de uma mensagem, com os dados completos
- `/status` — serviços no ar, uptime, fila e gastos

Respostas longas chegam como arquivo `.md`.

Para testar uma pergunta pelo roteador de verdade, sem o Telegram (não entra na memória curta; `--record` para entrar):

```bash
docker compose exec bot python /repo/scripts/ask.py "me fala sobre o livro X que eu li"
```

Testes de integração (Mongo/Redis reais) rodam num container descartável na rede do compose:

```bash
sh scripts/run-integration-tests.sh
```

## Segurança

- Um filtro global (`security.py`) roda antes de qualquer handler e só deixa passar mensagens do `ALLOWED_TELEGRAM_USER_ID` em **chat privado**.
- Mensagens de outras pessoas ou de grupos são ignoradas em silêncio e registradas no log (`unauthorized_update_ignored`).
