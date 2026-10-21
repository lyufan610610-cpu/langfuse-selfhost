#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知识库问答准确性定期审核 (LLM-as-Judge)

做什么：
  1. 从 Langfuse 拉取被监控 Dify 应用最近的问答 trace（默认只看发生过知识检索的样本）
  2. 把「用户问题 + 检索到的知识库原文片段 + 助手最终回答」交给本地大模型判卷
  3. 把分数写回 Langfuse（名字可配置，默认：知识库准确率 / 知识库忠实度 / 知识库编造 / 问题已解决）
  4. Langfuse 里即可看到准确率与趋势

只依赖 requests，不需要联网安装任何包。

v2（2026-09-20）改动要点：
  * 分数名改为可配置（支持中文）              -> review.env 的 SCORE_NAME_*
  * 多个知识检索节点：自动合并 + 去重 + 按相关度排序 + 只取前 N 段
  * 多个回复节点：所有回答都会收集（不再只取第一个）
  * 节点名识别可配置（RETRIEVAL_NAME_HINTS / ANSWER_NAME_HINTS），工作流改名不用动脚本
  * 上下文改为「按段贪心填充」，不再从中间截断一段话

v3（2026-09-21）改动要点：
  * 判卷提示词补齐三个指标的独立定义与方向：
    accuracy/faithfulness 越高越好（0~1），hallucination 越低越好（true=有编造）
  * faithfulness 不再「模型没给就静默复制 accuracy」，缺字段会打警告
  * 分数 comment 里显式标注方向，Langfuse 页面可直接看懂

v4（2026-09-23）改动要点：
  * 新增第四个指标 resolved（问题已解决）：1=已解决 / 0=未解决，越高越好
    与 accuracy 的本质区别：accuracy 看「回答符不符合片段」，
    resolved 看「用户的问题有没有被真正答掉」（片段本身没答案导致拒答 -> resolved=0）
  * 字段缺失或非法时不再瞎猜，跳过该条分数并打警告
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ENV = os.path.join(HERE, "review.env")

# ---------------------------------------------------------------- 配置

DEFAULTS = {
    "LANGFUSE_HOST": "http://127.0.0.1:8083",
    "LANGFUSE_PUBLIC_KEY": "",
    "LANGFUSE_SECRET_KEY": "",
    "JUDGE_BASE_URL": "http://127.0.0.1:8001/v1",
    "JUDGE_API_KEY": "not-needed",
    "JUDGE_MODEL": "deepseek-r1-14b-awq",
    "JUDGE_TIMEOUT": "600",
    "JUDGE_MAX_TOKENS": "3072",
    "LOOKBACK_HOURS": "24",
    "MAX_TRACES": "30",
    "REQUIRE_RETRIEVAL": "true",
    "TRACE_NAME_FILTER": "",
    "EXCLUDE_USERS": "lf-deploy-check",

    # 分数名（写回 Langfuse 的名字）。留空则退回 SCORE_PREFIX + 英文后缀。
    "SCORE_PREFIX": "kb",
    "SCORE_NAME_ACCURACY": "",
    "SCORE_NAME_FAITHFULNESS": "",
    "SCORE_NAME_HALLUCINATION": "",
    "SCORE_NAME_RESOLVED": "",

    # 节点名识别（逗号分隔，大小写不敏感）。工作流里改了节点名，在这里追加即可。
    "RETRIEVAL_NAME_HINTS": "知识检索,retriev,knowledge,检索",
    "ANSWER_NAME_HINTS": "直接回复,answer,回复",

    "ONLY_UNSCORED": "true",
    "MIN_CONTEXT_CHARS": "50",
    "MAX_CONTEXT_CHARS": "6000",
    "MAX_CONTEXT_CHUNKS": "8",
    "MAX_ANSWER_CHARS": "4000",
}

DEFAULT_RETRIEVAL_HINTS = [x for x in DEFAULTS["RETRIEVAL_NAME_HINTS"].split(",") if x]
DEFAULT_ANSWER_HINTS = [x for x in DEFAULTS["ANSWER_NAME_HINTS"].split(",") if x]


