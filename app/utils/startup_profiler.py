"""Profile RimWorld's startup and mod loading from the launcher side.

RimSort starts the game, so it is also the natural place to observe a launch: a
background thread waits for the game process, timestamps every new line of
``Player.log`` and samples the process (I/O counters, CPU time, RSS, threads).
The result is a report with the phase markers the game already logs, the biggest
stalls between consecutive log lines and a resource summary.

This complements in-game instrumentation: a mod cannot time the phase in which
mods themselves are loaded, because its own constructor runs inside
``LoadedModManager.LoadAllActiveMods``. The launcher can watch the whole run,
from process start until exit, without touching the game.

Nothing in this module is fatal: any failure is logged as a warning and the
launcher continues normally. Set ``RIMSORT_STARTUP_PROFILE=0`` to disable it.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import psutil
from loguru import logger
from platformdirs import PlatformDirs

GAME_PROCESS_NAME = "RimWorldLinux"
PROFILE_ENV_VAR = "RIMSORT_STARTUP_PROFILE"
DEFAULT_MAX_SECONDS = 1200.0
DEFAULT_POLL_SECONDS = 0.05
DEFAULT_SAMPLE_SECONDS = 1.0
DEFAULT_WRITE_SECONDS = 10.0
GAP_THRESHOLD_SECONDS = 0.2

#: Lines that are written on a fixed cadence by our own tooling (FastLoad's
#: periodic report, RimSort markers). They would otherwise show up as fake
#: "stalls" of exactly the write interval.
GAP_IGNORE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\[FastLoad\] 报告已写出"),
    re.compile(r"\[FastLoad\] \+"),
)

#: Lines the game (or its mods) already log with a timing inside.
PHASE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Prepatcher: Starting.*"),
    re.compile(r"Prepatcher: .*took .*"),
    re.compile(r"Prepatcher: Done loading"),
    re.compile(r"Prepatcher: Restarted.*"),
    re.compile(r"- Loaded All Assemblies.*"),
    re.compile(r"\[HugsLib\] version.*"),
    re.compile(r".*took [\d.]+ (?:milliseconds|seconds|ms|s)\b.*"),
    re.compile(r".*Total: [\d.]+ ms.*"),
)

#: Things worth counting in the report.
INTERESTING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("贴图加载失败", re.compile(r"Could not load Texture2D")),
    ("程序集类型加载异常", re.compile(r"ReflectionTypeLoadException")),
    ("XML/Def 错误", re.compile(r"^.*(?:XML error|Config error|Could not find).*$")),
)


@dataclass(frozen=True)
class LogEntry:
    """One timestamped log line."""

    elapsed: float
    line: str


@dataclass(frozen=True)
class Sample:
    """One process sample."""

    elapsed: float
    read_bytes: int
    write_bytes: int
    read_count: int
    write_count: int
    cpu_user: float
    cpu_system: float
    rss_bytes: int
    threads: int


@dataclass
class ProfileResult:
    """Everything collected during one launch."""

    outcome: str
    pid: int | None
    process_create_time: float | None
    start_wall: float
    end_wall: float
    entries: list[LogEntry] = field(default_factory=list)
    samples: list[Sample] = field(default_factory=list)

    @property
    def duration(self) -> float:
        """Wall-clock seconds observed (process start to last observation)."""
        if self.process_create_time is not None:
            return max(0.0, self.end_wall - self.process_create_time)
        return max(0.0, self.end_wall - self.start_wall)


class LogTailer:
    """Read new lines from a log file, tolerating truncation and recreation.

    The game rewrites ``Player.log`` when it starts, so the reader must notice
    both a changed inode and a shrunken size and restart from offset zero.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._inode: int | None = None
        self._offset = 0
        self._buffer = b""
        self.truncations = 0

    def read_new(self) -> list[str]:
        """Return complete new lines, keeping any partial line buffered."""
        try:
            stat = self._path.stat()
        except OSError:
            return []

        if self._inode != stat.st_ino or stat.st_size < self._offset:
            if self._inode is not None:
                self.truncations += 1
            self._inode = stat.st_ino
            self._offset = 0
            self._buffer = b""

        if stat.st_size <= self._offset:
            return []

        try:
            with self._path.open("rb") as handle:
                handle.seek(self._offset)
                data = handle.read()
        except OSError:
            return []

        self._offset += len(data)
        chunks = (self._buffer + data).split(b"\n")
        self._buffer = chunks.pop()
        return [
            chunk.decode("utf-8", errors="replace").rstrip("\r") for chunk in chunks
        ]


