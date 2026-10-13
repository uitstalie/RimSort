"""Make CJK fonts available inside Steam Linux Runtime containers.

RimWorld 1.7 replaced Unity with Ludeon's Waylith engine, and the two engines
look for OS fonts in incompatible ways, so this repair covers both:

Unity (1.6 and earlier)
    The Linux player scans ``/usr/share/fonts`` only (it does not use
    fontconfig), so a CJK font simply has to exist somewhere in that tree.
    Without one, Chinese text renders completely blank.

Waylith (1.7+)
    Text is drawn through FreeType + ImGui, and ``RimWorld.dll`` hardcodes a
    list of absolute font paths that it feeds to ``AddFontFromFileTTF``; it
    never consults fontconfig either. The CJK entries in that list are
    :data:`WAYLITH_FONT_TARGETS`. Without one of those files, the game falls
    back to a Latin font and Chinese text renders as tofu boxes.

Steam runs native Linux games inside a scout/soldier container whose
``/usr/share/fonts`` ships nothing but DejaVu (Latin) fonts, while the host
fonts are mounted at ``/run/host/fonts`` -- a path neither engine looks at.

Two details make the repair non-obvious:

* the container root is a hardlink farm of ``<runtime>/<platform>/files``, and
* the set of hardlinked files is defined by that platform's ``usr-mtree.txt.gz``
  manifest, so dropping a font into ``files/share/fonts/`` without adding a
  matching manifest entry is silently ignored.

:func:`ensure_cjk_fonts_in_steam_runtimes` copies the host CJK fonts into the
newest platform of every runtime, into the legacy ``share/fonts/cjk`` directory
and into the Waylith paths, and records all of them in the manifest. It is
idempotent and safe to call on every launch.

Note:
    Unity's directory scan ignores ``.ttc`` font collections, but Waylith's
    hardcoded list names ``.ttc`` files explicitly, so both extensions are
    installed.
"""

from __future__ import annotations

import gzip
import hashlib
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

import vdf  # type: ignore
from loguru import logger

#: Platform directories of the Steam Linux Runtimes that wrap native games.
RUNTIME_GLOBS: tuple[str, ...] = (
    "SteamLinuxRuntime_soldier/soldier_platform_*",
    "SteamLinuxRuntime_sniper/sniper_platform_*",
    "SteamLinuxRuntime_4/steamrt4_platform_*",
)

#: Steam roots on Linux, relative to the user's home directory.
STEAM_ROOT_SUFFIXES: tuple[str, ...] = (
    ".local/share/Steam",
    ".steam/steam",
    # Flatpak
    ".var/app/com.valvesoftware.Steam/.local/share/Steam",
)

#: Library manifests, relative to a Steam root.
LIBRARY_MANIFESTS: tuple[str, ...] = (
    "config/libraryfolders.vdf",
    "steamapps/libraryfolders.vdf",
)

#: Host CJK fonts, in order of preference.
DEFAULT_FONT_CANDIDATES: tuple[Path, ...] = (
    Path("/usr/share/fonts/google-droid-sans-fonts/DroidSansFallbackFull.ttf"),
    Path("/usr/local/share/fonts/s/simhei.ttf"),
    Path("/usr/share/fonts/wqy-microhei-fonts/wqy-microhei.ttc"),
    Path("/usr/share/fonts/google-noto-sans-cjk-fonts/NotoSansCJK-Regular.ttc"),
)

#: Legacy Unity font directory inside a runtime platform directory
#: (``files/`` holds the container's ``/usr``).
LEGACY_FONT_SUBDIR = Path("share/fonts/cjk")

