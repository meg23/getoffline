(() => {
  let rows = Array.from(
    document.querySelectorAll("#downloads-table-body tr[data-row-id]"),
  );
  const tableBody = document.getElementById("downloads-table-body");
  const filterInput = document.getElementById("library-filter");
  const filterMode = document.getElementById("library-filter-mode");
  const clearButton = document.getElementById("library-filter-clear");
  const filterWrap = document.getElementById("library-filter-wrap");
  const filterToggle = document.getElementById("library-filter-toggle");
  const selectAll = document.getElementById("select-all");
  const batchAction = document.getElementById("batch-action");
  const batchApply = document.getElementById("batch-apply");
  const selectors = () =>
    rows.map((row) => row.querySelector(".row-selector")).filter(Boolean);

  const visibleRows = () => rows.filter((row) => row.style.display !== "none");

  function refreshRows() {
    rows = Array.from(
      document.querySelectorAll("#downloads-table-body tr[data-row-id]"),
    );
  }

  function bindSelector(input) {
    if (!input || input.dataset.libraryBound === "1") return;
    input.dataset.libraryBound = "1";
    input.addEventListener("change", updateBatchState);
  }

  function rowUrl(name, id) {
    const suffix = name === "player" ? `play/${id}/` : `${name}/${id}/`;
    return `/${suffix}`;
  }

  function renderCell(row, className, text) {
    const cell = document.createElement("td");
    cell.className = className;
    const labels = {
      "channel-col": "Channel",
      "source-col": "Source",
      "type-col": "Type",
      "size-col": "Size",
      "status-col": "Status",
    };
    if (labels[className]) cell.dataset.label = labels[className];
    cell.textContent = text || "";
    row.appendChild(cell);
    return cell;
  }

  function itemRow(item) {
    const row = document.createElement("tr");
    const id = String(item.id || "");
    row.dataset.rowId = id;
    row.dataset.played = item.played ? "1" : "0";
    row.dataset.favorite = item.favorite ? "1" : "0";
    row.dataset.channel = item.source_name || "";
    row.dataset.title = item.title || "";
    row.dataset.kind = item.display_kind || "audio";
    row.dataset.downloadStatus = item.download_status || "";
    row.dataset.mediaUrl = item.stream_url || `/api/stream/${id}`;
    row.dataset.subtitleUrl = item.has_subtitles
      ? item.api_subtitles_url || `/api/subtitle/${id}`
      : "";
    row.dataset.resumeSeconds = String(item.last_position_seconds || 0);

    const channel = renderCell(
      row,
      "channel-col",
      item.source_name || item.source_type || "",
    );
    channel.title = item.source_name || "";

    const episode = document.createElement("td");
    episode.className = "episode-col";
    episode.dataset.label = "Episode";
    const link = document.createElement("a");
    link.className = "episode-link";
    link.href = rowUrl("player", id);
    link.dataset.rowId = id;
    link.dataset.kind = item.display_kind || "audio";
    link.dataset.source = item.source_name || item.source_type || "";
    link.dataset.hasSubtitles = item.has_subtitles ? "1" : "0";
    link.dataset.resumeSeconds = String(item.last_position_seconds || 0);
    link.dataset.title = item.title || "Untitled";
    link.dataset.playLink = "1";
    link.textContent = item.title || "Untitled";
    episode.appendChild(link);
    row.appendChild(episode);

    const source = renderCell(row, "source-col", "");
    const sourcePill = document.createElement("span");
    sourcePill.className = "pill status-new";
    sourcePill.textContent = String(item.source_type || "").toUpperCase();
    source.appendChild(sourcePill);

    const type = renderCell(row, "type-col", "");
    const typePill = document.createElement("span");
    typePill.className = "pill";
    typePill.textContent = item.display_type || "?";
    type.appendChild(typePill);

    renderCell(row, "size-col", item.display_size || "—");

    const status = renderCell(row, "status-col", "");
    const statusPill = document.createElement("span");
    statusPill.className = `pill ${item.status_class || "status-unplayed"}`;
    statusPill.textContent = item.status_label || "UNPLAYED";
    status.appendChild(statusPill);

    const selection = document.createElement("td");
    selection.className = "selection-cell";
    const checkbox = document.createElement("input");
    checkbox.setAttribute("form", "batch-form");
    checkbox.type = "checkbox";
    checkbox.className = "row-selector";
    checkbox.name = "ids";
    checkbox.value = id;
    checkbox.setAttribute("aria-label", `Select ${item.title || "item"}`);
    selection.appendChild(checkbox);
    row.appendChild(selection);
    bindSelector(checkbox);

    return row;
  }

  async function hydrateEmptyLibraryFromApi() {
    if (!tableBody || rows.length > 0) return;
    const url = new URL("/api/frontend/library", window.location.origin);
    const mode = filterMode?.value || "unplayed";
    if (mode !== "unplayed") url.searchParams.set("filter", mode);
    const response = await fetch(url, {
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) return;
    const payload = await response.json();
    const downloads = Array.isArray(payload.downloads) ? payload.downloads : [];
    if (!downloads.length) return;
    tableBody.replaceChildren(...downloads.map(itemRow));
    refreshRows();
  }

  function applyFilters() {
    const term = (filterInput?.value || "").trim().toLowerCase();
    const mode = filterMode?.value || "unplayed";
    rows.forEach((row) => {
      const text =
        `${row.dataset.channel || ""} ${row.dataset.title || ""}`.toLowerCase();
      const matchesText = !term || text.includes(term);
      const played = row.dataset.played === "1";
      const favorite = row.dataset.favorite === "1";
      const unavailable = ["missing", "retention_deleted"].includes(
        row.dataset.downloadStatus || "",
      );
      const matchesMode =
        mode === "all" ||
        (!unavailable && mode === "played" && played) ||
        (!unavailable && mode === "favorites" && favorite) ||
        (!unavailable && mode === "unplayed" && !played);
      row.style.display = matchesText && matchesMode ? "" : "none";
    });
    updateBatchState();
  }

  function updateBatchState() {
    const selected = visibleRows().some(
      (row) => row.querySelector(".row-selector")?.checked,
    );
    if (batchApply) batchApply.disabled = !selected || !batchAction?.value;
    if (selectAll) {
      const visible = visibleRows();
      const checked = visible.filter(
        (row) => row.querySelector(".row-selector")?.checked,
      ).length;
      selectAll.checked = visible.length > 0 && checked === visible.length;
      selectAll.indeterminate = checked > 0 && checked < visible.length;
    }
  }

  function syncServerFilterMode() {
    if (!filterMode) return false;
    const serverMode = filterMode.dataset.serverMode || "unplayed";
    const selectedMode = filterMode.value || "unplayed";
    if (selectedMode === serverMode) return false;

    const url = new URL(window.location.href);
    if (selectedMode === "unplayed") {
      url.searchParams.delete("filter");
    } else {
      url.searchParams.set("filter", selectedMode);
    }
    window.location.href = url.toString();
    return true;
  }

  filterInput?.addEventListener("input", applyFilters);
  filterMode?.addEventListener("change", () => {
    if (!syncServerFilterMode()) applyFilters();
  });
  clearButton?.addEventListener("click", () => {
    if (filterInput) filterInput.value = "";
    if (filterMode) filterMode.value = "unplayed";
    if (!syncServerFilterMode()) applyFilters();
  });
  function setFilterOpen(isOpen, focusInput = false) {
    if (!filterWrap || !filterToggle) return;
    filterWrap.classList.toggle("is-open", isOpen);
    filterToggle.setAttribute("aria-expanded", String(isOpen));
    if (isOpen && focusInput) filterInput?.focus();
  }
  filterToggle?.addEventListener("click", () => {
    setFilterOpen(!filterWrap?.classList.contains("is-open"), true);
  });
  setFilterOpen(
    filterMode?.value !== "unplayed" || Boolean(filterInput?.value),
  );
  selectors().forEach(bindSelector);
  batchAction?.addEventListener("change", updateBatchState);
  selectAll?.addEventListener("change", () => {
    visibleRows().forEach((row) => {
      const input = row.querySelector(".row-selector");
      if (input) input.checked = selectAll.checked;
    });
    updateBatchState();
  });

  function openModal(id) {
    const el = document.getElementById(id);
    if (el) {
      el.classList.add("is-open");
      el.setAttribute("aria-hidden", "false");
    }
  }
  function closeModals() {
    document.querySelectorAll(".modal-backdrop").forEach((el) => {
      el.classList.remove("is-open");
      el.setAttribute("aria-hidden", "true");
    });
  }
  document
    .getElementById("quick-add-open")
    ?.addEventListener("click", () => openModal("quick-add-backdrop"));
  document
    .getElementById("quick-add-open-hero")
    ?.addEventListener("click", () => openModal("quick-add-backdrop"));
  document
    .getElementById("transcript-search-open")
    ?.addEventListener("click", () => openModal("transcript-search-backdrop"));
  document
    .querySelectorAll("[data-modal-close]")
    .forEach((button) => button.addEventListener("click", closeModals));
  document.querySelectorAll(".modal-backdrop").forEach((backdrop) =>
    backdrop.addEventListener("click", (event) => {
      if (event.target === backdrop) closeModals();
    }),
  );
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeModals();
  });

  hydrateEmptyLibraryFromApi().finally(applyFilters);
})();
(() => {
  function readCookie(name) {
    return (
      document.cookie
        .split(";")
        .map((v) => v.trim())
        .find((v) => v.startsWith(`${name}=`))
        ?.slice(name.length + 1) || ""
    );
  }

  const form = document.getElementById("update-form");
  const button = document.getElementById("update-button");
  const csrf =
    form?.querySelector("[name=csrfmiddlewaretoken]")?.value ||
    decodeURIComponent(readCookie("csrftoken") || "");
  const doneStatuses = new Set(["succeeded", "failed"]);
  const statusStorageKey = "getoffline:update-downloads-status-url";
  let pollTimer = 0;

  function setLoading(loading) {
    if (!button) return;
    button.classList.toggle("is-pulsing", loading);
    button.disabled = loading;
    button.setAttribute("aria-busy", loading ? "true" : "false");
  }

  function rememberStatusUrl(statusUrl) {
    try {
      window.sessionStorage?.setItem(statusStorageKey, statusUrl);
    } catch (_) {}
  }

  function forgetStatusUrl() {
    try {
      window.sessionStorage?.removeItem(statusStorageKey);
    } catch (_) {}
  }

  function schedulePoll(statusUrl) {
    pollTimer = window.setTimeout(() => {
      pollUntilDone(statusUrl).catch(() => schedulePoll(statusUrl));
    }, 1500);
  }

  async function pollUntilDone(statusUrl) {
    const response = await fetch(statusUrl, {
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error("Unable to check update status.");
    const payload = await response.json();
    if (payload.finished || doneStatuses.has(String(payload.status || ""))) {
      forgetStatusUrl();
      setLoading(false);
      if (payload.status === "failed" || payload.ok === false) {
        window.alert(payload.error_message || "The source update failed.");
      } else {
        window.location.reload();
      }
      return;
    }
    schedulePoll(statusUrl);
  }

  function startPolling(statusUrl) {
    rememberStatusUrl(statusUrl);
    setLoading(true);
    pollUntilDone(statusUrl).catch(() => schedulePoll(statusUrl));
  }

  function showQueueError(message) {
    setLoading(false);
    window.clearTimeout(pollTimer);
    window.alert(message || "Failed to start the source update.");
  }

  try {
    const storedStatusUrl = window.sessionStorage?.getItem(statusStorageKey) || "";
    if (storedStatusUrl) startPolling(storedStatusUrl);
  } catch (_) {}

  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form || !button || button.disabled) return;
    setLoading(true);
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "X-CSRFToken": csrf,
          "X-Requested-With": "XMLHttpRequest",
        },
      });
      if (!response.ok) throw new Error("The source update request failed.");
      const payload = await response.json();
      if (!payload.status_url) throw new Error("Missing update status URL.");
      startPolling(payload.status_url);
    } catch (error) {
      showQueueError(error.message);
    }
  });
})();
(() => {
  function readCookie(name) {
    return (
      document.cookie
        .split(";")
        .map((v) => v.trim())
        .find((v) => v.startsWith(`${name}=`))
        ?.slice(name.length + 1) || ""
    );
  }
  const csrf =
    document.querySelector("[name=csrfmiddlewaretoken]")?.value ||
    decodeURIComponent(readCookie("csrftoken") || "");
  const batchForm = document.getElementById("batch-form");
  const batchAction = document.getElementById("batch-action");
  const metadataBackdrop = document.getElementById("metadata-edit-backdrop");
  const metadataForm = document.getElementById("metadata-edit-form");
  const metadataId = document.getElementById("metadata-edit-id");
  const metadataTitle = document.getElementById("metadata-edit-item-title");
  const metadataSource = document.getElementById("metadata-edit-source-name");

  function selectedRows() {
    return Array.from(document.querySelectorAll(".row-selector:checked"))
      .map((input) => input.closest("tr"))
      .filter(Boolean);
  }

  batchForm?.addEventListener("submit", (event) => {
    if (batchAction?.value !== "edit-metadata") return;
    event.preventDefault();
    const rows = selectedRows();
    if (rows.length !== 1) {
      window.alert("Select exactly one row to edit metadata.");
      return;
    }
    const row = rows[0];
    if (metadataId) metadataId.value = row.dataset.rowId || "";
    if (metadataTitle) metadataTitle.value = row.dataset.title || "";
    if (metadataSource) metadataSource.value = row.dataset.channel || "";
    metadataBackdrop?.classList.add("is-open");
    metadataBackdrop?.setAttribute("aria-hidden", "false");
  });

  metadataForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const response = await fetch(metadataForm.action, {
      method: "POST",
      body: new FormData(metadataForm),
      headers: { "X-CSRFToken": csrf },
    });
    if (!response.ok) {
      window.alert("Failed to update metadata.");
      return;
    }
    window.location.reload();
  });

  const searchInput = document.getElementById("transcript-search-input");
  const searchResults = document.getElementById("transcript-search-results");
  let searchTimer = 0;
  function renderResults(results) {
    if (!searchResults) return;
    if (!results.length) {
      searchResults.innerHTML =
        '<div class="quick-add-empty">No transcript matches found.</div>';
      return;
    }
    searchResults.innerHTML = results
      .map(
        (item) =>
          `<a class="quick-add-result transcript-result" href="${item.url}"><div class="transcript-result-content"><div class="quick-add-meta-title">${escapeHtml(item.title)}</div><div class="quick-add-meta-sub"><span>${escapeHtml(item.source_name)}</span><span>${escapeHtml(item.position_label || formatTime(item.start_seconds))}</span></div><p class="transcript-result-snippet">${escapeHtml(item.text)}</p></div><span class="transcript-result-arrow" aria-hidden="true">›</span></a>`,
      )
      .join("");
  }
  function escapeHtml(value) {
    return String(value || "").replace(
      /[&<>'"]/g,
      (ch) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          "'": "&#39;",
          '"': "&quot;",
        })[ch],
    );
  }
  function formatTime(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
  }
  searchInput?.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    const q = searchInput.value.trim();
    if (q.length < 2) {
      if (searchResults)
        searchResults.innerHTML =
          '<div class="quick-add-empty">Type at least 2 characters.</div>';
      return;
    }
    searchTimer = window.setTimeout(async () => {
      const response = await fetch(
        `/transcript-search/?q=${encodeURIComponent(q)}`,
        { cache: "no-store" },
      );
      renderResults(response.ok ? (await response.json()).results || [] : []);
    }, 180);
  });

  const miniBackdrop = document.getElementById("mini-player-backdrop");
  const miniPlayer = document.getElementById("mini-player");
  const miniTitle = document.getElementById("mini-player-title");
  const miniSource = document.getElementById("mini-player-source");
  const miniOpen = document.getElementById("mini-player-open");
  const miniClose = document.getElementById("mini-player-close");
  const audio = document.getElementById("mini-player-audio");
  const video = document.getElementById("mini-player-video");
  const transcript = document.getElementById("mini-player-transcript");
  const settingsKey = "getofflineMediaElementSettings";
  const stateKey = "getofflineMiniPlayerState";
  let lastPersisted = -9999;
  let lastActiveCue = null;
  let transcriptReady = false;

  function saveMediaSettings(media) {
    localStorage.setItem(
      settingsKey,
      JSON.stringify({ volume: Number(media.volume), muted: !!media.muted }),
    );
  }
  function applyMediaSettings(media) {
    try {
      const stored = JSON.parse(localStorage.getItem(settingsKey) || "null");
      if (stored) {
        if (Number.isFinite(Number(stored.volume)))
          media.volume = Math.max(0, Math.min(1, Number(stored.volume)));
        media.muted = !!stored.muted;
      }
    } catch (_) {}
  }
  function activeMedia(kind) {
    return kind === "video" ? video : audio;
  }
  function progressUrl(rowId) {
    return `/downloads/${rowId}/position/`;
  }
  function postProgress(state, media, force, reason) {
    if (!state?.rowId || !media) return;
    const seconds =
      reason === "mini-ended"
        ? 0
        : Math.max(0, Number(media.currentTime || state.currentTime || 0));
    document
      .querySelectorAll(`a[data-play-link="1"][data-row-id="${state.rowId}"]`)
      .forEach((link) => {
        link.dataset.resumeSeconds = String(seconds);
      });
    if (!force && Math.abs(seconds - lastPersisted) < 5) return;
    lastPersisted = seconds;
    const body = new URLSearchParams();
    body.set("position_seconds", seconds.toFixed(3));
    body.set("reason", reason || "mini-timeupdate");
    const url = progressUrl(state.rowId);
    fetch(url, {
      method: "POST",
      body,
      credentials: "same-origin",
      keepalive: !!force,
      headers: { "X-CSRFToken": csrf },
    }).catch(() => {});
  }
  function clearTranscript() {
    transcriptReady = false;
    lastActiveCue = null;
    if (transcript) {
      transcript.textContent = "";
      transcript.classList.remove("is-visible");
    }
  }
  function buildTranscript(media) {
    if (!transcript || !media?.textTracks?.length) return;
    const track = media.textTracks[0];
    track.mode = "hidden";
    const cues = Array.from(track.cues || []);
    if (!cues.length) return;
    transcript.textContent = "";
    cues.forEach((cue, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "mini-player-transcript-line";
      button.dataset.idx = String(index);
      button.textContent = String(cue.text || "")
        .replace(/\s+/g, " ")
        .trim();
      button.addEventListener("click", () => {
        media.currentTime = Math.max(0, cue.startTime || 0);
        media.play().catch(() => {});
      });
      transcript.appendChild(button);
      cue._goIndex = index;
    });
    track.addEventListener("cuechange", () => {
      const active =
        track.activeCues && track.activeCues.length
          ? track.activeCues[0]
          : null;
      if (active === lastActiveCue) return;
      lastActiveCue = active;
      transcript
        .querySelectorAll(".mini-player-transcript-line")
        .forEach((line, index) => {
          const isActive = cues[index] === active;
          line.classList.toggle("active", isActive);
          if (isActive)
            line.scrollIntoView({ behavior: "smooth", block: "nearest" });
        });
    });
    transcriptReady = true;
    transcript.classList.add("is-visible");
    return true;
  }
  function scheduleTranscriptInit(media) {
    if (!transcript || transcriptReady) return;
    transcript.textContent = "Loading transcript…";
    transcript.classList.add("is-visible");
    let attempts = 0;
    const maxAttempts = 40;
    const timer = window.setInterval(() => {
      attempts += 1;
      if (buildTranscript(media) || attempts >= maxAttempts) {
        window.clearInterval(timer);
        if (!transcriptReady)
          transcript.textContent = "No subtitle cues available.";
      }
    }, 150);
  }
  function stopMini() {
    [audio, video].forEach((el) => {
      if (!el) return;
      el.pause();
      el.removeAttribute("src");
      while (el.firstChild) el.removeChild(el.firstChild);
      el.load();
      el.style.display = "none";
      el.classList.remove("is-active");
      el.onvolumechange = null;
      el.ontimeupdate = null;
      el.onpause = null;
      el.onended = null;
    });
    clearTranscript();
  }
  function setExpanded(expanded) {
    const isExpanded = !!expanded;
    miniPlayer?.classList.toggle("is-maximized", isExpanded);
    miniBackdrop?.classList.toggle("is-open", isExpanded);
    miniBackdrop?.setAttribute("aria-hidden", isExpanded ? "false" : "true");
    if (miniOpen) {
      miniOpen.textContent = isExpanded ? "Minimize" : "Maximize";
      miniOpen.setAttribute(
        "aria-label",
        isExpanded ? "Minimize player" : "Maximize player",
      );
    }
  }
  function closeMini(options = {}) {
    if (!options.skipProgress) {
      try {
        const state = JSON.parse(localStorage.getItem(stateKey) || "null");
        const media = activeMedia(state?.kind);
        postProgress(state, media, true, "mini-close");
      } catch (_) {}
    }
    localStorage.removeItem(stateKey);
    stopMini();
    miniPlayer?.classList.remove("is-visible");
    setExpanded(false);
  }
  function renderMini(state) {
    if (!state?.rowId || !state.src || !miniPlayer) return;
    stopMini();
    const media = activeMedia(state.kind);
    if (!media) return;
    if (miniTitle) miniTitle.textContent = state.title || "Now playing";
    if (miniSource) miniSource.textContent = state.source || "";
    const resumeAtLoad = Math.max(0, Number(state.currentTime || 0));
    media.src = resumeAtLoad > 0 ? `${state.src}#t=${resumeAtLoad.toFixed(3)}` : state.src;
    let miniResumeApplied = !(resumeAtLoad > 0);
    const applyMiniResume = () => {
      if (miniResumeApplied) return true;
      const target = Number.isFinite(media.duration) && media.duration > 1
        ? Math.min(resumeAtLoad, Math.max(media.duration - 1, 0))
        : resumeAtLoad;
      try {
        if (Math.abs(Number(media.currentTime || 0) - target) > 0.75) media.currentTime = target;
        miniResumeApplied = Math.abs(Number(media.currentTime || 0) - target) <= 0.75;
        console.debug('[getoffline] mini resume seek', { rowId: state.rowId, target, currentTime: media.currentTime, applied: miniResumeApplied });
      } catch (err) {
        console.debug('[getoffline] mini resume seek failed', { rowId: state.rowId, target, err });
      }
      return miniResumeApplied;
    };
    if (state.kind !== "video" && state.hasSubtitles && state.subtitleUrl) {
      const track = document.createElement("track");
      track.kind = "subtitles";
      track.srclang = "en";
      track.label = "English";
      track.default = state.kind !== "video";
      track.src = state.subtitleUrl;
      track.addEventListener("load", () => scheduleTranscriptInit(media));
      media.appendChild(track);
      if (state.kind === "video" && track.track) track.track.mode = "hidden";
    }
    media.style.display = "block";
    media.classList.add("is-active");
    applyMediaSettings(media);
    media.addEventListener(
      "loadedmetadata",
      () => {
        applyMediaSettings(media);
        applyMiniResume();
        if (!state.paused) media.play().catch((err) => console.debug("[getoffline] mini autoplay after metadata failed", { rowId: state.rowId, err }));
      },
      { once: true },
    );
    media.addEventListener("loadeddata", () => scheduleTranscriptInit(media), {
      once: true,
    });
    media.addEventListener("canplay", applyMiniResume, { once: true });
    media.addEventListener("playing", applyMiniResume);
    media.ontimeupdate = () => {
      if (!miniResumeApplied && !applyMiniResume()) return;
      state.currentTime = media.currentTime || 0;
      state.paused = media.paused;
      localStorage.setItem(stateKey, JSON.stringify(state));
      if (!media.paused) postProgress(state, media, false, "mini-timeupdate");
    };
    media.onpause = () => {
      if (!miniResumeApplied && !applyMiniResume()) return;
      state.currentTime = media.currentTime || 0;
      state.paused = true;
      localStorage.setItem(stateKey, JSON.stringify(state));
      postProgress(state, media, true, "mini-pause");
    };
    media.onplay = () => {
      applyMediaSettings(media);
      state.paused = false;
      localStorage.setItem(stateKey, JSON.stringify(state));
    };
    media.onvolumechange = () => saveMediaSettings(media);
    media.onended = () => {
      postProgress(state, media, true, "mini-ended");
      try {
        media.currentTime = 0;
      } catch (_) {}
      closeMini({ skipProgress: true });
    };
    media.autoplay = !state.paused;
    media.load();
    if (!state.paused) media.play().catch((err) => console.debug("[getoffline] mini autoplay failed", { rowId: state.rowId, err }));
    miniPlayer.classList.add("is-visible");
    setExpanded(false);
  }
  miniClose?.addEventListener("click", closeMini);
  miniOpen?.addEventListener("click", () =>
    setExpanded(!miniPlayer?.classList.contains("is-maximized")),
  );
  miniBackdrop?.addEventListener("click", (event) => {
    if (event.target === miniBackdrop) closeMini();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && miniBackdrop?.classList.contains("is-open"))
      closeMini();
  });

  document.addEventListener(
    "click",
    (event) => {
      const link = event.target?.closest?.('a[data-play-link="1"]');
      if (!link) return;
      if (link.dataset.kind === "document") return;
      if (
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey ||
        event.button !== 0
      )
        return;
      event.preventDefault();
      event.stopPropagation();
      const row = link.closest("tr");
      const state = {
        rowId: Number(link.dataset.rowId || row?.dataset.rowId || 0),
        title: link.dataset.title || row?.dataset.title || "",
        source: link.dataset.source || row?.dataset.channel || "",
        kind: link.dataset.kind || row?.dataset.kind || "audio",
        hasSubtitles: link.dataset.hasSubtitles === "1",
        subtitleUrl: row?.dataset.subtitleUrl || "",
        src: row?.dataset.mediaUrl || link.href,
        currentTime: Number(
          link.dataset.resumeSeconds || row?.dataset.resumeSeconds || 0,
        ),
        paused: false,
      };
      localStorage.setItem(stateKey, JSON.stringify(state));
      renderMini(state);
    },
    true,
  );
  try {
    const saved = JSON.parse(localStorage.getItem(stateKey) || "null");
    if (saved && saved.paused === false) renderMini(saved);
  } catch (_) {}
})();

