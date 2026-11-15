"""Hybrid Overleaf MCP Server (Web Session + Git Bridge)."""

from __future__ import annotations

import json
from typing import Any, Literal

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # pragma: no cover - compatibility with mcp < 2.0
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]

from .config import OverleafConfig, get_credentials_file
from .git_client import OverleafGitClient, extract_project_id_from_ref
from .web_client import OverleafWebClient

mcp = MCPServer(
    "overleaf",
    instructions=(
        "Hybrid Overleaf MCP server supporting both Free/Paid Web Session authentication "
        "(browser cookies or OVERLEAF_COOKIE) and Paid/Institutional Overleaf Git Bridge "
        "(OVERLEAF_GIT_TOKEN). Use project IDs, full Overleaf project URLs, or exact project names."
    ),
)


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def _web() -> OverleafWebClient:
    return OverleafWebClient(OverleafConfig.load())


def _git() -> OverleafGitClient:
    return OverleafGitClient(OverleafConfig.load())


@mcp.tool()
def overleaf_auth_status(verify_live: bool = True) -> str:
    """Check Overleaf authentication status for both Web Session (cookies) and Git Bridge (token).

    Args:
        verify_live: When True, tests the Web Session against Overleaf to confirm login validity.
    """
    cfg = OverleafConfig.load()
    status: dict[str, Any] = {
        "host": cfg.host,
        "git_host": cfg.git_host,
        "credentials_file": str(get_credentials_file()),
        "cookie_configured": bool(cfg.cookie),
        "git_token_configured": bool(cfg.git_token),
    }

    if verify_live:
        try:
            client = OverleafWebClient(cfg)
            projects = client.list_projects()
            status["web_session"] = {
                "authenticated": True,
                "source": client._auth_source,
                "accessible_projects": len(projects),
            }
        except Exception as exc:
            status["web_session"] = {
                "authenticated": False,
                "error": str(exc),
                "how_to_fix": (
                    f"1) Log into https://{cfg.host} in Chrome/Firefox on this Mac, OR "
                    "2) Copy the 'overleaf_session2' cookie from browser DevTools -> Application -> Cookies "
                    "and call overleaf_configure_auth(cookie='overleaf_session2=...')."
                ),
            }

    if cfg.git_token:
        status["git_bridge"] = {
            "ready": True,
            "note": "Token is configured. Works on Overleaf Paid, Institutional, and Server Pro accounts.",
        }
    else:
        status["git_bridge"] = {
            "ready": False,
            "note": (
                "Optional (Paid/Institutional accounts): generate a Git Token in Overleaf Account Settings "
                "and call overleaf_configure_auth(git_token='olp_...')."
            ),
        }

    return _json(status)


@mcp.tool()
def overleaf_configure_auth(
    cookie: str | None = None,
    git_token: str | None = None,
    host: str | None = None,
    git_host: str | None = None,
) -> str:
    """Save Overleaf session cookie, Git Bridge token, or self-hosted host URL locally (mode 0600).

    Args:
        cookie: Either 'browser' (auto-read from Chrome/Firefox), a full Cookie header string,
            or the value of 'overleaf_session2' from browser DevTools.
        git_token: Overleaf Git integration token (from Overleaf Account Settings -> Git Integration).
        host: Overleaf web host (default: 'www.overleaf.com', or your self-hosted domain).
        git_host: Overleaf Git host (default: 'git.overleaf.com').
    """
    cfg = OverleafConfig.load()
    saved_path = cfg.save_updates(
        cookie=cookie,
        git_token=git_token,
        host=host,
        git_host=git_host,
    )
    updated = OverleafConfig.load()
    return _json(
        {
            "saved_to": str(saved_path),
            "host": updated.host,
            "git_host": updated.git_host,
            "cookie_configured": bool(updated.cookie),
            "git_token_configured": bool(updated.git_token),
        }
    )


@mcp.tool()
def overleaf_list_projects(
    include_archived: bool = False,
    include_trashed: bool = False,
) -> str:
    """List all Overleaf projects in the user's account with IDs, titles, and editor URLs.

    Args:
        include_archived: Include archived projects.
        include_trashed: Include trashed projects.
    """
    projects = _web().list_projects(
        include_archived=include_archived,
        include_trashed=include_trashed,
    )
    return _json({"count": len(projects), "projects": projects})


