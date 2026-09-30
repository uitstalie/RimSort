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

    ensure_cjk_fonts_in_steam_runtimes(app_dir, [font])

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

    ensure_cjk_fonts_in_steam_runtimes(app_dir, [font])
    first_manifest = _read_manifest(platform)
    backup = platform / (MANIFEST_NAME + MANIFEST_BACKUP_SUFFIX)
    first_backup = backup.read_bytes()

    ensure_cjk_fonts_in_steam_runtimes(app_dir, [font])

    assert _read_manifest(platform) == first_manifest
    assert backup.read_bytes() == first_backup


def test_updates_every_platform_directory(tmp_path: Path) -> None:
    """All runtime platform directories get the fonts."""
    app_dir = tmp_path / "steamapps" / "common"
    platforms = [
        _make_platform(app_dir, "soldier_platform_1"),
        _make_platform(app_dir, "soldier_platform_2"),
    ]
    font = _make_font(tmp_path / "fonts" / "simhei.ttf")

    ensure_cjk_fonts_in_steam_runtimes(app_dir, [font])

    for platform in platforms:
        assert (platform / "files" / FONT_SUBDIR / font.name).is_file()
        assert font.name in _read_manifest(platform)


def test_missing_steam_directory_is_ignored(tmp_path: Path) -> None:
    """A missing Steam library does not raise."""
    font = _make_font(tmp_path / "fonts" / "simhei.ttf")

    ensure_cjk_fonts_in_steam_runtimes(tmp_path / "missing", [font])


def test_missing_candidate_fonts_are_ignored(tmp_path: Path) -> None:
    """No available host font means no changes and no error."""
    app_dir = tmp_path / "steamapps" / "common"
    platform = _make_platform(app_dir, "soldier_platform_1")
    before: str = _read_manifest(platform)

    ensure_cjk_fonts_in_steam_runtimes(app_dir, [tmp_path / "fonts" / "absent.ttf"])

    assert _read_manifest(platform) == before
    assert not (platform / "files" / FONT_SUBDIR).exists()


def test_platform_without_manifest_keeps_fonts(tmp_path: Path) -> None:
    """A platform lacking a manifest still receives the font files."""
    app_dir = tmp_path / "steamapps" / "common"
    platform = app_dir / "SteamLinuxRuntime_soldier" / "soldier_platform_1"
    (platform / "files").mkdir(parents=True)
    font = _make_font(tmp_path / "fonts" / "simhei.ttf")

    ensure_cjk_fonts_in_steam_runtimes(app_dir, [font])

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
    ensure_cjk_fonts_in_steam_runtimes(app_dir, [font])

    assert _read_manifest(platform) == before
    assert not (platform / "files" / FONT_SUBDIR).exists()


def test_candidate_order_prefers_existing_files(tmp_path: Path) -> None:
    """Only existing candidates are installed, in the given order."""
    app_dir = tmp_path / "steamapps" / "common"
    platform = _make_platform(app_dir, "soldier_platform_1")
    absent = tmp_path / "fonts" / "absent.ttf"
    present = _make_font(tmp_path / "fonts" / "simhei.ttf")
    candidates: Sequence[Path] = [absent, present]

    ensure_cjk_fonts_in_steam_runtimes(app_dir, candidates)

    font_dir = platform / "files" / FONT_SUBDIR
    assert (font_dir / present.name).is_file()
    assert not (font_dir / absent.name).exists()
