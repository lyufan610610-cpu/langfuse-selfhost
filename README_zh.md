# Langfuse 单机自托管与知识库问答自动化审核套件 (LLM-as-Judge)

[English](README.md) | [中文](README_zh.md)

一套生产就绪的 **Langfuse v4** 单机自托管可观测性方案，附带专为 Dify 知识库及企业级 RAG 应用设计的零依赖 **LLM-as-Judge 自动化质量审核引擎**。

---

## 整体架构与业务价值

在企业落地大模型知识库（RAG）与智能体应用时，可观测性体系不仅要沉淀调用链路（Tracing），更要具备**无人值守的持续质量评估能力**。本项目提供开箱即用的闭环方案：
1. **轻量化资源优化的 Langfuse v4 底座**：6 个容器组成的 Docker Compose 架构，通过定制 ClickHouse 日志裁剪策略解决磁盘膨胀与内存泄漏，配合网络端口隔离与免邮件组织的自动注册引导；
2. **自动化 RAG 质量审核引擎（`review/kb_review.py`）**：纯 Python 标准库与 `requests` 实现，定期拉取 Dify 问答 trace，提取检索片段与回答，调用本地模型执行四维指标评测，并将得分与判定依据自动回填至 Langfuse 看板。

```
+----------------------------------------------------------------------------------------------------+
|                                      系统整体架构与评估闭环                                        |
|                                                                                                    |
|  +------------------------+                +----------------------------------------------------+  |
|  | Dify 业务应用 (RAG)    | -- Traces ---> | Langfuse v4 自托管可观测底座                       |  |
|  | (Agent / 工作流 / 知识) |                |                                                    |  |
|  +------------------------+                |  - langfuse-web (对外端口 8083)  - langfuse-worker   |  |
|                                            |  - ClickHouse (低内存剪裁配置)   - PostgreSQL 17      |  |
|                                            |  - Redis 7.2 (密码鉴权)          - MinIO (S3 大对象)  |  |
|                                            +----------------------------------------------------+  |
|                                                                      |                             |
|                                          1. 定时拉取知识检索 Trace   v                             |
|  +----------------------------------------------------------------------------------------------+  |
|  |                      LLM-as-Judge 自动化评测套件 (review/)                                   |  |
|  |                                                                                              |  |
|  |  +------------------------+  +--------------------------+  +------------------------------+  |  |
|  |  | 多检索节点合并与去重   |  | 段落贪心填充上下文预算   |  | 多回复节点合并与清洗         |  |  |
|  |  +------------------------+  +--------------------------+  +------------------------------+  |  |
|  |                                           |                                                  |  |
|  |                                           v                                                  |  |
|  |  +----------------------------------------------------------------------------------------+  |  |
|  |  | 结构化判卷矩阵 (知识库准确率 / 知识库忠实度 / 知识库编造 / 问题已解决)                  |  |  |
|  |  +----------------------------------------------------------------------------------------+  |  |
|  |                                           |                                                  |  |
|  |                                           v                                                  |  |
|  |                 本地大模型推理服务 (如 DeepSeek-R1 / Qwen 等推理与判卷模型)                  |  |
|  +----------------------------------------------------------------------------------------------+  |
|                                              |                                                     |
|                                              | 2. 回写评分结果与判卷详细分析                       |
|                                              v                                                     |
|                         Langfuse 质量大盘与准确率走势图 (Dashboard)                                |
+----------------------------------------------------------------------------------------------------+
```

---

## 底座工程特性