def load_env(path):
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if v[:1] in ('"', "'"):
                    q = v[0]
                    end = v.find(q, 1)
                    v = v[1:end] if end > 0 else v.strip(q)
                else:
                    # 去掉行内注释（如 "=50   # 说明"）
                    v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
                cfg[k.strip()] = v
    # 允许环境变量覆盖
    for k in list(cfg.keys()):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


def split_hints(s, fallback):
    got = [x.strip() for x in str(s or "").split(",") if x.strip()]
    return got or list(fallback)


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def to_local_str(ts):
    """把 trace 的 UTC 时间戳转成本地（服务器时区）'MM-DD HH:MM'，失败返回空串。"""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%m-%d %H:%M")
    except Exception:
        return ""


# ---------------------------------------------------------------- Langfuse

class Langfuse:
    def __init__(self, host, pk, sk):
        self.host = host.rstrip("/")
        self.s = requests.Session()
        tok = base64.b64encode(f"{pk}:{sk}".encode()).decode()
        self.s.headers.update({"Authorization": f"Basic {tok}"})

    def list_traces(self, from_ts, limit=50, page=1, name=None):
        params = {"page": page, "limit": limit, "fromTimestamp": from_ts}
        if name:
            params["name"] = name
        r = self.s.get(f"{self.host}/api/public/traces", params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def get_trace(self, trace_id):
        r = self.s.get(f"{self.host}/api/public/traces/{trace_id}", timeout=60)
        r.raise_for_status()
        return r.json()

    def scores_of_trace(self, trace_id):
        """取某条 trace 的分数。

        注意：本版 Langfuse 的 GET /api/public/scores 会忽略 traceId 过滤条件
        （实测传入不存在的 traceId 仍返回全部分数），因此改从 trace 详情读取。
        """
        try:
            d = self.get_trace(trace_id)
        except Exception:
            return []
        return d.get("scores") or []

    def create_score(self, trace_id, name, value, data_type="NUMERIC",
                     comment=None, metadata=None, timestamp=None):
        """写一条分数，并把「分数时间」钉在提问时间上。

        为什么要绕：
          POST /api/public/scores 不接受自定义 timestamp（OpenAPI 里 ScoreBody /
          CreateScoreRequest 都没有这个字段，实测传了也被静默忽略），
          分数时间恒为写入时刻 —— 这就是 scores 页面整列都是 07:30 的原因。

          唯一可行路径是走 ingestion 的 score-create 事件：事件 envelope 上的
          timestamp 会被用作分数的 timestamp。实测：传 trace 自己的 timestamp，
          读回就是提问时间（2026-09-20T10:03:48.774Z），而 createdAt 才是写入时刻。

        幂等：分数 id 由 trace_id + 分数名派生（uuid5），重跑覆盖同一条，
              不像直写那样每跑一次就多一条。
        """
        body = {"traceId": trace_id, "name": name, "value": value, "dataType": data_type}
        if comment:
            body["comment"] = comment[:900]
        if metadata:
            body["metadata"] = metadata
        body["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.host}|{trace_id}|{name}"))

        ev = {
            "id": str(uuid.uuid4()),
            "type": "score-create",
            "timestamp": timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "body": body,
        }
        r = self.s.post(f"{self.host}/api/public/ingestion", json={"batch": [ev]}, timeout=60)
        if r.status_code >= 300:
            raise RuntimeError(f"score {name} failed {r.status_code}: {r.text[:200]}")
        try:
            res = r.json()
        except Exception:
            res = {}
        # ingestion 是「部分成功」语义：errors 非空说明这条没进去
        if res.get("errors"):
            raise RuntimeError(f"score {name} rejected: {str(res['errors'])[:200]}")
        return res


# ---------------------------------------------------------------- 内容抽取

def _maybe_json(x):
    if isinstance(x, (dict, list)):
        return x
    if isinstance(x, str):
        try:
            return json.loads(x)
        except Exception:
            return x
    return x


def hint_match(name, hints):
    """节点名是否命中关键词（大小写不敏感）。"""
    n = str(name or "")
    low = n.lower()
    for h in hints:
        if h in n or h.lower() in low:
            return True
    return False


