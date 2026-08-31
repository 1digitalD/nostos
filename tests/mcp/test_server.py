from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nostos.mcp import server as mcp_server

CLI_COMMANDS = frozenset({"init", "watch", "rank", "list", "explain"})

TOOL_CLI_FLAGS: dict[str, frozenset[str]] = {
    "init": frozenset(
        {
            "profile",
            "citypack",
            "force",
            "non_interactive",
            "city",
            "max_rent",
            "beds",
            "baths_min",
            "baths_max",
            "min_area",
            "laundry",
            "must_have_laundry",
            "parking",
            "pets",
            "avoid_basement",
            "require_unfurnished",
            "source",
            "notify_url",
            "schedule",
        }
    ),
    "watch": frozenset({"profile", "db", "citypack", "source", "dry_run", "yes"}),
    "rank": frozenset({"profile", "db", "citypack"}),
    "list": frozenset({"profile", "db", "citypack", "limit"}),
    "explain": frozenset({"listing_id", "profile", "db"}),
}


def _tool_names() -> set[str]:
    tools = asyncio.run(mcp_server.mcp.list_tools())
    return {tool.name for tool in tools}


def _tool_schema(tool_name: str) -> dict[str, Any]:
    tools = asyncio.run(mcp_server.mcp.list_tools())
    for tool in tools:
        if tool.name == tool_name:
            return dict(tool.input_schema.get("properties", {}))
    raise AssertionError(f"tool not found: {tool_name}")


def test_mcp_tool_names_match_cli_commands() -> None:
    assert _tool_names() == CLI_COMMANDS


def test_mcp_exposes_no_extra_tools() -> None:
    assert _tool_names() == CLI_COMMANDS


@pytest.mark.parametrize("tool_name", sorted(CLI_COMMANDS))
def test_mcp_tool_argument_shapes_match_cli_flags(tool_name: str) -> None:
    properties = _tool_schema(tool_name)
    assert frozenset(properties) == TOOL_CLI_FLAGS[tool_name]


def test_init_tool_invokes_cli_with_expected_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    def fake_invoke(argv: list[str]) -> str:
        captured.append(list(argv))
        return "ok"

    monkeypatch.setattr(mcp_server, "_invoke_cli", fake_invoke)

    result = mcp_server.init(
        profile="/tmp/profile.yaml",
        citypack="/tmp/citypack.yaml",
        non_interactive=True,
        city="vancouver",
        max_rent=3400,
        beds=2,
        laundry="nice-to-have",
        source=["craigslist", "kijiji"],
    )

    assert result == "ok"
    assert captured == [
        [
            "init",
            "--profile",
            "/tmp/profile.yaml",
            "--citypack",
            "/tmp/citypack.yaml",
            "--non-interactive",
            "--city",
            "vancouver",
            "--max-rent",
            "3400",
            "--beds",
            "2",
            "--laundry",
            "nice-to-have",
            "--avoid-basement",
            "--require-unfurnished",
            "--source",
            "craigslist",
            "--source",
            "kijiji",
            "--schedule",
            "0 */6 * * *",
        ]
    ]


def test_watch_tool_invokes_cli_with_expected_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    def fake_invoke(argv: list[str]) -> str:
        captured.append(list(argv))
        return "ok"

    monkeypatch.setattr(mcp_server, "_invoke_cli", fake_invoke)

    result = mcp_server.watch(
        profile="/tmp/profile.yaml",
        db="/tmp/nostos.db",
        source=["stub"],
        dry_run=True,
        yes=True,
    )

    assert result == "ok"
    assert captured == [
        [
            "watch",
            "--profile",
            "/tmp/profile.yaml",
            "--db",
            "/tmp/nostos.db",
            "--source",
            "stub",
            "--dry-run",
            "--yes",
        ]
    ]


def test_explain_tool_invokes_cli_with_listing_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    def fake_invoke(argv: list[str]) -> str:
        captured.append(list(argv))
        return "ok"

    monkeypatch.setattr(mcp_server, "_invoke_cli", fake_invoke)

    result = mcp_server.explain("craigslist:abc123", profile="/tmp/profile.yaml")

    assert result == "ok"
    assert captured == [
        [
            "explain",
            "craigslist:abc123",
            "--profile",
            "/tmp/profile.yaml",
        ]
    ]
