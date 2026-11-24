#!/usr/bin/env bash
# 一键把本目录推到 GitHub。
#
# 用法：
#   GH_TOKEN=ghp_xxxxx ./push_to_github.sh                  # 默认仓名 ldcs-workflow，私有
#   GH_TOKEN=ghp_xxxxx ./push_to_github.sh ldcs-workflow    # 指定仓名
#
# Token 从环境变量传入，不写进任何文件、不写进 .git/config。
# 需要权限：classic token 勾 repo；fine-grained token 勾 Administration(读写) + Contents(读写)。
set -euo pipefail

REPO="${1:-ldcs-workflow}"

if [ -z "${GH_TOKEN:-}" ]; then
  echo "❌ 没给 token。用法：GH_TOKEN=ghp_xxx ./push_to_github.sh [仓名]" >&2
  exit 1
fi

API="https://api.github.com"
AUTH=(-H "Authorization: Bearer ${GH_TOKEN}" -H "Accept: application/vnd.github+json")

cd "$(dirname "$0")"

echo "→ 校验 token"
OWNER="$(curl -fsS "${AUTH[@]}" "$API/user" | sed -n 's/.*"login"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
if [ -z "$OWNER" ]; then
  echo "❌ token 无效或网络不通" >&2
  exit 1
fi
echo "  账号：$OWNER"

echo "→ 检查仓库 $OWNER/$REPO"
CODE="$(curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" "$API/repos/$OWNER/$REPO")"
if [ "$CODE" = "404" ]; then
  echo "  不存在，创建私有仓库"
  curl -fsS -X POST "${AUTH[@]}" "$API/user/repos" \
    -d "{\"name\":\"$REPO\",\"private\":true,\"description\":\"广州养老三项测算工作流 + MCP server\"}" \
    -o /dev/null || { echo "❌ 建仓失败（token 缺 Administration 权限？）" >&2; exit 1; }
elif [ "$CODE" = "200" ]; then
  echo "  已存在，直接推"
else
  echo "❌ 查询仓库返回 HTTP $CODE" >&2
  exit 1
fi

if [ ! -d .git ]; then
  echo "→ 初始化本地仓库"
  git init -q
  git symbolic-ref HEAD refs/heads/main
fi

git add -A
if git diff --cached --quiet 2>/dev/null; then
  echo "→ 没有新改动"
else
  git -c user.name="${OWNER}" -c user.email="${OWNER}@users.noreply.github.com" \
    commit -q -m "feat: 待遇测算工作流 + MCP server"
fi

echo "→ 推送"
git push --quiet "https://x-access-token:${GH_TOKEN}@github.com/${OWNER}/${REPO}.git" HEAD:main

# 远程地址保持干净形式，token 不落盘
git remote remove origin 2>/dev/null || true
git remote add origin "https://github.com/${OWNER}/${REPO}.git"

echo ""
echo "✅ 完成：https://github.com/${OWNER}/${REPO}"
echo "   本地已记下干净的 origin，以后可直接 git push（会走系统凭证）"
echo ""
echo "⚠️ 提醒：.env 已被 .gitignore 排除，密钥不会上传。"
