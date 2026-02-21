"""Core check-dist logic for checking Python source and wheel distributions."""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

import yaml

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class CheckDistError(Exception):
    """Error raised when distribution checks fail."""


# Maps file extensions from other platforms to this platform's equivalent.
# Python extension modules use .so on Linux/macOS and .pyd on Windows.
_PLATFORM_EXTENSION_MAP: dict[str, dict[str, str]] = {
    "win32": {
        ".so": ".pyd",
        ".dylib": ".dll",
    },
    "linux": {
        ".pyd": ".so",
        ".dll": ".so",
    },
    "darwin": {
        ".pyd": ".so",
        ".dll": ".dylib",
    },
}


def _get_platform_key() -> str:
    if sys.platform == "win32":
        return "win32"
    elif sys.platform == "darwin":
        return "darwin"
    return "linux"


def translate_extension(pattern: str) -> str:
    """Translate file extensions to the current platform's convention.

    For example, if a user specifies ``*.so`` on Windows, this returns ``*.pyd``.
    """
    mapping = _PLATFORM_EXTENSION_MAP.get(_get_platform_key(), {})
    for src_ext, dst_ext in mapping.items():
        if pattern.endswith(src_ext):
            return pattern[: -len(src_ext)] + dst_ext
    return pattern


def _wrong_platform_extensions() -> list[str]:
    """Return extensions that should NOT appear on the current platform."""
    mapping = _PLATFORM_EXTENSION_MAP.get(_get_platform_key(), {})
    return list(mapping.keys())


def load_config(pyproject_path: str | Path = "pyproject.toml", *, source_dir: str | Path | None = None) -> dict:
    """Load ``[tool.check-dist]`` configuration from *pyproject.toml*.

    If no ``[tool.check-dist]`` section exists and *source_dir* contains a
    ``.copier-answers.yaml`` with an ``add_extension`` key, sensible
    defaults are derived from the copier template answers.
    """
    path = Path(pyproject_path)
    empty = {
        "sdist": {"present": [], "absent": []},
        "wheel": {"present": [], "absent": []},
    }
    if not path.exists():
        # No pyproject.toml at all — try copier defaults
        if source_dir is not None:
            copier_cfg = load_copier_config(source_dir)
            defaults = copier_defaults(copier_cfg)
            if defaults is not None:
                return defaults
        return empty

    with open(path, "rb") as f:
        config = tomllib.load(f)

    cd = config.get("tool", {}).get("check-dist", {})

    # If there's no [tool.check-dist] section at all, try copier defaults
    if not cd:
        if source_dir is None:
            source_dir = path.parent
        copier_cfg = load_copier_config(source_dir)
        hatch_cfg = config.get("tool", {}).get("hatch", {}).get("build", {})
        defaults = copier_defaults(copier_cfg, hatch_config=hatch_cfg)
        if defaults is not None:
            return defaults
        return empty

    base_present = cd.get("present", [])
    base_absent = cd.get("absent", [])
    sdist_cfg = cd.get("sdist", {})
    wheel_cfg = cd.get("wheel", {})

    return {
        "sdist": {
            "present": [*base_present, *sdist_cfg.get("present", [])],
            "absent": [*base_absent, *sdist_cfg.get("absent", [])],
        },
        "wheel": {
            "present": [*base_present, *wheel_cfg.get("present", [])],
            "absent": [*base_absent, *wheel_cfg.get("absent", [])],
        },
    }


def load_hatch_config(pyproject_path: str | Path = "pyproject.toml") -> dict:
    """Load ``[tool.hatch.build]`` configuration from *pyproject.toml*."""
    path = Path(pyproject_path)
    if not path.exists():
        return {}

    with open(path, "rb") as f:
        config = tomllib.load(f)

    return config.get("tool", {}).get("hatch", {}).get("build", {})


# ── Copier template defaults ─────────────────────────────────────────

