"""Session-level delivery of ``mcp.json`` servers when the shared spec is refused.

The primary agent spec pins ``includeMcpJson: false``, so the spec rebuild is
what turns an ``mcp.json`` entry into a mounted server: it copies the entry into
``mcpServers`` and its ``@ref`` into ``tools``. An instance on a non-default
``KIROCREW_HOME`` that shares ``~/.kiro/agents`` with another install is refused
that rebuild (:func:`kiro_crew.agent._decline_shared_agent_home`), correctly --
the spec belongs to the other install and pins ITS home into every managed
entry. Without this module the refused instance's own ``mcp.json`` servers then
reach none of its sessions, while its dashboard probe shows them Online.

The refused rebuild therefore still runs the MCP half of the rebuild, in memory,
over this instance's own sources: the same merge, command resolution, alias
normalization and mute/disable handling a written spec would get
(the rebuild's own MCP passes, reached through :mod:`kiro_crew.agent`). What a written spec would
have mounted for a user-installed stdio server, and the shared spec does not, is
recorded in a file under THIS instance's data home -- never in the shared
agents dir. Each new primary session then appends those entries to its
``session/new`` array, the same session-level channel the gateway's broker
stubs ride. Nothing about the other install changes.

What is deliberately NOT carried:

* **Grants.** ``autoApprove`` is dropped and nothing is added to
  ``allowedTools``: the shared spec is not ours to widen, so every call to a
  delivered server goes through the approval gate. Mounting is not approving.
* **Remote (url) servers.** Only a stdio element is known to be accepted on the
  session-level channel; a remote server is named in the gateway log instead.
* **Registry mode.** An injected, unmarked entry is dropped by the client under
  a registry ceiling, and nothing here can resolve a name against the catalog,
  so nothing is delivered (the same ceiling the broker stubs observe).
* **Anything the session already has.** A name the shared spec declares, or a
  broker stub carries, is never delivered twice.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection
from pathlib import Path
from typing import Any

from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.agent_sdk.mcp_refs import parse_tools_refs
from kiro_crew.mcp_cleanup import mcp_entry_is_muted, mcp_entry_is_registry_governed
from kiro_crew.mcp_utils import mcp_server_alias

logger = logging.getLogger(__name__)

#: File name of the projection, under this instance's data home.
PROJECTION_FILENAME = "declined-home-mcp.json"

#: Keys an injected element must not carry: grants (see module docstring) and
#: our own provenance bookkeeping, which means nothing to the client.
_STRIPPED_KEYS = frozenset({"autoApprove", "disabled", "poolable"})

PRIMARY_AGENT = Path(AGENT_FILENAME).stem


def projection_path() -> Path:
    """The projection file, in this instance's own data home."""
    from kiro_crew.config.paths import config_dir

    return config_dir() / PROJECTION_FILENAME


def _source_aliases(sources: Any) -> set[str]:
    """Aliases of the servers a user installed through an ``mcp.json`` scope."""
    names: set[str] = set()
    for _label, scope in sources.scopes:
        if isinstance(scope, dict):
            names.update(mcp_server_alias(str(n)) for n in scope)
    return names - {mcp_server_alias(n) for n in sources.managed_names}


def compute_projection() -> dict[str, dict[str, Any]]:
    """What a written primary spec would mount for this instance's own servers.

    Runs the rebuild's MCP passes over an EMPTY base, so the result holds only
    what the sources contribute, resolved and normalized exactly as a written
    spec would hold it. Returns ``{alias: entry}`` for the user-installed stdio
    servers that pass every launch predicate; the shared spec is not consulted
    here (the session-side filter does that, against the file as it is then).
    """
    from kiro_crew import agent as agent_mod  # circular at module scope

    mcp_sources = agent_mod.mcp_sources
    mcp_aliases = agent_mod.mcp_aliases
    config: dict[str, Any] = {"mcpServers": {}, "tools": [], "allowedTools": []}
    sources = mcp_sources.merge_mcp_sources(config)
    resolved = mcp_sources.resolve_mcp_servers(config, sources, agent_mod._resolve_mcp_command)
    mounted = mcp_aliases.normalize_server_keys(config, resolved.unresolved)
    mcp_sources.sync_shared_server_refs(config, sources, mounted)

    user_aliases = _source_aliases(sources)
    grant_all, refs = parse_tools_refs(config.get("tools"))
    referenced = set(refs)
    out: dict[str, dict[str, Any]] = {}
    remote: list[str] = []
    for alias, entry in (config.get("mcpServers") or {}).items():
        if alias not in user_aliases or not isinstance(entry, dict):
            continue
        if not (grant_all or alias in referenced):
            continue
        if mcp_entry_is_muted(entry) or mcp_entry_is_registry_governed(entry):
            continue
        if entry.get("url"):
            remote.append(alias)
            continue
        if not isinstance(entry.get("command"), str):
            continue
        out[alias] = {k: v for k, v in entry.items() if k not in _STRIPPED_KEYS}
    if remote:
        logger.warning(
            "Shared agent home is not writable from this data home; remote MCP "
            "server(s) %s cannot be delivered at session level and will not reach "
            "this instance's sessions",
            ", ".join(sorted(remote)),
        )
    return out


