#!/usr/bin/env bash
# 查看 ECS 上容器日志
# 用法: deploy/logs.sh [container]  默认 xiaozhi-esp32-server
set -euo pipefail
CONTEXT="ecs"
CONTAINER="${1:-xiaozhi-esp32-server}"
docker --context "$CONTEXT" logs -f --tail 100 "$CONTAINER"
