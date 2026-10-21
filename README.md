# Langfuse Self-Host & Automated RAG Evaluation Suite (LLM-as-Judge)

[English](README.md) | [中文](README_zh.md)

A production-tuned, single-node deployment of **Langfuse v4** observability infrastructure, coupled with a zero-dependency **LLM-as-Judge** automated evaluation pipeline designed specifically for Dify Knowledge Base and enterprise RAG applications.

---

## Architecture Overview

Deploying LLM observability in production requires both robust tracing telemetry and automated quality evaluation. This repository provides an end-to-end operational setup:
1. **Resource-Optimized Langfuse v4 Core**: A 6-container Docker Compose topology optimized for enterprise on-premise deployment, incorporating ClickHouse log-pruning to eliminate disk bloat, network isolation, and automatic workspace assignment.
2. **Autonomous RAG Quality Review Engine (`review/kb_review.py`)**: A dependency-free LLM-as-Judge pipeline that periodically pulls Dify conversation traces, extracts contextual retrieval evidence, evaluates answer quality across four distinct RAG metrics, and syncs directional scores directly back to Langfuse.

```
+----------------------------------------------------------------------------------------------------+
|                                    System Architecture & Workflow                                  |
|                                                                                                    |
|  +------------------------+                +----------------------------------------------------+  |
|  | Dify Application (RAG) | -- Traces ---> | Langfuse Self-Hosted Telemetry Stack               |  |
|  | (Agent / Workflow / KB)|                |                                                    |  |
|  +------------------------+                |  - langfuse-web (Port 8083)  - langfuse-worker    |  |
|                                            |  - ClickHouse (Low-Mem)      - PostgreSQL 17      |  |
|                                            |  - Redis 7.2 (Auth)          - MinIO (S3 Storage) |  |
|                                            +----------------------------------------------------+  |
|                                                                      |                             |
|                                          1. Scheduled Trace Query    v                             |
|  +----------------------------------------------------------------------------------------------+  |
|  |                      Automated LLM-as-Judge Evaluation Engine (review/)                      |  |
|  |                                                                                              |  |
|  |  +------------------------+  +--------------------------+  +------------------------------+  |  |
|  |  | Multi-Node Retrieval   |  | Greedy Paragraph-Aware   |  | Multi-Reply Aggregation      |  |  |
|  |  | Merge & Deduplication  |  | Budget Packing           |  | & Sanitization               |  |  |
|  |  +------------------------+  +--------------------------+  +------------------------------+  |  |
|  |                                           |                                                  |  |
|  |                                           v                                                  |  |
|  |  +----------------------------------------------------------------------------------------+  |  |
|  |  | Multi-Dimensional Evaluation Matrix (Accuracy / Faithfulness / Hallucination / Resolved)|  |  |
|  |  +----------------------------------------------------------------------------------------+  |  |
|  |                                           |                                                  |  |
|  |                                           v                                                  |  |
|  |                  Local Judge Model (e.g. DeepSeek-R1 / Qwen Reasoning Models)                |  |
|  +----------------------------------------------------------------------------------------------+  |
|                                              |                                                     |
|                                              | 2. Backfill Evaluation Scores & Rationales          |
|                                              v                                                     |
|                        Langfuse Tracing Analytics & Continuous Dashboards                          |
+----------------------------------------------------------------------------------------------------+
```

---

## Infrastructure Highlights

### 1. Tailored Docker Compose Topology
- **`langfuse-web` (Port 8083 -> 3000)**: Serves the Web UI and ingestion API, mapped to a dedicated port to avoid collisions with existing host services.
- **`langfuse-worker` (Internal 3030)**: Handles asynchronous queue processing, batch exports, and ClickHouse writes.
- **`postgres:17`**: Stores relational data (users, organizations, projects, API keys, evaluation criteria). Configured without exposed host ports to isolate relational data.
- **`clickhouse` (`clickhouse/clickhouse-server:24.3-alpine`)**: High-throughput storage for traces and observation records.
- **`redis:7.2-alpine`**: Task queue layer protected with authentication and health-check verification.
- **`minio`**: S3-compatible blob storage handling large trace events (`events/`) and multimodal media assets (`media/`).

