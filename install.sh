#!/bin/bash
# 一键部署：建 venv、装依赖、装 systemd 单元、把 MCP 注册进 Claude Code。
# 用法：以装了 Claude Code 的那个用户执行  sudo ./install.sh
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
RUN_USER=${SUDO_USER:-$(id -un)}
[ -f "$DIR/config.json" ] || cp "$DIR/config.example.json" "$DIR/config.json"
cfg() { python3 -c "import json;c=json.load(open('$DIR/config.json'));print(c.get('$1','') if '$1'!='mode' else (c.get('companion') or {}).get('mode','off'))"; }
PORT=$(cfg port); PORT=${PORT:-18050}; MCP=$(cfg mcp_name); MCP=${MCP:-bibliotek}; MODE=$(cfg mode)

echo "== 1/4 系统依赖：pdftoppm（pdf 切页面图用）"
if ! command -v pdftoppm >/dev/null; then
  (command -v apt-get >/dev/null && apt-get install -y -q poppler-utils) || echo "  没装上 poppler-utils，pdf 书会翻不了页（epub / txt 不受影响），请自己装一下"
fi

echo "== 2/4 Python 环境"
[ -x "$DIR/venv/bin/python" ] || python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"
[ "$MODE" = "api" ] && "$DIR/venv/bin/pip" install -q anthropic
mkdir -p "$DIR/data"; chown -R "$RUN_USER" "$DIR/data"; touch "$DIR/.env"; chown "$RUN_USER" "$DIR/.env"; chmod 600 "$DIR/.env"

# claude_p 模式：systemd 里 PATH 很短，把 claude 的完整路径写进 config
if [ "$MODE" = "claude_p" ]; then
  CB=$(sudo -u "$RUN_USER" -H bash -lc 'command -v claude' 2>/dev/null || true)
  if [ -n "$CB" ]; then
    python3 - "$DIR/config.json" "$CB" <<'PY'
import json, sys
p, cb = sys.argv[1], sys.argv[2]
c = json.load(open(p, encoding="utf-8"))
cp = c.setdefault("companion", {}).setdefault("claude_p", {})
if cp.get("bin", "claude") == "claude":
    cp["bin"] = cb
    json.dump(c, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("  claude 在 " + cb + "，已写进 config.json")
PY
  else
    echo "  没找到 claude 命令：装好 Claude Code 后把完整路径填进 config.json 的 companion.claude_p.bin"
  fi
fi

echo "== 3/4 systemd 单元（运行用户 $RUN_USER，目录 $DIR）"
sed "s#__DIR__#$DIR#g; s#__USER__#$RUN_USER#g" "$DIR/systemd/les-sammen.service" > /etc/systemd/system/les-sammen.service
systemctl daemon-reload
systemctl enable --now les-sammen.service
systemctl restart les-sammen.service
sleep 2; curl -s -o /dev/null -w "书房 http://127.0.0.1:$PORT/ -> HTTP %{http_code}\n" "http://127.0.0.1:$PORT/"

echo "== 4/4 把 MCP 注册进 Claude Code（用户级，所有目录可见）"
if sudo -u "$RUN_USER" -H bash -lc "claude mcp add -s user --transport http $MCP http://127.0.0.1:$PORT/mcp" 2>/dev/null; then
  echo "已注册：$MCP -> http://127.0.0.1:$PORT/mcp"
else
  echo "没自动注册上（没装 Claude Code 就不用管）。要手动注册：claude mcp add -s user --transport http $MCP http://127.0.0.1:$PORT/mcp"
fi

echo "== 完成。陪读模式：$MODE"
echo "  书房：http://127.0.0.1:$PORT/   （公网访问一定要套密码，见 examples/nginx.example.conf）"
echo "  key 之类写在 $DIR/.env（比如 ANTHROPIC_API_KEY=...），config.json 里用 \"env:变量名\" 引用"
echo "  改了 config.json：sudo systemctl restart les-sammen"
