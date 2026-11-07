# 本地启动与端到端联调指南

面向 `web/`（Vue 3 + Vite）与 `backend/backend`（Spring Boot 3 + MySQL 8）。
照着做可以从零启动，并完整走通：

```text
注册 → 登录 → 创建学习空间 → 上传文档 → 对话 → 查记忆 → 看学习状态 → 退出
```

---

## 0. 页面与路由

| 页面     | 路由              | 说明                                          |
| -------- | ----------------- | --------------------------------------------- |
| 登录     | `/login`          | 唯一公开页面，未登录访问其他页面会被重定向    |
| 概览     | `/`               | 模块入口                                      |
| 对话     | `/conversations`  | SSE 流式聊天、引用来源                        |
| 文档     | `/documents`      | 上传 `.md` / `.txt`，轮询索引状态             |
| 学习状态 | `/learning-state` | 当前学习目标、Understanding、推荐关注         |
| 记忆     | `/memories`       | 列表 / 混合检索 / 编辑 / 归档                 |
| 学习空间 | `/spaces`         | 占位页（本期未实现管理界面）                  |

> 侧边栏菜单为：概览、学习空间、对话、文档、学习状态、记忆。
> 其中「学习空间」仍是占位页，所以创建空间要用本文第 4 节的 API 命令。

---

## 1. 环境准备

| 依赖       | 要求                                | 检查命令              |
| ---------- | ----------------------------------- | --------------------- |
| JDK        | 17+                                 | `java -version`       |
| MySQL      | 8.x，账号需要建库权限               | `mysql --version`     |
| Node.js    | 20.19+ 或 22.12+（Vite 8 的要求）   | `node -v`             |
| Python     | 3.10+，仅真实 AI / 文档索引需要     | `python --version`    |

端口占用：

- 后端 `8080`
- 前端 `5173`
- Python Agent `8000`（可选）

---

## 2. 启动后端（8080）

### 2.1 默认数据库配置

`application.yml` 的默认值是：

```text
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_DB=ails
MYSQL_USER=root
MYSQL_PASSWORD=root
```

连接串带 `createDatabaseIfNotExist=true`，库不存在会自动创建；`schema.sql` 每次启动
以 `CREATE TABLE IF NOT EXISTS` 幂等执行。

**建议本次演示使用一个全新的库**，避免旧库缺少后续迁移脚本：

PowerShell：

```powershell
cd d:\learningMatiarials\AI-Learning-Space\backend\backend
$env:MYSQL_DB = 'ails_demo'
.\mvnw.cmd spring-boot:run
```

Git Bash：

```bash
cd /d/learningMatiarials/AI-Learning-Space/backend/backend
MYSQL_DB=ails_demo ./mvnw spring-boot:run
```

MySQL 密码不是 `root` 时，不要改 `application.yml`，在本机覆盖文件里写：

`backend/backend/src/main/resources/application-local.yml`

```yaml
spring:
  datasource:
    username: root
    password: "你的密码"
```

该文件已被 `.gitignore` 排除，且由 `application.yml` 的
`spring.config.import: optional:classpath:application-local.yml` 自动合并。

### 2.2 使用旧库时

如果你复用了之前已经建过的库，需要确认已执行过这些迁移（按需）：

```text
src/main/resources/schema-migration-document-rag.sql
src/main/resources/schema-migration-memory-fulltext.sql     ← ngram 全文索引
src/main/resources/schema-migration-phase3-citations.sql
src/main/resources/schema-migration-phase54-message-seq.sql
src/main/resources/schema-migration-phase7-truncation.sql
src/main/resources/schema-migration-rag-reliability.sql
```

其中 `schema-migration-memory-fulltext.sql` 决定「记忆」页的关键词检索能否工作。
用全新库（如 `ails_demo`）则不需要手动执行。

### 2.3 验证后端已启动

```bash
curl -i http://localhost:8080/api/spaces
```

返回 `401` 说明服务已起来（该接口需要登录）；连不上则是启动失败，先看控制台日志。

### 2.4 真实 AI 与文档索引（可选但推荐）

后端默认 `ails.agent.gateway: stub`，对话会返回桩回显；**但文档解析/索引始终走
`ails.agent.base-url` 的 Python Agent**，Agent 没起时文档会进入 `FAILED`。

启动 Agent：

```bash
cd agent
pip install -r requirements.txt
python -m app.main          # 监听 127.0.0.1:8000
curl -s http://127.0.0.1:8000/health
```

让后端把对话也发给 Agent，在 `application-local.yml` 增加：

```yaml
ails:
  agent:
    gateway: http
```

> `agent/.env` 默认 `AILS_LLM_PROVIDER=mock`，回复是确定性的假输出，但足以验证
> 检索链路是否连通。要真实回答，改成 `openai` 并填 `AILS_LLM_BASE_URL` /
> `AILS_LLM_API_KEY`（任何 OpenAI 兼容服务均可）。

