"""
Log files shared by the API and the gateway: one file per week.

    <log_dir>/19102026_25102026.log
    <log_dir>/26102026_01112026.log

A week runs Monday to Sunday, IST; its file is named by those two dates,
ddmmyyyy.
Both processes append to the same file -- each line says which one wrote it
([api.access], [gps_gateway.session], ...) -- and each record goes out in a
single append, so lines from the two never interleave. The files hold
warnings and errors only; whether warnings are written too is the log
level, ERROR by default, so they stay small enough to read.

The level lives in `<log_dir>/log-level` (one word: ERROR, WARNING, ...),
written by `PUT /api/v1/logs/level`, read by both processes at start and
re-read every LEVEL_POLL_SECONDS: a change needs no restart and survives
one. Without that file each process uses its environment default
(API_LOG_LEVEL / GATEWAY_LOG_LEVEL).

Files older than the retention period are deleted whenever a process starts
or a new week begins, so the directory needs no cron job.

At the repository root rather than in api/ or gps_gateway/ because both
images copy it: the two processes must agree on the file names, and the
API's log endpoints read what the gateway wrote.
"""

import logging
import os
import re
from datetime import date, datetime, timedelta, timezone

# India has no daylight saving, so a fixed offset is exact.
IST = timezone(timedelta(hours=5, minutes=30), "IST")
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
LEVEL_FILE = "log-level"
LEVEL_POLL_SECONDS = 30
DEFAULT_RETENTION_WEEKS = 4
MAX_RETENTION_WEEKS = 12
# "19102026_25102026.log" (Monday_Sunday, ddmmyyyy), or ".packets.log" for
# the gateway's opt-in per-packet log.
WEEK_FILE = re.compile(r"^(\d{8})_(\d{8})(\.packets)?\.log$")
DATE_FORMAT = "%d%m%Y"


def week_start(moment: datetime) -> date:
    """The Monday (IST) of the week `moment` falls in."""
    day = moment.astimezone(IST).date()
    return day - timedelta(days=day.weekday())


def week_file(monday: date, kind: str = "") -> str:
    """`19102026_25102026.log` (kind "packets": `..._25102026.packets.log`)."""
    suffix = f".{kind}.log" if kind else ".log"
    sunday = monday + timedelta(days=6)
    return f"{monday.strftime(DATE_FORMAT)}_{sunday.strftime(DATE_FORMAT)}{suffix}"


def _parse(name: str) -> tuple[date, date, bool] | None:
    """A week file's Monday, Sunday and whether it is the packet log; None if it is not one."""
    match = WEEK_FILE.match(name)
    if not match:
        return None
    try:
        start = datetime.strptime(match.group(1), DATE_FORMAT).date()
        end = datetime.strptime(match.group(2), DATE_FORMAT).date()
    except ValueError:
        return None
    return start, end, bool(match.group(3))


def retention_weeks(value: str | None, default: int = DEFAULT_RETENTION_WEEKS) -> int:
    try:
        weeks = int(value) if value not in (None, "") else default
    except ValueError:
        weeks = default
    return min(MAX_RETENTION_WEEKS, max(1, weeks))


class IstFormatter(logging.Formatter):
    """Timestamps as `2026-10-22 11:25:32 IST`, whatever the server's timezone."""

    def formatTime(self, record, datefmt=None):  # noqa: N802 - logging's API
        return datetime.fromtimestamp(record.created, IST).strftime("%Y-%m-%d %H:%M:%S IST")


class WeeklyFileHandler(logging.Handler):
    """
    Appends each record to its week's file in `base_dir`. Warnings and
    errors only, whatever the logger lets through -- except the per-packet
    log (kind "packets"), which is asked for explicitly and written whole.
    """

    def __init__(self, base_dir: str, keep_weeks: int, kind: str = ""):
        super().__init__(logging.INFO if kind else logging.WARNING)
        self.base_dir = base_dir
        self.kind = kind
        self.keep_weeks = keep_weeks
        self._week: date | None = None
        self._fd: int | None = None
        os.makedirs(base_dir, exist_ok=True)
        prune(base_dir, keep_weeks)

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < self.level:
            return  # also when handed a record directly, not through a logger
        try:
            week = week_start(datetime.fromtimestamp(record.created, IST))
            if week != self._week:
                self._open(week)
            # One write() of the whole record on an O_APPEND file: the other
            # process's lines land before or after it, never inside it.
            os.write(self._fd, (self.format(record) + "\n").encode("utf-8"))
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)

    def _open(self, week: date) -> None:
        if self._fd is not None:
            os.close(self._fd)
        path = os.path.join(self.base_dir, week_file(week, self.kind))
        self._fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        if self._week is not None:
            prune(self.base_dir, self.keep_weeks, week)
        self._week = week

    def close(self) -> None:
        self.acquire()
        try:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
        finally:
            self.release()
        super().close()


