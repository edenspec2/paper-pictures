"""PDF in, PDF out: click a highlighted word to open its picture.

    python app.py
    python app.py paper.pdf -o paper_figures.pdf

Yellow highlights are figure and scheme mentions. Green highlights are
chemical names. In Adobe Acrobat, click a highlight to open the picture
beside the sentence, then use the arrow buttons to move it. The click
does not jump to another page.
"""

from __future__ import annotations

import argparse
import email
import json
import os
import threading
import uuid
from email import policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import figures

MAX_BYTES = 80 * 1024 * 1024
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paper pictures</title>
<style>
  :root {
    --paper: #f6f1e6;
    --ink: #1c1712;
    --muted: #5c5348;
    --rule: #d9d0c1;
    --green: #0c6b52;
    --green-soft: #e5f3ee;
    --shadow: 0 18px 50px rgba(40, 30, 10, .14);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    background:
      radial-gradient(920px 440px at 8% -8%, rgba(12, 107, 82, .14), transparent 62%),
      radial-gradient(720px 380px at 100% 0%, rgba(184, 97, 0, .12), transparent 58%),
      #e7e0d2;
    color: var(--ink);
    font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  }
  #home { max-width: 1040px; margin: 0 auto; padding: 48px 22px 80px; }
  .mast { max-width: 40rem; margin-bottom: 28px; }
  h1 {
    font-size: clamp(44px, 7vw, 72px);
    font-weight: 600;
    letter-spacing: -0.035em;
    line-height: 0.92;
    margin: 0 0 14px;
  }
  .lead { margin: 0; color: var(--muted); font-size: 21px; line-height: 1.4; }
  .markline { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 18px; }
  .swatch {
    border-radius: 4px;
    padding: 2px 7px 3px;
    font-family: "Segoe UI", sans-serif;
    font-size: 13px;
    font-weight: 700;
  }
  .swatch.fig { background: rgba(255, 184, 5, .82); box-shadow: inset 0 -2px 0 #b86100; }
  .swatch.mol { background: rgba(64, 199, 107, .82); box-shadow: inset 0 -2px 0 #096b2e; }
  .swatch.clip { background: rgba(70, 130, 230, .55); box-shadow: inset 0 -2px 0 #1d4e9e; }
  .showcase {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 14px;
    margin: 0 0 18px;
  }
  .scene {
    background: rgba(246, 241, 230, .92);
    border-radius: 18px;
    padding: 12px 12px 16px;
    box-shadow: var(--shadow);
  }
  .scene h2 { margin: 12px 4px 4px; font-size: 20px; font-weight: 600; }
  .scene p { margin: 0 4px; color: var(--muted); font-size: 15px; line-height: 1.4; }
  .stagelet {
    position: relative;
    height: 178px;
    border-radius: 12px;
    background: #fffdf8;
    border: 1px solid #eadfce;
    overflow: hidden;
  }
  .ruleline { position: absolute; height: 7px; border-radius: 99px; background: #efe6d6; }
  .stagelet .chip {
    position: absolute;
    font: 700 12px "Segoe UI", sans-serif;
    border-radius: 3px;
    padding: 1px 5px 2px;
  }
  .chip.fig { background: rgba(255, 184, 5, .82); box-shadow: inset 0 -2px 0 #b86100; }
  .chip.mol { background: rgba(64, 199, 107, .82); box-shadow: inset 0 -2px 0 #096b2e; }
  .chip.clip { background: rgba(70, 130, 230, .55); box-shadow: inset 0 -2px 0 #1d4e9e; }
  .floatcard {
    position: absolute;
    right: 10px;
    bottom: 12px;
    width: 122px;
    background: white;
    border: 1px solid #0c6b52;
    border-radius: 10px;
    box-shadow: 0 12px 28px rgba(40, 30, 10, .18);
    overflow: hidden;
  }
  .floathead {
    background: #e7f2ec;
    color: #14382c;
    font: 600 11px "Segoe UI", sans-serif;
    padding: 5px 8px;
  }
  .panelart {
    height: 74px;
    margin: 8px;
    border-radius: 6px;
    background: linear-gradient(160deg, #d7ecff, #7eafdf);
    position: relative;
  }
  .panelart span {
    position: absolute;
    left: 7px;
    top: 5px;
    color: #8a2a1a;
    font: 700 12px "Segoe UI", sans-serif;
  }
  .panelart::after {
    content: "";
    position: absolute;
    left: 38px;
    top: 16px;
    width: 34px;
    height: 34px;
    border-radius: 50%;
    border: 3px solid #c23b2e;
  }
  .molart { display: block; width: 100%; height: 78px; }
  .cropbox {
    position: absolute;
    right: 16px;
    top: 14px;
    width: 74px;
    height: 48px;
    border: 2px dashed #1d4e9e;
    background: rgba(70, 130, 230, .16);
    border-radius: 4px;
  }
  .cropart {
    background:
      radial-gradient(circle at 60% 55%, transparent 16px, #c23b2e 17px, #c23b2e 20px, transparent 21px),
      linear-gradient(160deg, #d7ecff, #7eafdf);
  }
  .sheet {
    background: var(--paper);
    border-radius: 18px;
    padding: 28px 32px 24px;
    box-shadow: var(--shadow);
  }
  .sheet h2 { margin: 0 0 6px; font-size: 26px; font-weight: 600; }
  .sheet .aside { margin: 0 0 18px; color: var(--muted); font-size: 16px; line-height: 1.45; max-width: 40rem; }
  form { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
  input[type="file"] { font-family: "Segoe UI", sans-serif; font-size: 14px; }
  button {
    font-family: "Segoe UI", sans-serif;
    font-size: 15px;
    background: var(--green);
    color: white;
    border: 0;
    border-radius: 8px;
    padding: 10px 16px;
    cursor: pointer;
  }
  button:disabled { opacity: 0.6; cursor: wait; }
  button.ghost { background: transparent; color: var(--green); border: 1px solid var(--green); }
  .status { margin: 8px 0 0; min-height: 1.4em; font-family: "Segoe UI", sans-serif; font-size: 14px; }
  .scan { margin-top: 18px; }
  .scan-track { height: 10px; background: #e4ddd0; border-radius: 99px; overflow: hidden; }
  .scan-fill { height: 100%; width: 0%; background: #0c6b52; border-radius: 99px; transition: width 180ms ease-out; }
  .status.ok { color: var(--green); }
  .status.bad { color: #8a2a1a; }
  .facts { display: flex; flex-wrap: wrap; gap: 8px; margin: 18px 0 0; padding: 0; list-style: none; }
  .facts li {
    font-family: "Segoe UI", sans-serif;
    font-size: 13px;
    color: #1d3b32;
    background: var(--green-soft);
    border-radius: 999px;
    padding: 6px 12px;
  }
  @media (max-width: 860px) {
    .showcase { grid-template-columns: 1fr; }
    .sheet { padding: 22px 18px 18px; }
  }
  body.reading { overflow: hidden; }
  #reader[hidden], #home[hidden] { display: none; }
  #reader {
    height: 100vh;
    display: flex;
    flex-direction: column;
    background: #e6dfd2;
  }
  .bar {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 16px;
    background: #f7f3ea;
    border-bottom: 1px solid var(--rule);
    font-family: "Segoe UI", sans-serif;
  }
  .bar strong { font-family: Georgia, serif; font-size: 18px; font-weight: 600; }
  .bar .spacer { flex: 1; }
  .bar button, .card button, .tools button {
    font-family: "Segoe UI", sans-serif;
    font-size: 13px;
    background: white;
    color: #14382c;
    border: 1px solid #0c6b52;
    border-radius: 7px;
    padding: 6px 10px;
    cursor: pointer;
  }
  #desk { flex: 1; overflow: auto; padding: 28px 0 80px; }
  .page {
    position: relative;
    margin: 0 auto 18px;
    background: white;
    box-shadow: 0 10px 30px rgba(40, 30, 10, .16);
  }
  .marks { position: absolute; left: 0; top: 0; }
  .mark {
    position: absolute;
    border: 0;
    padding: 0;
    cursor: pointer;
    border-radius: 2px;
  }
  .mark.figure { background: rgba(255, 184, 5, .55); box-shadow: inset 0 -2px 0 #b86100; }
  .mark.molecule { background: rgba(64, 199, 107, .5); box-shadow: inset 0 -2px 0 #0c6b3a; }
  .mark.clip { background: rgba(70, 130, 230, .48); box-shadow: inset 0 -2px 0 #1d4e9e; }
  .crop-layer { position: absolute; inset: 0; z-index: 5; cursor: crosshair; touch-action: none; }
  .crop-box { position: absolute; border: 2px solid #1d4e9e; background: rgba(70, 130, 230, .22); }
  #crop-hint {
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 0;
    padding: 8px 16px;
    background: #e7eef8;
    color: #163a6b;
    font-family: "Segoe UI", sans-serif;
    font-size: 14px;
  }
  #crop-hint[hidden] { display: none; }
  #crop-preview { height: 36px; border-radius: 4px; border: 1px solid #1d4e9e; background: white; }
  .bar button[aria-pressed="true"] { background: #1d4e9e; color: white; border-color: #1d4e9e; }
  .card {
    position: fixed;
    z-index: 40;
    display: flex;
    flex-direction: column;
    background: white;
    border: 1px solid #0c6b52;
    border-radius: 12px;
    box-shadow: 0 18px 50px rgba(20, 16, 8, .28);
    overflow: hidden;
    touch-action: none;
    user-select: none;
  }
  .card header {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 10px;
    background: #e7f2ec;
    cursor: grab;
  }
  .card header strong { flex: 1; font: 600 14px "Segoe UI", sans-serif; }
  .drag {
    font: 600 11px "Segoe UI", sans-serif;
    letter-spacing: .06em;
    text-transform: uppercase;
    color: #0c6b52;
  }
  .tools { display: flex; gap: 6px; padding: 6px 8px; background: #f6f8f6; }
  .stage { flex: 1; position: relative; overflow: hidden; background: white; cursor: pointer; }
  .stage img {
    position: absolute;
    left: 50%;
    top: 50%;
    max-width: 100%;
    max-height: 100%;
    transform: translate(-50%, -50%);
    pointer-events: none;
  }
  .card button.grip {
    position: absolute;
    right: 0;
    bottom: 0;
    width: 22px;
    height: 22px;
    padding: 0;
    border: 0;
    border-radius: 0;
    background: linear-gradient(135deg, transparent 50%, #0c6b52 50%);
    cursor: nwse-resize;
  }
  .card button[data-act="close"] { border-color: #8a2a1a; color: #8a2a1a; background: #fbf3f0; }
</style>
</head>
<body>
<main id="home">
  <header class="mast">
    <p class="markline"><span class="swatch fig">Figure 1a</span><span class="swatch mol">TEMPO</span><span class="swatch clip">your crop</span></p>
    <h1>Paper pictures</h1>
    <p class="lead">Click a marked word. Its picture opens on the sentence, and the paper does not jump.</p>
  </header>
  <section class="showcase" aria-label="What the reader does">
    <article class="scene">
      <div class="stagelet" aria-hidden="true">
        <span class="ruleline" style="left:14px;top:18px;width:42%"></span>
        <span class="ruleline" style="left:14px;top:36px;width:28%"></span>
        <span class="chip fig" style="left:14px;top:58px">Fig. 1a</span>
        <span class="ruleline" style="left:14px;top:148px;width:48%"></span>
        <div class="floatcard">
          <div class="floathead">Panel a</div>
          <div class="panelart"><span>a</span></div>
        </div>
      </div>
      <h2>The panel you named</h2>
      <p>A figure mention opens that figure. A panel letter opens only that panel, with the caption when you ask for it.</p>
    </article>
    <article class="scene">
      <div class="stagelet" aria-hidden="true">
        <span class="ruleline" style="left:14px;top:18px;width:46%"></span>
        <span class="chip mol" style="left:14px;top:42px">TEMPO</span>
        <span class="ruleline" style="left:14px;top:148px;width:36%"></span>
        <div class="floatcard">
          <div class="floathead">TEMPO</div>
          <svg class="molart" viewBox="0 0 120 78" fill="none">
            <path d="M38 22 L60 12 L82 22 L82 44 L60 54 L38 44 Z" stroke="#14382c" stroke-width="1.7"/>
            <path d="M60 12 V4 M82 22 L96 14 M38 44 L24 52 M60 54 V66" stroke="#14382c" stroke-width="1.7" stroke-linecap="round"/>
            <circle cx="60" cy="4" r="2.2" fill="#14382c"/>
            <text x="54" y="36" fill="#14382c" font-size="11" font-family="Segoe UI, sans-serif">N</text>
            <text x="86" y="12" fill="#c23b2e" font-size="11" font-family="Segoe UI, sans-serif">O</text>
          </svg>
        </div>
      </div>
      <h2>The structure, from the name</h2>
      <p>Recognized names and formulas are drawn beside the sentence. The same name opens the same picture wherever it appears.</p>
    </article>
    <article class="scene">
      <div class="stagelet" aria-hidden="true">
        <span class="ruleline" style="left:14px;top:18px;width:34%"></span>
        <span class="chip clip" style="left:14px;top:44px">ligand</span>
        <span class="chip clip" style="left:14px;top:72px">ligand</span>
        <span class="cropbox"></span>
        <div class="floatcard">
          <div class="floathead">Click to close</div>
          <div class="panelart cropart"></div>
        </div>
      </div>
      <h2>A crop that follows a word</h2>
      <p>Drag a rectangle on the page, then click a word. Every copy of that word opens it. One click on the picture closes it.</p>
    </article>
  </section>
  <section class="sheet">
    <h2>Open a paper</h2>
    <p class="aside">The scan marks figures and chemical names, then the paper opens here. Drag a picture to move it. Enlarge and zoom change how much you see.</p>
    <form id="upload">
      <input type="file" name="pdf" accept="application/pdf,.pdf" required>
      <button type="submit">Open paper</button>
      <button type="button" class="ghost" id="sample">Try the sample</button>
    </form>
    <div class="scan" id="scan" hidden>
      <div class="scan-track" id="scan-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" aria-label="Paper scan">
        <div class="scan-fill" id="scan-fill"></div>
      </div>
    </div>
    <p class="status" id="status"></p>
    <ul class="facts">
      <li>The picture stays put while you scroll</li>
      <li>One click on the picture closes it</li>
      <li>Download PDF also opens in Adobe Acrobat</li>
    </ul>
  </section>
</main>
<div id="reader" hidden>
  <div class="bar">
    <strong id="reader-title">Paper</strong>
    <span id="reader-count"></span>
    <span class="spacer"></span>
    <button type="button" id="paper-out">Smaller text</button>
    <button type="button" id="paper-in">Larger text</button>
    <button type="button" id="crop">Crop</button>
    <button type="button" id="download">Download PDF</button>
    <button type="button" id="back">New paper</button>
  </div>
  <p id="crop-hint" hidden><img id="crop-preview" alt="" hidden><span id="crop-hint-text"></span></p>
  <div id="desk"></div>
</div>
<script src="/vendor/pdf.min.js"></script>
<script>
  if (window.pdfjsLib) {
    pdfjsLib.GlobalWorkerOptions.workerSrc = "/vendor/pdf.worker.min.js";
  }
</script>
<script src="/reader.js"></script>
</body>
</html>
"""


def _download_name(filename: str) -> str:
    stem = Path(filename).stem.encode("ascii", "ignore").decode().strip() or "paper"
    return f"{stem}_figures.pdf"


def _meta(filename: str, summary: dict, details: dict, job_id: str) -> dict:
    return {
        "filename": filename,
        "job": job_id,
        "figures": summary["figures"],
        "references": summary["references"],
        "molecules": summary["molecules"],
        "pages": details["pages"],
        "popups": details["popups"],
    }


def _job_snapshot(job: dict) -> dict:
    if job.get("error"):
        return {"done": True, "error": job["error"]}
    if job.get("result") is not None:
        done = dict(job["result"])
        done["done"] = True
        done["fraction"] = 1
        done["label"] = "Opening the paper"
        return done
    return {
        "done": False,
        "label": job.get("label") or "Reading the PDF",
        "fraction": job.get("fraction") or 0,
    }


def _start_job(filename: str, source: bytes) -> str:
    job_id = uuid.uuid4().hex
    job: dict = {
        "label": "Opening the PDF",
        "fraction": 0.02,
        "error": None,
        "result": None,
        "source": source,
        "images": [],
        "popups": [],
        "download": None,
        "download_error": None,
        "embed_lock": threading.Lock(),
        "filename": filename,
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job
        overflow = [key for key in _JOBS if key != job_id]
        for old in overflow[:-5]:
            _JOBS.pop(old, None)

    def run() -> None:
        try:
            def progress(label: str, fraction: float) -> None:
                job["label"] = label
                job["fraction"] = float(fraction)

            summary, details, popups = figures.prepare_pdf(source, progress)
            job["images"] = details["images"]
            job["popups"] = popups
            job["result"] = _meta(filename, summary, details, job_id)
            job["fraction"] = 1
            job["label"] = "Opening the paper"
        except ValueError as exc:
            job["error"] = str(exc)
        except Exception as exc:
            print(f"annotate failed: {exc}", flush=True)
            job["error"] = "Could not read this file as a PDF."

    threading.Thread(target=run, daemon=True).start()
    return job_id


def _job_id_ok(job_id: str) -> bool:
    return len(job_id) == 32 and all(char in "0123456789abcdef" for char in job_id)


def _ensure_download(job: dict) -> bytes | None:
    with job["embed_lock"]:
        if job.get("download") is None and not job.get("download_error"):
            try:
                job["download"] = figures.embed_pdf(job["source"], job["popups"])
            except Exception as exc:
                print(f"download failed: {exc}", flush=True)
                job["download_error"] = "The PDF could not be prepared."
        return job.get("download")


def _read_upload(content_type: str, body: bytes) -> tuple[str, bytes]:
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii")
    message = email.message_from_bytes(header + body, policy=policy.default)
    if not message.is_multipart():
        raise ValueError("Choose a PDF file.")
    for part in message.iter_parts():
        filename = part.get_filename() or ""
        field = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        if field == "pdf" or filename.lower().endswith(".pdf"):
            return filename or "paper.pdf", payload
    raise ValueError("Choose a PDF file.")


class AppHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _send(self, code: int, content_type: str, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send(200, "application/json", b'{"ok":true}', {"Cache-Control": "no-store"})
            return
        if path == "/":
            self._send(
                200,
                "text/html; charset=utf-8",
                PAGE.encode("utf-8"),
                {"Cache-Control": "no-store"},
            )
            return
        if path == "/reader.js":
            script = Path(__file__).with_name("reader.js").read_bytes()
            self._send(200, "text/javascript; charset=utf-8", script, {"Cache-Control": "no-store"})
            return
        if path.startswith("/vendor/"):
            vendor = Path(__file__).with_name("vendor") / Path(path).name
            if vendor.is_file():
                kind = "text/javascript; charset=utf-8"
                self._send(200, kind, vendor.read_bytes(), {"Cache-Control": "no-store"})
                return
        if path == "/sample":
            try:
                source = figures._sample_pdf()
            except Exception as exc:
                print(f"sample failed: {exc}", flush=True)
                self._send(400, "text/plain; charset=utf-8", b"The sample did not open.")
                return
            self._send(
                200,
                "application/json",
                json.dumps({"job": _start_job("sample.pdf", source)}).encode("utf-8"),
                {"Cache-Control": "no-store"},
            )
            return
        if path.startswith("/progress/"):
            job_id = path.removeprefix("/progress/")
            if len(job_id) != 32 or any(char not in "0123456789abcdef" for char in job_id):
                self._send(404, "text/plain; charset=utf-8", b"Not found")
                return
            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
            if job is None:
                self._send(404, "text/plain; charset=utf-8", b"The scan is no longer running.")
                return
            snap = _job_snapshot(job)
            self._send(
                200,
                "application/json",
                json.dumps(snap).encode("utf-8"),
                {"Cache-Control": "no-store"},
            )
            return
        if path.startswith("/job/"):
            parts = [part for part in path.split("/") if part]
            if len(parts) < 3 or parts[0] != "job" or not _job_id_ok(parts[1]):
                self._send(404, "text/plain; charset=utf-8", b"Not found")
                return
            with _JOBS_LOCK:
                job = _JOBS.get(parts[1])
            if job is None or job.get("result") is None:
                self._send(404, "text/plain; charset=utf-8", b"The paper is not ready.")
                return
            kind = parts[2]
            if kind == "pdf" and len(parts) == 3:
                self._send(200, "application/pdf", job["source"], {"Cache-Control": "private, max-age=3600"})
                return
            if kind == "download" and len(parts) == 3:
                pdf = _ensure_download(job)
                if not pdf:
                    message = job.get("download_error") or "The PDF could not be prepared."
                    self._send(400, "text/plain; charset=utf-8", message.encode("utf-8"))
                    return
                name = job.get("filename") or "paper_figures.pdf"
                self._send(
                    200,
                    "application/pdf",
                    pdf,
                    {
                        "Cache-Control": "no-store",
                        "Content-Disposition": f'attachment; filename="{name}"',
                    },
                )
                return
            if kind == "image" and len(parts) == 4 and parts[3].isdigit():
                images = job.get("images") or []
                index = int(parts[3])
                if index >= len(images):
                    self._send(404, "text/plain; charset=utf-8", b"Not found")
                    return
                blob = images[index]
                mime = "image/jpeg" if blob[:2] == b"\xff\xd8" else "image/png"
                self._send(200, mime, blob, {"Cache-Control": "private, max-age=3600"})
                return
            self._send(404, "text/plain; charset=utf-8", b"Not found")
            return
        self._send(404, "text/plain; charset=utf-8", b"Not found")

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/annotate":
            self._send(404, "text/plain; charset=utf-8", b"Not found")
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BYTES:
            self._send(413, "text/plain; charset=utf-8", b"The PDF must be under 80 MB.")
            return
        body = self.rfile.read(length)
        try:
            filename, source = _read_upload(self.headers.get("Content-Type", ""), body)
        except ValueError as exc:
            self._send(400, "text/plain; charset=utf-8", str(exc).encode("utf-8"))
            return
        self._send(
            200,
            "application/json",
            json.dumps({"job": _start_job(_download_name(filename), source)}).encode("utf-8"),
            {"Cache-Control": "no-store"},
        )


def annotate_path(source: Path, dest: Path) -> dict:
    output, summary, _details = figures.annotate_pdf(source.read_bytes())
    dest.write_bytes(output)
    return summary


def serve(port: int) -> None:
    env_port = os.environ.get("PORT")
    if env_port:
        host = "0.0.0.0"
        chosen = int(env_port)
        server = ThreadingHTTPServer((host, chosen), AppHandler)
    else:
        host = "127.0.0.1"
        server = None
        chosen = port
        for candidate in range(port, port + 20):
            try:
                server = ThreadingHTTPServer((host, candidate), AppHandler)
                chosen = candidate
                break
            except OSError:
                continue
        if server is None:
            raise SystemExit(f"No free port from {port} to {port + 19}")
    print(f"Paper pictures  http://{host}:{chosen}", flush=True)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Highlight figure mentions and chemical names in a PDF.")
    parser.add_argument("pdf", nargs="?", type=Path, help="PDF to annotate. Omit this to open the upload page.")
    parser.add_argument("-o", "--output", type=Path, help="Where to write the annotated PDF.")
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    if args.pdf is None:
        serve(args.port)
        return
    if not args.pdf.is_file():
        raise SystemExit(f"PDF not found: {args.pdf}")
    dest = args.output or args.pdf.with_name(args.pdf.stem + "_figures.pdf")
    summary = annotate_path(args.pdf, dest)
    print(
        f"Wrote {dest}  figures={summary['figures']} references={summary['references']} molecules={summary['molecules']}",
        flush=True,
    )


if __name__ == "__main__":
    main()