### 1. 单机部署与端口隔离
- **`langfuse-web`（映射宿主 8083 -> 3000）**：提供控制台界面与数据上报 API，使用非默认端口避免冲突；
- **`langfuse-worker`（仅容器网络 3030）**：负责异步评估、队列消费与写入 ClickHouse；
- **`postgres:17`**：承载用户、组织、项目、API Key 与评分规则等关系型数据，不对宿主机暴露端口；
- **`clickhouse`（`clickhouse/clickhouse-server:24.3-alpine`）**：高性能存储 Trace 与 Observation 原始时序明细；
- **`redis:7.2-alpine`**：Web 与 Worker 间的异步任务队列，开启密码认证与健康检查；
- **`minio`**：S3 协议兼容的对象存储，存放大字段事件（`events/`）与富媒体多模态资产（`media/`）。

### 2. ClickHouse 低内存与防磁盘膨胀调优（`clickhouse-lowmem.xml`）
在默认的 ClickHouse 配置下，系统内置的指标和查询日志表（如 `trace_log`、`text_log`、`opentelemetry_span_log` 等）会在后台频繁刷写大量小文件，导致磁盘与内存迅速膨胀。
本方案将 `clickhouse-lowmem.xml` 挂载至 `/etc/clickhouse-server/config.d/lowmem.xml`，主动关停上述高开销日志表，保障单机长期稳定运行。

### 3. 无 SMTP 环境的组织与项目自动引导
在企业内网无法连接外部邮件服务器（SMTP）的场景下，通过配置 `LANGFUSE_DEFAULT_ORG_ID` 与 `LANGFUSE_DEFAULT_PROJECT_ID`，新同事注册账号后将自动加入预设的工作空间，免去邮件邀请流程。

---

## 知识库问答自动化审核引擎（`review/kb_review.py`）

评测脚本无需安装复杂评测框架，仅依赖标准 Python 库与 `requests` 即可运行：

### 四维核心评测矩阵

| 评估指标 | 取值范围 | 优化导向 | 业务含义与判定标准 |
|---|---|---|---|
| **知识库准确率** | `0.0 ~ 1.0` | 越高越好 | 回答内容是否符合检索到的知识库片段（陈述是否与事实相符）。 |
| **知识库忠实度** | `0.0 ~ 1.0` | 越高越好 | 回答是否严格基于检索片段生成，衡量是否存在无依据的外部联想与泛化。 |
| **知识库编造** | `0 / 1` (二值) | 越低越好 | 是否存在检索片段之外的凭空编造（`1` 为存在幻觉，`0` 为无幻觉）。 |
| **问题已解决** | `0 / 1` (二值) | 越高越好 | 用户的核心诉求是否得到实质回答。与准确率有本质区别：当知识库确实无相关记录时，助手正确声明“无法回答”，此时准确率为满分，但解决状态计为 `0`。 |

### 生产环境适配能力
- **多知识检索节点聚合**：支持复杂工作流中存在多个知识检索节点的情况，自动合并、去重并按相关度挑选 Top-N 知识片段；
- **段落贪心填充预算**：拼接上下文时按段落边界截取，杜绝从句子中间生硬截断；
- **多回复节点自动收集**：兼容包含分支判断的多回复节点工作流；
- **打分依据透出**：打分结果附带模型分析 Comment 与方向说明，在 Langfuse 详情页一目了然。

---

## 目录结构

```
langfuse-selfhost/
├── docker-compose.yml        # 6 容器生产级编排文件
├── clickhouse-lowmem.xml     # ClickHouse 日志关停配置
├── .env.example              # 基础底座环境变量示例
└── review/
    ├── kb_review.py          # 零依赖 LLM-as-Judge 评测脚本
    ├── review.env.example    # 评测引擎配置示例（判卷模型、检索关键词）
    └── run-review.sh         # 附带路径自解析的 Crontab 入口脚本
```

---

## 快速上手与部署

### 1. 启动 Langfuse 基础服务
```bash
cp .env.example .env
chmod 600 .env

# 编辑 .env，填入安全密钥与实际访问 IP
vi .env

# 启动全量容器
docker compose up -d
docker compose ps
```

在浏览器打开 `http://<服务器IP>:8083`，使用 `.env` 中预设的管理员凭据登录。