# Per-extension type defaults for sdist/wheel present/absent patterns.
# Keys follow the ``add_extension`` value in ``.copier-answers.yaml``.
_EXTENSION_DEFAULTS: dict[str, dict] = {
    "cpp": {
        "sdist_present_extra": ["cpp"],
        "sdist_absent_extra": [".clang-format"],
        "wheel_absent_extra": ["cpp"],
    },
    "rust": {
        "sdist_present_extra": ["rust", "Cargo.toml", "Cargo.lock"],
        "sdist_absent_extra": [".gitattributes", ".vscode", "target"],
        "wheel_absent_extra": ["rust", "src", "Cargo.toml"],
    },
    "js": {
        "sdist_present_extra": ["js"],
        "sdist_absent_extra": [".gitattributes", ".vscode"],
        "wheel_absent_extra": ["js"],
    },
    "jupyter": {
        "sdist_present_extra": ["js"],
        "sdist_absent_extra": [".gitattributes", ".vscode"],
        "wheel_absent_extra": ["js"],
    },
    "rustjswasm": {
        "sdist_present_extra": ["js", "rust", "Cargo.toml", "Cargo.lock"],
        "sdist_absent_extra": [".gitattributes", ".vscode", "target"],
        "wheel_absent_extra": ["js", "rust", "src", "Cargo.toml"],
    },
    "cppjswasm": {
        "sdist_present_extra": ["cpp", "js"],
        "sdist_absent_extra": [".clang-format", ".vscode"],
        "wheel_absent_extra": ["js", "cpp"],
    },
    "python": {
        "sdist_present_extra": [],
        "sdist_absent_extra": [],
        "wheel_absent_extra": [],
    },
}

# Common patterns shared across all extension types.
_COMMON_SDIST_PRESENT = ["LICENSE", "pyproject.toml", "README.md"]
_COMMON_SDIST_ABSENT = [
    ".copier-answers.yaml",
    "Makefile",
    ".github",
    "dist",
    "docs",
    "examples",
    "tests",
]
_COMMON_WHEEL_ABSENT = [
    ".gitignore",
    ".copier-answers.yaml",
    "Makefile",
    "pyproject.toml",
    ".github",
    "dist",
    "docs",
    "examples",
    "tests",
]


def load_copier_config(source_dir: str | Path) -> dict:
    """Load ``.copier-answers.yaml`` from *source_dir*, if it exists."""
    path = Path(source_dir) / ".copier-answers.yaml"
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _module_name_from_project(project_name: str) -> str:
    """Convert a human project name to a Python module name.

    Replaces spaces and hyphens with underscores.
    """
    return re.sub(r"[\s-]+", "_", project_name).strip("_")


def copier_defaults(copier_config: dict, hatch_config: dict | None = None) -> dict | None:
    """Derive default check-dist config from copier answers.

    Returns a config dict with the same shape as ``load_config`` output,
    or ``None`` if deriving defaults is not possible (no ``add_extension``
    key, or unknown extension type).

    When *hatch_config* is provided, ``sdist_present_extra`` patterns are
    filtered against the hatch sdist configuration to only require paths
    that will actually appear in the archive.  Follows hatch precedence:
    ``only-include`` (exhaustive) > ``packages`` (used as ``only-include``
    fallback) > ``include`` (filter on full tree).
    """
    extension = copier_config.get("add_extension")
    project_name = copier_config.get("project_name")
    if not extension or not project_name:
        return None

    ext_defaults = _EXTENSION_DEFAULTS.get(extension)
    if ext_defaults is None:
        return None

    module = _module_name_from_project(project_name)

    sdist_present_extra = list(ext_defaults.get("sdist_present_extra", []))

    if hatch_config:
        sdist_present_extra = _filter_extras_by_hatch(sdist_present_extra, hatch_config)

    sdist_present = [module, *sdist_present_extra, *_COMMON_SDIST_PRESENT]
    sdist_absent = [*_COMMON_SDIST_ABSENT, *ext_defaults.get("sdist_absent_extra", [])]
    wheel_present = [module]
    wheel_absent = [*_COMMON_WHEEL_ABSENT, *ext_defaults.get("wheel_absent_extra", [])]

    return {
        "sdist": {"present": sdist_present, "absent": sdist_absent},
        "wheel": {"present": wheel_present, "absent": wheel_absent},
    }


