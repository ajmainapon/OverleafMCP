"""Tests for Overleaf MCP Server protocol handshake, security guards, and core helpers."""

from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import requests
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from overleaf_mcp.config import OverleafConfig, should_ignore
from overleaf_mcp.git_client import extract_project_id_from_ref
from overleaf_mcp.web_client import (
    OverleafWebClient,
    _parse_cookie_string,
    _safe_json,
    parse_latex_log,
    safe_extract_zip,
)


def test_cookie_and_log_parsers() -> None:
    parsed = _parse_cookie_string("s%3Aabc123xyz")
    assert parsed == {"overleaf_session2": "s%3Aabc123xyz"}

    parsed_multi = _parse_cookie_string(
        "Cookie: overleaf_session2=s%3Afoo; GCLB=bar123"
    )
    assert parsed_multi["overleaf_session2"] == "s%3Afoo"
    assert parsed_multi["GCLB"] == "bar123"

    sample_log = "\n".join(
        [
            "This is pdfTeX, Version 3.141592653",
            "LaTeX Warning: Citation `vaswani2017' on page 1 undefined on input line 42.",
            "! Undefined control sequence.",
            "l.55 \\badmacro",
            "              {hello}",
            "Output written on output.pdf (4 pages, 123456 bytes).",
        ]
    )
    diag = parse_latex_log(sample_log)
    assert diag["error_count"] == 1
    assert diag["warning_count"] == 1
    assert "\\badmacro" in diag["errors"][0]


def test_dynamic_config_and_glob_ignore() -> None:
    # Verify glob matching works for both directory names and wildcard extensions
    assert should_ignore((".git", "config")) is True
    assert should_ignore(("sections", "intro.aux")) is True
    assert should_ignore(("sections", "intro.tex")) is False
    assert should_ignore(("build.pdf",), ("*.pdf",)) is True

    # Verify OVERLEAF_MCP_CONFIG_DIR is resolved dynamically at runtime (not frozen at import)
    old_val = os.environ.get("OVERLEAF_MCP_CONFIG_DIR")
    try:
        with tempfile.TemporaryDirectory() as tmp_cfg:
            os.environ["OVERLEAF_MCP_CONFIG_DIR"] = tmp_cfg
            cfg = OverleafConfig.load()
            saved_file = cfg.save_updates(git_token="olp_dynamic_test")
            assert saved_file == Path(tmp_cfg) / "credentials.json"
            reloaded = OverleafConfig.load()
            assert reloaded.git_token == "olp_dynamic_test"
    finally:
        if old_val is None:
            os.environ.pop("OVERLEAF_MCP_CONFIG_DIR", None)
        else:
            os.environ["OVERLEAF_MCP_CONFIG_DIR"] = old_val


def test_zip_slip_sibling_prefix_defense() -> None:
    """Ensure '../paper-evil/pwn.tex' cannot escape '/tmp/.../paper' via prefix matching."""
    with tempfile.TemporaryDirectory() as base_tmp:
        dest = Path(base_tmp) / "paper"
        sibling_evil = Path(base_tmp) / "paper-evil"

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("main.tex", "\\documentclass{article}")
            zf.writestr("../paper-evil/pwn.tex", "malicious")

        extracted = safe_extract_zip(buf.getvalue(), dest)
        assert extracted == ["main.tex"]
        assert (dest / "main.tex").exists()
        assert not (sibling_evil / "pwn.tex").exists()


