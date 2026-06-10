"""Coding-context awareness — base Hermes, every interactive surface.

When the user runs Hermes inside a code workspace (CLI, TUI, desktop app, or
an editor over ACP), Hermes shifts into a coding posture:

  1. **Tool restriction** — the toolset collapses to the coding-relevant set
     (file, terminal, search, web docs, skills, todo, delegate, vision,
     browser). Messaging / TTS / image-gen / smart-home / music / cron /
     computer-use fall away — noise the model never needs while pairing on
     code.
  2. **Operating brief** — a Cursor-style system block: gather context before
     editing, make focused diffs, verify, never fabricate.
  3. **Live workspace snapshot** — git root, branch + upstream (ahead/behind),
     worktree, dirty/staged counts, and recent commits.

The snapshot is built ONCE per session at prompt-build time and baked into the
stable system prompt — never re-probed per turn (that would shatter the prompt
cache). Branch and dirty state drift mid-session, so the brief tells the model
to re-check with ``git`` before acting on them.

Activation (config ``agent.coding_context``): ``auto`` (default) turns it on
for interactive coding surfaces sitting in a git repo; ``on`` forces it
anywhere; ``off`` disables it.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hermes.coding_context")

CODING_TOOLSET = "coding"

# Surfaces where a coding posture makes sense under ``auto``. Messaging
# platforms (telegram, discord, slack, …) are intentionally absent — a chat
# bot in a group is not pair-programming.
INTERACTIVE_CODING_PLATFORMS = {"cli", "tui", "acp", "desktop", ""}

_GIT_TIMEOUT = 2.5


# Cursor-style operating brief. Tool names referenced here (read_file,
# search_files, patch, terminal, todo) are in the coding toolset and in
# _HERMES_CORE_TOOLS, so they're present on every surface this fires on.
CODING_AGENT_GUIDANCE = (
    "You are a coding agent pairing with the user inside their codebase. "
    "Operate like a careful senior engineer:\n"
    "- Understand before you change: read the relevant files (`read_file`) "
    "and locate code with `search_files` rather than guessing. Never invent "
    "files, symbols, or APIs — if you haven't seen it, go look.\n"
    "- Make focused edits with `patch`/`write_file`; match the project's "
    "existing style and conventions (AGENTS.md / .cursorrules already in "
    "context win over your defaults). Touch only what the task needs.\n"
    "- Use `terminal` for git, builds, tests, and inspection; verify your "
    "work (run the relevant tests/linter/build) before claiming it's done.\n"
    "- Track multi-step work with `todo`. Reference code as `path:line` "
    "rather than pasting whole files.\n"
    "- Git is the user's, not yours: don't commit, push, or alter history "
    "unless asked. The Workspace block below is a snapshot from session "
    "start — re-run `git status`/`git branch` before relying on it.\n"
    "- Be concise. Lead with the change or answer, not a preamble."
)


def _coding_mode(config: Optional[dict[str, Any]]) -> str:
    """Return the normalized ``agent.coding_context`` mode (auto/on/off)."""
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}
    raw = ((config or {}).get("agent", {}) or {}).get("coding_context", "auto")
    mode = str(raw).strip().lower()
    if mode in {"on", "true", "yes", "1", "always"}:
        return "on"
    if mode in {"off", "false", "no", "0", "never"}:
        return "off"
    return "auto"


def _resolve_cwd(cwd: Optional[str | Path]) -> Path:
    if cwd:
        return Path(cwd).expanduser()
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        return resolve_agent_cwd()
    except Exception:
        return Path(os.getcwd())


def _git_root(cwd: Path) -> Optional[Path]:
    current = cwd.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def is_coding_context(
    *,
    platform: Optional[str] = None,
    cwd: Optional[str | Path] = None,
    config: Optional[dict[str, Any]] = None,
) -> bool:
    """Whether Hermes should operate in its coding posture right now.

    ``auto`` (default): true for an interactive coding surface sitting in a
    git repo. ``on``: always true. ``off``: always false.
    """
    mode = _coding_mode(config)
    if mode == "off":
        return False
    if mode == "on":
        return True
    if platform is not None and platform.strip().lower() not in INTERACTIVE_CODING_PLATFORMS:
        return False
    return _git_root(_resolve_cwd(cwd)) is not None


def _enabled_mcp_servers(config: Optional[dict[str, Any]]) -> list[str]:
    """Names of MCP servers the user has enabled — kept in the coding posture.

    MCP servers (figma, browser, tophat, …) are explicitly configured and part
    of the coding workflow, not noise to strip.
    """
    try:
        from hermes_cli.config import read_raw_config
        from hermes_cli.tools_config import _parse_enabled_flag

        servers = read_raw_config().get("mcp_servers") or {}
        return [
            str(name)
            for name, cfg in servers.items()
            if isinstance(cfg, dict)
            and _parse_enabled_flag(cfg.get("enabled", True), default=True)
        ]
    except Exception:
        return []


def coding_selection(
    *,
    platform: Optional[str] = None,
    cwd: Optional[str | Path] = None,
    config: Optional[dict[str, Any]] = None,
) -> Optional[list[str]]:
    """The toolset selection for the coding posture, or ``None`` when it's off.

    Callers apply this only when the user hasn't pinned an explicit selection
    (a ``--toolsets`` flag, ``HERMES_TUI_TOOLSETS``, …); they never override
    a pin. Returns the coding toolset plus the user's enabled MCP servers.
    """
    if not is_coding_context(platform=platform, cwd=cwd, config=config):
        return None
    return [CODING_TOOLSET, *_enabled_mcp_servers(config)]


def _git(cwd: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _parse_status(porcelain: str) -> tuple[dict[str, str], dict[str, int]]:
    """Parse ``git status --porcelain=2 --branch`` into branch + counts."""
    branch: dict[str, str] = {}
    counts = {"staged": 0, "modified": 0, "untracked": 0, "conflicts": 0}
    for line in porcelain.splitlines():
        if line.startswith("# branch.head"):
            branch["head"] = line.split(maxsplit=2)[-1]
        elif line.startswith("# branch.upstream"):
            branch["upstream"] = line.split(maxsplit=2)[-1]
        elif line.startswith("# branch.ab"):
            parts = line.split()
            branch["ahead"], branch["behind"] = parts[2].lstrip("+"), parts[3].lstrip("-")
        elif line.startswith(("1 ", "2 ")):
            xy = line.split(maxsplit=2)[1]
            if xy[0] != ".":
                counts["staged"] += 1
            if xy[1] != ".":
                counts["modified"] += 1
        elif line.startswith("u "):
            counts["conflicts"] += 1
        elif line.startswith("? "):
            counts["untracked"] += 1
    return branch, counts


def build_coding_workspace_block(cwd: Optional[str | Path] = None) -> str:
    """Live git/workspace snapshot for the system prompt (empty if not a repo)."""
    root = _git_root(_resolve_cwd(cwd))
    if root is None:
        return ""

    lines = ["Workspace (snapshot at session start — re-check with `git` before acting on it):"]
    lines.append(f"- Root: {root}")

    branch, counts = _parse_status(_git(root, "status", "--porcelain=2", "--branch"))
    head = branch.get("head", "")
    if head and head != "(detached)":
        line = f"- Branch: {head}"
        if branch.get("upstream"):
            line += f" \u2192 {branch['upstream']}"
            ahead, behind = branch.get("ahead", "0"), branch.get("behind", "0")
            if ahead != "0" or behind != "0":
                line += f" (ahead {ahead}, behind {behind})"
        lines.append(line)
    elif head == "(detached)":
        lines.append("- Branch: (detached HEAD)")

    # Linked worktree: the per-worktree git dir differs from the shared common dir.
    git_dir, common_dir = _git(root, "rev-parse", "--git-dir"), _git(root, "rev-parse", "--git-common-dir")
    if git_dir and common_dir and Path(git_dir).resolve() != Path(common_dir).resolve():
        main_tree = Path(common_dir).resolve().parent
        lines.append(f"- Worktree: linked (primary tree at {main_tree})")

    dirty = [f"{n} {label}" for label, n in (
        ("staged", counts["staged"]), ("modified", counts["modified"]),
        ("untracked", counts["untracked"]), ("conflicts", counts["conflicts"]),
    ) if n]
    lines.append(f"- Status: {', '.join(dirty) if dirty else 'clean'}")

    recent = _git(root, "log", "-3", "--pretty=%h %s")
    if recent:
        lines.append("- Recent commits:")
        lines.extend(f"    {c}" for c in recent.splitlines())

    return "\n".join(lines)
