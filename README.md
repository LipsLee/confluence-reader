# Confluence Reader for Codex

A read-only Codex skill for searching, reading, inspecting, and exporting
Confluence pages through the Confluence REST API.

This project is not affiliated with Atlassian.

## Features

- Read pages by URL, page id, `SPACE/Title`, or exact title
- Resolve Confluence short links to canonical page ids
- Search with plain text, space/label filters, or raw CQL
- Inspect page hierarchy, comments, labels, versions, and attachments
- Convert Confluence storage format to readable Markdown
- Download attachments with a configurable size limit
- Export a page tree to Markdown with a manifest and source URLs
- Support Confluence Cloud and Data Center/Server
- Support Basic authentication and personal access tokens
- Keep HTTP redirects on the configured Confluence origin
- Remain strictly read-only: no page creation, editing, or deletion

## Requirements

- Codex with local skill support
- Python 3.10+
- Network access to the Confluence instance
- An account that can view the pages being read

The script uses only the Python standard library.

## Install

### Windows PowerShell

```powershell
$destination = Join-Path $HOME ".codex\skills\confluence-reader"
git clone https://github.com/LipsLee/confluence-reader.git $destination
```

Update later:

```powershell
git -C (Join-Path $HOME ".codex\skills\confluence-reader") pull
```

### macOS or Linux

```bash
git clone https://github.com/LipsLee/confluence-reader.git \
  "${CODEX_HOME:-$HOME/.codex}/skills/confluence-reader"
```

Start a new Codex turn after installation so the skill can be discovered.

## Configure

The safest persistent option is a local JSON file at:

```text
~/.config/confluence-reader/config.json
```

On Windows, the reader also checks:

```text
%APPDATA%\confluence-reader\config.json
```

### Confluence Cloud

```json
{
  "base_url": "https://your-site.atlassian.net/wiki",
  "email": "you@example.com",
  "api_token": "<Atlassian API token>"
}
```

### Confluence Data Center or Server

```json
{
  "base_url": "https://confluence.example.com/confluence",
  "personal_access_token": "<personal access token>",
  "auth": "bearer"
}
```

Environment variables are also supported:

```text
CONFLUENCE_BASE_URL
CONFLUENCE_EMAIL
CONFLUENCE_API_TOKEN
CONFLUENCE_PERSONAL_ACCESS_TOKEN
CONFLUENCE_AUTH=auto|basic|bearer|none
CONFLUENCE_CONFIG
CONFLUENCE_VERIFY_SSL
CONFLUENCE_TIMEOUT
CONFLUENCE_PAGE_SIZE
```

See [references/setup.md](references/setup.md) for details and common errors.

## Verify

```powershell
$skill = Join-Path $HOME ".codex\skills\confluence-reader"
python "$skill\scripts\confluence.py" doctor
```

## Usage

```powershell
# Read a page
python "$skill\scripts\confluence.py" page "<Confluence URL or page id>" --json

# Search
python "$skill\scripts\confluence.py" search "PLC commissioning" --space ENG --json

# Raw CQL
python "$skill\scripts\confluence.py" search --cql 'type = page and text ~ "Siemens S7"' --json

# Page hierarchy
python "$skill\scripts\confluence.py" tree "<Confluence URL or page id>" --max-depth 4

# Comments and attachments
python "$skill\scripts\confluence.py" comments "<Confluence URL or page id>" --json
python "$skill\scripts\confluence.py" attachments "<Confluence URL or page id>" --json

# Export a page tree
python "$skill\scripts\confluence.py" export "<Confluence URL or page id>" `
  --descendants --output-dir "C:\path\to\export"
```

Once configured, Codex can use the skill for requests such as:

```text
Search Confluence for PLC commissioning checklists and summarize the most recent pages.
```

## Security

- Never commit API tokens or passwords.
- Prefer an Atlassian API token or a Data Center personal access token over a
  password.
- Restrict the permissions of the local config file to your account.
- The skill never prints the `Authorization` header or configuration secrets.
- Cross-origin redirects are blocked so credentials are not forwarded to an
  unexpected host.
- Page content and attachments are sent only to the configured Confluence host.

## Limitations

- Only content visible to the authenticated account can be read.
- Confluence search does not index every rendered macro or scanned attachment.
- Dynamic macros such as Jira, Draw.io, or embedded dashboards may expose only
  their source metadata through the API.
- Very large exports are bounded by `--max-pages`, `--max-items`, and
  `--max-attachment-mb`.
- Large first-time exports may be subject to Confluence rate limits.

## 中文快速开始

这是一个只读的 Codex 技能，可以按 URL、页面 ID 或空间/标题读取 Confluence，
支持 CQL 搜索、评论、附件、页面树和 Markdown 导出。

安装到 Windows 的 Codex 技能目录：

```powershell
git clone https://github.com/LipsLee/confluence-reader.git `
  (Join-Path $HOME ".codex\skills\confluence-reader")
```

配置好 `%USERPROFILE%\.config\confluence-reader\config.json` 后运行：

```powershell
$skill = Join-Path $HOME ".codex\skills\confluence-reader"
python "$skill\scripts\confluence.py" doctor
```

不要把 API Token 或密码提交到仓库或发到聊天中。

