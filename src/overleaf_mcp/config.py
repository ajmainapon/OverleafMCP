"""Configuration, credential storage, and ignore-pattern helpers for Overleaf MCP Server."""

from __future__ import annotations

import fnmatch
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_IGNORE_PATTERNS: tuple[str, ...] = (
    ".git",
    ".svn",
    ".hg",
    ".DS_Store",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    "*.aux",
    "*.fdb_latexmk",
    "*.fls",
    "*.synctex.gz",
    "*.out",
    "*.toc",
    "*.lof",
    "*.lot",
)


def should_ignore(
    rel_parts: tuple[str, ...],
    ignore_patterns: tuple[str, ...] = DEFAULT_IGNORE_PATTERNS,
) -> bool:
    """Return True if any path component matches any fnmatch glob pattern."""
    for part in rel_parts:
        for pat in ignore_patterns:
            if fnmatch.fnmatch(part, pat):
                return True
    return False


def get_config_dir() -> Path:
    return Path(
        os.environ.get("OVERLEAF_MCP_CONFIG_DIR", "~/.config/overleaf-mcp")
    ).expanduser()


def get_credentials_file() -> Path:
    return get_config_dir() / "credentials.json"


def get_default_git_cache_dir() -> Path:
    return Path(
        os.environ.get("OVERLEAF_MCP_GIT_DIR", "~/.cache/overleaf-mcp/repos")
    ).expanduser()


def _normalize_host(raw_host: str) -> str:
    host = raw_host.strip()
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix) :]
    return host.rstrip("/")


def _derive_git_host(web_host: str) -> str:
    if web_host in ("www.overleaf.com", "overleaf.com"):
        return "git.overleaf.com"
    return web_host


@dataclass
class OverleafConfig:
    """Resolved runtime configuration from environment variables and local config."""

    host: str = "www.overleaf.com"
    git_host: str = "git.overleaf.com"
    cookie: str | None = None
    git_token: str | None = None
    git_cache_dir: Path = field(default_factory=get_default_git_cache_dir)

    @classmethod
    def load(cls) -> OverleafConfig:
        creds_file = get_credentials_file()
        saved: dict[str, Any] = {}
        if creds_file.exists():
            try:
                saved = json.loads(creds_file.read_text(encoding="utf-8"))
            except Exception:
                saved = {}

        raw_host = (
            os.environ.get("OVERLEAF_HOST")
            or saved.get("host")
            or "www.overleaf.com"
        )
        host = _normalize_host(raw_host)

        raw_git_host = (
            os.environ.get("OVERLEAF_GIT_HOST")
            or saved.get("git_host")
            or _derive_git_host(host)
        )
        git_host = _normalize_host(raw_git_host)

        cookie = (
            os.environ.get("OVERLEAF_COOKIE")
            or os.environ.get("OVERLEAF_SESSION")
            or saved.get("cookie")
            or None
        )
        if cookie:
            cookie = cookie.strip()

        git_token = (
            os.environ.get("OVERLEAF_GIT_TOKEN")
            or saved.get("git_token")
            or None
        )
        if git_token:
            git_token = git_token.strip()

        git_dir_raw = os.environ.get("OVERLEAF_MCP_GIT_DIR") or saved.get("git_cache_dir")
        git_cache_dir = (
            Path(git_dir_raw).expanduser()
            if git_dir_raw
            else get_default_git_cache_dir()
        )

        return cls(
            host=host,
            git_host=git_host,
            cookie=cookie,
            git_token=git_token,
            git_cache_dir=git_cache_dir,
        )

    def save_updates(
        self,
        *,
        cookie: str | None = None,
        git_token: str | None = None,
        host: str | None = None,
        git_host: str | None = None,
    ) -> Path:
        """Persist non-None credential updates to <config_dir>/credentials.json (mode 0600)."""
        cfg_dir = get_config_dir()
        creds_file = get_credentials_file()

        cfg_dir.mkdir(parents=True, exist_ok=True)
        try:
            cfg_dir.chmod(stat.S_IRWXU)
        except OSError:
            pass

        saved: dict[str, Any] = {}
        if creds_file.exists():
            try:
                saved = json.loads(creds_file.read_text(encoding="utf-8"))
            except Exception:
                saved = {}

        if cookie is not None:
            saved["cookie"] = cookie.strip() if cookie.strip() else None
        if git_token is not None:
            saved["git_token"] = git_token.strip() if git_token.strip() else None
        if host is not None and host.strip():
            saved["host"] = _normalize_host(host)
        if git_host is not None and git_host.strip():
            saved["git_host"] = _normalize_host(git_host)

        creds_file.write_text(
            json.dumps(saved, indent=2) + "\n", encoding="utf-8"
        )
        try:
            creds_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return creds_file