def extract_phase_notes(entries: Sequence[LogEntry]) -> list[str]:
    """Return the log lines that carry timing information, in order."""
    notes: list[str] = []
    for entry in entries:
        for pattern in PHASE_PATTERNS:
            if pattern.match(entry.line):
                notes.append(f"+{entry.elapsed:7.2f}s  {entry.line.strip()}")
                break
    return notes


def count_interesting(entries: Sequence[LogEntry]) -> dict[str, int]:
    """Count occurrences of the patterns listed in :data:`INTERESTING_PATTERNS`."""
    counts = {label: 0 for label, _ in INTERESTING_PATTERNS}
    for entry in entries:
        for label, pattern in INTERESTING_PATTERNS:
            if pattern.search(entry.line):
                counts[label] += 1
    return counts


def largest_gaps(
    entries: Sequence[LogEntry], threshold: float = GAP_THRESHOLD_SECONDS
) -> list[tuple[float, str]]:
    """Return (gap, following line) pairs for gaps at or above ``threshold``.

    Lines matching :data:`GAP_IGNORE_PATTERNS` are skipped as *anchors*: a gap
    that spans such a line is not reported, because the missing line is our own
    fixed-cadence output rather than a real stall.

    :param entries: timestamped log lines in order
    :param threshold: minimum gap in seconds to report
    :return: gaps sorted from largest to smallest
    """
    gaps: list[tuple[float, str]] = []
    previous: LogEntry | None = None
    bridged = False
    for entry in entries:
        if any(pattern.search(entry.line) for pattern in GAP_IGNORE_PATTERNS):
            bridged = True
            continue
        if previous is not None and not bridged:
            gap = entry.elapsed - previous.elapsed
            if gap >= threshold:
                gaps.append((gap, f"+{entry.elapsed:7.2f}s  {entry.line.strip()}"))
        previous = entry
        bridged = False
    gaps.sort(key=lambda item: item[0], reverse=True)
    return gaps


def render_report(
    result: ProfileResult,
    *,
    report_path: Path,
    timeline_path: Path | None,
    samples_path: Path | None,
    max_gaps: int = 25,
) -> str:
    """Render the human-readable report (pure function, unit-testable).

    :param result: collected profile data
    :param report_path: path the report will be written to (shown in the report)
    :param timeline_path: optional path of the raw timeline dump
    :param samples_path: optional path of the raw sample dump
    :param max_gaps: maximum number of stalls to list
    :return: report text
    """
    lines: list[str] = []
    lines.append("=== RimSort 启动剖析（RimWorld / Linux） ===")
    lines.append(f"结果: {result.outcome}")
    lines.append(
        f"进程: pid={result.pid}  进程起始={_fmt_time(result.process_create_time)}"
    )
    lines.append(f"观测时长: {result.duration:.1f}s   日志行数: {len(result.entries)}")
    lines.append(f"采样点数: {len(result.samples)}   报告: {report_path}")
    if timeline_path is not None:
        lines.append(f"时间线: {timeline_path}")
    if samples_path is not None:
        lines.append(f"采样原始数据: {samples_path}")

    lines.append("")
    lines.append("--- 阶段标记（日志里自带耗时/阶段的行）---")
    notes = extract_phase_notes(result.entries)
    if notes:
        lines.extend(f"  {note}" for note in notes)
    else:
        lines.append("  （无）")

    lines.append("")
    lines.append("--- 最大停顿（相邻日志行间隔）---")
    gaps = largest_gaps(result.entries)[:max_gaps]
    if gaps:
        lines.extend(f"  {gap:6.2f}s  ← {text}" for gap, text in gaps)
    else:
        lines.append("  （无 ≥0.2s 的停顿）")

    lines.append("")
    lines.append("--- 资源摘要 ---")
    if result.samples:
        last = result.samples[-1]
        peak_rss = max(sample.rss_bytes for sample in result.samples)
        peak_threads = max(sample.threads for sample in result.samples)
        lines.append(f"  读取字节: {last.read_bytes / 1048576:.1f} MB")
        lines.append(f"  写入字节: {last.write_bytes / 1048576:.1f} MB")
        lines.append(
            f"  读系统调用: {last.read_count}   写系统调用: {last.write_count}"
        )
        lines.append(
            f"  CPU 时间: user={last.cpu_user:.1f}s system={last.cpu_system:.1f}s"
        )
        lines.append(
            f"  峰值 RSS: {peak_rss / 1048576:.0f} MB   峰值线程: {peak_threads}"
        )
    else:
        lines.append("  （无采样）")

    lines.append("")
    lines.append("--- 关注项计数 ---")
    for label, count in count_interesting(result.entries).items():
        lines.append(f"  {label}: {count}")

    return "\n".join(lines) + "\n"


