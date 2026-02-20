"""Comprehensive tests for check_dist."""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import textwrap
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from check_dist._core import (
    CheckDistError,
    _matches_hatch_pattern,
    _sdist_expected_files,
    check_absent,
    check_dist,
    check_present,
    check_sdist_vs_vcs,
    check_wrong_platform_extensions,
    find_dist_files,
    get_vcs_files,
    list_sdist_files,
    list_wheel_files,
    load_config,
    load_hatch_config,
    matches_pattern,
    translate_extension,
)

# ── translate_extension ───────────────────────────────────────────────


class TestTranslateExtension:
    def test_no_change_on_native(self):
        """No translation when extension is already native."""
        if sys.platform == "linux":
            assert translate_extension("foo.so") == "foo.so"
        elif sys.platform == "darwin":
            assert translate_extension("foo.so") == "foo.so"
        elif sys.platform == "win32":
            assert translate_extension("foo.pyd") == "foo.pyd"

    @patch("check_dist._core._get_platform_key", return_value="win32")
    def test_so_to_pyd_on_windows(self, _mock):
        assert translate_extension("mylib.so") == "mylib.pyd"

    @patch("check_dist._core._get_platform_key", return_value="win32")
    def test_dylib_to_dll_on_windows(self, _mock):
        assert translate_extension("mylib.dylib") == "mylib.dll"

    @patch("check_dist._core._get_platform_key", return_value="linux")
    def test_pyd_to_so_on_linux(self, _mock):
        assert translate_extension("mylib.pyd") == "mylib.so"

    @patch("check_dist._core._get_platform_key", return_value="linux")
    def test_dll_to_so_on_linux(self, _mock):
        assert translate_extension("mylib.dll") == "mylib.so"

    @patch("check_dist._core._get_platform_key", return_value="darwin")
    def test_pyd_to_so_on_darwin(self, _mock):
        assert translate_extension("mylib.pyd") == "mylib.so"

    @patch("check_dist._core._get_platform_key", return_value="darwin")
    def test_dll_to_dylib_on_darwin(self, _mock):
        assert translate_extension("mylib.dll") == "mylib.dylib"

    def test_unrecognized_extension(self):
        assert translate_extension("file.txt") == "file.txt"

    def test_no_extension(self):
        assert translate_extension("Makefile") == "Makefile"

    def test_glob_pattern(self):
        """Glob patterns with extensions are also translated."""
        with patch("check_dist._core._get_platform_key", return_value="win32"):
            assert translate_extension("*.so") == "*.pyd"


# ── matches_pattern ───────────────────────────────────────────────────


class TestMatchesPattern:
    def test_exact_file(self):
        assert matches_pattern("LICENSE", "LICENSE")

    def test_directory_prefix(self):
        assert matches_pattern("check_dist/__init__.py", "check_dist")

    def test_directory_prefix_no_false_positive(self):
        assert not matches_pattern("check_dist_extra/foo.py", "check_dist")

    def test_glob_star(self):
        assert matches_pattern("check_dist/__init__.py", "*.py")

    def test_glob_full_path(self):
        assert matches_pattern("check_dist/__init__.py", "check_dist/*.py")

    def test_no_match(self):
        assert not matches_pattern("README.md", "LICENSE")

    def test_basename_match(self):
        assert matches_pattern("some/deep/path/setup.cfg", "setup.cfg")

    @patch("check_dist._core._get_platform_key", return_value="win32")
    def test_cross_platform_extension(self, _mock):
        """On Windows, pattern ``*.so`` should match ``*.pyd``."""
        assert matches_pattern("mylib.pyd", "*.so")

    def test_question_mark_glob(self):
        assert matches_pattern("a.py", "?.py")
        assert not matches_pattern("ab.py", "?.py")

    def test_bracket_glob(self):
        assert matches_pattern("a1.txt", "a[0-9].txt")
        assert not matches_pattern("ab.txt", "a[0-9].txt")

    def test_dotfile(self):
        assert matches_pattern(".gitignore", ".gitignore")

    def test_nested_directory_pattern(self):
        assert matches_pattern(".github/workflows/ci.yml", ".github")


# ── check_present / check_absent ─────────────────────────────────────


