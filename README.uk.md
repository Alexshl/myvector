# myvec

Локальний CLI, що дає Claude Code гібридний (векторний + повнотекстовий) пошук по ваших проєктах
і дворівневу пам'ять (глобальна + по проєкту). Усе живе в одному SQLite-файлі; пам'ять
дублюється як Obsidian-сумісні markdown-файли (джерело істини).

## Встановлення

Одна команда (macOS або Linux; ідемпотентна, повторний запуск = оновлення):

```bash
git clone https://github.com/Alexshl/myvector ~/projects/myvector
~/projects/myvector/install.sh          # uv, ollama + embeddinggemma, myvec, skill /myvector, hooks, демон
```

`install.sh [--no-daemon] [--no-hooks]`. На Linux додатково: inotify-ліміти (`/etc/sysctl.d/60-myvec-inotify.conf`)
і `loginctl enable-linger` (через `sudo -n`; без безпарольного sudo лише попереджає). Вручну те саме:

```bash
uv tool install --reinstall --python 3.12 ~/projects/myvector   # команда `myvec`; --reinstall обовʼязковий при оновленні (версія не змінюється, uv інакше бере закешовану збірку)
ollama pull embeddinggemma                  # модель за замовчуванням (768 dims, контекст 2048 токенів)
myvec install-hooks                         # усі hooks (старт, правки, сесії) у ~/.claude/settings.json (з бекапом); після оновлення myvec запустити знову
myvec install-daemon                        # (опційно) сервіс `myvec serve` (LaunchAgent / systemd user unit): теплий пошук, watchers, веб-API
```

Розробка: `uv sync && uv run pytest -q`.

### Ембеддинги без Ollama: llama.cpp `llama-server` (Linux/CPU, devbox)

Замість Ollama можна взяти будь-який OpenAI-сумісний `/v1/embeddings` (llama.cpp `llama-server`, vLLM). У `~/MyVector/config.toml`:

```toml
ollama_url = "http://127.0.0.1:11435"   # адреса сервера (назва ключа історична)
embed_api = "openai"                    # "ollama" (за замовчуванням, /api/embed) або "openai" (/v1/embeddings)
```

Сервер для `embeddinggemma` - `contrib/llama-embed/` (GGUF `embeddinggemma-300M-Q8_0.gguf`, `--embeddings --pooling mean`,
2048 токенів на слот) і systemd user unit:

```bash
cp contrib/llama-embed/embed-server ~/bin/ && cp contrib/llama-embed/embed-server.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now embed-server
curl -s http://127.0.0.1:11435/health                # {"status":"ok"}
```

- Вектори сумісні з Ollama-індексом (переносити індекс із Mac можна без переембеджування): на 300 випадкових чанках
  косинус до збережених Ollama-векторів 0,9994 у середньому (мін. 0,997), top-10 на запитах збігається.
- `llama-server` не обрізає задовгий вхід, а повертає помилку (400 `exceed_context_size_error` або 500 «too large to process»,
  якщо вхід більший за `-ub`). `OpenAIEmbedder` тоді обрізає такі тексти до контексту слота через `/tokenize` + `/detokenize`
  (зберігає початок, як Ollama) і повторює пакет.
- Демон з `embed_api = "openai"` не шле keep-alive, а доступність перевіряє через `/health`.
- `install.sh` при `embed_api = "openai"` у конфігу пропускає встановлення Ollama.
- **Окремий сервер для запитів (`query_url`, опційно, E8-09, ADR-039).** Один `llama-server` обслуговує і індексацію демона, і пошук:
  запит (1 короткий текст) стоїть у черзі позаду батчів індексації (head-of-line blocking), на devbox під синком p50 пошуку
  був ~10 с. Другий інстанс лише для запитів прибирає чергу:

  ```toml
  query_url = "http://127.0.0.1:11436"   # "" (за замовчуванням) = запити йдуть на ollama_url, як раніше
  ```

  ```bash
  cp contrib/llama-embed/embed-query-server ~/bin/ && cp contrib/llama-embed/embed-query-server.service ~/.config/systemd/user/
  systemctl --user daemon-reload && systemctl --user enable --now embed-query-server
  curl -s http://127.0.0.1:11436/health
  ```

  Сервер слухає `127.0.0.1:11436`, `-np 1 -c 2048 -b 2048 -ub 2048`. **Модель - той самий файл, що й в основному сервері**:
  інший файл/квант дасть мовчки інші вектори, і пошук по індексу поламається без помилок (код цього не перевіряє).
  Встановлення ручне (власник), `myvec` сервіси сам не ставить; `query_url` читається на кожен запит, перезапуск демона не потрібен.
  Поведінка: `embed_query` іде на `query_url` з таймаутом 2 с; при помилці - одна спроба на основний `ollama_url` (10 с),
  далі `fts-fallback`; найгірший випадок (сервер приймає з'єднання, але висне) ~12 с. Fallback **тихий**: мертвий query-сервер
  видно лише як `embed query` від ~2 с у `myvec search --debug`. `embed_docs` і запис пам'яті завжди ходять на `ollama_url`.

### Перенесення на іншу машину (напр. Mac → devbox)

Вектори адресуються git blob SHA, тож скопійований індекс не переембеджується - лише переписуються абсолютні шляхи:

```bash
# на старій машині: консистентна копія БД (демон може працювати)
sqlite3 ~/MyVector/index.db ".backup /tmp/index.db"
rsync -a /tmp/index.db ~/MyVector/config.toml ~/MyVector/registry.toml new-host:MyVector/
rsync -a ~/MyVector/vault/ new-host:MyVector/vault/      # пам'ять (git-репо vault)
# на новій: install.sh, потім
myvec relocate --from /Users/olduser --to /home/newuser --dry-run   # показати, що зміниться
myvec relocate --from /Users/olduser --to /home/newuser
myvec install-daemon                         # перезапуск демона з новими шляхами
for d in $(myvec status | sed -n 's/^project .*(\(.*\)).*/\1/p'); do (cd "$d" && myvec sync); done
myvec import                                 # імпорт auto-memory/CLAUDE.md з новими шляхами
```

Проєкти мають лежати на новій машині там само відносно home (разом із `.myvector.toml`). `sync` доембеджує лише
файли, що відрізняються.

## Швидкий старт

```bash
cd ~/projects/project-b
myvec init --name project-b                     # пише .myvector.toml, реєструє, індексує у фоні
myvec search "де рахується ПДВ"             # код + пам'ять
myvec remember "Деплой іде через pipeline X" --pin
myvec status
```

## Старт нового проєкту

Порожня тека -> Claude Code -> `/myvector`. Skill (`skill/myvector/bootstrap.md`) веде інтерв'ю (що за проєкт, стек, команда тестів, лінтер, UI і як його перевіряти, машини, перша група тікетів, режим sync), показує план файлів, а після згоди викликає `myvec scaffold`, `myvec init` і `git`. Документи нового проєкту завжди англійською; GitHub-репо (`gh repo create --private`) - лише окремим питанням і за явним «так».

Що створюється: `CLAUDE.md`, `.claude/agents/*` (planner, coder, tester, reviewer, docs-writer, debugger, qa), `.claude/rules/{workflow,tickets}.md`, `.claude/settings.json` (злиття), `docs/todo/` (перша група, `INDEX.md`, шаблон тікета), `docs/decisions.md`, `docs/lessons.md`, дописаний `.gitignore`; далі `myvec init`, `git init` і перший коміт лише згенерованими файлами + `.myvector.toml`. Слоти, що найбільше різняться між проєктами: `adr_path` (шлях до файлу ADR, дефолт `docs/decisions.md`), `parallel_lines` (ліміт паралельних ліній, дефолт `2`), `branch_pattern` (дефолт `t/<ticket-id>-<slug>`, має містити `<ticket-id>`), `pitfalls` (зміст розділу пасток у CLAUDE.md: рядок або список); слоти-команди (`test_cmd`, `lint_cmd`, `tickets_check_cmd`, `tickets_index_cmd`) - одна команда без бектиків і пояснення в дужках (інакше код 2).

**Тека з кодом.** `/myvector` у теці з кодом спершу викликає `myvec scaffold --detect --json` (нічого не пише): стек, команди тестів і лінтера, UI, маркер, наявний процес, формат тікетів (`none|canon|foreign`), стан git і мова документів; читає лише білий список файлів, ніколи `.env*`/`.dev.vars`. Skill питає тільки невизначене, показує `--dry-run --diff --json` і по кожному конфлікту просить рішення: `--keep PATH` (лишити своє), `--overwrite PATH` (шаблон, `.bak-<ts>`), `--append CLAUDE.md` (дописати Stack, Pitfalls і розділ процесу; наявні розділи пропускаються). Git-правила проєкту - слоти відповідей: `vcs_host` (`github|gitlab|none`), `base_branch`, `parallel_mode` (`worktree|none`), `no_unrequested_commits` (ADR-021); skill питає їх у інтерв'ю для наявного проєкту. Чужі тікети (`foreign`) не чіпаються (`--no-tickets`, міграція - `myvec ticket migrate`). Запис транзакційний: відбитки цілей звіряються між плануванням і записом **в одному запуску** - якщо тека змінилась за цей час (у т.ч. з'явився файл чи симлінк), код 3 і нічого не записано; помилка посередині відкочується (`.bak` видаляється лише для відновлених файлів). Зміни між окремим `--dry-run` і запуском **не виявляються**: конфлікти рахуються заново, а правку власника, перезаписану через `--overwrite`, лишає лише `.bak-<ts>` - тож після diff і згоди запускати одразу, не редагуючи файли. `--keep`/`--append` діють на рядки `conflict`/`merge`; `.bak-*` лишаються в робочому дереві невідстеженими. Коміт - лише згенеровані шляхи (pathspec), чужі зміни в нього не потрапляють. У наявному проєкті перша група тікетів не питається й не створюється; `--keep PATH` діє й на рядок `create` (файл не створюється).

Ручне використання без skill:

```bash
myvec scaffold --answers answers.json --root ./demo --dry-run --json   # лише план: create/same/merge/conflict
myvec scaffold --answers answers.json --root ./demo                    # запис
myvec scaffold ... --overwrite CLAUDE.md                               # дозволити перезапис (по прапору на файл)
myvec scaffold --detect --json --root ./proj                           # факти про теку з кодом (нічого не пише)
```

Обов'язкові поля `answers.json`: `project`, `description`, `stack`, `test_cmd`. Конфлікт без `--overwrite` - код 3 і нічого не записано (все або нічого); відповіді із секретом - код 2. Перезаписані й злиті файли спершу копіюються в `.bak-<ts>`. Шаблони лежать у `src/myvec/scaffold/templates/` (слоти `{{key}}` та `<!-- if:key -->`); `.claude/` цього репо є їхнім рендером із `tests/fixtures/scaffold/myvec.answers.json` (ADR-021). Тека з уже наявним кодом (виявлення стеку, злиття з чужим `CLAUDE.md`) - окремий тікет E5-06.

**Версія процесу й стан (ADR-028).** `scaffold.PROCESS_VERSION` - номер версії шаблонів (піднімається на кожну зміну рендеру; тест-сторож `tests/fixtures/scaffold/process_version.json` звіряє дайджест рендеру). Кожен запис пише в проєкт `.myvec/scaffold.json` (комітити разом з `.claude/`): `version`, сирі `answers` і `files` - `mode` (`owned|keep|append`), `sha256` та `base` (копія рендеру, база для майбутнього 3-way). Стан іде рядком плану в тій самій транзакції; повторний запуск без змін - усе `same`, ті самі байти, без `.bak`. `--dry-run` рядок показує, не пишучи. Прочитати: `myvec scaffold --state [--root DIR] [--json]` (нічого не пише, exit 0): `status` = `none` (проєкт без стану, «невідома версія») | `current` | `outdated` | `newer` | `invalid`, плюс `version`, `current`, `files`, `reason`; answers і бази не виводяться. Проєкти, розгорнуті до E0-07, мають `none`, поки bootstrap «Existing code» не створить стан; `--update` їх відхиляє. **SessionStart (E0-10, ADR-036):** `hook session-start` при `outdated` додає один рядок `[myvec] process: ... v{N} ... v{M}. Suggest to the owner: say "онови конвенцію"` (лише числа й фіксований текст; тільки основний checkout, без `agent_id`, не worktree; помилка - тиша). `myvec.scaffold` хук імпортує лише коли `.myvec/scaffold.json` існує. Нових хуків не треба, `install-hooks` не перезапускати.

