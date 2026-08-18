import json
import shutil
import subprocess
from pathlib import Path

import pytest


def link_host_instructions_function() -> str:
    agent_init = (Path(__file__).parents[1] / "guest" / "agent-init.sh").read_text()
    start = agent_init.index("link_host_instructions() {")
    end = agent_init.index("\n}\n", start) + len("\n}\n")
    return agent_init[start:end]


def run_link(tmp_path: Path, host_instructions: Path, target: Path) -> None:
    script = tmp_path / "link.sh"
    script.write_text(
        "set -eu\n"
        # The guest runs this as root; the test only cares about the links.
        "chown() { :; }\n"
        f'host_instructions="{host_instructions}"\n'
        f"{link_host_instructions_function()}"
        f'link_host_instructions "{target}"\n'
    )
    subprocess.run(["sh", str(script)], check=True, capture_output=True, text=True)


def test_host_agents_md_is_linked_into_an_agent_configuration(tmp_path: Path) -> None:
    host = tmp_path / "host-instructions" / "AGENTS.md"
    host.parent.mkdir()
    host.write_text("# rules\n")
    target = tmp_path / "home" / ".claude" / "CLAUDE.md"
    target.parent.mkdir(parents=True)

    run_link(tmp_path, host, target)

    assert target.is_symlink()
    assert target.read_text() == "# rules\n"


def test_a_file_written_inside_the_sandbox_keeps_priority(tmp_path: Path) -> None:
    host = tmp_path / "host-instructions" / "AGENTS.md"
    host.parent.mkdir()
    host.write_text("# host\n")
    target = tmp_path / "home" / ".agents" / "AGENTS.md"
    target.parent.mkdir(parents=True)
    target.write_text("# sandbox\n")

    run_link(tmp_path, host, target)

    assert not target.is_symlink()
    assert target.read_text() == "# sandbox\n"


def test_a_link_to_a_removed_host_file_is_cleaned_up(tmp_path: Path) -> None:
    host = tmp_path / "host-instructions" / "AGENTS.md"
    target = tmp_path / "home" / ".agents" / "AGENTS.md"
    target.parent.mkdir(parents=True)
    target.symlink_to(host)

    run_link(tmp_path, host, target)

    assert not target.exists()
    assert not target.is_symlink()


def test_an_unrelated_link_survives_a_missing_host_file(tmp_path: Path) -> None:
    host = tmp_path / "host-instructions" / "AGENTS.md"
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("# other\n")
    target = tmp_path / "home" / ".agents" / "AGENTS.md"
    target.parent.mkdir(parents=True)
    target.symlink_to(elsewhere)

    run_link(tmp_path, host, target)

    assert target.is_symlink()
    assert target.read_text() == "# other\n"


def register_chrome_devtools_mcp_function() -> str:
    agent_init = (Path(__file__).parents[1] / "guest" / "agent-init.sh").read_text()
    start = agent_init.index("register_chrome_devtools_mcp() {")
    end = agent_init.index("\n}\n", start) + len("\n}\n")
    return agent_init[start:end]


def run_register(tmp_path: Path, config: Path, *, browser: bool = True) -> None:
    script = tmp_path / "register.sh"
    stubs = (
        # `command -v` finds shell functions, so these stand in for the binaries
        # the golden image installs.
        "chrome-devtools-mcp() { :; }\ngoogle-chrome() { :; }\n"
        if browser
        else "PATH=/nonexistent\n"
    )
    script.write_text(
        "set -eu\n"
        # The guest runs this as root; the test only cares about the JSON.
        'install() { while [ $# -gt 2 ]; do shift; done; cp "$1" "$2"; }\n'
        f"{stubs}"
        f"{register_chrome_devtools_mcp_function()}"
        f'register_chrome_devtools_mcp "{config}"\n'
    )
    subprocess.run(["bash", str(script)], check=True, capture_output=True, text=True)


needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required in the guest")


@needs_jq
def test_chrome_devtools_is_registered_as_a_headless_user_scope_mcp_server(
    tmp_path: Path,
) -> None:
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"numStartups": 3}))

    run_register(tmp_path, config)

    written = json.loads(config.read_text())
    server = written["mcpServers"]["chrome-devtools"]
    assert server["command"] == "chrome-devtools-mcp"
    assert "--headless" in server["args"]
    assert "--isolated" in server["args"]
    # Unrelated Claude Code state survives the rewrite.
    assert written["numStartups"] == 3


@needs_jq
def test_a_server_configured_inside_the_sandbox_keeps_priority(tmp_path: Path) -> None:
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"mcpServers": {"chrome-devtools": {"command": "my-own-chrome"}}}))

    run_register(tmp_path, config)

    written = json.loads(config.read_text())
    assert written["mcpServers"]["chrome-devtools"] == {"command": "my-own-chrome"}


@needs_jq
def test_a_missing_configuration_file_is_created(tmp_path: Path) -> None:
    config = tmp_path / ".claude.json"

    run_register(tmp_path, config)

    assert "chrome-devtools" in json.loads(config.read_text())["mcpServers"]


def test_an_image_without_chrome_registers_nothing(tmp_path: Path) -> None:
    config = tmp_path / ".claude.json"

    run_register(tmp_path, config, browser=False)

    assert not config.exists()


def register_playwright_mcp_function() -> str:
    agent_init = (Path(__file__).parents[1] / "guest" / "agent-init.sh").read_text()
    start = agent_init.index("register_playwright_mcp() {")
    end = agent_init.index("\n}\n", start) + len("\n}\n")
    return agent_init[start:end]


def run_register_playwright(tmp_path: Path, config: Path, *, browser: bool = True) -> None:
    script = tmp_path / "register-playwright.sh"
    stubs = "playwright-mcp() { :; }\n" if browser else "PATH=/nonexistent\n"
    script.write_text(
        "set -eu\n"
        'install() { while [ $# -gt 2 ]; do shift; done; cp "$1" "$2"; }\n'
        f"{stubs}"
        f"{register_playwright_mcp_function()}"
        f'register_playwright_mcp "{config}"\n'
    )
    subprocess.run(["bash", str(script)], check=True, capture_output=True, text=True)


@needs_jq
def test_playwright_is_registered_as_a_headless_user_scope_mcp_server(tmp_path: Path) -> None:
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"mcpServers": {"chrome-devtools": {"command": "keep-me"}}}))

    run_register_playwright(tmp_path, config)

    servers = json.loads(config.read_text())["mcpServers"]
    assert servers["playwright"]["command"] == "playwright-mcp"
    assert servers["playwright"]["args"] == ["--headless", "--isolated"]
    # The browser registered before it survives the rewrite.
    assert servers["chrome-devtools"] == {"command": "keep-me"}


@needs_jq
def test_a_playwright_server_configured_inside_the_sandbox_keeps_priority(tmp_path: Path) -> None:
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"mcpServers": {"playwright": {"command": "my-own-playwright"}}}))

    run_register_playwright(tmp_path, config)

    written = json.loads(config.read_text())
    assert written["mcpServers"]["playwright"] == {"command": "my-own-playwright"}


def test_an_image_without_playwright_registers_nothing(tmp_path: Path) -> None:
    config = tmp_path / ".claude.json"

    run_register_playwright(tmp_path, config, browser=False)

    assert not config.exists()
