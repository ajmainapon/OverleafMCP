"""Overleaf Web Session client (works on Free, Paid, Institutional, and Self-Hosted accounts)."""

from __future__ import annotations

import contextlib
import http.cookiejar as cookielib
import io
import json
import posixpath
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import browser_cookie3
import pyoverleaf
import requests
from bs4 import BeautifulSoup
from pyoverleaf._webapi import ProjectFile, ProjectFolder

from .config import DEFAULT_IGNORE_PATTERNS, OverleafConfig, should_ignore

_PROJECT_ID_RE = re.compile(r"^[0-9a-f]{24}$", re.IGNORECASE)
_PROJECT_URL_RE = re.compile(r"/project/([0-9a-f]{24})", re.IGNORECASE)


def _parse_cookie_string(raw: str) -> dict[str, str]:
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw[len("cookie:") :].strip()
    if "=" not in raw:
        # Bare overleaf_session2 value pasted directly from browser DevTools
        return {"overleaf_session2": raw}
    cookies: dict[str, str] = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        cookies[k.strip()] = v.strip()
    return cookies


def _safe_json(resp: requests.Response, context: str) -> dict[str, Any]:
    """Parse a JSON response safely, surfacing clear errors on HTML login redirects or 502 pages."""
    if "/login" in resp.url:
        raise RuntimeError(
            f"{context} redirected to login ({resp.url}). Your Overleaf session cookie has expired."
        )
    try:
        data = resp.json()
    except ValueError as exc:
        snippet = resp.text[:300].strip().replace("\n", " ")
        raise RuntimeError(
            f"{context} returned non-JSON response (HTTP {resp.status_code}): {snippet}"
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{context} returned unexpected JSON payload: {type(data).__name__}")
    return data


def parse_latex_log(log_text: str, max_items: int = 25) -> dict[str, Any]:
    """Extract concise, actionable LaTeX errors and warnings from a raw TeX build log."""
    lines = log_text.splitlines()
    errors: list[str] = []
    warnings: list[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("! "):
            snippet = [line]
            for j in range(i + 1, min(i + 6, len(lines))):
                snippet.append(lines[j])
                if lines[j].lstrip().startswith("l."):
                    break
            errors.append("\n".join(snippet))
        elif (
            "LaTeX Warning:" in line
            or "Package natbib Warning:" in line
            or "Package biblatex Warning:" in line
            or "Undefined control sequence" in line
        ):
            warnings.append(line.strip())
        i += 1

    return {
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors[:max_items],
        "warnings": warnings[:max_items],
        "log_tail": "\n".join(lines[-40:]) if lines else "",
    }


def safe_extract_zip(zip_bytes: bytes, dest: Path) -> list[str]:
    """Extract a zip archive into dest while blocking zip-slip path traversal (including prefix siblings)."""
    dest_resolved = dest.expanduser().resolve()
    dest_resolved.mkdir(parents=True, exist_ok=True)

    extracted: list[str] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        for member in zf.infolist():
            member_path = (dest_resolved / member.filename).resolve()
            if not member_path.is_relative_to(dest_resolved):
                continue
            if member_path == dest_resolved or member.is_dir():
                member_path.mkdir(parents=True, exist_ok=True)
            else:
                member_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member, "r") as src_f:
                    member_path.write_bytes(src_f.read())
                extracted.append(member_path.relative_to(dest_resolved).as_posix())
    return extracted


class OverleafWebClient:
    """High-level Overleaf Web API wrapper built on pyoverleaf + Overleaf web endpoints."""

    def __init__(self, config: OverleafConfig, timeout: int = 60):
        self.config = config
        self.timeout = timeout
        self._api: pyoverleaf.Api | None = None
        self._auth_source: str = "uninitialized"

    @property
    def host(self) -> str:
        return self.config.host

    def _cookie_domain_suffix(self) -> str:
        base = self.host.removeprefix("www.")
        return f".{base}" if "." in base else base

    def get_api(self) -> pyoverleaf.Api:
        if self._api is not None:
            return self._api

        api = pyoverleaf.Api(timeout=self.timeout, host=self.host)
        dot_domain = self._cookie_domain_suffix()
        bare_domain = dot_domain.lstrip(".")

        if self.config.cookie and self.config.cookie.lower() != "browser":
            cookie_dict = _parse_cookie_string(self.config.cookie)
            jar = cookielib.CookieJar()
            for name, value in cookie_dict.items():
                jar.set_cookie(
                    requests.cookies.create_cookie(name, value, domain=dot_domain)
                )
            api._cookies = jar
            api._session_initialized = True
            self._auth_source = "configured_cookie"
        else:
            try:
                # Redirect stdout -> stderr so keyring/browser backends never corrupt MCP stdio JSON-RPC
                with contextlib.redirect_stdout(sys.stderr):
                    raw_jar = browser_cookie3.load()
            except Exception as exc:
                raise RuntimeError(
                    f"Could not read local browser cookies ({exc}). "
                    "Either log into Overleaf in Chrome/Firefox and allow Keychain access, "
                    "or pass your 'overleaf_session2' cookie via overleaf_configure_auth(cookie=...)."
                ) from exc

            jar = cookielib.CookieJar()
            matched = 0
            for c in raw_jar:
                cdom = (c.domain or "").lstrip(".")
                if cdom == bare_domain or cdom.endswith(f".{bare_domain}"):
                    # Normalize domain to dot_domain so both requests and pyoverleaf._open_socket match
                    jar.set_cookie(
                        requests.cookies.create_cookie(
                            c.name, c.value, domain=dot_domain, path=c.path or "/"
                        )
                    )
                    matched += 1

            if matched == 0:
                raise RuntimeError(
                    f"No Overleaf cookies found for {bare_domain} in local browsers. "
                    f"Log into https://{self.host} in Chrome or Firefox, or set OVERLEAF_COOKIE / "
                    "call overleaf_configure_auth(cookie='overleaf_session2=...')."
                )
            api._cookies = jar
            api._session_initialized = True
            self._auth_source = "browser_cookies"

        self._api = api
        return api

    def _session(self) -> requests.Session:
        api = self.get_api()
        return api._get_session()

    def _get_project_root_folder(self, project_id: str) -> ProjectFolder:
        """Fetch project root folder via Socket.IO while guaranteeing socket.close() (fixes pyoverleaf leak)."""
        api = self.get_api()
        socket = api._open_socket(project_id)
        try:
            while True:
                line = socket.recv()
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                if line.startswith("7:"):
                    raise RuntimeError(
                        f"Unauthorized or rate-limited fetching files for project {project_id} ({line[:80]}). "
                        "Verify project permissions and session cookie."
                    )
                if line.startswith("5:"):
                    break
            payload = json.loads(line[len("5:") :].lstrip(":"))
            if payload.get("name") != "joinProjectResponse":
                raise RuntimeError(f"Unexpected Socket.IO event: {payload.get('name')}")
            project_data = payload["args"][0]["project"]
            return ProjectFolder.from_data(project_data["rootFolder"][0])
        finally:
            try:
                socket.close()
            except Exception:
                pass

    def _fetch_dashboard_html_and_csrf(self) -> tuple[BeautifulSoup, str]:
        session = self._session()
        resp = session.get(
            f"https://{self.host}/project",
            timeout=self.timeout,
            allow_redirects=True,
        )
        resp.raise_for_status()
        if "/login" in resp.url:
            raise RuntimeError(
                f"Overleaf session is not authenticated (redirected to {resp.url}). "
                f"Please refresh your login at https://{self.host} in your browser or update "
                "your session cookie with overleaf_configure_auth(cookie=...)."
            )
        soup = BeautifulSoup(resp.content, features="html.parser")
        csrf_meta = soup.find("meta", {"name": "ol-csrfToken"})
        if csrf_meta is None or not csrf_meta.get("content"):
            raise RuntimeError(
                "Authenticated page did not contain 'ol-csrfToken'. "
                "Your Overleaf session cookie may have expired."
            )
        return soup, str(csrf_meta.get("content"))

    def _get_project_csrf(self, project_id: str) -> str:
        api = self.get_api()
        return api._get_csrf_token(project_id)

    def list_projects(
        self, *, include_archived: bool = False, include_trashed: bool = False
    ) -> list[dict[str, Any]]:
        """Return Overleaf projects visible to the authenticated user."""
        soup, _ = self._fetch_dashboard_html_and_csrf()
        projects_meta = soup.find("meta", {"name": "ol-prefetchedProjectsBlob"})
        raw_projects: list[dict[str, Any]] = []

        if projects_meta and projects_meta.get("content"):
            blob = json.loads(str(projects_meta.get("content")))
            raw_projects = blob.get("projects", [])
        else:
            # Fallback for Overleaf instances using JSON script tags
            for script in soup.find_all("script", {"type": "application/json"}):
                try:
                    payload = json.loads(script.string or "{}")
                    if isinstance(payload, dict) and "projects" in payload:
                        raw_projects = payload["projects"]
                        break
                except Exception:
                    continue

        results: list[dict[str, Any]] = []
        for p in raw_projects:
            archived = bool(p.get("archived", False))
            trashed = bool(p.get("trashed", False))
            if not include_archived and archived:
                continue
            if not include_trashed and trashed:
                continue
            pid = p.get("id") or p.get("_id")
            results.append(
                {
                    "id": pid,
                    "name": p.get("name"),
                    "access_level": p.get("accessLevel"),
                    "last_updated": p.get("lastUpdated"),
                    "archived": archived,
                    "trashed": trashed,
                    "editor_url": f"https://{self.host}/project/{pid}",
                }
            )
        return results

    def resolve_project_id(self, project: str) -> str:
        """Resolve a 24-char project ID, Overleaf project URL, or project title to a project ID."""
        ref = project.strip()
        if _PROJECT_ID_RE.match(ref):
            return ref
        url_match = _PROJECT_URL_RE.search(ref)
        if url_match:
            return url_match.group(1)

        projects = self.list_projects(include_archived=True, include_trashed=False)
        exact = [p for p in projects if p["name"] == ref]
        if len(exact) == 1:
            return exact[0]["id"]
        if len(exact) > 1:
            ids = ", ".join(f"{p['id']} ({p['last_updated']})" for p in exact)
            raise ValueError(
                f"Multiple Overleaf projects named {ref!r}: {ids}. Pass the exact project ID."
            )

        ci = [p for p in projects if (p["name"] or "").lower() == ref.lower()]
        if len(ci) == 1:
            return ci[0]["id"]
        if len(ci) > 1:
            ids = ", ".join(f"{p['name']} [{p['id']}]" for p in ci)
            raise ValueError(
                f"Multiple Overleaf projects matched {ref!r} case-insensitively: {ids}. Pass the exact project ID."
            )

        available = ", ".join(f"{p['name']!r} ({p['id']})" for p in projects[:15])
        raise ValueError(
            f"Overleaf project {ref!r} not found. Available projects: {available or 'none'}"
        )

    def _flatten_tree(
        self, folder: ProjectFolder, prefix: str = ""
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for child in sorted(folder.children, key=lambda c: (c.type != "folder", c.name)):
            rel_path = f"{prefix}/{child.name}" if prefix else child.name
            if isinstance(child, ProjectFolder):
                entries.append(
                    {"path": rel_path, "type": "folder", "id": child.id}
                )
                entries.extend(self._flatten_tree(child, rel_path))
            else:
                entries.append(
                    {"path": rel_path, "type": child.type, "id": child.id}
                )
        return entries

    def list_files(self, project: str) -> dict[str, Any]:
        pid = self.resolve_project_id(project)
        root = self._get_project_root_folder(pid)
        entries = self._flatten_tree(root)
        return {
            "project_id": pid,
            "root_folder_id": root.id,
            "count": len(entries),
            "entries": entries,
        }

    def _ensure_folder(
        self, project_id: str, root: ProjectFolder, folder_path: str
    ) -> ProjectFolder:
        """Traverse or create nested folders while keeping the in-memory ProjectFolder tree updated."""
        cleaned = folder_path.strip("/")
        if not cleaned or cleaned == ".":
            return root

        api = self.get_api()
        current = root
        for part in cleaned.split("/"):
            if not part or part == ".":
                continue
            existing = None
            for child in current.children:
                if child.name == part:
                    if not isinstance(child, ProjectFolder):
                        raise FileExistsError(
                            f"Cannot create folder {part!r}: a file already exists at that path."
                        )
                    existing = child
                    break
            if existing is not None:
                current = existing
            else:
                created = api.project_create_folder(project_id, current.id, part)
                current.children.append(created)
                current = created
        return current

    def _find_entity(
        self, root: ProjectFolder, remote_path: str
    ) -> ProjectFolder | ProjectFile | None:
        cleaned = remote_path.strip("/")
        if not cleaned or cleaned == ".":
            return root
        current: ProjectFolder | ProjectFile = root
        for part in cleaned.split("/"):
            if not isinstance(current, ProjectFolder):
                return None
            for child in current.children:
                if child.name == part:
                    current = child
                    break
            else:
                return None
        return current

    def upload_bytes(
        self,
        project: str,
        remote_path: str,
        content: bytes,
        *,
        root_cache: ProjectFolder | None = None,
    ) -> dict[str, Any]:
        pid = self.resolve_project_id(project)
        api = self.get_api()
        root = root_cache or self._get_project_root_folder(pid)

        norm_path = posixpath.normpath(remote_path.lstrip("/"))
        if norm_path in ("", ".") or norm_path.startswith(".."):
            raise ValueError(f"Invalid remote file path: {remote_path!r}")

        parent_dir = posixpath.dirname(norm_path)
        filename = posixpath.basename(norm_path)

        target_folder = self._ensure_folder(pid, root, parent_dir)
        uploaded = api.project_upload_file(pid, target_folder.id, filename, content)
        # Keep in-memory folder tree synced if caller reuses root_cache across multiple files
        if not any(c.name == filename and not isinstance(c, ProjectFolder) for c in target_folder.children):
            target_folder.children.append(uploaded)

        return {
            "project_id": pid,
            "remote_path": norm_path,
            "entity_id": uploaded.id,
            "entity_type": uploaded.type,
            "bytes": len(content),
            "editor_url": f"https://{self.host}/project/{pid}",
        }

    def upload_file(
        self, project: str, local_path: str, remote_path: str | None = None
    ) -> dict[str, Any]:
        src = Path(local_path).expanduser().resolve()
        if not src.is_file():
            raise FileNotFoundError(f"Local file not found: {src}")
        dest_path = remote_path.strip("/") if remote_path else src.name
        if remote_path and remote_path.endswith("/"):
            dest_path = f"{remote_path.rstrip('/')}/{src.name}"
        return self.upload_bytes(project, dest_path, src.read_bytes())

    def upload_folder(
        self,
        project: str,
        local_dir: str,
        remote_dir: str = "",
        ignore_patterns: list[str] | None = None,
    ) -> dict[str, Any]:
        src_dir = Path(local_dir).expanduser().resolve()
        if not src_dir.is_dir():
            raise NotADirectoryError(f"Local directory not found: {src_dir}")

        pid = self.resolve_project_id(project)
        root = self._get_project_root_folder(pid)

        patterns = tuple(ignore_patterns) if ignore_patterns is not None else DEFAULT_IGNORE_PATTERNS
        prefix = remote_dir.strip("/")

        uploaded_files: list[str] = []
        total_bytes = 0

        for item in sorted(src_dir.rglob("*")):
            if not item.is_file():
                continue
            rel = item.relative_to(src_dir)
            if should_ignore(rel.parts, patterns):
                continue
            rel_posix = rel.as_posix()
            dest_posix = f"{prefix}/{rel_posix}" if prefix else rel_posix
            data = item.read_bytes()
            self.upload_bytes(pid, dest_posix, data, root_cache=root)
            uploaded_files.append(dest_posix)
            total_bytes += len(data)

        return {
            "project_id": pid,
            "local_dir": str(src_dir),
            "remote_dir": prefix or "/",
            "uploaded_count": len(uploaded_files),
            "total_bytes": total_bytes,
            "uploaded_files": uploaded_files,
            "editor_url": f"https://{self.host}/project/{pid}",
        }

    @staticmethod
    def _build_zip_from_directory(
        source_dir: Path, ignore_patterns: tuple[str, ...] = DEFAULT_IGNORE_PATTERNS
    ) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for item in sorted(source_dir.rglob("*")):
                if not item.is_file():
                    continue
                rel = item.relative_to(source_dir)
                if should_ignore(rel.parts, ignore_patterns):
                    continue
                zf.write(item, arcname=rel.as_posix())
        return buf.getvalue()

    def create_project(
        self,
        project_name: str,
        *,
        source_path: str | None = None,
        template: str = "none",
        ignore_patterns: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a brand-new Overleaf project (blank/example, or populated from a local folder or .zip)."""
        name = project_name.strip()
        if not name:
            raise ValueError("project_name must not be empty.")

        _, csrf = self._fetch_dashboard_html_and_csrf()
        session = self._session()
        patterns = tuple(ignore_patterns) if ignore_patterns is not None else DEFAULT_IGNORE_PATTERNS

        if source_path:
            src = Path(source_path).expanduser().resolve()
            if not src.exists():
                raise FileNotFoundError(f"source_path does not exist: {src}")

            if src.is_dir():
                zip_bytes = self._build_zip_from_directory(src, patterns)
            elif src.is_file() and src.suffix.lower() == ".zip":
                zip_bytes = src.read_bytes()
            else:
                raise ValueError(
                    f"source_path must be a directory or a .zip archive, got: {src}"
                )

            zip_filename = f"{name}.zip"
            resp = session.post(
                f"https://{self.host}/project/new/upload",
                data={
                    "name": zip_filename,
                    "type": "application/zip",
                },
                files={
                    "qqfile": (zip_filename, zip_bytes, "application/zip"),
                },
                headers={
                    "Referer": f"https://{self.host}/project",
                    "Accept": "application/json",
                    "Cache-Control": "no-cache",
                    "x-csrf-token": csrf,
                },
                timeout=max(self.timeout, 120),
            )

            if resp.ok:
                payload = _safe_json(resp, "POST /project/new/upload")
                pid = payload.get("project_id")
                if pid:
                    return {
                        "created": True,
                        "method": "zip_upload",
                        "project_id": pid,
                        "project_name": name,
                        "editor_url": f"https://{self.host}/project/{pid}",
                    }

            # Fallback if zip archive lacked a top-level .tex file and Overleaf returned 422:
            if src.is_dir():
                blank = self.create_project(name, source_path=None, template="none")
                pid = blank["project_id"]
                upload_summary = self.upload_folder(
                    pid, str(src), ignore_patterns=list(patterns)
                )
                return {
                    "created": True,
                    "method": "blank_then_folder_upload",
                    "project_id": pid,
                    "project_name": name,
                    "uploaded_count": upload_summary["uploaded_count"],
                    "editor_url": f"https://{self.host}/project/{pid}",
                }

            raise RuntimeError(
                f"Failed to upload new project archive ({resp.status_code}): {resp.text[:500]}"
            )

        # Create blank or example project via POST /project/new
        tpl = "example" if template == "example" else "none"
        resp = session.post(
            f"https://{self.host}/project/new",
            json={"projectName": name, "template": tpl},
            headers={
                "Referer": f"https://{self.host}/project",
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "x-csrf-token": csrf,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        payload = _safe_json(resp, "POST /project/new")
        pid = payload.get("project_id")
        if not pid:
            raise RuntimeError(f"Unexpected /project/new response: {payload}")
        return {
            "created": True,
            "method": f"template:{tpl}",
            "project_id": pid,
            "project_name": name,
            "editor_url": f"https://{self.host}/project/{pid}",
        }

    def read_file(
        self,
        project: str,
        remote_path: str,
        *,
        save_to_path: str | None = None,
    ) -> dict[str, Any]:
        pid = self.resolve_project_id(project)
        api = self.get_api()
        root = self._get_project_root_folder(pid)
        entity = self._find_entity(root, remote_path)
        if entity is None:
            raise FileNotFoundError(
                f"Remote path {remote_path!r} not found in Overleaf project {pid}."
            )
        if isinstance(entity, ProjectFolder):
            raise IsADirectoryError(
                f"Remote path {remote_path!r} is a folder. Use overleaf_list_files or overleaf_pull_project."
            )

        raw_bytes = api.project_download_file(pid, entity)
        assert isinstance(raw_bytes, bytes)

        if save_to_path:
            out = Path(save_to_path).expanduser().resolve()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(raw_bytes)
            return {
                "project_id": pid,
                "remote_path": remote_path.strip("/"),
                "saved_to": str(out),
                "bytes": len(raw_bytes),
            }

        try:
            text = raw_bytes.decode("utf-8")
            return {
                "project_id": pid,
                "remote_path": remote_path.strip("/"),
                "type": entity.type,
                "bytes": len(raw_bytes),
                "content": text,
            }
        except UnicodeDecodeError:
            return {
                "project_id": pid,
                "remote_path": remote_path.strip("/"),
                "type": entity.type,
                "bytes": len(raw_bytes),
                "binary": True,
                "message": "File is binary. Pass save_to_path='/path/to/file' to download it locally.",
            }

    def delete_path(self, project: str, remote_path: str) -> dict[str, Any]:
        pid = self.resolve_project_id(project)
        api = self.get_api()
        root = self._get_project_root_folder(pid)
        entity = self._find_entity(root, remote_path)
        if entity is None:
            return {
                "deleted": False,
                "project_id": pid,
                "remote_path": remote_path.strip("/"),
                "message": "Path did not exist.",
            }
        if entity.id == root.id:
            raise ValueError("Cannot delete the project root folder.")
        api.project_delete_entity(pid, entity)
        return {
            "deleted": True,
            "project_id": pid,
            "remote_path": remote_path.strip("/"),
            "entity_type": entity.type,
        }

    def pull_project_zip(self, project: str, output_dir: str) -> dict[str, Any]:
        """Download an entire Overleaf project as a .zip and extract it to output_dir."""
        pid = self.resolve_project_id(project)
        zip_bytes = self.get_api().download_project(pid)
        assert isinstance(zip_bytes, bytes)

        dest = Path(output_dir).expanduser().resolve()
        extracted = safe_extract_zip(zip_bytes, dest)

        return {
            "project_id": pid,
            "output_dir": str(dest),
            "extracted_count": len(extracted),
            "files": extracted[:200],
        }

    def _resolve_output_url(
        self, project_id: str, output_file: dict[str, Any], compile_data: dict[str, Any]
    ) -> str:
        raw_url = output_file.get("url") or ""
        if raw_url.startswith("http://") or raw_url.startswith("https://"):
            return raw_url
        if raw_url.startswith("/"):
            return f"https://{self.host}{raw_url}"
        build_id = output_file.get("build")
        path = output_file.get("path", "output.pdf")
        if build_id:
            return f"https://{self.host}/project/{project_id}/build/{build_id}/output/{path}"
        prefix = compile_data.get("outputUrlPrefix") or ""
        if prefix.startswith("/"):
            return f"https://{self.host}{prefix}/{path}"
        return f"https://{self.host}/project/{project_id}/output/{path}"

    def compile_project(
        self,
        project: str,
        *,
        save_pdf_path: str | None = None,
        save_log_path: str | None = None,
        draft: bool = False,
        root_doc_path: str | None = None,
    ) -> dict[str, Any]:
        """Trigger remote Overleaf compilation, parse build logs, and optionally download PDF/log."""
        pid = self.resolve_project_id(project)
        csrf = self._get_project_csrf(pid)
        session = self._session()

        body: dict[str, Any] = {
            "draft": draft,
            "check": "silent",
            "incrementalCompilesEnabled": True,
        }

        if root_doc_path:
            root = self._get_project_root_folder(pid)
            entity = self._find_entity(root, root_doc_path)
            if entity is None or isinstance(entity, ProjectFolder):
                raise FileNotFoundError(
                    f"Specified root_doc_path {root_doc_path!r} not found in project {pid}."
                )
            body["rootDoc_id"] = entity.id

        compile_url = f"https://{self.host}/project/{pid}/compile"
        compile_headers = {
            "Referer": f"https://{self.host}/project/{pid}",
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "x-csrf-token": csrf,
        }
        resp = session.post(
            compile_url,
            json=body,
            headers=compile_headers,
            timeout=max(self.timeout, 180),
        )
        resp.raise_for_status()
        data = _safe_json(resp, f"POST /project/{pid}/compile")

        # Retry once if Overleaf throttles back-to-back compiles
        if data.get("status") == "too-recently-compiled":
            time.sleep(2.5)
            resp = session.post(
                compile_url,
                json=body,
                headers=compile_headers,
                timeout=max(self.timeout, 180),
            )
            resp.raise_for_status()
            data = _safe_json(resp, f"POST /project/{pid}/compile (retry)")

        status = data.get("status", "unknown")
        output_files: list[dict[str, Any]] = data.get("outputFiles") or []

        clsi_server_id = data.get("clsiServerId")
        query_suffix = f"?clsiserverid={clsi_server_id}" if clsi_server_id else ""

        pdf_entry = next(
            (f for f in output_files if f.get("type") == "pdf" or f.get("path") == "output.pdf"),
            None,
        )
        log_entry = next(
            (f for f in output_files if f.get("type") == "log" or f.get("path") == "output.log"),
            None,
        )

        result: dict[str, Any] = {
            "project_id": pid,
            "status": status,
            "editor_url": f"https://{self.host}/project/{pid}",
            "output_files": [
                {"path": f.get("path"), "type": f.get("type")} for f in output_files
            ],
        }

        if data.get("validationProblems"):
            result["validation_problems"] = data["validationProblems"]

        # Fetch and parse output.log whenever available
        if log_entry is not None:
            log_url = self._resolve_output_url(pid, log_entry, data)
            if "?" not in log_url and query_suffix:
                log_url += query_suffix
            log_resp = session.get(
                log_url,
                headers={"Referer": f"https://{self.host}/project/{pid}"},
                timeout=self.timeout,
            )
            if log_resp.ok:
                log_text = log_resp.content.decode("utf-8", errors="replace")
                result["diagnostics"] = parse_latex_log(log_text)
                if save_log_path:
                    log_out = Path(save_log_path).expanduser().resolve()
                    log_out.parent.mkdir(parents=True, exist_ok=True)
                    log_out.write_text(log_text, encoding="utf-8")
                    result["saved_log_path"] = str(log_out)

        # Download output.pdf if requested
        if save_pdf_path and pdf_entry is not None:
            pdf_url = self._resolve_output_url(pid, pdf_entry, data)
            if "?" not in pdf_url and query_suffix:
                pdf_url += query_suffix
            pdf_resp = session.get(
                pdf_url,
                headers={"Referer": f"https://{self.host}/project/{pid}"},
                timeout=max(self.timeout, 120),
            )
            pdf_resp.raise_for_status()
            pdf_out = Path(save_pdf_path).expanduser().resolve()
            pdf_out.parent.mkdir(parents=True, exist_ok=True)
            pdf_out.write_bytes(pdf_resp.content)
            result["saved_pdf_path"] = str(pdf_out)
            result["pdf_bytes"] = len(pdf_resp.content)
        elif save_pdf_path and pdf_entry is None:
            result["pdf_warning"] = (
                "No output.pdf was produced by this compile run (check diagnostics.errors)."
            )

        return result
