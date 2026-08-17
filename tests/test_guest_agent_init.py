import subprocess
from pathlib import Path


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
