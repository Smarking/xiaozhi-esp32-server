# 本地开发 → 火山引擎 ECS 部署工具

面向本仓库的「本地开发完立即推到火山 ECS」的一套脚本，基于 **Docker Context over SSH**。

## 目标机器
- 火山 ECS(cn-shanghai)，公网 `118.196.120.182`，Ubuntu 24.04 x86_64，4C/8G。
- 部署目录：ECS 上 `/opt/xiaozhi-server`（compose + data + models + 源码热同步目录 src）。
- 本地 docker context 名：`ecs`（`ssh://root@118.196.120.182`）。

## 一次性准备（已完成，供重装参考）
```bash
# 1. 本地公钥装到 ECS（免密）
ssh-copy-id -i ~/.ssh/id_ed25519.pub root@118.196.120.182
# 2. 本地建 docker context
docker context create ecs --docker "host=ssh://root@118.196.120.182"
# 3. ECS 装 docker（见部署记录），目录 /opt/xiaozhi-server 下放 docker-compose_all.yml、
#    docker-compose.override.yml、data/.config.yaml、models/SenseVoiceSmall/model.pt、src/(python源码)
```

## 三种推送姿势

### A. 改 Python 业务代码（高频）→ 热同步，秒级生效
采用**外科式代码挂载**：只把会变的代码子路径（`app.py`、`core/`、`plugins_func/`、
`config/`、`agent-base-prompt.txt`、`mcp_server_settings.json`）bind-mount 进容器，
镜像自带的运行时资产（`models/` 里的 VAD onnx、SenseVoice 配置、`music/`、pip 依赖）
保持不动，永不被 host 目录 shadow。大模型 `model.pt` 由 base compose 单独从 host 挂载。

改完本地代码后：
```bash
deploy/sync-python.sh
```
= 服务端 git 快照(防覆盖) → 按白名单 rsync 上述代码路径到 ECS `/opt/xiaozhi-server/src/`(软链至数据盘 `/data/xiaozhi-src`) → 服务端记录「本地上线」快照 → `docker --context ecs restart xiaozhi-esp32-server`。**不重建镜像、依赖不动、绝不误删模型；两端改动均入 git 可回滚。**

> 挂载点定义在 `deploy/docker-compose.override.yml`，与 sync 脚本的白名单严格一一对应。
> 新增需要热同步的代码路径时，两处都要同步添加。
> 服务端源码是 `/data/xiaozhi-src`(数据盘 git 仓库)，可在服务端直接 commit/回滚；
> 详见 [DEPLOY-NOTES.md](./DEPLOY-NOTES.md) 的「源码纪律」段。

### B. 改了依赖（requirements.txt）→ 增量重建镜像
```bash
deploy/rebuild-server.sh
```
以官方 `server_latest` 为基座，在 ECS 上只补装 `requirements.txt` 的依赖，
构建本地镜像 `xiaozhi-esp32-server:server_local`，改 override 的 `image` 指向它并
force-recreate 容器。**不重装系统依赖、不重灌模型**，改依赖后照常 `deploy/sync-python.sh` 同步代码。

### C. 版本级发布（拉官方镜像 / 重启全模块）→ 远程 compose
```bash
deploy/deploy.sh           # 在 ECS 上 compose up -d（拉官方镜像）
deploy/deploy.sh pull      # 仅拉取最新官方镜像
deploy/deploy.sh ps        # 查看容器状态
deploy/deploy.sh down      # 停并移除容器
```

## 与官方两套部署文档的关系（覆盖度）

本仓库 `docs/` 下有两套官方部署文档，定位与本工具不同：

| 官方文档 | 范式 | 本 deploy 工具的覆盖 |
|---|---|---|
| `Deployment_all.md` | Docker 全模块首装（localhost/单机语境） | ✅ 首装思路一致；本工具在其上增加了「本地开发热同步 + 公网 ECS」能力 |
| `dev-ops-integration.md` | 源码裸跑 + `git pull` 自动更新（conda/JDK/Node + nohup + nginx） | ⚠️ 见下方能力边界 |

