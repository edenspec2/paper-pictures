/* The paper opens here. Drag a picture, enlarge the card, zoom inside it. */
const home = document.getElementById("home");
const reader = document.getElementById("reader");
const desk = document.getElementById("desk");
const status = document.getElementById("status");
const form = document.getElementById("upload");
const title = document.getElementById("reader-title");

let paper = null;
let generation = 0;
let zTop = 40;
let cropOn = false;

const SKIP_WORDS = new Set("the a an of and or to in on for with by as at from is was be this that these those it its are were".split(" "));

const observer = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (entry.isIntersecting) renderPage(entry.target);
  }
}, { root: desk, rootMargin: "600px" });

function downloadPaper() {
  if (!paper?.job) return;
  const button = document.getElementById("download");
  button.disabled = true;
  const previous = button.textContent;
  button.textContent = "Preparing PDF…";
  fetch(`/job/${paper.job}/download`)
    .then((response) => {
      if (!response.ok) throw new Error("download");
      return response.blob();
    })
    .then((blob) => {
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = paper.filename;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    })
    .catch(() => {
      button.textContent = "Download failed";
      setTimeout(() => {
        if (button.textContent === "Download failed") button.textContent = previous;
      }, 1600);
    })
    .finally(() => {
      button.disabled = false;
      if (button.textContent === "Preparing PDF…") button.textContent = previous;
    });
}

function closeCards() {
  document.querySelectorAll(".card").forEach((card) => card.remove());
}

function raise(card) {
  zTop += 1;
  card.style.zIndex = String(zTop);
}

function applyZoom(card) {
  const image = card.querySelector("img");
  image.style.transform = `translate(calc(-50% + ${card._panX}px), calc(-50% + ${card._panY}px)) scale(${card._zoom})`;
}

function track(target, event, onMove) {
  event.preventDefault();
  const pointer = event.pointerId;
  const startX = event.clientX;
  const startY = event.clientY;
  target.setPointerCapture(pointer);
  const move = (next) => onMove(next.clientX - startX, next.clientY - startY);
  const end = () => target.removeEventListener("pointermove", move);
  target.addEventListener("pointermove", move);
  target.addEventListener("pointerup", end, { once: true });
  target.addEventListener("pointercancel", end, { once: true });
}

function resizeCard(card, factor) {
  const width = Math.min(window.innerWidth - 24, Math.max(260, card.offsetWidth * factor));
  const height = Math.min(window.innerHeight - 72, Math.max(200, card.offsetHeight * factor));
  card.style.width = `${width}px`;
  card.style.height = `${height}px`;
}

