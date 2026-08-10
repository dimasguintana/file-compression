"""PDF compression.

Three engines, tried in order of how much they can save:

``pikepdf``
    Always available. Recompresses streams, dedupes objects and rebuilds the
    cross-reference table. Completely lossless - text and images are untouched.

``ghostscript``
    Used when the ``gs`` binary is present. Downsamples and re-encodes embedded
    images, subsets fonts and rewrites the page content. This is what produces
    the dramatic savings on scanned documents.

``pikepdf + Pillow`` (pure Python)
    The fallback for machines without Ghostscript. Walks the page tree, pulls
    out embedded raster images and re-encodes them as JPEG at a target DPI.

Whichever engine produces the smallest *valid* PDF wins, and the original is
always in the running so the output can never be larger than the input.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pikepdf
from PIL import Image

from .. import config

MODES = ("lossless", "light", "balanced", "strong", "extreme")

# mode -> (ghostscript preset, colour/gray DPI, mono DPI, JPEG quality)
_MODE_SETTINGS: dict[str, tuple[str, int, int, int]] = {
    "light": ("/printer", 200, 600, 85),
    "balanced": ("/ebook", 150, 450, 75),
    "strong": ("/ebook", 110, 300, 62),
    "extreme": ("/screen", 72, 200, 45),
}

# Images smaller than this are not worth re-encoding in the Python fallback.
_MIN_IMAGE_BYTES = 4096
_MAX_FORM_DEPTH = 12


class PdfCompressionError(Exception):
    """Raised when a PDF cannot be opened or every engine failed."""


@dataclass(slots=True)
class PdfOptions:
    mode: str = "balanced"
    grayscale: bool = False
    linearize: bool = False  # "fast web view" - optimise for streaming
    never_grow: bool = True


@dataclass(slots=True)
class PdfResult:
    output_path: Path
    output_name: str
    output_size: int
    method: str
    note: str = ""


def ghostscript_available() -> bool:
    return config.GHOSTSCRIPT_BIN is not None


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _page_count(path: Path) -> int | None:
    """Open a candidate and count its pages; ``None`` means it is unusable."""
    try:
        with pikepdf.open(path) as pdf:
            return len(pdf.pages)
    except Exception:
        return None


def _valid_candidate(path: Path, expected_pages: int) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    return _page_count(path) == expected_pages


# --------------------------------------------------------------------------- #
# Engine 1: pikepdf lossless
# --------------------------------------------------------------------------- #
def _run_pikepdf(src: Path, dst: Path, opts: PdfOptions) -> bool:
    try:
        with pikepdf.open(src) as pdf:
            pdf.save(
                dst,
                compress_streams=True,
                recompress_flate=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                linearize=opts.linearize,
            )
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Engine 2: Ghostscript
# --------------------------------------------------------------------------- #
def _run_ghostscript(src: Path, dst: Path, opts: PdfOptions) -> bool:
    if not config.GHOSTSCRIPT_BIN or opts.mode == "lossless":
        return False

    preset, image_dpi, mono_dpi, quality = _MODE_SETTINGS[opts.mode]

    args = [
        config.GHOSTSCRIPT_BIN,
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.7",
        f"-dPDFSETTINGS={preset}",
        "-dNOPAUSE",
        "-dBATCH",
        "-dQUIET",
        "-dSAFER",
        "-dAutoRotatePages=/None",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-dSubsetFonts=true",
        "-dEmbedAllFonts=true",
        # Explicit image settings come *after* -dPDFSETTINGS so they win.
        "-dDownsampleColorImages=true",
        "-dColorImageDownsampleType=/Bicubic",
        f"-dColorImageResolution={image_dpi}",
        "-dColorImageDownsampleThreshold=1.0",
        "-dDownsampleGrayImages=true",
        "-dGrayImageDownsampleType=/Bicubic",
        f"-dGrayImageResolution={image_dpi}",
        "-dGrayImageDownsampleThreshold=1.0",
        "-dDownsampleMonoImages=true",
        "-dMonoImageDownsampleType=/Subsample",
        f"-dMonoImageResolution={mono_dpi}",
        "-dAutoFilterColorImages=false",
        "-dColorImageFilter=/DCTEncode",
        "-dAutoFilterGrayImages=false",
        "-dGrayImageFilter=/DCTEncode",
        f"-dJPEGQ={quality}",
    ]

    if opts.grayscale:
        args += [
            "-sColorConversionStrategy=Gray",
            "-dProcessColorModel=/DeviceGray",
            "-dOverrideICC=true",
        ]

    if opts.linearize:
        args.append("-dFastWebView=true")

    args += [f"-sOutputFile={dst}", str(src)]

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=config.GHOSTSCRIPT_TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False

    return proc.returncode == 0 and dst.exists() and dst.stat().st_size > 0


# --------------------------------------------------------------------------- #
# Engine 3: pure-Python image downsampling
# --------------------------------------------------------------------------- #
def _iter_image_xobjects(resources, seen_forms: set, depth: int = 0):
    """Yield every image XObject reachable from a resource dictionary."""
    if depth > _MAX_FORM_DEPTH or resources is None:
        return
    try:
        xobjects = resources.get("/XObject")
    except Exception:
        return
    if xobjects is None:
        return

    try:
        items = list(xobjects.items())
    except Exception:
        return

    for _name, obj in items:
        try:
            subtype = obj.get("/Subtype")
        except Exception:
            continue
        if subtype == "/Image":
            yield obj
        elif subtype == "/Form":
            key = obj.objgen
            if key in seen_forms:
                continue
            seen_forms.add(key)
            yield from _iter_image_xobjects(obj.get("/Resources"), seen_forms, depth + 1)


def _recompress_image(obj, max_w: int, max_h: int, quality: int, grayscale: bool) -> bool:
    """Re-encode one embedded image in place. Returns True if it got smaller."""
    try:
        if obj.get("/ImageMask"):
            return False  # 1-bit stencil mask; JPEG would destroy it
        if obj.get("/Mask") is not None:
            return False  # colour-key masking breaks under lossy re-encoding
        raw = obj.read_raw_bytes()
    except Exception:
        return False

    if len(raw) < _MIN_IMAGE_BYTES:
        return False

    try:
        pil = pikepdf.PdfImage(obj).as_pil_image()
    except Exception:
        return False

    try:
        if pil.mode in ("RGBA", "LA", "P", "PA"):
            pil = pil.convert("RGB")
        elif pil.mode == "CMYK":
            pil = pil.convert("RGB")
        elif pil.mode == "1":
            pil = pil.convert("L")

        if grayscale and pil.mode != "L":
            pil = pil.convert("L")

        if pil.width > max_w or pil.height > max_h:
            scale = min(max_w / pil.width, max_h / pil.height)
            new_size = (max(1, round(pil.width * scale)), max(1, round(pil.height * scale)))
            pil = pil.resize(new_size, Image.Resampling.LANCZOS)

        if pil.mode not in ("RGB", "L"):
            pil = pil.convert("RGB")

        import io

        buffer = io.BytesIO()
        pil.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
        data = buffer.getvalue()
    except Exception:
        return False

    # Only accept a clear win, so we never trade quality for nothing.
    if len(data) >= len(raw) * 0.95:
        return False

    try:
        obj.write(data, filter=pikepdf.Name("/DCTDecode"))
        obj.ColorSpace = pikepdf.Name("/DeviceGray" if pil.mode == "L" else "/DeviceRGB")
        obj.BitsPerComponent = 8
        obj.Width = pil.width
        obj.Height = pil.height
        for key in ("/Decode", "/Interpolate"):
            if key in obj:
                del obj[key]
    except Exception:
        return False
    return True


def _run_python_images(src: Path, dst: Path, opts: PdfOptions) -> bool:
    if opts.mode == "lossless":
        return False

    _preset, image_dpi, _mono, quality = _MODE_SETTINGS[opts.mode]

    try:
        with pikepdf.open(src) as pdf:
            seen_images: set = set()
            seen_forms: set = set()
            changed = 0

            for page in pdf.pages:
                # An image can never need more pixels than the page can show at
                # the target DPI, so the page box gives us a safe upper bound.
                try:
                    box = [float(v) for v in page.mediabox]
                    page_w = abs(box[2] - box[0]) / 72.0
                    page_h = abs(box[3] - box[1]) / 72.0
                except Exception:
                    page_w, page_h = 8.5, 11.0
                max_w = max(64, round(page_w * image_dpi))
                max_h = max(64, round(page_h * image_dpi))

                for obj in _iter_image_xobjects(page.obj.get("/Resources"), seen_forms):
                    key = obj.objgen
                    if key in seen_images:
                        continue
                    seen_images.add(key)
                    if _recompress_image(obj, max_w, max_h, quality, opts.grayscale):
                        changed += 1

            pdf.save(
                dst,
                compress_streams=True,
                recompress_flate=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                linearize=opts.linearize,
            )
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def compress_pdf(src: Path, out_dir: Path, original_name: str, opts: PdfOptions) -> PdfResult:
    if opts.mode not in MODES:
        opts.mode = "balanced"

    try:
        with pikepdf.open(src) as pdf:
            expected_pages = len(pdf.pages)
            was_encrypted = pdf.is_encrypted
    except pikepdf.PasswordError as exc:
        raise PdfCompressionError("This PDF is password protected.") from exc
    except Exception as exc:
        raise PdfCompressionError(f"Not a readable PDF: {exc}") from exc

    if expected_pages == 0:
        raise PdfCompressionError("This PDF has no pages.")

    original_size = src.stat().st_size
    work_dir = out_dir / f".work-{src.stem}"
    work_dir.mkdir(parents=True, exist_ok=True)

    engines: list[tuple[str, object]] = [("pikepdf", _run_pikepdf)]
    if opts.mode != "lossless":
        if ghostscript_available():
            engines.append(("ghostscript", _run_ghostscript))
        engines.append(("python-images", _run_python_images))

    best_path: Path | None = None
    best_size = original_size if opts.never_grow else None
    best_method = "copy"

    try:
        for name, runner in engines:
            candidate = work_dir / f"{name}.pdf"
            try:
                ok = runner(src, candidate, opts)  # type: ignore[operator]
            except Exception:
                ok = False
            if not ok or not _valid_candidate(candidate, expected_pages):
                candidate.unlink(missing_ok=True)
                continue

            size = candidate.stat().st_size
            if best_size is None or size < best_size:
                if best_path is not None:
                    best_path.unlink(missing_ok=True)
                best_path, best_size, best_method = candidate, size, name
            else:
                candidate.unlink(missing_ok=True)

        out_name = Path(original_name).stem + "-compressed.pdf"
        out_path = out_dir / out_name
        notes: list[str] = []

        if best_path is None:
            # Nothing beat the original (or every engine failed on a file we can
            # still read) - hand back a byte-identical copy.
            shutil.copyfile(src, out_path)
            final_size = original_size
            best_method = "copy"
            notes.append("already optimal - original kept")
        else:
            shutil.move(str(best_path), out_path)
            final_size = out_path.stat().st_size

        if was_encrypted:
            notes.append("encryption removed")
        if opts.mode != "lossless" and best_method == "pikepdf":
            notes.append("no images to downsample")
        if opts.mode != "lossless" and not ghostscript_available():
            notes.append("Ghostscript not installed - used Python fallback")

        return PdfResult(out_path, out_name, final_size, best_method, ", ".join(notes))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
