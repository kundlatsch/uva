# Notes for Claude

## Commits

- Never add `Co-Authored-By: Claude ...` or `Claude-Session: ...` trailers to commit messages. Commits are authored by kundlatsch only.
- Keep the bot's own trailers (`UVA-Self-Edit`, `UVA-Self-Code`, `UVA-Feature-Id`, `UVA-Reverts`); `/reverter` depends on them.

## i18n (mandatory for every change)

- Every text the user reads goes through `t()` (`uva/i18n.py`) with the key in **both** `src/uva/locales/en/` and `src/uva/locales/pt/`, with the same placeholders: command replies, buttons and their toasts, notices (task results, routine/goal reports, reminders, initiatives, review notices, recovery questions), errors shown in replies, `/help` descriptions and usage hints, the Telegram command menu, list labels and status words, dashboard texts, and labels the user can see later (what `/contexto`, `/historico`, `/parar` or `/log` show, such as "[photo]" or "[document x]" turn labels).
- Never a hard-coded literal for any of these, in either language, not even a short label or a single word: add a key. An f-string that mixes a literal word with values is a hard-coded literal too.
- Plugins can't import `t()`: they keep their own `TEXTS` / `{"en": ..., "pt": ...}` tables, and the `@command` description (and a usage hint with words) is such a dict.
- Ready-made phrases exist in both `data/fallback_phrases.json` (pt) and `data/fallback_phrases.en.json` (en), with the same categories and placeholders (each category has at least 10).
- Exempt by design: text only the LLM reads (prompts, context blocks, lookup results, action descriptions) stays in English; keep it in prompt functions or constants that are clearly LLM-only. Git commit messages stay in English. Second-brain titles, file names and note content (journal lines, "Resumo do dia", "O que eu aprendi hoje") stay in Portuguese whatever the bot language (constants or `t(..., lang="pt")`).
- New code must pass `tests/test_i18n.py` (key and placeholder parity, every literal key used in the code exists, no hard-coded prose in replies/buttons/shown errors/command metadata, plugin tables, fallback phrases). When a change adds a new kind of user-facing text the test does not cover yet, extend the test in the same commit.
