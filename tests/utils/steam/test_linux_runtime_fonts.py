"""Tests for the Steam Linux Runtime CJK font repair."""

import gzip
from collections.abc import Sequence
from pathlib import Path

import pytest

from app.utils.steam.linux_runtime_fonts import (
    FONT_SUBDIR,
    MANIFEST_BACKUP_SUFFIX,
    MANIFEST_NAME,
    ensure_cjk_fonts_in_steam_runtimes,
    steam_apps_dirs,
)


def _write_manifest(platform: Path) -> Path:
    """Create a minimal gzipped mtree manifest inside ``platform``."""
    manifest = platform / MANIFEST_NAME
    with gzip.open(manifest, "wt") as handle:
        handle.write("#mtree\n. type=dir\n./share type=dir\n./share/fonts type=dir\n")
    return manifest


def _read_manifest(platform: Path) -> str:
    """Return the decompressed manifest text of ``platform``."""
    with gzip.open(platform / MANIFEST_NAME, "rt") as handle:
        return handle.read()


def _make_font(path: Path) -> Path:
    """Create a fake font file with deterministic content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x01\x00\x00fake-font")
    return path


def _make_platform(apps_dir: Path, name: str) -> Path:
    """Create a fake runtime platform directory inside ``apps_dir``."""
    platform = apps_dir / "SteamLinuxRuntime_soldier" / name
    (platform / "files").mkdir(parents=True, exist_ok=True)
    _write_manifest(platform)
    return platform


def test_installs_fonts_and_manifest_entries(tmp_path: Path) -> None:
    """The font file is copied and recorded in the runtime manifest."""
    app_dir = tmp_path / "steamapps" / "common"
    platform = _make_platform(app_dir, "soldier_platform_1")
    font = _make_font(tmp_path / "fonts" / "DroidSansFallbackFull.ttf")

    ensure_cjk_fonts_in_steam_runtimes([app_dir], [font])

    installed = platform / "files" / FONT_SUBDIR / font.name
    assert installed.read_bytes() == font.read_bytes()

    manifest = _read_manifest(platform)
    assert f"./{FONT_SUBDIR.as_posix()} type=dir" in manifest
    assert f"./{FONT_SUBDIR.as_posix()}/{font.name} type=file" in manifest
    assert f"size={font.stat().st_size}" in manifest
    assert (platform / (MANIFEST_NAME + MANIFEST_BACKUP_SUFFIX)).is_file()


def test_is_idempotent(tmp_path: Path) -> None:
    """A second run changes neither the manifest nor the backup."""
    app_dir = tmp_path / "steamapps" / "common"
    platform = _make_platform(app_dir, "soldier_platform_1")
    font = _make_font(tmp_path / "fonts" / "simhei.ttf")

    ensure_cjk_fonts_in_steam_runtimes([app_dir], [font])
    first_manifest = _read_manifest(platform)
    backup = platform / (MANIFEST_NAME + MANIFEST_BACKUP_SUFFIX)
    first_backup = backup.read_bytes()

    ensure_cjk_fonts_in_steam_runtimes([app_dir], [font])

    assert _read_manifest(platform) == first_manifest
    assert backup.read_bytes() == first_backup


def test_only_newest_platform_is_repaired(tmp_path: Path) -> None:
    """Only the newest platform of a runtime is touched, to save disk space."""
    app_dir = tmp_path / "steamapps" / "common"
    old = _make_platform(app_dir, "soldier_platform_1")
    newest = _make_platform(app_dir, "soldier_platform_2")
    font = _make_font(tmp_path / "fonts" / "simhei.ttf")

    ensure_cjk_fonts_in_steam_runtimes([app_dir], [font])

    assert (newest / "files" / FONT_SUBDIR / font.name).is_file()
    assert font.name in _read_manifest(newest)
    assert not (old / "files" / FONT_SUBDIR).exists()
    assert font.name not in _read_manifest(old)


def test_shared_runtime_across_apps_dirs_is_processed_once(tmp_path: Path) -> None:
    """A runtime reachable through several Steam roots is repaired once."""
    app_dir = tmp_path / "steamapps" / "common"
    platform = _make_platform(app_dir, "soldier_platform_1")
    font = _make_font(tmp_path / "fonts" / "simhei.ttf")

    ensure_cjk_fonts_in_steam_runtimes([app_dir, app_dir], [font])

    assert _read_manifest(platform).count(f"./{FONT_SUBDIR.as_posix()} type=dir") == 1


def test_missing_steam_directory_is_ignored(tmp_path: Path) -> None:
    """A missing Steam library does not raise."""
    font = _make_font(tmp_path / "fonts" / "simhei.ttf")

    ensure_cjk_fonts_in_steam_runtimes([tmp_path / "missing"], [font])


def test_missing_candidate_fonts_are_ignored(tmp_path: Path) -> None:
    """No available host font means no changes and no error."""
    app_dir = tmp_path / "steamapps" / "common"
    platform = _make_platform(app_dir, "soldier_platform_1")
    before: str = _read_manifest(platform)

    ensure_cjk_fonts_in_steam_runtimes([app_dir], [tmp_path / "fonts" / "absent.ttf"])

    assert _read_manifest(platform) == before
    assert not (platform / "files" / FONT_SUBDIR).exists()


def test_platform_without_manifest_keeps_fonts(tmp_path: Path) -> None:
    """A platform lacking a manifest still receives the font files."""
    app_dir = tmp_path / "steamapps" / "common"
    platform = app_dir / "SteamLinuxRuntime_soldier" / "soldier_platform_1"
    (platform / "files").mkdir(parents=True)
    font = _make_font(tmp_path / "fonts" / "simhei.ttf")

    ensure_cjk_fonts_in_steam_runtimes([app_dir], [font])

    assert (platform / "files" / FONT_SUBDIR / font.name).is_file()


def test_non_linux_platforms_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repair is a no-op outside Linux."""
    import app.utils.steam.linux_runtime_fonts as module

    app_dir = tmp_path / "steamapps" / "common"
    platform = _make_platform(app_dir, "soldier_platform_1")
    font = _make_font(tmp_path / "fonts" / "simhei.ttf")
    before: str = _read_manifest(platform)

    monkeypatch.setattr(module.sys, "platform", "win32")
    ensure_cjk_fonts_in_steam_runtimes([app_dir], [font])

    assert _read_manifest(platform) == before
    assert not (platform / "files" / FONT_SUBDIR).exists()