#: Font paths RimWorld 1.7 (Waylith) hardcodes, paired with the host font whose
#: name should be installed there. Waylith feeds these to
#: ``AddFontFromFileTTF`` and never falls back to fontconfig.
WAYLITH_FONT_TARGETS: tuple[tuple[Path, str], ...] = (
    (
        Path("share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
        "DroidSansFallbackFull.ttf",
    ),
    (Path("share/fonts/droid/DroidSansFallbackFull.ttf"), "DroidSansFallbackFull.ttf"),
    (Path("share/fonts/truetype/wqy/wqy-microhei.ttc"), "wqy-microhei.ttc"),
    (
        Path("share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc"),
        "wqy-microhei.ttc",
    ),
    (Path("share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"), "NotoSansCJK-Regular.ttc"),
    (
        Path("share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        "NotoSansCJK-Regular.ttc",
    ),
)

MANIFEST_NAME = "usr-mtree.txt.gz"
MANIFEST_BACKUP_SUFFIX = ".orig"


def _deduplicate(paths: Sequence[Path]) -> list[Path]:
    """Return existing, unique directories, keeping the given order.

    :param paths: candidate directories.
    :return: directories that exist, without duplicates.
    """
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen or not path.is_dir():
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _library_apps_dirs(steam_root: Path) -> list[Path]:
    """Read a Steam root's library manifest and return its ``steamapps/common`` dirs.

    Games and runtimes may live in a secondary library, so every library path
    recorded by Steam is considered, not just the default one.

    :param steam_root: Steam installation root, e.g. ``~/.local/share/Steam``.
    :return: ``steamapps/common`` directories of the recorded libraries.
    """
    for relative in LIBRARY_MANIFESTS:
        manifest = steam_root / relative
        if not manifest.is_file():
            continue
        try:
            with manifest.open("r") as handle:
                data = vdf.load(handle)
        except Exception:
            logger.warning(
                f"Could not parse Steam library file: {manifest}", exc_info=True
            )
            return []
        libraries = data.get("libraryfolders", {})
        if not isinstance(libraries, dict):
            return []
        return [
            Path(entry["path"]) / "steamapps/common"
            for entry in libraries.values()
            if isinstance(entry, dict) and entry.get("path")
        ]
    return []


def steam_apps_dirs(
    game_install_path: Path | None = None,
    home: Path | None = None,
) -> list[Path]:
    """Locate the ``steamapps/common`` directories that may hold Steam runtimes.

    The library containing the configured game is reported first: runtimes of a
    game installed outside the default Steam library are only found that way.

    :param game_install_path: configured RimWorld folder, if known.
    :param home: home directory to search, defaults to the current user's.
    :return: existing ``steamapps/common`` directories, most relevant first.
    """
    home_dir = Path.home() if home is None else home
    candidates: list[Path] = []

    if game_install_path is not None:
        common_dir = game_install_path.parent
        if common_dir.name == "common" and common_dir.parent.name == "steamapps":
            candidates.append(common_dir)

    for suffix in STEAM_ROOT_SUFFIXES:
        steam_root = home_dir / suffix
        candidates.append(steam_root / "steamapps/common")
        candidates.extend(_library_apps_dirs(steam_root))

    return _deduplicate(candidates)


def _discover_candidates(font_candidates: Sequence[Path] | None) -> list[Path]:
    """Return the CJK fonts that exist on this host.

    :param font_candidates: override for the default candidate list.
    :return: existing candidate font files, in the given order.
    """
    candidates = DEFAULT_FONT_CANDIDATES if font_candidates is None else font_candidates
    return [path for path in candidates if path.is_file()]


def _name_key(name: str) -> str:
    """Return a case-insensitive, extension-insensitive font name key.

    Waylith's hardcoded paths and the fonts installed on a host disagree on
    casing and sometimes on the container extension, so matching ignores both.

    :param name: font file name, e.g. ``NotoSansCJK-Regular.ttf``.
    :return: normalised lookup key.
    """
    return Path(name).stem.casefold()


def waylith_font_targets(
    fonts: Sequence[Path],
) -> list[tuple[Path, Path]]:
    """Pair each Waylith font path with the host font to install there.

    :param fonts: host font files that exist.
    :return: ``(container-relative path, host font)`` pairs, in target order.
    """
    by_key: dict[str, Path] = {}
    for font in fonts:
        by_key.setdefault(_name_key(font.name), font)
    paired: list[tuple[Path, Path]] = []
    for relative, wanted_name in WAYLITH_FONT_TARGETS:
        key = _name_key(wanted_name)
        if key in by_key:
            paired.append((relative, by_key[key]))
    return paired


def _install_font(platform: Path, relative: Path, font: Path) -> Path:
    """Copy one host font to its container path, if not already present.

    :param platform: runtime platform directory (``…/soldier_platform_*``).
    :param relative: path of the font relative to the platform ``files/`` root.
    :param font: host font file to copy.
    :return: the installed font path.
    """
    target = platform / "files" / relative
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(font, target)
    return target


def _parent_dir_entries(relatives: Sequence[Path]) -> list[str]:
    """Return manifest ``type=dir`` lines for the parents of ``relatives``.

    :param relatives: container-relative file paths.
    :return: unique directory entries, parents before children.
    """
    entries: list[str] = []
    for relative in relatives:
        parts = relative.parts
        for depth in range(2, len(parts)):
            entry = f"./{'/'.join(parts[:depth])} type=dir"
            if entry not in entries:
                entries.append(entry)
    return entries


def _manifest_entry(entry_path: str, font_path: Path) -> str:
    """Build the ``mtree`` manifest line describing ``font_path``.

    :param entry_path: path of the file relative to the platform root, e.g.
        ``share/fonts/cjk/DroidSansFallbackFull.ttf``.
    :param font_path: font file on disk.
    :return: a manifest line, without trailing newline.
    """
    stat = font_path.stat()
    mode = oct(stat.st_mode)[-3:]
    digest = hashlib.sha256(font_path.read_bytes()).hexdigest()
    return (
        f"./{entry_path} type=file mode={mode} time={int(stat.st_mtime)}.0 "
        f"size={stat.st_size} sha256={digest}"
    )


def _add_manifest_entries(manifest: Path, entries: Sequence[str]) -> int:
    """Insert missing ``entries`` into a runtime manifest.

    The manifest is a gzipped ``mtree`` listing shipped with the runtime. Entries
    are appended after the last existing ``./share/fonts`` line so related paths
    stay together. The original manifest is backed up once, and nothing is
    written when every entry is already present.

    :param manifest: path to ``usr-mtree.txt.gz``.
    :param entries: manifest lines to ensure, including the directory entry.
    :return: number of entries added.
    """
    with gzip.open(manifest, "rt", errors="surrogateescape") as handle:
        lines = handle.read().splitlines()

    existing = set(lines)
    missing = [entry for entry in entries if entry not in existing]
    if not missing:
        return 0

    backup = manifest.with_name(manifest.name + MANIFEST_BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(manifest, backup)

    anchor = [
        index for index, line in enumerate(lines) if line.startswith("./share/fonts")
    ]
    insert_at = anchor[-1] + 1 if anchor else len(lines)
    with gzip.open(manifest, "wt", errors="surrogateescape") as handle:
        handle.write(
            "\n".join([*lines[:insert_at], *missing, *lines[insert_at:]]) + "\n"
        )
    return len(missing)


def _ensure_platform(platform: Path, fonts: Sequence[Path]) -> None:
    """Copy ``fonts`` into one runtime platform directory and record them.

    Fonts are installed twice: once into the legacy ``share/fonts/cjk``
    directory Unity scans (1.6 and earlier), and once into every Waylith
    hardcoded path that a discovered font can satisfy (1.7+).

    :param platform: runtime platform directory (``…/soldier_platform_*``).
    :param fonts: host font files to install.
    """
    installed: list[tuple[Path, Path]] = [
        (_install_font(platform, LEGACY_FONT_SUBDIR / font.name, font), font)
        for font in fonts
    ]
    paired = waylith_font_targets(fonts)
    installed += [
        (_install_font(platform, relative, font), font) for relative, font in paired
    ]

    manifest = platform / MANIFEST_NAME
    if not manifest.is_file():
        logger.warning(
            f"Steam Linux Runtime platform is missing its manifest, skipped: {platform}"
        )
        return

    relatives = [LEGACY_FONT_SUBDIR / font.name for font in fonts] + [
        relative for relative, _ in paired
    ]
    legacy_dir_entry = f"./{LEGACY_FONT_SUBDIR.as_posix()} type=dir"
    entries = [legacy_dir_entry]
    entries += [
        entry for entry in _parent_dir_entries(relatives) if entry != legacy_dir_entry
    ]
    entries += [
        _manifest_entry("./" + path.relative_to(platform / "files").as_posix(), path)
        for path, _ in installed
    ]
    added = _add_manifest_entries(manifest, entries)
    logger.info(
        f"Steam Linux Runtime CJK fonts ready for {platform.name}: "
        f"{len(installed)} font path(s), {added} new manifest entrie(s)"
    )


def _newest_platform(apps_dir: Path, pattern: str) -> Path | None:
    """Return the newest runtime platform directory matching ``pattern``.

    Platform directory names embed a sortable version, and Steam runs the most
    recent platform of a runtime, so older platforms are left untouched: copying
    fonts into them would only waste disk space.

    :param apps_dir: ``steamapps/common`` directory to search.
    :param pattern: glob of one runtime family, e.g. ``SteamLinuxRuntime_*/…``.
    :return: the newest matching platform directory, if any.
    """
    matches = sorted(apps_dir.glob(pattern))
    return matches[-1] if matches else None


def ensure_cjk_fonts_in_steam_runtimes(
    apps_dirs: Sequence[Path] | None = None,
    font_candidates: Sequence[Path] | None = None,
) -> None:
    """Install CJK fonts into the newest platform of every Steam Linux Runtime.

    Call this before launching the game through the Steam protocol on Linux:
    that launch path wraps the game in a container which otherwise has no font
    able to render Chinese, Japanese or Korean text.

    The function never raises: a missing Steam installation, a missing host CJK
    font or an unwritable runtime only produces a log message, because a launch
    should not be blocked by this repair.

    :param apps_dirs: ``steamapps/common`` directories to repair, defaults to
        :func:`steam_apps_dirs`.
    :param font_candidates: host font files to install, defaults to
        :data:`DEFAULT_FONT_CANDIDATES`.
    """
    if sys.platform != "linux":
        return

    roots = list(apps_dirs) if apps_dirs is not None else steam_apps_dirs()
    if not roots:
        return

    fonts = _discover_candidates(font_candidates)
    if not fonts:
        logger.warning(
            "No CJK font found on this system; Chinese text may stay blank when "
            "the game is launched through Steam"
        )
        return

    visited: set[Path] = set()
    for apps_dir in roots:
        for pattern in RUNTIME_GLOBS:
            platform = _newest_platform(Path(apps_dir), pattern)
            if platform is None:
                continue
            key = platform.resolve()
            if key in visited:
                continue
            visited.add(key)
            try:
                _ensure_platform(platform, fonts)
            except OSError:
                logger.warning(
                    f"Could not install CJK fonts into Steam runtime platform: {platform}",
                    exc_info=True,
                )
