"""CLI."""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import AsyncIterator, List, Optional, Sequence, TextIO

from .conan import (
    TIMEOUT,
    find_conanfile,
    inspect_requires_conanfile,
    search_versions_parallel,
)
from .filter import matches_any
from .version import (
    Version,
    VersionLike,
    VersionLikeOrRange,
    VersionPart,
    VersionRange,
    find_update,
    is_semantic_version,
)

if sys.version_info >= (3, 8):
    from importlib import metadata
else:
    import importlib_metadata as metadata


class Colors:
    """ANSI color codes."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DISABLE = "\033[2m"
    UNDERLINE = "\033[4m"
    REVERSE = "\033[07m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    ORANGE = "\033[33m"
    BLUE = "\033[34m"
    PURPLE = "\033[35m"
    CYAN = "\033[36m"


def colored(txt: str, *colors: str) -> str:
    return "".join((*colors, txt, Colors.RESET))


def highlight_version_diff(version: str, compare: str, highlight=Colors.RED) -> str:
    """Highlight differing parts of version string."""
    i_first_diff = next(
        (i for i, (s1, s2) in enumerate(zip(version, compare)) if s1 != s2),
        None,
    )
    if i_first_diff is None:
        return version
    return version[:i_first_diff] + colored(version[i_first_diff:], highlight)


def text_update_column(
    current_version: Optional[VersionLike],  # None if not resolved
    update_version: Optional[Version],
    versions: Sequence[VersionLike],
) -> str:
    if not is_semantic_version(current_version):
        return ", ".join(map(str, versions))  # print list of available versions
    if update_version:
        return highlight_version_diff(str(update_version), str(current_version))
    return str(current_version)


def resolve_version(
    version_or_range: VersionLikeOrRange,
    versions: Sequence[VersionLike],
) -> Optional[VersionLike]:
    if isinstance(version_or_range, VersionRange):
        versions_semantic = list(filter(is_semantic_version, versions))
        return version_or_range.max_satifies(versions_semantic)
    return version_or_range


async def async_progressbar(
    it: AsyncIterator,
    total: int,
    desc: str = "",
    size: int = 20,
    keep: bool = False,
    file: TextIO = sys.stderr,
):
    def show(j):
        assert 0 <= j <= total
        nbar = int(size * j / total) if total > 0 else size
        perc = int(100 * j / total) if total > 0 else 100
        file.write(f"{desc}[{'=' * nbar}{'-' * (size - nbar)}] {j}/{total} {perc}%\r")
        file.flush()

    i = 0
    show(i)
    async for item in it:
        yield item
        i += 1
        show(i)

    file.write("\n" if keep else "\r")
    file.flush()


async def run(path: Path, *, package_filter: List[str], target: VersionPart, timeout: int):
    conanfile = find_conanfile(path)
    print("Checking", colored(str(conanfile), Colors.BOLD))
    refs = inspect_requires_conanfile(conanfile)
    refs_filtered = [ref for ref in refs if matches_any(ref.package, *package_filter)]

    results = [
        result
        async for result in async_progressbar(
            search_versions_parallel(refs_filtered, timeout=timeout),
            total=len(refs_filtered),
        )
    ]

    cols = {
        "cols_package": max(0, 10, *(len(str(r.ref.package)) for r in results)) + 1,
        "cols_version": max(0, 10, *(len(str(r.ref.version)) for r in results)),
    }
    format_str = "{:<{cols_package}} {:>{cols_version}}  \u2192  {}"

    for result in sorted(results, key=lambda r: r.ref.package):
        current_version_or_range = result.ref.version
        current_version_resolved = resolve_version(current_version_or_range, result.versions)

        update_version = (
            find_update(current_version_resolved, result.versions, target=target)
            if current_version_resolved
            else None
        )

        skip = is_semantic_version(current_version_resolved) and update_version is None
        if skip:
            continue

        print(
            format_str.format(
                result.ref.package,
                str(current_version_or_range),
                text_update_column(current_version_resolved, update_version, result.versions),
                **cols,
            )
        )


def get_version():
    """Get package version."""
    return metadata.version(__package__ or __name__)


def main():
    """Main function executed by conan-check-updates executable."""

    target_choices = {
        "major": VersionPart.MAJOR,
        "minor": VersionPart.MINOR,
        "patch": VersionPart.PATCH,
    }

    def list_choices(it) -> str:
        items = list(map(str, it))
        last = " or ".join(items[-2:])
        return ", ".join((*items[:-2], last))

    parser = argparse.ArgumentParser(
        "conan-check-updates",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Check for updates of your conanfile.txt/conanfile.py requirements.",
        add_help=False,
    )
    parser.add_argument(
        "filter",
        nargs="*",
        # metavar="<filter>",
        type=str,
        default=None,
        help=(
            "Include only package names matching any of the given strings or patterns.\n"
            "Wildcards (*, ?) are allowed.\n"
            "Patterns can be inverted with a prepended !, e.g. !boost*."
        ),
    )
    parser.add_argument(
        "--cwd",
        dest="cwd",
        # metavar="<path>",
        type=Path,
        default=Path("."),
        help=(
            "Path to a folder containing a recipe or to a recipe file directly "
            "(conanfile.py or conanfile.txt)."
        ),
    )
    parser.add_argument(
        "--target",
        dest="target",
        # metavar="<target>",
        choices=list(target_choices.keys()),
        default="major",
        help=f"Limit update level: {list_choices(target_choices.keys())}.",
    )
    parser.add_argument(
        "--timeout",
        # metavar="<s>",
        type=int,
        default=TIMEOUT,
        help="Timeout for `conan info|search` in seconds.",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=metadata.version(__package__ or __name__),
        help="Show the version and exit.",
    )
    parser.add_argument(
        "-h",
        "--help",
        action="help",
        help="Show this message and exit.",
    )

    args = parser.parse_args()

    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            run(
                args.cwd,
                package_filter=args.filter,
                target=target_choices.get(args.target, VersionPart.MAJOR),
                timeout=args.timeout,
            ),
        )
    except KeyboardInterrupt:
        ...
