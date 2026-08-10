"""End-to-end tests. Run with: .venv/bin/pytest -q"""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

import pikepdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config
from app.compress import ImageOptions, PdfOptions, compress_image, compress_pdf, safe_filename
from app.compress.images import ImageCompressionError
from app.compress.pdfs import PdfCompressionError

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def fixtures_exist():
    if not (FIXTURES / "photo.jpg").exists():
        from . import make_fixtures  # type: ignore

        make_fixtures.main()


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def upload(client, files, **options):
    payload = [("files", (name, data, ctype)) for name, data, ctype in files]
    return client.post("/api/jobs", files=payload, data=options)


def wait_for(client, job_id, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["complete"]:
            return job
        time.sleep(0.15)
    raise AssertionError("job did not finish in time")


def read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# --------------------------------------------------------------------------- #
# Unit: helpers
# --------------------------------------------------------------------------- #
def test_safe_filename_blocks_traversal():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("..\\..\\windows\\system32\\x.dll") == "x.dll"
    assert safe_filename("") == "file"
    assert safe_filename("...") == "file"
    assert "/" not in safe_filename("a/b/c.png")
    assert safe_filename("CON.pdf") == "file"
    assert safe_filename("bad\x00name.png") == "bad_name.png"
    # Unicode names stay readable.
    assert safe_filename("naïve photo (1).JPG") == "naïve photo (1).JPG"
    assert safe_filename("報告書.pdf") == "報告書.pdf"


# --------------------------------------------------------------------------- #
# Unit: images
# --------------------------------------------------------------------------- #
def test_image_shrinks_jpeg(tmp_path):
    src = FIXTURES / "photo.jpg"
    result = compress_image(src, tmp_path, "photo.jpg", ImageOptions(quality=78))
    assert result.output_size < src.stat().st_size * 0.6
    with Image.open(result.output_path) as img:
        assert img.format == "JPEG"
        assert img.size == (2400, 1600)


def test_image_never_grows(tmp_path):
    """An already-tiny GIF must come back no larger than it went in."""
    src = FIXTURES / "spin.gif"
    result = compress_image(src, tmp_path, "spin.gif", ImageOptions(quality=95))
    assert result.output_size <= src.stat().st_size


def test_image_alpha_survives_png(tmp_path):
    result = compress_image(
        FIXTURES / "logo-alpha.png", tmp_path, "logo-alpha.png", ImageOptions(quality=80)
    )
    with Image.open(result.output_path) as img:
        assert img.convert("RGBA").getchannel("A").getextrema()[0] == 0  # still transparent


def test_image_alpha_flattened_for_jpeg(tmp_path):
    result = compress_image(
        FIXTURES / "logo-alpha.png", tmp_path, "logo-alpha.png",
        ImageOptions(quality=80, output_format="jpeg"),
    )
    with Image.open(result.output_path) as img:
        assert img.format == "JPEG"
        assert img.mode == "RGB"


def test_image_resize(tmp_path):
    result = compress_image(
        FIXTURES / "photo.jpg", tmp_path, "photo.jpg",
        ImageOptions(quality=78, max_dimension=800),
    )
    with Image.open(result.output_path) as img:
        assert max(img.size) == 800


def test_image_format_conversion(tmp_path):
    for fmt, pil_name in (("webp", "WEBP"), ("avif", "AVIF"), ("png", "PNG")):
        result = compress_image(
            FIXTURES / "photo.jpg", tmp_path, "photo.jpg",
            ImageOptions(quality=70, output_format=fmt),
        )
        with Image.open(result.output_path) as img:
            assert img.format == pil_name, fmt


def test_image_smallest_picks_a_winner(tmp_path):
    result = compress_image(
        FIXTURES / "photo.png", tmp_path, "photo.png",
        ImageOptions(quality=70, output_format="smallest"),
    )
    assert result.output_size < (FIXTURES / "photo.png").stat().st_size


def test_animated_gif_keeps_frames(tmp_path):
    result = compress_image(
        FIXTURES / "spin.gif", tmp_path, "spin.gif",
        ImageOptions(quality=70, output_format="auto"),
    )
    with Image.open(result.output_path) as img:
        assert getattr(img, "n_frames", 1) == 12


def test_exif_orientation_baked_in_when_stripping(tmp_path):
    """Stripping EXIF must rotate the pixels, or the image would display sideways."""
    portrait = Image.new("RGB", (100, 300), "red")
    exif = Image.Exif()
    exif[274] = 6  # Orientation: rotate 90 CW
    src = tmp_path / "rotated.jpg"
    portrait.save(src, exif=exif, quality=90)

    result = compress_image(src, tmp_path, "rotated.jpg", ImageOptions(strip_metadata=True))
    with Image.open(result.output_path) as img:
        assert img.size == (300, 100)          # pixels rotated
        assert img.getexif().get(274) in (None, 1)  # tag gone


def test_exif_kept_when_requested(tmp_path):
    photo = Image.new("RGB", (200, 200), "blue")
    exif = Image.Exif()
    exif[271] = "TestCam"  # Make
    src = tmp_path / "meta.jpg"
    photo.save(src, exif=exif, quality=95)

    result = compress_image(
        src, tmp_path, "meta.jpg", ImageOptions(strip_metadata=False, never_grow=False)
    )
    with Image.open(result.output_path) as img:
        assert img.getexif().get(271) == "TestCam"


def test_palette_transparency_survives(tmp_path):
    """A GIF-style paletted image with a transparent index must stay transparent."""
    palette = Image.new("P", (200, 200))
    palette.putpalette([0, 0, 0] + [255, 0, 0] * 255)
    palette.info["transparency"] = 0
    src = tmp_path / "sprite.png"
    palette.save(src, transparency=0)

    result = compress_image(src, tmp_path, "sprite.png", ImageOptions(never_grow=False))
    with Image.open(result.output_path) as img:
        assert "transparency" in img.info or img.mode == "RGBA"


@pytest.mark.parametrize("mode,size", [("1", (600, 600)), ("L", (600, 600)), ("I;16", (400, 400))])
def test_low_bit_depth_png_not_quantised(tmp_path, mode, size):
    """Quantising a bilevel/gray/16-bit PNG loses fidelity for no size win."""
    src = tmp_path / f"{mode.replace(';', '')}.png"
    Image.new(mode, size, 1 if mode == "1" else 30000 if mode == "I;16" else 128).save(src)

    result = compress_image(
        src, tmp_path, src.name,
        ImageOptions(quality=75, output_format="png", lossy_png=True, never_grow=False),
    )
    with Image.open(result.output_path) as img:
        assert img.mode != "P", f"{mode} was needlessly palettised"


def test_paletted_resize_uses_true_colour(tmp_path):
    """Resizing a paletted image must not fall back to blocky NEAREST output."""
    rgb = Image.new("RGB", (800, 800))
    for x in range(800):
        for y in range(0, 800, 8):
            rgb.putpixel((x, y), (x % 256, y % 256, 128))
    src = tmp_path / "pal.png"
    rgb.convert("P", palette=Image.Palette.ADAPTIVE).save(src)

    result = compress_image(
        src, tmp_path, "pal.png",
        ImageOptions(quality=80, max_dimension=200, output_format="png"),
    )
    with Image.open(result.output_path) as img:
        assert max(img.size) == 200


def test_corrupt_image_raises(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage" * 100)
    with pytest.raises(ImageCompressionError):
        compress_image(bad, tmp_path, "bad.png", ImageOptions())


# --------------------------------------------------------------------------- #
# Unit: PDFs
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["lossless", "light", "balanced", "strong", "extreme"])
def test_pdf_modes_preserve_pages(tmp_path, mode):
    src = FIXTURES / "scanned.pdf"
    out = tmp_path / mode
    out.mkdir()
    result = compress_pdf(src, out, "scanned.pdf", PdfOptions(mode=mode))
    assert result.output_size <= src.stat().st_size
    with pikepdf.open(result.output_path) as pdf:
        assert len(pdf.pages) == 3


def test_pdf_lossy_actually_shrinks(tmp_path):
    src = FIXTURES / "scanned.pdf"
    result = compress_pdf(src, tmp_path, "scanned.pdf", PdfOptions(mode="balanced"))
    assert result.output_size < src.stat().st_size * 0.5


def test_pdf_grayscale(tmp_path):
    result = compress_pdf(
        FIXTURES / "scanned.pdf", tmp_path, "scanned.pdf",
        PdfOptions(mode="balanced", grayscale=True),
    )
    with pikepdf.open(result.output_path) as pdf:
        assert len(pdf.pages) == 3


def test_pdf_python_fallback_without_ghostscript(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GHOSTSCRIPT_BIN", None)
    src = FIXTURES / "scanned.pdf"
    result = compress_pdf(src, tmp_path, "scanned.pdf", PdfOptions(mode="strong"))
    assert result.method == "python-images"
    assert result.output_size < src.stat().st_size * 0.6
    with pikepdf.open(result.output_path) as pdf:
        assert len(pdf.pages) == 3


def test_pdf_password_protected_is_reported(tmp_path):
    locked = tmp_path / "locked.pdf"
    with pikepdf.open(FIXTURES / "text.pdf") as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(user="secret", owner="secret"))
    with pytest.raises(PdfCompressionError, match="password"):
        compress_pdf(locked, tmp_path, "locked.pdf", PdfOptions())


def test_pdf_owner_password_only_still_works(tmp_path):
    """Owner-locked (but readable) PDFs compress; the restriction is dropped."""
    locked = tmp_path / "owner.pdf"
    with pikepdf.open(FIXTURES / "scanned.pdf") as pdf:
        pdf.save(locked, encryption=pikepdf.Encryption(user="", owner="owner-pw"))
    result = compress_pdf(locked, tmp_path, "owner.pdf", PdfOptions(mode="balanced"))
    assert "encryption removed" in result.note
    with pikepdf.open(result.output_path) as pdf:
        assert len(pdf.pages) == 3


def test_corrupt_pdf_raises(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.7\nthis is not really a pdf\n%%EOF")
    with pytest.raises(PdfCompressionError):
        compress_pdf(bad, tmp_path, "bad.pdf", PdfOptions())


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def test_capabilities(client):
    body = client.get("/api/capabilities").json()
    assert "ghostscript" in body
    assert "jpeg" in body["image_formats"]
    assert "balanced" in body["pdf_modes"]


def test_index_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "PDF &amp; Photo Compression" in response.text


def test_full_batch_roundtrip(client):
    response = upload(
        client,
        [
            ("photo.jpg", read("photo.jpg"), "image/jpeg"),
            ("scanned.pdf", read("scanned.pdf"), "application/pdf"),
            ("logo-alpha.png", read("logo-alpha.png"), "image/png"),
        ],
        image_quality="75", pdf_mode="balanced",
    )
    assert response.status_code == 201
    job = wait_for(client, response.json()["id"])

    assert job["succeeded"] == 3
    assert job["failed"] == 0
    assert job["totals"]["saved_percent"] > 50

    for entry in job["files"]:
        assert entry["status"] == "done"
        assert entry["output_size"] <= entry["input_size"]
        download = client.get(f"/api/jobs/{job['id']}/files/{entry['id']}/download")
        assert download.status_code == 200
        assert len(download.content) == entry["output_size"]

    archive = client.get(f"/api/jobs/{job['id']}/archive")
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zf:
        assert len(zf.namelist()) == 3
        assert zf.testzip() is None


def test_unsupported_file_is_rejected_per_file(client):
    response = upload(
        client,
        [
            ("notes.txt", read("notes.txt"), "text/plain"),
            ("photo.webp", read("photo.webp"), "image/webp"),
        ],
    )
    job = wait_for(client, response.json()["id"])
    by_name = {f["name"]: f for f in job["files"]}
    assert by_name["notes.txt"]["status"] == "error"
    assert "Unsupported" in by_name["notes.txt"]["error"]
    assert by_name["photo.webp"]["status"] == "done"  # the good file still ran


def test_mislabelled_extension_is_routed_by_content(client):
    """A PDF uploaded as .jpg is still detected and compressed as a PDF."""
    response = upload(client, [("trick.jpg", read("scanned.pdf"), "image/jpeg")])
    job = wait_for(client, response.json()["id"])
    assert job["files"][0]["kind"] == "pdf"
    assert job["files"][0]["status"] == "done"


def test_traversal_filename_is_neutralised(client):
    response = upload(client, [("../../../evil.png", read("logo-alpha.png"), "image/png")])
    job = wait_for(client, response.json()["id"])
    entry = job["files"][0]
    assert entry["name"] == "evil.png"
    assert ".." not in (entry["output_name"] or "")


def test_too_many_files(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_FILES_PER_JOB", 2)
    response = upload(client, [("a.gif", read("spin.gif"), "image/gif")] * 3)
    assert response.status_code == 400


def test_oversize_file_is_rejected(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_FILE_SIZE", 1024)
    response = upload(client, [("photo.jpg", read("photo.jpg"), "image/jpeg")])
    job = wait_for(client, response.json()["id"])
    assert job["files"][0]["status"] == "error"
    assert "limit" in job["files"][0]["error"]


def test_empty_file_is_rejected(client):
    response = upload(client, [("empty.png", b"", "image/png")])
    job = wait_for(client, response.json()["id"])
    assert job["files"][0]["status"] == "error"


def test_job_delete_and_404(client):
    response = upload(client, [("spin.gif", read("spin.gif"), "image/gif")])
    job_id = response.json()["id"]
    wait_for(client, job_id)
    assert client.delete(f"/api/jobs/{job_id}").status_code == 204
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/archive").status_code == 404


def test_unknown_job_404(client):
    assert client.get("/api/jobs/does-not-exist").status_code == 404


def test_colliding_output_names_keep_distinct_content(client):
    """photo.jpg and photo.tiff both want to become photo-compressed.jpg.

    They must not overwrite each other on disk: each download has to return
    that file's own bytes, and the archive must hold both.
    """
    response = upload(
        client,
        [
            ("photo.jpg", read("photo.jpg"), "image/jpeg"),
            ("photo.tiff", read("photo.tiff"), "image/tiff"),
        ],
        image_quality="78",
    )
    job = wait_for(client, response.json()["id"])
    assert job["succeeded"] == 2

    downloaded = []
    for entry in job["files"]:
        body = client.get(f"/api/jobs/{job['id']}/files/{entry['id']}/download").content
        assert len(body) == entry["output_size"], entry["name"]
        downloaded.append(body)
    assert downloaded[0] != downloaded[1]

    archive = client.get(f"/api/jobs/{job['id']}/archive")
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zf:
        sizes = sorted(info.file_size for info in zf.infolist())
        assert len(sizes) == 2
        assert sizes[0] != sizes[1]


def test_duplicate_names_both_land_in_archive(client):
    response = upload(
        client,
        [
            ("photo.jpg", read("photo.jpg"), "image/jpeg"),
            ("photo.jpg", read("photo.webp"), "image/webp"),
        ],
        image_format="jpeg",
    )
    job = wait_for(client, response.json()["id"])
    archive = client.get(f"/api/jobs/{job['id']}/archive")
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zf:
        assert len(zf.namelist()) == 2
        assert len(set(zf.namelist())) == 2


def test_expired_jobs_are_purged(client, monkeypatch):
    response = upload(client, [("spin.gif", read("spin.gif"), "image/gif")])
    job_id = response.json()["id"]
    wait_for(client, job_id)
    directory = config.JOBS_DIR / job_id
    assert directory.exists()

    monkeypatch.setattr(config, "JOB_TTL_SECONDS", -1)
    from app.jobs import manager

    manager.purge_expired()
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    assert not directory.exists()