class TestCheckPresent:
    FILES = [
        "check_dist/__init__.py",
        "check_dist/_core.py",
        "LICENSE",
        "pyproject.toml",
        "README.md",
    ]

    def test_all_present(self):
        errors = check_present(self.FILES, ["check_dist", "LICENSE"], "sdist")
        assert errors == []

    def test_missing_pattern(self):
        errors = check_present(self.FILES, ["CHANGELOG.md"], "sdist")
        assert len(errors) == 1
        assert "CHANGELOG.md" in errors[0]

    def test_glob_present(self):
        errors = check_present(self.FILES, ["*.md"], "sdist")
        assert errors == []

    def test_empty_patterns(self):
        assert check_present(self.FILES, [], "sdist") == []

    @patch("check_dist._core._get_platform_key", return_value="win32")
    def test_cross_platform_present(self, _mock):
        """Pattern '*.so' on Windows should look for '*.pyd'."""
        files = ["mypackage/ext.pyd"]
        errors = check_present(files, ["*.so"], "wheel")
        assert errors == []

    @patch("check_dist._core._get_platform_key", return_value="win32")
    def test_cross_platform_missing(self, _mock):
        """Pattern '*.so' on Windows should fail if no .pyd found."""
        files = ["mypackage/__init__.py"]
        errors = check_present(files, ["*.so"], "wheel")
        assert len(errors) == 1
        assert "translated" in errors[0]


class TestCheckAbsent:
    FILES = [
        "check_dist/__init__.py",
        "Makefile",
        ".github/workflows/ci.yml",
        "docs/index.md",
    ]

    def test_unwanted_present(self):
        errors = check_absent(self.FILES, ["Makefile"], "sdist")
        assert len(errors) == 1
        assert "Makefile" in errors[0]

    def test_unwanted_directory(self):
        errors = check_absent(self.FILES, [".github"], "sdist")
        assert len(errors) == 1
        assert ".github" in errors[0]

    def test_all_clean(self):
        errors = check_absent(self.FILES, ["dist", ".gitignore"], "sdist")
        assert errors == []

    def test_empty_patterns(self):
        assert check_absent(self.FILES, [], "sdist") == []

    def test_docs_directory(self):
        errors = check_absent(self.FILES, ["docs"], "sdist")
        assert len(errors) == 1
        assert "docs" in errors[0]


# ── check_wrong_platform_extensions ──────────────────────────────────


class TestCheckWrongPlatformExtensions:
    @patch("check_dist._core._get_platform_key", return_value="win32")
    def test_so_on_windows(self, _mock):
        files = ["mypackage/ext.so"]
        errors = check_wrong_platform_extensions(files, "wheel")
        assert len(errors) == 1
        assert ".so" in errors[0]

    @patch("check_dist._core._get_platform_key", return_value="linux")
    def test_pyd_on_linux(self, _mock):
        files = ["mypackage/ext.pyd"]
        errors = check_wrong_platform_extensions(files, "wheel")
        assert len(errors) == 1
        assert ".pyd" in errors[0]

    @patch("check_dist._core._get_platform_key", return_value="linux")
    def test_clean_on_linux(self, _mock):
        files = ["mypackage/ext.so", "mypackage/__init__.py"]
        errors = check_wrong_platform_extensions(files, "wheel")
        assert errors == []

    @patch("check_dist._core._get_platform_key", return_value="darwin")
    def test_clean_on_darwin(self, _mock):
        files = ["mypackage/ext.so", "mypackage/__init__.py"]
        errors = check_wrong_platform_extensions(files, "wheel")
        assert errors == []


# ── check_sdist_vs_vcs ───────────────────────────────────────────────


