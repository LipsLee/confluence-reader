# Confluence Reader Setup

The reader requires Python 3.10+ and uses only the standard library. It supports
Confluence Cloud and Confluence Data Center/Server through the Confluence REST
API.

## Authentication

### Cloud

Set:

```text
CONFLUENCE_BASE_URL=https://your-site.atlassian.net/wiki
CONFLUENCE_EMAIL=you@example.com
CONFLUENCE_API_TOKEN=<Atlassian API token>
```

Create an API token at <https://id.atlassian.com/manage-profile/security/api-tokens>.
The token inherits the account's Confluence permissions.

### Data Center or Server

Prefer a personal access token:

```text
CONFLUENCE_BASE_URL=https://confluence.example.com/confluence
CONFLUENCE_PERSONAL_ACCESS_TOKEN=<personal access token>
CONFLUENCE_AUTH=bearer
```

Older deployments that still use basic authentication can use
`CONFLUENCE_EMAIL` and `CONFLUENCE_API_TOKEN` with `CONFLUENCE_AUTH=basic`.
The value in `CONFLUENCE_API_TOKEN` may be the account password for that mode.

### Public content

For an anonymously readable site:

```text
CONFLUENCE_AUTH=none
```

## Optional environment variables

```text
CONFLUENCE_VERIFY_SSL=true|false
CONFLUENCE_TIMEOUT=30
CONFLUENCE_PAGE_SIZE=100
CONFLUENCE_CONFIG=<path to a JSON config file>
HTTP_PROXY / HTTPS_PROXY / NO_PROXY
```

`CONFLUENCE_PAGE_SIZE` is capped at 100 by the Confluence API.

## Persistent config file

Environment variables take precedence over the config file. The reader checks:

1. The path in `CONFLUENCE_CONFIG`
2. `~/.config/confluence-reader/config.json`
3. `%APPDATA%\confluence-reader\config.json` on Windows
4. `~/.confluence-reader.json`

Example:

```json
{
  "base_url": "https://your-site.atlassian.net/wiki",
  "email": "you@example.com",
  "api_token": "<Atlassian API token>",
  "verify_ssl": true,
  "timeout": 30
}
```

For a Data Center personal access token, use:

```json
{
  "base_url": "https://confluence.example.com/confluence",
  "personal_access_token": "PAT...",
  "auth": "bearer"
}
```

Treat this file as a secret. On Windows, restrict it to the current account:

```powershell
$dir = Join-Path $HOME ".config\confluence-reader"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$file = Join-Path $dir "config.json"
icacls $file /inheritance:r /grant:r "$env:USERNAME:(R,W)"
```

Do not paste a real token into a chat message or commit it to a repository.

## Verify configuration

```powershell
$skill = Join-Path $HOME ".codex\skills\confluence-reader"
python "$skill\scripts\confluence.py" doctor
```

Use `doctor --json` for a machine-readable result. It reports the display name
of the authenticated user and whether a sample space can be listed. A successful
user lookup but failed space-list check usually means the account can read pages
through direct links but lacks global space-list permission.

## Common errors

- `401`: the email/token pair is wrong, the token expired, or the auth mode is
  incorrect.
- `403`: the account can authenticate but cannot view the requested page or
  endpoint.
- `404`: the page id, space key, title, or endpoint is wrong; some restricted
  pages intentionally return `404` instead of `403`.
- `TLS error`: install the organization's root certificate or use a trusted
  `CONFLUENCE_BASE_URL`. `--insecure` exists only as a last-resort diagnostic.