def _fmt_time(timestamp: float | None) -> str:
    if timestamp is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def find_game_process(name: str = GAME_PROCESS_NAME) -> psutil.Process | None:
    """Return the newest process whose name matches ``name``, if any.

    :param name: process name to look for (``RimWorldLinux`` by default)
    :return: newest matching process, or ``None``
    """
    newest: psutil.Process | None = None
    newest_create = -1.0
    for proc in psutil.process_iter(["name", "create_time"]):
        try:
            if proc.info.get("name") != name:
                continue
            create_time = float(proc.info.get("create_time") or 0.0)
            if create_time > newest_create:
                newest, newest_create = proc, create_time
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return newest


def process_exited(process: psutil.Process) -> bool:
    """Return whether the process has finished, including the zombie case.

    ``psutil.Process.is_running()`` keeps returning ``True`` for a zombie (it
    only turns ``False`` once the PID is reused), so a launcher waiting for a
    game that exited without being reaped would hang until its timeout. The
    game is launched with ``Popen`` and never waited on, so it does become a
    zombie.

    :param process: process to inspect
    :return: True when the process finished or cannot be inspected
    """
    try:
        if not process.is_running():
            return True
        return process.status() == psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True


def _take_sample(proc: psutil.Process, elapsed: float) -> Sample | None:
    try:
        io = proc.io_counters()
        times = proc.cpu_times()
        memory = proc.memory_info()
        return Sample(
            elapsed=elapsed,
            read_bytes=int(io.read_bytes),
            write_bytes=int(io.write_bytes),
            read_count=int(io.read_count),
            write_count=int(io.write_count),
            cpu_user=float(times.user),
            cpu_system=float(times.system),
            rss_bytes=int(memory.rss),
            threads=int(proc.num_threads()),
        )
    except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
        return None


