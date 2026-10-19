# Notes for Claude

## Commits

- Never add `Co-Authored-By: Claude ...` or `Claude-Session: ...` trailers to commit messages. Commits are authored by kundlatsch only.
- Keep the bot's own trailers (`UVA-Self-Edit`, `UVA-Self-Code`, `UVA-Feature-Id`, `UVA-Reverts`); `/reverter` depends on them.

## i18n

- Every text the user reads (command replies, `/help` descriptions and usage hints, buttons and their toasts, notices, errors shown in replies, the Telegram command menu, routine/goal reports, review notices, dashboard texts) follows the i18n architecture: it goes through `t()` (`uva/i18n.py`) with the key in both `src/uva/locales/en/` and `src/uva/locales/pt/`, with the same placeholders. Never hard-code user-facing text in one language.
- Plugins can't import `t()`: they keep their own `{"en": ..., "pt": ...}` tables, and the `@command` description (and a usage hint with words) is such a dict.
- Ready-made phrases exist in both `data/fallback_phrases.json` (pt) and `data/fallback_phrases.en.json` (en), with the same categories and placeholders.
- By design: LLM prompts and anything only the LLM reads stay in English; second-brain titles, file names and note content (journal lines, the "Resumo do dia") stay in Portuguese.
- `tests/test_i18n.py` must pass: it checks key and placeholder parity, that every literal key used in the code exists, that replies/buttons/shown errors have no hard-coded prose, the plugin tables and the fallback phrases.
