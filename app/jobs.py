"""In-memory job registry backed by per-job directories on disk.

A *job* is one batch the user dropped on the page. Each file inside it is
processed independently on a worker thread, so one bad file never sinks the
batch. Jobs and their files are deleted once :data:`config.JOB_TTL_SECONDS`
has elapsed.
"""

from __future__ import annotations

import logging
import secrets
import shutil
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .compress import (
    ImageCompressionError,
    ImageOptions,
    PdfCompressionError,
    PdfOptions,
    compress_image,
    compress_pdf,
    human_size,
    savings_percent,
)

log = logging.getLogger("compressor.jobs")

STATUS_QUEUED = "queued"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_ERROR = "error"


@dataclass
class JobFile:
    id: str
    original_name: str
    kind: str  # "image" | "pdf"
    input_path: Path
    input_size: int
    status: str = STATUS_QUEUED
    output_path: Path | None = None
    output_name: str | None = None
    output_size: int | None = None
    method: str = ""
    note: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        saved = None
        if self.output_size is not None:
            saved = savings_percent(self.input_size, self.output_size)
        return {
            "id": self.id,
            "name": self.original_name,
            "kind": self.kind,
            "status": self.status,
            "input_size": self.input_size,
            "input_size_human": human_size(self.input_size),
            "output_size": self.output_size,
            "output_size_human": human_size(self.output_size) if self.output_size is not None else None,
            "output_name": self.output_name,
            "saved_percent": saved,
            "saved_bytes": (self.input_size - self.output_size) if self.output_size is not None else None,
            "method": self.method,
            "note": self.note,
            "error": self.error,
        }


@dataclass
class Job:
    id: str
    directory: Path
    created_at: float = field(default_factory=time.time)
    files: list[JobFile] = field(default_factory=list)
    image_options: ImageOptions = field(default_factory=ImageOptions)
    pdf_options: PdfOptions = field(default_factory=PdfOptions)

    @property
    def input_dir(self) -> Path:
        return self.directory / "in"

    @property
    def output_dir(self) -> Path:
        return self.directory / "out"

    def to_dict(self) -> dict:
        files = [f.to_dict() for f in self.files]
        finished = sum(1 for f in self.files if f.status in (STATUS_DONE, STATUS_ERROR))
        done = [f for f in self.files if f.status == STATUS_DONE]
        total_in = sum(f.input_size for f in done)
        total_out = sum(f.output_size or 0 for f in done)
        return {
            "id": self.id,
            "created_at": self.created_at,
            "complete": finished == len(self.files) and bool(self.files),
            "total": len(self.files),
            "finished": finished,
            "succeeded": len(done),
            "failed": sum(1 for f in self.files if f.status == STATUS_ERROR),
            "totals": {
                "input_size": total_in,
                "output_size": total_out,
                "input_size_human": human_size(total_in),
                "output_size_human": human_size(total_out),
                "saved_bytes": total_in - total_out,
                "saved_human": human_size(total_in - total_out),
                "saved_percent": savings_percent(total_in, total_out),
            },
            "files": files,
        }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=config.MAX_WORKERS, thread_name_prefix="compress"
        )

    # -- lifecycle ---------------------------------------------------------
    def create_job(self, image_options: ImageOptions, pdf_options: PdfOptions) -> Job:
        job_id = secrets.token_urlsafe(16)
        directory = config.JOBS_DIR / job_id
        job = Job(id=job_id, directory=directory, image_options=image_options, pdf_options=pdf_options)
        job.input_dir.mkdir(parents=True, exist_ok=True)
        job.output_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def get_file(self, job_id: str, file_id: str) -> tuple[Job, JobFile] | None:
        job = self.get(job_id)
        if job is None:
            return None
        for jf in job.files:
            if jf.id == file_id:
                return job, jf
        return None

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        shutil.rmtree(job.directory, ignore_errors=True)
        return True

    def start(self, job: Job) -> None:
        for job_file in job.files:
            # Files rejected at upload time are already marked; don't queue them.
            if job_file.status == STATUS_QUEUED:
                self._executor.submit(self._process, job, job_file)

    # -- worker ------------------------------------------------------------
    def _process(self, job: Job, job_file: JobFile) -> None:
        job_file.status = STATUS_PROCESSING
        try:
            # Each file gets its own directory. Two inputs can easily map to the
            # same output name (photo.jpg and photo.tiff both become
            # photo-compressed.jpg), and sharing a directory would let one
            # silently overwrite the other.
            file_out_dir = job.output_dir / job_file.id
            file_out_dir.mkdir(parents=True, exist_ok=True)

            if job_file.kind == "pdf":
                result = compress_pdf(
                    job_file.input_path, file_out_dir, job_file.original_name, job.pdf_options
                )
            else:
                result = compress_image(
                    job_file.input_path, file_out_dir, job_file.original_name, job.image_options
                )
            job_file.output_path = result.output_path
            job_file.output_name = result.output_name
            job_file.output_size = result.output_size
            job_file.method = result.method
            job_file.note = result.note
            job_file.status = STATUS_DONE
        except (ImageCompressionError, PdfCompressionError) as exc:
            job_file.error = str(exc)
            job_file.status = STATUS_ERROR
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("Unexpected failure compressing %s", job_file.original_name)
            job_file.error = f"Unexpected error: {exc.__class__.__name__}"
            job_file.status = STATUS_ERROR
        finally:
            # The upload is no longer needed once we have a result.
            try:
                job_file.input_path.unlink(missing_ok=True)
            except OSError:
                pass

    # -- archive -----------------------------------------------------------
    def build_archive(self, job: Job) -> Path | None:
        """Zip every successful output. Returns ``None`` if there is nothing to zip."""
        ready = [f for f in job.files if f.status == STATUS_DONE and f.output_path]
        if not ready:
            return None

        archive = job.directory / "compressed.zip"
        tmp = job.directory / "compressed.zip.tmp"
        used: dict[str, int] = {}
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
            for job_file in ready:
                name = job_file.output_name or job_file.original_name
                # Two inputs can produce the same output name; keep both.
                if name in used:
                    used[name] += 1
                    stem, dot, ext = name.rpartition(".")
                    name = f"{stem}-{used[name]}{dot}{ext}" if dot else f"{name}-{used[name]}"
                else:
                    used[name] = 0
                zf.write(job_file.output_path, arcname=name)
        tmp.replace(archive)
        return archive

    # -- housekeeping ------------------------------------------------------
    def purge_expired(self) -> int:
        cutoff = time.time() - config.JOB_TTL_SECONDS
        with self._lock:
            expired = [jid for jid, job in self._jobs.items() if job.created_at < cutoff]
            for jid in expired:
                self._jobs.pop(jid, None)
        for jid in expired:
            shutil.rmtree(config.JOBS_DIR / jid, ignore_errors=True)

        # Sweep up directories left behind by a previous process.
        known = set(self._jobs)
        if config.JOBS_DIR.exists():
            for path in config.JOBS_DIR.iterdir():
                if path.is_dir() and path.name not in known:
                    try:
                        if path.stat().st_mtime < cutoff:
                            shutil.rmtree(path, ignore_errors=True)
                    except OSError:
                        pass
        return len(expired)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


manager = JobManager()
