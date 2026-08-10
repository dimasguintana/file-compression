# PDF & Photo Compression

A local web app for shrinking PDFs and images. FastAPI backend, plain
HTML/CSS/JS frontend — no build step, no npm, no external services. Files never
leave your machine.

![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![FastAPI](https://img.shields.io/badge/fastapi-0.115%2B-009688)

## Quick start

```bash
./run.sh
```

Then open <http://127.0.0.1:8000>. The first run creates `.venv/` and installs
dependencies; later runs start immediately.

To change host or port:

```bash
HOST=0.0.0.0 PORT=9000 ./run.sh
```

### Manual setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/uvicorn app.main:app --reload
```

### Ghostscript (recommended)

PDF compression is far stronger with Ghostscript installed:

```bash
sudo apt install ghostscript      # Debian/Ubuntu
brew install ghostscript          # macOS
```

Without it the app falls back to a pure-Python path (pikepdf + Pillow) that
still works but saves less on some files. The UI tells you which one is active.

## What it does

**Images** — JPEG, PNG, WebP, AVIF, GIF, BMP, TIFF

| Control | Effect |
| --- | --- |
| Quality | 1–100, drives the lossy encoder |
| Output format | Keep original, *Smallest* (tries every codec and picks the winner), or force JPEG/PNG/WebP/AVIF |
| Resize longest edge | Optional downscale, LANCZOS resampling |
| Strip EXIF | Removes camera/GPS metadata; the ICC colour profile is kept so colours don't shift |
| Palette reduction for PNG | Lossy PNG quantisation, applied only to true-colour images |

Presets: **High quality** (q92) · **Balanced** (q78) · **Small** (q62, 1920 px) ·
**Tiny** (q45, 1280 px).

**PDFs**

| Mode | Image DPI | Use for |
| --- | --- | --- |
| Lossless | untouched | Any PDF — rebuilds and restreams, no quality loss |
| Light | 200 | Documents you may still print |
| Balanced | 150 | Email and sharing (default) |
| Strong | 110 | Big scans where size matters more than sharpness |
| Extreme | 72 | Screen reading only |

Plus optional grayscale conversion and "fast web view" (linearisation).

### Guarantees

- **Output is never larger than input.** Every engine's result competes against
  the untouched original, and the smallest one wins. (Turn this off with the
  checkbox if you always want a re-encode.)
- **Page count is verified.** A compressed PDF is only accepted if it reopens
  cleanly with the same number of pages.
- **One bad file doesn't sink the batch.** Failures are reported per file.
- **Nothing is uploaded anywhere.** All processing is local; results are deleted
  from disk after 60 minutes.

## How it works

```
Browser ──POST /api/jobs──▶ FastAPI ──▶ ThreadPoolExecutor
   ▲                                          │
   └──poll GET /api/jobs/{id}────────────┐    ├─ images.py  → Pillow
                                         │    └─ pdfs.py    → pikepdf │ Ghostscript │ Pillow
   ◀──GET .../download, .../archive──────┘
```

Uploads are streamed to disk (never buffered whole in RAM), then handed to a
worker thread. The browser polls for progress and downloads results individually
or as a zip.

Each engine produces *candidates*; the orchestrator picks the smallest valid
one. That is why `text.pdf` may come back from the Python engine while
`scanned.pdf` comes back from Ghostscript — whichever actually won.

## Project layout

```
app/
  main.py              FastAPI routes, upload streaming, lifespan
  config.py            every tunable setting, all env-overridable
  jobs.py              job registry, worker pool, zip building, TTL cleanup
  compress/
    images.py          Pillow strategies and format selection
    pdfs.py            pikepdf / Ghostscript / pure-Python engines
    utils.py           magic-byte sniffing, filename sanitising, formatting
  static/
    index.html         the whole UI
    style.css          light + dark themes
    app.js             upload, polling, rendering
tests/
  make_fixtures.py     generates sample files
  test_app.py          42 unit + end-to-end tests
```

## Configuration

All optional, set as environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `COMPRESSOR_MAX_FILE_MB` | `250` | Per-file upload limit |
| `COMPRESSOR_MAX_FILES` | `50` | Files per batch |
| `COMPRESSOR_WORKERS` | `4` | Concurrent compression threads |
| `COMPRESSOR_JOB_TTL_MIN` | `60` | How long results stay on disk |
| `COMPRESSOR_DATA_DIR` | `./data` | Where jobs are stored |
| `COMPRESSOR_GS_BIN` | auto-detected | Path to the `gs` binary |
| `COMPRESSOR_GS_TIMEOUT` | `600` | Seconds before a Ghostscript run is killed |
| `COMPRESSOR_MAX_IMAGE_PIXELS` | `150000000` | Decompression-bomb guard |

## Tests

```bash
./.venv/bin/pip install pytest httpx
./.venv/bin/python tests/make_fixtures.py
./.venv/bin/python -m pytest tests/ -q
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/capabilities` | Limits, supported formats, Ghostscript status |
| `POST` | `/api/jobs` | Upload files + options, returns a job |
| `GET` | `/api/jobs/{id}` | Poll status and per-file results |
| `GET` | `/api/jobs/{id}/files/{fid}/download` | One compressed file |
| `GET` | `/api/jobs/{id}/archive` | All results as a zip |
| `DELETE` | `/api/jobs/{id}` | Delete a job and its files now |
| `GET` | `/healthz` | Liveness check |

Interactive docs are at `/docs` while the server is running.

## A note on binding to 0.0.0.0

The app has no authentication — it is built to run on your own machine. If you
expose it on a network, put it behind a reverse proxy with access control.
