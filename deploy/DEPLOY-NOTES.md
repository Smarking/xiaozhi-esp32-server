# 部署经验与结论

本文件沉淀本项目「本地开发 → 火山 ECS Docker 部署」全过程的**结论式**经验，只记结论、不记过程日志。操作 SOP 见 [README.md](./README.md)。可观测（OTel+Jaeger 全链路时延/LLM 观测）接入见 [OBSERVABILITY-NOTES.md](./OBSERVABILITY-NOTES.md)。

## 环境事实
- 目标机器：火山 ECS(cn-shanghai)，公网 `118.196.120.182`，Ubuntu 24.04 x86_64，4C/8G。
- 磁盘：系统盘 `/dev/vda` 40G(清冗余后约 46%，docker 镜像/容器在此)；**数据盘 `/dev/vdb` 10G → ext4(label `xzdata`)挂 `/data`**(重资产)，fstab 用 UUID `0fbd3494-6e3e-4dc3-b735-9206cfa15210` + `noatime` 持久化。
- **目录布局（多服务就绪，2026-07 重构）**：项目根 `/srv/<service>/`；共享模型 `/data/models/`；服务私有重资产 `/data/<service>/`。
  - xiaozhi：`/srv/xiaozhi-server/`(compose + `repo/`(git clone origin) + `src→repo/main/xiaozhi-server` 软链 + `data/`密钥 + `uploadfile/`)；重资产在 `/data/xiaozhi-server/`(mysql、voice_sessions)、`/data/models/`(共享 model.pt)。
- **源码管理（2026-07 重构：rsync 双源头 → git 单一源头）**：唯一源头 = GitHub `origin`(`Smarking/xiaozhi-esp32-server`，你的 fork)，上游 `xinnan-tech` 降级为 `upstream`。server `/srv/xiaozhi-server/repo` 是 origin 的 checkout，`git pull` 更新。**不再有 rsync、不再有 server 端独立 git 仓**。
- 部署形态：5 容器全链路——server(8000/8003)、web(manager-api+manager-web, 8002)、db(mysql)、redis、jaeger(可观测)；8000/8002/8003 公网可达已验证(HTTP 200/404)。
- 访问地址：智控台 `:8002`；设备 WS `ws://.../xiaozhi/v1/`；OTA `http://.../xiaozhi/ota/`。

## 硬约束
- 安全组：8000(WS 设备接入)、8003(视觉分析)公网开放；8002(智控台)必须仅限用户出口 IP(`/32`)访问。
- 容器通信：`data/.config.yaml` 的 `manager-api.url` 必须用内部容器名 `http://xiaozhi-esp32-server-web:8002/xiaozhi`，不能用 127.0.0.1。
- 挂载：禁止挂整个 `./src` 到容器工作目录，必须外科式挂载代码子路径（经 `src→repo/main/xiaozhi-server` 软链），保护镜像内置 `models/` 资产。
- 版本管理：`/srv/xiaozhi-server/repo` 是 origin(Smarking) 的标准 git checkout，`.git` 在工作区内；唯一源头是 GitHub，server 只 pull 不自定义提交。
- 数据盘边界：`/data` 是持久数据盘(共享模型 `/data/models`、各服务重资产 `/data/<service>/`)，**不随容器/镜像生命周期**；`/srv/xiaozhi-server/src` 软链指向 `repo/main/xiaozhi-server`，动软链前必须先停容器并确认路径解析。

## 源码纪律（2026-07 重构：rsync 双源头 → git 单一源头）
- **演进**：①「本地=唯一源头，ECS=纯运行时」rsync 覆盖(无历史) → ②「服务端 `/data/xiaozhi-src` 一等 git 源头」rsync+快照(双源头仍发散) → ③**当前：GitHub origin(Smarking) 唯一源头，两端都从它拉取**。
- **当前纪律**：
  - 唯一源头 = GitHub `origin`(你的 fork)，上游 `xinnan-tech` = `upstream`(只拉更新)。本地 `git push origin main`，server `/srv/xiaozhi-server/repo` `git pull`。
  - 代码上线 = `deploy/pull.sh`：server `git pull --ff-only` + `docker restart`，秒级，不重建镜像。**rsync 已退役，`sync-python.sh` 已删**。
  - 改依赖 = `deploy/rebuild-server.sh`：server 先 `git pull`，再从 repo 的 `requirements.txt` 增量重建镜像。
  - 回滚 = server `git checkout <tag>` + restart，干净。
  - server 端只 pull 不自定义提交（应急改码也应本地改→push→pull，保持单一源头）。
