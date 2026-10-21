# langfuse-selfhost

Langfuse v4 单机自托管部署，附带一套针对 Dify 知识库问答的 **LLM-as-Judge 定期审核**脚本。

Dify 应用侧开启 tracing、把 Langfuse 的 `pk-lf-...` / `sk-lf-...` 填进去之后，问答 trace 就会落到这里；
`review/kb_review.py` 每天定时拉取最近的 trace，用本地大模型判卷，再把分数写回 Langfuse。

## 目录结构

```
langfuse-selfhost/
├── docker-compose.yml        # 6 个容器：web / worker / postgres / clickhouse / redis / minio
├── clickhouse-lowmem.xml     # 关掉用不上的日志表，省磁盘
├── .env.example              # 环境变量示例（复制为 .env）
└── review/
    ├── kb_review.py          # 审核脚本，只用 requests，无额外依赖
    ├── review.env.example    # 审核配置示例（复制为 review.env）
    └── run-review.sh         # crontab 入口
```

## 架构

| 容器 | 作用 | 宿主端口 |
|---|---|---|
| `langfuse-web` | 网页端 / API | `8083 -> 3000` |
| `langfuse-worker` | 队列消费、异步评估、写 ClickHouse | 仅 `127.0.0.1:3030` |
| `postgres` | 用户 / 项目 / API Key / 评分规则等关系型数据 | 不发布 |
| `clickhouse` | trace / observation 明细 | 仅 `127.0.0.1:8123`、`127.0.0.1:9000` |
| `redis` | web 与 worker 之间的任务队列 | 仅 `127.0.0.1:6379` |
| `minio` | trace 大字段（events/）与图片等媒体（media/） | `9090 -> 9000`、仅 `127.0.0.1:9091 -> 9001` |

相对官方 compose 的主要改动：

1. web 端口由 `3000` 改为 `8083`（避开宿主已占用端口）；
2. postgres 不发布宿主端口（宿主 5432/5433 已被其它数据库占用）；
3. ClickHouse 挂载 `clickhouse-lowmem.xml`，关闭 `trace_log` / `text_log` 等日志表；
4. Redis 开启密码，健康检查带 `-a` 认证；
5. 增加 `LANGFUSE_DEFAULT_ORG_ID` / `LANGFUSE_DEFAULT_PROJECT_ID`，让新注册用户自动落到既有组织与项目（无 SMTP 时替代邀请邮件）。

## 快速开始

```bash
cp .env.example .env
chmod 600 .env
# 编辑 .env：至少改掉所有 change-me，并把 NEXTAUTH_URL 改成实际访问地址

docker compose up -d
docker compose ps
```

浏览器打开 `http://<host>:8083`，用 `.env` 里 `LANGFUSE_INIT_USER_*` 创建的账号登录。

> `LANGFUSE_INIT_*` 只在**空库第一次启动**时生效。库已初始化后再改没有效果，除非 `docker compose down -v` 重建。

## 关键环境变量

| 变量 | 说明 |
|---|---|
| `NEXTAUTH_URL` | 浏览器访问地址 + 登录回调地址 |
| `NEXTAUTH_SECRET` / `SALT` / `ENCRYPTION_KEY` | 核心密钥，**装好之后不要再改**，否则库里已加密的凭据解不开 |
| `DATABASE_URL` | PostgreSQL 连接串，账号密码要和 `POSTGRES_*` 一致 |
| `CLICKHOUSE_PASSWORD` / `REDIS_AUTH` | 中间件密码 |
| `MINIO_ROOT_*` | MinIO 账号，必须与 `LANGFUSE_S3_*_ACCESS_KEY_ID/SECRET_ACCESS_KEY` 一致 |
| `LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT` | 浏览器取图片用的 MinIO 地址，必须是浏览器可达的地址 |
| `LANGFUSE_LLM_CONNECTION_WHITELISTED_IPS` / `_IP_SEGMENTS` | 网页端 LLM 评估器允许连接的内网地址/网段 |
| `LANGFUSE_INIT_ORG_ID` / `_PROJECT_ID` | 首次启动创建的组织 / 项目标识 |
| `LANGFUSE_DEFAULT_ORG_ID` / `_PROJECT_ID` | 新用户自助注册后自动加入的组织 / 项目 |

改完 `.env` 必须 `docker compose down && docker compose up -d`；只 `restart` 不会重新读取 `.env`。

## Dify 侧接入

在 Dify 应用的「监控 / Tracing」里选择 Langfuse，填入：

- Host：`http://<langfuse-host>:8083`
- Public Key / Secret Key：`.env` 中 `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` / `SECRET_KEY` 的值（或网页端项目设置里新建的 Key）

## 问答质量定期审核

`review/kb_review.py` 做的事：

1. 从 Langfuse 拉取最近 `LOOKBACK_HOURS` 小时内、发生过知识检索的 trace；
2. 取「用户问题 + 检索片段 + 助手最终回答」；
3. 交给 OpenAI 兼容接口的判卷模型打分；
4. 把 4 个分数写回对应 trace。

| 分数 | 含义 | 方向 |
|---|---|---|
| `知识库准确率` | 回答是否符合检索片段 | 0~1，越高越好 |
| `知识库忠实度` | 是否只依据片段作答 | 0~1，越高越好 |
| `知识库编造` | 是否存在片段外编造 | 0/1，越低越好 |
| `问题已解决` | 用户问题是否被真正答掉 | 0/1，越高越好 |

准备配置并试跑：

```bash
cd review
cp review.env.example review.env
chmod 600 review.env
# 编辑 review.env：填 Langfuse Key、判卷模型的地址与模型名

python3 kb_review.py --dry-run --force   # 只打印，不回写
python3 kb_review.py                     # 正式回写
```

定时执行（每天 07:30）：

```cron
30 7 * * * bash /path/to/langfuse-selfhost/review/run-review.sh
```

`run-review.sh` 以脚本自身所在目录为工作目录，不依赖固定绝对路径；日志追加到 `review/logs/cron.log`。

节点名与工作流不一致时不用改脚本，直接在 `review.env` 里追加 `RETRIEVAL_NAME_HINTS` / `ANSWER_NAME_HINTS` 关键词。

## 运维提示

- 数据都在命名卷里（`langfuse_postgres_data`、`langfuse_clickhouse_data`、`langfuse_minio_data` 等），备份请直接备份卷或用官方导出；
- 单机内存占用参考：postgres 2G、clickhouse 8G、worker 4G、web 4G、redis 1G、minio 1G（compose 中已设 `mem_limit`，可按机器调整）；
- 内网部署建议关闭遥测：`TELEMETRY_ENABLED=false`。

## 安全说明

本仓库只包含**模板**：`.env.example` / `review.env.example` 里全部为占位符。
真实密钥、账号、内网 IP、组织与项目标识均未入库，`.env`、`review/review.env`、`review/logs/`、`backup/` 已被 `.gitignore` 排除。
