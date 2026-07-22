#!/usr/bin/env bash
# 拉取最新代码到 server 并重启容器（秒级生效，不重建镜像）。
#
# 这是新的代码上线方式，取代旧的 rsync 热同步（sync-python.sh 已退役）。
# 工作流：本地 commit → git push origin main → 本脚本在 server 端 git pull + restart。
# 单一源头 = GitHub origin(Smarking)，server /srv/xiaozhi-server/repo 是它的 checkout。
#
# 用法: deploy/pull.sh
#
# 何时用它 vs rebuild-server.sh:
#   - 只改 .py 代码          -> deploy/pull.sh（秒级，git pull + restart）
#   - 改了 requirements.txt  -> deploy/rebuild-server.sh（重建镜像后再 restart）
set -euo pipefail

SSH_HOST="root@118.196.120.182"
REMOTE_DIR="/srv/xiaozhi-server"
REMOTE_REPO="$REMOTE_DIR/repo"
CONTAINER="xiaozhi-esp32-server"
SSH_OPTS="-o BatchMode=yes"

echo ">> [1/2] server 端 git pull --ff-only ($REMOTE_REPO)"
ssh $SSH_OPTS "$SSH_HOST" "cd '$REMOTE_REPO' && git pull --ff-only origin main"

echo ">> [2/2] restart container $CONTAINER"
ssh $SSH_OPTS "$SSH_HOST" "docker restart '$CONTAINER'"

echo ">> done. tail logs: deploy/logs.sh"
