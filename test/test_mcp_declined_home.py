"""An ``mcp.json`` server must reach new sessions on a refused shared agent home.

Shape under test: an instance on a non-default ``KIROCREW_HOME`` shares
``~/.kiro/agents`` with a default-home install. ``_decline_shared_agent_home``
refuses every spec rebuild from it, which is correct -- but the primary spec pins
``includeMcpJson: false``, so the rebuild is the ONLY way a server added to
``~/.kiro/settings/mcp.json`` reaches a session, while the gateway's probe starts
the server from the source file and shows it Online. A gateway stub reaches the
session regardless, because the overlay is injected at ``session/new`` from this
instance's own data home.

The refused rebuild projects this instance's own servers privately, and the
session path delivers them at ``session/new``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

#: An absolute command that exists on every CI OS, so command resolution keeps it.
_CMD = sys.executable


@pytest.fixture(autouse=True)
def _pin_default_home(monkeypatch, tmp_path):
    """Keep the guard's default-home comparison and breadcrumb off the real home."""
    from kiro_crew.config import paths

    monkeypatch.setattr(paths, "_resolved_home", None, raising=False)
    monkeypatch.setattr(paths, "_resolve_default_home", lambda: tmp_path / "pinned-default-home")
    monkeypatch.setattr(paths, "_write_recovery_breadcrumb", lambda data_home: None, raising=False)
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)


def _shared_agents(monkeypatch, agents_dir: Path) -> None:
    """Make *agents_dir* both the write target and the ambient (shared) agents dir."""
    from kiro_crew import agent

    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(agent, "ambient_agents_dir", lambda: agents_dir)


def _durable_checkout(monkeypatch) -> None:
    from kiro_crew import agent

    monkeypatch.setattr(agent, "__file__", "/durable-install/KiroCrew/src/kiro_crew/agent.py")


def _write_spec(agents_dir: Path, spec: dict) -> None:
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")


#: What a DEFAULT-home install leaves in the shared agents dir: managed entries
#: pinned to no ``KIROCREW_HOME`` (the default-home writer's signature).
_DEFAULT_HOME_SPEC = {
    "name": "kirocrew",
    "includeMcpJson": False,
    "mcpServers": {
        "kirocrew-core": {"command": "kirocrew", "args": ["mcp", "core"], "env": {}},
        "taskei": {"command": "taskei-mcp"},
    },
    "tools": ["@kirocrew-core", "@taskei"],
}


def _declined_home(monkeypatch, tmp_path, settings_servers: dict) -> Path:
    """A non-default home beside a default-home spec, with *settings_servers*.

    Returns the shared agents dir. Nothing is faked on the decision path: the
    real guard refuses because the shared spec carries the default-home signature.
    """
    from kiro_crew import agent

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "relocated-home"))
    _durable_checkout(monkeypatch)
    shared = tmp_path / "agents"
    _write_spec(shared, _DEFAULT_HOME_SPEC)
    _shared_agents(monkeypatch, shared)
    settings = tmp_path / "settings-mcp.json"
    settings.write_text(json.dumps({"mcpServers": settings_servers}), encoding="utf-8")
    monkeypatch.setattr(agent, "_KIRO_MCP_JSON", settings)
    return shared


def _kiro_session_array(tmp_path, stubs=()) -> list[dict]:
    """What a kiro ``session/new`` receives in ``mcpServers`` (the shared append)."""
    from kiro_crew.acp.client import AcpClient

    client = AcpClient(work_dir=tmp_path / "work", agent="kirocrew")
    client._pooled_broker_stubs = lambda: [dict(s) for s in stubs]  # type: ignore[method-assign]
    return client._pooled_mcp_servers()