def _source_fingerprint() -> list[Any]:
    """What the projection was computed from, cheaply: each source file's stat
    signature plus the search ``PATH``.

    A refused instance re-attempts the rebuild on every refresh poll, and the
    projection runs the full MCP passes (command resolution, audit events). An
    unchanged fingerprint skips them; any edit to an ``mcp.json`` scope, or a
    ``PATH`` change that can move command resolution, recomputes.
    """
    import os

    from kiro_crew import agent as agent_mod  # circular at module scope

    files = [agent_mod._KIRO_MCP_JSON, agent_mod._user_dir() / "mcp.json"]
    files.extend(agent_mod.mcp_sources._extra_mcp_scope_globals())
    sig: list[Any] = []
    for path in files:
        try:
            st = Path(path).stat()
            sig.append([str(path), st.st_mtime_ns, st.st_size])
        except OSError:
            sig.append([str(path), None, None])
    sig.append(os.environ.get("PATH", ""))
    return sig


def write_projection() -> dict[str, dict[str, Any]]:
    """Recompute and persist the projection when its inputs moved; returns it.

    Called from the refused rebuild, so it runs wherever that rebuild runs (boot,
    a Sync, a sessions restart, a dashboard MCP change, the refresh poll). The
    caller treats a failure as best-effort: the previous projection stays.
    """
    from kiro_crew.atomic_write import atomic_write

    fingerprint = _source_fingerprint()
    path = projection_path()
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = None
    if isinstance(stored, dict) and stored.get("fingerprint") == fingerprint:
        servers = stored.get("servers")
        return servers if isinstance(servers, dict) else {}

    servers = compute_projection()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        path,
        json.dumps({"fingerprint": fingerprint, "servers": servers}, indent=2, sort_keys=True),
    )
    if servers:
        logger.info(
            "Shared agent home is not writable from this data home; delivering %d "
            "mcp.json server(s) at session level: %s",
            len(servers),
            ", ".join(sorted(servers)),
        )
    return servers


def clear_projection() -> None:
    """Remove the projection once this instance owns its spec again."""
    try:
        projection_path().unlink(missing_ok=True)
    except OSError:
        logger.debug("could not remove %s", projection_path(), exc_info=True)


def _read_projection() -> dict[str, dict[str, Any]]:
    path = projection_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("unreadable %s; delivering no session-level servers", path)
        return {}
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return {}
    return {str(k): v for k, v in servers.items() if isinstance(v, dict)}


def _shared_spec_names() -> set[str] | None:
    """Aliases the shared primary spec declares, or ``None`` when unreadable."""
    from kiro_crew.agent import kiro_agents_dir_path
    from kiro_crew.agent_discovery import _read_agent_spec

    path = kiro_agents_dir_path() / AGENT_FILENAME
    if not path.is_file():
        return set()
    spec = _read_agent_spec(path, operation="declined_home_session_mcp", source="unknown")
    if spec is None:
        return None
    servers = spec.get("mcpServers")
    if not isinstance(servers, dict):
        return set()
    return {mcp_server_alias(str(n)) for n in servers}


def _env_pairs(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, dict):
        return []
    return [{"name": str(k), "value": str(v)} for k, v in raw.items()]


def session_servers(
    agent: str | None,
    *,
    work_dir: str | Path | None = None,
    present: Collection[str] = (),
) -> list[dict[str, Any]]:
    """ACP ``session/new`` elements for this session's undelivered servers.

    ``present`` is every name the caller's array already carries (the broker
    stubs); a server under that name, raw or aliased, is never added again.
    Empty -- the pre-existing behaviour -- unless ALL of these hold: the session
    runs the user-level primary agent, the shared spec write is refused right
    now, no registry ceiling applies, and the shared spec is readable. Blocking
    file I/O; callers are already off the event loop. Never raises.
    """
    try:
        return _session_servers(agent, work_dir=work_dir, present=present)
    except Exception:
        logger.warning("session-level mcp.json delivery failed; delivering none", exc_info=True)
        return []


def _session_servers(
    agent: str | None,
    *,
    work_dir: str | Path | None,
    present: Collection[str],
) -> list[dict[str, Any]]:
    from kiro_crew.agent import (  # circular at module scope
        _decline_shared_agent_home,
        _mcp_registry_mode,
        _project_shadow_of,
    )

    if (agent or PRIMARY_AGENT) != PRIMARY_AGENT:
        return []
    if work_dir and (
        _project_shadow_of(PRIMARY_AGENT, work_dir, markdown_specs=False, dispatchable_only=True)
        is not None
    ):
        # The checkout declares its own agent of this name: the session is not
        # running the user-level spec this projection supplements.
        return []
    if _decline_shared_agent_home(audit=False) is None:
        # The instance writes its own spec, which already mounts every server.
        return []
    if _mcp_registry_mode():
        return []
    projection = _read_projection()
    if not projection:
        return []
    declared = _shared_spec_names()
    if declared is None:
        # Cannot tell what the spec already mounts; delivering could register a
        # server twice, so deliver nothing.
        return []
    taken = declared | {mcp_server_alias(str(n)) for n in present} | set(present)
    out: list[dict[str, Any]] = []
    for alias, entry in sorted(projection.items()):
        if alias in taken or mcp_entry_is_muted(entry):
            continue
        command = entry.get("command")
        if not isinstance(command, str) or not command:
            continue
        raw_args = entry.get("args")
        args = [
            a if isinstance(a, str) else json.dumps(a, sort_keys=True, default=str)
            for a in (raw_args if isinstance(raw_args, (list, tuple)) else ())
        ]
        out.append(
            {
                "name": alias,
                "command": command,
                "args": args,
                "env": _env_pairs(entry.get("env")),
            }
        )
    return out
