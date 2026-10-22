"""
Server logs (admin): the weekly warning/error files the API and the gateway
write to one folder outside the project (weekly_logs.py), and the log level.

    GET /logs                 every week's file, newest first
    GET /logs/{name}          one file as text (?tail=, ?level=)
    GET /logs/level           the level now
    PUT /logs/level           change it -- the API at once, the gateway
                              within 30 s, kept across restarts
"""

import logging
import os
import re

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse

from weekly_logs import LEVEL_FILE, list_logs, log_path, read_level, write_level

from ..config import ApiConfig
from ..deps import get_config, require_admin
from ..schemas import LogFileOut, LogLevelIn, LogLevelOut

router = APIRouter(prefix="/logs", tags=["logs"], dependencies=[Depends(require_admin)])

log = logging.getLogger(__name__)

# A file is read from its end, at most this much: warnings and errors stay
# small, but a week of a noisy failure should not be loaded whole.
MAX_READ_BYTES = 5 * 1024 * 1024
# Each record starts with its timestamp; the lines after it (a traceback)
# belong to it until the next one.
RECORD_START = re.compile(r"^\d{4}-\d{2}-\d{2} \d\d:\d\d:\d\d IST (\w+)")


@router.get("", response_model=list[LogFileOut], summary="The weekly log files")
async def list_files(config: ApiConfig = Depends(get_config)) -> list[dict]:
    """One file per week (Monday to Sunday, IST), named `ddmmyyyy_ddmmyyyy.log`,
    with the API's and the gateway's warnings and errors; the last few weeks
    are kept (API_LOG_RETENTION_WEEKS, default 4)."""
    return list_logs(config.log_dir) if config.log_dir else []


@router.get("/level", response_model=LogLevelOut, summary="The log level now")
async def get_level(config: ApiConfig = Depends(get_config)) -> LogLevelOut:
    return LogLevelOut(
        level=read_level(config.log_dir, config.log_level),
        saved=bool(config.log_dir) and os.path.isfile(os.path.join(config.log_dir, LEVEL_FILE)),
    )


@router.put("/level", response_model=LogLevelOut, summary="Change the log level")
async def set_level(payload: LogLevelIn, config: ApiConfig = Depends(get_config)) -> LogLevelOut:
    """ERROR (the default) writes errors only; WARNING adds warnings. Applies to
    the API at once and to the gateway within 30 seconds, and is kept across
    restarts -- until changed here again."""
    saved = False
    if config.log_dir:
        try:
            write_level(config.log_dir, payload.level)
            saved = True
        except OSError as exc:
            log.error("Could not save the log level to %r: %s", config.log_dir, exc)
    logging.getLogger().setLevel(payload.level)
    return LogLevelOut(level=payload.level, saved=saved)


@router.get(
    "/{name}",
    response_class=PlainTextResponse,
    summary="One week's log file",
    responses={404: {"description": "No such file."}},
)
async def read_file(
    name: str,
    tail: int = Query(default=1000, ge=1, le=20000, description="At most this many lines, the newest."),
    level: str | None = Query(
        default=None, pattern="^(ERROR|WARNING)$", description="Only records at this level."
    ),
    config: ApiConfig = Depends(get_config),
) -> str:
    path = log_path(config.log_dir, name) if config.log_dir else None
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"No log file {name}")
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - MAX_READ_BYTES))
        text = f.read().decode("utf-8", errors="replace")
    lines = text.splitlines()
    if size > MAX_READ_BYTES:
        lines = lines[1:]  # the first may be cut mid-line
    if level:
        lines = _only_level(lines, level)
    return "\n".join(lines[-tail:]) + ("\n" if lines else "")


def _only_level(lines: list[str], level: str) -> list[str]:
    """The records at `level`, each with its continuation lines (a traceback)."""
    kept, keeping = [], False
    for line in lines:
        start = RECORD_START.match(line)
        if start:
            keeping = start.group(1) == level
        if keeping:
            kept.append(line)
    return kept
