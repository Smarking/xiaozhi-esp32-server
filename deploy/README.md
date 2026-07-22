# 本地开发 → 火山引擎 ECS 部署工具

面向本仓库的「本地开发完立即推到火山 ECS」的一套脚本，基于 **Git 单一源头 + SSH**。

## 源码管理模型（重要）

**单一源头 = GitHub `origin`（你的 fork `Smarking/xiaozhi-esp32-server`）**。上游 `xinnan-tech` 降级为 `upstream`（只拉更新）。

```
GitHub origin(Smarking)   ←  唯一源头
   ↑ git push                ↓ git pull
本地 Mac (开发)           server /srv/xiaozhi-server/repo (checkout)
                            ↓ src 软链 → repo/main/xiaozhi-server（外科式 bind-mount）
                         容器 xiaozhi-esp32-server
```

- 本地改代码 → `git commit && git push origin main` → `deploy/pull.sh`（server 端 `git pull` + `docker restart`，秒级）。
- 改依赖 → `deploy/rebuild-server.sh`（server 端从 repo 读 requirements 重建镜像）。
- 回滚 → server `git checkout <tag>` 后 restart。
- **不再有 rsync、不再有 server 端独立 git 仓**。两端都从同一个 GitHub 仓拉取，永不发散。

## 目标机器
- 火山 ECS(cn-shanghai)，公网 `118.196.120.182`，Ubuntu 24.04 x86_64，4C/8G。
- 系统盘 `/dev/vda` 40G（docker 镜像/容器 + 共享模型在此）；数据盘 `/dev/vdb` 10G 挂 `/data`（**仅用户产生的运行时数据**）。
- 访问地址：智控台 `:8002`；设备 WS `ws://118.196.120.182:8000/xiaozhi/v1/`；OTA `http://118.196.120.182:8002/xiaozhi/ota/`。

## 目录布局（多服务就绪）

每个服务一个自包含项目根（系统盘）；**共享模型放系统盘 `/srv/models/`**（多服务复用）；**数据盘 `/data/` 仅放用户产生的运行时数据**。完整约定见 server `/srv/README.md`。

```
/srv/                            # 系统盘 — 服务根 + 共享资产
  models/                        # ★ 共享模型（多服务复用）
    SenseVoiceSmall/model.pt     # 893M
  <service>/                     # 项目根（compose + 代码 + 配置）
    docker-compose_all.yml
    docker-compose.override.yml
    repo/                        # git clone origin（代码，小）
    src -> repo/main/xiaozhi-server  # 软链（外科式 bind-mount 用 ./src/...）
    data/                        # 服务密钥（.config.yaml 等，小）

/data/                           # 数据盘 — 仅用户产生的运行时数据
  <service>/
    xiaozhi-server/mysql/data/   # mysql
    xiaozhi-server/voice_sessions/
    xiaozhi-server/uploadfile/   # web 上传
```

xiaozhi 的 compose 挂载：
- 代码：`./src/app.py`、`./src/core`、`./src/plugins_func`、`./src/config`、`./src/agent-base-prompt.txt`、`./src/mcp_server_settings.json`（经 src 软链解析到 repo）。
- 模型：`/srv/models/SenseVoiceSmall/model.pt`（系统盘共享，多服务复用）。
- mysql：`/data/xiaozhi-server/mysql/data`。
- 语音数据集：`/data/xiaozhi-server/voice_sessions`。
- 上传文件：`/data/xiaozhi-server/uploadfile`。
- 配置：`./data`（含 `.config.yaml` 密钥）。

> 外科式挂载原则不变：只挂会变的代码子路径，镜像自带的运行时资产（VAD onnx、SenseVoice 配置、`music/`、pip 依赖）永不被 host 目录 shadow。

## 一次性准备（新 Mac 参考）
```bash
# 1. SSH 免密
ssh-copy-id -i ~/.ssh/id_ed25519.pub root@118.196.120.182
# 2. remote：origin=你的 fork，upstream=上游
git remote rename origin upstream              # 原 origin 是上游 xinnan-tech
git remote add origin https://github.com/Smarking/xiaozhi-esp32-server.git
git push -u origin main
# 3. server 端建布局 + clone（见 DEPLOY-NOTES.md 的迁移记录）
#    /srv/xiaozhi-server/repo = clone origin；src 软链；/srv/models 放共享模型；/data/xiaozhi-server 放用户运行时数据
```

## 三种推送姿势

### A. 改 Python 业务代码（高频）→ git pull，秒级生效
```bash
git commit -am "..." && git push origin main
deploy/pull.sh
```
= server 端 `git pull --ff-only` → `docker restart xiaozhi-esp32-server`。**不重建镜像、依赖不动、绝不误删模型。**

### B. 改了依赖（requirements.txt）→ 增量重建镜像
```bash
git commit -am "deps" && git push origin main
deploy/rebuild-server.sh
```
server 端先 `git pull`，再以 `server_latest` 为基座补装 repo 里的 `requirements.txt`，构建 `server_local` 并 `--force-recreate`。**不重装系统依赖、不重灌模型**。

### C. 版本级发布（拉官方镜像 / 重启全模块）→ 远程 compose
```bash
deploy/deploy.sh           # 在 ECS 上 compose up -d
deploy/deploy.sh pull      # 仅拉取最新官方镜像
deploy/deploy.sh ps        # 查看容器状态
deploy/deploy.sh down      # 停并移除容器
```

## 回滚
```bash
# Python 代码回滚：server 端 checkout 目标 tag/commit 后 restart
ssh root@118.196.120.182 "cd /srv/xiaozhi-server/repo && git checkout <tag> && docker restart xiaozhi-esp32-server"
# 镜像回滚（撤销 rebuild 的 server_local）：编辑 override 删 image 行，force-recreate 即回官方 server_latest
```

## 常用命令
```bash
deploy/logs.sh                    # 跟随 server 日志
deploy/logs.sh xiaozhi-esp32-server-web   # 指定容器
deploy/deploy.sh ps               # 容器状态
```

## 排障
- **server 启动即退出、日志报 `NO_SUCHFILE` VAD onnx / `SenseVoiceSmall is not registered`**：host 目录把镜像自带模型 shadow 了。确认 override 只挂代码子路径（经 src 软链），`src/models` 不应被挂载。
- **server 日志报 manager-api 认证失败 / 拉不到配置**：检查 `data/.config.yaml` 的 `manager-api.secret`（长度应=36）与 `url`（须为 `http://xiaozhi-esp32-server-web:8002/xiaozhi`，不能用 127.0.0.1）。
- **首次启动 8000 端口报错 / `POST /config/server-base 异步请求失败`**：属正常——web 容器还在起时 server 先请求，重试后即恢复（配好 secret 并 restart 后必恢复）。
- **改了挂载但不生效**：挂载变更必须 `--force-recreate`，`restart` 不会重读 compose。
- **server 端 git pull 慢**：ECS 国内直连 GitHub 拉对象偏慢，但日常 `git pull` 只拉增量 delta，秒级；首次 clone 走本地 rsync（见 DEPLOY-NOTES.md）。

## 端口（需在火山安全组放行）
- 8000 WebSocket（设备连接）——须公网开放 `0.0.0.0/0`
- 8002 智控台 Web/OTA——建议只放行你的办公网出口 IP（`<你的IP>/32`）
- 8003 视觉分析接口——按需（设备/公网回调用）
