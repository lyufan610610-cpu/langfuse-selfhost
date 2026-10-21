#!/bin/bash
# 知识库问答准确性定期审核 —— 供 crontab 调用
# 手动执行: bash <本脚本所在目录>/run-review.sh
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1
mkdir -p "$DIR/logs"
PY="$(command -v python3 || echo /usr/bin/python3)"
echo "===== $(date '+%F %T') 开始审核 =====" >> "$DIR/logs/cron.log"
"$PY" "$DIR/kb_review.py" >> "$DIR/logs/cron.log" 2>&1
echo "===== $(date '+%F %T') 结束 (exit=$?) =====" >> "$DIR/logs/cron.log"
