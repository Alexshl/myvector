# cloud/ - Worker MyVector PM (локально)

Cloudflare Worker + D1: прийом і читання снапшотів нод (ADR-047, ADR-050). **Нічого не деплоїти**: лише `wrangler dev` (локальна D1 у `cloud/.wrangler`, не в git) і vitest. Деплой, акаунт, Access JWT - E4-05; WebSocket/Durable Object - E4-06; команди - E4-07.

Усі команди з `cloud/`, node >= 22.12 (на devbox `PATH=$HOME/.nvm/versions/node/v22.23.4/bin:$PATH`). Телеметрія wrangler вимкнена (`WRANGLER_SEND_METRICS=false` у скриптах, `send_metrics = false`).

## Команди

- `npm ci` - залежності з `package-lock.json` (точні версії; `npm ls workerd` - одна копія). Якщо `npm install` (npm 10) падає з `Cannot read properties of null (reading 'edgesOut')` - оновлювати lock через `npx npm@11 install`; `npm ci` з lock працює на npm 10.
- `npm test` - vitest + `@cloudflare/vitest-pool-workers` (послідовно, один воркер; devbox має мало пам'яті - не запускати разом із `uv run pytest -q`). Пакет перейменовано в `@cloudflare/vitest-plugin`; міграція - окремий тікет.
- `npm run typecheck` - `tsc --noEmit`.
- `npm run db:migrate:local` - застосувати `migrations/` до локальної D1.
- `npm run dev` - `wrangler dev --local --var ACCESS_DEV_BYPASS:1` на 127.0.0.1:8787.
- `npm run mint-token -- --node NAME --out PATH [--label L] [--revoke-oldest]` - токен лише у файл `0600` (stdout: ім'я, шлях, 8 символів хешу); у D1 лише SHA-256; не більше 2 активних на ноду. Для E4-03 `--out` ставити в тестовий `MYVEC_HOME/state/cloud.token`, не в справжній `~/MyVector`.

`pytest` лишається Python-only; `tests/test_cloud_static.py` лише читає файли (точні версії, `.gitignore`, у `wrangler.toml` нема `account_id`/routes/`workers_dev`).

## API

- `POST /node/v1/snapshot` - `Authorization: Bearer <43 base64url>`, `Content-Type: application/json`, тіло <= 8 МіБ: `{v:1, type:"snapshot", gen, ts, node?, projects:[{project, fingerprint, head{sha,time,branch}, dirty, tickets_mtime, tickets[]}], sessions[]}`. 200 `snapshot.ack`; 400 (схема, `v != 1`, PEM/AKIA), 401, 403 (`node` у тілі чужий), 409 `stale_gen`, 413, 415. `gen` менший за застосований - 409, той самий - 200 без запису, більший - атомарна заміна даних ноди.
- `GET /api/v1/{nodes,projects,tickets?project=[&node=],sessions[?node=&project=]}` - браузер. Fail-closed: 403, доки нема перевірки Access JWT (E4-05). Локальний обхід - лише `ACCESS_DEV_BYPASS=1` **і** host `localhost`/`127.0.0.1`. Змінної нема в `wrangler.toml`: вмикається `npm run dev` або gitignored `cloud/.dev.vars`; тести ставлять її по кейсу.
- `GET /node/v1/ws` - 501 (E4-06).

## Схема D1 (`migrations/0001_init.sql`)

- `nodes(name PK, created_at, last_seen_at, last_gen, last_snapshot_at, myvec_version)`
- `node_tokens(token_hash PK = hex SHA-256, node -> nodes ON DELETE CASCADE, created_at, label)`; сирого токена в D1 нема
- `node_projects(node, project, fingerprint, head_sha, head_time, head_branch, dirty, tickets_mtime, received_at; PK node+project)`
- `tickets(node, project, id, kind, status, parent, data JSON; PK node+project+id)` - у `data` лише поля білого списку
- `sessions(node, session_id, project, data JSON; PK node+session_id)`

Файли `*.sql` `myvec` не індексує (`filters.py`), тому схема продубльована тут.