(() => {
  function readCookie(name) {
    return (
      document.cookie
        .split(";")
        .map((v) => v.trim())
        .find((v) => v.startsWith(`${name}=`))
        ?.slice(name.length + 1) || ""
    );
  }

  const dropZone = document.body;
  const overlay = document.getElementById("manual-upload-dropzone");
  const status = document.getElementById("manual-upload-status");
  const uploadUrl = overlay?.dataset.uploadUrl || "/manual-upload/";
  const csrf =
    document.querySelector("[name=csrfmiddlewaretoken]")?.value ||
    decodeURIComponent(readCookie("csrftoken") || "");
  const supportedExtensions = new Set([
    "mp3",
    "m4a",
    "wav",
    "flac",
    "aac",
    "ogg",
    "mp4",
    "mkv",
    "webm",
    "mov",
    "pdf",
  ]);
  let dragDepth = 0;

  function hasFiles(event) {
    return Array.from(event.dataTransfer?.types || []).includes("Files");
  }
  function showOverlay() {
    overlay?.classList.add("is-visible");
    overlay?.setAttribute("aria-hidden", "false");
    if (status) status.textContent = "Drop audio, video, or PDF files here to store as manual downloads.";
  }
  function hideOverlay() {
    dragDepth = 0;
    overlay?.classList.remove("is-visible", "is-uploading");
    overlay?.setAttribute("aria-hidden", "true");
  }
  function supportedFiles(fileList) {
    return Array.from(fileList || []).filter((file) => {
      const ext = String(file.name || "").split(".").pop()?.toLowerCase() || "";
      return (
        supportedExtensions.has(ext) ||
        String(file.type || "").startsWith("audio/") ||
        String(file.type || "").startsWith("video/") ||
        String(file.type || "") === "application/pdf"
      );
    });
  }
  async function uploadFiles(files) {
    if (!files.length) {
      if (status) status.textContent = "No supported audio, video, or PDF files were dropped.";
      window.setTimeout(hideOverlay, 1600);
      return;
    }
    overlay?.classList.add("is-uploading");
    if (status) status.textContent = `Uploading ${files.length} manual upload${files.length === 1 ? "" : "s"}…`;
    const body = new FormData();
    files.forEach((file) => body.append("files", file, file.name));
    const response = await fetch(uploadUrl, {
      method: "POST",
      body,
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "X-CSRFToken": csrf,
        "X-Requested-With": "XMLHttpRequest",
      },
    });

    // Try to parse response as JSON, but handle non-JSON responses
    let payload = {};
    let errorText = "";
    try {
      // Clone the response to read the body as text first
      const textResponse = response.clone();
      errorText = await textResponse.text().catch(() => "");
      payload = JSON.parse(errorText);
    } catch (e) {
      // Not valid JSON - errorText contains the raw response
      if (!response.ok) {
        throw new Error(`Server error (${response.status}): ${errorText || response.statusText}`);
      }
      payload = {};
    }

    // Check for HTTP error OR JSON payload with ok: false
    if (!response.ok || (payload && payload.ok === false)) {
      const message =
        payload.error_message ||
        payload.errors?.[0]?.error ||
        payload.errors?.[0] ||
        `HTTP ${response.status}: ${response.statusText}`;
      throw new Error(message);
    }
    if (status) status.textContent = "Upload complete. Refreshing library…";
    window.setTimeout(() => window.location.reload(), 800);
  }

  ["dragenter", "dragover"].forEach((eventName) => {
    dropZone.addEventListener(eventName, (event) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      event.stopPropagation();
      if (eventName === "dragenter") dragDepth += 1;
      event.dataTransfer.dropEffect = "copy";
      showOverlay();
    });
  });
  dropZone.addEventListener("dragleave", (event) => {
    if (!hasFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) hideOverlay();
  });
  dropZone.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.stopPropagation();
    const files = supportedFiles(event.dataTransfer.files);
    uploadFiles(files).catch((error) => {
      const message = error.message || "Upload failed.";
      if (status) status.textContent = message;
      window.alert(`Upload Error: ${message}`);
      window.setTimeout(hideOverlay, 2400);
    });
  });
})();