function openCard(popup, clientX, clientY) {
  const card = document.createElement("article");
  card.className = "card";
  card._zoom = 1;
  card._panX = 0;
  card._panY = 0;
  const width = 380;
  const height = 320;
  card.style.width = `${width}px`;
  card.style.height = `${height}px`;
  card.style.left = `${Math.max(8, Math.min(clientX + 14, window.innerWidth - width - 8))}px`;
  card.style.top = `${Math.max(58, Math.min(clientY + 14, window.innerHeight - height - 8))}px`;
  card.innerHTML = `
    <header>
      <span class="drag">Drag</span>
      <strong></strong>
      <button type="button" data-act="close">Close</button>
    </header>
    <div class="tools">
      <button type="button" data-act="out">Zoom out</button>
      <button type="button" data-act="in">Zoom in</button>
      <button type="button" data-act="shrink">Smaller</button>
      <button type="button" data-act="grow">Enlarge</button>
    </div>
    <div class="stage" title="Click to close. Drag to move."><img alt=""></div>
    <button type="button" class="grip" title="Drag to enlarge"></button>
  `;
  card.querySelector("strong").textContent = popup.label;
  const image = card.querySelector("img");
  image.src = popup.src || `/job/${paper.job}/image/${popup.image}`;
  image.alt = popup.label;
  const tools = card.querySelector(".tools");
  if (popup.expanded != null && popup.expanded !== popup.image) {
    const swap = document.createElement("button");
    swap.type = "button";
    swap.dataset.act = "caption";
    swap.textContent = popup.panel ? "With caption" : "Show caption";
    tools.append(swap);
  }
  document.body.append(card);
  raise(card);

  const header = card.querySelector("header");
  const stage = card.querySelector(".stage");
  const grip = card.querySelector(".grip");

  header.addEventListener("pointerdown", (event) => {
    if (event.target.closest("button")) return;
    raise(card);
    const left = card.offsetLeft;
    const top = card.offsetTop;
    track(header, event, (dx, dy) => {
      card.style.left = `${Math.max(0, Math.min(window.innerWidth - 80, left + dx))}px`;
      card.style.top = `${Math.max(48, Math.min(window.innerHeight - 48, top + dy))}px`;
    });
  });

  let clickCloses = false;
  stage.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    raise(card);
    clickCloses = false;
    const originX = event.clientX;
    const originY = event.clientY;
    const left = card.offsetLeft;
    const top = card.offsetTop;
    const panX = card._panX;
    const panY = card._panY;
    const zoomed = card._zoom > 1.02;
    let moved = false;
    const pointer = event.pointerId;
    stage.setPointerCapture(pointer);
    const move = (next) => {
      const dx = next.clientX - originX;
      const dy = next.clientY - originY;
      if (!moved && Math.hypot(dx, dy) < 5) return;
      moved = true;
      if (zoomed) {
        card._panX = panX + dx;
        card._panY = panY + dy;
        applyZoom(card);
        return;
      }
      card.style.left = `${Math.max(0, Math.min(window.innerWidth - 80, left + dx))}px`;
      card.style.top = `${Math.max(48, Math.min(window.innerHeight - 48, top + dy))}px`;
    };
    const end = () => {
      stage.removeEventListener("pointermove", move);
      clickCloses = !moved;
    };
    stage.addEventListener("pointermove", move);
    stage.addEventListener("pointerup", end, { once: true });
    stage.addEventListener("pointercancel", end, { once: true });
  });
  stage.addEventListener("click", (event) => {
    if (!clickCloses) return;
    event.stopPropagation();
    card.remove();
  });

  grip.addEventListener("pointerdown", (event) => {
    raise(card);
    const width = card.offsetWidth;
    const height = card.offsetHeight;
    track(grip, event, (dx, dy) => {
      card.style.width = `${Math.max(260, width + dx)}px`;
      card.style.height = `${Math.max(200, height + dy)}px`;
    });
  });

  stage.addEventListener("wheel", (event) => {
    event.preventDefault();
    const next = card._zoom * (event.deltaY < 0 ? 1.12 : 1 / 1.12);
    card._zoom = Math.min(8, Math.max(0.4, next));
    if (card._zoom <= 1.02) {
      card._panX = 0;
      card._panY = 0;
    }
    applyZoom(card);
  }, { passive: false });

  stage.addEventListener("dblclick", () => {
    card._zoom = 1;
    card._panX = 0;
    card._panY = 0;
    applyZoom(card);
  });

  card.addEventListener("click", (event) => {
    const action = event.target.closest("button")?.dataset.act;
    if (!action) {
      raise(card);
      return;
    }
    if (action === "close") card.remove();
    if (action === "in") card._zoom = Math.min(8, card._zoom * 1.25);
    if (action === "out") card._zoom = Math.max(0.4, card._zoom / 1.25);
    if (action === "in" || action === "out") {
      if (card._zoom <= 1.02) {
        card._panX = 0;
        card._panY = 0;
      }
      applyZoom(card);
    }
    if (action === "grow") resizeCard(card, 1.22);
    if (action === "shrink") resizeCard(card, 1 / 1.22);
    if (action === "caption") {
      const button = event.target.closest("button");
      card._full = !card._full;
      image.src = `/job/${paper.job}/image/${card._full ? popup.expanded : popup.image}`;
      button.textContent = card._full
        ? (popup.panel ? "This panel" : "Hide caption")
        : (popup.panel ? "With caption" : "Show caption");
      card._zoom = 1;
      card._panX = 0;
      card._panY = 0;
      applyZoom(card);
    }
  });
}

