"""Sao lưu cơ sở dữ liệu vào thư mục nội bộ theo lịch hằng đêm."""

import datetime
import logging
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import environ
from apscheduler.schedulers.background import BackgroundScheduler
from django.conf import settings

logger = logging.getLogger("pg_backup")
env = environ.Env()

BACKUP_DIR = Path(
    env("BACKUP_DIR", default=str(Path(settings.BASE_DIR) / "backup"))
).resolve()
BACKUP_CRON_TIMES = env("BACKUP_CRON_TIMES", default="02:00")
BACKUP_RETENTION_DAYS = env.int("BACKUP_RETENTION_DAYS", default=30)
DATE_FORMAT = "%Y-%m-%d_%H-%M-%S"
_scheduler = None


def _remove_expired_backups():
    if BACKUP_RETENTION_DAYS <= 0 or not BACKUP_DIR.exists():
        return
    cutoff = datetime.datetime.now().timestamp() - BACKUP_RETENTION_DAYS * 86400
    for path in BACKUP_DIR.glob("joydigi-*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("Không thể xóa bản sao lưu cũ %s: %s", path, exc)


def _backup_postgres(database, target):
    pg_dump = shutil.which("pg_dump")
    if not pg_dump and os.name == "nt":
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        candidates = list(
            (program_files / "PostgreSQL").glob("*/bin/pg_dump.exe")
        )

        def version_key(path):
            try:
                return int(path.parent.parent.name)
            except ValueError:
                return 0

        if candidates:
            pg_dump = str(max(candidates, key=version_key))
    if not pg_dump:
        raise RuntimeError("Không tìm thấy chương trình pg_dump trong PATH.")
    process_env = os.environ.copy()
    process_env["PGPASSWORD"] = str(database.get("PASSWORD") or "")
    command = [
        pg_dump,
        "-h",
        str(database.get("HOST") or "localhost"),
        "-p",
        str(database.get("PORT") or 5432),
        "-U",
        str(database.get("USER") or ""),
        "-F",
        "c",
        "-b",
        "-f",
        str(target),
        str(database["NAME"]),
    ]
    subprocess.run(command, check=True, env=process_env, capture_output=True)


def _backup_sqlite(database, target):
    source_path = Path(database["NAME"]).resolve()
    with sqlite3.connect(source_path) as source, sqlite3.connect(target) as destination:
        source.backup(destination)


def backup_database():
    database = settings.DATABASES["default"]
    engine = database["ENGINE"]
    timestamp = datetime.datetime.now().strftime(DATE_FORMAT)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if "postgresql" in engine:
            target = BACKUP_DIR / f"joydigi-{timestamp}.dump"
            _backup_postgres(database, target)
        elif "sqlite" in engine:
            target = BACKUP_DIR / f"joydigi-{timestamp}.sqlite3"
            _backup_sqlite(database, target)
        else:
            logger.warning("Chưa hỗ trợ sao lưu loại cơ sở dữ liệu: %s", engine)
            return
        _remove_expired_backups()
        logger.info("Đã sao lưu cơ sở dữ liệu: %s", target)
    except (OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        logger.error("Sao lưu cơ sở dữ liệu thất bại: %s", exc)


def start():
    global _scheduler
    if _scheduler is not None or not BACKUP_CRON_TIMES.strip():
        return
    scheduler = BackgroundScheduler(timezone=str(settings.TIME_ZONE))
    for time_text in BACKUP_CRON_TIMES.split(","):
        try:
            hour, minute = (int(value) for value in time_text.strip().split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
            scheduler.add_job(
                backup_database,
                "cron",
                hour=hour,
                minute=minute,
                id=f"joydigi_backup_{hour:02d}_{minute:02d}",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
        except ValueError:
            logger.error("Giờ sao lưu không hợp lệ: %s", time_text)
    if scheduler.get_jobs():
        scheduler.start()
        _scheduler = scheduler
        logger.info("Đã bật lịch sao lưu nội bộ lúc %s.", BACKUP_CRON_TIMES)
