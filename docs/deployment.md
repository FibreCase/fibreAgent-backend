# Docker 部署

仓库自带 `Dockerfile` 与 `docker-compose.yaml`。镜像构建走 `uv`，安装的是 `uv.lock` 里锁定的**精确**依赖版本；运行时以宿主用户身份（见下文）启动。

**关键点：** 本服务只发起**出站**连接（Telegram long polling + LLM API），**没有任何入站端口**，所以 compose 里**不声明 `ports`**、Dockerfile 里**没有 `EXPOSE`**——这是正常的，不是漏配。

## 用 Docker Compose

```bash
cp .env.example .env                   # 与本地 uv run 共用同一个 .env（.env 已被 gitignore）
docker compose up -d --build           # 构建并后台运行
docker compose logs -f                 # 看日志；出现 "agent backend initialised" 即就绪
docker compose down                    # 停止（数据卷保留）
```

## 持久化与配置

- **同一个 `.env`**：Docker 与本地 `uv run fibrecase-agent-backend` **共用同一个 `.env`**（均以 `.env.example` 为模板）。容器通过 compose 的 `env_file: .env`（或 `docker run --env-file .env`）在**运行时**读取它，`.env` **不会**被打进镜像（`.dockerignore` 已排除）。改配置只改这一处文件，两种启动方式一致。
- **路径无需为容器单独改**：`.env` 里 `DATABASE_URL`、`SYSTEM_PROMPT_PATH`、`ATTACHMENT_STORAGE_PATH` 都是**相对路径**，容器内 `WORKDIR=/app`，所以它们解析到 `/app/data/agent.db`、`/app/config/system_prompt.txt`、`/app/data/attachments`，与本地运行的相对语义一致。
- **数据持久化 + 目录权限**：SQLite 库与图片 blob 都在容器内 `/app/data/`。compose 用**绑定挂载**把它落到仓库下的 `./data/`（`./data:/app/data`），**与本地 `uv run` 共用同一个 `data/`**——同一个库文件、同一套 blob、同一套宿主权限。
  - **为什么不 chown**：绑定挂载会用**宿主目录自身的属主/权限**覆盖镜像里的 `/app/data`。若 `data/` 是 `1000:1000 755`，而容器以别的 uid 运行，容器用户落在 *other* 位（只读），首次建库会 `EACCES`。**解法：让容器以宿主用户身份运行**——compose 里 `user: "${HOST_UID:-1000}:${HOST_GID:-1000}"`，在 `.env` 里设 `HOST_UID`/`HOST_GID`（`id -u`/`id -g`；Linux 首个用户常为 1000，macOS 默认用户常为 501）。这样 `data/` 保持你默认的 `755` 就能被容器写入（因为是 *owner*），你在宿主上也照样能读库，无需任何 `chown`。
  - 设好 `HOST_UID/HOST_GID` 后直接 `docker compose up -d --build` 即可；`data/` 若不存在，`create_engine` 会自动建（首次可能由 root 建为空目录，属主随你而变，一般无碍）。
  - 若你坚持容器用固定的专用 uid 而不想以宿主用户跑，那就改用**命名卷** `-v agent-data:/app/data`：Docker 首次使用会按镜像里 `/app/data` 的属主初始化，省去宿主 chown；代价是查库要走 `docker volume inspect` / 辅助容器，不能直接 `ls ./data`。
- **系统提示词**：镜像里已内置 `config/system_prompt.txt`（`WORKDIR=/app`，与 `.env` 中 `SYSTEM_PROMPT_PATH` 的相对路径一致）。想临时改用别的文件而不重建镜像，可在 compose 里加一行挂载（文件内已注明）。
- **时区**：`python:slim` 镜像默认是 **UTC**，所以容器里的 `get_current_time`（`datetime.now()`）和日志时间戳会跟你的墙钟差几个小时。compose 通过 `environment: TZ=${TZ:-Asia/Shanghai}` 注入时区（镜像自带 `tzdata`）。把 `.env` 里 `TZ` 改成你的 IANA 时区（如 `Asia/Shanghai`）即可与宿主一致。**用 `TZ` 而不是挂载 `/etc/localtime`**，因为 macOS 上根本没有 `/etc/localtime` 可挂，Linux 上挂载也只会影响 `TZ=...` 未设置时的兜底。仅影响 Docker；本地 `uv run` 直接用宿主时区，无需设置。
- **优雅停机**：`docker stop` / `compose down` 发 SIGTERM，PTB 会捕获并触发 `post_shutdown`（关闭审批 broker、LLM 客户端与数据库连接），不会丢正在写的 SQLite 数据。

## CI / 镜像发布

`.github/workflows/build-image.yml` 在每次推送 `v*` tag 时构建镜像并推到 **ghcr.io**（`ghcr.io/<owner>/<repo>:<tag>` + `:<short-sha>`），用内置 `GITHUB_TOKEN`（`packages: write`）。发版流程：bump 版本号（`pyproject.toml` + `src/fibrecase_agent_backend/__init__.py`）→ `uv lock` → 提交 → `git tag -a vX.Y.Z` → `git push` + `git push origin vX.Y.Z`。发版同时要更新 `README.md`（见仓库约定）。
