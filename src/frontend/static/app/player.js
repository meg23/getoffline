(() => {
  const media = document.getElementById("player");
  const documentViewer = document.querySelector("[data-document-viewer]");
  const fullscreenButton = document.querySelector("[data-document-fullscreen]");
  const nextButton = document.getElementById("next-playlist-button");
  const form = document.getElementById("position-form");
  const input = document.getElementById("position-seconds");
  const playlistId = media?.dataset.playlistId || "";
  const playlistMode = Boolean(playlistId);
  const seek = playlistMode ? 0 : Number(media?.dataset.seekSeconds || 0);
  const playlistItems = String(media?.dataset.playlistItems || "")
    .split(",")
    .map((value) => Number(value))
    .filter(Boolean);
  let currentItemId = Number(media?.dataset.currentId || 0);
  let currentMediaKind = media?.dataset.mediaKind || "audio";
  const shouldAutoPlay = true;
  let lastSentSeconds = -1;
  let pendingForcedSave = false;
  let hasAppliedInitialSeek = !(seek > 0);
  const periodicProgressSeconds = 5;
  const fullscreenIntentKey = "getoffline:playlist-fullscreen-next";
  let fullscreenRestoreAttempted = false;
  let nextItemInFlight = false;
  let standaloneNextId = 0;

  function mediaIsFullscreen() {
    return document.fullscreenElement === media || document.webkitFullscreenElement === media;
  }

  function rememberFullscreenForNextItem() {
    if (!playlistMode) return;
    try {
      sessionStorage.setItem(fullscreenIntentKey, mediaIsFullscreen() ? "1" : "0");
    } catch (_) {}
  }

  async function restoreFullscreenIfRequested() {
    if (!playlistMode || fullscreenRestoreAttempted || !media) return;
    fullscreenRestoreAttempted = true;
    let shouldRestore = false;
    try {
      shouldRestore = sessionStorage.getItem(fullscreenIntentKey) === "1";
      sessionStorage.removeItem(fullscreenIntentKey);
    } catch (_) {}
    if (!shouldRestore || !media.requestFullscreen) return;
    try {
      await media.requestFullscreen();
    } catch (err) {
      console.debug("[getoffline] playlist fullscreen restore failed", { err });
    }
  }

  async function loadNextPlaylistItem(nextId, playlistIndex) {
    const response = await fetch(
      `/api/frontend/player/${nextId}?playlist=${playlistId}&t=0`,
      { credentials: "same-origin", headers: { Accept: "application/json" } },
    );
    if (!response.ok) throw new Error("Unable to load the next playlist item.");
    const payload = await response.json();
    const nextKind = payload.media_kind || "audio";
    if (nextKind !== currentMediaKind) {
      rememberFullscreenForNextItem();
      window.location.assign(`/play/${nextId}/?playlist=${playlistId}`);
      return;
    }

    currentItemId = nextId;
    standaloneNextId = 0;
    media.dataset.currentId = String(nextId);
    currentMediaKind = nextKind;
    media.dataset.seekSeconds = "0";
    media.src = `/media/${nextId}/#t=0.000`;
    media.load();
    if (form) form.action = `/downloads/${nextId}/position/`;
    if (input) input.value = "0.000";
    lastSentSeconds = -1;
    hasAppliedInitialSeek = true;
    const title = document.querySelector(".player-header h1");
    if (title) title.textContent = payload.item?.title || "Untitled";
    window.history.replaceState(
      {},
      "",
      `/play/${nextId}/?playlist=${playlistId}&playlist_index=${playlistIndex}`,
    );
    try {
      await media.play();
    } catch (err) {
      console.debug("[getoffline] next playlist item autoplay failed", { err });
    }
  }

  async function nextLibraryId() {
    if (standaloneNextId) return standaloneNextId;
    const response = await fetch(`/api/frontend/player/${currentItemId}/next`, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) return 0;
    const payload = await response.json();
    standaloneNextId = Number(payload.next?.id || 0);
    return standaloneNextId;
  }

  function nextPlaylistId() {
    const currentIndex = playlistItems.indexOf(currentItemId);
    return playlistItems[currentIndex + 1] || 0;
  }

  async function updateNextButton() {
    if (!nextButton) return;
    nextButton.disabled = true;
    const nextId = playlistMode ? nextPlaylistId() : await nextLibraryId();
    nextButton.disabled = !nextId || nextItemInFlight;
  }

  async function advanceToNextPlaylistItem() {
    const nextId = playlistMode ? nextPlaylistId() : await nextLibraryId();
    if (!nextId || nextItemInFlight) return false;
    nextItemInFlight = true;
    updateNextButton();
    const currentIndex = playlistItems.indexOf(currentItemId);
    try {
      await loadNextPlaylistItem(nextId, currentIndex + 1);
    } catch (err) {
      rememberFullscreenForNextItem();
      window.location.assign(`/play/${nextId}/?playlist=${playlistId}`);
    } finally {
      nextItemInFlight = false;
      updateNextButton();
    }
    return true;
  }

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
    restoreFullscreenIfRequested();
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
  nextButton?.addEventListener("click", () => {
    advanceToNextPlaylistItem();
  });
  updateNextButton();
  media?.addEventListener("ended", async () => {
    await savePosition("ended", true);
    await advanceToNextPlaylistItem();
  });
  window.addEventListener("pagehide", () => {
    if (!savePositionBeacon("pagehide")) savePosition("pagehide", true);
  });
})();
