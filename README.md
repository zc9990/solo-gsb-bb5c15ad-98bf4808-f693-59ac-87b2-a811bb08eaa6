# 共享 JSON 配置三方合并 API

基于 **Python 3.11 + FastAPI + SQLite** 的共享 JSON 配置存储。文档更新携带
`base_revision`、`request_id` 与 [RFC 7386 JSON Merge Patch](https://www.rfc-editor.org/rfc/rfc7386)；
当基线过期（服务端已有更新修订）时，服务端对 **基线 / 当前版 / 来稿** 做递归三方比较：

- 双方修改的路径互不重叠，或改动结果相同 → **自动重放（replay）**，生成新修订；
- 同一路径被双方改成不同结果 → **409 冲突**，返回路径（RFC 6901 JSON Pointer）
  及三方值，**绝不写入**；
- 同一 `request_id` + 相同内容 → **幂等**，直接返回原结果；
- 同一 `request_id` + 不同内容（patch、base_revision 或 guards 变化）→ **409**。

更新还可携带可选 **`guards` 前置条件**：每项用 RFC 6901 JSON Pointer 要求
**当前修订**某处等于 `expected` 或必须缺失。服务端在同一写事务内、三方合并
之前按输入顺序逐条检查，任一不通过返回 `409 precondition_failed`
（列出期望 / 实际与缺失语义），不写修订、不占用 `request_id`；
`guards` 纳入幂等指纹。原子发布的每个更新项同样支持 `guards`。

需要"某段时间内只有我能写"的互斥语义时，可用 **文档级临时写入租约**：
`POST /documents/{id}/leases` 获取租约，服务端在 SQLite 写事务内按
**服务端 UTC** 判断过期，为新持有者分配**全局严格递增的 fencing token**
（响应字段 `lease_token`）。PATCH 与原子发布项可携带 `lease_token`：
目标文档存在未过期租约时，仅 token 匹配的请求可写入，否则
`409 lease_conflict`（不写修订、不占用 `request_id` / `release_id`）；
租约过期后旧 token 永久失效，新持有者可立即接管；
无租约文档的原有写入行为完全不变。详见[写入租约](#写入租约leases)一节。

需要回滚某次历史修改时，可用 **`POST /documents/{id}/reverts` 按修订号撤销**：
请求携带待撤销的 `revision`、`request_id` 与可选 `author`。服务端以目标修订的
**前后快照**构造逆向 Merge Patch，再以目标修订为基线、当前内容为服务端版做三方合并：

- 目标修订必须**存在且大于 1**（修订 1 是创建快照，无可撤销的前态）
  → 否则 `422 invalid_revert_revision`；
- 目标修订**之后**发生在其他路径的修改自动保留；
- 目标涉及的路径后来被改成**不同值** → `409 revert_conflict`，
  返回 JSON Pointer 与 base/current/theirs 三方值，事务回滚、**不写新修订**；
- 撤销成功生成一条**普通可追溯修订**（逆向补丁存入 `patch`、目标修订号存入
  `base_revision`），最终候选仍须通过文档当前的 JSON Schema 契约；
- `request_id` 与 PATCH 共用幂等表：相同请求（`revision`/`author` 指纹一致）
  重放原结果，同 ID 不同内容（或与 PATCH 混用）→ `409 request_id_conflict`。

需要一次发布多个互不相关的文档时，可用 **`POST /releases` 原子发布**：
请求携带 `release_id` 与 2～20 项文档更新（`document_id`、`base_revision`、
`patch`、`author`，且 `document_id` 互不重复），服务端在**同一个 SQLite 写事务**内
逐项复用上面的三方合并与契约校验：

- 任一文档不存在、基线无效、路径冲突或契约校验失败
  → `422 release_failed`，`failures` 按输入顺序汇总每项的结构化错误，
  **所有文档均不产生修订**，且 `release_id` 不被占用（修正后可用同 ID 重试）；
- 全部通过 → 一次性写入各文档新修订，返回每项最终的 `revision` / `content`；
- `release_id` 对**规范化整批请求**（含项顺序与 `author`）持久化幂等：
  相同请求重放原结果，不同请求返回 `409 release_id_conflict`；失败不占用 ID。

每个文档还可以挂载一个**可选的 [JSON Schema 2020-12](https://json-schema.org/draft/2020-12) 契约**：

- 设置契约时先校验 Schema **本身**是否合法，并在**同一事务**内验证当前文档；
  任一不通过都返回 `422` 且**不替换**既有契约；
- 文档存在契约时，PATCH 先完成 Merge Patch 与三方重放，再校验**最终候选内容**；
  失败返回 `422 schema_validation_failed`，错误按实例路径稳定列出，
  不写入修订、不占用 `request_id`（修正后可用同 ID 重试）；
- 删除契约后，PATCH 立即恢复为无契约的原更新行为。

需要审计两个历史修订之间的差异时，可用 **`GET /documents/{id}/diff?from_revision=&to_revision=`**：
递归比较两个已存在快照，按 RFC 6901 路径稳定排序返回 `add` / `remove` / `replace` 变更；
每项同时给出 `before` / `after`，并用 `missing` 区分键缺失与真实 `null`，数组作为整体值比较；
任一修订不存在返回 `404` 并指出缺失修订，响应附各操作计数，空差异返回空数组。

需要追溯每个字段"最后是谁改的"时，可用 **`GET /documents/{id}/blame?revision=` 历史字段归因**：
服务端在**同一个 SQLite 读事务**内从修订 1 起按修订顺序比较相邻快照，
对目标快照中的每个叶子值给出 `path`（RFC 6901）、`value`、`last_changed_revision`、
`author`、`request_id` 与 `created_at`；对象递归到叶子，数组整体视为叶子；
路径删除后再次出现时归属于**重建它**的修订，即使值与删除前相同；
结果按 `path` 稳定排序，目标修订不存在返回 `404`，查询不产生任何写入。

配置被拆分到多个文档、需要拼装成一份完整视图时，可用
**`GET /documents/{id}/resolved?revision=` 跨文档引用解析**：
文档中**仅含 `$ref` 键**且值形如 `doc:<ID>@<正整数|current>#<RFC6901指针>` 的对象
会被视为引用，服务端在**同一个 SQLite 读事务快照**内固定根文档与所有 `current`
引用的修订，递归替换对象与数组中的引用（只读，不产生修订）：

- 返回 `resolved`（解析结果）、`revision`（根修订）与 `dependencies`
  （按首次遍历排序、按 文档+修订+指针 去重的依赖清单）；
- 文档 / 修订 / 指针不存在 → `422` 及从根文档出发的完整引用链；
- 依赖成环 → `409` 及首尾闭合的环链；最大解析深度为 32。

提交前想确认"这个 patch 会影响哪些引用我的配置视图"时，可用
**`POST /documents/{id}/impact-preview` 变更影响预演**：请求携带
`base_revision`、`patch` 与 1～20 个 `root_document_ids`，服务端在
**同一个 SQLite 读事务快照**内先按现有三方合并与契约规则生成**不落库候选**，
再分别以当前内容和候选覆盖目标文档、递归解析每个根文档的 `$ref`：

- 候选冲突 / 契约失败 / 基线无效 → 整体返回与 PATCH 相同的结构化错误；
- 每个根返回固定修订、两遍解析的依赖并集与按差异审计语义排序的
  `resolved_changes`；单个根解析失败仅在该根结果中记录引用链错误，
  不影响其他根；重复根按首次出现去重；
- 预演为只读分析：**不创建修订、不占用 `request_id`、不改变租约**
  （也不检查租约与 guards）。

## 一键启动

需要 Docker 与 Docker Compose v2：

```bash
docker compose up -d --build
```

容器启动时自动建表（FastAPI lifespan 执行 `CREATE TABLE IF NOT EXISTS`），
无需任何初始化命令。SQLite 数据持久化在命名卷 `config-data`（容器内 `/data`）。

停止与清理：

```bash
docker compose down          # 停止，保留数据
docker compose down -v       # 停止并删除数据卷
```

## 配置项

全部通过环境变量配置（均有默认值，可直接使用 `.env`，模板见 `.env.example`）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CONFIG_PORT` | `8000` | 宿主机映射端口（容器内固定监听 8000） |
| `CONFIG_DB_PATH` | `/data/config.db` | SQLite 数据库文件路径，父目录自动创建 |
| `CONFIG_HOST` | `0.0.0.0` | 监听地址 |
| `CONFIG_HISTORY_MAX_LIMIT` | `200` | 修订历史单页返回上限 |

## 访问地址与端口

默认端口 `8000`：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/docs | Swagger UI（可在线调试） |
| http://localhost:8000/redoc | ReDoc 文档 |
| http://localhost:8000/openapi.json | OpenAPI 描述 |
| http://localhost:8000/health | 健康检查（容器 HEALTHCHECK 也使用它） |

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/documents` | 创建文档（修订号从 1 开始） |
| `GET` | `/documents` | 列出文档 |
| `GET` | `/documents/{id}` | 读取当前版本（含 `revision`） |
| `PATCH` | `/documents/{id}` | 提交 Merge Patch（三方合并，存在契约时最终校验；可选 `guards` 前置条件、`lease_token` 租约凭证） |
| `POST` | `/documents/{id}/reverts` | 按修订号撤销（逆向补丁 + 三方合并，生成普通可追溯修订） |
| `POST` | `/releases` | 原子发布 2～20 个文档更新（单事务、聚合失败、整批幂等；更新项可携带 `lease_token`） |
| `POST` | `/documents/{id}/leases` | 获取写入租约（新持有者分配严格递增的 fencing token） |
| `POST` | `/documents/{id}/leases/renew` | 续期租约（holder + token 匹配当前未过期租约，沿用 token） |
| `POST` | `/documents/{id}/leases/release` | 释放租约（holder + token 匹配才删除；不存在时幂等 no-op） |
| `GET` | `/documents/{id}/leases` | 查询当前租约（含按服务端 UTC 计算的 `expired` 标志） |
| `GET` | `/documents/{id}/revisions` | 修订历史（支持 `limit`/`offset`/`order=asc|desc`） |
| `GET` | `/documents/{id}/revisions/{rev}` | 查询指定修订的内容与 patch |
| `GET` | `/documents/{id}/diff` | 比较两个修订快照（`from_revision`/`to_revision`），返回按 RFC 6901 路径排序的 add/remove/replace 差异 |
| `GET` | `/documents/{id}/blame` | 历史字段归因（`revision` 指定目标快照）：每个叶子值最后由哪次修订改变 |
| `GET` | `/documents/{id}/resolved` | 跨文档引用解析（`revision` 可选，默认 `current`），返回 `resolved` 与去重依赖清单 |
| `POST` | `/documents/{id}/impact-preview` | 变更影响预演（只读）：生成不落库候选，对比各根文档当前 / 候选两遍解析结果 |
| `PUT` | `/documents/{id}/schema` | 设置 / 替换 JSON Schema 2020-12 契约 |
| `GET` | `/documents/{id}/schema` | 查询当前契约（未设置返回 `404 schema_not_found`） |
| `DELETE` | `/documents/{id}/schema` | 删除契约（幂等，删除后恢复原更新行为） |

### 创建文档

```bash
curl -X POST http://localhost:8000/documents \
  -H 'Content-Type: application/json' \
  -d '{"id": "demo", "content": {"a": 1, "b": 2, "c": {"x": 1}}, "author": "alice"}'
# id 可省略，省略时服务端生成；id 重复返回 409 document_exists
```

### 读取文档

```bash
curl http://localhost:8000/documents/demo
# {"id":"demo","revision":1,"content":{"a":1,"b":2,"c":{"x":1}},...}
```

读取后请记下 `revision`，作为下次更新的 `base_revision`。

### 更新（快路径：基线即当前版）

```bash
curl -X PATCH http://localhost:8000/documents/demo \
  -H 'Content-Type: application/json' \
  -d '{"base_revision": 1, "request_id": "req-001", "patch": {"a": 100}}'
```

```json
{
  "id": "demo",
  "revision": 2,
  "base_revision": 1,
  "request_id": "req-001",
  "replayed": false,
  "changed": true,
  "idempotent": false,
  "content": {"a": 100, "b": 2, "c": {"x": 1}}
}
```

### 更新（基线过期：路径不冲突 → 自动重放）

在别人基于 rev1 已把 `a` 改成 100（当前 rev2）之后，你仍基于 rev1 只改 `b`：

```bash
curl -X PATCH http://localhost:8000/documents/demo \
  -H 'Content-Type: application/json' \
  -d '{"base_revision": 1, "request_id": "req-002", "patch": {"b": 200}}'
```

```json
{ "revision": 3, "replayed": true, "changed": true,
  "content": {"a": 100, "b": 200, "c": {"x": 1}} }
```

三方在对象层级递归比较；重放结果与当前版完全相同时不产生新修订
（`"changed": false`，`revision` 保持当前号）。

### 更新（分歧路径 → 409，且不写入）

rev1 中 `a=1`，服务端已把 `a` 改成 `100`，来稿基于 rev1 要改成 `999`：

```bash
curl -i -X PATCH http://localhost:8000/documents/demo \
  -H 'Content-Type: application/json' \
  -d '{"base_revision": 1, "request_id": "req-003", "patch": {"a": 999}}'
```

```json
HTTP/1.1 409 Conflict
{
  "detail": {
    "error": "conflict",
    "document_id": "demo",
    "request_id": "req-003",
    "base_revision": 1,
    "current_revision": 2,
    "conflicts": [
      {
        "path": "/a",
        "values": {"base": 1, "current": 100, "theirs": 999},
        "missing": []
      }
    ]
  }
}
```

- `path` 为 RFC 6901 JSON Pointer，根冲突为 `""`，键中的 `~`/`/` 转义为 `~0`/`~1`；
- `values` 给出该路径上的 **基线值 / 服务端当前值 / 来稿值**；
- 键缺失（被删除）时对应位置为 `null`，并在 `missing` 中标注
  （如 `"missing": ["current"]`，以区别于真实存储的 JSON `null`）；
- 冲突请求在事务内回滚：文档内容、修订号均不变，且该 `request_id` 不被占用，
  调用方修正基线/内容后可用同一 ID 重试。

### 幂等

```bash
# 同 request_id + 同 (base_revision, patch) 再发任意次 → 返回原结果
# 响应中 "idempotent": true，不产生新修订
```

同 `request_id` 但 `base_revision`、`patch` 或 `guards` 不同 → `409 request_id_conflict`。
指纹为 `sha256(base_revision + 规范化JSON(patch) [+ 规范化JSON(guards)])`
（未声明 guards 时不追加第三段），幂等记录按 `(document_id, request_id)` 持久化。

### 更新前置条件（guards）

`PATCH /documents/{id}` 接受可选 `guards` 数组，声明“当前修订必须满足”的
前置条件。服务端在**同一写事务**内、基线检查 / 三方合并 / 契约校验**之前**，
按输入顺序逐条对**当前修订**求值；全部通过才进入原有更新流程。

每条 guard 用 RFC 6901 JSON Pointer 指定位置，且**恰好**声明一种条件：

| 条件 | 形态 | 语义 |
| --- | --- | --- |
| 等值 | `{"path": "/a/b", "expected": <任意JSON>}` | 当前内容在该指针处的值必须等于 `expected`（规范化比较：键序无关；`1`/`1.0`/`true` 视为不同值） |
| 缺失 | `{"path": "/a/b", "missing": true}` | 该指针处必须**不存在**（真实存储的 `null` 视为存在，不等于缺失） |

```bash
# 仅当当前版本 mode 仍为 "off"、且不存在 legacy 键时，才把 mode 切到 "on"
curl -X PATCH http://localhost:8000/documents/demo \
  -H 'Content-Type: application/json' \
  -d '{
    "base_revision": 3,
    "request_id": "req-100",
    "patch": {"mode": "on"},
    "guards": [
      {"path": "/mode",   "expected": "off"},
      {"path": "/legacy", "missing": true}
    ]
  }'
```

任一 guard 不通过 → `409 precondition_failed`，`failures` 按输入顺序列出
**全部**未通过项；每项给出 `expected` / `actual`，并用 `missing` 标注缺失侧
（与三方冲突、差异审计的 `missing` 约定一致）：

```json
HTTP/1.1 409 Conflict
{
  "detail": {
    "error": "precondition_failed",
    "document_id": "demo",
    "request_id": "req-100",
    "base_revision": 3,
    "current_revision": 3,
    "failures": [
      {"path": "/mode",   "expected": "off", "actual": "on", "missing": []},
      {"path": "/legacy", "expected": null,  "actual": 1,    "missing": ["expected"]},
      {"path": "/absent", "expected": 5,     "actual": null, "missing": ["actual"]}
    ]
  }
}
```

- `missing: []` —— 等值条件：两侧都存在但值不同；
- `missing: ["actual"]` —— 等值条件：指针处不存在（`actual` 为 `null` 占位）；
- `missing: ["expected"]` —— 缺失条件：指针处存在（`expected` 为 `null` 占位）。

语义要点：

- guards 针对**当前修订**求值，与 `base_revision` 无关；基线过期时先过
  guards 再做三方重放（guards 失败优先于 `unknown_base_revision`、
  `conflict` 与契约校验）；
- 失败在事务内回滚：**不写修订、不占用 `request_id`**，修正后可同 ID 重试；
- `guards` 纳入幂等指纹：同一 `request_id` 改变 guards（新增 / 删除 / 换序 /
  改值）→ `409 request_id_conflict`；空数组 `[]` 与省略 `guards` 等价，
  显式 `missing: false` 与省略该键等价；
- 指针非法（不是 `""` 或 `/` 开头、`~` 转义非法）、同时声明 `expected` 与
  `missing: true`、或两者都不声明 → `422`（模型层校验，不落库、不占用 ID）；
- 幂等重放直接返回原结果，不重新求值 guards；
- `POST /releases` 的每个更新项同样支持 `guards`：任一项未通过按
  `precondition_failed`（`status_code: 409`）聚合进 `failures`，
  整批回滚且不占用 `release_id`；guards 同样纳入整批幂等指纹。

### 撤销修订（POST /documents/{id}/reverts）

撤销不是“把文档指针拨回旧版”，而是一次**带三方合并的普通更新**：
服务端取出目标修订 `revision` 的后快照（该修订的内容）与前快照（`revision - 1` 的内容），
按 RFC 7386 构造**逆向 Merge Patch**，再以「目标修订 / 当前版 / 前态」做递归三方比较。
因此目标修订之后其他人在**不相干路径**上的修改会被保留，只有同一路径后来又被
改成不同值时才报冲突。

```bash
# 历史：rev1 {"a":1,"b":2} → rev2 a=100 → rev3 c.y=2
curl -X POST http://localhost:8000/documents/demo/reverts \
  -H 'Content-Type: application/json' \
  -d '{"revision": 2, "request_id": "undo-001", "author": "alice"}'
```

```json
{
  "id": "demo",
  "revision": 4,
  "reverted_revision": 2,
  "request_id": "undo-001",
  "replayed": true,
  "changed": true,
  "idempotent": false,
  "content": {"a": 1, "b": 2, "c": {"x": 1, "y": 2}}
}
```

- `reverted_revision` 为请求中指定的目标修订号；撤销本身是**新的普通修订**：
  修订历史中该条的 `patch` 为逆向补丁、`base_revision` 为目标修订号、
  `request_id` 为本次撤销的去重 ID、`author` 为请求所给作者；
- 目标修订就是当前版时走快路径（`replayed: false`）；其后还有修订时走三方重放
  （`replayed: true`）；重放结果与当前版完全相同则 `changed: false`、不产生新修订；
- 目标修订不存在或 `revision <= 1` → `422 invalid_revert_revision`
  （响应含 `revision` 与 `current_revision`）；
- 撤销成功的候选内容仍须通过文档**当前**的 JSON Schema 契约（若有），
  失败为 `422 schema_validation_failed`，不写修订、不占用 `request_id`。

#### 撤销冲突：409 revert_conflict

目标修订涉及的路径在此后被改成了不同值时，冲突响应与 PATCH 同构，
`error` 为 `revert_conflict`，并额外给出 `revert_revision`：

```json
HTTP/1.1 409 Conflict
{
  "detail": {
    "error": "revert_conflict",
    "document_id": "demo",
    "request_id": "undo-001",
    "revert_revision": 2,
    "base_revision": 2,
    "current_revision": 3,
    "conflicts": [
      {
        "path": "/a",
        "values": {"base": 100, "current": 300, "theirs": 1},
        "missing": []
      }
    ]
  }
}
```

三方值的含义：`base` 是目标修订（后快照）在该路径的值，`current` 是当前值，
`theirs` 是撤销想要恢复的前态值；删除语义同样用 `missing` 标注。
冲突在事务内回滚：**不写新修订、不占用 `request_id`**，
调用方解决分歧（或改用更新的修订号）后可用同一 ID 重试。

#### 撤销的幂等

撤销请求与 PATCH **共用** `(document_id, request_id)` 幂等表，
指纹为 `sha256("revert" + 规范化JSON([revision, author]))`：

- 相同请求重放（任意次、哪怕文档又前进了）→ 返回首次撤销的原始结果，
  `"idempotent": true`，不重新合并、不产生新修订；
- 同一 `request_id` 对应不同的 `revision` 或 `author` → `409 request_id_conflict`；
- 同一 `request_id` 先用于 PATCH、再用于撤销（或反过来）同样 → `409`；
- 只有**成功**的撤销才持久化幂等记录（冲突 / 契约失败不占用 ID）。

### 原子发布（POST /releases）

一次发布把多个互不重复的文档更新放进**同一个 SQLite 写事务**：
逐项复用单文档 PATCH 的 guards 前置检查 / Merge Patch / 三方重放 / 契约校验逻辑，
**全部通过才提交**，任一失败则整体回滚。每批限 **2～20 项**，
`updates` 中 `document_id` 必须互不重复（同一文档的多项修改请在调用方合并为一个 patch）。
每个更新项可携带可选 `guards`（语义与单文档 PATCH 完全一致，见上节）。

```bash
curl -X POST http://localhost:8000/releases \
  -H 'Content-Type: application/json' \
  -d '{
    "release_id": "release-2026-01-01-001",
    "updates": [
      {"document_id": "demo",  "base_revision": 1, "patch": {"a": 100}, "author": "alice"},
      {"document_id": "other", "base_revision": 3, "patch": {"b": 200}, "author": "bob"}
    ]
  }'
```

```json
{
  "release_id": "release-2026-01-01-001",
  "idempotent": false,
  "results": [
    {"document_id": "demo",  "revision": 2, "base_revision": 1, "replayed": false,
     "changed": true, "author": "alice", "content": {"a": 100, "b": 2}, "created_at": "..."},
    {"document_id": "other", "revision": 4, "base_revision": 3, "replayed": true,
     "changed": true, "author": "bob",   "content": {"b": 200},      "created_at": "..."}
  ]
}
```

`results` 按输入顺序排列；每项字段与单文档 PATCH 响应一致
（`replayed` 表示该项是否走了过期基线三方重放；重放结果与当前版相同的项
`changed=false`、不产生新修订）。发布产生的修订在修订历史中以
`request_id = release_id` 记录，便于从文档侧追溯所属发布。

#### 失败聚合：422 release_failed

任一项不通过都会收集错误并继续处理剩余项（不止于首个失败），
最终按 **输入顺序** 返回 `failures`，HTTP 状态统一为 `422`；
每个失败项自带其对应单文档接口的 `error` 与 `status_code`：

```json
HTTP/1.1 422 Unprocessable Entity
{
  "detail": {
    "error": "release_failed",
    "release_id": "release-2026-01-01-001",
    "reason": "发布中存在失败项，全部文档未产生修订",
    "failures": [
      {"index": 1, "document_id": "ghost", "error": "not_found", "status_code": 404},
      {"index": 2, "document_id": "demo", "error": "unknown_base_revision",
       "status_code": 422, "base_revision": 99, "current_revision": 2,
       "reason": "base_revision 不存在"},
      {"index": 3, "document_id": "other", "error": "conflict", "status_code": 409,
       "base_revision": 3, "current_revision": 5,
       "conflicts": [{"path": "/b", "values": {"base": 1, "current": 9, "theirs": 200},
                      "missing": []}]},
      {"index": 4, "document_id": "bound", "error": "schema_validation_failed",
       "status_code": 422, "base_revision": 1, "current_revision": 1,
       "errors": [{"path": "/a", "validator": "minimum", "message": "...",
                   "schema_path": "/properties/a/minimum"}]},
      {"index": 5, "document_id": "guarded", "error": "precondition_failed",
       "status_code": 409, "base_revision": 2, "current_revision": 2,
       "failures": [{"path": "/mode", "expected": "off", "actual": "on",
                     "missing": []}]}
    ]
  }
}
```

回滚保证：响应 422 时**没有任何文档**产生新修订（包括同批本可成功的项），
且该 `release_id` **不被占用**——修正请求内容后可用同一个 ID 重试。
请求体本身的问题（批大小越界、`document_id` 重复、字段不合法等）
在模型层直接返回 `422` 校验错误，同样不落库、不占用 ID。

#### 整批幂等

指纹为 `sha256(规范化JSON(按序的 [document_id, base_revision, patch, author] 列表))`
（任一项声明 `guards` 时，每项追加其 guards 规范化形态，未声明的项为 `null`）：
JSON 空白与键顺序不影响判定，但**项顺序、文档集合、基线号、patch、author、
guards 的差异都视为不同请求**。

- 相同请求重放（即使其中基线号已经过期）→ 返回首次发布的原始结果，
  `"idempotent": true`，不重新合并、不产生新修订；
- 同一 `release_id` 对应不同请求 → `409 release_id_conflict`；
- 只有**成功**的发布才持久化幂等记录（失败不占用 ID，见上）；
- 发布幂等记录独立于单文档 `request_id` 表，两者同名也不冲突。

### 写入租约（leases）

文档级**临时写入租约**提供"某段时间内只有持约人能写"的互斥语义。
租约状态存放在 `leases` 表（每文档至多一行），fencing token 计数器存放在
独立的 `lease_fencing` 表，均随 SQLite 数据卷持久化；所有获取 / 续期 / 释放
与写入门禁检查都在 **SQLite 写事务内按服务端 UTC** 判断过期，并发安全。

#### 获取租约

```bash
curl -X POST http://localhost:8000/documents/demo/leases \
  -H 'Content-Type: application/json' \
  -d '{"holder": "deploy-worker-7", "ttl_seconds": 30}'
```

```json
HTTP/1.1 201 Created
{
  "document_id": "demo",
  "holder": "deploy-worker-7",
  "lease_token": 5,
  "ttl_seconds": 30,
  "acquired_at": "2026-09-20T08:00:00.000000Z",
  "expires_at": "2026-09-20T08:00:30.000000Z",
  "server_time": "2026-09-20T08:00:00.000000Z",
  "reused": false
}
```

- `lease_token` 即 **fencing token**：全局（跨文档）严格递增、从 1 开始，
  由独立计数器分配，租约释放 / 被接管都不会使计数器回退；
- 无租约或租约已过期 → 新持有者接管，分配新 token，返回 `201`
  （`reused: false`）；**旧 token 自此永久失效**——即使同一 holder 重新
  获取也会得到更大的新 token；
- 同一 holder 对**未过期**租约重复获取 → 沿用当前 token，仅按新的
  `ttl_seconds` 刷新过期时间，返回 `200`（`reused: true`）；
- 其他 holder 持有未过期租约 → `409 lease_conflict`
  （响应含当前 `holder` 与 `expires_at`）；
- `ttl_seconds` 默认 30，范围 (0, 3600]，支持小数（如 `0.5`）。

#### 续期租约

```bash
curl -X POST http://localhost:8000/documents/demo/leases/renew \
  -H 'Content-Type: application/json' \
  -d '{"holder": "deploy-worker-7", "lease_token": 5, "ttl_seconds": 30}'
```

holder 与 `lease_token` 必须**同时匹配**当前未过期租约；成功时沿用当前
token，仅把过期时间顺延为服务端 UTC 当前时间 + `ttl_seconds`。
租约不存在 → `404 lease_not_found`；已过期 → `409 lease_conflict`
（旧 token 已永久失效，需重新获取）；holder / token 不匹配 → `409 lease_conflict`。

#### 释放租约

```bash
curl -X POST http://localhost:8000/documents/demo/leases/release \
  -H 'Content-Type: application/json' \
  -d '{"holder": "deploy-worker-7", "lease_token": 5}'
# 200 → {"document_id":"demo","released": true}
```

holder 与 `lease_token` 匹配当前租约才删除；释放后文档回到无租约状态，
写入恢复原有行为。租约不存在时为幂等 no-op（`released: false`）；
凭证不匹配 → `409 lease_conflict`，租约保持有效。

#### 查询租约

```bash
curl http://localhost:8000/documents/demo/leases
# 200 → {"document_id":"demo","holder":"deploy-worker-7","lease_token":5,
#        "acquired_at":"...","expires_at":"...","server_time":"...","expired":false}
```

只读接口：`expired` 按服务端 UTC 当场计算；过期租约在被接管或释放前仍可
查到（`expired: true`）；从未租约或已释放 → `404 lease_not_found`。

#### 租约对写入的约束

`PATCH /documents/{id}` 与 `POST /releases` 的每个更新项都可携带可选
`lease_token`；服务端在写事务内、guards 与三方合并**之前**检查：

| 文档租约状态 | 请求携带匹配 token | 请求携带错误 / 失效 token | 请求不携带 token |
| --- | --- | --- | --- |
| 存在未过期租约 | ✅ 正常写入 | `409 lease_conflict` | `409 lease_conflict` |
| 无租约 / 已过期 / 已释放 | —（不存在可匹配的 token） | `409 lease_conflict` | ✅ 正常写入（原有行为） |

- 冲突在事务内回滚：**不写修订、不占用 `request_id` / `release_id`**，
  修正 token（或释放 / 等待租约过期）后可用同一 ID 重试；
- 原子发布中某项租约冲突 → 按 `lease_conflict`（`status_code: 409`）聚合进
  `failures`，整批回滚；
- 无有效租约时携带 token 同样被拒绝：旧 token 在租约过期或释放后即永久
  失效，避免"以为有租约保护、实际没有"的误写；
- 不携带 token 的写入在无租约 / 租约过期后完全保持原有行为（兼容）；
- 撤销（`POST /documents/{id}/reverts`）不携带 token，因此文档存在未过期
  租约时同样被 `409 lease_conflict` 拒绝；
- `lease_token` 是写入凭证而非请求内容，**不纳入** PATCH / 发布的幂等指纹；
  已成功请求的幂等重放直接返回原结果，不重新检查租约。

```json
HTTP/1.1 409 Conflict
{
  "detail": {
    "error": "lease_conflict",
    "document_id": "demo",
    "request_id": "req-200",
    "base_revision": 3,
    "holder": "deploy-worker-7",
    "expires_at": "2026-09-20T08:00:30.000000Z",
    "reason": "文档存在未过期租约，写入必须携带匹配的 lease_token"
  }
}
```

### 修订历史

```bash
curl 'http://localhost:8000/documents/demo/revisions?order=desc&limit=20&offset=0'
curl http://localhost:8000/documents/demo/revisions/3
```

历史列表为轻量条目（修订号、基线号、request_id、是否重放、作者、时间）；
单条修订详情含完整 `content`、原始 `patch` 与重放标记。

### 修订差异审计（GET /documents/{id}/diff）

只读接口：递归比较 `from_revision` 与 `to_revision` 两个**已存在**快照，
按 RFC 6901 JSON Pointer 稳定排序返回 `add` / `remove` / `replace` 变更。
`from_revision` 大于 `to_revision` 也可以（反向审计，`before`/`after` 随之对调）。

```bash
# 历史：rev1 创建 {"a":1,"b":2,"c":{"x":1},"note":null,"tags":["u"]}
#       → rev2 a=100 → rev3 删 b、c 下加 y、note 改为 "hi"、tags 追加 "v"
curl 'http://localhost:8000/documents/demo/diff?from_revision=1&to_revision=3'
```

```json
{
  "document_id": "demo",
  "from_revision": 1,
  "to_revision": 3,
  "changes": [
    {"op": "replace", "path": "/a",    "before": 1,     "after": 100,        "missing": []},
    {"op": "remove",  "path": "/b",    "before": 2,     "after": null,       "missing": ["after"]},
    {"op": "add",     "path": "/c/y",  "before": null,  "after": 2,          "missing": ["before"]},
    {"op": "replace", "path": "/note", "before": null,  "after": "hi",       "missing": []},
    {"op": "replace", "path": "/tags", "before": ["u"], "after": ["u", "v"], "missing": []}
  ],
  "counts": {"add": 1, "remove": 1, "replace": 3, "total": 5}
}
```

变更项字段：

| 字段 | 说明 |
| --- | --- |
| `op` | `add`（仅新快照有）/ `remove`（仅旧快照有）/ `replace`（两侧都有但值不同） |
| `path` | 变更位置的 RFC 6901 JSON Pointer，根为 `""`，键中的 `~`/`/` 转义为 `~0`/`~1` |
| `before` / `after` | 旧 / 新快照在该路径的值；键缺失一侧为 `null` |
| `missing` | 哪一侧键缺失：`["before"]`（add）、`["after"]`（remove）、`[]`（replace）——据此区分“键不存在”与真实存储的 JSON `null`（如上例 `/note` 的 `before` 是真实 `null`，`missing` 为空） |

语义要点：

- 两侧都是对象才按键递归；**数组与标量作为整体值比较**（不按下标 / 逐字符差分），
  如上例 `/tags` 整体替换；
- 新增 / 删除的子树作为**整体值**挂在该路径上，不再向下展开；
- 结果按 `path` 稳定排序；`counts` 汇总各操作计数与 `total`；
- 空差异（如 `from_revision == to_revision`）返回 `"changes": []`、计数全 0；
- 文档不存在 → `404 not_found`；任一修订不存在 → `404 revision_not_found`，
  `missing_revisions` 指出缺失的修订号：

  ```json
  {"detail": {"error": "revision_not_found", "document_id": "demo",
    "from_revision": 1, "to_revision": 99, "missing_revisions": [99]}}
  ```

## 历史字段归因（GET /documents/{id}/blame）

只读接口：回答"目标快照里每个叶子值**最后是由哪次修订**改成现在这样的"。
服务端在**同一个 SQLite 读事务**（WAL 快照读，不阻塞并发写入）内从修订 1 起
按修订顺序比较相邻快照，把每个叶子路径归属到它最后一次发生变化的修订；
查询不产生任何写入。

```bash
# 历史：rev1 alice 创建 {"a":1,"b":2,"c":{"x":1},"tags":["u"]}
#       → rev2 bob 改 a=100（request_id: req-001）
#       → rev3 carol 删 b、tags 追加 "v"（request_id: req-002）
curl 'http://localhost:8000/documents/demo/blame?revision=3'
```

```json
{
  "document_id": "demo",
  "revision": 3,
  "entries": [
    {"path": "/a",     "value": 100,       "last_changed_revision": 2,
     "author": "bob",   "request_id": "req-001", "created_at": "2026-09-20T08:00:01.000000Z"},
    {"path": "/c/x",   "value": 1,         "last_changed_revision": 1,
     "author": "alice", "request_id": null,      "created_at": "2026-09-20T08:00:00.000000Z"},
    {"path": "/tags",  "value": ["u", "v"], "last_changed_revision": 3,
     "author": "carol", "request_id": "req-002", "created_at": "2026-09-20T08:00:02.000000Z"}
  ]
}
```

归因项字段：

| 字段 | 说明 |
| --- | --- |
| `path` | 叶子位置的 RFC 6901 JSON Pointer，根为 `""`，键中的 `~`/`/` 转义为 `~0`/`~1` |
| `value` | 目标快照在该路径的叶子值 |
| `last_changed_revision` | 该叶子最后一次发生变化（含出现 / 重建）的修订号 |
| `author` / `request_id` / `created_at` | 该修订落库时的作者、去重 ID 与时间（修订 1 为创建快照，`request_id` 为 `null`；发布产生的修订 `request_id` 为 `release_id`） |

语义要点：

- **叶子定义**：对象按键递归到叶子；**数组 / 标量 / `null` 整体视为叶子**
  （不按下标展开，与 RFC 7386 整体替换语义一致）；空对象没有可递归的子键，
  本身作为叶子（值 `{}`）；
- **删除后重建**：路径消失后其归属即被丢弃，之后再次出现时归属于**重建它**的
  修订——即使值与删除前完全相同（如上例中若 `b` 被删后又以原值 `2` 重建，
  `/b` 归属于重建修订而非修订 1）；
- 值比较用确定性序列化：`1` / `1.0` / `true` 这类落库字节不同的值视为不同，
  变化即重新归属；
- 归因按**快照内容**而非补丁：三方重放、撤销、原子发布产生的修订同样按
  其最终内容参与归属；
- 结果按 `path` 稳定排序；`revision` 必须是正整数（缺失或非法 → `422`）；
- 文档不存在 → `404 not_found`；目标修订不存在 → `404 revision_not_found`：

  ```json
  {"detail": {"error": "revision_not_found", "document_id": "demo", "revision": 99}}
  ```

## 跨文档引用解析（GET /documents/{id}/resolved）

只读接口：把分散在多个文档中的配置按 `$ref` 引用拼装成一份完整视图。
对象**恰好只含一个 `$ref` 键**、且值整体匹配
`doc:<ID>@<正整数|current>#<RFC6901指针>` 时视为引用，在对象与数组中递归替换；
其余形态的 `$ref`（含额外键、值不匹配格式）只是普通数据，原样保留。

- `@current` —— 解析时固定为目标文档的当前修订；`@<正整数>` —— 固定为指定历史修订；
- `#` 之后为 RFC 6901 JSON Pointer（单独的 `#` 指整篇文档），数组下标为
  `0` 或不带前导零的正整数；指针按字面解释，不做百分号解码；
- 被引用的目标值本身也会继续解析（目标文档里的嵌套引用在目标上下文中展开）；
- 同一 (文档, 修订, 指针) 目标在**当前解析路径**上再次出现 → 依赖成环；
  不同分支重复引用同一目标是合法的（菱形），直接复用首次解析结果。

```bash
# base: {"host": "db.internal", "port": 5432}
# app:  {"database": {"$ref": "doc:base@current#"}, "debug": false}
curl 'http://localhost:8000/documents/app/resolved'
```

```json
{
  "id": "app",
  "revision": 1,
  "resolved": {"database": {"host": "db.internal", "port": 5432}, "debug": false},
  "dependencies": [
    {"document_id": "base", "revision": 1, "pointer": ""}
  ]
}
```

响应字段：

| 字段 | 说明 |
| --- | --- |
| `revision` | 实际使用的根文档修订（`?revision=` 可指定历史修订，默认 `current`） |
| `resolved` | 全部引用递归展开后的文档内容 |
| `dependencies` | 依赖清单，按**首次遍历**排序、按 (文档, 修订, 指针) 去重；`current` 引用以快照内固定的具体修订号给出 |

一致性保证：根文档与所有 `current` 引用的修订在**同一个 SQLite 读事务快照**
（WAL 快照读，不阻塞并发写入）内固定，一次解析看到的内容彼此一致；
解析不写入任何数据。最大解析深度为 **32**（沿解析路径的引用跳数上限）。

### 解析失败：422 与完整引用链

被引用的文档 / 修订 / 指针不存在时返回 `422`，`chain` 给出从根文档出发的
**完整引用链**（每项含 `document_id` / `revision` / `pointer` /
`ref`（原始引用串）/ `at`（引用在上一跳文档中的位置））：

```bash
# app 的内容：{"database": {"$ref": "doc:ghost@current#/host"}}
curl -i 'http://localhost:8000/documents/app/resolved'
```

```json
HTTP/1.1 422 Unprocessable Entity
{
  "detail": {
    "error": "document_not_found",
    "document_id": "ghost",
    "reason": "被引用文档不存在",
    "chain": [
      {"document_id": "app",   "revision": 1, "pointer": "",      "ref": null, "at": null},
      {"document_id": "ghost", "revision": null, "pointer": "/host",
       "ref": "doc:ghost@current#/host", "at": "/database"}
    ]
  }
}
```

同类错误还有 `revision_not_found`（指定修订不存在）、`pointer_not_found`
（指针在目标修订中不存在或转义非法）、`max_depth_exceeded`（超过深度 32），
均携带完整引用链；`?revision=` 不是正整数或 `current` 时返回 `422 invalid_revision`。

### 依赖成环：409 与首尾闭合链

```json
HTTP/1.1 409 Conflict
{
  "detail": {
    "error": "reference_cycle",
    "document_id": "b",
    "revision": 1,
    "pointer": "/x",
    "reason": "依赖成环",
    "chain": [
      {"document_id": "b", "revision": 1, "pointer": "/x", "ref": "doc:b@1#/x", "at": "/start"},
      {"document_id": "c", "revision": 1, "pointer": "/y", "ref": "doc:c@1#/y", "at": "/b"},
      {"document_id": "b", "revision": 1, "pointer": "/x", "ref": "doc:b@1#/x", "at": "/c"}
    ]
  }
}
```

`chain` 只含环本身：链首与链尾是同一个 (文档, 修订, 指针)，首尾闭合。

## 变更影响预演（POST /documents/{id}/impact-preview）

只读接口：在**同一个 SQLite 读事务快照**内回答"如果现在提交这个 patch，
哪些根文档的解析视图会发生什么变化"。请求体：

| 字段 | 说明 |
| --- | --- |
| `base_revision` | 来稿所基于的修订号（与 PATCH 语义相同） |
| `patch` | RFC 7386 JSON Merge Patch |
| `root_document_ids` | 1～20 个根文档 ID；重复项按首次出现去重 |

服务端先按现有三方合并与契约规则生成**不落库候选**（与 PATCH 相同的
基线检查 / 三方重放 / 契约校验），再对每个根文档做两遍 `$ref` 解析：
一遍用当前内容，一遍用候选覆盖目标文档。候选视为目标文档的**下一个修订**：
`@current` 引用在候选解析中固定到 `candidate_revision` 并读到候选内容，
`@<历史修订>` 的固定引用始终读历史快照，不受候选影响。

```bash
# dep: {"host": "a", "port": 5432}（当前 rev1）
# app: {"db": {"$ref": "doc:dep@current#"}, "debug": false}
curl -X POST http://localhost:8000/documents/dep/impact-preview \
  -H 'Content-Type: application/json' \
  -d '{"base_revision": 1, "patch": {"host": "b"}, "root_document_ids": ["app"]}'
```

```json
{
  "id": "dep",
  "base_revision": 1,
  "current_revision": 1,
  "candidate_revision": 2,
  "replayed": false,
  "changed": true,
  "candidate": {"host": "b", "port": 5432},
  "results": [
    {
      "root_document_id": "app",
      "revision": 1,
      "dependencies": [
        {"document_id": "dep", "revision": 1, "pointer": ""},
        {"document_id": "dep", "revision": 2, "pointer": ""}
      ],
      "resolved_changes": [
        {"op": "replace", "path": "/db/host", "before": "a", "after": "b", "missing": []}
      ]
    }
  ]
}
```

响应字段：

| 字段 | 说明 |
| --- | --- |
| `current_revision` | 快照内固定的目标文档当前修订 |
| `candidate_revision` | 候选若落库将占用的修订号：`changed` 时为 `current_revision + 1`，否则等于 `current_revision`（与 PATCH 的 changed 语义一致） |
| `replayed` / `changed` / `candidate` | 与 PATCH 相同的三方合并标记与最终候选内容（不落库） |
| `results[].revision` | 根文档在快照内固定的修订 |
| `results[].dependencies` | 两遍解析依赖的**并集**：按首次遍历排序（当前解析在前）、按 (文档, 修订, 指针) 去重；候选解析中目标文档的 `@current` 依赖以 `candidate_revision` 给出 |
| `results[].resolved_changes` | 当前解析 → 候选解析的差异，与 `GET /documents/{id}/diff` 同一语义（add/remove/replace、按 RFC 6901 路径排序、`missing` 区分键缺失与真实 `null`） |

错误语义：

- 候选生成失败（`404 not_found`、`422 unknown_base_revision`、
  `409 conflict`、`422 schema_validation_failed`）→ **整体**返回与 PATCH
  相同的结构化错误（预演无 `request_id` 字段），不返回任何根结果；
- 单个根解析失败 → 仅该根的结果为错误项，其他根不受影响：

  ```json
  {"root_document_id": "app", "phase": "candidate",
   "error": "pointer_not_found", "status_code": 422,
   "document_id": "dep", "revision": 2, "pointer": "/host",
   "reason": "指针在目标修订中不存在",
   "chain": [
     {"document_id": "app", "revision": 1, "pointer": "", "ref": null, "at": null},
     {"document_id": "dep", "revision": 2, "pointer": "/host",
      "ref": "doc:dep@current#/host", "at": "/db"}
   ]}
  ```

  `phase` 区分失败发生在哪一遍：`current`（当前内容已无法解析，
  预演基线缺失）或 `candidate`（候选引入的解析失败——正是预演要发现的影响）；
- 预演为**只读**分析：不创建修订、不占用 `request_id`、不改变租约，
  也不检查租约与 guards；同一预演重复执行不会产生任何副作用。

## JSON Schema 契约（可选）

每个文档最多一个契约，遵循 [JSON Schema 2020-12](https://json-schema.org/draft/2020-12)
（根为 Schema 对象或布尔 `true`/`false`）。契约存放在独立的 `contracts` 表中，
随 SQLite 数据卷持久化，容器重启不丢失。

### 设置契约

`PUT /documents/{id}/schema`，请求体固定为 `{"schema": <schema>}`：

```bash
curl -X PUT http://localhost:8000/documents/demo/schema \
  -H 'Content-Type: application/json' \
  -d '{"schema": {
        "type": "object",
        "required": ["a", "b"],
        "properties": {
          "a": {"type": "integer", "minimum": 0},
          "b": {"type": "string"}
        },
        "additionalProperties": false
      }}'
# 200 → {"document_id":"demo","schema":{...},"revision":1,"updated_at":"..."}
```

服务端**严格按顺序**执行两步，均在同一写事务中完成（事务内重读当前文档，
避免与并发 PATCH 的竞态）：

1. **校验 Schema 本身**（2020-12 元 Schema）。非法 → `422 invalid_schema`，
   不写入、不替换：

   ```json
   {"detail": {"error": "invalid_schema", "document_id": "demo",
     "reason": "schema 本身不符合 JSON Schema 2020-12",
     "errors": [{"path": "/type", "validator": "anyOf",
                 "message": "...", "schema_path": "/allOf/3/properties/type/anyOf"}]}}
   ```

2. **验证当前文档实例**。不通过 → `422 schema_validation_failed`，
   旧契约（若有）原样保留，文档内容与修订号不变：

   ```json
   {"detail": {"error": "schema_validation_failed", "document_id": "demo",
     "revision": 1, "reason": "当前文档不满足所给 schema，契约未替换",
     "errors": [{"path": "/a", "validator": "type",
                 "message": "'oops' is not of type 'integer'",
                 "schema_path": "/properties/a/type"}]}}
   ```

> **关于 `$ref`**：支持 Schema 内部引用（`{"$ref": "#/$defs/..."}`）与
> 带 `$id` 的文档内解析；服务运行在离线注册表上、**不发起远程检索**，
> 指向外部 URI（或错误内部指针）的 `$ref` 在设置时即以 `invalid_schema`
> （`validator: "unresolvable_ref"`）拒绝，不会留到 PATCH 时才暴露。

### 查询 / 删除契约

```bash
curl http://localhost:8000/documents/demo/schema           # 200 或 404 schema_not_found
curl -X DELETE http://localhost:8000/documents/demo/schema
# 200 → {"document_id":"demo","deleted": true}；重复删除为幂等 no-op，deleted=false
```

### 契约存在时的 PATCH

PATCH 流程变为：**幂等检查 → （可选）租约门禁 → （可选）guards 前置条件 →
Merge Patch 应用到基线 →（基线过期时）三方重放 → 契约校验最终候选内容 → 写入**。
也就是说，校验发生在合并**之后**，
约束的是即将落库的最终文档，而不是来稿的原始 patch：

```bash
# a 改成字符串且删除必填的 b → 422，一次返回全部错误
curl -i -X PATCH http://localhost:8000/documents/demo \
  -H 'Content-Type: application/json' \
  -d '{"base_revision": 1, "request_id": "req-bad", "patch": {"a": "oops", "b": null}}'
```

```json
HTTP/1.1 422 Unprocessable Entity
{"detail": {"error": "schema_validation_failed", "document_id": "demo",
  "request_id": "req-bad", "base_revision": 1, "current_revision": 1,
  "errors": [
    {"path": "",      "validator": "required", "message": "'b' is a required property",
     "schema_path": "/required"},
    {"path": "/a",    "validator": "type",     "message": "'oops' is not of type 'integer'",
     "schema_path": "/properties/a/type"}
  ]}}
```

错误对象字段：

| 字段 | 说明 |
| --- | --- |
| `path` | 错误所在的实例位置，RFC 6901 JSON Pointer，根为 `""` |
| `validator` | 触发失败的 Schema 关键字（`type`/`required`/`minimum`…） |
| `message` | 人类可读的错误说明 |
| `schema_path` | 失败约束在 Schema 内的 JSON Pointer |

错误顺序稳定：按 `path`、再按 `schema_path`、最后按 `message` 排序，
且一次收集**全部**错误（非快速失败）。

语义与回滚保证：

- 校验失败的请求在事务内回滚：**不产生修订、不占用 `request_id`**，
  调用方修正内容后可用同一个 `request_id` 重试；
- 过期基线请求先做三方重放：路径有分歧时仍返回 `409 conflict`
  （冲突优先于契约校验，因为此时不存在合法的候选内容）；可重放但候选不满足契约时才是 `422`；
- 重放结果与当前版完全相同（`changed=false`）时不产生新修订；
- 同一 `request_id` 的幂等重放直接返回原结果，不再重新校验；
- `DELETE` 契约后，PATCH 立即恢复无契约的原更新行为。

## Merge Patch 语义要点（RFC 7386）

- patch 是**对象**时按键合并：键值为 `null` 表示**删除该键**（不是存成 null）；
- patch 为**非对象**（数组 / 字符串 / 数字 / 布尔 / `null`）时**整体替换**文档；
  数组整体替换，不按下标合并；
- 对非对象的目标打对象补丁时，目标先视为空对象；
- 真实的 JSON `null` 作为键值可以正常存储，仅 patch 中的 `null` 才表示删除。

## 状态码

| 状态码 | 场景 |
| --- | --- |
| `200` | 更新成功（含幂等重放）、契约查询 / 设置 / 删除成功 |
| `201` | 文档创建成功 |
| `404 not_found` | 文档不存在（契约接口同理） |
| `404 schema_not_found` | 文档存在但未设置契约（GET） |
| `404 lease_not_found` | 文档当前没有租约（查询 / 续期） |
| `404 revision_not_found` | 指定修订不存在（单修订查询 / 差异审计 / 字段归因，差异审计以 `missing_revisions` 指出缺失修订） |
| `409 conflict` | 三方合并存在分歧路径（原子发布单项失败时聚合在 `failures` 中） |
| `409 lease_conflict` | 写入被未过期租约拒绝、携带失效 lease_token、或租约获取 / 续期 / 释放凭证不匹配（不写修订、不占用 request_id / release_id） |
| `409 precondition_failed` | guards 前置条件未满足（`failures` 列出 expected/actual 与缺失侧；不写修订、不占用 request_id） |
| `409 revert_conflict` | 撤销目标修订涉及的路径后来被改成不同值（不写新修订、不占用 request_id） |
| `409 request_id_conflict` | 同一 request_id 对应了不同内容（PATCH 与撤销共用幂等表） |
| `409 release_id_conflict` | 同一 release_id 对应了不同的整批请求 |
| `409 document_exists` | 创建时 ID 已存在 |
| `409 reference_cycle` | 引用解析依赖成环（响应含首尾闭合的环链） |
| `422 release_failed` | 原子发布存在失败项（`failures` 按输入顺序汇总，整批回滚、不占用 release_id） |
| `422 unknown_base_revision` | base_revision 不是该文档的现存修订 |
| `422 invalid_revert_revision` | 待撤销 revision 不存在或不大于 1（修订 1 为创建快照） |
| `422 invalid_schema` | 设置契约时 Schema 本身不符合 JSON Schema 2020-12 |
| `422 schema_validation_failed` | 当前文档（设置时）或合并后的最终候选（PATCH 时）不满足契约 |
| `422 invalid_revision` | 引用解析的 `revision` 参数不是正整数修订号或 `current` |
| `422 document_not_found` | 引用解析中（根或被引用的）文档不存在（含完整引用链） |
| `422 revision_not_found` | 引用解析中指定修订不存在（含完整引用链） |
| `422 pointer_not_found` | 引用解析中指针在目标修订中不存在或转义非法（含完整引用链） |
| `422 max_depth_exceeded` | 引用解析深度超过 32（含完整引用链） |

## 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与更新编排（事务、幂等、guards、写入租约、冲突响应、契约接口、差异审计、字段归因、引用解析、变更影响预演）
│   ├── merge.py       # RFC 7386 打补丁 + 递归三方合并（无框架依赖，可单测）
│   ├── guards.py      # 更新前置条件求值（RFC 6901 指针 + 等值/缺失检查，无框架依赖，可单测）
│   ├── diff.py        # 两快照递归差异审计（add/remove/replace，无框架依赖，可单测）
│   ├── blame.py       # 历史字段归因（叶子值最后变更归属，无框架依赖，可单测）
│   ├── resolve.py     # 跨文档 $ref 引用递归解析（环检测、深度上限，无框架依赖，可单测）
│   ├── validation.py  # JSON Schema 2020-12 元校验与实例校验（错误稳定排序）
│   ├── db.py          # SQLite 连接、自动建表、文档/修订/幂等/契约/发布/租约表读写、读写事务
│   ├── schemas.py     # Pydantic 请求模型（额外字段拒绝）
│   └── config.py      # 环境变量配置
├── tests/
│   ├── test_merge.py     # 合并算法单元测试（含逆向补丁差分，34 例）
│   ├── test_api.py       # API 集成测试（23 例，独立临时数据库）
│   ├── test_contracts.py # 契约接口与 PATCH 校验集成测试（19 例）
│   ├── test_reverts.py   # POST /documents/{id}/reverts 撤销集成测试（25 例）
│   ├── test_releases.py  # POST /releases 原子发布集成测试（17 例）
│   ├── test_diff.py      # GET /documents/{id}/diff 差异审计集成测试（17 例）
│   ├── test_blame.py     # GET /documents/{id}/blame 历史字段归因集成测试（24 例）
│   ├── test_resolve.py   # GET /documents/{id}/resolved 引用解析集成测试（33 例）
│   ├── test_impact_preview.py # POST /documents/{id}/impact-preview 变更影响预演集成测试（24 例）
│   ├── test_guards.py    # guards 前置条件与发布项 guards 集成测试（27 例）
│   └── test_leases.py    # 写入租约（获取/续期/释放/查询 + PATCH/发布门禁）集成测试（29 例）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
export CONFIG_DB_PATH=./data/config.db   # 可选，默认 /data/config.db
uvicorn app.main:app --reload --port 8000
```

运行测试：

```bash
python3 -m pytest -q
```