### 2. ClickHouse Low-Memory Tuning (`clickhouse-lowmem.xml`)
In default ClickHouse deployments, internal metric and query log tables (`trace_log`, `text_log`, `metric_log`, etc.) generate hundreds of thousands of daily log entries, consuming disk I/O and causing memory creep. 
Our configuration mounts `clickhouse-lowmem.xml` into `/etc/clickhouse-server/config.d/lowmem.xml`, actively disabling these diagnostic system log tables and maintaining long-term stability on single-node instances.

### 3. SMTP-Free Automatic Workspace Bootstrapping
Self-hosted enterprise servers often lack an active outbound SMTP gateway. By configuring `LANGFUSE_DEFAULT_ORG_ID` and `LANGFUSE_DEFAULT_PROJECT_ID`, newly registered team members are automatically provisioned into the designated workspace upon account creation without requiring email invitation tokens.

---

## Automated RAG Evaluation Engine (`review/kb_review.py`)

The companion evaluation pipeline is a zero-dependency Python script (requiring only `requests`) that operates as an autonomous **LLM-as-Judge** auditor:

### The 4-Dimensional Metric Evaluation Matrix

| Metric Name | Value Range | Optimization Direction | Semantic Definition |
|---|---|---|---|
| **Knowledge Accuracy** | `0.0 ~ 1.0` | Higher is better | Measures whether the assistant's claims factually align with the retrieved knowledge chunks. |
| **Knowledge Faithfulness** | `0.0 ~ 1.0` | Higher is better | Measures groundedness: evaluates whether the answer is strictly derived from retrieved evidence without ungrounded extrapolation. |
| **Knowledge Hallucination** | `0 / 1` (Binary) | Lower is better | Flags external factual fabrications not supported by the retrieved context (`1` = Hallucination detected, `0` = Clean). |
| **Task Resolution** | `0 / 1` (Binary) | Higher is better | Evaluates whether the user's intent was resolved. Crucially distinguishes between helpful answers and correct refusals when context is empty (`resolved=0` on context-absent refusals, even when `accuracy=1.0`). |

### Production Workflow Adaptations
- **Multi-Node Retrieval Consolidation**: Aggregates, deduplicates, and ranks retrieved snippets across multiple retrieval nodes in complex Dify workflows.
- **Greedy Paragraph-Aware Packing**: Assembles context snippets up to character budgets (`MAX_CONTEXT_CHARS`) on clean paragraph boundaries, preventing mid-sentence truncation.
- **Multi-Reply Node Aggregation**: Gathers and concatenates outputs from branched synthesis nodes.
- **Non-Invasive Directional Comments**: Attaches detailed scoring rationales and metric definitions directly to Langfuse score objects.

---

## Directory Structure

```
langfuse-selfhost/
├── docker-compose.yml        # 6-container production deployment topology
├── clickhouse-lowmem.xml     # ClickHouse log-suppression configuration
├── .env.example              # Core infrastructure environment variables template
└── review/
    ├── kb_review.py          # Zero-dependency LLM-as-Judge evaluation engine
    ├── review.env.example    # Evaluator configuration template (Judge model, endpoints)
    └── run-review.sh         # Crontab wrapper script with self-resolving directory paths
```

---

## Quickstart & Deployment

### 1. Deploy Langfuse Infrastructure
```bash
cp .env.example .env
chmod 600 .env

# Edit .env with your secrets and host endpoints
vi .env

# Start all services
docker compose up -d
docker compose ps
```

Access the Web Console at `http://<your-host>:8083` and sign in using the administrative credentials initialized in `.env`.

> **Note**: `LANGFUSE_INIT_*` credentials are processed only during the initial bootstrap of an empty database.

---

