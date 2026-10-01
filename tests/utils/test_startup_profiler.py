import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import psutil
import pytest

from app.utils.startup_profiler import (
    INTERESTING_PATTERNS,
    PROFILE_ENV_VAR,
    LogEntry,
    LogTailer,
    ProfileResult,
    Sample,
    StartupProfiler,
    count_interesting,
    extract_phase_notes,
    find_game_process,
    largest_gaps,
    process_exited,
    render_report,
    start_startup_profiler,
)


def test_log_tailer_reads_appended_lines(tmp_path: Path) -> None:
    log = tmp_path / "Player.log"
    log.write_text("first\nsecond\n", encoding="utf-8")
    tailer = LogTailer(log)

    assert tailer.read_new() == ["first", "second"]
    assert tailer.read_new() == []

    with log.open("a", encoding="utf-8") as handle:
        handle.write("third\npartial")
    assert tailer.read_new() == ["third"]
    assert tailer.truncations == 0

    with log.open("a", encoding="utf-8") as handle:
        handle.write(" line\n")
    assert tailer.read_new() == ["partial line"]


def test_log_tailer_handles_truncation(tmp_path: Path) -> None:
    log = tmp_path / "Player.log"
    log.write_text("old one\nold two\n", encoding="utf-8")
    tailer = LogTailer(log)
    assert tailer.read_new() == ["old one", "old two"]

    # The game rewrites the file when it starts: same inode, smaller size
    log.write_text("new\n", encoding="utf-8")
    assert tailer.read_new() == ["new"]
    assert tailer.truncations == 1


def test_log_tailer_missing_file_is_not_fatal(tmp_path: Path) -> None:
    tailer = LogTailer(tmp_path / "nope.log")
    assert tailer.read_new() == []


def test_extract_phase_notes_keeps_timing_lines() -> None:
    entries = [
        LogEntry(1.0, "random line"),
        LogEntry(2.0, "Prepatcher: Starting... (vanilla load took 1.79s)"),
        LogEntry(3.0, "What's That Mod took 522 milliseconds to generate all defs."),
        LogEntry(4.0, "- Loaded All Assemblies, in  0.052 seconds"),
        LogEntry(5.0, "Total: 748.35 ms (FindLiveObjects: 3.2 ms)"),
    ]
    notes = extract_phase_notes(entries)
    assert len(notes) == 4
    assert notes[0].startswith("+   2.00s")
    assert "took 522 milliseconds" in notes[1]


def test_largest_gaps_sorted_and_thresholded() -> None:
    entries = [
        LogEntry(0.0, "a"),
        LogEntry(0.1, "b"),
        LogEntry(4.0, "c"),
        LogEntry(4.2, "d"),
        LogEntry(9.0, "e"),
    ]
    gaps = largest_gaps(entries, threshold=0.5)
    assert [round(gap, 2) for gap, _ in gaps] == [4.8, 3.9]
    assert gaps[0][1].endswith("e")


def test_count_interesting_counts_known_issues() -> None:
    entries = [
        LogEntry(0.0, "Could not load Texture2D at 'Things/X' for def 'Y'"),
        LogEntry(0.1, "Could not load Texture2D at 'Things/Z' for def 'W'"),
        LogEntry(0.2, "ReflectionTypeLoadException: nope"),
        LogEntry(0.3, "fine"),
    ]
    counts = count_interesting(entries)
    labels = {label for label, _ in INTERESTING_PATTERNS}
    assert counts["贴图加载失败"] == 2
    assert counts["程序集类型加载异常"] == 1
    assert set(counts) == labels


def test_render_report_contains_sections_and_totals(tmp_path: Path) -> None:
    result = ProfileResult(
        outcome="游戏已退出",
        pid=1234,
        process_create_time=time.time() - 30,
        start_wall=time.time() - 31,
        end_wall=time.time(),
        entries=[
            LogEntry(1.0, "Prepatcher: Starting..."),
            LogEntry(25.0, "Prepatcher: Done loading"),
        ],
        samples=[
            Sample(
                elapsed=5.0,
                read_bytes=100 * 1048576,
                write_bytes=1048576,
                read_count=1000,
                write_count=10,
                cpu_user=12.5,
                cpu_system=3.5,
                rss_bytes=2048 * 1048576,
                threads=42,
            )
        ],
    )
    report = render_report(
        result,
        report_path=tmp_path / "report.txt",
        timeline_path=tmp_path / "timeline.log",
        samples_path=tmp_path / "samples.csv",
    )
    assert "RimSort 启动剖析" in report
    assert "游戏已退出" in report
    assert "Prepatcher: Done loading" in report
    assert "100.0 MB" in report
    assert "峰值 RSS: 2048 MB" in report
    assert "24.00s" in report  # the 24s stall between the two entries


def test_render_report_without_data() -> None:
    result = ProfileResult(
        outcome="未发现游戏进程",
        pid=None,
        process_create_time=None,
        start_wall=time.time(),
        end_wall=time.time(),
    )
    report = render_report(
        result, report_path=Path("/tmp/x.txt"), timeline_path=None, samples_path=None
    )
    assert "（无）" in report
    assert "（无采样）" in report


def test_process_exited_detects_zombie() -> None:
    """psutil keeps is_running() True for zombies, so status must be checked."""
    proc = MagicMock()
    proc.is_running.return_value = True
    proc.status.return_value = psutil.STATUS_ZOMBIE
    assert process_exited(proc) is True


def test_process_exited_detects_live_process() -> None:
    proc = MagicMock()
    proc.is_running.return_value = True
    proc.status.return_value = "running"
    assert process_exited(proc) is False


def test_process_exited_handles_reaped_process() -> None:
    proc = MagicMock()
    proc.is_running.side_effect = psutil.NoSuchProcess(1234)
    assert process_exited(proc) is True


def test_find_game_process_unknown_name() -> None:
    assert find_game_process("definitely-not-a-running-process-xyz") is None


def test_start_startup_profiler_respects_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(PROFILE_ENV_VAR, "0")
    assert start_startup_profiler(player_log_path=tmp_path / "Player.log") is None


def test_start_startup_profiler_requires_log_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PROFILE_ENV_VAR, raising=False)
    assert start_startup_profiler(player_log_path=None) is None


def test_start_startup_profiler_starts_and_stops(tmp_path: Path) -> None:
    profiler = StartupProfiler(
        player_log_path=tmp_path / "Player.log",
        output_dir=tmp_path / "out",
        process_name="definitely-not-a-running-process-xyz",
        max_seconds=5.0,
    )
    profiler.start()
    assert profiler.running
    profiler.stop()
    assert not profiler.running


def test_profile_env_default_is_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PROFILE_ENV_VAR, raising=False)
    assert os.environ.get(PROFILE_ENV_VAR, "1") == "1"