def test_candidate_order_prefers_existing_files(tmp_path: Path) -> None:
    """Only existing candidates are installed, in the given order."""
    app_dir = tmp_path / "steamapps" / "common"
    platform = _make_platform(app_dir, "soldier_platform_1")
    absent = tmp_path / "fonts" / "absent.ttf"
    present = _make_font(tmp_path / "fonts" / "simhei.ttf")
    candidates: Sequence[Path] = [absent, present]

    ensure_cjk_fonts_in_steam_runtimes([app_dir], candidates)

    font_dir = platform / "files" / FONT_SUBDIR
    assert (font_dir / present.name).is_file()
    assert not (font_dir / absent.name).exists()


def test_steam_apps_dirs_prefers_game_library(tmp_path: Path) -> None:
    """The library holding the configured game is reported first."""
    home = tmp_path / "home"
    game_library = tmp_path / "library"
    game = game_library / "steamapps" / "common" / "RimWorld"
    game.mkdir(parents=True)
    default_common = home / ".local" / "share" / "Steam" / "steamapps" / "common"
    default_common.mkdir(parents=True)

    dirs = steam_apps_dirs(game, home=home)

    assert dirs[0] == game.parent
    assert default_common in dirs


def test_steam_apps_dirs_reads_library_folders(tmp_path: Path) -> None:
    """Secondary libraries recorded by Steam are considered."""
    home = tmp_path / "home"
    steam_root = home / ".local" / "share" / "Steam"
    (steam_root / "steamapps" / "common").mkdir(parents=True)
    other_library = tmp_path / "second-library"
    (other_library / "steamapps" / "common").mkdir(parents=True)
    (steam_root / "config").mkdir(parents=True)
    (steam_root / "config" / "libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n\t"0"\n\t{{\n\t\t"path"\t\t"{other_library}"\n\t}}\n}}\n'
    )

    dirs = steam_apps_dirs(home=home)

    assert other_library / "steamapps" / "common" in dirs


def test_steam_apps_dirs_includes_flatpak_root(tmp_path: Path) -> None:
    """A Flatpak Steam installation is found too."""
    home = tmp_path / "home"
    flatpak_common = (
        home
        / ".var"
        / "app"
        / "com.valvesoftware.Steam"
        / ".local"
        / "share"
        / "Steam"
        / "steamapps"
        / "common"
    )
    flatpak_common.mkdir(parents=True)

    assert flatpak_common in steam_apps_dirs(home=home)


def test_steam_apps_dirs_skips_missing_and_deduplicates(tmp_path: Path) -> None:
    """Missing directories are skipped and symlinked roots are not repeated."""
    home = tmp_path / "home"
    common_dir = home / ".local" / "share" / "Steam" / "steamapps" / "common"
    game = common_dir / "RimWorld"
    game.mkdir(parents=True)
    symlink = home / ".steam" / "steam"
    symlink.parent.mkdir(parents=True)
    symlink.symlink_to(home / ".local" / "share" / "Steam")

    dirs = steam_apps_dirs(game, home=home)

    assert dirs.count(common_dir) == 1
    assert all(entry.is_dir() for entry in dirs)


def test_steam_apps_dirs_survives_broken_library_manifest(tmp_path: Path) -> None:
    """An unreadable library manifest does not break discovery."""
    home = tmp_path / "home"
    steam_root = home / ".local" / "share" / "Steam"
    (steam_root / "steamapps" / "common").mkdir(parents=True)
    (steam_root / "config").mkdir(parents=True)
    (steam_root / "config" / "libraryfolders.vdf").write_text("not a vdf {{{")

    dirs = steam_apps_dirs(home=home)

    assert steam_root / "steamapps" / "common" in dirs
