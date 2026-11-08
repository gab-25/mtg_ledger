// MTG Ledger — camera page: live preview, tap the shutter to send the card under the guide.
// The server answers with the HTML of the "last card" box; everything else is plain forms.
(() => {
  const root = document.getElementById("scan");
  const video = root.querySelector("video");
  const guide = root.querySelector("[data-guide]");
  const shutter = root.querySelector("[data-shutter]");
  const help = root.querySelector("[data-help]");
  const csrf = root.querySelector("input[name=csrfmiddlewaretoken]").value;

  navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment", width: { ideal: 1920 } } })
    .then((stream) => { video.srcObject = stream; shutter.disabled = false; })
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

  shutter.addEventListener("click", async () => {
    shutter.disabled = true;
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
      shutter.disabled = false;
      guide.classList.remove("is-busy");
    }
  });
})();