---

## 3. 启动前端（5173）

```bash
cd web
npm install
npm run dev
```

打开 <http://localhost:5173>。

前端通过 `web/.env.development` 指定后端地址（当前已是正确值）：

```bash
VITE_API_BASE_URL=http://localhost:8080
```

修改环境变量后需要重启 `npm run dev`。

生产构建自检：

```bash
npm run build
```

---

## 4. 注册账号并创建学习空间（一次性，API 调用）

### 4.1 为什么必须走 API

前端只实现了登录页，没有注册页；同时本期也没有学习空间管理页。
所以第一次使用需要手动调用两个接口。账号和空间只需创建一次，之后都在页面里操作。

### 4.2 PowerShell

```powershell
# 1) 注册（用户名重复会返回 409，此时直接走登录即可）
$reg = Invoke-RestMethod -Method Post `
  -Uri 'http://localhost:8080/api/auth/register' `
  -ContentType 'application/json' `
  -Body (@{
    username = 'demo'
    password = 'demo1234'
    email    = 'demo@example.com'
  } | ConvertTo-Json)

$token = $reg.data.token
$headers = @{ Authorization = "Bearer $token" }

# 2) 创建学习空间（文档、对话都按空间隔离）
$spaceBody = @{
  name        = '我的学习空间'
  description = '本地联调'
  visibility  = 'PRIVATE'
} | ConvertTo-Json

$space = Invoke-RestMethod -Method Post `
  -Uri 'http://localhost:8080/api/spaces' `
  -Headers $headers `
  -ContentType 'application/json; charset=utf-8' `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($spaceBody))

$space.data.id    # 记下这个 id，例如 1
```

### 4.3 Git Bash / curl

```bash
BASE=http://localhost:8080

REG=$(curl -s -X POST "$BASE/api/auth/register" \
  -H 'Content-Type: application/json' \
  -d '{"username":"demo","password":"demo1234","email":"demo@example.com"}')

TOKEN=$(printf '%s' "$REG" | python -c \
  "import sys,json;print(json.load(sys.stdin)['data']['token'])")