**Оновлення процесу (`myvec scaffold --update [--write] [--json]`, ADR-035).** Перерендерює процес проєкту поточними шаблонами за answers зі стану (або `--answers FILE`) і зливає 3-way: ours = файл на диску, base = `files[rel].base` зі стану, theirs = новий рендер; злиття - `git merge-file -p` в ізольованому оточенні (приватна tmp-тека, `GIT_CONFIG_GLOBAL=/dev/null`, `HOME` у tmp; потрібен `git`). Без `--write` лише план і diff (з маскуванням секретів; файл із hard secret - `diff_withheld`), нічого не пишеться; `--write` - транзакційний `apply` (ADR-023) і новий стан останнім рядком. Дії по файлу: `same`, `unchanged`, `update` (диск = база), `merge`, `conflict`, `keep` (keep-файл, якого нема на диску / не звичайний файл / не UTF-8: нічого зливати, не створюється), `orphan` (шаблон файла більше не має: лишається на диску, зі стану прибирається), `skip` (новий файл під `docs/todo/`), `create`. Конфлікт ніколи не пишеться (маркерів у проєкті нема), розв'язується `--keep PATH` (лишити диск; ручне злиття = відредагувати й `--keep`) або `--overwrite PATH` (новий рендер). Файли, що замінюються, спершу копіюються в `.bak-<ts>` (шаблон `*.bak-*` відфільтровано з індексу й у `.gitignore` проєкту). Коди: 0 - перегляд, записано або вже актуально; 2 - нема стану/`invalid`/`newer`, невалідні answers, нема git, несумісні прапорці (`--append`, `--no-tickets`, `--ticket-template`, `--detect`, `--state`, `--dry-run`, `--diff`; `--write` без `--update`); 3 - нерозв'язані конфлікти (нічого не записано) або тека змінилась між знімком і записом. `--json`: `{status: preview|written|conflict|current, from_version, to_version, files, conflicts, write, backups?}`; `write` - шляхи для коміту pathspec-ом. **Файли `mode: keep` (E0-11, ADR-038)** зливаються тим самим 3-way, що owned (база = `base` зі стану, яка є для кожного запису); конфлікт keep-файлу блокує запис (код 3). Розв'язання: `--keep PATH` (мод лишається `keep`, база = новий рендер; приймається й на чистому `merge`/`update`, щоб відхилити його) або `--overwrite PATH` (шаблон, `.bak-<ts>`, мод стає `owned`; на відсутньому keep-файлі створює його з шаблону). Рядки keep-файлів у `--json` мають `"mode": "keep"`. Сценарій для власника проєкту - skill `/myvector`, розділ «Update the process / convention».

