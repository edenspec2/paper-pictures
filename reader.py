"""Scheme reader: a local page that draws RDKit structures onto a chemistry paper.

Run:
    python reader.py

The papers directory defaults to the steric-paper extracts used to build this.
Pass --papers to point it somewhere else. Only .txt extracts are read.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import chemistry

DEFAULT_PAPERS = Path(
    r"C:\Users\edens\Desktop\automation_new\descripytor_paper_v4_submission\steric_papers"
)

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scheme reader</title>
<style>
  :root {
    --paper: #f7f3ea;
    --ink: #1d1914;
    --rule: #d9d0c1;
    --green: #0c6b52;
    --green-soft: #e5f3ee;
    --amber: #8a5a12;
    --dock: #fbf9f4;
    --shadow: 0 12px 40px rgba(40, 30, 10, .16);
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; }
  body {
    background: #e6dfd2;
    color: var(--ink);
    font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  }
  button, select, input { font: inherit; }
  .app {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 340px;
    min-height: 100vh;
  }
  main { padding: 28px 28px 80px; }
  article.sheet {
    max-width: 760px;
    margin: 0 auto;
    background: var(--paper);
    border-radius: 14px;
    padding: 36px 42px 64px;
    box-shadow: var(--shadow);
  }
  header.top h1 {
    font-size: 28px;
    line-height: 1.25;
    font-weight: 600;
    margin: 0 0 8px;
  }
  .stats { color: #5e564b; font-family: "Segoe UI", sans-serif; font-size: 13px; margin: 0 0 18px; }
  .scheme {
    background: white;
    border: 1px solid var(--rule);
    border-radius: 12px;
    padding: 14px 16px 10px;
    margin: 0 0 22px;
  }
  .scheme h2 { font-size: 15px; margin: 0 0 4px; font-family: "Segoe UI", sans-serif; }
  .scheme p { margin: 0 0 10px; color: #4d463c; font-size: 15px; }
  .scheme .rxn { overflow-x: auto; }
  .scheme .rxn svg { min-width: 640px; width: 100%; height: auto; }
  .scheme-row svg { max-width: none; height: 130px; width: auto; background: white; }
  .scheme-row { display: flex; gap: 14px; overflow-x: auto; padding-bottom: 6px; }
  .scheme-row figure { margin: 0; min-width: 140px; text-align: center; }
  .scheme-row figcaption { font-family: "Segoe UI", sans-serif; font-size: 12px; color: #4d463c; }
  #body p { font-size: 18.5px; line-height: 1.55; margin: 0 0 14px; }
  mark.chem {
    background: transparent;
    color: inherit;
    border-bottom: 2px solid var(--green);
    cursor: pointer;
    padding: 0;
  }
  mark.chem.group { border-bottom-style: dotted; border-bottom-color: var(--amber); }
  mark.chem.solvent { border-bottom-style: dotted; border-bottom-color: #8d8d8d; }
  mark.chem:hover, mark.chem.pinned { background: var(--green-soft); }
  sup.cite { font-family: "Segoe UI", sans-serif; font-size: 11px; margin-left: 1px; }
  sup.cite a { color: var(--green); text-decoration: none; }
  .inline-slot svg {
    height: 78px; width: auto; vertical-align: middle;
    margin: 0 4px; background: white; border-radius: 8px;
    box-shadow: 0 1px 3px rgba(0,0,0,.12);
  }
  body.hide-inline .inline-slot { display: none; }
  body:not(.show-groups) mark.group { border: 0; pointer-events: none; background: none; }
  body:not(.show-solvents) mark.solvent { border: 0; pointer-events: none; background: none; }
  body:not(.show-groups) .g-card.group { display: none; }
  body:not(.show-solvents) .g-card.solvent { display: none; }
  h3.refs-title { font-size: 18px; margin: 28px 0 8px; }
  ol.refs { list-style: none; padding: 0; margin: 0; }
  li.ref {
    display: grid;
    grid-template-columns: 36px 1fr;
    gap: 8px;
    padding: 10px 0;
    border-top: 1px solid var(--rule);
    font-size: 14.5px;
    line-height: 1.45;
  }
  .ref-num { font-family: "Segoe UI", sans-serif; color: var(--green); font-weight: 650; }
  .ref-thumbs { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }
  .ref-thumb svg { height: 68px; width: auto; background: white; border-radius: 6px; }
  .dock {
    position: sticky; top: 0; height: 100vh; overflow: auto;
    background: var(--dock);
    border-left: 1px solid var(--rule);
    padding: 16px 14px 28px;
    font-family: "Segoe UI", sans-serif;
  }
  .brand { font-family: Georgia, serif; font-size: 20px; margin: 0 0 2px; }
  .brand-note { color: #5e564b; font-size: 12.5px; line-height: 1.4; margin: 0 0 12px; }
  label.pick { display: block; font-size: 11px; letter-spacing: .04em; text-transform: uppercase; color: #6b645b; margin: 10px 0 4px; }
  select, input[type="search"] {
    width: 100%; padding: 8px 10px; border-radius: 8px;
    border: 1px solid var(--rule); background: white; color: var(--ink);
  }
  .toggles { display: flex; flex-direction: column; gap: 6px; margin: 10px 0 14px; font-size: 13px; }
  .toggles label { display: flex; gap: 8px; align-items: center; }
  #gallery { display: flex; flex-direction: column; gap: 8px; }
  .g-card {
    display: grid; grid-template-columns: 96px 1fr; gap: 8px;
    background: white; border: 1px solid var(--rule); border-radius: 10px;
    padding: 6px; cursor: pointer; text-align: left;
  }
  .g-card svg { width: 96px; height: 72px; }
  .g-card strong { display: block; font-size: 13px; line-height: 1.3; }
  .g-card span { color: #6b645b; font-size: 11px; }
  .g-card:hover { border-color: var(--green); }
  #card {
    position: fixed; z-index: 20; width: 340px;
    background: white; border-radius: 14px; box-shadow: var(--shadow);
    padding: 10px 12px 12px; border: 1px solid var(--rule);
  }
  #card[hidden] { display: none; }
  #card svg { width: 100%; height: auto; }
  #card h3 { font-family: "Segoe UI", sans-serif; font-size: 15px; margin: 4px 0; }
  #card .alias, #card .note { font-family: "Segoe UI", sans-serif; font-size: 12.5px; color: #4d463c; margin: 0 0 6px; }
  #card .note { color: var(--amber); }
  #card code { display: block; font-size: 11px; color: #5c564e; word-break: break-all; }
  #status { font-size: 13px; color: #5e564b; }
  @media (max-width: 980px) {
    .app { grid-template-columns: 1fr; }
    .dock { position: relative; height: auto; border-left: 0; border-top: 1px solid var(--rule); }
    article.sheet { padding: 22px 16px 40px; }
  }
</style>
</head>
<body class="hide-inline show-groups">
<div class="app">
  <main>
    <article class="sheet" id="sheet">
      <p id="status">Loading papers…</p>
    </article>
  </main>
  <aside class="dock">
    <p class="brand">Scheme reader</p>
    <p class="brand-note">RDKit drawings sit on IUPAC names, phosphine formulas, and ligand abbreviations. Hover a marked name. Citations jump to the reference, which carries the structures named in it.</p>
    <label class="pick" for="paper">Paper</label>
    <select id="paper"></select>
    <div class="toggles">
      <label><input type="checkbox" id="opt-inline"> Picture at the first mention</label>
      <label><input type="checkbox" id="opt-groups" checked> Abbreviations (Me, Ph, Cy…)</label>
      <label><input type="checkbox" id="opt-solvents"> Solvents</label>
    </div>
    <label class="pick" for="filter">Structures in this paper</label>
    <input id="filter" type="search" placeholder="Filter by name">
    <div id="gallery"></div>
  </aside>
</div>
<div id="card" hidden></div>
<script>
const paperSelect = document.getElementById("paper");
const sheet = document.getElementById("sheet");
const gallery = document.getElementById("gallery");
const card = document.getElementById("card");
const filter = document.getElementById("filter");
let doc = null;
let byId = {};
let pinned = false;

function esc(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[ch]));
}

function applyToggles() {
  document.body.classList.toggle("hide-inline", !document.getElementById("opt-inline").checked);
  document.body.classList.toggle("show-groups", document.getElementById("opt-groups").checked);
  document.body.classList.toggle("show-solvents", document.getElementById("opt-solvents").checked);
  paintGallery();
}

async function boot() {
  const response = await fetch("/api/papers");
  const papers = await response.json();
  paperSelect.innerHTML = papers.map((paper) =>
    `<option value="${esc(paper.file)}">${esc(paper.title)}</option>`
  ).join("");
  const preferred = papers.find((paper) => paper.file === "wu_doyle.txt") || papers[0];
  if (preferred) {
    paperSelect.value = preferred.file;
    await openPaper(preferred.file);
  } else {
    sheet.innerHTML = "<p>No paper extracts in the folder.</p>";
  }
}

async function openPaper(file) {
  pinned = false;
  card.hidden = true;
  sheet.innerHTML = "<p id='status'>Drawing structures…</p>";
  const response = await fetch("/api/paper?file=" + encodeURIComponent(file));
  doc = await response.json();
  if (doc.error) {
    sheet.innerHTML = "<p>" + esc(doc.error) + "</p>";
    return;
  }
  byId = {};
  doc.molecules.forEach((mol) => { byId[mol.mid] = mol; });
  const schemes = (doc.schemes || []).map(renderScheme).join("");
  const refs = doc.refs_html
    ? `<h3 class="refs-title">References</h3><ol class="refs">${doc.refs_html}</ol>`
    : "";
  const stats = doc.stats;
  sheet.innerHTML = `
    <header class="top">
      <h1>${esc(doc.title)}</h1>
      <p class="stats">${stats.mentions} marked names · ${stats.unique} structures · ${stats.references} references</p>
    </header>
    ${schemes}
    <div id="body">${doc.body_html}</div>
    ${refs}`;
  fillSlots();
  paintGallery();
}

function renderScheme(scheme) {
  let picture = scheme.svg ? `<div class="rxn">${scheme.svg}</div>` : "";
  if (!picture) {
    const cells = (scheme.molecules || []).map((item) => {
      const mol = byId[item.smiles];
      const svg = mol ? mol.svg : "";
      return `<figure>${svg}<figcaption>${esc(item.name)}</figcaption></figure>`;
    }).join("");
    picture = `<div class="scheme-row">${cells}</div>`;
  }
  return `<section class="scheme"><h2>${esc(scheme.title)}</h2><p>${esc(scheme.caption)}</p>${picture}</section>`;
}

function fillSlots() {
  document.querySelectorAll(".inline-slot, .ref-thumb").forEach((slot) => {
    const mol = byId[slot.dataset.mid];
    if (mol) slot.innerHTML = mol.svg;
  });
}

function paintGallery() {
  if (!doc) return;
  const query = filter.value.trim().toLowerCase();
  const showGroups = document.getElementById("opt-groups").checked;
  const showSolvents = document.getElementById("opt-solvents").checked;
  gallery.innerHTML = "";
  doc.molecules.forEach((mol) => {
    if (mol.kind === "group" && !showGroups) return;
    if (mol.kind === "solvent" && !showSolvents) return;
    const hay = (mol.title + " " + mol.aliases.join(" ")).toLowerCase();
    if (query && !hay.includes(query)) return;
    const cardEl = document.createElement("button");
    cardEl.className = "g-card " + mol.kind;
    cardEl.innerHTML = `${mol.svg}<div><strong>${esc(mol.title)}</strong><span>${mol.count ? mol.count + " in the text" : "in the scheme"} · ${esc(mol.kind)}</span></div>`;
    cardEl.addEventListener("click", () => {
      const mark = sheet.querySelector(`mark.chem[data-mid="${cssEscape(mol.mid)}"]`);
      if (!mark) return;
      mark.scrollIntoView({ block: "center" });
      pinned = true;
      card.dataset.mid = mol.mid;
      showCard(mark);
    });
    gallery.appendChild(cardEl);
  });
}

function cssEscape(value) {
  if (window.CSS && CSS.escape) return CSS.escape(value);
  return value.replace(/["\\]/g, "\\$&");
}

function showCard(mark) {
  const mol = byId[mark.dataset.mid];
  if (!mol) return;
  document.querySelectorAll("mark.pinned").forEach((el) => el.classList.remove("pinned"));
  mark.classList.add("pinned");
  const alias = mol.aliases.filter((name) => name.toLowerCase() !== mol.title.toLowerCase()).slice(0, 4);
  card.innerHTML = `
    ${mol.svg}
    <h3>${esc(mark.textContent)}</h3>
    ${alias.length ? `<p class="alias">Also written ${esc(alias.join(", "))}</p>` : ""}
    ${mol.note ? `<p class="note">${esc(mol.note)}</p>` : ""}
    <code>${esc(mol.smiles)}</code>`;
  card.hidden = false;
  const rect = mark.getBoundingClientRect();
  const width = 340;
  let left = Math.min(rect.left, window.innerWidth - width - 12);
  left = Math.max(8, left);
  let top = rect.bottom + 8;
  card.style.left = left + "px";
  card.style.top = top + "px";
  const box = card.getBoundingClientRect();
  if (box.bottom > window.innerHeight - 8) {
    top = rect.top - box.height - 8;
  }
  card.style.top = Math.max(8, Math.min(top, window.innerHeight - box.height - 8)) + "px";
}

function hideCard() {
  card.hidden = true;
  document.querySelectorAll("mark.pinned").forEach((el) => el.classList.remove("pinned"));
  pinned = false;
}

sheet.addEventListener("mouseover", (event) => {
  const mark = event.target.closest("mark.chem");
  if (!mark || pinned) return;
  showCard(mark);
});
sheet.addEventListener("mouseout", (event) => {
  if (pinned) return;
  const mark = event.target.closest("mark.chem");
  if (!mark) return;
  const next = event.relatedTarget;
  if (next && (mark.contains(next) || card.contains(next))) return;
  hideCard();
});
sheet.addEventListener("click", (event) => {
  const mark = event.target.closest("mark.chem");
  if (!mark) return;
  if (pinned && card.dataset.mid === mark.dataset.mid) {
    hideCard();
    return;
  }
  pinned = true;
  card.dataset.mid = mark.dataset.mid;
  showCard(mark);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") hideCard();
});
document.addEventListener("click", (event) => {
  if (pinned && !card.contains(event.target) && !event.target.closest("mark.chem") && !event.target.closest(".g-card")) {
    hideCard();
  }
});
paperSelect.addEventListener("change", () => openPaper(paperSelect.value));
filter.addEventListener("input", paintGallery);
document.getElementById("opt-inline").addEventListener("change", applyToggles);
document.getElementById("opt-groups").addEventListener("change", applyToggles);
document.getElementById("opt-solvents").addEventListener("change", applyToggles);
boot();
</script>
</body>
</html>
"""