async function renderPage(shell) {
  if (!paper || shell.dataset.token === String(generation)) return;
  const token = generation;
  shell.dataset.token = String(token);
  const pageIndex = Number(shell.dataset.page);
  const pdfPage = await paper._pdf.getPage(pageIndex + 1);
  if (token !== generation) return;
  const [pageWidth] = paper.pages[pageIndex];
  const fitted = Math.min(920, Math.max(320, desk.clientWidth - 48));
  const scale = (fitted / pageWidth) * paper.viewScale;
  const viewport = pdfPage.getViewport({ scale });
  const canvas = shell.querySelector("canvas");
  const ratio = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.floor(viewport.width * ratio);
  canvas.height = Math.floor(viewport.height * ratio);
  canvas.style.width = `${viewport.width}px`;
  canvas.style.height = `${viewport.height}px`;
  shell.style.width = `${viewport.width}px`;
  shell.style.height = `${viewport.height}px`;
  await pdfPage.render({
    canvasContext: canvas.getContext("2d"),
    viewport: pdfPage.getViewport({ scale: scale * ratio }),
  }).promise;
  if (token !== generation) return;
  paintPageOverlay(shell);
}

function paintPageOverlay(shell) {
  const pageIndex = Number(shell.dataset.page);
  const [pageWidth] = paper.pages[pageIndex];
  const scale = shell.clientWidth / pageWidth;
  const marks = shell.querySelector(".marks");
  marks.innerHTML = "";
  marks.style.width = `${shell.clientWidth}px`;
  marks.style.height = `${shell.clientHeight}px`;
  for (const popup of paper.popups) {
    if (popup.page !== pageIndex) continue;
    const [x0, y0, x1, y1] = popup.anchor;
    const kind = popup.kind === "molecule" ? "molecule" : popup.kind === "clip" ? "clip" : "figure";
    const mark = document.createElement("button");
    mark.type = "button";
    mark.className = `mark ${kind}`;
    mark.style.left = `${x0 * scale}px`;
    mark.style.top = `${y0 * scale}px`;
    mark.style.width = `${Math.max(8, (x1 - x0) * scale)}px`;
    mark.style.height = `${Math.max(8, (y1 - y0) * scale)}px`;
    mark.title = `Open ${popup.label}`;
    mark.addEventListener("click", (event) => openCard(popup, event.clientX, event.clientY));
    marks.append(mark);
  }
  let layer = shell.querySelector(".crop-layer");
  if (!cropOn) {
    layer?.remove();
    return;
  }
  if (!layer) {
    layer = document.createElement("div");
    layer.className = "crop-layer";
    layer.setAttribute("role", "application");
    layer.setAttribute("aria-label", "Drag a rectangle, then click a word");
    shell.append(layer);
    bindCrop(layer, shell);
  }
}

function refreshMarks() {
  desk.querySelectorAll(".page").forEach((shell) => {
    if (shell.querySelector("canvas")?.width) paintPageOverlay(shell);
  });
}

