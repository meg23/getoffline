(() => {
  const media = document.getElementById("player");
  const documentViewer = document.querySelector("[data-document-viewer]");
  const fullscreenButton = document.querySelector("[data-document-fullscreen]");
  const form = document.getElementById("position-form");
  const input = document.getElementById("position-seconds");
  const seek = Number(media?.dataset.seekSeconds || 0);
  const shouldAutoPlay = true;
  let lastSentSeconds = -1;
  let pendingForcedSave = false;
  let hasAppliedInitialSeek = !(seek > 0);
  const periodicProgressSeconds = 5;

  function documentViewerIsFullscreen() {
    return (
      document.fullscreenElement === documentViewer ||
      document.webkitFullscreenElement === documentViewer
    );
  }

  function updateFullscreenButton() {
    if (!fullscreenButton || !documentViewer) return;
    const supported = Boolean(
      documentViewer.requestFullscreen ||
        documentViewer.webkitRequestFullscreen,
    );
    fullscreenButton.hidden = !supported;
    if (!supported) return;
    const active = documentViewerIsFullscreen();
    fullscreenButton.setAttribute("aria-pressed", String(active));
    fullscreenButton.setAttribute(
      "aria-label",
      active ? "Exit fullscreen" : "Enter fullscreen",
    );
    fullscreenButton.innerHTML = `<span aria-hidden="true">${
      active ? "⤢" : "⛶"
    }</span> ${active ? "Exit fullscreen" : "Fullscreen"}`;
  }

  async function toggleDocumentFullscreen() {
    if (!documentViewer) return;
    try {
      if (documentViewerIsFullscreen()) {
        if (document.exitFullscreen) {
          await document.exitFullscreen();
        } else if (document.webkitExitFullscreen) {
          document.webkitExitFullscreen();
        }
        return;
      }
      if (documentViewer.requestFullscreen) {
        await documentViewer.requestFullscreen();
      } else if (documentViewer.webkitRequestFullscreen) {
        documentViewer.webkitRequestFullscreen();
      }
    } catch (err) {
      console.debug("[getoffline] document fullscreen failed", { err });
    }
  }

  fullscreenButton?.addEventListener("click", toggleDocumentFullscreen);
  document.addEventListener("fullscreenchange", updateFullscreenButton);
  document.addEventListener("webkitfullscreenchange", updateFullscreenButton);
  updateFullscreenButton();

  function initialSeekTarget() {
    if (!media || !(seek > 0)) return 0;
    return Number.isFinite(media.duration) && media.duration > 1
      ? Math.min(seek, Math.max(media.duration - 1, 0))
      : seek;
  }

  function applyInitialSeek() {
    if (!media || hasAppliedInitialSeek) return true;
    const target = initialSeekTarget();
    if (!(target > 0)) {
      hasAppliedInitialSeek = true;
      return true;
    }
    try {
      if (Math.abs(Number(media.currentTime || 0) - target) > 0.75) {
        media.currentTime = target;
      }
      hasAppliedInitialSeek =
        Math.abs(Number(media.currentTime || 0) - target) <= 0.75;
      console.debug("[getoffline] player resume seek", {
        target,
        currentTime: media.currentTime,
        applied: hasAppliedInitialSeek,
      });
    } catch (err) {
      console.debug("[getoffline] player resume seek failed", { err });
    }
    return hasAppliedInitialSeek;
  }

  function savePosition(reason = "timeupdate", forced = false) {
    if (!media || !form || !input) return Promise.resolve(null);
    if (!hasAppliedInitialSeek && !applyInitialSeek()) {
      return Promise.resolve(null);
    }
    const seconds =
      reason === "ended" ? 0 : Math.max(0, Number(media.currentTime || 0));
    if (
      !forced &&
      Math.abs(seconds - lastSentSeconds) < periodicProgressSeconds
    ) {
      return Promise.resolve(null);
    }
    lastSentSeconds = seconds;
    input.value = seconds.toFixed(3);
    const body = new FormData(form);
    body.set("reason", reason);
    body.set("forced", forced ? "1" : "0");
    return fetch(form.action, { method: "POST", body, keepalive: forced })
      .then((response) => {
        console.debug("[getoffline] player save position", {
          reason,
          forced,
          seconds,
          status: response.status,
        });
        return response;
      })
      .catch((err) => {
        console.debug("[getoffline] player save position failed", {
          reason,
          forced,
          seconds,
          err,
        });
        return null;
      });
  }

  function savePositionBeacon(reason) {
    if (!media || !form || !input || !navigator.sendBeacon) return false;
    if (!hasAppliedInitialSeek && !applyInitialSeek()) return false;
    const seconds =
      reason === "ended" ? 0 : Math.max(0, Number(media.currentTime || 0));
    input.value = seconds.toFixed(3);
    const body = new FormData(form);
    body.set("reason", reason);
    body.set("forced", "1");
    return navigator.sendBeacon(form.action, body);
  }

  media?.addEventListener("loadedmetadata", applyInitialSeek);
  media?.addEventListener("canplay", () => {
    applyInitialSeek();
    if (shouldAutoPlay && media.paused) {
      media
        .play()
        .catch((err) =>
          console.debug("[getoffline] player autoplay failed", { err }),
        );
    }
  });
  media?.addEventListener("playing", applyInitialSeek);
  media?.addEventListener("timeupdate", () => {
    if (!hasAppliedInitialSeek && !applyInitialSeek()) return;
    if (!media.paused) savePosition("timeupdate", false);
  });
  media?.addEventListener("pause", () => savePosition("pause", true));
  media?.addEventListener("seeking", () => {
    pendingForcedSave = true;
  });
  media?.addEventListener("seeked", () => {
    if (pendingForcedSave) savePosition("seeked", true);
    pendingForcedSave = false;
  });
  media?.addEventListener("ended", () => savePosition("ended", true));
  window.addEventListener("pagehide", () => {
    if (!savePositionBeacon("pagehide")) savePosition("pagehide", true);
  });
})();
