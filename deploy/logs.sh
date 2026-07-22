#!/usr/bin/env bash
# 查看 ECS 上容器日志（走 SSH，无需本地 docker context）
# 用法: deploy/logs.sh [container]  默认 xiaozhi-esp32-server
set -euo pipefail
SSH_HOST="root@118.196.120.182"
SSH_OPTS="-o BatchMode=yes"
CONTAINER="${1:-xiaozhi-esp32-server}"
ssh $SSH_OPTS "$SSH_HOST" "docker logs -f --tail 100 '$CONTAINER'"
