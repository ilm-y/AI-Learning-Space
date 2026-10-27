# 环境配置与启动

所有命令都在本仓库当前代码上验证过（2026-10-27）。默认路径是**离线可跑**：
Mock LLM + Fixture 检索，不需要 API Key。

## 1. 依赖与端口

| 依赖 | 版本 / 地址 | 是否必需 | 用途 |
| --- | --- | --- | --- |
| JDK | 17+（验证：17.0.12） | 必需 | Spring Boot |
| Maven Wrapper | 仓库自带 `./mvnw` | 必需 | 构建 / 测试 |
| Python | 3.10+（验证：3.10.11） | 必需 | Agent |
| MySQL | 8.x（验证：8.0.37），`127.0.0.1:3306` | 必需 | 22 张业务表 |
| Qdrant | `http://127.0.0.1:6333` | 仅文档 RAG 需要 | 文档切片向量 |
| RabbitMQ | `127.0.0.1:5672`，管理界面 `15672` | 仅 `ails.analysis.mode=mq` 需要 | 分析任务队列 |
| Docker | 可选 | 仅 RabbitMQ / Testcontainers 用 | `docker compose` |

> 仓库的 `docker/docker-compose.yml` **只包含 RabbitMQ**，不包含 MySQL 和 Qdrant。
> 不要假设 `docker compose up -d` 会把数据库和向量库一起拉起来。

## 2. 启动顺序

### 2.1 MySQL

确保本机 MySQL 8 可用。`application.yml` 默认：

```text
host=localhost port=3306 database=ails user=root password=root
```

连接串带 `createDatabaseIfNotExist=true`，库不存在会自动建。本机密码不同时，
写进 `backend/backend/src/main/resources/application-local.yml`（已被
`backend/backend/.gitignore` 排除，**不要提交**）：

```yaml
spring:
  datasource:
    username: root
    password: "本机密码"
```

`application.yml` 通过 `spring.config.import: optional:classpath:application-local.yml`
自动合并；文件不存在时静默跳过，新克隆仓库仍可按默认值启动。

建表：

- 正常启动：`spring.sql.init` 每次执行 `schema.sql`（全部 `CREATE TABLE IF NOT EXISTS`，幂等），
  不需要手工建表。
- 清库重建：手工执行 `schema-dev-reset.sql`（只 DROP）再执行 `schema.sql`。
- 已有库升级（文档 RAG 相关列）：手工执行 `schema-migration-document-rag.sql`。
  `CREATE TABLE IF NOT EXISTS` **不会**给已有表加列。

验证：

```bash
MYSQL_PWD=<密码> mysql -h127.0.0.1 -uroot -N -B -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='ails'"
# 期望 22
```

### 2.2 Qdrant（可选）

只有需要文档检索 / 引用评测时才启动。未启动时：

- 聊天仍可运行；
- 知识检索会进入失败 / 降级状态，`evidence_guard` 只在知识问句上追加资料状态说明；
- 不会回退成「假装检索成功」。

```bash
curl -s http://127.0.0.1:6333/healthz
# healthz check passed
```

### 2.3 RabbitMQ（仅 MQ 模式）

```bash
cd docker
docker compose up -d
docker compose ps
# ails-rabbitmq ... Up (healthy) 127.0.0.1:5672->5672, 127.0.0.1:15672->15672
```

管理界面：`http://127.0.0.1:15672`，默认 `guest / guest`。
端口只绑定 `127.0.0.1`；队列里会有用户原话，不要改成 `0.0.0.0`。

### 2.4 Spring Boot

```bash
cd backend/backend
./mvnw spring-boot:run
# 或
./mvnw -DskipTests clean package
java -jar target/backend-0.0.1-SNAPSHOT.jar
```

默认 `ails.agent.gateway=stub`：后端不调用 Python Agent，也能启动和返回桩回答。
要接真 Agent，在 `application-local.yml` 写：

```yaml
ails:
  agent:
    gateway: http
```

或用环境变量 `AILS_AGENT_GATEWAY=http`。

验证：

```bash
curl -s http://127.0.0.1:8080/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"<用户>","password":"<密码>"}'
```

### 2.5 Python Agent

```bash
cd agent
pip install -r requirements-dev.txt     # 只跑服务时 requirements.txt 即可
cp .env.example .env                    # 可选；不建也能按默认值启动
python -m app.main
```