(() => {
  const panel = document.getElementById("active-pipeline-panel");
  const list = document.getElementById("active-pipeline-list");
  if (!panel || !list) return;
  const statusUrl = panel.dataset.statusUrl;
  if (!statusUrl) return;

  function formatItem(item) {
    const title = item.title || "Untitled download";
    const stage = item.stage_label || "Working";
    const status = item.status || "queued";
    return `${stage}: ${title} (${status})`;
  }

  function render(items) {
    panel.hidden = items.length === 0;
    if (!items.length) {
      list.replaceChildren();
      return;
    }

    const text = items.map(formatItem).join("   •   ");
    const repeats = [text, text].map((value) => {
      const span = document.createElement("span");
      span.className = "active-pipeline-marquee-text";
      span.textContent = value;
      return span;
    });
    list.replaceChildren(...repeats);
    list.style.animationDuration = `${Math.max(18, text.length / 4)}s`;
  }

  async function refresh() {
    try {
      const response = await fetch(statusUrl, {
        cache: "no-store",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("Unable to fetch active jobs.");
      const payload = await response.json();
      render(Array.isArray(payload.items) ? payload.items : []);
    } catch (_) {
      // Keep the last known state visible; polling will retry shortly.
    } finally {
      window.setTimeout(refresh, panel.hidden ? 5000 : 1500);
    }
  }

  refresh();
})();
