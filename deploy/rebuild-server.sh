#!/usr/bin/env bash
# 改了 requirements.txt（新增/升级 Python 依赖）时，增量重建 server 镜像。
# 策略：以官方 server_latest 为基座，只补装 requirements.txt 里的依赖，
#       构建出本地镜像 server_local，并让容器改用它。
#       不重装系统依赖、不重灌模型，1-3 分钟完成。
#
# 用法: deploy/rebuild-server.sh
#
# 何时用它 vs sync-python.sh:
#   - 只改 .py 代码          -> deploy/sync-python.sh（秒级，无需本脚本）
#   - 改了 requirements.txt  -> 本脚本（补装依赖后再照常 sync 代码）
set -euo pipefail

SSH_HOST="root@118.196.120.182"
REMOTE_DIR="/opt/xiaozhi-server"
LOCAL_REQ="$(cd "$(dirname "$0")/.." && pwd)/main/xiaozhi-server/requirements.txt"
BASE_IMAGE="ghcr.nju.edu.cn/xinnan-tech/xiaozhi-esp32-server:server_latest"
LOCAL_IMAGE="xiaozhi-esp32-server:server_local"

if [[ ! -f "$LOCAL_REQ" ]]; then
  echo "!! 找不到 requirements.txt: $LOCAL_REQ" >&2
  exit 1
fi

echo ">> 1/4 push requirements.txt to ECS (clean build dir)"
ssh -o BatchMode=yes "$SSH_HOST" "mkdir -p '$REMOTE_DIR/.build'"
rsync -az -e "ssh -o BatchMode=yes" "$LOCAL_REQ" "$SSH_HOST:$REMOTE_DIR/.build/requirements.txt"

echo ">> 2/4 build $LOCAL_IMAGE on ECS (base=server_latest, deps only)"
ssh -o BatchMode=yes "$SSH_HOST" \
  "REMOTE_DIR='$REMOTE_DIR' BASE_IMAGE='$BASE_IMAGE' LOCAL_IMAGE='$LOCAL_IMAGE' bash -s" <<'REMOTE'
set -euo pipefail
cd "$REMOTE_DIR/.build"
cat > Dockerfile <<DOCKERFILE
FROM $BASE_IMAGE
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt --default-timeout=120 --retries 5
DOCKERFILE
docker build -t "$LOCAL_IMAGE" .
docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep server_local
REMOTE

echo ">> 3/4 point container to local image (override image field)"
ssh -o BatchMode=yes "$SSH_HOST" \
  "REMOTE_DIR='$REMOTE_DIR' LOCAL_IMAGE='$LOCAL_IMAGE' bash -s" <<'REMOTE'
set -euo pipefail
OVERRIDE="$REMOTE_DIR/docker-compose.override.yml"
if grep -q "image: $LOCAL_IMAGE" "$OVERRIDE"; then
  echo "   override already points to $LOCAL_IMAGE"
elif grep -qE "^[[:space:]]+image:" "$OVERRIDE"; then
  sed -i "s#^\([[:space:]]*\)image:.*#\1image: $LOCAL_IMAGE#" "$OVERRIDE"
  echo "   updated override image -> $LOCAL_IMAGE"
else
  sed -i "/^  xiaozhi-esp32-server:/a\\    image: $LOCAL_IMAGE" "$OVERRIDE"
  echo "   inserted override image -> $LOCAL_IMAGE"
fi
REMOTE

echo ">> 4/4 force-recreate container (compose runs on ECS)"
ssh -o BatchMode=yes "$SSH_HOST" "cd '$REMOTE_DIR' && docker compose -f docker-compose_all.yml -f docker-compose.override.yml up -d --force-recreate xiaozhi-esp32-server"

echo ">> done. check logs with deploy/logs.sh (expect vad/asr success)."
