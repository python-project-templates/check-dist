__version__ = "0.1.2"

from ._core import (  # noqa: F401
    CheckDistError,
    check_absent,
    check_dist,
    check_present,
    check_sdist_vs_vcs,
    check_wrong_platform_extensions,
    copier_defaults,
    find_dist_files,
    get_vcs_files,
    list_sdist_files,
    list_wheel_files,
    load_config,
    load_copier_config,
    load_hatch_config,
    matches_pattern,
    translate_extension,
)
