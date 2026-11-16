# myvec bench

Бенчмарк вибору embedding-моделі та налаштувань sqlite-vec для `myvec` (локальний гібридний пошук по коду та нотатках).
Результат і висновки: [`REPORT.md`](REPORT.md). Сирі дані: `results/`.

## Вимоги
- macOS, `uv`, Python 3.12, Ollama на `http://localhost:11434` з моделями `qwen3-embedding:0.6b`, `bge-m3`, `embeddinggemma`
  (`embed.py` сам робить `ollama pull`, якщо моделі нема).
- ripgrep: або `rg` у PATH, або вбудований у Claude Code (`ARGV0=rg ~/.local/bin/claude ...`; `evalbench.py` сам вибирає).
- Корпус читається лише для читання з `~/projects/{project-b/site,project-b/maximus-libs,project-b/ocr,project-d.se/public_html,project-g}`.

## Відтворення (з каталогу `bench/`)
```bash
uv sync                                              # numpy + sqlite-vec==0.1.9 у bench/.venv
python3 make_golden.py                               # golden.jsonl (45 запитів) + перевірка, що файли існують
python3 corpus.py                                    # emb/chunks.jsonl (~30k chunks, 60 рядків / 10 overlap). Не перезаписує існуючий (--force)
uv run python embed.py qwen3 bge-m3 gemma            # ~25 хв на модель; чекпоінти в emb/<key>_parts/, повторний запуск продовжує
uv run python latency_embed.py                       # warm латентність embedding запиту (Ollama)
uv run python evalbench.py                           # recall@5/10, MRR, FTS/hybrid/ripgrep, латентність sqlite, токени -> results/
uv run python loadtest.py                            # синтетичний sqlite-vec test (50k/200k/1M x512, 200k x1024); БД в tmp/ видаляються
uv run python analysis.py; uv run python throughput_clean.py
uv run python make_tables.py                         # results/tables.md з усіх JSON
```
Латентності мірити на «тихій» машині (без паралельного embedding/indexing).

## Живий бенч (E8-20, E8-21)

Окремо від офлайн-корпусу вище: `live_eval.py` викликає справжній `myvec.search.search()` на справжньому індексі машини, тож міряє продакшн-скоринг (ваги скоупу, маршрути, `path_weight`).

- Вимоги: індекс `~/MyVector/index.db` (читається лише `mode=ro` + sqlite-vec, нічого не пишеться), проєкти з `golden_live.jsonl` у реєстрі (відсутній - пропуск із рядком у `skipped`), доступний embed-сервер (на devbox `query_url`). Демон не потрібен. Ембеддинги запитів кешуються в `bench/tmp/live_qvec.json` (gitignored).
- Запуск з кореня репо: `uv run python bench/live_eval.py [--variants base V3_t0.8 ...] [--project NAME] [--test-path-weight 0.8] [--doc-path-weight 0.9] [--embed-timeout 20]`. За замовчуванням `--test-path-weight 1.0`: варіанти V1-V4 перераховуються над top-50 старої видачі; `--test-path-weight 0.8` проганяє реалізацію з `search()`. `--doc-path-weight` (дефолт 1,0) так само для `doc_path_weight`; варіанти `B_d`/`Bw_d`/`Cw_d` (d ∈ 0,9/0,8/0,7/0,6) і `Qw` - перерахунок над top-50. `--embed-timeout` піднімає таймаут ембеддера лише в скрипті (під навантаженням devbox `query_url` відповідає ~8 с при `QUERY_URL_TIMEOUT` 2 с).
- `golden_live.jsonl`: 67 кейсів (`where-code`, `test-seeking`, `identifier`, `docs-ish`, `arch-code`, `docs-seeking`; `split` dev/holdout; `expected` із завершальним `/` = будь-який файл під текою), написані з читання коду до першого прогону.
- `results/live_metrics.json` (R@5/R@10/MRR, `test_above_code`, `tests@3`, `docs_above_code`, `docs@3` по групах і split) і `results/live_per_query.csv` - лише шляхи й ранги, без тексту чанків і пам'яті.

## Файли
| Файл | Призначення |
|---|---|
| `corpus.py` | відбір файлів (`git ls-files`, виключення) і chunking |
| `make_golden.py`, `golden.jsonl` | золотий набір (написаний ШІ після читання коду) |
| `common.py` | моделі/префікси, Ollama `/api/embed`, MRL-усічення |
| `embed.py` | embedding chunks + запитів, npy-чекпоінти |
| `evalbench.py` | вектор / FTS5 (unicode61, trigram) / hybrid RRF k=60 / ripgrep, метрики |
| `latency_embed.py` | латентність embedding запиту |
| `loadtest.py` | sqlite-vec: f32 full scan / partition key / int8 / bit+rescoring |
| `make_tables.py` | JSON -> markdown таблиці |
| `analysis.py` | bootstrap CI та "adaptive" роутер з per_query.csv -> `results/analysis.json` |
| `throughput_clean.py` | повторний швидкий замір throughput embedding |
| `live_eval.py`, `golden_live.jsonl` | живий бенч поверх `search()` (розділ вище) |
| `results/` | `metrics.json`, `per_query.csv`, `latency_*.json`, `tokens.json`, `loadtest.json`, `embed_*.json`, `tables.md` |
| `emb/`, `tmp/` | великі артефакти (embeddings .npy, sqlite БД) — не комітити |