- **首次 clone 坑**：ECS 国内直连 GitHub 拉对象慢（~25KB/s，195M 全历史要 30min+）。解法=本地 Mac 已有 repo，`rsync -az --no-o --no-g` 整仓到 ECS `/srv/xiaozhi-server/repo`，再 `git reset --hard HEAD` 修 macOS NFD→Linux NFC 的中文文件名问题。之后日常 `git pull` 只拉 delta，秒级。
- 仓库 owner root（rsync 用 `--no-o --no-g` 落成 root:root，避开 UID 501 的 `safe.directory` 拒写）。

## 三种推送姿势（结论）
- A 改 Python 代码 → `deploy/pull.sh`：本地 `git push` 后，server `git pull --ff-only` + `docker restart`，秒级，不重建镜像、不动依赖、绝不误删模型。单一源头、可回滚。
- B 改 `requirements.txt` → `deploy/rebuild-server.sh`：server 先 `git pull`，再以 `server_latest` 为基座从 repo 的 requirements 增量补装依赖，构建 `server_local` 并 `--force-recreate`；不重装系统依赖、不重灌模型。
- C 版本级发布 → `deploy/deploy.sh`：远程 compose 拉官方镜像、重启全模块。

## 资产边界（结论）
- 代码：走热同步挂载。
- 运行时资产（`models/` 里 VAD onnx、SenseVoice 配置、`music/`、pip 依赖）：留给镜像自带，不被 host shadow。
- 大模型 `model.pt`(893M)：由 base compose 从共享路径 `/data/models/SenseVoiceSmall/model.pt` 挂载（多服务可复用）。

## 外科式挂载清单
override 只挂 6 个代码子路径（经 `src` 软链解析到 `repo/main/xiaozhi-server`）；新增需挂载的代码路径时，override 的 `volumes` 和 `repo/main/xiaozhi-server` 下都要有：
`app.py`、`core/`、`plugins_func/`、`config/`、`agent-base-prompt.txt`、`mcp_server_settings.json`。

## rebuild-server.sh 实现要点
- compose 必须 ssh 到 ECS 本地执行；不能用 `docker --context ecs compose -f <远程路径>`，`-f` 会在本地解析导致找不到文件。
- step2 用引号 heredoc `<<'REMOTE'` + ssh 命令行显式传参，规避 `set -u` 下变量展开陷阱。
- build 上下文用独立干净的 `.build` 目录，避免把 893M `model.pt` 打进上下文。

## 踩坑结论
- 挂载生效：挂载变更必须 `--force-recreate`，`restart` 不重读 compose。
- 模型缺失根因：整目录挂 `./src` 会 shadow 镜像自带模型 → `silero_vad.onnx` 缺失致 server Restarting、`SenseVoiceSmall is not registered`(ASR 缺 config.yaml/configuration.json/.bpe.model)；修复=改外科式挂载 + 从镜像 `docker cp` 补齐缺失文件。
- model.pt 陷阱：rsync 会创建 0 字节空占位覆盖真实模型，必须用 host 已下载的真实 893M `model.pt` 覆盖，sync 白名单绝不纳入 `models`。
- secret 写入：`server.secret` 写入后必须回读校验长度(正确为 36)，曾出现 length=0 空写；用 `read -rsp` + python yaml 方式安全写入，不经聊天传递。
- 首启 8000 报错属正常：智控台未配 secret 前 server 必然报错，配好 secret 并 restart 后恢复。
- 镜像拉取慢属正常：server 镜像 10.6GB，GHCR 走南大代理 `ghcr.nju.edu.cn` 约 1.2MB/s，首拉约 840s；registry-mirrors 只加速 docker.io、不覆盖 GHCR。

