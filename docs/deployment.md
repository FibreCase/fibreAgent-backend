# Docker 部署

仓库自带 `Dockerfile` 与 `docker-compose.yaml`。镜像构建走 `uv`，安装的是 `uv.lock` 里锁定的**精确**依赖版本；运行时以宿主用户身份（见下文）启动。

**关键点：** 本服务**默认**只发起**出站**连接（Telegram long polling + LLM API），**没有任何入站端口**，所以基础 compose（`docker-compose.yaml`）里**不声明 `ports`**、Dockerfile 里**没有 `EXPOSE`**——这是正常的，不是漏配。**唯一例外**是 MCP 用户级 OAuth（phase 4.x）：一旦配置（`.env` 里设置 `OAUTH_CALLBACK_BASE_URL`），应用会额外开一个入站监听 `GET /oauth/callback`（`OAUTH_CALLBACK_PORT`，默认 `8090`），必须能被 OAuth provider（如 Google）公网访问——此时需要**额外**发布该端口（见下文「OAuth 回调端口（phase 4.x）」）。

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
- **时区**：`python:3.14-slim` 镜像默认是 **UTC**，所以容器里的 `get_current_time` 和日志时间戳会跟你的墙钟差几个小时。`get_current_time` 现在返回**带 UTC 偏移**的本地时间（如 `2026-08-29 13:37:37+08:00`），偏移直接取自容器系统时区——**看到 `+00:00` 就说明时区没设对**。compose 通过 `environment: TZ=${TZ:-Asia/Shanghai}` 注入时区（镜像自带 `tzdata`）。把 `.env` 里 `TZ` 改成你的 IANA 时区（如 `Asia/Shanghai`）即可与宿主一致，`get_current_time` 的偏移会相应变成 `+08:00`。**用 `TZ` 而不是挂载 `/etc/localtime`**，因为 macOS 上根本没有 `/etc/localtime` 可挂，Linux 上挂载也只会影响 `TZ=...` 未设置时的兜底。仅影响 Docker；本地 `uv run` 直接用宿主时区，无需设置。
- **优雅停机**：`docker stop` / `compose down` 发 SIGTERM，PTB 会捕获并触发 `post_shutdown`（关闭审批 broker、LLM 客户端与数据库连接），不会丢正在写的 SQLite 数据。

## OAuth 回调端口（phase 4.x）

**是否配 OAuth 就意味着要开放端口？——只有启用时才需要。** OAuth 默认是**关闭**的（`OAUTH_CALLBACK_BASE_URL` 为空）：此时不构造 provider、不起回调服务器、**不开放任何端口**，应用保持 100% 出站，与 Phase 1~4 完全一致。

只有当你**启用** OAuth（`.env` 里设置 `OAUTH_CALLBACK_BASE_URL`，且至少一台 MCP 服务器声明 `authentication.type: oauth`、且相应 provider 凭据已配置）时，应用才会在 `OAUTH_CALLBACK_PORT`（默认 `8090`）上开**唯一一个**入站监听 `GET /oauth/callback`——OAuth provider（如 Google）在授权完成后会把浏览器重定向回这个地址，所以它**必须能被 provider 公网访问到**。

### 为什么不能直接写在基础 compose 里

Docker 容器里 `0.0.0.0` 的本地监听**对外不可达**，除非 compose 里 `ports:` 把它**发布**到宿主。但 Compose **不支持**按环境变量**条件**地发布端口（`ports` 映射没有 `when`/`condition` 字段——那只是 `depends_on` 的东西），所以在基础文件里写死一个端口，会给**所有**用户（包括从不启用 OAuth 的）开一个入站口。为守住「默认无入站端口」这条不变量，我们把它拆成一个**按需加入**的覆盖文件：

```bash
# OAuth 关闭（默认）——照常运行，不发布任何端口：
docker compose up -d --build

# OAuth 启用——多带一个覆盖文件，只发布回调端口：
docker compose -f docker-compose.yaml -f docker-compose.oauth.yaml up -d --build
```

`docker-compose.oauth.yaml` 只做一件事：`ports: - "${OAUTH_CALLBACK_PORT:-8090}:${OAUTH_CALLBACK_PORT:-8090}/tcp"`。**不要**把它里的 `ports:` 抄进基础 `docker-compose.yaml`（基础文件里已用 `DANGER` 注释标出原因）。

### `OAUTH_CALLBACK_BASE_URL` 与宿主端口的关系

- `OAUTH_CALLBACK_BASE_URL` 是 **provider 实际访问的公网 origin**（裸 origin，如 `https://oauth.example.com`），它指向的完整回调 URI 是 `<base>/oauth/callback`，必须能在你的 Google Cloud OAuth 客户端里登记、且能被 Google 公网访问。
- 这个公网地址**不必**等于 `OAUTH_CALLBACK_PORT` 直接暴露的宿主端口——中间可以有一层反向代理 / 隧道（如 Caddy/Nginx、Cloudflare Tunnel、tailscale funnel、ngrok 等）终止 HTTPS 并转发到容器的回调端口。
- **何时才直接把端口发布到 `0.0.0.0`**：只有当宿主本身能被 provider 直接公网访问、且你接受明文/自行处理 TLS 时。若宿主机在 NAT/内网后，通常应把 `OAUTH_CALLBACK_PORT` 绑定到 `127.0.0.1`（在覆盖文件里把映射改成 `"127.0.0.1:8090:8090"`），再交给你的反代/隧道对外暴露——这样公网只看到你的反代，而不是容器。

## CI / 镜像发布

`.github/workflows/build-image.yml` 在每次推送 `v*` tag 时构建镜像并推到 **ghcr.io**（`ghcr.io/<owner>/<repo>:<tag>` + `:<short-sha>`），用内置 `GITHUB_TOKEN`（`packages: write`）。发版流程：bump 版本号（`pyproject.toml` + `src/fibrecase_agent_backend/__init__.py`）→ `uv lock` → 提交 → `git tag -a vX.Y.Z` → `git push` + `git push origin vX.Y.Z`。发版同时要更新 `README.md`（见仓库约定）。
