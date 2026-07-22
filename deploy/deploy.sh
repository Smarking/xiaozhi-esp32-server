#!/usr/bin/env bash
# 远程部署/更新全模块（在 ECS 本地执行 compose，卷路径语义清晰）
# 用法: deploy/deploy.sh [up|down|restart|ps|pull|logs]
set -euo pipefail

SSH_HOST="root@118.196.120.182"
REMOTE_DIR="/srv/xiaozhi-server"
FILES="-f docker-compose_all.yml -f docker-compose.override.yml"
ACTION="${1:-up}"

remote() { ssh -o BatchMode=yes "$SSH_HOST" "cd $REMOTE_DIR && docker compose $FILES $*"; }

case "$ACTION" in
  up)      remote up -d ;;
  down)    remote down ;;
  restart) remote restart ;;
  ps)      remote ps ;;
  pull)    remote pull ;;
  logs)    remote logs -f --tail 100 "${2:-xiaozhi-esp32-server}" ;;
  *) echo "usage: $0 [up|down|restart|ps|pull|logs [container]]"; exit 1 ;;
esac
