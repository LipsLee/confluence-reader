# Setup

The Confluence Agent requires Python 3.10 or newer and a network path to the
Confluence site. The recommended installer is
[uv](https://docs.astral.sh/uv/). The MCP server is local `stdio`; no inbound
port or web server is opened.

## Fast setup

The recommended path is the interactive wizard. It hides token input, writes a
user-only config file, tests access, and prints an MCP configuration snippet.

1. Create an Atlassian API token at
   <https://id.atlassian.com/manage-profile/security/api-tokens>.
2. Run the wizard from an interactive terminal:

   ```powershell
   $skill = Join-Path $HOME ".codex\skills\confluence-reader"
   uv run --project $skill confluence setup
   ```

3. Enter the Confluence base URL, account email, and token when prompted.
   Choose whether normal writes should be enabled. The token is never echoed.
4. Start a new agent turn and verify:

   ```powershell
   uv run --project $skill confluence doctor --json
   ```

Do not send a token to an agent chat. If the wizard cannot run interactively,
use the manual JSON configuration below.

## Install

### Windows PowerShell

```powershell
$skill = Join-Path $HOME ".codex\skills\confluence-reader"
git clone https://github.com/LipsLee/confluence-reader.git $skill
uv tool install --editable $skill
confluence --version
```

### macOS or Linux

```bash
skill="${CODEX_HOME:-$HOME/.codex}/skills/confluence-reader"
git clone https://github.com/LipsLee/confluence-reader.git "$skill"
uv tool install --editable "$skill"
confluence --version
```

If you do not want a global command, use
`uv run --project <repo> confluence ...`. The legacy entry point
`python scripts/confluence.py` finds the same shared implementation and will
restart through `uv` when the Markdown dependency is missing.

## Configuration

Environment variables take precedence over the JSON file. The reader checks:

1. The path in `CONFLUENCE_CONFIG`
2. `~/.config/confluence-reader/config.json`
3. `%APPDATA%\confluence-reader\config.json` on Windows
4. `~/.confluence-reader.json`

The file is secret. Do not commit it. On Windows, remove inherited access and
grant only your account after creating the file:

```powershell
$dir = Join-Path $HOME ".config\confluence-reader"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$file = Join-Path $dir "config.json"
# Write the JSON file first, then restrict it:
icacls $file /inheritance:r /grant:r "$env:USERNAME:(R,W)"
```

### Confluence Cloud

Create an Atlassian API token at
<https://id.atlassian.com/manage-profile/security/api-tokens>.

```json
{
  "base_url": "https://your-site.atlassian.net/wiki",
  "email": "you@example.com",
  "api_token": "<Atlassian API token>",
  "verify_ssl": true,
  "timeout": 30
}
```

Start with a scoped token that can read pages, comments, labels, and
attachments. Add write scopes for create/update/label/comment/attachment
operations, and the delete scope if trash/restore is required.

### Confluence Data Center or Server

Prefer a personal access token:

```json
{
  "base_url": "https://confluence.example.com/confluence",
  "personal_access_token": "<personal access token>",
  "auth": "bearer"
}
```

Legacy Basic authentication is still accepted:

```json
{
  "base_url": "https://confluence.example.com/confluence",
  "email": "username",
  "api_token": "<password or legacy credential>",
  "auth": "basic"
}
```

Use that compatibility mode only where a PAT is unavailable.

### Anonymous public content

```json
{
  "base_url": "https://confluence.example.com/confluence",
  "auth": "none"
}
```

Anonymous access is always read-only, even when the write switch is on.

### Environment variables

```text
CONFLUENCE_BASE_URL
CONFLUENCE_EMAIL
CONFLUENCE_API_TOKEN
CONFLUENCE_PERSONAL_ACCESS_TOKEN
CONFLUENCE_AUTH=auto|basic|bearer|none
CONFLUENCE_CONFIG
CONFLUENCE_FLAVOR=auto|cloud|data_center
CONFLUENCE_ENABLE_WRITES=true|false
CONFLUENCE_STATE_DIR
CONFLUENCE_FILE_ROOTS
CONFLUENCE_MAX_ATTACHMENT_MB
CONFLUENCE_VERIFY_SSL
CONFLUENCE_TIMEOUT
CONFLUENCE_PAGE_SIZE
HTTP_PROXY / HTTPS_PROXY / NO_PROXY
```

`CONFLUENCE_PAGE_SIZE` is capped at 100. `CONFLUENCE_TIMEOUT` is in seconds.
`CONFLUENCE_FILE_ROOTS` uses the platform path separator (`;` on Windows, `:`
on POSIX) and limits attachment and export paths. `CONFLUENCE_STATE_DIR`
defaults to `%LOCALAPPDATA%\confluence-reader` on Windows and
`~/.local/state/confluence-reader` elsewhere.

Writes are enabled by default after successful authenticated configuration. Set
`CONFLUENCE_ENABLE_WRITES=false` for a read-only deployment. The server never
accepts a token as a tool argument.

### Flavor detection

`CONFLUENCE_FLAVOR=auto` treats `.atlassian.net` hosts as Cloud and otherwise
uses the Data Center/Server v1-compatible path. For a Cloud tenant on a custom
domain, set `CONFLUENCE_FLAVOR=cloud` explicitly. An explicit flavor is also
useful when a reverse proxy makes host detection ambiguous.

## Verify

```powershell
$skill = Join-Path $HOME ".codex\skills\confluence-reader"
uv run --project $skill confluence doctor --json
```

The result includes the detected flavor, user lookup, space-list probe, write
state, and capabilities. A successful user lookup with a failed space-list
probe is not necessarily fatal: some accounts can read linked pages without
listing every space.

## Connect an MCP client

The command is `confluence-mcp`; it takes no arguments. Configure one process
per site. See [clients.md](clients.md) for Codex, Claude Desktop, Cursor, and
generic examples.

Minimal Codex configuration:

```toml
[mcp_servers.confluence]
command = "uv"
args = ["run", "--project", "C:\\path\\to\\confluence-reader", "confluence-mcp"]
startup_timeout_sec = 60
tool_timeout_sec = 120
```

## Common errors

- `AUTH_FAILED`: check email/token, PAT, auth mode, and token expiration.
- `PERMISSION_DENIED`: the account can authenticate but lacks the space or API
  scope required for the operation.
- `NOT_FOUND`: the id, title, URL, or endpoint is wrong, or access is restricted.
  Confluence sometimes returns `404` rather than `403` for restricted content.
- `CONFIG_ERROR`: missing `base_url`, an unreadable config file, or an invalid
  auth/flavor setting.
- `WRITES_DISABLED`: set `CONFLUENCE_ENABLE_WRITES=true` for an authenticated
  account, or stop using anonymous auth.
- TLS error: install the organization's root certificate or use a trusted
  `base_url`. `--insecure` is diagnostic only.

For destructive operations, a preview is not a failure. Show it to the user and
use the returned token only after explicit approval.