> **提示**：`LANGFUSE_INIT_*` 仅在首次空库初始化时生效。

---

### 2. Dify 接入链路追踪
在 Dify 应用的**「监控 / Tracing」**配置中启用 Langfuse：
- **Host**：`http://<服务器IP>:8083`
- **Public Key**：`.env` 中配置的 `LANGFUSE_INIT_PROJECT_PUBLIC_KEY`
- **Secret Key**：`.env` 中配置的 `LANGFUSE_INIT_PROJECT_SECRET_KEY`

配置完成后，所有问答调用的 Trace 将实时回流至自建 Langfuse。

---

### 3. 配置并运行评测引擎

```bash
cd review
cp review.env.example review.env
chmod 600 review.env

# 配置判卷模型服务（支持任何兼容 OpenAI 协议的推理接口，如 vLLM / Ollama）
vi review.env

# 干跑测试（仅控制台输出打分结果，不回写数据库）
python3 kb_review.py --dry-run --force

# 正式评测并回写得分
python3 kb_review.py
```

### 4. 配置定时任务（Crontab）
设置每天早上 07:30 自动执行前一天问答的抽检审核：

```cron
30 7 * * * bash /path/to/langfuse-selfhost/review/run-review.sh
```

日志将自动写入 `review/logs/cron.log`。

---

## 配置参考

### 基础设施配置（`.env`）

| 变量名 | 说明 |
|---|---|
| `NEXTAUTH_URL` | 控制台访问地址与回调地址（例：`http://192.168.1.100:8083`） |
| `NEXTAUTH_SECRET` / `SALT` / `ENCRYPTION_KEY` | 核心加密密钥，**部署后切勿修改**，否则已存凭证无法解密 |
| `DATABASE_URL` | PostgreSQL 数据库连接串 |
| `CLICKHOUSE_PASSWORD` | ClickHouse 鉴权密码 |
| `REDIS_AUTH` | Redis 鉴权密码 |
| `LANGFUSE_DEFAULT_ORG_ID` / `_PROJECT_ID` | 新注册用户默认挂载的组织与项目 ID |
| `TELEMETRY_ENABLED` | 内网离线部署建议设为 `false` |

### 评测引擎配置（`review/review.env`）

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `LANGFUSE_HOST` | `http://127.0.0.1:8083` | Langfuse 服务地址 |
| `JUDGE_BASE_URL` | `http://127.0.0.1:8001/v1` | 判卷大模型的 OpenAI 兼容接口地址 |
| `JUDGE_MODEL` | `deepseek-r1-14b-awq` | 判卷大模型标识 |
| `LOOKBACK_HOURS` | `24` | 每次审核回溯的时间窗口（小时） |
| `MAX_TRACES` | `30` | 单次抽检的最大 Trace 样本数 |
| `REQUIRE_RETRIEVAL` | `true` | 是否仅审核触发了知识库检索的会话 |
| `RETRIEVAL_NAME_HINTS` | `知识检索,retriev,knowledge,检索` | 识别工作流中检索节点的关键词（逗号分隔） |
| `ANSWER_NAME_HINTS` | `直接回复,answer,回复` | 识别工作流中回复节点的关键词（逗号分隔） |

---

## 运维与安全注意事项

- **数据持久化保障**：数据存储于具名数据卷（`langfuse_postgres_data`、`langfuse_clickhouse_data`、`langfuse_minio_data`），建议定期通过数据卷快照或原生导出工具进行灾备。
- **内存规划参考**：
  - PostgreSQL：约 2 GB
  - ClickHouse：约 4~8 GB（关停系统日志表后稳定）
  - Worker 与 Web：约 2~4 GB
  - Redis 与 MinIO：各约 1 GB
- **离线安全**：通过配置 `TELEMETRY_ENABLED=false` 彻底关闭外部遥测上报，满足金融及政企纯内网合规需求。