def test_websocket_close_and_safe_json() -> None:
    """Ensure _get_project_root_folder always closes its socket and _safe_json catches HTML."""
    cfg = OverleafConfig(cookie="overleaf_session2=s%3Adummy")
    client = OverleafWebClient(cfg)

    fake_socket = MagicMock()
    join_payload = {
        "name": "joinProjectResponse",
        "args": [
            {
                "project": {
                    "rootFolder": [
                        {
                            "_id": "root123",
                            "name": "rootFolder",
                            "folders": [],
                            "fileRefs": [],
                            "docs": [{"_id": "doc1", "name": "main.tex"}],
                        }
                    ]
                }
            }
        ],
    }
    fake_socket.recv.side_effect = ["1::", f"5:::{json.dumps(join_payload)}"]

    fake_api = MagicMock()
    fake_api._open_socket.return_value = fake_socket
    client._api = fake_api

    root = client._get_project_root_folder("65f0a1b2c3d4e5f601234567")
    assert root.id == "root123"
    assert len(root.children) == 1
    fake_socket.close.assert_called_once()

    # Verify _safe_json surfaces clear error on HTML gateway response
    bad_resp = requests.Response()
    bad_resp.status_code = 502
    bad_resp.url = "https://www.overleaf.com/project/123/compile"
    bad_resp._content = b"<html><body>502 Bad Gateway</body></html>"
    try:
        _safe_json(bad_resp, "POST /compile")
        raise AssertionError("Expected RuntimeError on non-JSON response")
    except RuntimeError as exc:
        assert "502 Bad Gateway" in str(exc)


def test_zip_builder_and_git_ref() -> None:
    pid = extract_project_id_from_ref(
        "https://www.overleaf.com/project/65f0a1b2c3d4e5f601234567"
    )
    assert pid == "65f0a1b2c3d4e5f601234567"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "main.tex").write_text("\\documentclass{article}", encoding="utf-8")
        (root / "figures").mkdir()
        (root / "figures" / "plot.png").write_bytes(b"PNGDATA")
        (root / ".git").mkdir()
        (root / ".git" / "config").write_text("secret", encoding="utf-8")
        (root / "main.aux").write_text("aux", encoding="utf-8")

        zip_bytes = OverleafWebClient._build_zip_from_directory(root)
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            names = set(zf.namelist())
            assert "main.tex" in names
            assert "figures/plot.png" in names
            assert ".git/config" not in names
            assert "main.aux" not in names


async def verify_mcp_stdio() -> None:
    venv_bin = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "overleaf-mcp"
    with tempfile.TemporaryDirectory() as cfg_dir:
        env = os.environ.copy()
        env["OVERLEAF_MCP_CONFIG_DIR"] = cfg_dir
        server_params = StdioServerParameters(
            command=str(venv_bin),
            args=[],
            env=env,
        )
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools_resp = await session.list_tools()
                tool_names = {t.name for t in tools_resp.tools}
                expected = {
                    "overleaf_auth_status",
                    "overleaf_configure_auth",
                    "overleaf_list_projects",
                    "overleaf_create_project",
                    "overleaf_list_files",
                    "overleaf_read_file",
                    "overleaf_write_file",
                    "overleaf_upload_file",
                    "overleaf_upload_folder",
                    "overleaf_delete_path",
                    "overleaf_pull_project",
                    "overleaf_compile",
                    "overleaf_git_sync",
                }
                assert expected.issubset(tool_names), f"Missing tools: {expected - tool_names}"

                # Test configure_auth & auth_status round-trip over MCP stdio
                cfg_res = await session.call_tool(
                    "overleaf_configure_auth",
                    {"git_token": "olp_test_token_123", "host": "www.overleaf.com"},
                )
                payload = json.loads(cfg_res.content[0].text)
                assert payload["git_token_configured"] is True

                status_res = await session.call_tool(
                    "overleaf_auth_status",
                    {"verify_live": False},
                )
                status_payload = json.loads(status_res.content[0].text)
                assert status_payload["git_bridge"]["ready"] is True
                assert status_payload["credentials_file"] == str(
                    Path(cfg_dir) / "credentials.json"
                )
                print(f"Verified {len(tool_names)} MCP tools and all security/reliability tests!")


if __name__ == "__main__":
    test_cookie_and_log_parsers()
    test_dynamic_config_and_glob_ignore()
    test_zip_slip_sibling_prefix_defense()
    test_websocket_close_and_safe_json()
    test_zip_builder_and_git_ref()
    asyncio.run(verify_mcp_stdio())
