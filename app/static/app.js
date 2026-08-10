/* PDF & Photo Compression - front-end controller.
 *
 * Flow: collect files -> POST /api/jobs -> poll /api/jobs/{id} until complete
 * -> render per-file results with download links.
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const el = {
    dropzone: $("dropzone"),
    fileInput: $("fileInput"),
    dzHint: $("dzHint"),
    queue: $("queue"),
    queueList: $("queueList"),
    queueCount: $("queueCount"),
    clearQueue: $("clearQueue"),
    caps: $("caps"),
    imagePanel: $("imagePanel"),
    pdfPanel: $("pdfPanel"),
    imageBadge: $("imageBadge"),
    pdfBadge: $("pdfBadge"),
    imagePreset: $("imagePreset"),
    imageQuality: $("imageQuality"),
    imageQualityOut: $("imageQualityOut"),
    imageFormat: $("imageFormat"),
    imageMaxDim: $("imageMaxDim"),
    imageStrip: $("imageStrip"),
    imageLossyPng: $("imageLossyPng"),
    pdfGrayscale: $("pdfGrayscale"),
    pdfLinearize: $("pdfLinearize"),
    gsNote: $("gsNote"),
    neverGrow: $("neverGrow"),
    compressBtn: $("compressBtn"),
    results: $("results"),
    resultList: $("resultList"),
    summarySaved: $("summarySaved"),
    summaryBar: $("summaryBar"),
    summarySizes: $("summarySizes"),
    downloadAll: $("downloadAll"),
    toast: $("toast"),
    ttl: $("ttl"),
  };

  const PRESETS = {
    quality: { quality: 92, maxDim: "0" },
    balanced: { quality: 78, maxDim: "0" },
    small: { quality: 62, maxDim: "1920" },
    tiny: { quality: 45, maxDim: "1280" },
  };

  const IMAGE_EXT = /\.(jpe?g|png|webp|avif|gif|bmp|tiff?|heic|heif)$/i;
  const PDF_EXT = /\.pdf$/i;

  /** Files staged for upload, keyed by name+size+mtime so re-drops don't duplicate. */
  let queue = [];
  let caps = null;
  let pollTimer = null;
  let currentJobId = null;
  let busy = false;

  // ----------------------------------------------------------- utilities --
  function humanSize(bytes) {
    if (bytes === null || bytes === undefined) return "—";
    const units = ["B", "KB", "MB", "GB"];
    let value = bytes;
    let i = 0;
    while (Math.abs(value) >= 1024 && i < units.length - 1) {
      value /= 1024;
      i += 1;
    }
    return i === 0 ? `${Math.round(value)} B` : `${value.toFixed(1)} ${units[i]}`;
  }

  function plural(count, word) {
    return `${count} ${word}${count === 1 ? "" : "s"}`;
  }

  function fileKey(file) {
    return `${file.name}|${file.size}|${file.lastModified}`;
  }

  function kindOf(file) {
    if (PDF_EXT.test(file.name) || file.type === "application/pdf") return "pdf";
    if (IMAGE_EXT.test(file.name) || file.type.startsWith("image/")) return "image";
    return "other";
  }

  let toastTimer = null;
  function toast(message) {
    el.toast.textContent = message;
    el.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.toast.hidden = true; }, 5000);
  }

  function svgIcon(paths) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
      stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;
  }

  const DOWNLOAD_ICON = svgIcon(
    '<path d="M12 4v12"/><path d="m7 11 5 5 5-5"/><path d="M20 16v2a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-2"/>'
  );

  // -------------------------------------------------------------- queue ---
  function addFiles(fileList) {
    const seen = new Set(queue.map(fileKey));
    let rejected = 0;

    for (const file of fileList) {
      if (seen.has(fileKey(file))) continue;
      if (kindOf(file) === "other") { rejected += 1; continue; }
      if (caps && file.size > caps.max_file_size) { rejected += 1; continue; }
      if (caps && queue.length >= caps.max_files) {
        toast(`Only ${caps.max_files} files can be queued at once.`);
        break;
      }
      seen.add(fileKey(file));
      queue.push(file);
    }

    if (rejected > 0) {
      toast(`${plural(rejected, "file")} skipped — unsupported type or over the size limit.`);
    }
    renderQueue();
  }

  function removeFile(key) {
    queue = queue.filter((f) => fileKey(f) !== key);
    renderQueue();
  }

  function renderQueue() {
    el.queueList.replaceChildren();

    let images = 0;
    let pdfs = 0;
    for (const file of queue) {
      if (kindOf(file) === "pdf") pdfs += 1; else images += 1;

      const li = document.createElement("li");
      li.className = "chip";

      const name = document.createElement("span");
      name.className = "chip-name";
      name.textContent = file.name;              // textContent: no HTML injection
      name.title = file.name;

      const size = document.createElement("span");
      size.className = "chip-size";
      size.textContent = humanSize(file.size);

      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "×";
      remove.setAttribute("aria-label", `Remove ${file.name}`);
      remove.addEventListener("click", () => removeFile(fileKey(file)));

      li.append(name, size, remove);
      el.queueList.append(li);
    }

    el.queue.hidden = queue.length === 0;
    el.queueCount.textContent = String(queue.length);
    el.compressBtn.disabled = queue.length === 0 || busy;

    el.imageBadge.hidden = images === 0;
    el.imageBadge.textContent = String(images);
    el.pdfBadge.hidden = pdfs === 0;
    el.pdfBadge.textContent = String(pdfs);

    // Dim the panel that has nothing to act on, but leave it usable.
    el.imagePanel.classList.toggle("dimmed", queue.length > 0 && images === 0);
    el.pdfPanel.classList.toggle("dimmed", queue.length > 0 && pdfs === 0);
  }

  // ----------------------------------------------------------- settings ---
  function applyPreset(name) {
    const preset = PRESETS[name];
    if (!preset) return;
    el.imageQuality.value = String(preset.quality);
    el.imageQualityOut.textContent = String(preset.quality);
    el.imageMaxDim.value = preset.maxDim;
    for (const btn of el.imagePreset.querySelectorAll("button")) {
      btn.classList.toggle("active", btn.dataset.preset === name);
    }
  }

  function clearPresetHighlight() {
    for (const btn of el.imagePreset.querySelectorAll("button")) {
      btn.classList.remove("active");
    }
  }

  function selectedPdfMode() {
    const checked = document.querySelector('input[name="pdfmode"]:checked');
    return checked ? checked.value : "balanced";
  }

  function buildFormData() {
    const data = new FormData();
    for (const file of queue) data.append("files", file, file.name);
    data.append("image_quality", el.imageQuality.value);
    data.append("image_format", el.imageFormat.value);
    data.append("image_max_dimension", el.imageMaxDim.value);
    data.append("image_strip_metadata", String(el.imageStrip.checked));
    data.append("image_lossy_png", String(el.imageLossyPng.checked));
    data.append("pdf_mode", selectedPdfMode());
    data.append("pdf_grayscale", String(el.pdfGrayscale.checked));
    data.append("pdf_linearize", String(el.pdfLinearize.checked));
    data.append("never_grow", String(el.neverGrow.checked));
    return data;
  }

  // ------------------------------------------------------------ results ---
  function renderResults(job) {
    el.results.hidden = false;
    el.resultList.replaceChildren();

    for (const file of job.files) {
      el.resultList.append(buildResultRow(job, file));
    }

    const totals = job.totals;
    const anyDone = job.succeeded > 0;
    el.summarySaved.textContent = anyDone ? `${totals.saved_percent}%` : "—";
    el.summaryBar.style.width = `${anyDone ? Math.max(0, Math.min(100, totals.saved_percent)) : 0}%`;
    el.summarySizes.textContent = anyDone
      ? `${totals.input_size_human} → ${totals.output_size_human} · ${totals.saved_human} saved across ${plural(job.succeeded, "file")}`
      : "Working…";
    el.downloadAll.disabled = !anyDone;
  }

  function buildResultRow(job, file) {
    const li = document.createElement("li");
    li.className = "result";

    const icon = document.createElement("div");
    icon.className = `result-icon ${file.kind === "pdf" ? "pdf" : "image"}`;
    icon.textContent = file.kind === "pdf" ? "PDF" : "IMG";

    const body = document.createElement("div");
    body.className = "result-body";

    const name = document.createElement("div");
    name.className = "result-name";
    name.textContent = file.name;
    name.title = file.name;
    body.append(name);

    if (file.status === "done") {
      const meta = document.createElement("div");
      meta.className = "result-meta";
      meta.textContent = `${file.input_size_human} → ${file.output_size_human}`;
      body.append(meta);
      if (file.note) {
        const note = document.createElement("div");
        note.className = "result-note";
        note.textContent = file.note;
        body.append(note);
      }
    } else if (file.status === "error") {
      const err = document.createElement("div");
      err.className = "result-error";
      err.textContent = file.error || "Failed.";
      body.append(err);
    } else {
      const meta = document.createElement("div");
      meta.className = "result-meta";
      meta.textContent = file.status === "processing" ? "Compressing…" : "Queued";
      body.append(meta);
      const track = document.createElement("div");
      track.className = "result-track";
      track.innerHTML = '<div class="result-track-fill"></div>';
      body.append(track);
    }

    const right = document.createElement("div");
    right.className = "result-right";

    const pill = document.createElement("span");
    if (file.status === "done") {
      const saved = file.saved_percent;
      pill.className = `pill ${saved > 1 ? "good" : "flat"}`;
      pill.textContent = saved > 1 ? `−${saved}%` : "no gain";
    } else if (file.status === "error") {
      pill.className = "pill bad";
      pill.textContent = "failed";
    } else {
      pill.className = "pill wait";
      pill.textContent = file.status === "processing" ? "working" : "queued";
    }
    right.append(pill);

    if (file.status === "done") {
      const link = document.createElement("a");
      link.className = "icon-btn";
      link.href = `/api/jobs/${job.id}/files/${file.id}/download`;
      link.setAttribute("download", "");
      link.title = `Download ${file.output_name}`;
      link.setAttribute("aria-label", `Download ${file.output_name}`);
      link.innerHTML = DOWNLOAD_ICON;
      right.append(link);
    }

    li.append(icon, body, right);
    return li;
  }

  // ------------------------------------------------------------- upload ---
  function setBusy(state, label) {
    busy = state;
    el.compressBtn.disabled = state || queue.length === 0;
    el.compressBtn.classList.toggle("loading", state);
    el.compressBtn.querySelector(".btn-label").textContent =
      label || (state ? "Working…" : "Compress");
  }

  function upload(formData) {
    // XHR rather than fetch: it reports upload progress, which matters for
    // multi-hundred-megabyte batches.
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/jobs");
      xhr.responseType = "json";

      xhr.upload.addEventListener("progress", (event) => {
        if (!event.lengthComputable) return;
        const percent = Math.round((event.loaded / event.total) * 100);
        setBusy(true, percent < 100 ? `Uploading ${percent}%` : "Compressing…");
      });

      xhr.addEventListener("load", () => {
        const body = xhr.response;
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(body);
        } else {
          reject(new Error((body && body.detail) || `Upload failed (${xhr.status})`));
        }
      });
      xhr.addEventListener("error", () => reject(new Error("Network error during upload.")));
      xhr.addEventListener("abort", () => reject(new Error("Upload cancelled.")));
      xhr.send(formData);
    });
  }

  async function poll(jobId) {
    let response;
    try {
      response = await fetch(`/api/jobs/${jobId}`);
    } catch {
      toast("Lost contact with the server.");
      setBusy(false);
      return;
    }
    if (!response.ok) {
      toast("This job expired.");
      setBusy(false);
      return;
    }

    const job = await response.json();
    if (jobId !== currentJobId) return;  // a newer batch superseded this one

    renderResults(job);

    if (job.complete) {
      setBusy(false);
      if (job.failed > 0 && job.succeeded === 0) toast("No files could be compressed.");
    } else {
      pollTimer = setTimeout(() => poll(jobId), 600);
    }
  }

  async function startCompression() {
    if (busy || queue.length === 0) return;
    clearTimeout(pollTimer);

    // Drop the previous job's files server-side before starting a new batch.
    if (currentJobId) {
      const stale = currentJobId;
      currentJobId = null;
      fetch(`/api/jobs/${stale}`, { method: "DELETE" }).catch(() => {});
    }

    setBusy(true, "Uploading…");
    el.results.hidden = false;
    el.resultList.replaceChildren();
    el.downloadAll.disabled = true;

    try {
      const job = await upload(buildFormData());
      currentJobId = job.id;
      renderResults(job);
      poll(job.id);
    } catch (error) {
      setBusy(false);
      toast(error.message || "Something went wrong.");
    }
  }

  // -------------------------------------------------------------- wiring --
  el.dropzone.addEventListener("click", () => el.fileInput.click());
  el.dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      el.fileInput.click();
    }
  });
  el.fileInput.addEventListener("change", () => {
    addFiles(el.fileInput.files);
    el.fileInput.value = "";  // allow re-picking the same file
  });

  ["dragenter", "dragover"].forEach((type) => {
    el.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      el.dropzone.classList.add("dragging");
    });
  });
  ["dragleave", "drop"].forEach((type) => {
    el.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      if (type === "dragleave" && el.dropzone.contains(event.relatedTarget)) return;
      el.dropzone.classList.remove("dragging");
    });
  });
  el.dropzone.addEventListener("drop", (event) => {
    if (event.dataTransfer && event.dataTransfer.files.length) addFiles(event.dataTransfer.files);
  });

  // Stop the browser from navigating away when a file misses the drop zone.
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => e.preventDefault());

  el.clearQueue.addEventListener("click", () => { queue = []; renderQueue(); });

  el.imagePreset.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-preset]");
    if (button) applyPreset(button.dataset.preset);
  });
  el.imageQuality.addEventListener("input", () => {
    el.imageQualityOut.textContent = el.imageQuality.value;
    clearPresetHighlight();
  });
  el.imageMaxDim.addEventListener("change", clearPresetHighlight);

  el.compressBtn.addEventListener("click", startCompression);
  el.downloadAll.addEventListener("click", () => {
    if (currentJobId) window.location.href = `/api/jobs/${currentJobId}/archive`;
  });

  // ---------------------------------------------------------- capabilities --
  fetch("/api/capabilities")
    .then((r) => r.json())
    .then((data) => {
      caps = data;
      el.ttl.textContent = String(data.job_ttl_minutes);
      el.dzHint.textContent =
        `PDF · JPEG · PNG · WebP · AVIF · GIF · BMP · TIFF — up to ${data.max_file_size_human} each, ${data.max_files} at a time`;

      const badges = [];
      badges.push(
        data.ghostscript
          ? '<span class="cap ok">Ghostscript ready</span>'
          : '<span class="cap miss">Ghostscript missing</span>'
      );
      if (data.image_formats.includes("avif")) badges.push('<span class="cap">AVIF</span>');
      badges.push(`<span class="cap">v${data.version}</span>`);
      el.caps.innerHTML = badges.join("");

      if (!data.ghostscript) {
        el.gsNote.hidden = false;
        el.gsNote.textContent =
          "Ghostscript is not installed, so PDFs use the slower pure-Python fallback and compress less. Install it with: sudo apt install ghostscript";
      }
    })
    .catch(() => toast("Could not reach the server."));

  renderQueue();
})();