class TestCheckSdistVsVcs:
    def test_matching(self):
        sdist = ["check_dist/__init__.py", "pyproject.toml", "README.md"]
        vcs = ["check_dist/__init__.py", "pyproject.toml", "README.md"]
        errors = check_sdist_vs_vcs(sdist, vcs, {})
        assert errors == []

    def test_extra_in_sdist(self):
        sdist = ["check_dist/__init__.py", "pyproject.toml", "stray_file.txt"]
        vcs = ["check_dist/__init__.py", "pyproject.toml"]
        errors = check_sdist_vs_vcs(sdist, vcs, {})
        assert any("stray_file.txt" in e for e in errors)

    def test_missing_from_sdist(self):
        sdist = ["check_dist/__init__.py"]
        vcs = ["check_dist/__init__.py", "pyproject.toml"]
        errors = check_sdist_vs_vcs(sdist, vcs, {})
        assert any("pyproject.toml" in e for e in errors)

    def test_generated_files_ignored(self):
        sdist = ["PKG-INFO", "check_dist/__init__.py"]
        vcs = ["check_dist/__init__.py"]
        errors = check_sdist_vs_vcs(sdist, vcs, {})
        assert errors == []

    def test_egg_info_ignored(self):
        sdist = ["check_dist/__init__.py", "check_dist.egg-info/PKG-INFO"]
        vcs = ["check_dist/__init__.py"]
        errors = check_sdist_vs_vcs(sdist, vcs, {})
        assert errors == []

    def test_hatch_packages_filter(self):
        """When hatch sdist packages are set, only those should be expected."""
        sdist = ["mylib/__init__.py", "pyproject.toml"]
        vcs = [
            "mylib/__init__.py",
            "pyproject.toml",
            "tests/test_foo.py",
            "docs/index.md",
        ]
        hatch = {"targets": {"sdist": {"packages": ["mylib"]}}}
        errors = check_sdist_vs_vcs(sdist, vcs, hatch)
        assert errors == []

    def test_dotfiles_ignored_in_missing(self):
        sdist = ["check_dist/__init__.py"]
        vcs = ["check_dist/__init__.py", ".gitignore"]
        errors = check_sdist_vs_vcs(sdist, vcs, {})
        assert errors == []

    def test_artifacts_not_flagged_as_extra(self):
        """Build artifacts (e.g. .so) should not be flagged as extra."""
        sdist = ["pkg/__init__.py", "pkg/ext.so"]
        vcs = ["pkg/__init__.py"]
        hatch = {"artifacts": ["*.so"]}
        errors = check_sdist_vs_vcs(sdist, vcs, hatch)
        assert errors == []

    def test_only_include_vcs_check(self):
        """only-include config should scope the VCS comparison."""
        sdist = ["pkg/__init__.py", "rust/lib.rs", "Cargo.toml", "pyproject.toml"]
        vcs = ["pkg/__init__.py", "rust/lib.rs", "Cargo.toml", "pyproject.toml", "Makefile"]
        hatch = {"targets": {"sdist": {"only-include": ["pkg", "rust", "Cargo.toml"]}}}
        errors = check_sdist_vs_vcs(sdist, vcs, hatch)
        assert errors == []


# ── _sdist_expected_files ────────────────────────────────────────────


class TestMatchesHatchPattern:
    def test_exact_file(self):
        assert _matches_hatch_pattern("Cargo.toml", "Cargo.toml")

    def test_directory_prefix(self):
        assert _matches_hatch_pattern("rust/src/lib.rs", "rust")

    def test_rooted_pattern(self):
        assert _matches_hatch_pattern(".gitignore", "/.gitignore")

    def test_no_match(self):
        assert not _matches_hatch_pattern("src/lib.rs", "rust")

    def test_glob(self):
        assert _matches_hatch_pattern("foo.pyc", "*.pyc")

    def test_basename_match(self):
        assert _matches_hatch_pattern("deep/Makefile", "Makefile")


class TestSdistExpectedFiles:
    def test_no_hatch_config(self):
        vcs = ["a.py", "b.py", "pkg/c.py"]
        result = _sdist_expected_files(vcs, {})
        assert result == {"a.py", "b.py", "pkg/c.py"}

    def test_with_packages(self):
        vcs = ["pkg/__init__.py", "tests/test.py", "setup.py"]
        hatch = {"targets": {"sdist": {"packages": ["pkg"]}}}
        result = _sdist_expected_files(vcs, hatch)
        assert "pkg/__init__.py" in result
        assert "setup.py" not in result
        assert "tests/test.py" not in result

    def test_with_only_include(self):
        vcs = ["pkg/__init__.py", "rust/src/lib.rs", "Cargo.toml", "tests/test.py"]
        hatch = {"targets": {"sdist": {"only-include": ["pkg", "rust", "Cargo.toml"]}}}
        result = _sdist_expected_files(vcs, hatch)
        assert "pkg/__init__.py" in result
        assert "rust/src/lib.rs" in result
        assert "Cargo.toml" in result
        assert "tests/test.py" not in result

    def test_with_exclude(self):
        vcs = ["pkg/__init__.py", "rust/target/debug/foo"]
        hatch = {"targets": {"sdist": {"only-include": ["pkg", "rust"], "exclude": ["target"]}}}
        result = _sdist_expected_files(vcs, hatch)
        assert "pkg/__init__.py" in result
        assert "rust/target/debug/foo" not in result