def _filter_extras_by_hatch(extras: list[str], hatch_config: dict) -> list[str]:
    """Filter copier sdist_present_extra against the hatch sdist config.

    Uses the same precedence as hatchling:
    1. ``only-include`` — exhaustive; keep extras that appear in the list.
    2. ``packages`` (when no ``only-include``) — acts as ``only-include``
       fallback; keep extras that appear in the list.
    3. ``include`` — keep extras that match an include pattern.
    4. ``force-include`` — destinations are always present; keep extras
       whose path appears as a force-include destination.
    5. No constraints — keep everything.
    """
    sdist_cfg = hatch_config.get("targets", {}).get("sdist", {})
    only_include = sdist_cfg.get("only-include")
    packages = sdist_cfg.get("packages")
    includes = sdist_cfg.get("include")
    force_include = sdist_cfg.get("force-include") or hatch_config.get("force-include") or {}

    # Collect the set of paths that will appear in the sdist.
    allowed: set[str] | None = None

    if only_include is not None:
        allowed = set(only_include)
    elif packages is not None:
        # Hatch treats packages as only-include (explicit walk).
        # include is bypassed during an explicit walk.
        allowed = set(packages)

    # force-include destinations are always present.
    force_paths = {v.strip("/") for v in force_include.values()}

    if allowed is not None:
        allowed |= force_paths
        return [p for p in extras if p in allowed]

    # include-only (no packages, no only-include): filter extras.
    if includes is not None:
        include_set = set(includes) | force_paths
        return [p for p in extras if p in include_set]

    # No restrictions — keep all extras.
    return extras


# ── Building ──────────────────────────────────────────────────────────


