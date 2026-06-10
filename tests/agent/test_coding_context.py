"""Tests for agent.coding_context — resolver, toolset substitution, git probe."""

import subprocess

import pytest

from agent import coding_context as cc


def _git_init(path):
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    for args in (
        ["init", "-q", "-b", "main"],
        ["commit", "-q", "--allow-empty", "-m", "init commit"],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, env={**env, "HOME": str(path)})


# ── resolver ──────────────────────────────────────────────────────────────

class TestIsCodingContext:
    def test_off_never_activates(self, tmp_path):
        _git_init(tmp_path)
        cfg = {"agent": {"coding_context": "off"}}
        assert cc.is_coding_context(platform="cli", cwd=tmp_path, config=cfg) is False

    def test_on_forces_even_without_git(self, tmp_path):
        cfg = {"agent": {"coding_context": "on"}}
        assert cc.is_coding_context(platform="telegram", cwd=tmp_path, config=cfg) is True

    def test_auto_requires_git_repo(self, tmp_path):
        cfg = {"agent": {"coding_context": "auto"}}
        assert cc.is_coding_context(platform="cli", cwd=tmp_path, config=cfg) is False
        _git_init(tmp_path)
        assert cc.is_coding_context(platform="cli", cwd=tmp_path, config=cfg) is True

    def test_auto_skips_messaging_surfaces(self, tmp_path):
        _git_init(tmp_path)
        cfg = {"agent": {"coding_context": "auto"}}
        assert cc.is_coding_context(platform="discord", cwd=tmp_path, config=cfg) is False
        assert cc.is_coding_context(platform="tui", cwd=tmp_path, config=cfg) is True

    def test_default_mode_is_auto(self, tmp_path):
        # Unknown/missing value normalizes to auto.
        _git_init(tmp_path)
        assert cc.is_coding_context(platform="cli", cwd=tmp_path, config={}) is True


# ── toolset substitution ────────────────────────────────────────────────────

class TestCodingSelection:
    def test_selects_coding_when_active(self, tmp_path):
        _git_init(tmp_path)
        cfg = {"agent": {"coding_context": "on"}}
        out = cc.coding_selection(platform="cli", cwd=tmp_path, config=cfg)
        assert out[0] == cc.CODING_TOOLSET

    def test_none_when_inactive(self, tmp_path):
        cfg = {"agent": {"coding_context": "off"}}
        assert cc.coding_selection(platform="cli", cwd=tmp_path, config=cfg) is None

    def test_coding_toolset_is_registered(self):
        from toolsets import resolve_toolset

        tools = resolve_toolset(cc.CODING_TOOLSET)
        # Coding essentials present…
        for t in ("read_file", "write_file", "patch", "search_files", "terminal", "todo"):
            assert t in tools
        # …and the noise is gone.
        for t in ("send_message", "text_to_speech", "image_generate", "computer_use"):
            assert t not in tools


# ── git/workspace probe ─────────────────────────────────────────────────────

class TestWorkspaceBlock:
    def test_empty_outside_repo(self, tmp_path):
        assert cc.build_coding_workspace_block(tmp_path) == ""

    def test_reports_branch_and_clean_status(self, tmp_path):
        _git_init(tmp_path)
        block = cc.build_coding_workspace_block(tmp_path)
        assert "Workspace" in block
        assert f"Root: {tmp_path.resolve()}" in block or "Root:" in block
        assert "Branch: main" in block
        assert "Status: clean" in block
        assert "init commit" in block

    def test_reports_dirty_counts(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "untracked.txt").write_text("hi")
        block = cc.build_coding_workspace_block(tmp_path)
        assert "untracked" in block
        assert "clean" not in block.split("Status:")[1].splitlines()[0]


# ── prompt assembly integration ─────────────────────────────────────────────

class TestStatusParsing:
    def test_parse_status_counts_and_branch(self):
        porcelain = (
            "# branch.head feature\n"
            "# branch.upstream origin/feature\n"
            "# branch.ab +2 -1\n"
            "1 M. N... 100644 100644 100644 aaa bbb staged.py\n"
            "1 .M N... 100644 100644 100644 ccc ddd modified.py\n"
            "? new.py\n"
            "u UU N... 1 2 3 abc def conflict.py\n"
        )
        branch, counts = cc._parse_status(porcelain)
        assert branch["head"] == "feature"
        assert branch["upstream"] == "origin/feature"
        assert branch["ahead"] == "2" and branch["behind"] == "1"
        assert counts["staged"] == 1
        assert counts["modified"] == 1
        assert counts["untracked"] == 1
        assert counts["conflicts"] == 1