# ── load_config ───────────────────────────────────────────────────────


class TestLoadConfig:
    def test_missing_file(self, tmp_path):
        cfg = load_config(tmp_path / "nonexistent.toml")
        assert cfg["sdist"]["present"] == []
        assert cfg["wheel"]["absent"] == []

    def test_valid_config(self, tmp_path):
        toml = tmp_path / "pyproject.toml"
        toml.write_text(
            textwrap.dedent("""\
            [tool.check-dist.sdist]
            present = ["src"]
            absent = ["dist"]
            [tool.check-dist.wheel]
            present = ["mypkg"]
            absent = [".github"]
        """)
        )
        cfg = load_config(toml)
        assert cfg["sdist"]["present"] == ["src"]
        assert cfg["sdist"]["absent"] == ["dist"]
        assert cfg["wheel"]["present"] == ["mypkg"]
        assert cfg["wheel"]["absent"] == [".github"]

    def test_partial_config(self, tmp_path):
        toml = tmp_path / "pyproject.toml"
        toml.write_text(
            textwrap.dedent("""\
            [tool.check-dist.sdist]
            present = ["src"]
        """)
        )
        cfg = load_config(toml)
        assert cfg["sdist"]["present"] == ["src"]
        assert cfg["sdist"]["absent"] == []
        assert cfg["wheel"]["present"] == []

    def test_no_check_dist_section(self, tmp_path):
        toml = tmp_path / "pyproject.toml"
        toml.write_text("[project]\nname = 'foo'\n")
        cfg = load_config(toml)
        assert cfg == {
            "sdist": {"present": [], "absent": []},
            "wheel": {"present": [], "absent": []},
        }


class TestLoadHatchConfig:
    def test_missing_file(self, tmp_path):
        assert load_hatch_config(tmp_path / "nonexistent.toml") == {}

    def test_valid_config(self, tmp_path):
        toml = tmp_path / "pyproject.toml"
        toml.write_text(
            textwrap.dedent("""\
            [tool.hatch.build.targets.sdist]
            packages = ["mylib"]
            [tool.hatch.build.targets.wheel]
            packages = ["mylib"]
        """)
        )
        cfg = load_hatch_config(toml)
        assert cfg["targets"]["sdist"]["packages"] == ["mylib"]


# ── list_sdist_files ──────────────────────────────────────────────────


class TestListSdistFiles:
    def test_tar_gz(self, tmp_path):
        archive = tmp_path / "pkg-1.0.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            for name, content in [
                ("pkg-1.0/pyproject.toml", b"[project]\n"),
                ("pkg-1.0/src/__init__.py", b""),
            ]:
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))

        files = list_sdist_files(str(archive))
        assert files == ["pyproject.toml", "src/__init__.py"]

    def test_zip(self, tmp_path):
        archive = tmp_path / "pkg-1.0.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("pkg-1.0/pyproject.toml", "[project]\n")
            zf.writestr("pkg-1.0/src/__init__.py", "")

        files = list_sdist_files(str(archive))
        assert files == ["pyproject.toml", "src/__init__.py"]

    def test_unknown_format(self, tmp_path):
        archive = tmp_path / "pkg-1.0.rpm"
        archive.touch()
        with pytest.raises(CheckDistError, match="Unknown sdist format"):
            list_sdist_files(str(archive))


# ── list_wheel_files ──────────────────────────────────────────────────


class TestListWheelFiles:
    def test_wheel(self, tmp_path):
        archive = tmp_path / "pkg-1.0-py3-none-any.whl"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("pkg/__init__.py", "")
            zf.writestr("pkg-1.0.dist-info/METADATA", "")

        files = list_wheel_files(str(archive))
        assert files == ["pkg-1.0.dist-info/METADATA", "pkg/__init__.py"]