### 2. Connect Dify Tracing
In your Dify application console:
1. Navigate to **Monitoring / Tracing**.
2. Select **Langfuse** as the provider.
3. Configure:
   - **Host**: `http://<langfuse-host>:8083`
   - **Public Key**: `<LANGFUSE_INIT_PROJECT_PUBLIC_KEY>`
   - **Secret Key**: `<LANGFUSE_INIT_PROJECT_SECRET_KEY>`

Your Dify conversation traces will now stream directly into the self-hosted Langfuse stack.

---

### 3. Configure & Run the Evaluation Engine

```bash
cd review
cp review.env.example review.env
chmod 600 review.env

# Configure your Judge model API (OpenAI-compatible endpoint, e.g. local vLLM / Ollama)
vi review.env

# Dry-run test (prints evaluation scores to stdout without writing back)
python3 kb_review.py --dry-run --force

# Live execution (commits evaluation scores back to Langfuse traces)
python3 kb_review.py
```

### 4. Schedule Unattended Daily Audits
Set up a crontab entry (e.g. daily audit at 07:30 AM):

```cron
30 7 * * * bash /path/to/langfuse-selfhost/review/run-review.sh
```

Logs are automatically appended to `review/logs/cron.log`.

---

## Configuration Reference

### Infrastructure Environment (`.env`)

| Variable | Description |
|---|---|
| `NEXTAUTH_URL` | Web console access URL and OAuth callback base (e.g. `http://192.168.1.100:8083`). |
| `NEXTAUTH_SECRET` / `SALT` / `ENCRYPTION_KEY` | Core encryption keys. **Do not modify after deployment** to prevent key mismatch on stored secrets. |
| `DATABASE_URL` | PostgreSQL connection string matching `POSTGRES_*` settings. |
| `CLICKHOUSE_PASSWORD` | ClickHouse authentication password. |
| `REDIS_AUTH` | Redis authentication password. |
| `LANGFUSE_DEFAULT_ORG_ID` / `_PROJECT_ID` | Default workspace for new user auto-onboarding without SMTP invitations. |
| `TELEMETRY_ENABLED` | Set to `false` for air-gapped on-premise environments. |

### Evaluator Environment (`review/review.env`)

| Variable | Default Value | Description |
|---|---|---|
| `LANGFUSE_HOST` | `http://127.0.0.1:8083` | Langfuse API address. |
| `JUDGE_BASE_URL` | `http://127.0.0.1:8001/v1` | OpenAI-compatible endpoint for the Judge model (vLLM, Ollama, SGLang). |
| `JUDGE_MODEL` | `deepseek-r1-14b-awq` | Judge model identifier. |
| `LOOKBACK_HOURS` | `24` | Time window for querying recent conversation traces. |
| `MAX_TRACES` | `30` | Maximum number of traces to audit per evaluation run. |
| `REQUIRE_RETRIEVAL` | `true` | Filter to audit only traces that executed knowledge retrieval steps. |
| `SCORE_NAME_ACCURACY` | `""` | Custom score identifier (defaults to localized or standard name). |
| `RETRIEVAL_NAME_HINTS` | `知识检索,retriev,knowledge,检索` | Comma-separated workflow node name substrings used to detect retrieval steps. |
| `ANSWER_NAME_HINTS` | `直接回复,answer,回复` | Comma-separated workflow node name substrings used to detect answer steps. |

---

## Production Operations & Maintenance

- **Persistent Volumes**: All critical data resides in Docker named volumes (`langfuse_postgres_data`, `langfuse_clickhouse_data`, `langfuse_minio_data`). For backups, snapshot these volumes directly or use standard database dump utilities.
- **Memory Allocation**: Typical resource usage:
  - Postgres: ~2 GB
  - ClickHouse: ~4–8 GB (with log suppression active)
  - Worker & Web: ~2–4 GB each
  - Redis & MinIO: ~1 GB each
- **Air-Gapped Isolation**: Set `TELEMETRY_ENABLED=false` to ensure zero external outbound calls.