## 与官方文档的关系（覆盖度结论）
- `docs/Deployment_all.md`：Docker 全模块首装(localhost 语境)，存在多处笔误与重复；本工具在其上加了热同步 + 公网 ECS 能力。
- `docs/dev-ops-integration.md`：源码裸跑 + `git pull` 自动更新(conda/JDK/Node + nohup + nginx)。
- 结论：deploy 工具在 Python(xiaozhi-server)侧**完全覆盖且更快**(rsync 免编译)；已知缺口——Java(manager-api)源码热更、前端(manager-web)源码热更、nginx 反代/HTTPS，仍需走官方 `dev-ops-integration.md` 裸跑方案。当前二次开发集中在 Python 侧，缺口通常不影响日常。

## 本地 CPU TTS 选型验证（ECS 4C/8G 纯 CPU 实测）
在隔离容器 `tts-lab`(基于 server_local 镜像)实测两套本地 TTS，均达到快于实时(RTF<1)：

- Sambert-HiFiGAN(ModelScope, `damo/speech_sambert-hifigan_tts_zh-cn_16k`)——**中文首选**。
  - 实测：import 3.32s、model_load 10.96s(一次性)；2 线程稳态 RTF 0.49~1.23；**4 线程+预热稳态 RTF 0.52~0.74**，正常对话句合成 0.9~1.8s。
  - 依赖链重：需 build-essential(gcc/g++/make)编译 pysptk、kantts-1.0.1(从 modelscope 源装非 PyPI 空壳)、datasets==3.2.0、scipy==1.10.1(新版移除 `scipy.signal.kaiser`)、setuptools<81(保留 pkg_resources)。
  - 必须离线加载：`MODELSCOPE_OFFLINE=1` + 用本地 `snapshot_download` 路径构造 pipeline，否则即使已缓存 `pipeline()` 仍联网校验超时。
- Pocket TTS(Kyutai 100M, `pocket-tts[audio]`)——**仅英文/欧语，无中文**，作对照。
  - 实测：load_model 缓存后 3.78s、voice state 5.87s、warmup 1.08s；**4 线程稳态 RTF 0.51~0.66**。
  - 依赖：torch>=2.5(与主环境 torch 2.2.2/modelscope numpy<2 冲突，必须独立 venv)；模型走 `HF_ENDPOINT=https://hf-mirror.com`；用 `get_state_for_audio_prompt("alba")` 预置音色(ungated)，切勿传 `.wav`(触发 gated 声克隆)。

结论：**中文语音自然度诉求 → 落地 Sambert(zhitian/zhiyan 女声)**；Pocket 不适用中文，仅印证 CALM 系 100M 模型在 CPU 上的低时延可行性。样音见仓库 `deploy/tts-samples/`。

## 踩坑：dockerd 单容器死锁与 nsenter 绕行
- 现象：容器内残留 D-state 的 `pip install`(大包下载)进程 + 多个 hung `docker exec` 客户端堆积，导致 dockerd 对**该容器**的 exec/cp/logs/inspect/kill/restart 全部 hang，但 `docker ps` 与其它容器不受影响。
- 生产安全绕行：不要重启 dockerd(会波及 restart=always 的生产容器)。改用宿主机 `ps` 找到容器主进程 PID(host 命名空间可见)，`kill -9` 残留 pip/exec 客户端；再用 `nsenter -t <pid> -m -p -u -i -n <cmd>` 直接进容器执行命令、用 `/proc/<pid>/root/...` 直接读写容器 rootfs 完成文件投递，全程不经 docker API。
- 大包投递：ECS 公网下载 torch 等大 wheel 极不稳定(IncompleteRead/ReadTimeout)。可靠路径=本机下载 wheel → `rsync --partial --inplace`(断点续传)到 ECS 主机 → `cp` 到 `/proc/<pid>/root/work/` → `pip install --no-index`(注意 wheel 文件名须符合 `name-ver-...` 规范否则报 Invalid wheel filename)。
