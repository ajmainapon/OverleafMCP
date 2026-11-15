"""Tests for Overleaf MCP Server protocol handshake and core helpers."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import zipfile
import io
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from overleaf_mcp.config import OverleafConfig
from overleaf_mcp.git_client import OverleafGitClient, extract_project_id_from_ref
from overleaf_mcp.web_client import (
    OverleafWebClient,
    _parse_cookie_string,
    parse_latex_log,
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
                print(f"Verified {len(tool_names)} MCP tools over Stdio successfully!")


if __name__ == "__main__":
    test_cookie_and_log_parsers()
    test_zip_builder_and_git_ref()
    asyncio.run(verify_mcp_stdio())
