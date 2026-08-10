"""Image compression built on Pillow.

Design notes
------------
* Every strategy produces *candidate bytes* in memory. The smallest valid
  candidate wins, and the untouched original is always one of the candidates
  unless the user explicitly asked for a different output format. That makes it
  impossible to hand back a file bigger than the one that came in.
* EXIF orientation is only baked into the pixels when metadata is being
  stripped. If we stripped the tag without rotating, the image would come out
  sideways; if we rotated while keeping the tag, it would come out rotated
  twice.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence

from .. import config
from .utils import replace_suffix

Image.MAX_IMAGE_PIXELS = config.MAX_IMAGE_PIXELS

# Formats we can write. "auto" keeps the input format, "smallest" tries them all.
OUTPUT_FORMATS = ("auto", "smallest", "jpeg", "png", "webp", "avif")

_EXTENSIONS = {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "avif": ".avif", "gif": ".gif"}

_FORMAT_ALIASES = {"jpg": "jpeg", "jpe": "jpeg", "tif": "tiff", "mpo": "jpeg"}


class ImageCompressionError(Exception):
    """Raised when an image cannot be read or no usable output was produced."""


@dataclass(slots=True)
class ImageOptions:
    quality: int = 78
    output_format: str = "auto"
    max_dimension: int = 0  # longest edge in px; 0 disables resizing
    strip_metadata: bool = True
    lossy_png: bool = True  # allow palette quantisation for PNG output
    never_grow: bool = True


@dataclass(slots=True)
class ImageResult:
    output_path: Path
    output_name: str
    output_size: int
    method: str
    note: str = ""


def _normalise_format(fmt: str | None) -> str:
    fmt = (fmt or "").lower()
    return _FORMAT_ALIASES.get(fmt, fmt)


def _is_animated(img: Image.Image) -> bool:
    return getattr(img, "n_frames", 1) > 1


def _has_alpha(img: Image.Image) -> bool:
    if img.mode in ("RGBA", "LA", "PA"):
        return True
    return img.mode == "P" and "transparency" in img.info


def _flatten(img: Image.Image) -> Image.Image:
    """Composite transparency onto white so the image can be saved as JPEG."""
    if img.mode not in ("RGBA", "LA", "PA", "P"):
        return img.convert("RGB") if img.mode != "RGB" else img
    rgba = img.convert("RGBA")
    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    return Image.alpha_composite(background, rgba).convert("RGB")


def _resize(img: Image.Image, max_dimension: int) -> Image.Image:
    if max_dimension <= 0:
        return img
    longest = max(img.size)
    if longest <= max_dimension:
        return img
    # Pillow silently downgrades to NEAREST for "1" and "P" images, which looks
    # blocky. Promote to true colour first so LANCZOS can actually run.
    if img.mode in ("1", "P", "PA"):
        img = img.convert("RGBA" if _has_alpha(img) else "RGB")
    scale = max_dimension / longest
    new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    return img.resize(new_size, Image.Resampling.LANCZOS)


def _png_colors(quality: int) -> int:
    """Map a 1-100 quality onto a palette size, floored at 32 colours."""
    return max(32, min(256, round(256 * (quality / 100) ** 0.5)))


def _encode(img: Image.Image, fmt: str, opts: ImageOptions, meta: dict) -> bytes | None:
    """Encode a single still frame. Returns ``None`` if the format cannot hold it."""
    buffer = io.BytesIO()
    params: dict = {}

    # An ICC profile is worth keeping even when stripping metadata: dropping it
    # can visibly shift colours, and it is usually small.
    if meta.get("icc_profile"):
        params["icc_profile"] = meta["icc_profile"]
    if not opts.strip_metadata and meta.get("exif"):
        params["exif"] = meta["exif"]

    if fmt == "jpeg":
        if _has_alpha(img):
            img = _flatten(img)
        elif img.mode not in ("RGB", "L", "CMYK"):
            img = img.convert("RGB")
        params.update(quality=opts.quality, optimize=True, progressive=True)
        if opts.quality >= 90:
            params["subsampling"] = 0

    elif fmt == "webp":
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if _has_alpha(img) else "RGB")
        params.update(quality=opts.quality, method=6)

    elif fmt == "avif":
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if _has_alpha(img) else "RGB")
        params.update(quality=opts.quality, speed=6)

    elif fmt == "png":
        if img.mode == "CMYK":
            img = img.convert("RGB")
        # Palette reduction only pays off on true-colour images. Quantising
        # something that is already bilevel, grayscale, paletted or 16-bit
        # loses fidelity and usually produces a *larger* file.
        if opts.lossy_png and opts.quality < 100 and img.mode in ("RGB", "RGBA"):
            # FASTOCTREE is the only Pillow quantiser that keeps an alpha channel.
            img = img.quantize(colors=_png_colors(opts.quality), method=Image.Quantize.FASTOCTREE)
        params.update(optimize=True, compress_level=9)
        params.pop("exif", None)

    elif fmt == "gif":
        if img.mode not in ("P", "L"):
            img = img.convert("P", palette=Image.Palette.ADAPTIVE)
        params.update(optimize=True)
        params.pop("exif", None)
        params.pop("icc_profile", None)

    else:
        return None

    try:
        img.save(buffer, format=fmt.upper(), **params)
    except (OSError, ValueError, KeyError):
        return None
    return buffer.getvalue()


def _encode_animated(img: Image.Image, fmt: str, opts: ImageOptions) -> bytes | None:
    """Re-encode every frame of an animated GIF/WebP, keeping the animation."""
    if fmt not in ("gif", "webp"):
        return None

    frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(img):
        durations.append(frame.info.get("duration", img.info.get("duration", 100)))
        converted = frame.convert("RGBA")
        frames.append(_resize(converted, opts.max_dimension))
    if not frames:
        return None

    buffer = io.BytesIO()
    params: dict = {
        "save_all": True,
        "append_images": frames[1:],
        "loop": img.info.get("loop", 0),
        "duration": durations,
    }
    try:
        if fmt == "webp":
            params.update(quality=opts.quality, method=6)
            frames[0].save(buffer, format="WEBP", **params)
        else:
            palette_frames = [f.convert("P", palette=Image.Palette.ADAPTIVE) for f in frames]
            params["append_images"] = palette_frames[1:]
            params["optimize"] = True
            params["disposal"] = 2
            palette_frames[0].save(buffer, format="GIF", **params)
    except (OSError, ValueError):
        return None
    return buffer.getvalue()


def _candidate_formats(opts: ImageOptions, source_format: str, has_alpha: bool) -> list[str]:
    requested = opts.output_format.lower()

    if requested == "auto":
        # Keep the container the user gave us, with a nudge for formats we
        # cannot write efficiently (or at all).
        if source_format in ("jpeg", "png", "webp", "avif", "gif"):
            return [source_format]
        return ["png"] if has_alpha else ["jpeg"]

    if requested == "smallest":
        formats = ["webp", "avif", "png"]
        if not has_alpha:
            formats.insert(0, "jpeg")
        return formats

    if requested in OUTPUT_FORMATS:
        return [requested]
    return [source_format or "jpeg"]


def compress_image(src: Path, out_dir: Path, original_name: str, opts: ImageOptions) -> ImageResult:
    """Compress ``src`` into ``out_dir`` and return the winning candidate."""
    try:
        with Image.open(src) as probe:
            probe.load()
            source_format = _normalise_format(probe.format)
            animated = _is_animated(probe)
    except Image.DecompressionBombError as exc:
        raise ImageCompressionError(
            f"Image is too large to process safely ({exc})."
        ) from exc
    except (OSError, ValueError) as exc:
        raise ImageCompressionError(f"Not a readable image: {exc}") from exc

    original_size = src.stat().st_size
    requested = opts.output_format.lower()
    keep_original_allowed = opts.never_grow and requested in ("auto", "smallest")

    candidates: list[tuple[str, bytes]] = []
    notes: list[str] = []

    with Image.open(src) as img:
        meta = {"exif": img.info.get("exif"), "icc_profile": img.info.get("icc_profile")}

        if animated and requested in ("auto", "smallest"):
            # Preserve the animation rather than silently flattening it.
            target = "webp" if source_format == "webp" else "gif"
            data = _encode_animated(img, target, opts)
            if data:
                candidates.append((target, data))
            notes.append("animated")
        else:
            if animated:
                notes.append("animation flattened to first frame")

            frame = img
            if opts.strip_metadata:
                frame = ImageOps.exif_transpose(img) or img
            # copy() rather than convert(): it detaches from the file handle
            # while keeping ``info`` (palette transparency lives there).
            frame = frame.copy()
            frame = _resize(frame, opts.max_dimension)
            if frame.size != img.size:
                notes.append(f"resized to {frame.width}x{frame.height}")

            for fmt in _candidate_formats(opts, source_format, _has_alpha(frame)):
                data = _encode(frame, fmt, opts, meta)
                if data:
                    candidates.append((fmt, data))

    if not candidates and not keep_original_allowed:
        raise ImageCompressionError("Could not encode this image in the requested format.")

    best_format: str | None = None
    best_data: bytes | None = None
    for fmt, data in candidates:
        if best_data is None or len(data) < len(best_data):
            best_format, best_data = fmt, data

    resized = any(note.startswith("resized") for note in notes)
    use_original = (
        keep_original_allowed
        and not resized
        and (best_data is None or len(best_data) >= original_size)
    )

    if use_original:
        ext = _EXTENSIONS.get(source_format, Path(original_name).suffix or ".bin")
        out_name = replace_suffix(original_name, ext, "compressed")
        out_path = out_dir / out_name
        out_path.write_bytes(src.read_bytes())
        note = "already optimal - original kept"
        if notes:
            note = f"{note} ({', '.join(notes)})"
        return ImageResult(out_path, out_name, original_size, f"copy:{source_format}", note)

    assert best_data is not None and best_format is not None
    out_name = replace_suffix(original_name, _EXTENSIONS.get(best_format, ".bin"), "compressed")
    out_path = out_dir / out_name
    out_path.write_bytes(best_data)

    method = f"pillow:{best_format}"
    if source_format and source_format != best_format:
        notes.insert(0, f"{source_format} -> {best_format}")
    return ImageResult(out_path, out_name, len(best_data), method, ", ".join(notes))