curl -s -X POST "$BASE/api/spaces" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"我的学习空间","description":"本地联调","visibility":"PRIVATE"}'
```

约束：`username` 3–50 字符，`password` 6–72 字符，`email` 可空。

---

## 5. 登录与鉴权行为

1. 打开 <http://localhost:5173/login>。
2. 输入上面注册的 `demo / demo1234`，点击「登录」。
3. 成功后 token 写入 `localStorage` 的 `ails_token`，随后跳转 `/`。

验收这些行为：

- 未登录直接访问 `/documents` → 被带到 `/login?redirect=/documents`；
  登录成功后回到 `/documents`。
- 已登录再访问 `/login` → 自动回到 `/`。
- 右上角「退出登录」→ 清除 token 并回到登录页。
- 后端返回 `401`（例如 token 过期）→ 自动回到登录页。

---

## 6. 端到端验收流程

### 6.1 上传文档

1. 进入「文档」。
2. 顶部选择学习空间（第 4 节创建的那个）。
3. 点击上传，选择 `.md` 或 `.txt`，大小 ≤ 10 MB。
4. 观察状态列：

| 状态         | 含义                                   |
| ------------ | -------------------------------------- |
| `UPLOADING`  | 已提交，正在落盘                       |
| `PROCESSING` | Agent 正在解析、切块、建索引           |
| `READY`      | 索引完成，可被对话检索                 |
| `FAILED`     | 失败，鼠标悬停可看 `indexError`        |
| `DELETING`   | 删除中                                 |
| `DELETED`    | 已删除                                 |

页面每 3 秒自动轮询，直到进入终态。

- 重复上传同一内容会返回 `duplicate: true`，页面会提示但不会报错。
- 若一直是 `FAILED`：确认 Python Agent 已启动、`ails.agent.base-url` 可达，
  然后点「重建」重试。
- 超过 10 MB 返回 `413`；扩展名不符会被前端拦截。

### 6.2 对话

1. 进入「对话」。
2. 选择同一个学习空间。
3. 点击「新建对话」，在底部输入框提问并回车（Shift+Enter 换行）。
4. 观察：回答逐字流式出现；结束后 AI 消息下方显示引用来源（`el-tag`）。
5. 生成过程中可以点「停止」中断。

说明：

- 默认 `gateway: stub` 时回答是桩回显，用来验证前后端 SSE 链路；
  改成 `http` 并启动 Agent 后才是真实模型回答。
- 若返回 `503`，通常是 `gateway: http` 但 Agent 没启动，或 Agent 不可达。
- 已归档的对话不能继续发送。

### 6.3 记忆

进入「记忆」：

- 不输关键词 → 调 `GET /api/memories` 浏览列表（可按空间过滤）。
- 输入关键词 → 调 `GET /api/memories/search/hybrid`，按 RRF 分数排序。

首次使用时列表可能为空。可以先用 API 造一条全局记忆：

PowerShell：

```powershell
$memoryBody = @{
  memoryType = 'PREFERENCE'
  content    = '用户喜欢先看具体例子，再看理论定义。'
  importance = 0.8
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri 'http://localhost:8080/api/memories' `
  -Headers $headers `
  -ContentType 'application/json; charset=utf-8' `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($memoryBody))
```

> Windows PowerShell 5.1 默认按 Latin-1 发送字符串请求体，中文会变成乱码。
> 上面用 `UTF8.GetBytes(...)` 显式编码，Git Bash 的 `curl` 示例不受影响。

Git Bash：

```bash
curl -s -X POST "$BASE/api/memories" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"memoryType":"PREFERENCE","content":"用户喜欢先看具体例子，再看理论定义。","importance":0.8}'
```

然后在页面搜索「具体例子」即可命中。演示时建议**把空间筛选留空**：
不带 `spaceId` 时检索该用户的全部 `ACTIVE` 记忆；选中空间且关键词为空时，
列表只显示挂在该空间下的记忆，而上面创建的是 `GLOBAL` 级记忆。

混合检索状态说明：

| 状态              | 含义                                       |
| ----------------- | ------------------------------------------ |
| `OK`              | 关键词 + 向量两路都成功                    |
| `DEGRADED_VECTOR` | 向量路不可用（Agent 未启动等），仅关键词   |
| `DEGRADED_KEYWORD`| 关键词路不可用，仅向量                     |
| `UNAVAILABLE`     | 两路都不可用                               |
| `BLANK_QUERY`     | 关键词为空                                 |

注意：

- 关键词检索用 MySQL ngram 全文索引，**中文关键词建议至少 2 个字**，
  单字查询通常没有结果。
- 记忆状态机为 `CANDIDATE → ACTIVE → ARCHIVED`，**`ARCHIVED` 是终态**。
  页面上的「恢复」按钮因此被禁用并附有提示；这是后端约束，不是前端遗漏。
- 编辑走完整 `PATCH`，会提交 `memoryType` 与 `content`。

### 6.4 学习状态

进入「学习状态」：

- **当前学习目标**：无进行中的 Session 时显示 `el-empty`，属正常情况。
- **Understanding 状态**：新账号为空；产生学习证据后才会出现。
- **推荐关注**：按「误区 > 久未验证 > 到期待复习」排序。
- 点击「查看理解」，若该知识点尚未建立理解，后端返回 `data: null`，
  弹窗显示「该知识点尚未建立理解」的空状态，而不是报错。

想看到非空数据，需要让对话产生学习证据：启动 Agent、把 `gateway` 设为 `http`，
围绕同一个概念进行几轮有实质内容的学习对话，然后刷新页面。
（即使 Agent 不可用，后端也可能退回 `rules-v1` 规则分析，效果取决于对话内容。）

---

## 7. 常见问题

| 现象                                   | 排查                                                         |
| -------------------------------------- | ------------------------------------------------------------ |
| 前端打开空白 / 请求失败                | 确认后端 8080 已启动；确认 `VITE_API_BASE_URL` 正确并重启 dev |
| 登录提示网络错误                       | 后端未启动，或 MySQL 连接失败（看后端日志）                  |
| 登录后立刻被踢回登录页                 | token 过期或后端重启导致 JWT 失效，重新登录                  |
| 文档一直 `FAILED`                      | Python Agent 未启动 / 不可达；修复后点「重建」                |
| 对话返回 `503`                         | `gateway: http` 但 Agent 未启动                              |
| 记忆搜索无结果                         | 关键词少于 2 个中文字符；或库中没有匹配内容                  |
| 记忆自动出现 `DEGRADED_VECTOR`         | Agent 的向量同步/检索不可用，关键词路仍然有效                |
| 上传返回 `413`                         | 文件超过 10 MB                                               |
| 上传前端直接报错                       | 只允许 `.md` / `.txt`                                        |
| `/spaces` 是空白占位页                 | 本期未实现空间管理，创建空间请用第 4 节 API                  |

---

## 8. 最终自检清单

- [ ] `npm run build` 通过，无 TypeScript 报错
- [ ] 后端 `8080` 与前端 `5173` 同时可访问
- [ ] 注册成功（或用户名已存在）
- [ ] 页面登录成功，`localStorage.ails_token` 有值
- [ ] 未登录访问内页会跳转 `/login?redirect=...`
- [ ] 退出登录后回到登录页，内页不可直接访问
- [ ] 文档上传后能进入 `READY`（或能明确看到失败原因）
- [ ] 对话能建立会话并收到 SSE 流式回答与引用
- [ ] 记忆列表 / 混合检索可用，归档后按钮变为禁用状态
- [ ] 学习状态在无数据时显示 `el-empty`，不弹错误