def extract_question(trace, observations=None):
    inp = _maybe_json(trace.get("input"))
    if isinstance(inp, dict):
        for k in ("message", "query", "question", "input"):
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    if isinstance(inp, str) and inp.strip():
        return inp.strip()

    # Dify 工作流/对话流：用户问题在 workflow 或「知识检索」节点的 input 里
    for ob in observations or []:
        n = str(ob.get("name") or "")
        if n == "workflow" or hint_match(n, DEFAULT_RETRIEVAL_HINTS):
            d = _maybe_json(ob.get("input"))
            if isinstance(d, dict):
                for k in ("sys.query", "query", "question"):
                    v = d.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()

    # 兜底：送给大模型的消息里最后一条 user
    for m in extract_llm_messages(observations):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                return c.strip()
    return ""


def collect_answers(trace, observations=None, hints=None):
    """收集所有可能的「最终回答」，返回 [(来源, 文本), ...]，已去重。

    多个回复节点时，每个节点的回答都会被收进来（旧版只取第一个）。
    """
    hints = hints or DEFAULT_ANSWER_HINTS
    cand = []

    out = _maybe_json(trace.get("output"))
    if isinstance(out, list):
        for item in out:
            if isinstance(item, dict):
                c = item.get("content")
                if isinstance(c, str) and c.strip():
                    cand.append(("trace.output", c.strip()))
            elif isinstance(item, str) and item.strip():
                cand.append(("trace.output", item.strip()))
    elif isinstance(out, dict):
        preferred = [out[k] for k in ("answer", "text", "content", "output")
                     if isinstance(out.get(k), str) and out[k].strip()]
        if preferred:
            for v in preferred:
                cand.append(("trace.output", v.strip()))
        else:
            for k, v in out.items():
                if isinstance(v, str) and v.strip():
                    cand.append((f"trace.output.{k}", v.strip()))
                elif isinstance(v, dict):
                    for kk in ("answer", "text", "content", "output"):
                        vv = v.get(kk)
                        if isinstance(vv, str) and vv.strip():
                            cand.append((f"trace.output.{k}.{kk}", vv.strip()))
    elif isinstance(out, str) and out.strip():
        cand.append(("trace.output", out.strip()))

    # 节点级兜底：workflow / 各「直接回复」节点
    for ob in observations or []:
        n = str(ob.get("name") or "")
        if not (n == "workflow" or hint_match(n, hints)):
            continue
        d = _maybe_json(ob.get("output"))
        if isinstance(d, dict):
            got = False
            for k in ("answer", "text", "content", "output"):
                v = d.get(k)
                if isinstance(v, str) and v.strip():
                    cand.append((n, v.strip()))
                    got = True
            if not got:
                for k, v in d.items():
                    if isinstance(v, str) and v.strip():
                        cand.append((f"{n}.{k}", v.strip()))
        elif isinstance(d, str) and d.strip():
            cand.append((n, d.strip()))

    # 去重：同一条答案可能既在 trace.output 又在节点输出里
    seen, uniq = set(), []
    for src, txt in cand:
        key = txt.strip()[:200]
        if key in seen:
            continue
        seen.add(key)
        uniq.append((src, txt))
    return uniq


def extract_answer(trace, observations=None, hints=None):
    """兼容入口：把所有回答拼成一段文本。"""
    return "\n\n".join(t for _, t in collect_answers(trace, observations, hints))