class TestDeclinedAgentHomeDelivery:
    def test_mcp_json_server_reaches_session_new_through_the_real_decline(
        self, monkeypatch, tmp_path
    ):
        """The reported shape, end to end: refused rebuild -> session/new array.

        Reverted (no projection on the refused rebuild, or no append on the
        session path), ``outlook`` is absent from what kiro-cli receives -- the
        Online-but-no-tools report.
        """
        from kiro_crew import agent

        shared = _declined_home(
            monkeypatch,
            tmp_path,
            {
                "outlook": {"command": _CMD, "args": ["--mcp"], "autoApprove": ["*"]},
                "taskei": {"command": _CMD},
                "muted": {"command": _CMD, "disabled": True},
                "remote": {"url": "https://example.invalid/mcp"},
            },
        )

        _path, wrote = agent.rebuild_agent_config_reporting()

        assert wrote is False, "the default-home install's shared spec must not be written"
        spec = json.loads((shared / "kirocrew.json").read_text(encoding="utf-8"))
        assert spec == _DEFAULT_HOME_SPEC, "the other install's spec must stay unchanged"

        by_name = {e["name"]: e for e in _kiro_session_array(tmp_path)}
        # taskei: already in the shared spec. muted: disabled. remote: not stdio.
        assert set(by_name) == {"outlook"}, by_name
        outlook = by_name["outlook"]
        assert os.path.normcase(outlook["command"]) == os.path.normcase(_CMD)
        assert outlook["args"] == ["--mcp"]
        # No grant rides along, and no session credential reaches a third party.
        assert "autoApprove" not in outlook
        assert all("TOKEN" not in pair["name"] for pair in outlook["env"])

    def test_a_stubbed_name_is_not_registered_twice(self, monkeypatch, tmp_path):
        from kiro_crew import agent

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()
        stub = {"name": "outlook", "command": "/stub", "args": [], "env": [], "type": "stdio"}

        array = _kiro_session_array(tmp_path, stubs=[stub])

        assert [e["name"] for e in array] == ["outlook"]
        assert array[0]["command"] == "/stub"

    def test_nothing_is_delivered_once_the_home_owns_its_spec(self, monkeypatch, tmp_path):
        """A written spec mounts the server itself; the projection is retired."""
        from kiro_crew import agent, mcp_declined_home

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()
        assert mcp_declined_home.projection_path().is_file()

        # The other install's specs are gone: this home now writes its own.
        for spec in (tmp_path / "agents").glob("kirocrew*.json"):
            spec.unlink()
        _path, wrote = agent.rebuild_agent_config_reporting()

        assert wrote is True
        assert not mcp_declined_home.projection_path().exists()
        assert _kiro_session_array(tmp_path) == []

    def test_registry_mode_delivers_nothing(self, monkeypatch, tmp_path):
        from kiro_crew import agent

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()
        monkeypatch.setattr(agent, "_mcp_registry_mode", lambda: True)

        assert _kiro_session_array(tmp_path) == []

    def test_an_unchanged_source_is_not_recomputed_and_an_edit_is(self, monkeypatch, tmp_path):
        """The refresh poll re-runs the refused rebuild; it must not redo the passes."""
        from kiro_crew import agent, mcp_declined_home

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        calls: list[int] = []
        real = mcp_declined_home.compute_projection

        def counting():
            calls.append(1)
            return real()

        monkeypatch.setattr(mcp_declined_home, "compute_projection", counting)
        agent.rebuild_agent_config()
        agent.rebuild_agent_config()
        assert len(calls) == 1

        settings = tmp_path / "settings-mcp.json"
        settings.write_text(
            json.dumps({"mcpServers": {"calendar": {"command": _CMD, "args": ["x"]}}}),
            encoding="utf-8",
        )
        agent.rebuild_agent_config()
        assert len(calls) == 2
        assert [e["name"] for e in _kiro_session_array(tmp_path)] == ["calendar"]

    def test_another_agent_gets_nothing(self, monkeypatch, tmp_path):
        from kiro_crew import agent, mcp_declined_home

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()

        assert mcp_declined_home.session_servers("kirocrew-worker") == []

    @pytest.mark.asyncio
    async def test_the_shared_runtime_appends_after_the_stubs(self, monkeypatch, tmp_path):
        """AcpRuntime (kiro and KAS) takes the same delivery; mirrors do not."""
        from kiro_crew import agent
        from kiro_crew.acp.runtime import AcpRuntime
        from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()
        stubs = [{"name": "kirocrew-core", "command": "/stub", "args": [], "env": []}]

        fake = SimpleNamespace(acp_backend=ACP_BACKEND_KIRO, _agent="kirocrew")
        out = await AcpRuntime._with_declined_home_servers(fake, stubs, None, tmp_path)
        assert [e["name"] for e in out] == ["kirocrew-core", "outlook"]

        fake.acp_backend = ACP_BACKEND_CLAUDE
        assert await AcpRuntime._with_declined_home_servers(fake, stubs, None, tmp_path) == stubs
