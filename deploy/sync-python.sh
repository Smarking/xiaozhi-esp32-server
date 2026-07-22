#!/usr/bin/env bash
# 热同步 Python 源码到火山 ECS 并重启容器（秒级生效，不重建镜像）
# 用法: deploy/sync-python.sh
#
# 加固后策略：白名单同步 + 服务端 git 一等源头。
#   1) 推送前先在服务端 git 快照（收录服务端上的任何手改），防止随后的 rsync
#      --delete 静默覆盖服务端改动而丢失历史。
#   2) 白名单 rsync：只推送 override 里真正 bind-mount 的代码路径，绝不触碰
#      models/ 等镜像自带资产。即便用 --delete，也只作用于代码子树，
#      不可能误删模型；仓库 .git 在工作区根，不在任何子树内，永不被删。
#   3) 推送后再在服务端 git 提交一份「本地上线」快照，形成可回滚的上线点。
#   数据盘仓库位置：ECS /data/xiaozhi-src（REMOTE_SRC 经软链指向它）。
set -euo pipefail

SSH_HOST="root@118.196.120.182"
LOCAL_SRC="$(cd "$(dirname "$0")/.." && pwd)/main/xiaozhi-server"
REMOTE_SRC="/opt/xiaozhi-server/src"          # 软链 → /data/xiaozhi-src
REMOTE_REPO="/data/xiaozhi-src"               # 数据盘上的实际 git 工作区
CONTAINER="xiaozhi-esp32-server"
CONTEXT="ecs"
SSH_OPTS="-o BatchMode=yes"

# 与 docker-compose.override.yml 的挂载点严格一致的代码路径白名单
PATHS=(
  app.py
  core
  plugins_func
  config
  agent-base-prompt.txt
  mcp_server_settings.json
)

RSYNC_EXCLUDES=(
  --exclude '__pycache__/'
  --exclude '*.pyc'
)

TS="$(date +%Y-%m-%d_%H:%M:%S)"

# ── 1) 推送前：服务端 git 快照（把服务端任何未提交改动先落历史，防被覆盖丢失） ──
echo ">> [1/4] 服务端推送前快照（防覆盖）"
ssh $SSH_OPTS "$SSH_HOST" bash -s <<REMOTE
set -euo pipefail
cd "$REMOTE_REPO"
git config --global --add safe.directory "$REMOTE_REPO" 2>/dev/null || true
if [[ -n "\$(git status --porcelain)" ]]; then
  git add -A
  git commit -q -m "snapshot(server): 本地推送前自动快照 $TS" \
    && echo "   ✓ 已快照服务端改动"
else
  echo "   · 服务端无未提交改动，跳过"
fi
REMOTE

# ── 2) 白名单 rsync ──
echo ">> [2/4] 白名单 rsync 推送本地代码"
for p in "${PATHS[@]}"; do
  src="$LOCAL_SRC/$p"
  if [[ ! -e "$src" ]]; then
    echo "!! 跳过不存在的路径: $src" >&2
    continue
  fi
  if [[ -d "$src" ]]; then
    echo "   sync dir  $p/"
    rsync -az --delete "${RSYNC_EXCLUDES[@]}" \
      -e "ssh $SSH_OPTS" \
      "$src/" "$SSH_HOST:$REMOTE_SRC/$p/"
  else
    echo "   sync file $p"
    rsync -az -e "ssh $SSH_OPTS" \
      "$src" "$SSH_HOST:$REMOTE_SRC/$p"
  fi
done

# ── 3) 推送后：服务端 git 提交「本地上线」快照（可回滚上线点） ──
echo ">> [3/4] 服务端记录本次上线快照"
ssh $SSH_OPTS "$SSH_HOST" bash -s <<REMOTE
set -euo pipefail
cd "$REMOTE_REPO"
if [[ -n "\$(git status --porcelain)" ]]; then
  git add -A
  git commit -q -m "deploy(local): 本地工作区上线 $TS" \
    && echo "   ✓ 已记录上线点 \$(git rev-parse --short HEAD)"
else
  echo "   · 与服务端当前一致，无变化"
fi
REMOTE

# ── 4) 重启容器 ──
echo ">> [4/4] restart container $CONTAINER"
docker --context "$CONTEXT" restart "$CONTAINER"

echo ">> done. tail logs with: deploy/logs.sh"
