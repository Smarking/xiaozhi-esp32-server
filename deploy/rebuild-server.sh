#!/usr/bin/env bash
# 改了 requirements.txt（新增/升级 Python 依赖）时，在 server 端增量重建 server 镜像。
# 策略：以官方 server_latest 为基座，只补装 requirements.txt 里的依赖，
#       构建出本地镜像 server_local，并让容器改用它。
#       不重装系统依赖、不重灌模型，1-3 分钟完成。
#
# 现在 requirements.txt 来自 server 端的 git checkout
# （/srv/xiaozhi-server/repo/main/xiaozhi-server/requirements.txt），
# 不再从本地 rsync。脚本会先 git pull 确保 requirements 最新。
#
# 用法: deploy/rebuild-server.sh
#
# 何时用它 vs pull.sh:
#   - 只改 .py 代码          -> deploy/pull.sh（秒级，git pull + restart）
#   - 改了 requirements.txt  -> 本脚本（补装依赖后再 restart）
set -euo pipefail

SSH_HOST="root@118.196.120.182"
REMOTE_DIR="/srv/xiaozhi-server"
REMOTE_REPO="$REMOTE_DIR/repo"
BASE_IMAGE="ghcr.nju.edu.cn/xinnan-tech/xiaozhi-esp32-server:server_latest"
LOCAL_IMAGE="xiaozhi-esp32-server:server_local"
SSH_OPTS="-o BatchMode=yes"

echo ">> 1/3 server 端 git pull（确保 requirements.txt 最新）"
ssh $SSH_OPTS "$SSH_HOST" "cd '$REMOTE_REPO' && git pull --ff-only origin main"

echo ">> 2/3 build $LOCAL_IMAGE on ECS (base=server_latest, deps from repo requirements.txt)"
ssh $SSH_OPTS "$SSH_HOST" \
  "REMOTE_DIR='$REMOTE_DIR' REMOTE_REPO='$REMOTE_REPO' BASE_IMAGE='$BASE_IMAGE' LOCAL_IMAGE='$LOCAL_IMAGE' bash -s" <<'REMOTE'
set -euo pipefail
mkdir -p "$REMOTE_DIR/.build"
cp "$REMOTE_REPO/main/xiaozhi-server/requirements.txt" "$REMOTE_DIR/.build/requirements.txt"
cd "$REMOTE_DIR/.build"
cat > Dockerfile <<DOCKERFILE
FROM $BASE_IMAGE
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt --default-timeout=120 --retries 5
DOCKERFILE
docker build -t "$LOCAL_IMAGE" .
docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep server_local
REMOTE

echo ">> 3/3 force-recreate container（挂载/镜像变更需 force-recreate，restart 不重读 compose）"
ssh $SSH_OPTS "$SSH_HOST" "cd '$REMOTE_DIR' && docker compose -f docker-compose_all.yml -f docker-compose.override.yml up -d --force-recreate xiaozhi-esp32-server"

echo ">> done. check logs: deploy/logs.sh (expect vad/asr success)."