def papers_in(folder: Path) -> list[Path]:
    return sorted(path for path in folder.glob("*.txt") if path.is_file())


class ReaderHandler(BaseHTTPRequestHandler):
    folder: Path = DEFAULT_PAPERS
    cache: dict[tuple[str, float], dict] = {}

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
            return
        if parsed.path == "/api/papers":
            payload = [
                {
                    "file": path.name,
                    "title": chemistry.PAPER_TITLES.get(path.name, path.stem),
                }
                for path in papers_in(self.folder)
            ]
            self._send(200, "application/json", json.dumps(payload).encode("utf-8"))
            return
        if parsed.path == "/api/paper":
            name = parse_qs(parsed.query).get("file", [""])[0]
            safe = Path(name).name
            path = self.folder / safe
            if safe != name or path.suffix.lower() != ".txt" or not path.is_file():
                self._send(404, "application/json", b'{"error":"paper not found"}')
                return
            document = self._document(path)
            self._send(200, "application/json", json.dumps(document).encode("utf-8"))
            return
        self._send(404, "text/plain", b"not found")

    def _document(self, path: Path) -> dict:
        key = (str(path), path.stat().st_mtime)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        text = path.read_text(encoding="utf-8", errors="replace")
        document = chemistry.annotate_document(text, path.name)
        for molecule in document["molecules"]:
            molecule["svg"] = chemistry.depict_svg(molecule["smiles"])
        self.cache[key] = document
        return document


def main() -> None:
    parser = argparse.ArgumentParser(description="Read chemistry papers with RDKit structures on the names.")
    parser.add_argument("--papers", type=Path, default=DEFAULT_PAPERS)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    folder = args.papers
    if not folder.is_dir():
        raise SystemExit(f"Paper folder not found: {folder}")
    ReaderHandler.folder = folder
    server = None
    port = args.port
    for candidate in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), ReaderHandler)
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit(f"No free port from {args.port} to {args.port + 19}")
    print(f"Scheme reader  http://127.0.0.1:{port}", flush=True)
    print(f"Papers         {folder}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