function wordToken(text) {
  return text.replace(/^[\s([{"'`]+|[.,;:)\]}"'`]+$/g, "");
}

function setHint(text) {
  const hint = document.getElementById("crop-hint");
  document.getElementById("crop-hint-text").textContent = text;
  hint.hidden = !text;
}

function updateCount() {
  const clips = new Set(paper.popups.filter((popup) => popup.kind === "clip").map((popup) => popup.token));
  const figures = `${paper.references} figure mention${paper.references === 1 ? "" : "s"}`;
  const names = `${paper.molecules} chemical name${paper.molecules === 1 ? "" : "s"}`;
  const extra = clips.size ? `, ${clips.size} cropped picture${clips.size === 1 ? "" : "s"}` : "";
  document.getElementById("reader-count").textContent = `${figures}, ${names}${extra}`;
}

async function ensureText() {
  if (paper.words) return;
  const pages = [];
  for (let index = 0; index < paper._pdf.numPages; index += 1) {
    const pdfPage = await paper._pdf.getPage(index + 1);
    const content = await pdfPage.getTextContent();
    const pageHeight = paper.pages[index][1];
    const words = [];
    for (const item of content.items) {
      if (!item.str || !item.width) continue;
      const tx = item.transform;
      const height = Math.abs(tx[3]) || item.height || 10;
      const top = pageHeight - (tx[5] + height * 0.9);
      const bottom = pageHeight - (tx[5] - height * 0.25);
      const pattern = /\S+/g;
      let match;
      while ((match = pattern.exec(item.str))) {
        const token = wordToken(match[0]);
        if (!token) continue;
        const start = tx[4] + (match.index / item.str.length) * item.width;
        const end = tx[4] + ((match.index + match[0].length) / item.str.length) * item.width;
        words.push({ token, anchor: [start, top, end, bottom] });
      }
    }
    pages.push(words);
  }
  paper.words = pages;
}

function pointOnPage(shell, clientX, clientY) {
  const bounds = shell.getBoundingClientRect();
  const [pageWidth, pageHeight] = paper.pages[Number(shell.dataset.page)];
  return [
    ((clientX - bounds.left) / bounds.width) * pageWidth,
    ((clientY - bounds.top) / bounds.height) * pageHeight,
  ];
}

function wordAt(pageIndex, x, y) {
  let best = null;
  let bestDist = Infinity;
  for (const word of paper.words[pageIndex] || []) {
    const [x0, y0, x1, y1] = word.anchor;
    if (x < x0 - 3 || x > x1 + 3 || y < y0 - 3 || y > y1 + 3) continue;
    const dx = x - (x0 + x1) / 2;
    const dy = y - (y0 + y1) / 2;
    const dist = dx * dx + dy * dy;
    if (dist < bestDist) {
      best = word;
      bestDist = dist;
    }
  }
  return best;
}

function attachWord(token, src) {
  const key = token.toLowerCase();
  paper.popups = paper.popups.filter((popup) => popup.token !== key);
  let count = 0;
  paper.words.forEach((words, page) => {
    for (const word of words) {
      if (word.token.toLowerCase() !== key) continue;
      paper.popups.push({
        page,
        anchor: word.anchor,
        label: word.token,
        kind: "clip",
        token: key,
        src,
      });
      count += 1;
    }
  });
  refreshMarks();
  updateCount();
  return count;
}

function pickWord(shell, clientX, clientY) {
  const [x, y] = pointOnPage(shell, clientX, clientY);
  const word = wordAt(Number(shell.dataset.page), x, y);
  if (!word) {
    setHint("Click the word itself. Every copy of that word opens this picture.");
    return;
  }
  if (SKIP_WORDS.has(word.token.toLowerCase())) {
    setHint("Click a name or label. Common words are left unmarked.");
    return;
  }
  const count = attachWord(word.token, paper.clipSrc);
  const placed = [...paper.popups].reverse().find((popup) => (
    popup.token === word.token.toLowerCase()
    && popup.page === Number(shell.dataset.page)
    && Math.abs(popup.anchor[0] - word.anchor[0]) < 0.5
  ));
  if (placed) openCard(placed, clientX, clientY);
  const places = count === 1 ? "1 place" : `${count} places`;
  setHint(`Attached to “${word.token}” in ${places}. Click the picture to close it. Click another word, or Done when you finish.`);
}

function cropCanvas(shell, left, top, width, height) {
  const canvas = shell.querySelector("canvas");
  const ratio = canvas.width / shell.clientWidth;
  const out = document.createElement("canvas");
  const sw = Math.max(1, Math.round(width * ratio));
  const sh = Math.max(1, Math.round(height * ratio));
  out.width = sw;
  out.height = sh;
  out.getContext("2d").drawImage(
    canvas,
    Math.max(0, Math.round(left * ratio)),
    Math.max(0, Math.round(top * ratio)),
    sw,
    sh,
    0,
    0,
    sw,
    sh,
  );
  return out.toDataURL("image/png");
}

function bindCrop(layer, shell) {
  layer.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    const bounds = shell.getBoundingClientRect();
    const startX = event.clientX - bounds.left;
    const startY = event.clientY - bounds.top;
    const box = document.createElement("div");
    box.className = "crop-box";
    layer.append(box);
    let moved = false;
    const move = (next) => {
      const current = shell.getBoundingClientRect();
      const x = next.clientX - current.left;
      const y = next.clientY - current.top;
      const left = Math.min(startX, x);
      const top = Math.min(startY, y);
      const width = Math.abs(x - startX);
      const height = Math.abs(y - startY);
      if (width > 6 || height > 6) moved = true;
      box.style.left = `${left}px`;
      box.style.top = `${top}px`;
      box.style.width = `${width}px`;
      box.style.height = `${height}px`;
    };
    const end = (next) => {
      layer.removeEventListener("pointermove", move);
      box.remove();
      const current = shell.getBoundingClientRect();
      const x = next.clientX - current.left;
      const y = next.clientY - current.top;
      if (!moved) {
        if (paper?.clipSrc) pickWord(shell, next.clientX, next.clientY);
        return;
      }
      const left = Math.max(0, Math.min(startX, x));
      const top = Math.max(0, Math.min(startY, y));
      const width = Math.min(current.width, Math.max(startX, x)) - left;
      const height = Math.min(current.height, Math.max(startY, y)) - top;
      if (width < 12 || height < 12) return;
      paper.clipSrc = cropCanvas(shell, left, top, width, height);
      const preview = document.getElementById("crop-preview");
      preview.src = paper.clipSrc;
      preview.hidden = false;
      setHint("Click a word. Every copy of that word opens this picture.");
    };
    layer.addEventListener("pointermove", move);
    layer.addEventListener("pointerup", end, { once: true });
    layer.addEventListener("pointercancel", () => {
      layer.removeEventListener("pointermove", move);
      box.remove();
    }, { once: true });
  });
}

async function startCrop() {
  cropOn = true;
  const button = document.getElementById("crop");
  button.setAttribute("aria-pressed", "true");
  button.textContent = "Done";
  setHint("Drag a rectangle around the picture you want.");
  try {
    await ensureText();
  } catch (error) {
    setHint("The words in this paper could not be read.");
    return;
  }
  if (!cropOn) return;
  refreshMarks();
}

function stopCrop() {
  const was = cropOn;
  cropOn = false;
  if (paper) paper.clipSrc = null;
  const button = document.getElementById("crop");
  if (button) {
    button.setAttribute("aria-pressed", "false");
    button.textContent = "Crop";
  }
  const preview = document.getElementById("crop-preview");
  if (preview) {
    preview.hidden = true;
    preview.removeAttribute("src");
  }
  setHint("");
  if (was) refreshMarks();
}

async function showPaper(data) {
  if (!window.pdfjsLib) {
    status.className = "status bad";
    status.textContent = "The viewer did not load. Check the connection and reload this page.";
    return;
  }
  closeCards();
  stopCrop();
  generation += 1;
  paper = data;
  paper.viewScale = 1;
  paper._pdf = await pdfjsLib.getDocument({
    url: `/job/${data.job}/pdf`,
    disableRange: true,
    disableStream: true,
  }).promise;
  title.textContent = data.filename;
  updateCount();
  home.hidden = true;
  reader.hidden = false;
  document.body.classList.add("reading");
  status.textContent = "";
  desk.innerHTML = "";
  for (let index = 0; index < paper._pdf.numPages; index += 1) {
    const [pageWidth, pageHeight] = data.pages[index];
    const shell = document.createElement("section");
    shell.className = "page";
    shell.dataset.page = String(index);
    const fitted = Math.min(920, Math.max(320, desk.clientWidth - 48));
    const scale = fitted / pageWidth;
    shell.style.width = `${pageWidth * scale}px`;
    shell.style.height = `${pageHeight * scale}px`;
    shell.innerHTML = '<canvas></canvas><div class="marks"></div>';
    desk.append(shell);
    observer.observe(shell);
  }
  const first = desk.querySelector(".page");
  if (first) renderPage(first);
}

function setPaperZoom(next) {
  if (!paper) return;
  paper.viewScale = Math.min(2.2, Math.max(0.6, next));
  generation += 1;
  desk.querySelectorAll(".page").forEach((shell) => {
    shell.dataset.token = "";
    observer.unobserve(shell);
    observer.observe(shell);
    if (shell.getBoundingClientRect().bottom > -200 && shell.getBoundingClientRect().top < window.innerHeight + 200) {
      renderPage(shell);
    }
  });
}

const scan = document.getElementById("scan");
const scanBar = document.getElementById("scan-bar");
const scanFill = document.getElementById("scan-fill");
const sampleButton = document.getElementById("sample");

function setScan(fraction, label) {
  scan.hidden = false;
  const pct = Math.max(0, Math.min(100, Math.round(fraction * 100)));
  scanFill.style.width = `${pct}%`;
  scanBar.setAttribute("aria-valuenow", String(pct));
  status.className = "status";
  status.textContent = label;
}

function hideScan() {
  scan.hidden = true;
  scanFill.style.width = "0%";
  scanBar.setAttribute("aria-valuenow", "0");
}

async function runScan(url, options) {
  const submit = form.querySelector("button[type='submit']");
  submit.disabled = true;
  sampleButton.disabled = true;
  setScan(0.02, "Opening the PDF");
  try {
    const response = await fetch(url, options);
    if (!response.ok) {
      hideScan();
      status.className = "status bad";
      status.textContent = await response.text();
      return;
    }
    const started = await response.json();
    if (!started.job) {
      hideScan();
      status.className = "status bad";
      status.textContent = "The scan did not start.";
      return;
    }
    let result = null;
    while (!result) {
      await new Promise((resolve) => setTimeout(resolve, 180));
      const tick = await fetch(`/progress/${started.job}`);
      if (!tick.ok) {
        hideScan();
        status.className = "status bad";
        status.textContent = "The scan stopped before the paper was ready.";
        return;
      }
      const event = await tick.json();
      if (event.error) {
        hideScan();
        status.className = "status bad";
        status.textContent = event.error;
        return;
      }
      setScan(event.fraction, event.label);
      if (event.done) result = event;
    }
    setScan(1, "Opening the paper");
    await showPaper(result);
    hideScan();
  } catch (error) {
    hideScan();
    status.className = "status bad";
    status.textContent = "The upload did not finish. Try the file again.";
  } finally {
    submit.disabled = false;
    sampleButton.disabled = false;
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  runScan("/annotate", { method: "POST", body: new FormData(form) });
});

sampleButton.addEventListener("click", () => {
  runScan("/sample");
});

document.getElementById("download").addEventListener("click", downloadPaper);
document.getElementById("crop").addEventListener("click", () => {
  if (cropOn) stopCrop();
  else startCrop();
});
document.getElementById("back").addEventListener("click", () => {
  closeCards();
  stopCrop();
  reader.hidden = true;
  home.hidden = false;
  document.body.classList.remove("reading");
  status.textContent = "";
});
document.getElementById("paper-in").addEventListener("click", () => setPaperZoom((paper?.viewScale || 1) * 1.15));
document.getElementById("paper-out").addEventListener("click", () => setPaperZoom((paper?.viewScale || 1) / 1.15));

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  const cards = [...document.querySelectorAll(".card")];
  if (cards.length) {
    cards.sort((a, b) => Number(b.style.zIndex) - Number(a.style.zIndex));
    cards[0].remove();
    return;
  }
  if (cropOn) stopCrop();
});