def extract_retrieval(observations, hints=None, max_chunks=8):
    """从所有「知识检索」span 取片段：合并 -> 去重 -> 按相关度排序 -> 取前 N 段。

    多个知识检索节点会被自动合并；同一片段被多个节点命中只算一次。
    """
    hints = hints or DEFAULT_RETRIEVAL_HINTS
    raw = []
    for ob in observations or []:
        if not hint_match(ob.get("name"), hints):
            continue
        out = _maybe_json(ob.get("output"))
        if not isinstance(out, dict):
            continue
        for item in out.get("result") or []:
            if not isinstance(item, dict):
                continue
            content = item.get("content") or ""
            if not (isinstance(content, str) and content.strip()):
                continue
            meta = item.get("metadata") or {}
            doc = item.get("title") or meta.get("document_name") or "未知文档"
            score = meta.get("score")
            raw.append({
                "doc": doc,
                "score": score if isinstance(score, (int, float)) else None,
                "content": content.strip(),
            })

    if raw:
        n_raw = len(raw)
        seen, uniq = set(), []
        for c in raw:
            key = c["content"][:300]
            if key in seen:
                continue
            seen.add(key)
            uniq.append(c)
        # 相关度高的排前面；没有分数的排最后（保持原有顺序）
        uniq.sort(key=lambda c: -(c["score"] if c["score"] is not None else -1))
        if max_chunks and max_chunks > 0:
            uniq = uniq[:max_chunks]
        out_chunks = []
        for c in uniq:
            head = f"《{c['doc']}》" + (f"（相关度 {c['score']:.3f}）" if c["score"] is not None else "")
            out_chunks.append(f"{head}\n{c['content']}")
        return out_chunks, n_raw

    # 兜底：代码/模板节点拼好的上下文
    for ob in observations or []:
        if "代码执行" not in str(ob.get("name") or ""):
            continue
        out = _maybe_json(ob.get("output"))
        if isinstance(out, dict):
            fc = out.get("formatted_context")
            if isinstance(fc, str) and fc.strip():
                return [fc.strip()], 1
    return [], 0


def extract_llm_messages(observations):
    """取 GENERATION 观察里真正送给模型的 messages。"""
    for ob in observations or []:
        if ob.get("type") != "GENERATION":
            continue
        inp = _maybe_json(ob.get("input"))
        if isinstance(inp, list):
            return inp
    return []


# ---------------------------------------------------------------- 判卷模型