def build_dists(source_dir: str, output_dir: str, *, no_isolation: bool = False) -> list[str]:
    """Build sdist and wheel into *output_dir*.

    Returns a list of warnings (e.g. when only one dist type could be built).
    """
    warnings: list[str] = []
    cmd_base = [sys.executable, "-m", "build", "--outdir", output_dir]
    if no_isolation:
        cmd_base.append("--no-isolation")
    cmd_base.append(source_dir)

    # Try building both together first (fastest path)
    cmd = [sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", output_dir]
    if no_isolation:
        cmd.insert(-1, "--no-isolation")
    cmd.append(source_dir)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        return warnings

    # Combined build failed – try each target individually so that a
    # wheel-only failure (e.g. missing native toolchain) doesn't block
    # sdist checks.
    built_any = False
    for target in ("--sdist", "--wheel"):
        cmd = [sys.executable, "-m", "build", target, "--outdir", output_dir]
        if no_isolation:
            cmd.insert(-1, "--no-isolation")
        cmd.append(source_dir)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            built_any = True
        else:
            kind = "sdist" if target == "--sdist" else "wheel"
            warnings.append(f"Warning: {kind} build failed:\n{r.stderr.strip()}")

    if not built_any:
        raise CheckDistError(f"Build failed:\n{result.stdout}\n{result.stderr}")

    return warnings


def find_dist_files(output_dir: str) -> tuple[str | None, str | None]:
    """Return ``(sdist_path, wheel_path)`` found in *output_dir*."""
    sdist_path = None
    wheel_path = None
    if not os.path.isdir(output_dir):
        return None, None
    for name in os.listdir(output_dir):
        if name.endswith((".tar.gz", ".zip")):
            sdist_path = os.path.join(output_dir, name)
        elif name.endswith(".whl"):
            wheel_path = os.path.join(output_dir, name)
    return sdist_path, wheel_path


_DEFAULT_DIST_DIRS = ["dist", "wheelhouse"]


def _find_pre_built(source_dir: str) -> str | None:
    """Search default directories for pre-built distributions.

    Checks ``dist/`` and ``wheelhouse/`` under *source_dir*.  Returns the
    first directory that contains at least one sdist or wheel, or ``None``.
    """
    for dirname in _DEFAULT_DIST_DIRS:
        candidate = os.path.join(source_dir, dirname)
        sdist, wheel = find_dist_files(candidate)
        if sdist or wheel:
            return candidate
    return None


# ── Listing files ─────────────────────────────────────────────────────


def list_sdist_files(sdist_path: str) -> list[str]:
    """List files inside an sdist, stripping the top-level directory."""
    files: list[str] = []
    if sdist_path.endswith(".tar.gz"):
        with tarfile.open(sdist_path) as tf:
            for member in tf.getmembers():
                if member.isfile():
                    parts = member.name.split("/", 1)
                    files.append(parts[1] if len(parts) > 1 else parts[0])
    elif sdist_path.endswith(".zip"):
        with zipfile.ZipFile(sdist_path) as zf:
            for name in zf.namelist():
                if not name.endswith("/"):
                    parts = name.split("/", 1)
                    files.append(parts[1] if len(parts) > 1 else parts[0])
    else:
        raise CheckDistError(f"Unknown sdist format: {sdist_path}")
    return sorted(files)


def list_wheel_files(wheel_path: str) -> list[str]:
    """List files inside a wheel."""
    with zipfile.ZipFile(wheel_path) as zf:
        return sorted(name for name in zf.namelist() if not name.endswith("/"))


# ── VCS integration ───────────────────────────────────────────────────


def get_vcs_files(source_dir: str) -> list[str]:
    """Return files tracked by git in *source_dir*."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--recurse-submodules"],
            capture_output=True,
            text=True,
            cwd=source_dir,
        )
    except FileNotFoundError:
        raise CheckDistError("git not found – only git is currently supported for VCS tracking")
    if result.returncode != 0:
        raise CheckDistError(f"git ls-files failed:\n{result.stderr}")
    return sorted(f for f in result.stdout.split("\0") if f)


# ── Pattern matching ──────────────────────────────────────────────────


def matches_pattern(filepath: str, pattern: str) -> bool:
    """Check whether *filepath* matches *pattern*.

    * A bare name like ``check_dist`` matches any file whose path starts
      with ``check_dist/``.
    * Glob wildcards (``*``, ``?``, ``[…]``) are matched against both the
      full path and the basename.
    * Extensions are translated to the current platform before matching.
    """
    translated = translate_extension(pattern)
    is_glob = any(c in translated for c in "*?[")

    if not is_glob:
        if filepath == translated:
            return True
        if filepath.startswith(translated + "/"):
            return True
        if os.path.basename(filepath) == translated:
            return True

    if fnmatch.fnmatch(filepath, translated):
        return True
    if fnmatch.fnmatch(os.path.basename(filepath), translated):
        return True

    return False


def _matches_hatch_pattern(filepath: str, pattern: str) -> bool:
    """Match a file path against a hatch include/exclude pattern.

    Hatch patterns may start with ``/`` (root-relative). Bare names match
    as either an exact file, a directory prefix, or any path component.
    """
    pat = pattern.lstrip("/")
    if filepath == pat:
        return True
    if filepath.startswith(pat.rstrip("/") + "/"):
        return True
    # Match as intermediate directory component (e.g. "target" in "rust/target/debug/foo")
    component = pat.rstrip("/") + "/"
    if "/" + component in "/" + filepath + "/":
        return True
    if fnmatch.fnmatch(filepath, pat):
        return True
    if fnmatch.fnmatch(os.path.basename(filepath), pat):
        return True
    return False


# ── Checking helpers ──────────────────────────────────────────────────


def check_present(files: list[str], patterns: list[str], dist_type: str) -> list[str]:
    """Return error strings for any *patterns* not found in *files*."""
    errors: list[str] = []
    for pattern in patterns:
        translated = translate_extension(pattern)
        if not any(matches_pattern(f, pattern) for f in files):
            msg = f"{dist_type}: required pattern '{pattern}' not found"
            if translated != pattern:
                msg += f" (translated to '{translated}' for {sys.platform})"
            errors.append(msg)
    return errors


def check_absent(files: list[str], patterns: list[str], dist_type: str, *, present_patterns: list[str] | None = None) -> list[str]:
    """Return error strings for any *patterns* found in *files*.

    When *present_patterns* is given, files nested inside a directory
    that matches a present pattern are not flagged.  This avoids false
    positives like ``lerna/tests/fake_package/pyproject.toml`` being
    flagged as unwanted when ``lerna`` is a required present pattern.
    """
    errors: list[str] = []
    for pattern in patterns:
        translated = translate_extension(pattern)
        matching = [f for f in files if matches_pattern(f, pattern)]
        if matching and present_patterns:
            matching = [f for f in matching if not any(matches_pattern(f, pp) for pp in present_patterns)]
        if matching:
            msg = f"{dist_type}: unwanted pattern '{pattern}' matched: {', '.join(matching)}"
            if translated != pattern:
                msg += f" (translated to '{translated}' for {sys.platform})"
            errors.append(msg)
    return errors


def check_wrong_platform_extensions(files: list[str], dist_type: str) -> list[str]:
    """Flag files that use an extension from another platform."""
    wrong_exts = _wrong_platform_extensions()
    errors: list[str] = []
    for f in files:
        for ext in wrong_exts:
            if f.endswith(ext):
                correct = translate_extension(f)
                errors.append(
                    f"{dist_type}: '{f}' uses extension '{ext}' which is incorrect for {sys.platform} (expected '{os.path.splitext(correct)[1]}')"
                )
    return errors


# Patterns for top-level files hatch automatically includes in sdists.
_HATCH_AUTO_INCLUDE_PATTERNS = [
    "pyproject.toml",
    "README*",
    "LICENSE*",
    "COPYING*",
    "NOTICE*",
    "AUTHORS*",
]


def _is_hatch_auto_included(filename: str) -> bool:
    """Return True if *filename* is a top-level file hatch auto-includes."""
    if "/" in filename:
        return False
    return any(fnmatch.fnmatch(filename, pat) for pat in _HATCH_AUTO_INCLUDE_PATTERNS)


def _sdist_expected_files(vcs_files: list[str], hatch_config: dict) -> set[str]:
    """Derive the set of VCS files we expect to see in the sdist,
    taking ``[tool.hatch.build.targets.sdist]`` into account.

    Follows hatchling's precedence:

    1. ``only-include`` is an exhaustive list of paths to walk.
    2. If absent, ``packages`` is used as ``only-include`` (hatch fallback).
    3. If neither is set and ``include`` is present, only files matching
       ``include`` patterns are kept.
    4. ``exclude`` always removes files (except force-included ones).
    5. ``force-include`` entries are always present regardless of other
       settings.  Hatch also auto-force-includes ``pyproject.toml``,
       ``.gitignore``, README, and LICENSE files.
    """
    sdist_cfg = hatch_config.get("targets", {}).get("sdist", {})
    only_include = sdist_cfg.get("only-include")
    packages = sdist_cfg.get("packages")
    includes = sdist_cfg.get("include", [])
    excludes = sdist_cfg.get("exclude", [])
    force_include = sdist_cfg.get("force-include", {})

    # Also pick up global-level force-include (target overrides global,
    # but if only global is set, use it).
    if not force_include:
        force_include = hatch_config.get("force-include", {})

    # Step 1: Determine base set from only-include / packages / include.
    # Hatch's fallback: only_include = configured or (packages or [])
    # When truthy, only those directory roots are walked.
    scan_paths = only_include if only_include is not None else packages

    expected = set()
    if scan_paths is not None:
        for f in vcs_files:
            if any(f == p or f.startswith(p.rstrip("/") + "/") for p in scan_paths):
                expected.add(f)
    elif includes:
        # No only-include or packages: full tree walk, but include
        # patterns act as a filter.
        for f in vcs_files:
            if any(_matches_hatch_pattern(f, inc) for inc in includes):
                expected.add(f)
    else:
        # No restrictions — everything in VCS is expected.
        expected = set(vcs_files)

    # Step 2: Apply excludes (never affects force-include).
    if excludes:
        expected = {f for f in expected if not any(_matches_hatch_pattern(f, exc) for exc in excludes)}

    # Step 3: Add force-include destinations.
    # force-include is {source: dest} — we care about the dest paths
    # since those are what appear in the archive.
    for dest in force_include.values():
        dest = dest.strip("/")
        # If the dest matches a VCS file, add it.
        for f in vcs_files:
            if f == dest or f.startswith(dest.rstrip("/") + "/"):
                expected.add(f)

    return expected


_GENERATED_SDIST_FILES = {"PKG-INFO"}


def check_sdist_vs_vcs(
    sdist_files: list[str],
    vcs_files: list[str],
    hatch_config: dict,
    sdist_absent: list[str] | None = None,
) -> list[str]:
    """Compare sdist contents against VCS-tracked files."""
    errors: list[str] = []
    expected = _sdist_expected_files(vcs_files, hatch_config)
    vcs_set = set(vcs_files)

    # Clean sdist set: remove generated metadata
    sdist_set = {f for f in sdist_files if f not in _GENERATED_SDIST_FILES and ".egg-info/" not in f and not f.endswith(".egg-info")}

    # Artifacts are built files not in VCS but expected in dists
    artifacts = hatch_config.get("artifacts", [])

    # "Extra" = files in sdist that are neither VCS-tracked nor
    # generated artifacts.  This catches truly stray files.
    extra = sorted(sdist_set - vcs_set)
    if artifacts:
        extra = [f for f in extra if not any(matches_pattern(f, art) for art in artifacts)]

    # "Missing" = VCS source files (under packages / includes) that
    # are absent from the sdist.
    missing = sorted(expected - sdist_set)
    # Filter common non-issues (dotfiles like .gitattributes)
    missing = [f for f in missing if not f.startswith(".")]
    # Filter files that match the user's sdist absent patterns —
    # if a file is explicitly expected to be absent, it's not "missing".
    # Always include the common absent patterns (docs, tests, etc.) since
    # most build systems exclude these from sdists.
    all_absent = list(_COMMON_SDIST_ABSENT)
    if sdist_absent:
        all_absent.extend(sdist_absent)
    missing = [f for f in missing if not any(matches_pattern(f, pat) for pat in all_absent)]

    if extra:
        errors.append("\nsdist contains files not tracked by VCS:\n\t" + "\n\t".join(extra))
    if missing:
        errors.append("\nVCS-tracked files missing from sdist: \n\t" + "\n\t".join(missing))
    return errors


# ── Main entry point ──────────────────────────────────────────────────


def check_dist(
    source_dir: str = ".",
    *,
    no_isolation: bool = False,
    verbose: bool = False,
    pre_built: str | None = None,
    rebuild: bool = False,
) -> tuple[bool, list[str]]:
    """Run all distribution checks.

    Parameters
    ----------
    source_dir:
        Path to the project root.
    no_isolation:
        Passed to ``python -m build --no-isolation``.
    verbose:
        List every file in each distribution.
    pre_built:
        If given, skip building and use existing dist files from this
        directory.  Useful when native toolchains have already produced
        the archives.
    rebuild:
        Force a fresh build even when pre-built distributions exist in
        ``dist/`` or ``wheelhouse/``.

    Returns ``(success, messages)``.
    """
    messages: list[str] = []
    errors: list[str] = []
    source_dir = os.path.abspath(source_dir)

    pyproject_path = os.path.join(source_dir, "pyproject.toml")
    config = load_config(pyproject_path, source_dir=source_dir)
    hatch_config = load_hatch_config(pyproject_path)

    if pre_built is not None:
        dist_dir = os.path.abspath(pre_built)
        messages.append(f"Using pre-built distributions from {dist_dir}")
        sdist_path, wheel_path = find_dist_files(dist_dir)
    elif not rebuild:
        # Auto-detect pre-built dists in dist/ or wheelhouse/
        detected = _find_pre_built(source_dir)
        if detected is not None:
            dist_dir = detected
            messages.append(f"Using pre-built distributions from {dist_dir}")
            sdist_path, wheel_path = find_dist_files(dist_dir)
            pre_built = dist_dir  # so downstream logic treats it as pre-built
        else:
            pre_built = None  # fall through to build
    else:
        pre_built = None  # --rebuild: ignore any existing dists

    if pre_built is None:
        tmpdir_ctx = tempfile.TemporaryDirectory(prefix="check-dist-")
        tmpdir = tmpdir_ctx.__enter__()
        try:
            messages.append("Building distributions...")
            build_warnings = build_dists(source_dir, tmpdir, no_isolation=no_isolation)
            for w in build_warnings:
                messages.append(f"  {w}")
            sdist_path, wheel_path = find_dist_files(tmpdir)
        except Exception:
            tmpdir_ctx.__exit__(None, None, None)
            raise

    try:
        if not sdist_path and not wheel_path:
            errors.append("No distributions found after build")
        elif pre_built is not None:
            if not sdist_path:
                errors.append("No sdist found in pre-built directory")
            if not wheel_path:
                errors.append("No wheel found in pre-built directory")

        # ── sdist checks ─────────────────────────────────────────
        if sdist_path:
            sdist_files = list_sdist_files(sdist_path)
            messages.append(f"\nsdist ({os.path.basename(sdist_path)}) – {len(sdist_files)} file(s):")
            if verbose:
                for f in sdist_files:
                    messages.append(f"  {f}")

            try:
                vcs_files = get_vcs_files(source_dir)
                errors.extend(check_sdist_vs_vcs(sdist_files, vcs_files, hatch_config, sdist_absent=config["sdist"]["absent"]))
            except CheckDistError as exc:
                messages.append(f"  Warning: could not compare against VCS: {exc}")

            errors.extend(check_present(sdist_files, config["sdist"]["present"], "sdist"))
            errors.extend(check_absent(sdist_files, config["sdist"]["absent"], "sdist", present_patterns=config["sdist"]["present"]))
            errors.extend(check_wrong_platform_extensions(sdist_files, "sdist"))

        # ── wheel checks ─────────────────────────────────────────
        if wheel_path:
            wheel_files = list_wheel_files(wheel_path)
            messages.append(f"\nwheel ({os.path.basename(wheel_path)}) – {len(wheel_files)} file(s):")
            if verbose:
                for f in wheel_files:
                    messages.append(f"  {f}")

            errors.extend(check_present(wheel_files, config["wheel"]["present"], "wheel"))
            errors.extend(check_absent(wheel_files, config["wheel"]["absent"], "wheel", present_patterns=config["wheel"]["present"]))
            errors.extend(check_wrong_platform_extensions(wheel_files, "wheel"))
    finally:
        if pre_built is None:
            tmpdir_ctx.__exit__(None, None, None)

    if errors:
        messages.append(f"\n{len(errors)} error(s) found:")
        for err in errors:
            messages.append(f"  ERROR: {err}")
        return False, messages

    messages.append("\nAll checks passed!")
    return True, messages
