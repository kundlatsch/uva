# Plano da UVA

Assistente pessoal com IA no Telegram, de um único usuário, rodando em Docker no PC.

## Fase 0: Fundação (concluída)
Estrutura do pacote, configuração via `.env`, Dockerfile + Compose (bot, mongo), inicialização automática no Windows.

## Fase 1: MVP (concluída)
Bot com long polling, filtro de usuário único, registro de comandos (`/start`, `/help`, `/ping`), logs estruturados.

## Fase 2: Log estruturado e /log
- Toda mensagem recebe um trace_id. Cada passo grava um evento com esse id: mensagem recebida, qual fallback resolveu, ação executada, chamada à LLM (modelo, tokens, custo estimado, duração), tarefa enfileirada, erro.
- Eventos vão para a coleção `events` no Mongo (índice TTL, ex. 90 dias) e para o console em JSON.
- /log com parâmetros: período (/log 30m, 6h, 2d, hoje; padrão última hora); filtro por tipo (/log 1d llm | goals | erros); detalhe (/log trace <id>).
- Resposta resumida; se passar do limite do Telegram, envia arquivo .txt/.md.
- /status: serviços no ar, uptime, tamanho da fila, gasto com API no dia e no mês.

Pronto: /log 1h mostra o que aconteceu e por qual caminho cada mensagem passou.

## Fase 3: Fallback 2 (similaridade)
- Comandos ganham apelidos e frases de exemplo. Texto normalizado (minúsculas, sem acentos/pontuação) comparado com rapidfuzz.
- Dois limiares configuráveis: alto executa; médio pergunta "Você quis dizer /log?" com botões Sim/Não; baixo vai para o fallback 3.
- Extração simples de argumentos ("log 2 horas" -> /log 2h). Cada decisão com a nota vai para o log.

Pronto: "ping" responde pong; "qual a capital da França" não casa com comando.

## Fase 4: Fallback 3 (LLM com ações em JSON)
- Registro de ações: função Python com nome, descrição e schema Pydantic. Iniciais: reply, search, run_command.
- Camada LLMProvider genérica com complete(mensagens, schema); adaptadores Anthropic e OpenAI-compatível; provedor/modelos do .env; cada chamada loga provedor, modelo, tokens, custo estimado (tabela de preços configurável).
- Resposta obrigatória: {"actions":[{"type":"search","params":{...}}],"reply":"..."}. Structured outputs/tool use quando suportado; sempre validar com Pydantic; JSON inválido ou ação desconhecida não executa nada (erro no log + resposta amigável). A LLM nunca executa código livre.
- search usa o SearchBackend configurado e devolve resumo com fontes.
- /llm mostra provedor e modelos. Histórico curto (últimas N mensagens) no contexto.
- Orçamentos LLM_DAILY_BUDGET_USD, LLM_MONTHLY_BUDGET_USD, PROACTIVE_DAILY_BUDGET_USD; ao bater, desliga o fallback 3 (ou o loop) até virar o período e avisa.

Pronto: "pesquisa pra mim sobre Fórmula 1 na Wikipédia" gera search e devolve resumo; /log trace mostra o JSON.

## Fase 5: Celery + Redis
- Serviços redis, worker, beat no Compose. Ações demoradas (search) viram tarefas: bot responde "Pesquisando..." e manda o resultado depois.
- Beat preparado para as fases 7 e 8. Retentativas com backoff. Lock no Redis de "ocupado". /tarefas e /cancelar <id>.

Pronto: duas pesquisas seguidas, /ping continua respondendo na hora.

## Fase 6: Second brain em Markdown (estilo Obsidian, método PARA + Zettelkasten)
- Estrutura: 00-inbox/, 01-projetos/, 02-areas/, 03-recursos/ (pesquisas), 04-arquivo/, diario/ (AAAA-MM-DD.md), LEIA-ME.md.
- Notas com YAML (title, created, tags, source: uva-bot) e links [[...]].
- Ações note_create, note_append, note_search, note_read; comandos /nota <texto> e /buscar <termo>. Pesquisas e resultados de objetivos são salvos aqui. Nunca apagar notas.

Pronto: pesquisa pelo Telegram vira nota formatada.

## Fase 7: Memória no Mongo
- Coleção memory: key (única), value, category (perfil|preferencia|fato|contexto), relevance 0-1, created_at, last_used_at, use_count, expires_at (TTL), source (trace_id).
- Ações memory_save/update/forget para a LLM.
- Na subida e 1x por dia: nota = relevância × recência × frequência; top N (env) vira "memória de trabalho" no prompt.
- Limpeza a cada 24h (beat): TTL remove expiradas; baixa nota sem uso há muito vai para coleção de arquivo.
- /memoria, /lembrar <chave> <valor>, /esquecer <chave>.

## Fase 8: Objetivos e loop proativo
- Coleção goals: title, description, kind (recorrente|unico), priority, status (ativo|pausado|concluido), min_interval, last_run_at, next_run_at, run_count, results.
- Ação goal_create; /objetivos, /pausar, /retomar, /remover.
- Beat a cada PROACTIVE_INTERVAL_MINUTES: se ocupado não faz nada; checa orçamento; escolhe objetivo de maior prioridade vencido; executa via LLM só com ações seguras (pesquisa, notas, memória); salva nota e loga. Silencioso no Telegram. Limite de passos e tokens por execução.

## Fase 9: SQLite
- Arquivo em volume, WAL, migrações simples, camada de acesso pequena. Comando exemplo: /gasto 25 mercado grava; /gastos mes soma.