class StartupProfiler:
    """Watch one game launch in a background thread.

    :param player_log_path: the game's ``Player.log`` to timestamp
    :param output_dir: directory the report/timeline/samples are written to
    :param process_name: process name to wait for
    :param max_seconds: hard cap on observation time
    :param poll_seconds: log polling interval
    :param sample_seconds: process sampling interval
    :param write_interval: how often the report is refreshed while running
    """

    def __init__(
        self,
        *,
        player_log_path: Path,
        output_dir: Path,
        process_name: str = GAME_PROCESS_NAME,
        max_seconds: float = DEFAULT_MAX_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        sample_seconds: float = DEFAULT_SAMPLE_SECONDS,
        write_interval: float = DEFAULT_WRITE_SECONDS,
    ) -> None:
        self._player_log_path = player_log_path
        self._output_dir = output_dir
        self._process_name = process_name
        self._max_seconds = max_seconds
        self._poll_seconds = poll_seconds
        self._sample_seconds = sample_seconds
        self._write_interval = write_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        """Whether the worker thread is still alive."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the background worker (idempotent)."""
        if self.running:
            return
        self._thread = threading.Thread(
            target=self._run, name="RimSortStartupProfiler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Ask the worker to finish and wait briefly for it."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        result: ProfileResult | None = None
        try:
            result = self._collect()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Startup profiling failed: {exc}")
        if result is None:
            return
        try:
            self._write_outputs(result)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Startup profiling report could not be written: {exc}")

    def _collect(self) -> ProfileResult | None:
        start_wall = time.time()
        deadline = start_wall + self._max_seconds
        result = ProfileResult(
            outcome="未发现游戏进程",
            pid=None,
            process_create_time=None,
            start_wall=start_wall,
            end_wall=start_wall,
        )

        process = self._wait_for_process(deadline)
        if process is None:
            result.end_wall = time.time()
            return result

        create_time = float(process.create_time())
        result.pid = process.pid
        result.process_create_time = create_time
        if create_time > start_wall:
            # The game started after us; align the deadline with the process.
            deadline = create_time + self._max_seconds

        tailer = LogTailer(self._player_log_path)
        next_sample = 0.0
        next_write = 0.0
        while not self._stop_event.is_set():
            if time.time() > deadline:
                result.outcome = "达到时间上限"
                break
            if process_exited(process):
                result.outcome = "游戏已退出"
                break

            for line in tailer.read_new():
                elapsed = time.time() - create_time
                result.entries.append(LogEntry(elapsed=elapsed, line=line))

            elapsed = time.time() - create_time
            if elapsed >= next_write:
                # 周期写盘：加载完成时就能看到结果，不必等进程退出
                result.end_wall = time.time()
                self._write_outputs(result)
                next_write = elapsed + self._write_interval
            if elapsed >= next_sample:
                sample = _take_sample(process, elapsed)
                if sample is not None:
                    result.samples.append(sample)
                next_sample = elapsed + self._sample_seconds

            time.sleep(self._poll_seconds)
        else:
            result.outcome = "被请求停止"

        result.end_wall = time.time()
        return result

    def _wait_for_process(self, deadline: float) -> psutil.Process | None:
        while not self._stop_event.is_set() and time.time() < deadline:
            process = find_game_process(self._process_name)
            if process is not None:
                return process
            time.sleep(0.2)
        return None

    def _write_outputs(self, result: ProfileResult) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(result.start_wall))
        report_path = self._output_dir / f"startup-{stamp}.txt"

        timeline_path: Path | None = None
        if result.entries:
            timeline_path = self._output_dir / f"timeline-{stamp}.log"
            timeline_path.write_text(
                "".join(
                    f"{entry.elapsed:8.2f}  {entry.line}\n" for entry in result.entries
                ),
                encoding="utf-8",
            )

        samples_path: Path | None = None
        if result.samples:
            samples_path = self._output_dir / f"samples-{stamp}.csv"
            header = (
                "elapsed,read_bytes,write_bytes,read_count,write_count,"
                "cpu_user,cpu_system,rss_bytes,threads"
            )
            rows = [header]
            rows.extend(
                f"{sample.elapsed:.2f},{sample.read_bytes},{sample.write_bytes},"
                f"{sample.read_count},{sample.write_count},{sample.cpu_user:.2f},"
                f"{sample.cpu_system:.2f},{sample.rss_bytes},{sample.threads}"
                for sample in result.samples
            )
            samples_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

        report = render_report(
            result,
            report_path=report_path,
            timeline_path=timeline_path,
            samples_path=samples_path,
        )
        report_path.write_text(report, encoding="utf-8")
        logger.info(f"Startup profile written to {report_path}")


def start_startup_profiler(
    *,
    player_log_path: Path | None,
    output_dir: Path | None = None,
    process_name: str = GAME_PROCESS_NAME,
) -> StartupProfiler | None:
    """Start profiling a launch, or return ``None`` when unavailable/disabled.

    Never raises: profiling is a convenience, not a launch requirement.

    :param player_log_path: the game's ``Player.log``, or ``None`` if unknown
    :param output_dir: report directory (defaults to RimSort's storage folder)
    :param process_name: process name to wait for
    :return: running profiler, or ``None``
    """
    if os.environ.get(PROFILE_ENV_VAR, "1").strip().lower() in {"0", "false", "no"}:
        logger.debug("Startup profiling disabled by environment")
        return None
    if player_log_path is None:
        logger.warning("Startup profiling skipped: Player.log path is unknown")
        return None
    try:
        if output_dir is None:
            dirs = PlatformDirs(appname="RimSort", appauthor=False)
            output_dir = Path(dirs.user_data_dir) / "startup-profiles"
        profiler = StartupProfiler(
            player_log_path=player_log_path,
            output_dir=output_dir,
            process_name=process_name,
        )
        profiler.start()
        logger.info(f"Startup profiling started (output: {output_dir})")
        return profiler
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Startup profiling could not start: {exc}")
        return None