JUDGE_SYSTEM = """你是一名严格的知识库问答质量审核员。你的职责是对【助手回答】给出四个互相独立的分项结论：知识库准确率、知识库忠实度、知识库编造、问题已解决。四者衡量的是不同问题，必须分别独立判断，不允许互相套用同一个值。

四个指标的取值与方向（务必记清方向）：
- accuracy（知识库准确率）：0.0~1.0 的分数，**越高越好**。
- faithfulness（知识库忠实度）：0.0~1.0 的分数，**越高越好**。
- hallucination（知识库编造）：布尔值 true / false，**越低越好**；true = 存在编造，false = 未发现编造。
- resolved（问题已解决）：取值 1 或 0，**越高越好**；1 = 用户的问题已解决，0 = 未解决。

────────────────────
一、accuracy（知识库准确率，越高越好）：回答「答得对不对、全不全」
评分标准（只针对知识准确性，不考虑文风、长度、礼貌）：
- 1.0：回答的所有关键事实都能在片段中找到直接依据；数值/单位/型号/标准号与原文完全一致；没有编造。
- 0.8：主体正确，存在个别表述不够精确或轻微遗漏，但无编造。
- 0.6：部分结论缺少片段依据，或存在明显遗漏；但未出现与原文冲突的内容。
- 0.4：有 1 处明显编造（片段中不存在的事实/数值），或关键数值与原文不一致。
- 0.2：多处编造，或回答与片段内容冲突。
- 0.0：完全没有依据/答非所问/拒答但片段中本有答案。

二、faithfulness（知识库忠实度，越高越好）：回答「有没有超出片段、有没有歪曲片段」
只考察回答与片段的一致性，不考虑片段本身是否足以回答该问题（这点与 accuracy 不同）。
- 1.0：回答中所有事实性表述都能在片段中找到直接依据，没有任何超出片段的内容。即使片段完全没有答案、助手如实说明「知识库中没有相关内容」，忠实度同样是 1.0。
- 0.8：绝大部分有依据，仅有少量不涉及具体事实的连接性/常识性表述。
- 0.6：有若干片段未覆盖的表述，但属于泛化解释，未引入新的具体事实（数值/型号/标准号/结论）。
- 0.4：在片段之外引入了具体事实（数值/型号/标准号/结论）。
- 0.2：多处引入片段中不存在的事实，或对片段做了明显外推。
- 0.0：回答与片段相冲突，或完全脱离片段自行发挥。

三、hallucination（知识库编造，越低越好；true=有编造，false=没编造）
判为 true（满足任一）：
- 回答里出现了片段中找不到依据的具体事实性内容：具体数值、单位、型号、标准号、参数、条款、结论性断言；
- 回答与片段内容直接冲突；
- 片段为空或与问题明显无关，助手仍给出具体事实性结论。
判为 false（满足任一）：
- 回答中所有具体事实都能在片段里找到依据；
- 助手只做了不带具体事实的泛化表述；
- 助手明确说明「知识库中没有相关内容」或拒答，且未编造任何具体内容。

────────────────────
四、resolved（问题已解决，越高越好）：站在用户视角回答「他这次的问题被答掉了吗」
这一项只看【用户问题】与【助手回答】，与片段是否完整无关（这点与 accuracy 不同）。
判为 1（已解决）需同时满足：
- 回答正面回应了用户的真实问题意图，不是答非所问、不是只做寒暄或流程性说明；
- 关键信息完整：用户读完这条回答就能解决问题，不需要就同一件事再追问一次；
- 结论明确且正确，不含自相矛盾，也没有把「无法确定」当成答案交付。
说明：即使回答里的部分内容是片段之外的常识补充，只要结论正确且完整地解答了用户问题，仍算已解决（此时准确率/忠实度可能偏低，属于正常差异）。
判为 0（未解决），满足任一即可：
- 答非所问、跑题，或只是重复用户的问题；
- 拒答，或回答「知识库中没有相关内容」「请咨询厂商/请联系客服」等把问题推回给用户；
- 只回答了问题的一部分，关键结论缺失，用户还需要追问；
- 结论错误、与片段冲突，会把用户带偏；
- 回答为空、被截断、是报错文本，或只有退场/流程话术。

────────────────────
四者可以为不同数值，示例（仅用于校准，不要照搬）：
- 片段与问题无关，助手如实回答「知识库中没有相关内容」：accuracy=1.0，faithfulness=1.0，hallucination=false，resolved=0。
- 片段里有答案，助手答对主体但自行补了一个片段中没有的数值：accuracy≈0.4，faithfulness≈0.4，hallucination=true，resolved=1。
- 片段里有答案，助手基本逐句复述：accuracy=1.0，faithfulness=1.0，hallucination=false，resolved=1。
- 片段里有答案，助手答成另一型号且补充了错误参数：accuracy≈0.0，faithfulness≈0.0，hallucination=true，resolved=0。
- 片段里有部分答案，助手漏答一半但引用准确：accuracy≈0.6，faithfulness=1.0，hallucination=false，resolved=0。
- 片段里有答案，助手答非所问但抄得一字不差：accuracy=0.0，faithfulness=0.0，hallucination=false，resolved=0。

特别判定：
- 如果【知识库片段】为空或明显与问题无关，而助手仍给出具体事实性结论 → hallucination=true，且 accuracy 不得高于 0.2。
- 如果助手明确表示「知识库中没有相关内容」且片段确实无相关内容 → accuracy=1.0，faithfulness=1.0，hallucination=false，但 resolved=0（用户并没有拿到答案）。
- 【知识库片段】可能来自多个知识检索节点，各段之间用 --- 分隔；回答中只要任一段能支撑，即算有依据。

只输出一个 JSON 对象，不要输出任何其他文字、不要用 markdown 代码块。accuracy、faithfulness、hallucination、resolved 四个字段都必须给出，缺一不可。格式：
{"accuracy": 0.0, "faithfulness": 0.0, "hallucination": false, "resolved": 0, "reason": "不超过120字的中文说明：先指出方向性问题（问题是否被解决/是否答非所问/是否编造/是否遗漏），再给依据"}
"""


