from hashlib import sha256
from pathlib import Path
from uuid import UUID
import os

import psycopg
from psycopg.rows import dict_row
from redis import Redis


DATA = Path(os.getenv("OCR_DATA_ROOT", "/data")).resolve()
RESULT_TTL = int(os.getenv("RESULT_TTL_SECONDS", 7 * 86400))
DOWNLOAD_TTL = int(os.getenv("DOWNLOAD_TTL_SECONDS", 86400))
FAILED_TTL = int(os.getenv("FAILED_TTL_SECONDS", 86400))


def database(*, autocommit=False):
    return psycopg.connect(row_factory=dict_row, autocommit=autocommit, connect_timeout=5)


def redis_client():
    return Redis.from_url(
        os.getenv("REDIS_URL", "redis://redis:6379/0"), socket_connect_timeout=3, socket_timeout=5
    )


def inside(directory: Path, relative: str) -> Path:
    candidate = (directory / relative).resolve()
    if Path(relative).is_absolute() or not candidate.is_relative_to(directory) or candidate == directory:
        raise ValueError("Путь вне каталога задания")
    return candidate


def job_dir(job_id) -> Path:
    return inside(DATA / "temporary" / "jobs", str(UUID(str(job_id))))


def job_file(job_id, relative_path: str) -> Path:
    base = job_dir(job_id)
    relative = Path(relative_path).relative_to(base.relative_to(DATA))
    return inside(base, str(relative))


def relative(path: Path) -> str:
    return path.relative_to(DATA).as_posix()


def digest(path: Path) -> str:
    result = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()
