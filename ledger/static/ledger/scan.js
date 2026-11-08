// MTG Ledger — camera auto-capture for card scanning.
// Captures a card when the picture is still and sharp, posts it to the server, shows the result.
(() => {
  "use strict";

  // Tuning (mean absolute grey-level difference 0-255, Laplacian variance).
  const SAMPLE_MS = 150;     // how often frames are compared
  const STILL_DIFF = 6;      // below: the picture is not moving
  const MOTION_DIFF = 18;    // above: the card was moved (re-arms capture)
  const STILL_MS = 600;      // still for this long before capturing
  const SHARP_MIN = 60;      // minimum sharpness inside the guide
  const CAPTURE_LONG_SIDE = 1000;
  const JPEG_QUALITY = 0.8;

  const root = document.getElementById("scan");
  if (!root) return;
  const $ = (sel) => root.querySelector(sel);
  const video = $(".scan-video");
  const guide = $("[data-guide]");
  const spinner = $("[data-spinner]");
  const help = $("[data-help]");
  const toast = $("[data-toast]");
  const toastState = $("[data-toast-state]");
  const toastName = $("[data-toast-name]");
  const toastMeta = $("[data-toast-meta]");
  const foilBtn = $("[data-foil]");
  const undoBtn = $("[data-undo]");
  const shutter = $("[data-shutter]");
  const countEl = $("[data-count]");
  const csrf = root.querySelector("input[name=csrfmiddlewaretoken]").value;
  const captureUrl = root.dataset.captureUrl;
  const lineUrl = (id) => root.dataset.lineUrl.replace(/\/0\/$/, `/${id}/`);

  const small = document.createElement("canvas");
  small.width = 80;
  small.height = 60;
  const smallCtx = small.getContext("2d", { willReadFrequently: true });
  const sharpCanvas = document.createElement("canvas");
  const sharpCtx = sharpCanvas.getContext("2d", { willReadFrequently: true });

  let previous = null;
  let stillSince = 0;
  let armed = true;      // false after a capture until the card moves
  let busy = false;
  let timer = null;

  function setHelp(text) { help.textContent = text; }

  function showToast({ state, name = "", meta = "", error = false, lineId = "", foil = false }) {
    toast.hidden = false;
    toast.classList.toggle("is-error", error);
    toastState.textContent = state;
    toastName.textContent = name;
    toastMeta.textContent = meta;
    for (const btn of [foilBtn, undoBtn]) {
      btn.dataset.line = lineId;
      btn.hidden = !lineId;
    }
    foilBtn.setAttribute("aria-pressed", foil ? "true" : "false");
  }

  // Region of the video frame under the on-screen guide, in video pixels.
  function guideRect() {
    const vw = video.videoWidth, vh = video.videoHeight;
    const box = video.getBoundingClientRect();
    const g = guide.getBoundingClientRect();
    const scale = Math.max(box.width / vw, box.height / vh);
    const offX = (box.width - vw * scale) / 2;
    const offY = (box.height - vh * scale) / 2;
    const x = Math.max(0, (g.left - box.left - offX) / scale);
    const y = Math.max(0, (g.top - box.top - offY) / scale);
    return {
      x, y,
      w: Math.min(vw - x, g.width / scale),
      h: Math.min(vh - y, g.height / scale),
    };
  }

  function grey(data, i) { return (data[i] * 3 + data[i + 1] * 6 + data[i + 2]) / 10; }

  function frameDiff() {
    smallCtx.drawImage(video, 0, 0, small.width, small.height);
    const { data } = smallCtx.getImageData(0, 0, small.width, small.height);
    const current = new Float32Array(small.width * small.height);
    for (let p = 0, i = 0; p < current.length; p++, i += 4) current[p] = grey(data, i);
    let diff = Infinity;
    if (previous) {
      let sum = 0;
      for (let p = 0; p < current.length; p++) sum += Math.abs(current[p] - previous[p]);
      diff = sum / current.length;
    }
    previous = current;
    return diff;
  }

  function sharpness(r) {
    const w = 160, h = Math.round(160 * r.h / r.w);
    sharpCanvas.width = w;
    sharpCanvas.height = h;
    sharpCtx.drawImage(video, r.x, r.y, r.w, r.h, 0, 0, w, h);
    const { data } = sharpCtx.getImageData(0, 0, w, h);
    const g = new Float32Array(w * h);
    for (let p = 0, i = 0; p < g.length; p++, i += 4) g[p] = grey(data, i);
    let sum = 0, sumSq = 0, n = 0;
    for (let y = 1; y < h - 1; y++) {
      for (let x = 1; x < w - 1; x++) {
        const p = y * w + x;
        const lap = 4 * g[p] - g[p - 1] - g[p + 1] - g[p - w] - g[p + w];
        sum += lap; sumSq += lap * lap; n++;
      }
    }
    const mean = sum / n;
    return sumSq / n - mean * mean;
  }

  function snapshot(r) {
    const scale = CAPTURE_LONG_SIDE / Math.max(r.w, r.h);
    const c = document.createElement("canvas");
    c.width = Math.round(r.w * Math.min(1, scale));
    c.height = Math.round(r.h * Math.min(1, scale));
    c.getContext("2d").drawImage(video, r.x, r.y, r.w, r.h, 0, 0, c.width, c.height);
    return new Promise((resolve) => c.toBlob(resolve, "image/jpeg", JPEG_QUALITY));
  }

  async function post(url, body) {
    const resp = await fetch(url, {
      method: "POST",
      body,
      headers: { "X-CSRFToken": csrf, Accept: "application/json" },
      credentials: "same-origin",
    });
    let data = null;
    try { data = await resp.json(); } catch (_) { /* not JSON */ }
    return { resp, data };
  }

  async function capture() {
    if (busy || !video.videoWidth) return;
    busy = true;
    armed = false;
    guide.classList.add("is-busy");
    spinner.hidden = false;
    try {
      const blob = await snapshot(guideRect());
      const id = (crypto.randomUUID && crypto.randomUUID()) || `${Date.now()}-${Math.random()}`;
      const form = () => {
        const f = new FormData();
        f.append("frame", blob, "card.jpg");
        f.append("capture_id", id);
        return f;
      };
      let result;
      try {
        result = await post(captureUrl, form());
      } catch (_) {
        result = await post(captureUrl, form()); // one retry, same capture_id: never counted twice
      }
      const { data } = result;
      if (data && data.status === "added") {
        const l = data.line;
        countEl.textContent = data.count;
        showToast({
          state: `Added · now ${l.quantity} ${l.quantity === 1 ? "copy" : "copies"}`,
          name: l.name,
          meta: `${l.set_code} ${l.number} · ${l.condition} · ${l.language}`,
          lineId: l.id, foil: l.foil,
        });
        if (navigator.vibrate) navigator.vibrate(30);
        setHelp("Move to the next card");
      } else if (data && data.status === "uncertain") {
        window.location.href = data.resolve_url;
        return;
      } else {
        throw new Error((data && data.message) || "Unexpected answer");
      }
    } catch (_) {
      showToast({ state: "Couldn't recognise — hold the card still to retry", error: true });
      armed = true; // retry without moving the card
    } finally {
      busy = false;
      guide.classList.remove("is-busy");
      spinner.hidden = true;
      stillSince = 0;
    }
  }

  function tick() {
    if (busy || !video.videoWidth || document.hidden) return;
    const diff = frameDiff();
    if (diff > MOTION_DIFF) armed = true;
    const still = diff < STILL_DIFF;
    guide.classList.toggle("is-still", still && armed);
    if (!still || !armed) {
      stillSince = 0;
      return;
    }
    const now = performance.now();
    if (!stillSince) stillSince = now;
    if (now - stillSince < STILL_MS) return;
    if (sharpness(guideRect()) < SHARP_MIN) {
      setHelp("Hold steady — focusing…");
      return;
    }
    capture();
  }

  async function lineAction(btn, fields) {
    const id = btn.dataset.line;
    if (!id) return;
    const f = new FormData();
    for (const [k, v] of Object.entries(fields)) f.append(k, v);
    try {
      const { resp, data } = await post(lineUrl(id), f);
      if (!resp.ok || !data) throw new Error();
      countEl.textContent = data.count;
      return data;
    } catch (_) {
      showToast({ state: "Couldn't update the card — try again from Review", error: true });
      return null;
    }
  }

  foilBtn.addEventListener("click", async () => {
    const on = foilBtn.getAttribute("aria-pressed") !== "true";
    const data = await lineAction(foilBtn, { foil: on ? "1" : "0" });
    if (!data) return;
    foilBtn.setAttribute("aria-pressed", on ? "true" : "false");
    if (data.line_id) { foilBtn.dataset.line = data.line_id; undoBtn.dataset.line = data.line_id; }
  });

  undoBtn.addEventListener("click", async () => {
    const data = await lineAction(undoBtn, { undo: "1" });
    if (!data) return;
    showToast({ state: "Removed one copy" });
  });

  shutter.addEventListener("click", () => { armed = true; capture(); });

  async function start() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setHelp("This browser cannot use the camera here (HTTPS is required). Use Search to add cards.");
      return;
    }
    try {
      video.srcObject = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "environment", width: { ideal: 1920 }, height: { ideal: 1080 } },
        audio: false,
      });
      await video.play();
      timer = setInterval(tick, SAMPLE_MS);
    } catch (err) {
      setHelp(err && err.name === "NotAllowedError"
        ? "Camera permission denied. Allow it in the browser settings, or use Search."
        : "No camera available. Use Search to add cards.");
    }
  }

  document.addEventListener("visibilitychange", () => { previous = null; stillSince = 0; });
  window.addEventListener("pagehide", () => {
    clearInterval(timer);
    const stream = video.srcObject;
    if (stream) stream.getTracks().forEach((t) => t.stop());
  });
  if (!foilBtn.dataset.line) { foilBtn.hidden = true; undoBtn.hidden = true; }
  start();
})();