@mcp.tool()
def overleaf_create_project(
    project_name: str,
    source_path: str | None = None,
    template: Literal["none", "example"] = "none",
    ignore_patterns: list[str] | None = None,
) -> str:
    """Create a brand-new Overleaf project (blank, example, or uploaded from a local folder or .zip).

    Args:
        project_name: Title for the new Overleaf project.
        source_path: Optional local directory path or .zip archive path to populate the new project.
        template: When source_path is omitted, create 'none' (basic main.tex) or 'example'.
        ignore_patterns: Optional glob patterns to skip when zipping a local directory.
    """
    res = _web().create_project(
        project_name,
        source_path=source_path,
        template=template,
        ignore_patterns=ignore_patterns,
    )
    return _json(res)


@mcp.tool()
def overleaf_list_files(project: str) -> str:
    """List the complete folder and file tree inside an Overleaf project.

    Args:
        project: Overleaf 24-char project ID, full project URL, or exact project title.
    """
    return _json(_web().list_files(project))


@mcp.tool()
def overleaf_read_file(
    project: str,
    remote_path: str,
    save_to_path: str | None = None,
) -> str:
    """Read a text/LaTeX file (.tex, .bib, .sty, .cls) from Overleaf or download a binary file locally.

    Args:
        project: Overleaf project ID, project URL, or exact project title.
        remote_path: Path inside the Overleaf project (e.g. 'main.tex' or 'sections/intro.tex').
        save_to_path: Optional local file path to save raw bytes (required for binary files like PDFs/images).
    """
    return _json(_web().read_file(project, remote_path, save_to_path=save_to_path))


@mcp.tool()
def overleaf_write_file(
    project: str,
    remote_path: str,
    content: str,
) -> str:
    """Create or overwrite a text file (.tex, .bib, .sty, etc.) directly in an Overleaf project.

    Parent folders are created automatically if they do not exist.

    Args:
        project: Overleaf project ID, project URL, or exact project title.
        remote_path: Destination path inside Overleaf (e.g. 'main.tex' or 'sections/methods.tex').
        content: Full UTF-8 text content to write.
    """
    res = _web().upload_bytes(project, remote_path, content.encode("utf-8"))
    return _json(res)


@mcp.tool()
def overleaf_upload_file(
    project: str,
    local_path: str,
    remote_path: str | None = None,
) -> str:
    """Upload a single local file (.tex, .bib, .png, .pdf, .eps, etc.) to an Overleaf project.

    Parent folders in remote_path are created automatically.

    Args:
        project: Overleaf project ID, project URL, or exact project title.
        local_path: Path to the file on the local machine.
        remote_path: Target path in Overleaf (e.g. 'figures/arch.pdf'). Defaults to the local filename at root.
    """
    return _json(_web().upload_file(project, local_path, remote_path))


@mcp.tool()
def overleaf_upload_folder(
    project: str,
    local_dir: str,
    remote_dir: str = "",
    method: Literal["auto", "web", "git"] = "auto",
    commit_message: str = "Sync folder via Overleaf MCP Agent",
    delete_missing_in_git: bool = False,
    ignore_patterns: list[str] | None = None,
) -> str:
    """Upload an entire local directory (recursively) to an existing Overleaf project.

    Supports both Web Session upload (works on Free & Paid accounts) and Git Bridge push (Paid/Institutional).

    Args:
        project: Overleaf project ID, project URL, or exact project title.
        local_dir: Local folder whose contents will be uploaded.
        remote_dir: Optional target subdirectory in Overleaf (Web mode; e.g. 'figures'). Empty uploads to project root.
        method: 'web' (session cookie API), 'git' (Git Bridge commit & push), or 'auto' (prefers 'git' when token + root upload, else 'web').
        commit_message: Git commit message when using method='git'.
        delete_missing_in_git: In 'git' mode, remove remote files that no longer exist in local_dir.
        ignore_patterns: Optional filename/directory globs to ignore (defaults skip .git, .DS_Store, *.aux, etc.).
    """
    cfg = OverleafConfig.load()

    use_git = False
    if method == "git":
        use_git = True
    elif method == "auto" and cfg.git_token and not remote_dir.strip("/"):
        # Only auto-select git if we have a project ID/URL (or resolve it via web if possible)
        use_git = True

    if use_git:
        try:
            pid = extract_project_id_from_ref(project)
        except ValueError:
            pid = OverleafWebClient(cfg).resolve_project_id(project)
        res = OverleafGitClient(cfg).sync_folder_and_push(
            project_id=pid,
            source_dir=local_dir,
            commit_message=commit_message,
            delete_missing=delete_missing_in_git,
            ignore_patterns=ignore_patterns,
        )
        return _json(res)

    res = OverleafWebClient(cfg).upload_folder(
        project=project,
        local_dir=local_dir,
        remote_dir=remote_dir,
        ignore_patterns=ignore_patterns,
    )
    return _json(res)