def prune(base_dir: str, keep_weeks: int, current: date | None = None) -> None:
    """Deletes the files of weeks that started more than `keep_weeks` weeks before this one."""
    current = current or week_start(datetime.now(IST))
    oldest = current - timedelta(weeks=keep_weeks - 1)
    try:
        names = os.listdir(base_dir)
    except OSError:
        return
    for name in names:
        week = _parse(name)
        if week and week[0] < oldest:
            try:
                os.remove(os.path.join(base_dir, name))
            except OSError:
                pass  # the other process got there first


def read_level(base_dir: str, default: str) -> str:
    """The level saved in `<base_dir>/log-level`, or `default` when there is none."""
    if not base_dir:
        return default
    try:
        with open(os.path.join(base_dir, LEVEL_FILE), encoding="utf-8") as f:
            name = f.read().strip().upper()
    except OSError:
        return default
    return name if name in LEVELS else default


def write_level(base_dir: str, level: str) -> None:
    """Saves the level for both processes (replaced atomically, never half-written)."""
    os.makedirs(base_dir, exist_ok=True)
    path = os.path.join(base_dir, LEVEL_FILE)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(level + "\n")
    os.replace(tmp, path)


def list_logs(base_dir: str) -> list[dict]:
    """Every week's file, newest first."""
    try:
        names = os.listdir(base_dir)
    except OSError:
        return []
    files = []
    for name in names:
        week = _parse(name)
        path = os.path.join(base_dir, name)
        if week and os.path.isfile(path):
            stat = os.stat(path)
            files.append(
                {
                    "name": name,
                    "week_start": week[0],
                    "week_end": week[1],
                    "packets": week[2],
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc),
                }
            )
    return sorted(files, key=lambda f: (f["week_start"], not f["packets"]), reverse=True)


def log_path(base_dir: str, name: str) -> str | None:
    """The week file called `name`, or None -- never a path outside `base_dir`."""
    if _parse(name) is None:
        return None
    path = os.path.join(base_dir, name)
    return path if os.path.isfile(path) else None


class LevelWatcher:
    """Applies `<base_dir>/log-level` to the root logger whenever it changes."""

    def __init__(self, base_dir: str, default: str):
        self.base_dir = base_dir
        self.default = default
        self.current = read_level(base_dir, default)

    def apply(self) -> bool:
        """Re-reads the file; True if the level changed."""
        level = read_level(self.base_dir, self.default)
        logging.getLogger().setLevel(level)
        changed = level != self.current
        self.current = level
        return changed


# "2026-10-22 10:00:00 IST ERROR   [api.error] [3f9a1c0b7e21] it broke" (the
# API's), "... IST ERROR [gps_gateway.sink] write failed" (the gateway's), or
# "... IST IN   868720065896205  LOCATION ..." (the packet log's).
_RECORD = re.compile(r"^(\d{4}-\d{2}-\d{2} \d\d:\d\d:\d\d) IST (\S+)\s*(.*)$")
_LOGGER = re.compile(r"^\[([^\]]+)\]\s*(?:\[([0-9a-f]{12}|-)\]\s*)?(.*)$", re.S)


def read_entries(path: str, max_bytes: int = 5 * 1024 * 1024) -> list[dict]:
    """
    A week file's records, newest first: when (UTC), level, which process
    and logger wrote it, the request id (API only), the message, and the
    lines after it -- a traceback -- as `details`. Reads at most the last
    `max_bytes` of the file.
    """
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - max_bytes))
        lines = f.read().decode("utf-8", errors="replace").splitlines()
    if size > max_bytes:
        lines = lines[1:]  # the first may be cut mid-line
    entries: list[dict] = []
    for number, line in enumerate(lines, start=1):
        match = _RECORD.match(line)
        if not match:
            if entries:  # a traceback line, or a message's own newline
                entries[-1]["details"] = (entries[-1]["details"] + "\n" + line).lstrip("\n")
            continue
        stamp, first, rest = match.groups()
        at = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST).astimezone(timezone.utc)
        entry = {"line": number, "at": at, "level": None, "source": "packets", "logger": None,
                 "request_id": None, "message": f"{first} {rest}".strip(), "details": ""}
        if first in LEVELS:
            entry["level"] = first
            logger = _LOGGER.match(rest)
            if logger:
                entry["logger"], request_id, entry["message"] = logger.groups()
                entry["request_id"] = request_id if request_id and request_id != "-" else None
            else:
                entry["message"] = rest
            entry["source"] = "gateway" if (entry["logger"] or "").startswith("gps_gateway") else "api"
        entries.append(entry)
    for entry in entries:
        entry["details"] = entry["details"] or None
    entries.reverse()
    return entries
