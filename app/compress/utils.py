"""Small helpers shared by the image and PDF compressors."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Magic-byte signatures. We sniff content instead of trusting the extension a
# browser hands us, so a mislabelled file still gets routed to the right engine.
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
)

KIND_PDF = "pdf"
KIND_IMAGE = "image"


def sniff_kind(path: Path) -> tuple[str | None, str | None]:
    """Return ``(kind, subtype)`` for a file, or ``(None, None)`` if unsupported.

    ``kind`` is ``"pdf"`` or ``"image"``; ``subtype`` is a lowercase format hint
    such as ``"jpeg"`` or ``"webp"``.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(32)
    except OSError:
        return None, None

    if head.startswith(b"%PDF-"):
        return KIND_PDF, "pdf"

    for signature, subtype in _IMAGE_SIGNATURES:
        if head.startswith(signature):
            return KIND_IMAGE, subtype

    # RIFF-based (WebP) and ISO-BMFF-based (HEIC/AVIF) containers need a look
    # past the first bytes to identify the brand.
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return KIND_IMAGE, "webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return KIND_IMAGE, "avif"
        if brand in (b"heic", b"heix", b"heim", b"heis", b"hevc", b"mif1", b"msf1"):
            return KIND_IMAGE, "heic"

    return None, None


# Control characters plus the set Windows forbids. Everything else - including
# accented and CJK characters - is kept, so names stay recognisable.
_UNSAFE_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]+')

# Legacy DOS device names, which are still special on Windows.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name: str, fallback: str = "file") -> str:
    """Reduce a user-supplied name to something safe to write to disk.

    Strips any directory component, control characters and reserved names so the
    value can never escape the job directory.
    """
    name = unicodedata.normalize("NFKC", name or "")
    # Cut everything before the last separator of either flavour.
    name = name.replace("\\", "/").split("/")[-1]
    name = _UNSAFE_CHARS.sub("_", name).strip(" .")
    if not name or name in {".", ".."} or Path(name).stem.upper() in _RESERVED:
        name = fallback
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 8:
            name = stem[: 110 - len(ext)] + "." + ext
        else:
            name = name[:120]
    return name


def replace_suffix(name: str, new_suffix: str, tag: str = "") -> str:
    """``photo.png`` + ``.webp`` -> ``photo-compressed.webp``."""
    stem = Path(name).stem or "file"
    if tag:
        stem = f"{stem}-{tag}"
    return stem + new_suffix


def human_size(num_bytes: float) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num_bytes) < step or unit == "GB":
            if unit == "B":
                return f"{int(num_bytes)} {unit}"
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= step
    return f"{num_bytes:.1f} GB"


def savings_percent(original: int, compressed: int) -> float:
    if original <= 0:
        return 0.0
    return round((1 - compressed / original) * 100, 1)