**Правило пошуку й `--adopt` (E8-12, ADR-041).** Правило «спершу `myvec search`» - окремий статичний шаблон `.claude/rules/search.md` (англійською, без слотів; частина рендеру з `PROCESS_VERSION` 3; з E8-14 - 5): Claude Code підвантажує `.claude/rules/*.md` головній сесії й субагентам, тож один файл доставляє правило без правки агентів; рядок Search у General rules і `CLAUDE.md` шаблону лише посилається на нього. Проєкти зі станом отримують файл через `--update` (`create`; власний `search.md` дає `conflict` - розв'язати `--keep PATH`). Проєкту без стану (`none`) файл додає `myvec scaffold --adopt [--root DIR] [--write] [--json]`: без `--write` лише план і текст правила, з `--write` - транзакційний `apply` (ADR-023) створює тільки `.claude/rules/search.md`; **стан `.myvec/scaffold.json` не пишеться** (база з поточних файлів змусила б наступний `--update` переписати кастомні агенти шаблоном). Наявний файл, байт-у-байт рівний одному зі старих рендерів (реєстр `problems.SEARCH_RULE_OLD`: sha256 сирих байтів -> перша `PROCESS_VERSION`; E8-17, ADR-046), - дія `update`: без `--write` план з версією, з `--write` `apply` перезаписує його, лишаючи `search.md.bak-<ts>` (CLI друкує нові бекапи), стан так само не пишеться. Будь-який інший наявний файл (правка власника, власний текст) - рядок `foreign`, не перезаписується (`--overwrite` нема), текст злити вручну; вже готовий - `current`. Код 2: є стан (`current`/`outdated` - `--update`; `newer`/`invalid`), нема `.claude/` чи це симлінк, секрет/слот у шаблоні, несумісні прапорці (`--update`, `--answers`, `--detect`, `--state`, `--overwrite`, `--keep`, `--append`, `--no-tickets`, `--ticket-template`, `--dry-run`, `--diff`); код 3 - тека змінилась між планом і записом. `--json`: `{status: preview|written|current|foreign, version, backups, files, write, text}` (`version` - лише для `update`). Коміт - шляхи з `write`.

**Пошук у ролях і стислий вивід (E8-14, ADR-045; `PROCESS_VERSION` 5).** Шаблони `planner` і `reviewer` починаються з пошуку: `myvec search` (+ `--kind docs`) за темою/зміненими символами, якщо в теці чи вище є `.myvector.toml` (інакше fallback на Read/Grep), а в `## Log` тікета - рядок `search: "<запит>" -> <файли>` (без секретів і приватних шляхів). Статичний `search.md` тепер згадує стислий вивід у не-tty, `--expand` і `--json` (ADR-043). Проєкти зі станом отримують зміни через `scaffold --update` (кастомний planner/reviewer дає конфлікт, код 3); прийнятий через `--adopt` `search.md` оновлює повторний `--adopt` (E8-17), якщо файл не правили; відредагований лишається `foreign`.

## Команди

| Команда | Що робить |
|---|---|
| `init [--name] [--parent] [--include] [--exclude] [--share-global/--no-share-global] [--fg]` | маркер `.myvector.toml` у cwd, реєстрація, фонова синхронізація (`--fg` - на передньому плані); додає `.myvector.toml` до **глобальних git-excludes** (див. нижче). Відмовляє, якщо шлях проєкту вже зайнятий іншим існуючим каталогом (два `api` не заміняють один одного) - тоді порадить `--name <батьківський-каталог>-<ім'я>`; некоректні імена (`..`, абсолютні, `global`/`_archive`/`_topics`) відхиляються |
| `sync [--bg] [--quiet]` | індексація поточного проєкту (усі репозиторії, поточна гілка); в кінці - життєвий цикл гілкових пам'ятей. Якщо lock зайнятий, чекає до 2 с (інший sync міг щойно завершуватись) і лише тоді друкує "sync already running" |
| `search "q" [-q "q2" ...] [--k 10] [--scope repo\|project\|all] [--by-project] [--project <назва\|шлях>] [--kind code\|memory\|docs\|all] [--mode auto\|vector\|hybrid] [--compact\|--expand] [--full] [--json] [--debug]` | пошук (стислий вивід за не-tty, кілька `-q`, рядок `total in index` для ідентифікатора - «Архітектура»; `--by-project` і `--project` - крос-проєктні знання, див. «Архітектура»); спершу пробує демон (`POST /api/cli/search`, connect-таймаут ~150 мс), інакше напряму - вивід однаковий |
| `remember "text" [--global] [--repo] [--branch] [--tag x] [--pin] [--supersedes ID]` | зберегти пам'ять (за замовчуванням - у scope проєкту); схожа (cosine >= 0.92) у тому ж scope **і з тим самим фільтром гілки** (NULL з NULL) - оновлюється, `branch` існуючої пам'яті ніколи не змінюється. Якщо запис у БД не вдався - файл у vault відкочується |
| `forget ID` / `pin ID [--unpin]` / `promote ID` | архівація / закріплення / перенесення у глобальний scope (`vault/global/`) |
| `forget-project [TARGET] [--missing] [--yes]` | прибрати з індексу проєкт, чий корінь зник (ADR-044); не плутати з `forget ID` (архівація пам'яті). **Без `--yes` - dry-run**: для кожної цілі друкує стан кореня й кількість файлів та чанків. Також рядок `dry run: the sweep would also drop N vectors, M chunks, K blobs` - що прибере підмітання сиріт (`orphan_counts`; числа дорівнюють `swept ...` з `--yes`, давні сироти теж). З `--yes` (під `sync_lock`) видаляє repos проєкту з векторами, тікети, рядок `projects`, корінь із `registry.toml` і чанки-сироти. **Пам'ять у vault не видаляється**; дочірні проєкти, історія сесій/прогонів/токенів і маркер теж цілі. Стан кореня: `present`, `gone` (теки нема, батьківська непорожня), `unreachable` (батьківської нема або вона порожня/нечитабельна - це може бути відключений диск чи чужа ОС). `--missing` бере лише `gone`; `unreachable` (і `present`) забувається лише явною ціллю. `TARGET` - шлях проєкту (`projects.path`) або тека кореня; **назва проєкту має пріоритет над теку з тим самим іменем у cwd** (dry-run показує `root=`). Рівно одне з `TARGET`/`--missing` (інакше код 2); невідома ціль або зайнятий `sync_lock` - код 1. Без `index.db` друкує «no index yet» і нічого не створює. `status` і `gc` друкують підказку, коли є відсутні корені; **автоприбирання нема** (ні в демоні, ні в `gc`, ні в `rebuild`) |
| `mode auto\|always\|off [--project PATH] [--window-hours N]` | режим синхронізації проєкту та вікно активності |
| `serve [--port]` | демон на `127.0.0.1:7878` (передній план) |
| `install-daemon [--no-load]` / `uninstall-daemon` | macOS: LaunchAgent `~/Library/LaunchAgents/com.myvec.serve.plist` + `launchctl bootstrap`; Linux: `~/.config/systemd/user/myvec.service` + `systemctl --user enable --now` |
| `relocate --from OLD --to NEW [--dry-run]` | переписує префікси шляхів (projects.root, repos.dir, memories.vault_file, registry.toml) після перенесення індексу на іншу машину; вектори не чіпає |
| `import` | read-only імпорт `~/.claude/projects/*/memory/*.md`, `CLAUDE.md`, `graphify-out/GRAPH_REPORT.md`. Frontmatter (`---`, `name`/`description`/`metadata`) нотаток Claude не індексується: перший рядок пам'яті - `description` (або `name`), далі тіло без frontmatter. Повторний `import` оновлює рядки файлу на місці (без дублікатів) |
| `rebuild [--no-import]` | відновити таблиці пам'яті з vault і перезапустити sync (index.db можна видалити будь-коли) |
| `reembed` | перерахувати всі вектори після зміни `model`/`dims` у `config.toml` (одна транзакція: збій посередині лишає старий індекс цілим) |
| `ticket new\|check\|index\|claim\|log\|migrate` | тікети канону v1 (див. «Тікети»): `new --title --slug [--parent --kind --id --size --deps]` створює файл; `check [--strict]` перевіряє (`--strict`: попередження теж помилка; поради здоров'я друкуються тегом `HEALTH`, рахуються окремо в підсумку «N помилок, M попереджень, K порад» і не впливають на exit code, `--strict` теж; `no_session` тут не рахується, бо потрібні сесії); `index` перегенеровує блоки між `<!-- generated:start/end -->`; `claim [ID] [--session --release]` - хто з сесій бере тікет; `log ID --agent X "текст"` дописує в `## Log` (секрети маскуються, hard secret відхиляється). `migrate [--dir D] [--detect [--json]\|--brief [--out F]\|--check\|--fix [--write] [--add-goal-heading]\|--split [--write]]` - міграція старого формату на канон (ADR-024): знаходить кандидатів і формат (лише ключі шапки, статуси, назви розділів), друкує бриф для агента проєкту, `--check` показує, що лишилось (exit 0 лише коли все), `--fix` - dry-run механічних правок шапки (`epic` -> `parent`, `id` синтетичних груп), `--write` застосовує, `--split` розбиває формат одного файлу на теку тікетів (див. «Міграція тікетів»). `index [--index-name N]` - інше ім'я файла індексу. Усі приймають `--root`, `--dir` (де є) |
| `gc` | видалити репозиторії, чий каталог зник (з маніфестами; пишеться в `logs/sync.log`), маніфести гілок, яких більше немає локально, блоби/чанки/вектори без посилань; життєвий цикл пам'ятей. `--vacuum` — потім стиснути `index.db` (повертає диску місце після великих видалень; пошук на цей час чекає) |
| `topics` | офлайн-кластеризація пам'яті (numpy) -> `vault/_topics/<scope>.md` з wikilinks |
| `memory-audit [--json] [--candidates]` | (E8-04, ADR-034) read-only аудит складу пам'яті: з'єднання `mode=ro`, без ембеддера/демона, нічого не пише. За замовчуванням лише агрегати (source × global/project, розміри скоупів без назв, вік за місяцями, pinned/branch/archived, дублікати імпорту між скоупами). `--candidates` додає id + перший рядок (обрізаний, маскований) vault project-спогадів, схожих на інші скоупи (cos ≥ 0,75), для ручного `myvec promote ID`; призначено для локального перегляду власником, не для тікетів/логів |
| `status` / `context` | стан індексу (режими, вікна активності, masked/dropped секрети) / ≤300 токенів контексту для старту сесії |
| `install-hooks [--settings PATH]` | ідемпотентно додає hooks у settings.json (бекап оригіналу): SessionStart, PostToolUse (Edit/Write/MultiEdit), UserPromptSubmit, Stop, Notification, SessionEnd, PreToolUse/PostToolUse `AskUserQuestion`, async-пульс PostToolUse `*`, SubagentStart (підказка пошуку для субагентів), PreToolUse `Bash|Grep` (`pre-search`). Нові hooks (зокрема SubagentStart, pre-search: після оновлення myvec повторно запустити `myvec install-hooks`) з'являються лише після повторного запуску |
| `hook session-start\|post-edit\|session-event\|session-pulse\|subagent-start\|pre-search` | точки входу hooks (stdin JSON, ~30-40 мс, завжди exit 0, помилки у `logs/hook.log`). `subagent-start` (E8-01, ADR-031) віддає субагентам фіксований рядок «спершу `myvec search` (ідентифікатори теж), `--by-project` для «чи робили ми це деінде», grep лише для точних рядків у вже знайдених файлах і повного підрахунку» через JSON `additionalContext`, лише у проєкті з `.myvector.toml`; той самий рядок головна сесія бачить у `myvec context`. `pre-search` (E8-11, ADR-040): перед `grep -r`/`rg`/`ag`/`ack` (Bash, також `xargs grep`) або інструментом Grep по теці проєкту дає одне речення з готовою `myvec search "<слова з шаблону>"` (`additionalContext`, виклик не блокується). Мовчить: grep по файлах, цілі поза коренем проєкту й теки `logs`/`log`/`tmp`, підрахунок (`-c`, `\| wc`), Glob/`find`, поза проєктом; після `myvec search`/`recall` - 30 хв, не частіше раз на 10 хв на (сесію, агента). Стан - лише два числа в `state/presearch/`, текст команди не зберігається |
| `sessions [--all] [--json]` | сесії Claude Code зі станами; `--all` - разом із завершеними > доби тому. Без демона сама складає журнал у таблицю |
| `tokens [--ticket ID] [--project P\|.] [--session SID] [--by agent,model,effort,ticket,project,session,day] [--since 7d\|24h\|YYYY-MM-DD] [--context] [--json] [--no-scan]` | витрати токенів із транскриптів Claude Code (див. «Витрати токенів»). Перед звітом сам доскановує, `--no-scan` вимикає `--session SID --context` - розмір контексту (input + cache read + cache write) останнього запиту головного транскрипту, без саб-агентів, для порогу передачі ~200 тис.; з `--json` `{session, context}`, без даних `context: null` і код 1; сумісний лише з `--json`/`--no-scan` (інакше код 2) |
| `searches [--days N] [--project P\|.] [--json] [--no-scan]` | метрика використання пошуку по проєктах (ADR-029): частка `myvec` проти self-пошуку (`grep/rg/find/Grep/Glob`) з транскриптів (`tokens.db.tool_calls`), промахи, нульові видачі, p50 латентності з `logs/searches.jsonl`; без тексту запитів; не враховуються рядки з cwd чужої ОС (`foreign`) і до відліку проєкту (`pre_onset`; відлік `onset` = мін(`registered_at`, перший myvec-виклик)), `--project` приймає ім'я, `.` або наявний шлях усередині проєкту (також worktree; корінь, записаний через симлінк після `relocate`, зіставляється за `abspath`, `realpath`, `PWD` і реальним коренем; зареєстрований проєкт без викликів - нульовий рядок, невідоме ім'я/неіснуючий шлях - код 1); поля `sessions_explore` (поки що сесії з будь-яким self), `sessions_myvec_explore`, `p50_ms_24h` (за добу поряд із `p50_ms` за вікно), `pre_onset`, `onset`, `total.foreign`; CLI друкує підвал «не враховано: N до відліку, M з іншої ОС» і колонки «p50 доба», «до відліку» (ADR-042); підвид self-пошуку (E8-16, доповнення до ADR-042): колонки «розвідка» (`explore`+`exact`+некласифіковані), «частка (розв.)» = `myvec / (myvec + explore + exact + некласифіковані)` (нижня межа; `logs/count/file/name` - законне використання grep, поза знаменником), «класиф.» (частка self з міткою); підвал завжди має рядки «self за підвидом», «читання доків» (Read доків, grep по доках, `myvec --kind docs`) і «некласифіковано: N» (транскрипти видалено або команда нерозбірна; ці виклики лишаються в знаменнику); у `--json` - `self_by_kind` (+`unclassified`), `classified`, `share_explore`, `doc_reads`, `self_doc`, `myvec_docs`; маршрути `vector\|hybrid\|fts-fallback` (колонка «FTS-fallback» - пошуки, де вектор був недоступний, і запит пішов лише по FTS; у `--json` - `fts_fallback` і `routes`) |
| `measure [--ticket ID] [--agent NAME] [--label TEXT] [--interval S] -- <cmd>` | запускає команду й міряє дерево процесів (див. «Вимірювання прогонів») |

## Де що лежить (`MYVEC_HOME`, за замовчуванням `~/MyVector`)

```
index.db        SQLite (WAL, chmod 600): чанки, вектори, FTS, маніфести, пам'ять
config.toml     model, dims, ollama_url, embed_api, keep_alive, префікси моделей, ваги scope, weight_softness, test_path_weight (0.8; 1 = вимкнено), doc_path_weight (0.9; 1 = вимкнено), lessons_project ("vector-local"), output_budget_tokens,
                expand_top, expand_lines, k, max_memory_hits, import_weight, window_hours, daemon_port,
                load_enabled (true), load_sample_s (5, мін. 1), load_raw_hours (24), load_minutes_days (30), load_temp_sensor (""), load_alert_pct (85), load_alert_minutes (5; не більше 55 і не більше місткості кільця 720·load_sample_s/60 − 1), test_regression_pct (25), test_regression_runs (10), test_regression_min_runs (3), test_regression_min_abs_s (2),
                tokens_enabled (true), tokens_scan_s (60), tokens_scan_budget_mb (512), claude_projects_dir ("")
tokens.db       SQLite витрат токенів (ADR-013, chmod 600), НЕ похідна: див. «Витрати токенів»
load.db         SQLite навантаження ноди (ADR-012): сирі знімки 24 год, хвилинні агрегати 30 днів; не похідна від vault.
                `relocate` її не переписує (шляхи проєктів `project_path` лишаються старими)
registry.toml   список коренів проєктів (щоб index.db можна було відтворити)
sync.lock       flock: один sync одночасно (CLI, hooks і демон; інший друкує "sync already running"); власник пише в файл свій pid,
                тож `is_locked()` (hooks) перевіряє pid і не бере lock сам
vault-git.lock  flock: автокоміти vault серіалізуються (CLI, демон, життєвий цикл)
daemon.json     port, pid, CSRF-токен демона (chmod 600; пише `serve`, читає CLI)
activity        черга звернень від hooks, якщо БД була зайнята (демон/`status` вливають у projects.last_access; вливання
                безпечне при одночасних викликах - кожен забирає чергу під унікальним іменем)
vault/          git-репозиторій: global/*.md, projects/<шлях проєкту>/*.md, _archive/, _topics/
logs/           sync.log, hook.log, hook-sync.log, vault-git.log, queries.jsonl (ротація: останні ~2 MB + одна копія `.1`),
                events.jsonl (ротація ~3 MB), searches.jsonl (лог пошуків без тексту запиту, ADR-029; ротація 8 MB × 2, 0600; бекапити), daemon.*.log
state/searchlog.salt  сіль HMAC-хешу запитів у `searches.jsonl` (0600; не втрачати без потреби - зміниться хеш повторів)
state/test_runs.jsonl  журнал прогонів `measure`, якщо БД була зайнята (демон і наступний `measure` переносять у `test_runs`)
```

`embed_api = "openai"` - брати ембединги з OpenAI-сумісного `/v1/embeddings` за адресою `ollama_url` (llama.cpp
`llama-server --embeddings`, vLLM) замість Ollama `/api/embed`; демон тоді перевіряє `/health` і не тримає модель
прогрітою, а `install.sh` не ставить Ollama. Вектори сумісні з Ollama-індексом тієї ж моделі (devbox: llama.cpp з
`embeddinggemma-300M-Q8_0.gguf` дає ті самі бали, що Ollama на Mac).

`MYVEC_EMBEDDER=fake` - детермінований хеш-ембедер без Ollama (для тестів). `MYVEC_LAUNCHAGENTS_DIR` / `MYVEC_SYSTEMD_DIR` - інший
каталог для plist / unit (тоді `launchctl` / `systemctl` не викликаються).
Якщо `model`/`dims` у конфігу відрізняються від збережених у `meta`, `search`/`sync` відмовляються
працювати і просять `myvec reembed`.

## Архітектура

- **Проєкти.** Корінь = каталог із `.myvector.toml`; поточний проєкт - найближчий предок cwd із маркером.
  Шлях проєкту - сегменти `[A-Za-z0-9._-]+` без `.`/`..`/порожніх, не абсолютний, перший сегмент не
  `global`/`_archive`/`_topics`; маркер з некоректним ім'ям відхиляється з поясненням (hooks мовчки виходять з 0).
  Шлях проєкту належить одному кореню: другий каталог з тим самим ім'ям не перехоплює репо й пам'ять першого;
  "каталог переїхав" - лише якщо старого каталогу вже нема. Файли дочірнього проєкту (вкладений маркер) батьківське
  репо не індексує вдруге.
  Шлях проєкту: `project-b`, піддиректорії з `.git` (глибина ≤ 4) стають репо `project-b/site` тощо; файли поза
  під-репо належать псевдо-репо = кореню проєкту (гілка `-`). Вкладені маркери дозволені (дочірній
  проєкт успадковує шлях `батько/ім'я`). `exclude = ["site-cloud"]` у `.myvector.toml` (gitignore-синтаксис)
  повністю пропускає каталог: і при пошуку репозиторіїв, і в обході псевдо-репо, і у watchers.
- **Індексація (branch-aware, content-addressed).** Список файлів: `git ls-files -s` (sha блобів) +
  untracked + `git status` для змінених (sha рахується з вмісту, кеш `file_state` по mtime/size).
  Для не-git каталогів - обхід із `.gitignore` + deny-list. Маніфест `branch_files(repo, branch, path, blob_sha)`
  порівнюється зі збереженим; ембедяться лише блоби, яких ще не було в жодному репо/гілці. Перемикання
  гілки майже безкоштовне. Ембединг, обхід файлів і хешування відбуваються поза транзакціями (кеш `file_state` пишеться
  короткими `BEGIN IMMEDIATE`-батчами), запис - короткими батчами. Ключ блоба - sha реально прочитаних байтів: файл,
  змінений між переліком і читанням, індексується під своїм sha (манифест виправляється, репо позначається застарілим),
  а не під старим; `unreadable` ніколи не зберігається.
- **Фільтри.** vendor, node_modules, dist, build, templates_c, graphify-out (лише GRAPH_REPORT.md), `.env*`, `.dev.vars*`,
  `*password*`, `*secret*`, `*.pem`, `*.key`, `id_*`, `*.sql`, `*.sqlite`, `*.min.*`, lock-файли, бінарні
  (NUL), > 1 MB.
- **Секрети: маскуємо, не викидаємо.** Літеральне значення в будь-якому ідентифікаторі, що *містить*
  `password|passwd|pass|pwd|secret|token|api_key|access_key|private_key|secret_key|auth_key|client_secret|dsn`
  (`SECRET_KEY = '...'`, `AWS_SECRET_ACCESS_KEY=...`, `$db_pass = '...'`, `'password' => '...'`, `"api_key": "..."`),
  а також у PHP `define('DB_PASSWORD', '...')` / `define("..._KEY", "...")` замінюється на `•••` (зберігається й
  ембедиться вже маскований текст; так само `symbol` чанка - заголовки markdown - і заголовки імпорту). Змінні (`$token`), виклики (`$this->getToken()`), `env()/getenv()/config()`,
  ідентифікатори (`self.token = refresh_token`), шаблони (`${X}`, `{{x}}`) не чіпаються. Чанк **викидається
  цілком** лише для приватних ключів (`-----BEGIN ... PRIVATE KEY-----`; у файлі шукаються діапазони BEGIN..END і
  відкидається кожен чанк, що їх перетинає - середина довгого ключа маркерів не має; голий `-----END ... PRIVATE KEY-----`
  теж "жорсткий") і відомих форматів токенів: AWS `AKIA…`,
  `sk_live_`/`rk_live_`, GitHub `gh[pousr]_`, Slack `xox[abprs]-`, Telegram-бот, JWT, Google `AIza…`.
  Лічильники masked/dropped - по репо в `myvec status` та `GET /api/status`. `remember` маскує так само і
  відмовляє лише для приватних ключів / відомих токенів.
- **Чанкінг.** tree-sitter (php, js, ts/tsx, python, c#, go, java, rust, bash): чанк на функцію/клас/метод,
  великі розбиваються по методах; коментарі/doc-коментарі/атрибути/декоратори безпосередньо над оголошенням входять до
  його чанка; маленькі сусідні чанки одного класу зливаються, поки чанк не досягне мінімуму (12 рядків або 500 символів,
  але не більше 120 рядків / 5800 символів), `symbol` злитого чанка - ім'я класу (клас лише з дрібних членів = один
  чанк); на верхньому рівні зливається лише "клей" без символу (імпорти, `namespace`, закриваючі дужки) - з сусіднім
  оголошенням, дрібні іменовані функції лишаються окремими; markdown - за заголовками; решта (і vue) - вікна 60 рядків / overlap 10.
  Текст чанка обмежений 5800 символами (разом із `шлях :: символ` ≤ 6000 симв. ≈ 1500 токенів - контекст
  embeddinggemma 2048): завеликі вікна скорочуються за символами, надто довгі рядки ріжуться на шматки, нічого
  не обрізається мовчки. Ембедиться `шлях :: символ\nтекст` (префікс не зберігається).
- **Сховище.** `chunks_vec` (vec0, `repo_id` partition key, float32 cosine), `chunks_bit` (vec0 `bit[dims]`, тільки
  для грубого проходу, якщо dims кратне 8), `chunk_repo` (мапінг vec-рядок -> (repo, chunk)), `chunks_fts`
  (FTS5 `unicode61 remove_diacritics 2`), `memories` + `memories_vec` (vec0, `scope` - partition key: KNN обмежується видимими scope
  всередині індексу) + `memories_fts`; індекс `branch_files_sha(blob_sha, repo_id, branch)` для FTS-JOIN. Trigram-FTS більше немає
  (за бенчмарком не дав приросту, індекс у 2.2x більший) - при оновленні таблиця видаляється міграцією.
  Версія схеми - `meta.schema_version` (зараз 4: індекс `branch_files_sha`, колонки пам'яті `base_commit`/`tip_seen`,
  `memories_vec` із partition key - вектори переносяться автоматично); міграції ідемпотентні (`db.migrate`).
- **Пошук (роутинг).** Запит з одного токена без пробілів (`renderPdf`, `Foo::bar`, `snake_case`, `a.b.c`) -> hybrid:
  RRF(k=60) від vector top-50 і FTS top-50; природна мова -> лише vector (FTS додає шум для uk/ru). Пам'ять -
  те саме правило. `--mode auto|vector|hybrid` перевизначає. **Бал** однаковий для коду й пам'яті, тому вони
  зливаються в один список за фінальним балом: vector -> `схожість` (1 - косинусна відстань) × вага; hybrid ->
  `RRF / (2/61)` (нормалізований до 0..1) × вага. Вага скоупу: поточне репо 1.0, інші репо того ж проєкту 0.8, пам'ять
  проєкту/предків 1.0, глобальна пам'ять 0.6, інші проєкти (лише `--scope all`) 0.5, імпортована пам'ять
  (`source=import`) додатково × `import_weight = 0.85` (щоб нотатки, які писав Claude, вигравали при рівності).
  Ваги **пом'якшуються**: `w_eff = 1 - weight_softness × (1 - w)`, `weight_softness = 0.5` (0.6 -> 0.8, 0.8 -> 0.9,
  0.85 -> 0.925; множаться пом'якшені множники) - вага лише розрізняє майже однакові хіти, а не перекриває схожість
  (плоский RRF сам по собі цього не витримував). Усе - в `config.toml`. У змішаному виводі (`--kind all`, за замовчуванням) не більше
  `max_memory_hits = 3` пам'ятей (за score), решта слотів - код за score; `--kind memory` без обмеження (k пам'ятей);
  однаково в CLI, `/api/cli/search` і `/api/search`. `--scope all` при > 100k рядків у scope: грубий top-500 по `bit[]`-таблиці, потім
  точний float32-рескорінг; менше - звичайний float32-скан.
  **Вага тестового шляху (E8-20, ADR-048):** у NL-запитах (маршрут `vector`) code-хіти з тестових шляхів (`is_test_path`: теки `tests/ test/ __tests__/ spec/`, `test_*.py`, `*_test.py`, `*_test.go`, `*.test.*`, `*.spec.*`, `*Test.php`) множаться на `test_path_weight = 0.8` (`score = similarity × weight_eff × path_weight`). Не діє для ідентифікаторів (hybrid), для запитів зі словом тесту (`тест*`, `test*`, `spec`, `pytest`, `фікстур*`, `fixture*`) і для пам'яті/доків; `test_path_weight = 1` вимикає. Обґрунтування - `bench/REPORT.md`, розд. 8. **Вага тікетів (E8-21, ADR-049):** так само `doc_path_weight = 0.9` для code-хітів `docs/todo/**` (`is_ticket_path`); не діє для `kind=docs`, ідентифікаторів і запитів зі словом про доки (`mentions_docs`: тікет, ADR, рішення, план, док, readme, урок, чому, звіт, аудит); `bench/REPORT.md`, розд. 9. Після оновлення - `myvec install-daemon` (пошук іде через демон).
- **Пошук по документах (E8-03, ADR-032).** `search --kind docs "q"` - лише документи: code-чанки файлів з розширенням `.md .markdown .mdx .rst .adoc` (`search.DOC_EXTS`; за розширенням шляху в `branch_files`, не за текою і не за `blobs.lang`), без пам'яті; `.txt` не входить. Фільтр шляху стоїть у самому KNN (`rowid IN (...)` по партиції `repo_id`) і в предикаті FTS, а не після top-200, тож доки не тонуть у ближчому коді. Ранжування, ваги й `--kind all` не змінені; працює разом із `--scope`, `--by-project`, `--project`. Схема БД не змінюється. **Після оновлення потрібен `myvec install-daemon`:** демон без перезапуску відхиляє `kind=docs` (CLI друкує `--kind must be code|memory|all`, код 1).
- **Крос-проєктні знання (E8-05, ADR-030).** `search --by-project "q"` (вмикає `scope=all`) відповідає «де ми таке робили»: хіти групуються за проєктом верхнього рівня (`crossproject.summarize`), до 6 проєктів за найкращим балом, у кожному кількість код/пам'ять і 2 найкращі хіти; поточна родина - лише рядком «current project: N more». Пул обмежений 50 кандидатами на список (код і пам'ять окремо), порогу «знайдено» немає - суди за балами; ранжування й дефолтний вивід `--scope all` не змінені. `--json` - `{query, kind, current, projects[]}`. `search --project <назва|шлях> "q"` - drill-down: `scope=project` від іншого проєкту, без global-пам'яті; відносний шлях рахується від cwd; помилка (код 1) для невідомого проєкту, відсутнього кореня на цій машині та цілі з `shared = false`. `--by-project` разом із `--project` - помилка. Демон повертає `"cross": 1`; якщо в відповіді його нема (старий демон), CLI йде в direct mode.
  `shared = false` у `.myvector.toml` (дефолт `true`) ховає проєкт і всіх його нащадків (код і пам'ять) від `--scope all`, `--by-project`, UI-пошуку й `--project` для будь-якого cwd поза його родиною `top_level`; родина бачить себе, global-пам'ять не зачіпається. Прапорець читається з маркера під час пошуку (кеш за `mtime`, колонки в БД нема), тож правка діє без sync. Зіпсований TOML або не-bool `shared` - проєкт прихований (fail-closed); відсутній маркер чи корінь - спільний. Перегляд пам'яті в UI (інструмент власника) фільтра не застосовує. `init` зберігає ключ `shared` при перезаписі маркера.
- **Вивід.** До `k = 10` хітів: `[i] репо path:start-end символ (score)` + 3 рядки (`score` - фінальний бал, 3 знаки, напр. `(0.742)`); топ-3 коду (`expand_top`)
  розгорнуті до 25 рядків (`expand_lines`); загальний бюджет `output_budget_tokens = 2000` (chars/4). `--full` -
  цілі чанки. Якщо індекс застарів - останній рядок `(індекс оновлюється у фоні)` (у `--json` - у stderr).
- **Стислий вивід для агентів (E8-13, ADR-043).** Коли stdout не tty (Bash агента, пайп), друкується стислий вивід: до `k` рядків `[репо ]path:start-end символ[ · ticket ID [статус]]` без сніпетів і балу (~600 Б на 10 хітів; префікс репо лише коли хіти з кількох репо або репо ≠ репо cwd), пам'ять - `[M] дата id: перші 100 символів`. У терміналі - розгорнутий, як раніше. `--compact`/`--expand` перебивають авто (разом - код 1), `--full` завжди розгорнутий, `--json` і `--by-project` без змін; порядок хітів той самий. `-q "…" -q "…"` (до 5, разом із позиційним - позиційний першим): окремий пошук і окремий запис у `searches.jsonl` на кожен, у тексті блоки з заголовком `# i/N <запит>`, `--json` з ≥ 2 запитами - список `{query, results}`. Для запиту з одного слова (≥ 3 символи, не `kind=memory`, не `--by-project`) в обох текстових режимах додається рядок `total in index: N lines in M files (word/prefix matches) · all: rg -n -F -- 'q' <корінь>` - кількість унікальних рядків (чанки перекриваються) із точним входженням у скоупі, знайдених FTS за цілим словом або префіксом (збіг усередині довшого слова не рахується - повний підсумок дає `rg`); новий CLI зі старим демоном у розгорнутому режимі цього рядка не отримає до `myvec install-daemon`; `≥N` - межа 5000 кандидатів; для `scope=all` і запиту з hard secret команди `rg` нема. Демон приймає `compact` у `/api/cli/search` і повертає `"compact": 1`; без ехо (старий демон) CLI йде прямим шляхом - після оновлення `myvec install-daemon`. У `searches.jsonl` є `fmt` (`compact|expanded|json|summary`).
- **Свіжість при пошуку.** Для кожного репо в scope порівнюється `.git/HEAD` (гілка) + mtime `.git/index` (для не-git
  псевдо-репо - найновіший mtime серед каталогів верхнього рівня) з тим, що записано при останньому sync. Це кілька
  `stat` (мс). Якщо відрізняється - фоновий sync (через демон, якщо він працює, інакше detached `myvec sync --quiet`,
  не частіше за раз на 20 с і не при зайнятому lock) + примітка у виводі. Режим `off` не запускає нічого.
- **Пам'ять.** Vault - джерело істини (frontmatter: `id, scope, [branch], origin_branch, origin_repo, commit,
  created, updated, tags, pinned, supersedes`). Ім'я файлу: `дата-слаг-id.md`. Схожі записи лінкуються `[[wikilink]]`.
  Нова пам'ять належить **проєкту** (scope); `origin_branch`/`origin_repo`/`commit` лише записуються як
  метадані. Тільки `remember --branch` ставить поле `branch` - фільтр видимості (пам'ять видна лише на цій гілці
  цього репо; не потрапляє у `search`/`context` на інших гілках).
  Життєвий цикл (у `sync` і `gc`, дешево - git викликається лише якщо є гілкові пам'яті): гілку злито в default
  (`origin/HEAD`, інакше перша наявна з `main`/`master`/`dev`) -> `branch` очищується (пам'ять стає загально-
  проєктною, `origin_branch` лишається, frontmatter переписується); гілку видалено і не злито -> архів
  (`vault/_archive/`, `superseded_by='archived'`). `remember --branch` додатково пише `base_commit`
  (merge-base з default). «Злито» потребує доказу власної роботи гілки: коміт запису був поза default
  (`commit != base_commit`), або життєвий цикл бачив гілку попереду default (`tip_seen`), або вістря - другий батько
  merge-коміту; і ці коміти тепер у default. Нотатка на свіжій гілці до її комітів, гілка видалена не злитою -> архів;
  свіжа гілка, лише перемотана з main -> не "злита". Записи без `base_commit` (старі) оцінюються за старим правилом. `promote ID` - у глобальний scope (`vault/global/`, `branch` знімається).
- **Шляхи vault.** `scope_dir`, `write_file`, архівація, `promote` і `topics` перевіряють, що розв'язаний шлях лишається
  всередині vault (`..` / абсолютні scope відхиляються).
- **Vault - git-репозиторій.** При першому використанні `git init` у `MYVEC_HOME/vault` з `.gitignore`
  (`.obsidian/workspace*.json`, `.trash/`); автокоміт після remember / forget / pin / unpin / promote / змін
  життєвого циклу (`myvec: remember <id> …`). Без remote і push. Окремий від налаштувань користувача
  (`GIT_CONFIG_GLOBAL=/dev/null`, без hooks/підпису); `add`+`commit` серіалізуються файлом `vault-git.lock`; помилки не
  ламають команду - лише `logs/vault-git.log`.
- **`.myvector.toml` прихований від git.** `init` дописує рядок у глобальний excludes-файл: `git config --global
  core.excludesFile`, а якщо не задано - `~/.config/git/ignore` (дефолт git; створюється, `core.excludesFile`
  НЕ виставляється). Рядок додається лише якщо його ще нема.
- **Hooks.** `install-hooks` пише шлях до `myvec` у лапках для shell. `session-start` друкує `myvec context` і запускає фоновий `sync --quiet`, якщо останній був > 60 с тому;
  `post-edit` - debounce 30 с і перевірка lock. Обидва - тільки stdlib, ~30-40 мс; не залежать від демона.
  Кожен виклик продовжує вікно активності проєкту; режим `off` - hooks нічого не синхронізують.
- **Сесії (ADR-007).** Hooks `session-event` не пишуть у БД: додають один JSONL-рядок (`O_APPEND`, ≤ 4 КБ, `0600`) у `MYVEC_HOME/state/session_events.jsonl`; демон раз на секунду забирає журнал rename-ом і складає в таблицю `sessions` (схема v6), без демона - `myvec sessions`. Стани: `working`, `waiting` (чекає на тебе: старт, Stop, питання, Notification), `idle` (`working` без подій довше `session_idle_minutes`, обчислюється при читанні), `ended` (видаляються через `session_retention_days`). Пульс `session-pulse` лише оновлює `last_seen` і стан не міняє. Без демона журнал обмежений 20 МБ (понад це пульси не пишуться). Приватність: текст промпту не зберігається; текст питання й хвіст відповіді проходять `mask_secrets`, hard secret -> порожньо. Ключі `config.toml`: `session_idle_minutes` (15), `session_retention_days` (30). Прив'язка до тікета (ADR-009) обчислюється при читанні: `myvec ticket claim` > гілка `t/<id>-…` > `.worktrees/<id>`; зовнішні worktree зводяться до головного кореня через git `commondir`. Відновлення з UI (ADR-010) потребує claude-manager з `cm send`/`cm who` (PR #2 в `user/pm-tool`); без нього режим `unavailable`, працює лише копіювання. Ключі: `cm_path` (за замовчуванням `~/.local/bin/cm`), `cm_timeout_s` (90). `myvec sessions --ticket <id>` — сесії тікета (`*` — claim); у картці тікета API віддає поле `sessions`. Відповідь на `AskUserQuestion` з вебу (ADR-014, схема v8): хук пише лейбли варіантів (`qo`, лише одне питання без multiSelect, до 8, замасковані), колонка `sessions.question_options`; якщо claude-manager має `cm answer`, картка показує кнопки варіантів і поле «Свій варіант» (один рядок), POST `/api/sessions/<id>/resume` приймає `{text}` або `{option}`; мульти-питання, multiSelect, permission-діалоги й багаторядковий текст - 409/400 «відповідай у терміналі».

## Тікети

Трекер тікетів живе у файлах проєкту: канон v1 (frontmatter `id/title/status/parent/size/deps/blocks/pr/decisions/updated`, розділи тіла, `## Log` лише дописується; парсер `src/myvec/tickets.py`, правила цього репо - `.claude/rules/tickets.md`). За замовчуванням тека `docs/todo`; інша - у `.myvector.toml`:

```toml
[tickets]
dir = "docs/todo"   # відносно кореня проєкту; без `..`, абсолютних і порожніх сегментів
```

Невалідне значення -> дефолт + попередження `bad_tickets_dir`, проєкт індексується. Статуси у файлах (емодзі або ключі) міняє лише оркестратор, `myvec ticket claim` пише в `MYVEC_HOME/state/ticket_claims.jsonl`, не в БД.

- **БД (схема v6; v6 додає `sessions`, див. «Сесії»).** Таблиці `tickets`, `ticket_edges`, `ticket_issues`, `ticket_dirs` - похідні від файлів, `myvec rebuild` / `sync` їх відновлюють; старі БД отримують їх при відкритті. Заголовок/підсумок/тіло проходять `mask_secrets`; тіло з hard secret не зберігається (`body_dropped=1`); файли за deny-list пропускаються. Проєкт без канону (нема теки чи шапок) тікетів не має.
- **Оновлення.** Демон за подією watcher під тікет-текою через ~1,5 с оновлює таблиці окремим потоком, без `sync_lock` і ембеддера (подія шини `tickets_updated`); `sync_project` теж оновлює їх. Відбиток теки (`stat` усіх `*.md`) робить повторне оновлення безкоштовним. Раз на 5 хв (і при старті) демон перевіряє всі зареєстровані проєкти, зокрема неактивні й ті, де тека в `.gitignore` та watcher її не бачить - зміни там з'являться з затримкою до 5 хв.
- **Пошук.** Окремого джерела немає: `.md` тікета - звичайні чанки. Хіт файла тікета отримує `ticket: {project, id, title, status, progress}` (у `--json`) і позначку ` · ticket <id> [<status>]` у заголовку. Ранжування не змінене.
- **`init` і маркер.** Перезапис `.myvector.toml` зберігає чужі ключі й таблиці (`[tickets]`); якщо щось не вдається зберегти (вкладені таблиці, дати) - `init` відмовляє (`MarkerRewriteRefused`) і просить правити файл руками.

### Зворотний канал уроків (ADR-027)

Загальний урок (про процес, агентів, шаблони, `myvec`), знайдений в іншому проєкті, оркестратор цього проєкту пропонує в inbox vector-local; рішення приймає оркестратор vector-local.

```
printf '%s\n' "Симптом: ... Причина: ... Правило: ..." | myvec ticket lesson --title "Короткий заголовок" [--to NAME] [--from NAME] -
```

- Текст лише зі stdin (`-`), не з argv; `--title` один рядок ≤ 120 символів. `--to` - проєкт-одержувач (дефолт `lessons_project` у `config.toml`, `vector-local`); корінь - `projects.root` з БД (read-only), тека тікетів - з маркера. `--from` - ім'я проєкту-джерела (дефолт - маркер cwd, інакше ім'я git toplevel). Вивід - лише ім'я створеного файла.
- Запис - тільки через `inbox.write` (ADR-019) з `kind: lesson`, `ticket: lesson`, `from`, `source: cli`; ім'я `<дата>-lesson-<HHMMSS>[-n].md`. Hard secret - відмова, літерали маскуються; абсолютний домашній шлях (`/home/<u>`, `/Users/<u>`, `~|$HOME/…MyVector`) і шлях у `MyVector/vault` відхиляються (`private`).
- Коди: 0 ок; 2 - некоректний ввід, секрет, приватний шлях, невідомий проєкт, нема БД, stdin > `MAX_TEXT*4`; 3 - черга повна (≤ 50).
- Хук сесій (ADR-022) в основному checkout показує `lesson<-<from>` і вказівку на розділ «Lessons back-channel» у `orchestrator.md`; текст уроку в контекст не потрапляє. Inbox локальний для машини: урок обробляється там, де лежить файл.

