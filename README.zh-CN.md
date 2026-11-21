# Windows Gemini Workspaces MCP

[English](README.md) | [简体中文](README.zh-CN.md)

一个面向 **Gemini App** 的 Windows 本地工作区 **MCP（Model Context Protocol）** 服务端，通过 **GitHub OAuth 2.0 + Tailscale Funnel** 提供安全、可控的实时文件检索、读写编辑与 PowerShell 命令执行能力。

## 核心特性

- **单用户 GitHub OAuth 与注册防护**：仅允许配置的 `MCP_GITHUB_OWNER_ID` 本人调用；OAuth 客户端与令牌经 Fernet 加密持久化；支持日常关闭公网动态注册（`MCP_ALLOW_DCR=0`）与客户端数量上限（`MCP_MAX_REGISTERED_CLIENTS`）。
- **Windows 路径与符号链接隔离**：基于 Win32 `CreateFileW` 句柄锁与重解析点校验，阻断目录穿越、NTFS Junction、符号链接、多硬链接别名、敏感文件（`.env`、`.git`、私钥等），并禁止写入正在运行的 MCP 服务自身目录。
- **高性能代码与文件检索**：内置 `ripgrep`（`rg.exe`）原生全文搜索（自动忽略 `.venv`/`node_modules`/`.git` 等）、Everything CLI（`es.exe`）极速找文件（缺失或失败自动降级 `rg --files`）、`ast-grep` 语法树检索以及只读 Git 查询。
- **CRLF 无损编辑与自动备份**：支持基于版本号（`version`）的乐观锁编辑，自动识别并保留 Windows 文件的 CRLF/LF 换行风格，每次写入自动备份并滚动保留最近 200 份快照。
- **受控 PowerShell 执行**：自动剥离 ANSI 色码并统一 UTF-8 输出，长输出采用头尾保留截断，自动过滤敏感环境变量（`*TOKEN*`、`*SECRET*`、`*API_KEY*`、`*PASSWORD*`），支持后台任务轮询，并通过 Win32 Job Object 清理整个子进程树以防孤儿进程残留。

## 工具清单（共 13 个）

| 工具名 | 权限 | 功能说明 |
|---|---|---|
| `probe` | 只读 | 连通性与 OAuth 鉴权探针 |
| `list_workspaces` | 只读 | 列出已配置的工作区名称与根目录 |
| `list_workspace_dir` | 只读 | 按指定深度与数量上限浏览目录结构 |
| `find_workspace_files` | 只读 | 通过 Everything CLI 搜索文件名（自动降级 `rg --files`） |
| `search_workspace_content` | 只读 | 基于 `rg.exe` 的大小写不敏感文本搜索 |
| `search_workspace_ast` | 只读 | 基于 `ast-grep` 的代码结构检索 |
| `read_workspace_ranges` | 只读 | 批量读取最多 8 个行区间并返回版本哈希 |
| `query_workspace_git` | 只读 | 查看工作区内的 `status`、`log` 或未暂存 `diff` |
| `edit_workspace_file` | 写入 | 精确替换 1–12 处文本，保留原换行符并自动备份 |
| `write_workspace_file` | 写入 | 新建或覆写 UTF-8 文件，支持版本校验与自动备份 |
| `run_workspace_command` | 执行 | 同步或后台运行 PowerShell 命令 |
| `read_command_job` | 只读 | 查询命令任务状态及捕获的 stdout/stderr |
| `stop_command_job` | 执行 | 终止运行中的后台任务及其子进程树 |

## 快速开始

### 1. 环境准备

- Windows 10/11，安装 **Python 3.11+** 与 **PowerShell 7 (`pwsh`)**
- 启用 Funnel 的 [Tailscale](https://tailscale.com/)（或其他 HTTPS 反向代理）
- 可选命令行工具（加入 `PATH` 或在环境变量中指定路径）：`rg.exe`、`es.exe`、`ast-grep.exe`、`git.exe`

### 2. 安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\pip.exe install -e .
.\.venv\Scripts\pip.exe install pytest httpx
```

### 3. 配置环境变量与 GitHub OAuth 密钥

1. 参考 `.env.example` 配置环境变量（`MCP_PUBLIC_BASE`、`MCP_GITHUB_CLIENT_ID`、`MCP_GITHUB_OWNER_ID`、`MCP_WORKSPACES`）。
2. 在 GitHub 创建一个 OAuth App，回调地址填写 `https://<your-node>.ts.net/auth/callback`。
3. 使用 Windows DPAPI（当前用户作用域）加密保存 GitHub Client Secret：

```powershell
$dir = Join-Path $env:LOCALAPPDATA 'WindowsGeminiMCP'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$secret = Read-Host -Prompt 'GitHub OAuth Client Secret' -AsSecureString
ConvertFrom-SecureString $secret | Set-Content (Join-Path $dir 'github-client-secret.dpapi') -Encoding UTF8
```

### 4. 配置 Tailscale Funnel 并启动服务

```powershell
tailscale funnel --bg --set-path=/mcp 8766
$env:MCP_ALLOW_DCR = '1'   # 首次在 Gemini App 关联连接器时临时开启
.\start_live_task.ps1
```

在 Gemini App 关联成功后，保持 `start_live_task.ps1` 默认的 `MCP_ALLOW_DCR=0` 即可关闭公网动态注册入口。

### 5. 运行测试

```powershell
.\.venv\Scripts\pytest.exe -v
```

### 6. 可选：托管为计划任务

在管理员 PowerShell 中执行 `.\register_tasks.ps1`，会创建两个当前用户级任务：

| 任务 | 触发器 | 动作 |
| --- | --- | --- |
| `WindowsGeminiMcpLive` | 登录时 + 开机时 | `wscript.exe //B //Nologo launch_hidden.vbs` |
| `WindowsGeminiMcpLiveGuard` | 每分钟 | `wscript.exe //B //Nologo guard_hidden.vbs` |

两者都经 `wscript.exe` 启动是有意为之：它是 GUI 子系统宿主，任务计划程序不会为任务创建控制台。若直接把 `pwsh.exe` 设为任务动作，它是控制台子系统程序，创建时就会拿到控制台窗口，而 `-WindowStyle Hidden` 只是事后再把这个窗口隐藏，中间那一瞬会在屏幕上闪出终端。

`launch_hidden.vbs` 以 `-Detach` 调用 `start_live_task.ps1`，后者用 `CreateNoWindow` 启动服务，使其完全不占用控制台，PID 写入 `live-server-<端口>.pid`，并等到端口真正监听后才报告成功。分离启动的进程没有 stdout/stderr，因此 `oauth_probe.py` 会在设置了 `MCP_LOG_FILE` 时把两者重定向到 `%LOCALAPPDATA%\WindowsGeminiMCP\server.log`。PID 文件名带端口，不同端口的实例不会互相误杀。

不配置 `RestartOnFailure`，自愈由守卫任务负责：它每分钟检查一次端口，服务在线时一秒内退出，否则重启主任务并把结果写入 `guard.log`。

## 安全建议

- 建议收紧 `%LOCALAPPDATA%\WindowsGeminiMCP` 目录权限（`icacls`），仅保留当前用户、`SYSTEM` 与 `Administrators`。
- 切勿将 `.env`、`workspaces.json` 或 `*.dpapi` 提交至公开仓库。
- `edit_workspace_file` 与 `write_workspace_file` 默认禁止修改正在运行的 MCP 服务自身目录。

## 开源协议

[MIT](LICENSE)
