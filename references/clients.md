# MCP client configuration

Every client starts one local `stdio` MCP process named `confluence` for one
Confluence site. The process does not open a network port. The examples below
use a checkout at `C:\tools\confluence-reader`; replace it with your absolute
path.

The MCP server is designed for the `v2.1.0` release. If you install from the
repository, check out or install `v2.1.0` explicitly when you need a fixed
version:

```powershell
git clone --branch v2.1.0 https://github.com/LipsLee/confluence-reader.git C:\tools\confluence-reader
uv sync --project C:\tools\confluence-reader
```

Using a local checkout means `uv run --project` always starts the code in that
checkout. Do not point two clients at different uncommitted versions by
accident.

## Codex

Add this to `~/.codex/config.toml` (use an absolute path):

```toml
[mcp_servers.confluence]
command = "uv"
args = [
  "run",
  "--project",
  "C:\\tools\\confluence-reader",
  "confluence-mcp"
]
startup_timeout_sec = 60
tool_timeout_sec = 120
```

If the process must receive credentials through the environment:

```toml
[mcp_servers.confluence.env]
CONFLUENCE_CONFIG = "C:\\Users\\YOUR_NAME\\.config\\confluence-reader\\config.json"
```

Restart Codex or start a new turn after changing the configuration. The
server caches its first configuration load; environment changes require a new
process.

## Claude Desktop

Use the following shape in Claude Desktop's local MCP configuration:

```json
{
  "mcpServers": {
    "confluence": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "C:\\tools\\confluence-reader",
        "confluence-mcp"
      ],
      "env": {
        "CONFLUENCE_CONFIG": "C:\\Users\\YOUR_NAME\\.config\\confluence-reader\\config.json"
      }
    }
  }
}
```

On macOS or Linux, use POSIX paths and `command: "uv"` with the same argument
shape. The config file is the recommended place for secrets; do not put a real
token in a client configuration that is committed to source control.

## Cursor

Cursor accepts the same local MCP server object in its MCP settings. Keep the
server name `confluence`:

```json
{
  "mcpServers": {
    "confluence": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "C:\\tools\\confluence-reader",
        "confluence-mcp"
      ]
    }
  }
}
```

## Generic stdio client

The protocol is JSON-RPC over standard input/output. Spawn:

```text
uv run --project <absolute-repo> confluence-mcp
```

No command-line arguments are required. Do not add a port, URL, token, or
Authorization argument. A client that supports MCP tool annotations should see
the read-only and destructive hints described in `tools.md`.

## Multiple sites

One server process is bound to one Confluence site. To use several sites, start
one MCP server per site and give each a distinct client-side name, for example
`confluence-eng` and `confluence-ops`. Each process needs its own
`CONFLUENCE_CONFIG` pointing to a different config file. Do not reuse a state
directory for unrelated sites unless you intentionally want one audit log.

## Troubleshooting

### The server exits immediately

Run the command from a terminal:

```powershell
uv run --project C:\tools\confluence-reader confluence doctor --json
```

Then run the MCP entry point without a client and check stderr:

```powershell
uv run --project C:\tools\confluence-reader confluence-mcp
```

It should wait for JSON-RPC input. Press `Ctrl+C` to stop it.

### Tools are missing

Check that the client points at the directory containing `pyproject.toml`, not
only at `src`. Confirm `uv --version` works and run `uv sync --project <repo>`
once. A missing `mcp` dependency produces a startup error rather than a partial
tool list.

### Authentication works in the CLI but not in the client

Clients that launch processes with a restricted environment may not inherit
your shell's variables. Set `CONFLUENCE_CONFIG` in the client's `env` block, or
set the required `CONFLUENCE_*` variables there. Never put a real token in a
shared team configuration.

### A write timed out

Do not immediately repeat it. Use `confluence_status` and `get_page` to inspect
the current state. A `create_page` or `add_comment` call may have succeeded even
though the response was lost.