### Міграція тікетів наявного проєкту (ADR-024)

Проєкт зі старим форматом (інша шапка, таблиці, один `todo.md`) трекер не бачить. `myvec ticket migrate` (`src/myvec/ticketmigrate.py`) лише виявляє, брифує й перевіряє; переписує тікети агент проєкту.

```
myvec ticket migrate --detect [--json]        # кандидати (docs/todo, todo.md, ...) і формат: canon | near_canon | frontmatter | single_file | notes
myvec ticket migrate --brief [--dir D] [--out FILE]   # бриф для агента проєкту: мапа полів і статусів
myvec ticket migrate --check [--dir D]        # що лишилось до канону; exit 0 лише коли нічого
myvec ticket migrate --fix [--dir D] [--write] [--add-goal-heading]  # механічні правки шапки; без --write - dry-run
myvec ticket migrate --split --dir todo.md [--group-id E1 --group-slug s --group-title T --dest D --status-style emoji|keys] [--write]  # один файл -> тека
```

- **CLI:** детермінований; нічого не пише, крім `--brief --out` і `--fix --write`. `--fix` робить лише безвтратне: `epic` -> `parent`, `id`/`kind: group`/`status` для синтетичних груп `index.md`; `id` для шапок без нього (префікс імені файла, інакше ім'я файла, `<група>-NN` лише при колізії), нові ключі вставляються на межі ключів (багаторядкові значення цілі); тіло байт у байт, повторний запуск - `nothing to fix`. `--add-goal-heading` - єдине, що змінює тіла (вставляє заголовок цілі). `--split`: dry-run, `--write` пише; текст дослівно, старий файл лишається посиланням (повтор - no-op). Без `--write` нічого не змінюється.
- **Агент проєкту** за брифом: розбиває `todo.md` на файли (старий лишається коротким посиланням), вирішує схему id і невідомі статуси (`?` у мапі - питання власнику), зберігає форму статусів і мову розділів проєкту. Одна гілка й PR: спершу коміт лише з `git mv`, потім правки; `## Log` не переписується. Завершення: `--fix --write` -> правки -> `migrate --check` до зеленого -> `ticket check --strict` -> `ticket index` -> PR.
- **Безпека:** `--dir` має бути всередині `--root` (без `..`, абсолютних шляхів поза root і симлінків; інакше код 2); перед записом файл звіряється з текстом, з якого будувався план. Detect і бриф містять лише ключі шапки, значення статусів і заголовки розділів, не текст тікетів. Якщо кандидатів кілька, `--brief/--check/--fix` без `--dir` відмовляють з кодом 2 і переліком.
- **Російські тікети:** парсер розпізнає ціль `## За один заход`, `INDEX.md` генерується російськими заголовками (`lang_for` -> `ru`). Вивід `ticket check`/`index`/`migrate --check` іде мовою тікетів проєкту (uk/ru/en).
- **Лічильник у INDEX:** загальний прогрес `N% (k/n)`: відсоток зважений (з часткою незавершених), `k/n` - лише завершені листи, тож «75% (0/1)» - не розбіжність.
- **Старі скрипти проєкту:** `--detect` і `--check` попереджають `legacy_script` (`package.json`, Makefile, `scripts/*`, що згадують `tickets` та старі поля) - лише `файл:рядок`; замінити на `myvec ticket check`/`index`. `--fix --write` ставить статус створеної групи за дітьми, наявний статус групи не чіпає.

`ticket index --index-name NAME` пише індекс під іншим іменем, коли `INDEX.md` збігається без урахування регістру з чужим `index.md` (macOS): без прапорця команда відмовляє кодом 2. Ім'я не зберігається, тож такий проєкт викликає індекс з прапорцем щоразу.

Приклад (формат «майже канон»: шапка є, але `epic:` замість `parent`, а групи `index.md` без `id`): `--detect` дає `near_canon`, `--fix` показує план по файлах, `--fix --write` його застосовує, `--check` лишає тільки семантичне (наприклад, ціль не розпізнано).

### Поради здоров'я тікетів (ADR-020)

Окрема severity `health`: поради не є помилками й не блокують `check --strict` та `index`. Правила (лише листи у статусах ⬜ 🟦 🟨 ⏳ ⛔ часткові; групи й ✅/🚫/❄️ пропускаються): `too_big` (розмір L), `no_ac` (нема чекбокса AC), `stale` (⬜ не оновлювався 30 днів, 🟦/🟨 - 7), `no_session` (🟦 без активної сесії ≥ 1 доби; лише UI/API, CLI його не рахує), `blocked_ready` (⛔, а всі `deps` уже ✅), `dep_cancelled` - `deps` містить 🚫. Поради обчислюються при читанні (`tickets.health`, `ticketdb._health`), у `ticket_issues` не пишуться, схема не змінена; пороги - константи в `tickets.py`. У UI: біля тікета в дереві іконка `warn-ic` з текстом у `title`, фільтр «Проблеми» враховує тікет з порадою, у картці тікета (між полями й сесіями) список `.health`.

### Вкладка «Проблеми» (E3-04)

Вкладка проєкту «Проблеми N» (перед «Витрати») групує все, що знаходять `ticket check`, health-поради й `ticket migrate`: ✖ помилки, ⚠ попередження, ℹ поради. Для кожного коду - назва, пояснення, «Що робити» (команда з кнопкою копіювання) і розгортуваний список тікетів; для старого формату, якщо в репо є гілка `myvec/process-migration`, - ще й команда її злиття. Словник кодів - `src/myvec/problems.py` (один для UI і, згодом, CLI); API `GET /api/tickets/<p>/problems` без тіл тікетів. Число в колонці «Проблеми» таблиці «Тікети» = N у вкладці й веде на неї. Код `process_outdated` (E0-10, ADR-036, warning, рівень проєкту): «процес застарів vN → vM», коли `scaffold.state.read(root)` дає `outdated`; підказка `myvec scaffold --update` (константи `problems.UPDATE_CMD`/`UPDATE_PHRASE`). Обчислюється при читанні (`problems.process_issues`, кеш за inode/mtime/ctime/size/`PROCESS_VERSION`), у БД не пишеться; `projects_summary` дає поле `n_process`, лічильник вкладки й колонки трекера = `n_issues + n_health + n_process`. Колір числа в таблиці «Проєкти» - за найгіршою серйозністю (E8-19): поле `worst` зведення (`error` > `warning` > `health`, невідома = `warning`, без проблем `null`) -> червоний `--danger` / жовтий `--warning` / `--info`; без `worst` при ненульовому числі - `--danger`. Вкладений підпроєкт (стан у корені репо, а не в корені проєкту) цієї проблеми не має.

Коди правила пошуку (E8-12, ADR-041; `problems.process_issues`, stdlib, `myvec.scaffold` не імпортується): `search_rule_missing` (warning) - є `.claude/agents`, нема стану, нема `.claude/rules/search.md` і жоден `CLAUDE.md`/`.claude/CLAUDE.md` не згадує `myvec search`; перевіряються корінь проєкту й прямі git-підрепо першого рівня (маркер може бути вище репо), для підрепо підказка містить `--root <підтека>`, бо `--adopt` у корені маркера дасть код 2; `foreign_search_hook` (warning) - у `.claude/settings{,.local}.json` є хук PreToolUse з `graphify` (текст команди не виводиться); `search_rule_outdated` (warning, E8-17, ADR-046) - `.claude/rules/search.md` (за `.claude/` без симлінка) збігається за sha256 зі старим рендером із `SEARCH_RULE_OLD`, підказка - `myvec scaffold --adopt --write`; `graphify_leftover` (health) - є `graphify-out/`, а команди `graphify` у `PATH` нема (health, бо у демона під launchd вужчий `PATH`). Рахуються при читанні, кеш за відбитками файлів, у БД не пишуться; входять у `n_process`. Увага: вкладка «Проблеми» є лише у проєктів із текою тікетів (`ticket_dirs`), проєкти без `docs/todo` цих кодів не побачать.

### Коментарі й запити статусу з картки (inbox, ADR-019)

У картці тікета (лише тікети, не групи) є форма коментаря й кнопка запиту статусу. Нода нічого не міняє в тікеті: пише файл `<корінь проєкту>/<тека тікетів>/inbox/<YYYY-MM-DD>-<id>-<HHMMSS>[-n].md` в основному checkout (шапка `ticket, kind: comment|status, requested_status, created, node, source: ui`, далі текст). Формат створення: `O_EXCL|O_NOFOLLOW`, 0600, tmp + `os.link` (без перезапису), усі операції відносно дескриптора теки `inbox`; символічний `inbox` або тека тікетів поза коренем - відмова. Нода кладе `inbox/.gitignore` з `*`, тож git, індексатор і `fingerprint` файлів не бачать; у `.gitignore` проєкту рекомендовано рядок `docs/todo/inbox/`. Ліміти: текст ≤ 4000 символів без керівних (CRLF нормалізується), тіло запиту ≤ 32 КБ, у черзі ≤ 50 файлів; hard secret - відмова, літерали маскуються. Обробляє оркестратор на початку ходу: переносить у `## Comments`/Log, видаляє файл, статус міняє сам. UI показує «надіслано», поки файл існує (читання теки напряму, опитування раз на 10 с), потім «оброблено» й перечитує картку. Код: `src/myvec/inbox.py` (лише stdlib + `tickets`).

Оркестратор дізнається про нове з хуків сесії (E3-02, ADR-022): `hook session-start` і `hook session-event` (UserPromptSubmit) додають у контекст рядок «inbox: N» з id, видом і статусом (текст коментаря - ніколи) лише в основному checkout (не в worktree, не саб-агентам); повторно - лише коли набір файлів змінився (стан у `MYVEC_HOME/state/inbox_seen/<session>`). Проєкт без файлів у inbox - вихід до імпортів, без доплати часу. Нові хуки не потрібні (`install-hooks` не перезапускати), досить перевстановити myvec.

## Активність і режими синхронізації

Для кожного проєкту: `sync_mode` = `auto` (за замовчуванням) | `always` | `off`; `window_hours` (за замовчуванням 2;
глобально - `window_hours` у `config.toml`, на проєкт - `myvec mode ... --window-hours N`); `last_access`.
Проєкт **активний**, якщо `always`, або `auto` і `now < last_access + window`. Будь-яке звернення продовжує вікно:
hooks (session-start, post-edit), `search`, `remember`, дії в UI (`mode`, `sync`, `activate`).
Реалізація для hooks (stdlib, < 100 мс): один `UPDATE projects SET last_access` з `busy_timeout` 100 мс; якщо БД зайнята
(іде sync) або ще не мігрована - рядок `epoch<TAB>проєкт` дописується у `MYVEC_HOME/activity`, а демон (щотіку)
і `status` вливають його у БД. `myvec status` показує режим, вікно та "active until". Режим і вікно живуть у `index.db`
(після повного `rebuild` індексу вони скидаються до дефолтів).

## Демон `myvec serve`

Один довгоживучий процес (Starlette + uvicorn, `127.0.0.1:7878`, порт - `daemon_port` або `--port`):

- тримає теплу БД-з'єднання (якщо `index.db` видалено/замінено - перевідкриває нову) й ембедер; запит ембедиться ДО взяття
  db-lock, з таймаутом Ollama 10 с (пакетна індексація - 300 с); поки є активний проєкт, раз на 30 хв шле Ollama запит із `keep_alive`
  (`keep_alive = "2h"` у конфігу також додається до кожного embedding-запиту);
- **планувальник/watchers**: для кожного *активного* проєкту - `watchdog` (FSEvents на macOS, inotify на Linux; підписка лише на зміни, не на читання) на корені, ігноруючи
  заборонені каталоги, `exclude` з маркера та `.gitignore`, АЛЕ з `.git/HEAD` і `.git/index` кожного репо
  (перемикання гілки/коміт/pull). На Linux inotify-підписка обрізана тими ж правилами (підклас `InotifyWatchGroup` у `watcher.py`,
  ADR-033): не підписуються `.git/objects` та інше під `.git`, заборонені/`exclude`/gitignored каталоги; потрібен `watchdog>=7.1,<8`
  (приватний API, без нього - стоковий `Observer` і рядок у логу). Переповнення черги inotify дає один debounced (30 с) sync проєкту. Sync через ~10 с після останньої події (debounce), страхувальний sync кожні 15 хв,
  watchers зупиняються, коли проєкт неактивний; активність перевіряється щохвилини. Той самий `sync.lock`, що й у CLI -
  одночасно ніколи. При зупинці демона (`state.stopping`) довгий sync відміняється кооперативно (`SyncStats.cancel`,
  `indexer.SyncCancelled`): перевірки лише між батчами, поза транзакціями БД; збережені батчі лишаються, наступний sync
  доробляє решту. `myvec sync` по Ctrl+C виходить з кодом 130 (рядок у `sync.log`, lock вільний);
- **шина подій**: кільцевий буфер (~500) + `logs/events.jsonl` (ротація ~3 MB): `sync_started/progress/finished/
  failed/cancelled/busy`, `watcher_start/stop`, `mode_changed`, `project_activated`, `memory_action`, `search` (запит, scope,
  cwd, мс, кількість результатів, ~токени, `via: daemon|direct`). Пошуки CLI у прямому режимі дописуються в
  `logs/queries.jsonl`, демон підхоплює їх у шину. (Синки, запущені самим CLI, у шину не потрапляють.)

**Безпека**: лише `127.0.0.1`; запити з `Host`, відмінним від `127.0.0.1:<port>`/`localhost:<port>`, відхиляються
(захист від DNS-rebinding); для не-GET запитів потрібні same-origin `Origin` (якщо є) І заголовок `X-CSRF-Token`,
що дорівнює випадковому токену, згенерованому при старті. `GET /` вставляє його в
`<meta name="csrf-token">`; CLI бере з `MYVEC_HOME/daemon.json`.

### JSON API

| Метод і шлях | Опис |
|---|---|
| `GET /api/status` | `projects` - дерево (проєкт -> `repos` [path, dir, current_branch, branches, blobs, chunks, last_sync, masked, dropped], `children`), для проєкту: `mode, window_hours, last_access, active_until, active, watching, syncing, progress, exclude`; `global`: model, dims, `model_mismatch`, db_size_bytes, memories {total, archived, by_scope}, `config` {model, dims, window_hours, k, expand_top, expand_lines, weights}, `home`, `vault` {path, git, last_commit}, `searches` {count_24h, p50_ms, p95_ms}, ollama {url, reachable}, daemon {port, pid, started, debounce_s, safety_s, ...} |
| `GET /api/projects/{path}/detail` | для сторінки проєкту: репо з гілками (`branch, chunks, files, manifest_bytes, updated, state` = current\|known\|merged\|gone), `unique_chunks`, секрети (лічильники + файли: лише шлях і кількість, без значень/імен ключів), виключення (маркер, `.gitignore`, типові), параметри watcher-а |
| `GET /api/projects/{path}/searches?limit=` | останні пошуки проєкту (і дочірніх) за 24 год із шини + `total_24h`, `p50_ms`, `p95_ms` |
| `POST /api/projects/{path}/mode` | `{"mode": "auto\|always\|off", "window_hours"?: N}` -> новий стан |
| `POST /api/projects/{path}/sync` | поставити sync у чергу -> `{queued}` |
| `POST /api/projects/{path}/activate` | продовжити вікно зараз |
| `POST /api/refresh` | перерахувати активність негайно |
| `GET /api/events` | SSE-потік шини (`?since=ID`, `?follow=0` - віддати накопичене й закрити; `Last-Event-ID` підтримується) |
| `GET /api/events/recent?limit=` | останні події (JSON) |
| `GET /api/memories?scope=&q=&include_archived=&limit=` | `q` - пошук по пам'яті (без ваг scope і без фільтра гілок), інакше нові першими; `scope` - точний або піддерево |
| `GET /api/memories/{id}` | один спогад (в т.ч. архівований; префікс id підтримується); кожен елемент пам'яті має `related` - `[[wikilinks]]` з тексту (`stem`, `id` з імені файлу) |
| `POST /api/memories/{id}/pin\|unpin\|archive\|promote` | дії над пам'яттю |
| `GET /api/search?q=&cwd=&scope=&kind=&mode=&k=` | структуровані результати, **лише читання** (не запускає sync і не продовжує вікно активності); для кожного `breakdown`: `vector_rank, fts_rank, similarity, rrf, rrf_max, weight, import_weight, weight_eff, path_weight, route` (`score` = `similarity × weight_eff × path_weight` для vector, `rrf / rrf_max × weight_eff` для hybrid; UI показує `× вага шляху`, коли `path_weight` ≠ 1); у відповіді `route`, `context`, `weights`, `weight_softness`, `import_weight`, `stale` (індекс застарів), `index_refreshing` (завжди `false`) |
| `POST /api/search` | ті самі параметри в JSON-тілі (CSRF); єдиний варіант із побічними ефектами: ставить sync застарілого індексу в чергу (`index_refreshing: true`) і продовжує вікно активності |
| `GET /api/search/usage?days=7\|14\|30` | використання пошуку агентами (E8-06, ADR-037): вихід `searchstats.usage()` - лише агрегати (на проєкт `sessions, sessions_myvec, myvec, recall, self, self_sub, myvec_sub, share, miss, logged, zero_results, p50_ms`, `total`, `collector`), без тексту запитів; без `days` - 14, інше - 400; лише читання (read-only `index.db`/`tokens.db`, без сканування транскриптів), кеш 30 с на `days`; CSRF не потрібен |
| `POST /api/cli/search` | для CLI: `{q, cwd, scope, kind, mode, k, full, json}` -> `{stdout, stderr, exit_code}` - точно те, що надрукував би CLI; некоректний `k` (не ціле, поза 1..100) -> 400 |
| `GET /api/tickets` | зведення тікетів по проєктах: `project_path, n_tickets, n_errors, progress, counts, n_issues, n_problems, n_health` (унікальні нетекові тікети зі статусом blocked, з issues або з порадами здоров'я; `n_health` - кількість порад), `updated_max`, `dir` |
| `GET /api/tickets/{path}/tree` | дерево тікетів проєкту (епіки/групи -> тікети), відсотки, лічильники, issues; 404 - проєкт без теки тікетів |
| `GET /api/tickets/{path}/ticket/{id}` | картка: поля, deps/blocks, `sections` (За один захід, AC з чекбоксами, Log), PR; тіло з hard secret не віддається (`body_dropped`, `sections: null`) |
| `GET\|POST /api/tickets/{path}/ticket/{id}/inbox` | inbox правок з UI (ADR-019): POST `{text, requested_status?}` створює файл у `<тека тікетів>/inbox/` (CSRF, 400 - порожньо/керівні/hard secret/небезпечний шлях, 404 - невідомий тікет, 409 - черга з 50 файлів, 413 - тіло > 32 КБ, також chunked); GET повертає файли, що ще чекають (`items`, текст замасковано) |
| `GET /api/sessions?project=&all=1` | `{node, cm, sessions}`: сесії з `mode` (send\|restart_send\|resume_send\|held\|unavailable), `wait_s`, `question`, прив'язкою до тікета; сесії з не-UUID id пропускаються |
| `GET /api/sessions/waiting` | лише `waiting`, довше чекаючі першими, з `ticket_project/ticket_id/ticket_title` |
| `POST /api/sessions/{sid}/resume` | `{text}` -> `{ok, action, mode, code, message, fork_cmd?}`; відновлює сесію через `cm` і (якщо `text` непорожній) передає відповідь. Текст ≤ 16000 символів, іде в `cm` через stdin. 400 поганий sid/текст, 404 невідома сесія, 409 `held`/`busy`/`dead`/дія вже йде, 502 помилка cm, 503 cm недоступний |
| `GET /api/load` | поточне навантаження ноди: CPU усього й по ядрах, load average, пам'ять/swap, частота, температура (`null`, якщо сенсора нема), шапка хоста, проєкти за CPU за останню хвилину (з кільця в пам'яті), макс. температура за добу. 503 `{enabled:false}`, якщо семплер вимкнено чи нема psutil |
| `GET /api/load/series?period=1h\|24h\|30d` | ряд для графіка: `1h` зі сирих знімків, `24h` і `30d` з хвилинних агрегатів (30d згорнуто до 30 хв) |
| `GET /api/load/projects?minutes=1..60` | CPU за проєктами (% одного ядра) з назвою головного процесу й рядком «інше» (`project_path = ''`) |
| `GET /api/tests/runs?project=&limit=1..200&before=` | прогони тестів (`test_runs`, лише читання), нові першими; без `project` - усі проєкти. Білий список полів (без `cwd`, `session_id`), `outcome` (`running\|passed\|failed\|interrupted\|lost`), `regression: null` (шов для E6-06). З `project` додає `summary` (кількість, перший/останній прогін, `wrapper`/`passive`, медіани `wall_s`/`cpu_s` останніх 10 `passed`). Прогони `running` з іншого хоста (після `relocate`) віддаються як `lost`; `command`/`label`/`agent` маскуються повторно на виході (hard secret -> `[команда приховано]`). 400 на поганий `limit`/`before` (в т.ч. nan/inf), 404 невідомий проєкт |
| `GET /api/tests/current` | `{running, recent}`: прогони `running` цього хоста і до 5 завершених за останні 24 год |
| `GET /` , `/static/*` | веб-інтерфейс: статичні файли в `src/myvec/ui/` (див. нижче) |

Помилки - JSON `{"error": ...}` (400/404/403/409/502).

`POST /api/search` додатково повертає `tokens`, `expand_top`, `expand_lines`; події `search` у шині мають `project, repo, branch, route`.

LaunchAgent: `myvec install-daemon` пише plist (RunAtLoad, KeepAlive, логи в `MYVEC_HOME/logs/daemon.{out,err}.log`,
`PATH` із каталогом `uv tool` і homebrew) і робить `launchctl bootstrap gui/$UID`; `uninstall-daemon` - `bootout` і видалення.

Логи демона (`daemon.err.log`, `daemon.out.log`) обрізаються на місці, коли перевищують 50 МБ (при старті й раз на годину): хвіст 1 МБ лишається в `<лог>.1`, файл truncate-ається (не rename - launchd/systemd тримають відкритий fd).

systemd (Linux): `install-daemon` пише `myvec.service` (Restart=always, `TimeoutStopSec=15`, логи в `MYVEC_HOME/logs/daemon.{out,err}.log`), `daemon-reload`, `enable --now` і `restart`; `uninstall-daemon` - `disable --now`, видалення файлу. `install.sh` ставить усе (uv, ollama, myvec, skill, hooks, daemon, inotify-ліміти, linger).

### Веб-інтерфейс

`http://127.0.0.1:7878/` - Огляд (`#/`), проєкт (`#/project/<шлях>`), Журнал (`#/journal`, SSE з паузою), Пам'ять
(`#/memory[/<id>]`), Тікети (`#/tickets` - усі проєкти; у проєкті вкладка «Тікети»: `#/project/<шлях>?tab=tickets&t=<id>`, дерево зі згортанням, фільтри, картка тікета, оновлення за SSE `tickets_updated`), Пошук (`#/search`, через `POST /api/search`; під формою картка «Використання пошуку агентами» (E8-06, ADR-037): по проєктах сесії з myvec / усього, виклики myvec і grep/find, частка myvec, промахи (> 50 % - попередження), частка grep у субагентах, рядок «Усього» і p50 / нульові видачі з логу пошуків; період `#/search?days=7|14|30`, за замовчуванням 14, опитування раз на 60 с; стани «Збір вимкнено» (`tokens_enabled`), «Транскрипти ще читаються», порожньо, помилка з «Повторити»), Налаштування (`#/settings`, лише перегляд). Навантаження (`#/load?period=1h|24h|30d`, за замовчуванням 24h): KPI (CPU, load average, пам'ять, температура - `—` без сенсора), графік CPU з порогом 85% і перемикачем періоду, проєкти за CPU, сітка ядер, поточні й недавні прогони тестів; без семплера - картка «Семплер вимкнено або нема psutil». Знімок оновлюється кожні 5 с, ряд раз на 60 с, пауза на прихованій вкладці. У проєкті вкладка «Тести» (`?tab=tests`, видна, коли є прогони): KPI останнього прогону, бар-чарти тривалості й CPU-часу (впали - червоним), історія з тікетом, агентом, «обгортка/пасивно»; опитування кожні 10 с лише поки на цьому хості щось іде. У проєкті вкладка «Витрати» (`?tab=tokens&days=7|14|30`, за замовчуванням 14; перемикач періоду в шапці проєкту; видна, коли збір токенів увімкнено): KPI, графік за днями (дні без витрат - нулі, «гарячий» день позначено), «За агентами» (рядок «оркестратор» і окремо «оркестратор / без тікета»; `runs` - кількість різних файлів транскриптів), найдорожчі тікети (топ-7); опитування раз на 60 с, пауза на прихованій вкладці. У картці тікета між AC і Log блок «Витрати» з рядком «У Log при закритті» (той самий `tokens.summary_line()`); без витрат блоку нема. Графіки - inline SVG під виміряну ширину (без бібліотек), підказка при наведенні/тапі. Сесії: на `#/tickets` блок «Чекають на тебе» (найдовше чекаючі зверху), у дереві тікетів колонка «Сесія», у картці тікета блок сесії з textarea та кнопками «Відповісти й відновити» (або неактивна «Відновити» для `held`), копіювання UUID / `cm resume <id>` / «Текст для manager» / «Форк» (`cm resume <id> --fork`); у проєкті вкладка «Сесії» (`?tab=sessions`). Якщо сесія чекає на AskUserQuestion (ADR-014), картка показує варіанти кнопками й однорядкове поле: відповідь іде через `cm answer` (вибір пункту за номером зі звіркою мітки або «Type something» + текст); застарілу картку сервер відхиляє 409, мітки на «Yes…» та діалоги дозволів — лише в терміналі; код 5 cm означає «відповідь могла пройти — перевір термінал». Потрібен claude-manager з `cm answer`. Оновлення за SSE `session_changed`/`session_action`. Vanilla JS
(ES-модулі, без збірки й CDN), дизайн-система Project-B (`app.css` = tokens + bundle + невеликі доповнення), світла/темна тема
(за `prefers-color-scheme`, вибір зберігається в localStorage). Усі не-GET запити шлють `X-CSRF-Token` з `<meta name="csrf-token">`.
«Відкрити в Obsidian» - посилання `obsidian://open?path=…`.

## Моніторинг навантаження ноди

Демон у окремому потоці `myvec-sampler` кожні `load_sample_s` (5 с) знімає CPU (усього й по ядрах), load average, пам'ять, swap, частоту й температуру CPU (Linux: `k10temp` → `coretemp` → `cpu_thermal`; Mac без sudo - `null`; `load_temp_sensor` перекриває вибір). CPU процесів розподіляється по зареєстрованих проєктах за cwd (worktree - до головного проєкту), решта - «інше». Одиниці: CPU ноди - % від усіх ядер, CPU проєкту - % одного ядра. Знімки лежать у пам'яті, а в `load.db` (окремо від `index.db`) пишуться пакетом раз на хвилину: сирі 24 год, хвилинні агрегати 30 днів; усі таблиці мають колонку `host`. При падінні губиться ≤ 1 хв. `load_enabled = false` вимикає семплер (ендпоінти віддають 503), `status().global.load` показує `enabled, interval_s, sampler_cpu_pct, last_ts, alert`.

**Тривога CPU (ADR-018).** Без нового потоку й схеми: `nodeload.cpu_alert` проходить кільце знімків (720). Тривога активна, коли CPU ноди ≥ `load_alert_pct` довше за `load_alert_minutes`; провали до `max(15 с, load_sample_s)` серію не рвуть, розрив часу (сон) рве. Віддається в `/api/load` і `/api/status` (`global.load.alert`): початок, середній CPU, винуватець (проєкт, найважчий процес `top_name`/`top_pid`, середні ядра) і, якщо в цьому проєкті йде прогін `running` цього хоста, `run` (`command` замаскований, `ticket_id`, `agent`). Банер показує лише сторінка «Навантаження». Процеси не вбиваються. Після перезапуску демона тривога відновлюється не раніше ніж за `load_alert_minutes`.

**Регресії тестів.** Рахуються при читанні (`testruns`), у БД не зберігаються. Прогін - регресія, якщо `wall_s` або `cpu_s` перевищує медіану останніх `test_regression_runs` попередніх базових прогонів того ж ключа на ≥ `test_regression_pct` % і на ≥ `test_regression_min_abs_s` с; база < `test_regression_min_runs` - вердикту нема. Ключ: `(проєкт, source, label)` для `wrapper` з `--label`, інакше `(проєкт, source, command)`. Базу складають `wrapper` зі статусом `passed` і `passive` зі статусом `ended`; `failed`/`interrupted`/`lost`/`running` у базі нема й регресією не позначаються. `running` позначається наживо лише за wall. `/api/tests/runs` і `/api/tests/current` віддають `regression` (`wall_pct`, `cpu_pct`, `median_wall_s`, `median_cpu_s`, `n`) і `regression_rule`.

## Витрати токенів (`myvec tokens`)

Демон раз на `tokens_scan_s` (60 с) у власному потоці читає транскрипти Claude Code (`<claude>/projects/<проєкт>/<сесія>.jsonl` і `…/subagents/**/agent-*.jsonl` з `.meta.json`) і складає в `MYVEC_HOME/tokens.db` (окремо від `index.db`, `0600`) токени по запитах: input, output, cache read, cache write, модель (фактична й запитана), effort, `agentType`, `cwd`, гілка. Читання інкрементальне за байтовим зсувом файлу (ротація/обрізання/неповний рядок обробляються), дедуп за `(message.id, requestId)`, бюджет `tokens_scan_budget_mb` за прохід. Каталог `<claude>`: `MYVEC_CLAUDE_DIR` > `CLAUDE_CONFIG_DIR` > `~/.claude`. Без демона `myvec tokens` сканує сам.

Поріг передачі оркестратора: `myvec tokens --session <sid> --context` (наближено: з `--no-scan` відстає до ~60 с). Політика - `skill/myvector/orchestrator.md`.

Атрибуція до тікета при читанні: id у description агента > claim сесії > гілка `t/<id>` > `.worktrees/<id>`. Витрати головної сесії без тікета - рядок «оркестратор / без тікета».

```
$ myvec tokens --project . --by agent,model --since 7d
agent    model              запитів  input  output  cache read  cache write  усього  кеш
-------  -----------------  -------  -----  ------  ----------  -----------  ------  ---
coder    claude-sonnet-5-5     1 204  310,2 тис.  1,1 млн  41,8 млн  3,2 млн  46,4 млн  90%
main     claude-opus-4-7         388   95,0 тис.  420,3 тис.  12,6 млн  1,9 млн  15,0 млн  84%
усього                         1 592  405,2 тис.  1,5 млн  54,4 млн  5,1 млн  61,4 млн  89%
```

Приватність: тексту повідомлень, промптів і description не зберігається - лише числа, ідентифікатори, модель, effort, `cwd`, гілка й до 3 id тікетів з description. Claude Code видаляє транскрипти через ~30 днів, тому `tokens.db` - єдина історія витрат: її не можна відновити (`rebuild` її не чіпає), бекапте разом із `~/MyVector`. `relocate` переписує `cwd` і в `tokens.db`. API лише читання: `GET /api/tokens/project?project=&days=7|14|30` і `GET /api/tokens/ticket?project=&id=` (звіти `tokens.project_report`/`ticket_card`; без `cwd`, `session_id`, `git_branch`; інший `days` - 400, невідомий проєкт - 404, збір вимкнено - 200 з порожніми масивами). `/api/status` має ключ `tokens` (`enabled, files, requests, last_scan, pending_bytes`).

### Виклики інструментів (`tool_calls`, `myvec searches`, ADR-029)

Той самий прохід колектора класифікує блоки `tool_use` і пише в `tokens.db.tool_calls` лише `tool_id, file_id, ts, seq, cat, cwd` (категорії `myvec`, `recall`, `self`; команда й вивід не зберігаються). `tokens.db` - схема v3: v2 додала `token_files.tool_seq`, v3 (E8-16) - `tool_calls.kind` (`logs|count|file|explore|exact|name`, NULL = нерозібрано) і `tool_calls.doc` (0/1, ціль із розширенням доків), плюс `cat = 'read'` для Read файла-документа (шлях не зберігається). Мітки ставить `searchkind.kind_of` (чистий класифікатор без ФС поверх парсера `presearch`; `search.DOC_EXTS` реекспортується з `searchkind`). При першому відкритті демоном/CLI - одноразовий дочит наявних транскриптів (суми токенів не міняються, мітки лише для рядків без міток): **доки дочит не завершився, підвид і частка (розв.) неповні**; рядки, чиї транскрипти вже видалено, лишаються «некласифіковано». `myvec searches` на ще не мігрованій (read-only) v2-БД не падає: всі self - некласифіковані. `myvec searches` поєднує це з `logs/searches.jsonl` (на кожен пошук: канал `cli|direct|ui`, k, кількість результатів, латентність, фактичний маршрут `route` (`vector|hybrid|fts-fallback`; `fts-fallback` - ембеддинг недоступний, пошук лише по FTS; UI показує бейдж «FTS»), довжина/слова/прапорці запиту й HMAC-хеш; **тексту запиту нема**). Текст у шині/`queries.jsonl` для UI маскується; рядки, записані збірками до E8-02, лишаються з сирим `q` до ротації. API й картка в UI - E8-06.

## Вимірювання прогонів (`myvec measure`)

```bash
myvec measure [--ticket ID] [--agent NAME] [--label TEXT] [--interval S] -- uv run pytest -q
```

Запускає команду з успадкованим stdio, міряє її дерево процесів і наприкінці друкує в **stderr** рядок-зведення
(stdout команди не змішується), придатний для `## Log` тікета:

```
measure: uv run pytest -q · 34,5 с · CPU 23,9 с · 0,7/1,3 ядра · 138 МБ · exit 0
```

Поля: wall-час, CPU-час усього дерева (user+sys), середні/пікові ядра, пік пам'яті дерева (USS, сума), код виходу;
якщо прогін перервано сигналом, додається `· перервано`. CPU береться з `os.wait4` (точно, враховує нащадків, яких
дочекалася команда: xdist, paratest) і з семплінгу `psutil` (раз на `--interval`, за замовчуванням 1 с) - більше з двох;
піки ядер і пам'яті дає лише семплінг, без `psutil` вони `–`/`null`. Команда в зведенні й у БД маскується як секрети.

**Що зберігається.** Рядок у таблиці `test_runs` (схема v7): проєкт, cwd, гілка, тікет (`--ticket`, інакше claim
сесії, гілка `t/<id>` або worktree `.worktrees/<id>`; фіксується на момент запуску), агент, label, `source`
(`wrapper`; `passive` пише демон, див. нижче), стан (`running` → `done`/`lost`), таймінги, CPU, ядра, пік пам'яті, код виходу,
сигнал, `interrupted`. На час прогону рядок має `state=running`. Якщо БД зайнята довше 300 мс або недоступна, подія
йде в `state/test_runs.jsonl`, а демон (тік) чи наступний `measure` переносить її в таблицю без дублів. `rebuild`
таблицю не відновлює (як `sessions`). Без демона обгортка пише в БД напряму.

**Код виходу** обгортки дорівнює коду команди; смерть від сигналу N дає `128+N`; команду не знайдено - 127, не
виконується - 126 (без запису). Помилка БД код не змінює.

**Ctrl+C.** З терміналу сигнал отримує вся група процесів, тож команда зупиняється сама, а обгортка записує прогін
(`signal=2`, `interrupted`) і виходить з кодом 130. Обмеження: поки обгортка на передньому плані терміналу,
`kill -INT <pid обгортки>` з іншого шела дитині **не** пересилається (вважається, що Ctrl+C уже дійшов) - зупиняйте
саму команду або шліть SIGTERM (він пересилається завжди).

## Пасивне виявлення прогонів тестів

Прогони, запущені поза `myvec measure` (з терміналу, агентом, IDE), демон ловить сам: `passive.Detector` підключений
слухачем до семплера навантаження (окремого потоку й налаштування нема, працює, поки ввімкнено семплер). Впізнаються
`pytest`/`python -m pytest`, `phpunit`, `paratest`, `php artisan test`, `vitest` (лише з `run`/`--run`, bare `vitest`
у TTY - watch, його не пишемо), `jest`, `npm|pnpm|yarn|bun test` (в т.ч. `uv run`/`npx`-обгортки). Watch-режими
(`--watch`, `--watchAll`, `vitest watch|dev`), `--collect-only`/`--co`/`--version`/`--help` ігноруються. Пишуться лише
прогони з `cwd` у проєктах із реєстру; дублі з `myvec measure` відсікаються (env-маркер `MYVEC_MEASURE_RUN` або предок
`myvec measure`). Прогін коротший за ~5 с (інтервал семплера) може не потрапити.

Рядок `test_runs` має `source='passive'`, `label` - вид (`pytest`, `npm test`, ...), `outcome` - `ended`
(«завершено»: код виходу, сигнал і RSS невідомі, тому не `passed`). У UI - позначка «пасивно». Події йдуть у
`state/test_runs.jsonl` (під `flock`, спільно з `measure`), демон фолдить їх щотіку; `run_uid` детермінований, тож
перезапуск демона не дублює прогін, а зниклий корінь стає `lost`. З env кандидата читаються лише `MYVEC_MEASURE_RUN` і
`CLAUDE_CODE_SESSION_ID`. Обмеження: чужі команди пишуться без згоди (маскування `safe_command` ловить `KEY=val` і
відомі токени, але не `--password x` окремим аргументом); watch-режим усередині скрипта `npm test` запишеться як дуже
довгий прогін. Медіани `summary` беруть лише прогони з `exit_code=0`, тож пасивні в них не потрапляють.

## Хмара (локально)

`cloud/` - Cloudflare Worker + D1 для MyVector PM (ADR-047, ADR-050): прийом снапшотів нод `POST /node/v1/snapshot` (токен ноди, у D1 лише SHA-256), read API `/api/v1/*` (fail-closed до Access JWT, E4-05). **Нічого не задеплоєно**: лише `wrangler dev` і vitest. Команди, схема D1, `mint-token` - у [cloud/README.md](cloud/README.md). `npm test` запускається окремо від `uv run pytest -q` (Python-only, `tests/test_cloud_static.py` лише читає файли).

## Тести

```bash
uv run pytest -q
```
