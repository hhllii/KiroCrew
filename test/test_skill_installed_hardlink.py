"""A hardlinking installer's built-in app skills still load.

uv (and other installers) hardlink package files out of a cache, so every file of
an installed ``kiro_crew`` package can carry ``st_nlink > 1``. The no-link reader
refuses that shape, which left a built-in app skill listed but never loadable,
and made an ``always: true`` one fail every session start. The reader now admits
such a file on CONTENT: it must be a ``SKILL.md`` the distribution RECORD lists
inside the package tree, and its bytes must hash to the recorded digest. A
hardlink anywhere else, the skills dir included, stays refused.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

import pytest

import kiro_crew.skills as skills_mod
from kiro_crew.testing.links import make_dir_link


def _write_skill(directory: Path, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    skill_file = directory / "SKILL.md"
    skill_file.write_text(body, encoding="utf-8")
    return skill_file


def _record_digest(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode(
        "ascii"
    )


class _FakeInstall:
    """A site dir whose ``kiro_crew`` package came from a hardlinking installer.

    The built-in app skill shares its inode with a second name (the installer's
    cache), and the distribution RECORD lists the digest the installer wrote.
    """

    BODY = "---\nname: pinned\ndescription: d\nalways: true\n---\n# Pinned\nstep one\n"

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        site = tmp_path / "site"
        self.package = site / "kiro_crew"
        self.package.mkdir(parents=True)
        (self.package / "__init__.py").write_text("", encoding="utf-8")
        self.skill_dir = self.package / "apps" / "builtins" / "demo" / "skills" / "pinned"
        self.skill = _write_skill(self.skill_dir, self.BODY)
        cache = tmp_path / "installer-cache"
        cache.mkdir()
        try:
            os.link(self.skill, cache / "SKILL.md")
        except OSError as exc:  # pragma: no cover — a filesystem without hardlinks
            pytest.skip(f"hardlinks unavailable here: {exc}")
        self.dist_info = site / "kirocrew-0.0.0.dist-info"
        self.dist_info.mkdir()
        self.write_record(self.skill.read_bytes())
        monkeypatch.setattr(skills_mod, "_INSTALLED_PACKAGE_DIR", self.package)
        monkeypatch.setattr(skills_mod, "_package_record_cache", {})
        self.base = tmp_path / "skills"
        self.base.mkdir()
        # The shape `apps.bridges._register_skills` lays down: a link on POSIX, a
        # junction on Windows.
        make_dir_link(self.base / "pinned", self.skill_dir)

    def write_record(self, recorded: bytes) -> None:
        rel = self.skill.relative_to(self.package.parent).as_posix()
        lines = [
            f"kiro_crew/__init__.py,{_record_digest(b'')},0",
            f"{rel},{_record_digest(recorded)},{len(recorded)}",
            f"{self.dist_info.name}/RECORD,,",
        ]
        (self.dist_info / "RECORD").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def loader(self) -> skills_mod.SkillsLoader:
        return skills_mod.SkillsLoader(self.base, install_builtins=False)


class TestAHardlinkingInstallerStillLoads:
    """A hardlinked package ``SKILL.md`` loads when its bytes are what RECORD lists."""

    def test_a_recorded_package_skill_lists_and_loads(self, tmp_path, monkeypatch):
        install = _FakeInstall(tmp_path, monkeypatch)
        assert install.skill.stat().st_nlink == 2
        loader = install.loader()
        try:
            assert [s["key"] for s in loader.list_skills()] == ["pinned"]
            assert loader.load_skill("pinned") == install.BODY
        finally:
            loader.close()

    def test_an_always_skill_reaches_the_startup_context(self, tmp_path, monkeypatch):
        install = _FakeInstall(tmp_path, monkeypatch)
        loader = install.loader()
        try:
            context = loader.get_context(budget=20_000)
        finally:
            loader.close()
        assert "### Skill: pinned" in context and "step one" in context

    def test_bytes_that_differ_from_the_record_are_refused(self, tmp_path, monkeypatch):
        install = _FakeInstall(tmp_path, monkeypatch)
        install.write_record(b"what the installer wrote\n")
        loader = install.loader()
        try:
            assert loader.load_skill("pinned") is None
        finally:
            loader.close()

    def test_a_reinstall_is_judged_against_its_own_record(self, tmp_path, monkeypatch):
        install = _FakeInstall(tmp_path, monkeypatch)
        loader = install.loader()
        try:
            assert loader.load_skill("pinned") == install.BODY
            install.write_record(b"a later release\n")
            # A new RECORD is a new identity even when the rewrite lands inside one
            # mtime tick at an equal size; pin that rather than racing the clock.
            record = install.dist_info / "RECORD"
            stat = record.stat()
            os.utime(record, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
            assert loader.load_skill("pinned") is None
        finally:
            loader.close()

    def test_a_hardlink_in_the_skills_dir_is_still_refused(self, tmp_path, monkeypatch):
        """Same bytes as the recorded file, but no RECORD entry at this path."""
        install = _FakeInstall(tmp_path, monkeypatch)
        local = _write_skill(install.base / "local", install.BODY)
        os.link(local, tmp_path / "second-name")
        loader = install.loader()
        try:
            assert loader.load_skill("local") is None
            assert loader.load_skill("pinned") == install.BODY
        finally:
            loader.close()