默认值：`AILS_LLM_PROVIDER=mock`、`AILS_RETRIEVER_MODE=auto`。
`auto` 的含义是「请求带 `authToken` 走 HTTP 回查后端，不带则退回 Fixture」。
这个降级是**静默的**：联调时先看 `/health` 和 `/agent/context/preview`，
不要只看最终回答。

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok","llm_provider":"mock","retriever_mode":"auto",...}
```

### 2.6 MQ Worker（仅 MQ 模式）

```bash
cd agent
AILS_INTERNAL_TOKEN=<与后端一致> python -m app.worker
```

`AILS_INTERNAL_TOKEN` 必须与后端 `ails.internal.token` 一致。为空时 Worker 的检索会被
后端拒绝。没有 RabbitMQ 时 Worker 连接失败并退出，不影响 `app.main`。

## 3. 后端环境变量

以下都可在 `application.yml` 找到默认值；本机差异优先写 `application-local.yml`，
临时覆盖再用环境变量。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MYSQL_HOST` / `MYSQL_PORT` / `MYSQL_DB` | `localhost` / `3306` / `ails` | 数据库 |
| `MYSQL_USER` / `MYSQL_PASSWORD` | `root` / `root` | 本机不同写 local 文件 |
| `JWT_SECRET` | 开发默认值 | 生产必须覆盖 |
| `JWT_EXPIRATION_MS` | `604800000` | 7 天 |
| `AILS_AGENT_GATEWAY` | `stub` | `stub` / `http` |
| `AILS_AGENT_BASE_URL` | `http://127.0.0.1:8000` | Agent 地址 |
| `AILS_ANALYSIS_MODE` | `inline` | `inline` / `mq` |
| `AILS_INTERNAL_TOKEN` | 空 | Worker 内部接口凭据；空表示禁用 |
| `RABBITMQ_HOST` / `RABBITMQ_PORT` | `localhost` / `5672` | 仅 mq 模式 |
| `AILS_DOCUMENT_STORAGE_ROOT` | `./data/documents` | 上传文件落盘目录 |
| `AILS_DOCUMENT_MAX_SIZE_BYTES` | `10485760` | 10 MiB，必须小于 multipart 上限 |

完整 MQ 参数（exchange、队列、TTL、重试次数）见
[`application.yml`](../../backend/backend/src/main/resources/application.yml) 的
`ails.analysis.mq`。

## 4. Agent 环境变量

完整模板见 [`agent/.env.example`](../../agent/.env.example)。关键项：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AILS_HOST` / `AILS_PORT` | `127.0.0.1` / `8000` | HTTP 服务 |
| `AILS_BACKEND_BASE_URL` | `http://localhost:8080` | HTTP 回查后端 |
| `AILS_LLM_PROVIDER` | `mock` | `mock` / `openai` |
| `AILS_LLM_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容地址 |
| `AILS_LLM_MODEL` | `deepseek-chat` | 模型名 |
| `AILS_LLM_API_KEY` | 空 | **只从本地 gitignored `.env` 或环境变量读取** |
| `AILS_RETRIEVER_MODE` | `auto` | `auto` / `http` / `fixture` |
| `AILS_INTERNAL_TOKEN` | 空 | 与后端一致才可走内部文档接口 / Worker |
| `AILS_RABBITMQ_URL` | `amqp://guest:guest@localhost:5672/` | Worker |
| `AILS_EMBEDDING_PROVIDER` | `hash` | `hash` 仅开发；真实语义检索用 `openai` |
| `AILS_EMBEDDING_MODEL` / `_DIMENSION` | `text-embedding-3-small` / `256` | 必须与模型实际维度一致 |
| `AILS_QDRANT_URL` | `http://127.0.0.1:6333` | Qdrant |
| `AILS_QDRANT_COLLECTION_PREFIX` | `ails_documents` | 实际 collection 为 `<prefix>_<model>_<dimension>` |

### 真实模型切换

```bash
AILS_LLM_PROVIDER=openai
AILS_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
AILS_LLM_MODEL=qwen-plus
AILS_LLM_TEMPERATURE=0.0
AILS_LLM_API_KEY=<从本地 gitignored 配置或环境变量注入>
```

`provider=openai` 但缺 Key 时**启动即报错**，不会静默退回 Mock。Embedding 同理：
`AILS_EMBEDDING_PROVIDER=openai` 失败不会回退到 hash。

## 5. 从零启动（离线 Mock，最小链路）

```bash
# 0) MySQL 已启动，库表由后端自动初始化
# 1) Agent（默认 Mock + Fixture）
cd agent && python -m app.main

# 2) 后端（新终端；application-local.yml 写 ails.agent.gateway: http）
cd backend/backend && ./mvnw spring-boot:run

# 3) 自检
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8080/api/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"<用户>","password":"<密码>"}'
```

完整 TLB 演示见 [DEMO.md](DEMO.md)。

## 6. 常见启动失败

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 后端启动即失败，日志提示数据库连接 | MySQL 未启动 / 密码不对 | 检查 `application-local.yml` |
| Agent 启动即失败，提示缺少 Key | `provider=openai` 但 Key 为空 | 补 Key，或改回 `mock` |
| 聊天返回 `503` | 后端 `gateway=http` 但 Agent 未启动 | 启动 `python -m app.main` |
| 知识回答总说没有资料 | Qdrant 未启动 / 文档未索引 / 检索为空 | 查 `/agent/context/preview` 的 `knowledge_status` |
| MQ 模式任务一直 PENDING | Worker 未启动 / 内部 token 不一致 | 启动 Worker，核对 `AILS_INTERNAL_TOKEN` |
| 新列读出来全是 null | 已有库没有执行迁移 | 执行 `schema-migration-document-rag.sql`，或清库重建 |
