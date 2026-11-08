// MTG Ledger — camera page: live preview; a card is sent when the picture stays still (auto) or on shutter tap.
// The server answers with the HTML of the "last card" box; everything else is plain forms.
(() => {
  const STILL = 6, MOTION = 18, STILL_MS = 700; // mean grey-level change per pixel (0-255), hold time
  const root = document.getElementById("scan");
  const video = root.querySelector("video");
  const guide = root.querySelector("[data-guide]");
  const shutter = root.querySelector("[data-shutter]");
  const auto = root.querySelector("[data-auto]");
  const help = root.querySelector("[data-help]");
  const csrf = root.querySelector("input[name=csrfmiddlewaretoken]").value;
  let busy = false, armed = true, stillSince = 0, previous = null;

  navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment", width: { ideal: 1920 } } })
    .then((stream) => {
      video.srcObject = stream;
      shutter.disabled = false;
      const [track] = stream.getVideoTracks();
      if (track.getCapabilities?.().focusMode?.includes("continuous")) {
        track.applyConstraints({ advanced: [{ focusMode: "continuous" }] });
      }
    })
    .catch(() => { help.textContent = "Camera not available (HTTPS and permission are required). Use Search."; });

  // The part of the video under the on-screen guide, as a JPEG.
  function photo() {
    const v = video.getBoundingClientRect(), g = guide.getBoundingClientRect();
    const k = Math.max(v.width / video.videoWidth, v.height / video.videoHeight); // object-fit: cover
    const x = (g.left - v.left - (v.width - video.videoWidth * k) / 2) / k;
    const y = (g.top - v.top - (v.height - video.videoHeight * k) / 2) / k;
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(g.width / k);
    canvas.height = Math.round(g.height / k);
    canvas.getContext("2d").drawImage(video, x, y, canvas.width, canvas.height, 0, 0, canvas.width, canvas.height);
    return new Promise((done) => canvas.toBlob(done, "image/jpeg", 0.8));
  }

  async function capture() {
    if (busy) return;
    busy = shutter.disabled = true;
    guide.classList.add("is-busy");
    const form = new FormData();
    form.append("frame", await photo(), "card.jpg");
    form.append("capture_id", crypto.randomUUID());
    try {
      const resp = await fetch(root.dataset.captureUrl, { method: "POST", body: form, headers: { "X-CSRFToken": csrf } });
      if (resp.headers.get("X-Scan-Redirect")) return void (location.href = resp.headers.get("X-Scan-Redirect"));
      document.getElementById("scan-toast").outerHTML = await resp.text();
      root.querySelector("[data-count]").textContent = resp.headers.get("X-Scan-Count");
    } catch {
      help.textContent = "No connection — take the photo again.";
    } finally {
      busy = shutter.disabled = false;
      guide.classList.remove("is-busy");
    }
  }

  // Auto: average change between small greyscale frames. A card that stays still is sent once;
  // the next capture needs movement first (a new card), so the same card is never sent twice.
  const small = document.createElement("canvas");
  small.width = 48; small.height = 36;
  const ctx = small.getContext("2d", { willReadFrequently: true });
  setInterval(() => {
    if (busy || auto.getAttribute("aria-pressed") !== "true" || !video.videoWidth) return;
    ctx.drawImage(video, 0, 0, small.width, small.height);
    const px = ctx.getImageData(0, 0, small.width, small.height).data;
    let change = 0;
    for (let i = 0; previous && i < px.length; i += 4) change += Math.abs(px[i + 1] - previous[i + 1]);
    change = previous ? change / (px.length / 4) : 255;
    previous = px;
    if (change > MOTION) { armed = true; stillSince = 0; return; }
    if (!armed || change > STILL) { stillSince = 0; return; }
    stillSince ||= performance.now();
    if (performance.now() - stillSince > STILL_MS) { armed = false; capture(); }
  }, 150);

  shutter.addEventListener("click", capture);
  auto.addEventListener("click", () => {
    const on = auto.getAttribute("aria-pressed") !== "true";
    auto.setAttribute("aria-pressed", on);
    auto.textContent = on ? "Auto on" : "Auto off";
    armed = on; stillSince = 0;
  });
})();
