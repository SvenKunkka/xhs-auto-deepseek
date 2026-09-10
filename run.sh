#!/usr/bin/env bash
# 便捷启动器：自动使用项目内的 .venv，不用每次敲 venv 路径
# 用法：./run.sh "话题" --pages 4 --no-publish
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 解析真实脚本路径：被 symlink 到 ~/.local/bin/xhs 时，BASH_SOURCE 指向的是
# 链接本身而不是项目目录，必须逐层展开，否则找不到 .venv
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  LINK_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [ "${SOURCE#/}" = "$SOURCE" ] && SOURCE="$LINK_DIR/$SOURCE"
done
DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
PY="$DIR/.venv/bin/python"

if [ ! -x "$PY" ]; then
  cat >&2 <<'EOF'
没找到 .venv，先执行一次：
  python3 -m venv .venv
  ./.venv/bin/pip install -r requirements.txt
  ./.venv/bin/playwright install chromium
EOF
  exit 1
fi

exec "$PY" "$DIR/xhs_auto.py" "$@"
