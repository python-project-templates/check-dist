# Configuration

`check-dist` validates your Python source distributions (sdists) and wheel distributions to ensure they contain exactly the files you expect — nothing more, nothing less.

## Quick start

Install the tool and run it from the root of your project:

```bash
pip install check-dist
check-dist
```

Or run it as a Python module:

```bash
python -m check_dist._cli
```

## How it works

1. **Build** — `check-dist` builds an sdist and a wheel using [build](https://pypi.org/project/build/).
2. **List** — It lists every file inside each archive.
3. **VCS comparison** — For the sdist, it compares the contents against files tracked by version control (currently git), taking into account any `[tool.hatch.build.targets.sdist]` configuration in `pyproject.toml`.
4. **Present / absent checks** — It verifies that files matching your `present` patterns exist and files matching your `absent` patterns do not, for both the sdist and the wheel.
5. **Platform extension checks** — It flags files that use a shared-library extension from another platform (e.g. a `.so` inside a Windows wheel).

## `pyproject.toml` configuration

All configuration lives under the `[tool.check-dist]` table, split into `sdist` and `wheel` sub-tables.

### `[tool.check-dist.sdist]`

| Key | Type | Description |
| --- | --- | --- |
| `present` | list of strings | Patterns that **must** match at least one file in the sdist. |
| `absent` | list of strings | Patterns that **must not** match any file in the sdist. |

### `[tool.check-dist.wheel]`

| Key | Type | Description |
| --- | --- | --- |
| `present` | list of strings | Patterns that **must** match at least one file in the wheel. |
| `absent` | list of strings | Patterns that **must not** match any file in the wheel. |

### Example

```toml
[tool.check-dist.sdist]
present = [
    "my_package",
    "LICENSE",
    "pyproject.toml",
    "README.md",
]
absent = [
    ".gitignore",
    ".copier-answers.yaml",
    "Makefile",
    ".github",
    "dist",
    "docs",
]

[tool.check-dist.wheel]
present = [
    "my_package",
    "LICENSE",
]
absent = [
    ".gitignore",
    ".copier-answers.yaml",
    "Makefile",
    "pyproject.toml",
    ".github",
    "dist",
    "docs",
]
```

## Pattern syntax

Patterns in `present` and `absent` lists support several matching modes:

| Pattern | Matches |
| --- | --- |
| `LICENSE` | A file named exactly `LICENSE` at any depth. |
| `my_package` | Any file whose path starts with `my_package/` (directory match). |
| `*.py` | Any file ending in `.py` (glob against the basename). |
| `my_package/*.py` | `.py` files directly inside `my_package/` (glob against the full path). |
| `.github` | Any file under `.github/`. |

Standard `fnmatch` wildcards are supported: `*`, `?`, `[seq]`, `[!seq]`.

## Platform-aware extension handling

`check-dist` automatically translates shared-library extensions across platforms so you can write a single configuration that works everywhere.

| Written in config | Linux | macOS | Windows |
| --- | --- | --- | --- |
| `*.so` | `*.so` | `*.so` | `*.pyd` |
| `*.pyd` | `*.so` | `*.so` | `*.pyd` |
| `*.dll` | `*.so` | `*.dylib` | `*.dll` |
| `*.dylib` | `*.so` | `*.dylib` | `*.dll` |

In addition, if a file with a **wrong** extension for the current platform appears in a distribution (e.g. a `.so` file in a Windows wheel), `check-dist` will raise an error.

## Hatch build integration

When your project uses [Hatch](https://hatch.pypa.io/) as the build backend,
`check-dist` reads `[tool.hatch.build.targets.sdist]` to decide which VCS-tracked files are expected in the sdist. For example, if your configuration says:

```toml
[tool.hatch.build.targets.sdist]
packages = ["my_package"]
```

then only files under `my_package/` are expected in the sdist, and VCS-tracked files outside those packages will not be flagged as missing.

The `only-include` directive is also supported:

```toml
[tool.hatch.build.targets.sdist]
only-include = [
    "my_package",
    "rust",
    "src",
    "Cargo.toml",
    "Cargo.lock",
]
exclude = ["target"]
```

This tells `check-dist` that the sdist should contain files from the listed paths, minus any excluded patterns.

> **Note:** Hatchling force-includes VCS exclusion files (e.g. `.gitignore`) in sdists regardless of exclude rules.  Do not add `.gitignore` to your `absent` list for sdist checks.

## Copier template defaults

If your project was scaffolded with a [Copier](https://copier.readthedocs.io/) template and has a `.copier-answers.yaml` file at the root, `check-dist` can derive sensible `present`/`absent` defaults automatically — **no `[tool.check-dist]` section required**.

This works when the answers file contains both:

- `project_name` — used to derive the Python module name (spaces and hyphens become underscores).
- `add_extension` — the extension type added by the template.

Supported extension types: `cpp`, `rust`, `js`, `jupyter`, `rustjswasm`, `cppjswasm`.

For example, given:

```yaml
# .copier-answers.yaml
project_name: python template rust
add_extension: rust
```

`check-dist` will automatically check for `Cargo.toml`, `rust/`, `python_template_rust/`, etc., in the sdist, and ensure build artefacts like `target/` are absent.

> An explicit `[tool.check-dist]` section in `pyproject.toml` always takes precedence over copier defaults. If you need to override or fine-tune the derived patterns, add your own configuration.

## CLI reference

```
usage: check-dist [-h] [--no-isolation] [-v] [--pre-built DIR] [source_dir]

Check Python source and wheel distributions for correctness

positional arguments:
  source_dir        Source directory (default: current directory)

options:
  -h, --help        show this help message and exit
  --no-isolation    Disable build isolation
  -v, --verbose     List every file inside each distribution
  --pre-built DIR   Use existing dist files from DIR instead of building
```

The `--pre-built` flag is useful when you have an existing build pipeline
that produces the sdist and wheel (e.g. projects with complex native
toolchains like Rust + WASM or C++ + Emscripten).  Point it at the
directory containing the `.tar.gz` and `.whl` files:

```bash
check-dist --pre-built dist/
```

If only one distribution type can be built (e.g. the wheel fails due to a
missing native compiler), `check-dist` will still run checks on whichever
dist was produced and warn about the failed build.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | All checks passed. |
| 1 | One or more checks failed. |
| 2 | An unexpected error occurred (e.g. build failure, git not found). |

## Python API

You can also use `check-dist` programmatically:

```python
from check_dist import check_dist

success, messages = check_dist(".", verbose=True)
for msg in messages:
    print(msg)
```

Key functions exposed from `check_dist`:

- `check_dist(source_dir, *, no_isolation=False, verbose=False, pre_built=None)` — run all checks, returns `(bool, list[str])`.  Pass `pre_built="dist/"` to skip building.
- `load_config(pyproject_path, *, source_dir=None)` — load `[tool.check-dist]` configuration, falling back to copier defaults when `source_dir` is provided.
- `load_copier_config(source_dir)` — load `.copier-answers.yaml` from a directory.
- `copier_defaults(copier_config)` — derive `present`/`absent` patterns from copier answers.
- `load_hatch_config(pyproject_path)` — load `[tool.hatch.build]` configuration.
- `list_sdist_files(path)` — list files in an sdist archive.
- `list_wheel_files(path)` — list files in a wheel archive.
- `get_vcs_files(source_dir)` — list git-tracked files.
- `translate_extension(pattern)` — translate a file extension for the current platform.
- `matches_pattern(filepath, pattern)` — test whether a file matches a pattern.
- `check_present(files, patterns, dist_type)` — verify required patterns are present.
- `check_absent(files, patterns, dist_type)` — verify unwanted patterns are absent.
