# SQLBot 本地部署（M1）

SQLBot（dataease/SQLBot）的本地 Docker 部署配置。Web 控制台 `:8000`，MCP 服务 `:8001`。

## 前置：安装 Docker Desktop

Windows 11 家庭版无 Hyper-V，Docker Desktop 必须用 **WSL2 后端**：

1. 管理员 PowerShell 执行：`wsl --install`（装 WSL2 + Ubuntu，需重启）
2. 重启后，从 https://www.docker.com/products/docker-desktop/ 下载安装 Docker Desktop
3. 打开 Docker Desktop，Settings → General 勾选 "Use the WSL 2 based engine"
4. 确认任务栏 Docker 图标变为运行中（或用 `docker ps` 验证）

## 启动

```
start.bat
# 或手动：
docker compose up -d
```

- Web 控制台：http://localhost:8000  （`admin` / `SQLBot@123456`）
- MCP 服务：  http://localhost:8001/mcp
- 查看日志：`docker compose logs -f sqlbot`；停止：`docker compose down`

## 镜像源

`docker-compose.yml` 默认使用阿里云镜像（Docker Hub 在国内直连会超时）：

```yaml
image: registry.cn-qingdao.aliyuncs.com/dataease/sqlbot:v1.10.3
```

当前最新版为 v1.10.3，可用 tag：`v1.10.3` / `v1.10.2` / `latest` / `v1`。

## 已知坑：docker-credential-desktop 报错

若 Git Bash 里跑 `docker compose` 报
`error getting credentials - exec: "docker-credential-desktop": executable file not found in %PATH%`，
是 Docker Desktop 按用户安装后 bin 目录不在当前会话 PATH 里，先执行：

```bash
export PATH="/c/Users/$USERNAME/AppData/Local/Programs/DockerDesktop/resources/bin:$PATH"
```

（cmd / PowerShell 里一般无此问题；`start.bat` 在 cmd 下可直接用。）

## 启动后需要在控制台里做的两件事

1. **配大模型**：控制台 → 系统管理 → AI 模型配置，添加一个模型（如 OpenAI / 通用OpenAI）。
   （后续 My Workbench 的「模型同步」会自动做这一步，当前 M1 手动配一个先跑通。）
2. **配数据源**：控制台 → 数据源，连你的数据库，完成问数验证。

## 注意

- `SECRET_KEY` 在 `.env` 里固定了，避免容器重启后 token 失效。内网/正式部署请重新生成。
- 内网离线部署走 SQLBot 的「离线安装包」：https://community.fit2cloud.com/#/products/sqlbot/downloads