class Judge:
    def __init__(self, base_url, model, api_key, timeout=600, max_tokens=3072):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.key = api_key
        self.timeout = timeout
        self.max_tokens = int(max_tokens or 3072)

    def ask(self, question, context, answer):
        user = (
            "【用户问题】\n" + (question or "(空)") +
            "\n\n【知识库片段】\n" + (context or "(无检索结果)") +
            "\n\n【助手回答】\n" + (answer or "(空)")
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        r = requests.post(self.url, json=payload, timeout=self.timeout,
                          headers={"Authorization": f"Bearer {self.key}",
                                   "Content-Type": "application/json"})
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"].get("content") or ""


def to_flag(v):
    """把模型给的各种写法统一成 1 / 0；无法识别时返回 None（调用方据此跳过该分数）。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return 1 if float(v) >= 0.5 else 0
    s = str(v).strip().lower().strip("。.！!~ \t\r\n\"'“”")
    TRUE_WORDS = ("1", "true", "yes", "y", "是", "已解决", "解决", "已解答", "已答",
                  "存在编造", "有编造", "编造", "命中")
    FALSE_WORDS = ("0", "false", "no", "n", "否", "未解决", "没解决", "未解答", "未答",
                   "答非所问", "未发现编造", "无编造", "没有编造", "不存在编造", "未命中")
    if s in TRUE_WORDS:
        return 1
    if s in FALSE_WORDS:
        return 0
    # 兜底 1：中文否定前缀（未解决 / 没解决 / 不存在编造 / 未发现编造）
    if s[:1] in ("未", "没", "不"):
        return 0
    # 兜底 2：形如 "1（已解决）"、"0 - 未解决"
    if s[:1] == "1":
        return 1
    if s[:1] == "0":
        return 0
    return None


def parse_judgement(text):
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    obj = None
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
        except Exception:
            obj = None
    if not isinstance(obj, dict):
        # 退化路径：思考过程里也写了花括号时，逐个候选片段重试
        for cand in reversed(re.findall(r"\{[^{}]*\}", t, re.S)):
            try:
                obj = json.loads(cand)
                break
            except Exception:
                obj = None
    if not isinstance(obj, dict):
        return None
    try:
        acc = float(obj.get("accuracy"))
    except (TypeError, ValueError):
        return None
    acc = max(0.0, min(1.0, acc))
    if obj.get("faithfulness") is None:
        log(f"  ! 判卷未返回 faithfulness，回退为 accuracy={acc:.2f}")
        faith = acc
    else:
        try:
            faith = float(obj["faithfulness"])
        except (TypeError, ValueError):
            log(f"  ! faithfulness 非数值 {obj.get('faithfulness')!r}，回退为 accuracy={acc:.2f}")
            faith = acc
    hall = to_flag(obj.get("hallucination", None))
    if hall is None:
        log("  ! 判卷未返回可识别的 hallucination，本条不写编造分数")
    resolved = to_flag(obj.get("resolved", None))
    if resolved is None:
        log("  ! 判卷未返回可识别的 resolved，本条不写「问题已解决」分数")
    return {
        "accuracy": acc,
        "faithfulness": max(0.0, min(1.0, faith)),
        "hallucination": hall,
        "resolved": resolved,
        "reason": str(obj.get("reason") or "")[:500],
    }


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="知识库问答准确性定期审核")
    ap.add_argument("--env", default=os.environ.get("REVIEW_ENV", DEFAULT_ENV))
    ap.add_argument("--hours", type=float, default=None, help="回看小时数（覆盖配置）")
    ap.add_argument("--max", type=int, default=None, help="本次最多审核条数")
    ap.add_argument("--force", action="store_true", help="忽略已打分，重新审核")
    ap.add_argument("--dry-run", action="store_true", help="只判卷不写回 Langfuse")
    args = ap.parse_args()

    cfg = load_env(args.env)
    hours = args.hours if args.hours is not None else float(cfg["LOOKBACK_HOURS"])
    max_traces = args.max if args.max is not None else int(cfg["MAX_TRACES"])
    require_retrieval = str(cfg["REQUIRE_RETRIEVAL"]).lower() in ("1", "true", "yes")
    only_unscored = (not args.force) and str(cfg["ONLY_UNSCORED"]).lower() in ("1", "true", "yes")

    prefix = cfg["SCORE_PREFIX"] or "kb"
    sc_acc = cfg.get("SCORE_NAME_ACCURACY") or f"{prefix}_accuracy"
    sc_fai = cfg.get("SCORE_NAME_FAITHFULNESS") or f"{prefix}_faithfulness"
    sc_hall = cfg.get("SCORE_NAME_HALLUCINATION") or f"{prefix}_hallucination"
    sc_res = cfg.get("SCORE_NAME_RESOLVED") or f"{prefix}_resolved"

    min_ctx = int(cfg["MIN_CONTEXT_CHARS"])
    max_ctx = int(cfg["MAX_CONTEXT_CHARS"])
    max_chunks = int(cfg.get("MAX_CONTEXT_CHUNKS") or 0)
    max_ans = int(cfg.get("MAX_ANSWER_CHARS") or 4000)
    name_filter = cfg["TRACE_NAME_FILTER"] or None
    exclude_users = {u.strip() for u in str(cfg.get("EXCLUDE_USERS") or "").split(",") if u.strip()}
    ret_hints = split_hints(cfg.get("RETRIEVAL_NAME_HINTS"), DEFAULT_RETRIEVAL_HINTS)
    ans_hints = split_hints(cfg.get("ANSWER_NAME_HINTS"), DEFAULT_ANSWER_HINTS)

    if not cfg["LANGFUSE_PUBLIC_KEY"] or not cfg["LANGFUSE_SECRET_KEY"]:
        log("缺少 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY，退出")
        return 2

    lf = Langfuse(cfg["LANGFUSE_HOST"], cfg["LANGFUSE_PUBLIC_KEY"], cfg["LANGFUSE_SECRET_KEY"])
    judge = Judge(cfg["JUDGE_BASE_URL"], cfg["JUDGE_MODEL"], cfg["JUDGE_API_KEY"],
                  int(cfg["JUDGE_TIMEOUT"]), int(cfg.get("JUDGE_MAX_TOKENS") or 3072))

    from_ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    log(f"回看起点 {from_ts}（近 {hours} 小时）| 判卷模型 {cfg['JUDGE_MODEL']} @ {cfg['JUDGE_BASE_URL']}")
    log(f"分数名 {sc_acc} / {sc_fai} / {sc_hall} / {sc_res}")
    log(f"检索节点识别 {ret_hints} | 回复节点识别 {ans_hints} | 单次片段上限 {max_chunks} 段 / {max_ctx} 字")

    # 1) 收集 trace
    traces, page = [], 1
    while len(traces) < max_traces and page <= 10:
        resp = lf.list_traces(from_ts, limit=50, page=page, name=name_filter)
        batch = resp.get("data", [])
        if not batch:
            break
        traces.extend(batch)
        meta = resp.get("meta", {})
        if page >= (meta.get("totalPages") or 1):
            break
        page += 1

    log(f"拉取到 {len(traces)} 条 trace")
    if not traces:
        log("没有可审核的 trace，结束")
        return 0

    results, judged, skipped = [], 0, 0
    today = datetime.now().strftime("%Y%m%d")
    log_dir = os.path.join(HERE, "logs")
    os.makedirs(log_dir, exist_ok=True)
    jsonl_path = os.path.join(log_dir, f"review-{today}.jsonl")

    for tr in traces:
        if judged >= max_traces:
            break
        tid = tr.get("id")
        if exclude_users and (tr.get("userId") or "") in exclude_users:
            skipped += 1
            continue
        try:
            detail = lf.get_trace(tid)
        except Exception as e:
            log(f"  ! 读取 trace {tid} 失败: {e}")
            skipped += 1
            continue

        obs = detail.get("observations") or []
        q_ts = detail.get("timestamp") or tr.get("timestamp")
        q_local = to_local_str(q_ts)
        question = extract_question(detail, obs)

        ans_pairs = collect_answers(detail, obs, ans_hints)
        answer = "\n\n".join(t for _, t in ans_pairs)
        ans_src = [s for s, _ in ans_pairs]

        if not answer.strip() or not question.strip():
            skipped += 1
            continue

        chunks, n_raw = extract_retrieval(obs, ret_hints, max_chunks)

        # 按段贪心填充，超出长度就舍弃整段（不再从中间截断一段话）
        parts, used = [], 0
        for c in chunks:
            if used + len(c) + 5 > max_ctx:
                continue
            parts.append(c)
            used += len(c) + 5
        context = "\n\n---\n\n".join(parts)

        if require_retrieval and len(context) < min_ctx:
            skipped += 1
            continue

        # 只有没打过分才审
        if only_unscored:
            existing = {s.get("name") for s in lf.scores_of_trace(tid) if not s.get("isDeleted")}
            if sc_acc in existing:
                skipped += 1
                continue

        if len(parts) < len(chunks):
            log(f"  · trace={tid} 片段过多，已用 {len(parts)}/{len(chunks)} 段（上限 {max_ctx} 字）")

        t0 = time.time()
        try:
            raw = judge.ask(question[:2000], context, answer[:max_ans])
            j = parse_judgement(raw)
        except Exception as e:
            log(f"  ! 判卷失败 trace={tid}: {repr(e)[:200]}")
            skipped += 1
            continue

        if not j:
            log(f"  ! 判卷返回无法解析 trace={tid}: {raw[:160]!r}")
            skipped += 1
            continue

        cost = time.time() - t0
        rec = {
            "trace_id": tid,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "q_local": q_local,
            "q_utc": str(q_ts or ""),
            "question": question[:300],
            "n_chunks": len(parts),
            "n_chunks_raw": n_raw,
            "answer_sources": ans_src[:5],
            "accuracy": j["accuracy"],
            "faithfulness": j["faithfulness"],
            "hallucination": j["hallucination"],
            "resolved": j["resolved"],
            "reason": j["reason"],
            "judge_model": cfg["JUDGE_MODEL"],
            "judge_seconds": round(cost, 1),
        }

        if not args.dry_run:
            # 分数时间钉在「提问时间」上（走 ingestion，见 create_score 注释）；
            # comment 开头再冗余一份，肉眼在分数列表里也能直接看到。
            tag = f"[提问 {q_local}] " if q_local else ""
            meta = {
                "question_time": q_local or str(q_ts or ""),
                "question_time_utc": str(q_ts or ""),
                "judged_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "question": question[:200],
                "judge_model": cfg["JUDGE_MODEL"],
                "trace_id": tid,
            }
            try:
                lf.create_score(tid, sc_acc, j["accuracy"], "NUMERIC",
                                tag + f"准确率（越高越好）｜{j['reason']}",
                                meta, timestamp=q_ts)
                lf.create_score(tid, sc_fai, j["faithfulness"], "NUMERIC",
                                tag + f"忠实度（越高越好）｜{j['reason']}", meta,
                                timestamp=q_ts)
                if j["hallucination"] is not None:
                    lf.create_score(tid, sc_hall,
                                    1 if j["hallucination"] else 0, "BOOLEAN",
                                    tag + ("存在编造（该项越低越好，1=有编造）"
                                           if j["hallucination"] else
                                           "未发现编造（该项越低越好，0=无编造）"),
                                    meta, timestamp=q_ts)
                if j["resolved"] is not None:
                    lf.create_score(tid, sc_res,
                                    j["resolved"], "BOOLEAN",
                                    tag + ("问题已解决（该项越高越好，1=解决）"
                                           if j["resolved"] else
                                           "问题未解决（该项越高越好，0=未解决）"),
                                    meta, timestamp=q_ts)
                rec["written"] = True
            except Exception as e:
                log(f"  ! 写回分数失败 trace={tid}: {repr(e)[:200]}")
                rec["written"] = False
        else:
            rec["written"] = False

        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        results.append(rec)
        judged += 1
        log(f"  ✓ [{judged}] {q_local} acc={j['accuracy']:.2f} faith={j['faithfulness']:.2f} "
            f"hall={j['hallucination']} res={j['resolved']} chunks={len(parts)}/{n_raw} "
            f"answers={len(ans_pairs)} {cost:.1f}s | {question[:40]}")

    # 2) 汇总
    if results:
        accs = [r["accuracy"] for r in results]
        faiths = [r["faithfulness"] for r in results]
        halls = [r for r in results if r.get("hallucination")]
        ress = [r["resolved"] for r in results if r.get("resolved") is not None]
        avg = sum(accs) / len(accs)
        log("-" * 60)
        log(f"审核 {len(results)} 条 | 平均准确率 {avg:.3f}（越高越好） | "
            f"平均忠实度 {sum(faiths) / len(faiths):.3f}（越高越好）")
        log(f"含编造 {len(halls)} 条 / 占比 {len(halls) / len(results):.3f}（越低越好） | "
            + (f"已解决 {sum(ress)} 条 / 解决率 {sum(ress) / len(ress):.3f}（越高越好）"
               if ress else "本次无可识别的 resolved 判定"))
        log(f"准确率最低 {min(accs):.2f} 最高 {max(accs):.2f} | 明细已写入 {jsonl_path}")
    else:
        log(f"本次没有需要审核的样本（跳过 {skipped} 条）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
