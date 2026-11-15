"""Overleaf Git Bridge client for paid, institutional, and Server Pro accounts."""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import OverleafConfig

_PROJECT_ID_RE = re.compile(r"([0-9a-f]{24})", re.IGNORECASE)


def extract_project_id_from_ref(project_ref: str) -> str:
    """Extract a 24-char hex Overleaf project ID from an ID or URL."""
    ref = project_ref.strip().rstrip("/")
    match = _PROJECT_ID_RE.search(ref)
    if match:
        return match.group(1)
    raise ValueError(
        f"Git Bridge requires a 24-character Overleaf project ID or URL, got: {project_ref!r}. "
        "Tip: Call overleaf_list_projects first if you only have the project title."
    )


class OverleafGitClient:
    """Executes authenticated Git operations against Overleaf's Git Bridge without storing secrets on disk."""

    def __init__(self, config: OverleafConfig):
        self.config = config

    def _require_token(self) -> str:
        if not self.config.git_token:
            raise RuntimeError(
                "No Overleaf Git Token configured. Generate one in Overleaf Account Settings -> "
                "Git Integration, then set OVERLEAF_GIT_TOKEN or call overleaf_configure_auth(git_token=...)."
            )
        return self.config.git_token

    def remote_url(self, project_id: str) -> str:
        pid = extract_project_id_from_ref(project_id)
        if self.config.git_host == "git.overleaf.com":
            return f"https://git.overleaf.com/{pid}"
        return f"https://{self.config.git_host}/git/{pid}"

    def _run_git(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        use_auth: bool = True,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["git"]
        if use_auth:
            token = self._require_token()
            # Overleaf Git Bridge uses HTTP Basic Auth with username 'git' and the token as password.
            # Passing http.extraHeader avoids writing the secret token into .git/config!
            b64 = base64.b64encode(f"git:{token}".encode("utf-8")).decode("ascii")
            cmd.extend(["-c", f"http.extraHeader=Authorization: Basic {b64}"])
        cmd.extend(args)

        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"

        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc

    def _sanitize_error(self, text: str) -> str:
        if self.config.git_token:
            text = text.replace(self.config.git_token, "***")
        return re.sub(r"Authorization:\s*Basic\s+[A-Za-z0-9+/=]+", "Authorization: Basic ***", text)

    def resolve_repo_dir(self, project_id: str, local_dir: str | None = None) -> Path:
        if local_dir:
            return Path(local_dir).expanduser().resolve()
        pid = extract_project_id_from_ref(project_id)
        return (self.config.git_cache_dir / pid).resolve()

    def clone_or_pull(self, project_id: str, local_dir: str | None = None) -> dict[str, Any]:
        """Clone an Overleaf Git repository locally, or pull latest changes if already cloned."""
        pid = extract_project_id_from_ref(project_id)
        target = self.resolve_repo_dir(pid, local_dir)
        url = self.remote_url(pid)

        if (target / ".git").exists():
            proc = self._run_git(["pull", "--rebase", "origin", "master"], cwd=target)
            if proc.returncode != 0:
                # Some newer repos may use main or default HEAD branch
                proc = self._run_git(["pull", "--rebase"], cwd=target)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"git pull failed in {target}: {self._sanitize_error(proc.stderr or proc.stdout)}"
                )
            action = "pulled"
            output = (proc.stdout or proc.stderr).strip()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            proc = self._run_git(["clone", url, str(target)])
            if proc.returncode != 0:
                raise RuntimeError(
                    f"git clone failed for {url}: {self._sanitize_error(proc.stderr or proc.stdout)}"
                )
            action = "cloned"
            output = (proc.stdout or proc.stderr).strip()

        files = [
            str(p.relative_to(target))
            for p in sorted(target.rglob("*"))
            if p.is_file() and ".git" not in p.parts
        ]
        return {
            "action": action,
            "project_id": pid,
            "remote_url": url,
            "local_dir": str(target),
            "file_count": len(files),
            "files": files[:200],
            "git_output": self._sanitize_error(output),
        }

    def status_and_diff(self, project_id: str, local_dir: str | None = None) -> dict[str, Any]:
        target = self.resolve_repo_dir(project_id, local_dir)
        if not (target / ".git").exists():
            raise FileNotFoundError(
                f"No Git repository found at {target}. Run action='clone' first."
            )
        status_proc = self._run_git(["status", "--short"], cwd=target, use_auth=False)
        diff_proc = self._run_git(["diff", "--stat"], cwd=target, use_auth=False)
        log_proc = self._run_git(
            ["log", "-n", "5", "--oneline"], cwd=target, use_auth=False
        )
        return {
            "local_dir": str(target),
            "status": status_proc.stdout.strip(),
            "diff_stat": diff_proc.stdout.strip(),
            "recent_commits": log_proc.stdout.strip().splitlines(),
        }

    def sync_folder_and_push(
        self,
        project_id: str,
        source_dir: str,
        commit_message: str = "Update from Overleaf MCP Agent",
        delete_missing: bool = False,
        ignore_patterns: list[str] | None = None,
    ) -> dict[str, Any]:
        """Copy files from any local folder into a cached Overleaf Git checkout, commit, and push."""
        src = Path(source_dir).expanduser().resolve()
        if not src.is_dir():
            raise NotADirectoryError(f"Source directory does not exist: {src}")

        # If source_dir itself is already the cloned Overleaf git repo, push directly in place!
        pid = extract_project_id_from_ref(project_id)
        if (src / ".git").exists():
            remote_proc = self._run_git(
                ["remote", "get-url", "origin"], cwd=src, use_auth=False
            )
            if pid in remote_proc.stdout:
                return self.commit_and_push(
                    project_id=pid,
                    local_dir=str(src),
                    commit_message=commit_message,
                )

        checkout_info = self.clone_or_pull(pid)
        repo_dir = Path(checkout_info["local_dir"])

        ignored = set(
            ignore_patterns
            or [
                ".git",
                ".venv",
                "venv",
                "__pycache__",
                ".DS_Store",
                "node_modules",
            ]
        )

        copied_files: list[str] = []
        source_rel_paths: set[Path] = set()
        for item in src.rglob("*"):
            rel = item.relative_to(src)
            if any(part in ignored for part in rel.parts):
                continue
            if item.is_file():
                source_rel_paths.add(rel)
                dest = repo_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest)
                copied_files.append(str(rel))

        removed_files: list[str] = []
        if delete_missing:
            for existing in repo_dir.rglob("*"):
                if not existing.is_file() or ".git" in existing.parts:
                    continue
                rel = existing.relative_to(repo_dir)
                if rel not in source_rel_paths:
                    existing.unlink()
                    removed_files.append(str(rel))

        push_result = self.commit_and_push(
            project_id=pid,
            local_dir=str(repo_dir),
            commit_message=commit_message,
        )
        push_result["copied_files_count"] = len(copied_files)
        push_result["removed_files_count"] = len(removed_files)
        return push_result

    def commit_and_push(
        self,
        project_id: str,
        local_dir: str | None = None,
        commit_message: str = "Update via Overleaf MCP Agent",
        paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Stage changes, commit, and push to Overleaf's Git Bridge."""
        pid = extract_project_id_from_ref(project_id)
        target = self.resolve_repo_dir(pid, local_dir)
        if not (target / ".git").exists():
            raise FileNotFoundError(
                f"No Git repository at {target}. Run overleaf_git_sync(action='clone', ...) first."
            )

        # Ensure git user.name / user.email exist locally if global git config is unset
        name_check = self._run_git(["config", "user.name"], cwd=target, use_auth=False)
        if not name_check.stdout.strip():
            self._run_git(
                ["config", "user.name", "Overleaf MCP Agent"],
                cwd=target,
                use_auth=False,
            )
        email_check = self._run_git(["config", "user.email"], cwd=target, use_auth=False)
        if not email_check.stdout.strip():
            self._run_git(
                ["config", "user.email", "agent@overleaf-mcp.local"],
                cwd=target,
                use_auth=False,
            )

        add_args = ["add", "--"] + paths if paths else ["add", "-A"]
        add_proc = self._run_git(add_args, cwd=target, use_auth=False)
        if add_proc.returncode != 0:
            raise RuntimeError(f"git add failed: {add_proc.stderr}")

        status_proc = self._run_git(
            ["status", "--porcelain"], cwd=target, use_auth=False
        )
        if not status_proc.stdout.strip():
            return {
                "pushed": False,
                "message": "Working tree clean — nothing new to commit or push.",
                "project_id": pid,
                "local_dir": str(target),
                "editor_url": f"https://{self.config.host}/project/{pid}",
            }

        commit_proc = self._run_git(
            ["commit", "-m", commit_message], cwd=target, use_auth=False
        )
        if commit_proc.returncode != 0:
            raise RuntimeError(
                f"git commit failed: {commit_proc.stderr or commit_proc.stdout}"
            )

        push_proc = self._run_git(["push", "origin", "HEAD"], cwd=target, use_auth=True)
        if push_proc.returncode != 0:
            # Try pulling with rebase once in case Overleaf had concurrent edits
            pull_proc = self._run_git(
                ["pull", "--rebase", "origin", "HEAD"], cwd=target, use_auth=True
            )
            if pull_proc.returncode == 0:
                push_proc = self._run_git(
                    ["push", "origin", "HEAD"], cwd=target, use_auth=True
                )
        if push_proc.returncode != 0:
            raise RuntimeError(
                f"git push failed: {self._sanitize_error(push_proc.stderr or push_proc.stdout)}"
            )

        rev_proc = self._run_git(
            ["rev-parse", "--short", "HEAD"], cwd=target, use_auth=False
        )
        return {
            "pushed": True,
            "commit": rev_proc.stdout.strip(),
            "commit_message": commit_message,
            "project_id": pid,
            "local_dir": str(target),
            "editor_url": f"https://{self.config.host}/project/{pid}",
            "git_output": self._sanitize_error(
                (push_proc.stdout or push_proc.stderr).strip()
            ),
        }
