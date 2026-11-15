# OverleafMCP (`overleaf-mcp`)

A hybrid **Model Context Protocol (MCP)** server in Python that lets AI coding agents (**Antigravity**, **OpenAI Codex**, **Claude Code**, **Claude Desktop**, **Cursor**, **Windsurf**, etc.) upload LaTeX files and folders, create brand-new Overleaf projects, pull remote sources, sync via Git Bridge, and trigger remote PDF compilation with structured LaTeX error log diagnostics.

Unlike Git-only Overleaf MCP servers that require a paid Overleaf subscription and manual project IDs, **OverleafMCP** supports **both**:
1. **Web Session Authentication (Free, Paid, Institutional & Self-Hosted):** Auto-detects your active Chrome/Firefox Overleaf login or uses an `overleaf_session2` cookie — enabling project listing, new project creation, binary figure/folder uploads, and remote PDF compilation even on **Free Overleaf accounts**.
2. **Overleaf Git Bridge (Paid, Institutional & Server Pro):** Uses your permanent Overleaf Git Token (`olp_...`) for versioned multi-file commits, diffs, and rebases without ever storing plaintext tokens inside `.git/config`.

---

## Quick Start (Zero-Clone via `uvx`)

Requires [uv](https://docs.astral.sh/uv/) (`brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`).

### 1. OpenAI Codex (CLI & Codex App)

Run the one-liner CLI command:

```bash
codex mcp add overleaf -- uvx --from git+https://github.com/ajmainapon/OverleafMCP overleaf-mcp
```

Or add this TOML block to `~/.codex/config.toml` (note: Codex uses **TOML**, not JSON):

```toml
[mcp_servers.overleaf]
command = "uvx"
args = ["--from", "git+https://github.com/ajmainapon/OverleafMCP", "overleaf-mcp"]
startup_timeout_sec = 30.0

# Optional environment variables:
# [mcp_servers.overleaf.env]
# OVERLEAF_COOKIE = "overleaf_session2=s%3A..."
# OVERLEAF_GIT_TOKEN = "olp_..."
# OVERLEAF_HOST = "www.overleaf.com"
```

### 2. Claude Code CLI

```bash
claude mcp add overleaf -- uvx --from git+https://github.com/ajmainapon/OverleafMCP overleaf-mcp
```

### 3. Antigravity, Claude Desktop & Cursor (JSON Config)

Add to your client's MCP JSON configuration file:
- **Antigravity:** `~/.gemini/config/mcp_config.json`
- **Claude Desktop (macOS):** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Claude Desktop (Windows):** `%APPDATA%\Claude\claude_desktop_config.json`
- **Cursor:** `~/.cursor/mcp.json`

```json
{
  "mcpServers": {
    "overleaf": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/ajmainapon/OverleafMCP",
        "overleaf-mcp"
      ]
    }
  }
}
```

*(Optional)* Pass credentials via `env` instead of configuring interactively through the agent:

```json
{
  "mcpServers": {
    "overleaf": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/ajmainapon/OverleafMCP",
        "overleaf-mcp"
      ],
      "env": {
        "OVERLEAF_COOKIE": "overleaf_session2=s%3A...",
        "OVERLEAF_GIT_TOKEN": "olp_...",
        "OVERLEAF_HOST": "www.overleaf.com"
      }
    }
  }
}
```

---

## Local Clone Installation (Fastest Startup / Development)

```bash
git clone https://github.com/ajmainapon/OverleafMCP.git
cd OverleafMCP
uv sync
```

Point your MCP client directly at the virtualenv binary:

**Codex (`~/.codex/config.toml`):**
```toml
[mcp_servers.overleaf]
command = "/absolute/path/to/OverleafMCP/.venv/bin/overleaf-mcp"
args = []
```

**Antigravity / Claude Desktop / Cursor (`mcp_config.json`):**
```json
{
  "mcpServers": {
    "overleaf": {
      "command": "/absolute/path/to/OverleafMCP/.venv/bin/overleaf-mcp",
      "args": []
    }
  }
}
```

---

## Authentication Guide

### Option A: Browser Auto-Login (Free & Paid Accounts)
1. Sign into [overleaf.com](https://www.overleaf.com) in **Chrome** or **Firefox**.
2. Ask your agent: *"Check my Overleaf authentication status and list my projects."*
3. *(macOS note)* Click **Always Allow** if Keychain prompts for browser cookie access on the first run.

### Option B: Session Cookie (Safari / Arc / Brave / Headless, Free & Paid)
1. Open [overleaf.com](https://www.overleaf.com) -> Browser DevTools (`Cmd+Option+I` / `F12`) -> **Application** (or **Storage**) -> **Cookies** -> `https://www.overleaf.com`.
2. Copy the value of **`overleaf_session2`** (starts with `s%3A...`).
3. Tell your agent:
   > *"Save my Overleaf cookie `s%3A...`"*
   *(Saved locally with `0600` permissions at `~/.config/overleaf-mcp/credentials.json`.)*

### Option C: Overleaf Git Bridge Token (Paid / Institutional / Server Pro)
1. Go to [Overleaf Account Settings](https://www.overleaf.com/user/settings) -> **Git Integration** -> **Create Token**.
2. Tell your agent:
   > *"Save my Overleaf Git token `olp_...`"*

---

## Available MCP Tools (13 Tools)

All project-scoped tools accept a **24-character Project ID**, a **full Overleaf project URL**, or an **exact Project Title**.

| Tool | Auth Mode | Description |
| :--- | :--- | :--- |
| `overleaf_auth_status` | Both | Check Web Session and Git Bridge authentication status (with optional live verification). |
| `overleaf_configure_auth` | Both | Save `cookie`, `git_token`, `host`, or `git_host` to `~/.config/overleaf-mcp/credentials.json` (`0600`). |
| `overleaf_list_projects` | Web | List all projects in your Overleaf account (IDs, titles, access levels, editor URLs). |
| `overleaf_create_project` | Web | Create a brand-new project (blank, example, from a `.zip`, or auto-zipped from a local folder). |
| `overleaf_list_files` | Web | List the complete folder and file tree inside an Overleaf project. |
| `overleaf_read_file` | Web | Read remote `.tex`/`.bib`/`.sty` text files or download binary files locally. |
| `overleaf_write_file` | Web | Create or overwrite a text file directly in Overleaf (auto-creates parent folders). |
| `overleaf_upload_file` | Web | Upload any local file (`.tex`, `.bib`, `.pdf`, `.png`, etc.) to Overleaf (auto-creates parent folders). |
| `overleaf_upload_folder` | Hybrid (`auto`/`web`/`git`) | Recursively upload a local directory to Overleaf while ignoring `.git`, `.DS_Store`, `*.aux`, etc. |
| `overleaf_delete_path` | Web | Delete a file or folder inside an Overleaf project. |
| `overleaf_pull_project` | Hybrid (`zip`/`git`) | Download & extract an entire Overleaf project locally via `.zip` or `git clone`/`pull`. |
| `overleaf_compile` | Web | Trigger remote LaTeX compilation, parse `! LaTeX Error` lines & warnings from `output.log`, and download `output.pdf`. |
| `overleaf_git_sync` | Git | Run `clone`, `pull`, `status`, or `push` directly against Overleaf's Git Bridge. |

---

## Example Prompts

- *"Create a new Overleaf project named `NeurIPS 2026 Submission` from `./paper` and compile the PDF."*
- *"Upload `./figures/ablation.pdf` to `figures/ablation.pdf` in my Overleaf project `NeurIPS 2026 Submission`."*
- *"Compile `NeurIPS 2026 Submission` on Overleaf, inspect any LaTeX errors in the build log, fix them, and download the compiled PDF to `./paper.pdf`."*
- *"Pull my Overleaf project `PhD Thesis` into `./thesis-local`."*

---

## Security & Reliability Notes

- **Local-only secret storage:** Credentials configured via `overleaf_configure_auth` are stored at `~/.config/overleaf-mcp/credentials.json` with strict `0600` (`rw-------`) user-only permissions.
- **Zero plaintext Git tokens in `.git/config`:** Git Bridge operations inject transient `http.extraHeader` Basic Auth headers per subprocess call and scrub tokens from error output.
- **Strict Stdio isolation:** Browser keyring/cookie loaders redirect `stdout` to `stderr` so third-party warnings never corrupt the JSON-RPC stream in strict clients like Codex.
- **Socket & Zip-Slip safety:** Socket.IO metadata connections close deterministically in `finally` blocks, and archive extraction enforces `Path.is_relative_to()` containment.

---

## Running Tests

```bash
uv sync
.venv/bin/python tests/test_server.py
```

## License

[MIT](./LICENSE)
