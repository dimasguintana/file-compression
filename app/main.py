"""FastAPI application: upload endpoints, status polling and downloads."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import secrets
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, config
from .compress import (
    ImageOptions,
    PdfOptions,
    ghostscript_available,
    human_size,
    safe_filename,
    sniff_kind,
)
from .compress.images import OUTPUT_FORMATS
from .compress.pdfs import MODES as PDF_MODES
from .jobs import STATUS_ERROR, JobFile, manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("compressor")

CHUNK_SIZE = 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    # Anything still on disk belongs to a previous run and can never be reached
    # again, because job ids only live in memory.
    for path in config.JOBS_DIR.iterdir():
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)

    async def janitor() -> None:
        while True:
            await asyncio.sleep(config.CLEANUP_INTERVAL_SECONDS)
            try:
                removed = await asyncio.to_thread(manager.purge_expired)
                if removed:
                    log.info("Purged %d expired job(s)", removed)
            except Exception:
                log.exception("Cleanup pass failed")

    task = asyncio.create_task(janitor())
    log.info(
        "Ready - Ghostscript: %s, workers: %d, max file: %s",
        config.GHOSTSCRIPT_BIN or "not found",
        config.MAX_WORKERS,
        human_size(config.MAX_FILE_SIZE),
    )
    try:
        yield
    finally:
        task.cancel()
        manager.shutdown()


app = FastAPI(title="PDF & Photo Compression", version=__version__, lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _build_image_options(
    quality: int, output_format: str, max_dimension: int, strip_metadata: bool,
    lossy_png: bool, never_grow: bool,
) -> ImageOptions:
    fmt = (output_format or "auto").lower()
    if fmt not in OUTPUT_FORMATS:
        fmt = "auto"
    return ImageOptions(
        quality=_clamp(quality, 1, 100),
        output_format=fmt,
        max_dimension=_clamp(max_dimension, 0, 20000),
        strip_metadata=strip_metadata,
        lossy_png=lossy_png,
        never_grow=never_grow,
    )


def _build_pdf_options(mode: str, grayscale: bool, linearize: bool, never_grow: bool) -> PdfOptions:
    mode = (mode or "balanced").lower()
    if mode not in PDF_MODES:
        mode = "balanced"
    return PdfOptions(mode=mode, grayscale=grayscale, linearize=linearize, never_grow=never_grow)


async def _save_upload(upload: UploadFile, dest: Path) -> tuple[int, str | None]:
    """Stream an upload to disk. Returns ``(bytes_written, error)``."""
    size = 0
    try:
        with dest.open("wb") as fh:
            while True:
                chunk = await upload.read(CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > config.MAX_FILE_SIZE:
                    fh.close()
                    dest.unlink(missing_ok=True)
                    return 0, f"Larger than the {human_size(config.MAX_FILE_SIZE)} limit."
                fh.write(chunk)
    except OSError as exc:
        dest.unlink(missing_ok=True)
        return 0, f"Could not store upload: {exc}"
    finally:
        await upload.close()

    if size == 0:
        dest.unlink(missing_ok=True)
        return 0, "The file is empty."
    return size, None


def _resolve_output(job, job_file) -> Path:
    """Return the output path after confirming it really sits inside the job dir."""
    if job_file.output_path is None:
        raise HTTPException(status_code=409, detail="This file has no result yet.")
    path = job_file.output_path.resolve()
    if not path.is_relative_to(job.output_dir.resolve()) or not path.exists():
        raise HTTPException(status_code=404, detail="Result file is gone.")
    return path


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/capabilities")
async def capabilities() -> dict:
    return {
        "version": __version__,
        "ghostscript": ghostscript_available(),
        "ghostscript_path": config.GHOSTSCRIPT_BIN,
        "max_file_size": config.MAX_FILE_SIZE,
        "max_file_size_human": human_size(config.MAX_FILE_SIZE),
        "max_files": config.MAX_FILES_PER_JOB,
        "image_formats": list(OUTPUT_FORMATS),
        "pdf_modes": list(PDF_MODES),
        "job_ttl_minutes": config.JOB_TTL_SECONDS // 60,
    }


@app.post("/api/jobs", status_code=201)
async def create_job(
    files: list[UploadFile] = File(...),
    image_quality: int = Form(78),
    image_format: str = Form("auto"),
    image_max_dimension: int = Form(0),
    image_strip_metadata: bool = Form(True),
    image_lossy_png: bool = Form(True),
    pdf_mode: str = Form("balanced"),
    pdf_grayscale: bool = Form(False),
    pdf_linearize: bool = Form(False),
    never_grow: bool = Form(True),
) -> JSONResponse:
    if not files:
        raise HTTPException(status_code=400, detail="No files were uploaded.")
    if len(files) > config.MAX_FILES_PER_JOB:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files at once - the limit is {config.MAX_FILES_PER_JOB}.",
        )

    job = manager.create_job(
        _build_image_options(
            image_quality, image_format, image_max_dimension,
            image_strip_metadata, image_lossy_png, never_grow,
        ),
        _build_pdf_options(pdf_mode, pdf_grayscale, pdf_linearize, never_grow),
    )

    for index, upload in enumerate(files):
        display_name = safe_filename(upload.filename or "", fallback=f"file-{index + 1}")
        file_id = secrets.token_urlsafe(8)
        # Store under an opaque id so two uploads named the same cannot collide.
        stored = job.input_dir / f"{file_id}{Path(display_name).suffix.lower()}"

        size, error = await _save_upload(upload, stored)
        if error:
            job.files.append(
                JobFile(
                    id=file_id, original_name=display_name, kind="unknown",
                    input_path=stored, input_size=size, status=STATUS_ERROR, error=error,
                )
            )
            continue

        kind, _subtype = await asyncio.to_thread(sniff_kind, stored)
        if kind is None:
            stored.unlink(missing_ok=True)
            job.files.append(
                JobFile(
                    id=file_id, original_name=display_name, kind="unknown",
                    input_path=stored, input_size=size, status=STATUS_ERROR,
                    error="Unsupported file type - upload a PDF, JPEG, PNG, WebP, AVIF, GIF, BMP or TIFF.",
                )
            )
            continue

        job.files.append(
            JobFile(
                id=file_id, original_name=display_name, kind=kind,
                input_path=stored, input_size=size,
            )
        )

    manager.start(job)
    return JSONResponse(job.to_dict(), status_code=201)


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> dict:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    return job.to_dict()


@app.delete("/api/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str) -> None:
    if not manager.delete(job_id):
        raise HTTPException(status_code=404, detail="Job not found or expired.")


@app.get("/api/jobs/{job_id}/files/{file_id}/download")
async def download_file(job_id: str, file_id: str) -> FileResponse:
    found = manager.get_file(job_id, file_id)
    if found is None:
        raise HTTPException(status_code=404, detail="File not found or expired.")
    job, job_file = found
    path = _resolve_output(job, job_file)
    media_type = mimetypes.guess_type(job_file.output_name or "")[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=job_file.output_name)


@app.get("/api/jobs/{job_id}/archive")
async def download_archive(job_id: str) -> FileResponse:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    archive = await asyncio.to_thread(manager.build_archive, job)
    if archive is None:
        raise HTTPException(status_code=409, detail="Nothing has finished compressing yet.")
    return FileResponse(archive, media_type="application/zip", filename="compressed.zip")


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


# The UI is served from the same origin, so no CORS setup is needed.
app.mount("/", StaticFiles(directory=str(config.STATIC_DIR), html=True), name="static")