@mcp.tool()
def overleaf_delete_path(project: str, remote_path: str) -> str:
    """Delete a file or folder from an Overleaf project.

    Args:
        project: Overleaf project ID, project URL, or exact project title.
        remote_path: File or folder path inside Overleaf to delete.
    """
    return _json(_web().delete_path(project, remote_path))


@mcp.tool()
def overleaf_pull_project(
    project: str,
    output_dir: str,
    method: Literal["zip", "git"] = "zip",
) -> str:
    """Download/pull all files from an Overleaf project into a local workspace folder.

    Args:
        project: Overleaf project ID, project URL, or exact project title.
        output_dir: Local directory where project files should be saved.
        method: 'zip' (works on Free & Paid accounts via Web Session) or 'git' (clones/pulls via Git Bridge).
    """
    if method == "git":
        cfg = OverleafConfig.load()
        try:
            pid = extract_project_id_from_ref(project)
        except ValueError:
            pid = OverleafWebClient(cfg).resolve_project_id(project)
        return _json(OverleafGitClient(cfg).clone_or_pull(pid, local_dir=output_dir))

    return _json(_web().pull_project_zip(project, output_dir))


@mcp.tool()
def overleaf_compile(
    project: str,
    save_pdf_path: str | None = None,
    save_log_path: str | None = None,
    draft: bool = False,
    root_doc_path: str | None = None,
) -> str:
    """Trigger LaTeX compilation on Overleaf, parse LaTeX errors/warnings, and optionally download the PDF.

    Args:
        project: Overleaf project ID, project URL, or exact project title.
        save_pdf_path: Optional local path to save the compiled output.pdf (e.g. './paper.pdf').
        save_log_path: Optional local path to save the full raw LaTeX output.log.
        draft: Compile in fast draft mode (skips rendering heavy figures).
        root_doc_path: Optional main .tex file path (e.g. 'main.tex') if overriding project default.
    """
    res = _web().compile_project(
        project,
        save_pdf_path=save_pdf_path,
        save_log_path=save_log_path,
        draft=draft,
        root_doc_path=root_doc_path,
    )
    return _json(res)


@mcp.tool()
def overleaf_git_sync(
    action: Literal["clone", "pull", "status", "push"],
    project_id: str,
    local_dir: str | None = None,
    commit_message: str = "Update via Overleaf MCP Agent",
    paths: list[str] | None = None,
) -> str:
    """Run direct Git Bridge operations (clone, pull, status, push) for Paid/Institutional Overleaf projects.

    Args:
        action: 'clone' or 'pull' (sync remote to local), 'status' (view diff & commits), or 'push' (stage, commit, push).
        project_id: 24-character Overleaf project ID or full Overleaf project URL.
        local_dir: Optional local checkout directory (defaults to ~/.cache/overleaf-mcp/repos/<project_id>).
        commit_message: Commit message when action='push'.
        paths: Optional specific relative paths to stage when action='push' (defaults to all changes).
    """
    git = _git()
    if action in ("clone", "pull"):
        return _json(git.clone_or_pull(project_id, local_dir=local_dir))
    if action == "status":
        return _json(git.status_and_diff(project_id, local_dir=local_dir))
    if action == "push":
        return _json(
            git.commit_and_push(
                project_id,
                local_dir=local_dir,
                commit_message=commit_message,
                paths=paths,
            )
        )
    raise ValueError(f"Unsupported git action: {action!r}")


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