# ── find_dist_files ───────────────────────────────────────────────────


class TestFindDistFiles:
    def test_finds_both(self, tmp_path):
        (tmp_path / "pkg-1.0.tar.gz").touch()
        (tmp_path / "pkg-1.0-py3-none-any.whl").touch()
        sdist, wheel = find_dist_files(str(tmp_path))
        assert sdist is not None and sdist.endswith(".tar.gz")
        assert wheel is not None and wheel.endswith(".whl")

    def test_finds_zip_sdist(self, tmp_path):
        (tmp_path / "pkg-1.0.zip").touch()
        sdist, wheel = find_dist_files(str(tmp_path))
        assert sdist is not None and sdist.endswith(".zip")
        assert wheel is None

    def test_empty_dir(self, tmp_path):
        sdist, wheel = find_dist_files(str(tmp_path))
        assert sdist is None
        assert wheel is None


# ── get_vcs_files ─────────────────────────────────────────────────────


class TestGetVcsFiles:
    def test_in_git_repo(self, tmp_path):
        """Integration test: create a real tiny git repo."""
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "hello.py").write_text("print('hi')\n")
        subprocess.run(["git", "add", "hello.py"], cwd=str(tmp_path), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True, check=True)

        files = get_vcs_files(str(tmp_path))
        assert "hello.py" in files


# ── Integration: check_dist ──────────────────────────────────────────


def _make_project(tmp_path: Path, *, extra_files: dict[str, str] | None = None) -> Path:
    """Create a minimal hatch-based project for integration tests."""
    proj = tmp_path / "proj"
    pkg = proj / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "0.0.1"\n')

    pyproject = textwrap.dedent("""\
        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"

        [project]
        name = "mypkg"
        version = "0.0.1"
        readme = "README.md"

        [tool.hatch.build.targets.sdist]
        packages = ["mypkg"]

        [tool.hatch.build.targets.wheel]
        packages = ["mypkg"]

        [tool.check-dist.sdist]
        present = ["mypkg", "pyproject.toml"]
        absent = [".github"]

        [tool.check-dist.wheel]
        present = ["mypkg"]
        absent = [".github", "pyproject.toml"]
    """)
    (proj / "pyproject.toml").write_text(pyproject)
    (proj / "README.md").write_text("# mypkg\n")

    if extra_files:
        for relpath, content in extra_files.items():
            p = proj / relpath
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)

    # Set up a git repo so VCS checks work
    subprocess.run(["git", "init", str(proj)], capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(proj), capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(proj), capture_output=True)
    subprocess.run(["git", "add", "."], cwd=str(proj), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(proj), capture_output=True, check=True)
    return proj


class TestCheckDistIntegration:
    @pytest.mark.slow
    def test_clean_project_passes(self, tmp_path):
        proj = _make_project(tmp_path)
        success, messages = check_dist(str(proj), no_isolation=False)
        combined = "\n".join(messages)
        assert success, combined

    @pytest.mark.slow
    def test_missing_present_pattern(self, tmp_path):
        """A missing required file should cause failure."""
        proj = _make_project(tmp_path)
        # Rewrite config to require CHANGELOG.md which doesn't exist
        pp = proj / "pyproject.toml"
        text = pp.read_text().replace(
            'present = ["mypkg", "pyproject.toml"]',
            'present = ["mypkg", "pyproject.toml", "CHANGELOG.md"]',
        )
        pp.write_text(text)

        success, messages = check_dist(str(proj), no_isolation=False)
        combined = "\n".join(messages)
        assert not success, combined
        assert "CHANGELOG.md" in combined

    @pytest.mark.slow
    def test_verbose_lists_files(self, tmp_path):
        proj = _make_project(tmp_path)
        success, messages = check_dist(str(proj), verbose=True)
        combined = "\n".join(messages)
        # Verbose mode should list individual files
        assert "mypkg/__init__.py" in combined


# ── CLI smoke test ────────────────────────────────────────────────────


class TestCLI:
    def test_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "check_dist._cli", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "check-dist" in result.stdout.lower() or "Check Python" in result.stdout
