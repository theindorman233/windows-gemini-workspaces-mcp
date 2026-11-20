# Windows Gemini Workspaces MCP

[English](README.md) | [简体中文](README.zh-CN.md)

A security-hardened **Model Context Protocol (MCP)** server that gives **Gemini App** live, controlled read/search/edit/command access to selected local Windows workspaces over **GitHub OAuth 2.0 + Tailscale Funnel**.

## Highlights

- **Single-Owner GitHub OAuth + DCR Guard**: Authenticates via GitHub OAuth with strict `MCP_GITHUB_OWNER_ID` verification, Fernet-encrypted persistent token/client storage, configurable Dynamic Client Registration lockdown (`MCP_ALLOW_DCR=0`), and client-count caps (`MCP_MAX_REGISTERED_CLIENTS`).
- **Windows Path & Symlink/Junction Sandboxing**: Uses Win32 `CreateFileW` handle locks and reparse-point checks to block directory traversal, NTFS junctions, symlinks, hardlink aliases, protected files (`.env`, `.git`, private keys), and edits to the running MCP server directory itself.
- **Fast Search & Code Navigation**: Native `ripgrep` (`rg.exe`) content search with automatic ignore rules, Everything CLI (`es.exe`) filename lookup with automatic `rg --files` fallback, `ast-grep` structural search, and read-only Git queries.
- **CRLF-Safe Atomic Edits & Automatic Backups**: Optimistic version-hash locking (`read_workspace_ranges` -> `edit_workspace_file` / `write_workspace_file`), automatic CRLF/LF detection and preservation, and rolling backup retention (latest 200 snapshots).
- **Controlled PowerShell Execution**: ANSI-stripped UTF-8 output with head+tail truncation, sensitive environment variable redaction (`*TOKEN*`, `*SECRET*`, `*API_KEY*`, `*PASSWORD*`), background job polling, and Win32 Job Object process-tree cleanup to prevent orphan child processes.

## Available Tools (13)

| Tool | Mode | Description |
|---|---|---|
| `probe` | Read-only | Connectivity and OAuth health check |
| `list_workspaces` | Read-only | List configured workspace names and local roots |
| `list_workspace_dir` | Read-only | Browse directory tree up to configurable depth/limit |
| `find_workspace_files` | Read-only | Fast filename search via Everything CLI (falls back to `rg --files`) |
| `search_workspace_content` | Read-only | Case-insensitive literal text search powered by `rg.exe` |
| `search_workspace_ast` | Read-only | Structural code search powered by `ast-grep` |
| `read_workspace_ranges` | Read-only | Read up to 8 line ranges with version hashes for optimistic edits |
| `query_workspace_git` | Read-only | Inspect `status`, `log`, or unstaged `diff` inside a workspace |
| `edit_workspace_file` | Write | Apply 1–12 unique replacements with CRLF preservation and auto-backup |
| `write_workspace_file` | Write | Create or overwrite a UTF-8 file with version guard and auto-backup |
| `run_workspace_command` | Exec | Run PowerShell commands synchronously or as background jobs |
| `read_command_job` | Read-only | Poll stdout/stderr and exit status of a command job |
| `stop_command_job` | Exec | Terminate a running background command job and its child process tree |

## Quick Start

### 1. Prerequisites

- Windows 10/11 with **Python 3.11+** and **PowerShell 7 (`pwsh`)**
- [Tailscale](https://tailscale.com/) with Funnel enabled (or another HTTPS reverse proxy)
- Optional CLI tools on `PATH` (or configured via `.env`): `rg.exe` (ripgrep), `es.exe` (Everything CLI), `ast-grep.exe`, `git.exe`

### 2. Install Dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\pip.exe install -e .
.\.venv\Scripts\pip.exe install pytest httpx
```

### 3. Configure Environment & GitHub OAuth Secret

1. Copy `.env.example` and configure your environment variables (`MCP_PUBLIC_BASE`, `MCP_GITHUB_CLIENT_ID`, `MCP_GITHUB_OWNER_ID`, `MCP_WORKSPACES`).
2. Create a GitHub OAuth App with callback URL `https://<your-node>.ts.net/auth/callback`.
3. Store your GitHub OAuth client secret encrypted with Windows DPAPI (CurrentUser scope):

```powershell
$dir = Join-Path $env:LOCALAPPDATA 'WindowsGeminiMCP'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$secret = Read-Host -Prompt 'GitHub OAuth Client Secret' -AsSecureString
ConvertFrom-SecureString $secret | Set-Content (Join-Path $dir 'github-client-secret.dpapi') -Encoding UTF8
```

4. Initialize the persistent state directory once:

```powershell
$stateDir = Join-Path $env:LOCALAPPDATA 'WindowsGeminiMCP\live-state'
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
.\.venv\Scripts\python.exe synthetic_edits.py init --workspaces
```

### 4. Expose via Tailscale Funnel & Start

```powershell
tailscale funnel --bg --set-path=/mcp 8766
$env:MCP_ALLOW_DCR = '1'   # Enable during first-time Gemini App linking
.\start_live_task.ps1
```

Once linked in Gemini App, set `MCP_ALLOW_DCR=0` (the default in `start_live_task.ps1`) to lock down public `/register` requests.

### 5. Run Tests

```powershell
.\.venv\Scripts\pytest.exe -v
```

## Security Notes

- Keep `%LOCALAPPDATA%\WindowsGeminiMCP` restricted to your Windows user, `SYSTEM`, and `Administrators` (`icacls`).
- Never commit `.env`, `workspaces.json`, or `*.dpapi` files.
- `edit_workspace_file` and `write_workspace_file` intentionally block writes into the running MCP server directory.

## License

[MIT](LICENSE)