**能力对比（vs `dev-ops-integration.md`）**

| 能力 | 官方 dev-ops | 本 deploy 工具 |
|---|---|---|
| Python 代码更新 | git pull + pip + nohup 重启 | ✅ rsync 热同步 + 容器 restart（秒级、免编译，更快） |
| Python 依赖变更 | `pip install -r requirements.txt` | ✅ `rebuild-server.sh` 增量重建镜像 |
| 更新来源 | `git pull` 拉**上游最新** | 推**本地工作区**改动上线（定位不同） |
| 数据库迁移 | Liquibase 自动执行 | ✅ 相同（由官方 web 镜像内的 Liquibase 执行） |
| Java(manager-api) 源码热更 | ✅ `mvn package` + 重启 8002 | ❌ 用官方 `web_latest` 镜像，不改 Java 源码 |
| 前端(manager-web) 源码热更 | ✅ `npm build` → nginx | ❌ 同上，用官方镜像 |
| 反向代理 / HTTPS | ✅ nginx 反代 | ❌ 直接暴露端口，未涉及 |

**结论**：本工具在 **Python(xiaozhi-server) 侧完全覆盖且体验更好**；若需**改 Java/前端源码**或**上 nginx/HTTPS**，仍走官方 `dev-ops-integration.md` 的裸跑方案。当前项目二次开发集中在 Python 侧，故此缺口通常不影响日常。

## 回滚
```bash
# 回滚到官方原版镜像（撤销 rebuild-server.sh 的 server_local）
# 编辑 ECS 上 docker-compose.override.yml，删除或改回 image 行，然后：
docker --context ecs compose -f /opt/xiaozhi-server/docker-compose_all.yml \
  -f /opt/xiaozhi-server/docker-compose.override.yml up -d --force-recreate xiaozhi-esp32-server
```
Python 代码回滚：本地 `git checkout` 到目标版本后再 `deploy/sync-python.sh` 即可。

## 常用命令
```bash
deploy/logs.sh                    # 跟随 server 日志
deploy/logs.sh xiaozhi-esp32-server-web   # 指定容器
docker --context ecs compose -f /opt/xiaozhi-server/docker-compose_all.yml ps
docker --context ecs compose -f /opt/xiaozhi-server/docker-compose_all.yml -f /opt/xiaozhi-server/docker-compose.override.yml up -d
```

## 排障
- **server 启动即退出、日志报 `NO_SUCHFILE` VAD onnx / `SenseVoiceSmall is not registered`**：
  多为 host 目录把镜像自带模型 shadow 掉了。确认 override 只挂代码子路径（不是整个工作目录），
  且 `src/models` 不应存在、不应被 rsync。
- **server 日志报 manager-api 认证失败 / 拉不到配置**：检查 `data/.config.yaml` 的
  `manager-api.secret`（长度应=智控台 server.secret）与 `url`（docker 部署须为
  `http://xiaozhi-esp32-server-web:8002/xiaozhi`，不能用 127.0.0.1）。
- **首次启动 8000 端口报错**：属正常——尚未在智控台配 secret 时 server 必然报错，
  配好 secret 并 restart 后即恢复（与官方 `Deployment_all.md` 的提示一致）。
- **改了挂载但不生效**：挂载变更必须 `--force-recreate`，`restart` 不会重读 compose。

## 端口（需在火山安全组放行）
- 8000 WebSocket（设备连接）——须公网开放 `0.0.0.0/0`
- 8002 智控台 Web/OTA——建议只放行你的办公网出口 IP（`<你的IP>/32`）
- 8003 视觉分析接口——按需（设备/公网回调用）

访问智控台：`http://118.196.120.182:8002`
设备 WebSocket：`ws://118.196.120.182:8000/xiaozhi/v1/`
OTA 接口：`http://118.196.120.182:8002/xiaozhi/ota/`
