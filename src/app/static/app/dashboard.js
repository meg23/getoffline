(() => {
  const rows = Array.from(document.querySelectorAll('#downloads-table-body tr[data-row-id]'));
  const filterInput = document.getElementById('library-filter');
  const filterMode = document.getElementById('library-filter-mode');
  const clearButton = document.getElementById('library-filter-clear');
  const selectAll = document.getElementById('select-all');
  const batchAction = document.getElementById('batch-action');
  const batchApply = document.getElementById('batch-apply');
  const selectors = () => rows.map((row) => row.querySelector('.row-selector')).filter(Boolean);

  const summaryTooltip = document.getElementById('summary-tooltip');

  function bindSummaryTooltips() {
    const hideSummaryTooltip = () => {
      if (!summaryTooltip) return;
      summaryTooltip.classList.remove('is-visible');
      summaryTooltip.setAttribute('aria-hidden', 'true');
    };
    const placeSummaryTooltip = (event) => {
      if (!summaryTooltip) return;
      const pad = 12;
      const rect = summaryTooltip.getBoundingClientRect();
      let x = event.clientX + 14;
      let y = event.clientY + 14;
      if (x + rect.width > window.innerWidth - pad) x = Math.max(pad, event.clientX - rect.width - 14);
      if (y + rect.height > window.innerHeight - pad) y = Math.max(pad, event.clientY - rect.height - 14);
      summaryTooltip.style.left = `${x}px`;
      summaryTooltip.style.top = `${y}px`;
    };
    document.querySelectorAll('a[data-play-link="1"]').forEach((link) => {
      if (link.dataset.summaryBound === '1') return;
      link.dataset.summaryBound = '1';
      link.addEventListener('mouseenter', (event) => {
        if (!summaryTooltip) return;
        const summaryText = String(link.dataset.summary || link.dataset.title || '').trim();
        if (!summaryText) return;
        summaryTooltip.textContent = summaryText;
        summaryTooltip.classList.add('is-visible');
        summaryTooltip.setAttribute('aria-hidden', 'false');
        placeSummaryTooltip(event);
      });
      link.addEventListener('mousemove', placeSummaryTooltip);
      link.addEventListener('mouseleave', hideSummaryTooltip);
      link.addEventListener('blur', hideSummaryTooltip);
    });
  }

  const visibleRows = () => rows.filter((row) => row.style.display !== 'none');

  function updateSummary() {
    const visible = visibleRows();
    const played = visible.filter((row) => row.dataset.played === '1').length;
    const favorites = visible.filter((row) => row.dataset.favorite === '1').length;
    const setText = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = String(value); };
    setText('summary-visible-items', visible.length);
    setText('summary-played-items', played);
    setText('summary-new-items', Math.max(visible.length - played, 0));
    setText('summary-favorite-items', favorites);
  }

  function applyFilters() {
    const term = (filterInput?.value || '').trim().toLowerCase();
    const mode = filterMode?.value || 'unplayed';
    rows.forEach((row) => {
      const text = `${row.dataset.channel || ''} ${row.dataset.title || ''}`.toLowerCase();
      const matchesText = !term || text.includes(term);
      const played = row.dataset.played === '1';
      const favorite = row.dataset.favorite === '1';
      const matchesMode = mode === 'all' || (mode === 'played' && played) || (mode === 'favorites' && favorite) || (mode === 'unplayed' && !played);
      row.style.display = matchesText && matchesMode ? '' : 'none';
    });
    updateSummary();
    updateBatchState();
  }

  function updateBatchState() {
    const selected = visibleRows().some((row) => row.querySelector('.row-selector')?.checked);
    if (batchApply) batchApply.disabled = !selected || !batchAction?.value;
    if (selectAll) {
      const visible = visibleRows();
      const checked = visible.filter((row) => row.querySelector('.row-selector')?.checked).length;
      selectAll.checked = visible.length > 0 && checked === visible.length;
      selectAll.indeterminate = checked > 0 && checked < visible.length;
    }
  }

  filterInput?.addEventListener('input', applyFilters);
  filterMode?.addEventListener('change', applyFilters);
  clearButton?.addEventListener('click', () => { if (filterInput) filterInput.value = ''; if (filterMode) filterMode.value = 'unplayed'; applyFilters(); });
  selectors().forEach((input) => input.addEventListener('change', updateBatchState));
  batchAction?.addEventListener('change', updateBatchState);
  selectAll?.addEventListener('change', () => { visibleRows().forEach((row) => { const input = row.querySelector('.row-selector'); if (input) input.checked = selectAll.checked; }); updateBatchState(); });

  function openModal(id) { const el = document.getElementById(id); if (el) { el.classList.add('is-open'); el.setAttribute('aria-hidden', 'false'); } }
  function closeModals() { document.querySelectorAll('.modal-backdrop').forEach((el) => { el.classList.remove('is-open'); el.setAttribute('aria-hidden', 'true'); }); }
  document.getElementById('quick-add-open')?.addEventListener('click', () => openModal('quick-add-backdrop'));
  document.getElementById('quick-add-open-hero')?.addEventListener('click', () => openModal('quick-add-backdrop'));
  document.getElementById('transcript-search-open')?.addEventListener('click', () => openModal('transcript-search-backdrop'));
  document.querySelectorAll('[data-modal-close]').forEach((button) => button.addEventListener('click', closeModals));
  document.querySelectorAll('.modal-backdrop').forEach((backdrop) => backdrop.addEventListener('click', (event) => { if (event.target === backdrop) closeModals(); }));
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeModals(); });

  bindSummaryTooltips();
  applyFilters();
})();

(() => {
  const csrf = document.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
  const batchForm = document.getElementById('batch-form');
  const batchAction = document.getElementById('batch-action');
  const metadataBackdrop = document.getElementById('metadata-edit-backdrop');
  const metadataForm = document.getElementById('metadata-edit-form');
  const metadataId = document.getElementById('metadata-edit-id');
  const metadataTitle = document.getElementById('metadata-edit-item-title');
  const metadataSource = document.getElementById('metadata-edit-source-name');

  function selectedRows() {
    return Array.from(document.querySelectorAll('.row-selector:checked')).map((input) => input.closest('tr')).filter(Boolean);
  }

  batchForm?.addEventListener('submit', (event) => {
    if (batchAction?.value !== 'edit-metadata') return;
    event.preventDefault();
    const rows = selectedRows();
    if (rows.length !== 1) {
      window.alert('Select exactly one row to edit metadata.');
      return;
    }
    const row = rows[0];
    if (metadataId) metadataId.value = row.dataset.rowId || '';
    if (metadataTitle) metadataTitle.value = row.dataset.title || '';
    if (metadataSource) metadataSource.value = row.dataset.channel || '';
    metadataBackdrop?.classList.add('is-open');
    metadataBackdrop?.setAttribute('aria-hidden', 'false');
  });

  metadataForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const response = await fetch(metadataForm.action, { method: 'POST', body: new FormData(metadataForm), headers: { 'X-CSRFToken': csrf } });
    if (!response.ok) {
      window.alert('Failed to update metadata.');
      return;
    }
    window.location.reload();
  });

  const searchInput = document.getElementById('transcript-search-input');
  const searchResults = document.getElementById('transcript-search-results');
  let searchTimer = 0;
  function renderResults(results) {
    if (!searchResults) return;
    if (!results.length) {
      searchResults.innerHTML = '<div class="quick-add-empty">No transcript matches found.</div>';
      return;
    }
    searchResults.innerHTML = results.map((item) => `<a class="quick-add-result" href="${item.url}"><div class="quick-add-meta-title">${escapeHtml(item.title)}</div><div class="quick-add-meta-sub">${escapeHtml(item.source_name)} · ${formatTime(item.start_seconds)}</div><div>${escapeHtml(item.text)}</div></a>`).join('');
  }
  function escapeHtml(value) { return String(value || '').replace(/[&<>'"]/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch])); }
  function formatTime(seconds) { const total = Math.max(0, Math.floor(Number(seconds) || 0)); const m = Math.floor(total / 60); const s = total % 60; return `${m}:${String(s).padStart(2, '0')}`; }
  searchInput?.addEventListener('input', () => {
    window.clearTimeout(searchTimer);
    const q = searchInput.value.trim();
    if (q.length < 2) { if (searchResults) searchResults.innerHTML = '<div class="quick-add-empty">Type at least 2 characters.</div>'; return; }
    searchTimer = window.setTimeout(async () => {
      const response = await fetch(`/transcript-search/?q=${encodeURIComponent(q)}`, { cache: 'no-store' });
      renderResults(response.ok ? (await response.json()).results || [] : []);
    }, 180);
  });


  const miniBackdrop = document.getElementById('mini-player-backdrop');
  const miniPlayer = document.getElementById('mini-player');
  const miniTitle = document.getElementById('mini-player-title');
  const miniSource = document.getElementById('mini-player-source');
  const miniOpen = document.getElementById('mini-player-open');
  const miniClose = document.getElementById('mini-player-close');
  const audio = document.getElementById('mini-player-audio');
  const video = document.getElementById('mini-player-video');
  const transcript = document.getElementById('mini-player-transcript');
  const settingsKey = 'getofflineMediaElementSettings';
  const stateKey = 'getofflineMiniPlayerState';
  let lastPersisted = -9999;

  function saveMediaSettings(media) { localStorage.setItem(settingsKey, JSON.stringify({ volume: media.volume, muted: media.muted })); }
  function applyMediaSettings(media) { try { const stored = JSON.parse(localStorage.getItem(settingsKey) || 'null'); if (stored) { if (Number.isFinite(Number(stored.volume))) media.volume = Math.max(0, Math.min(1, Number(stored.volume))); media.muted = !!stored.muted; } } catch (_) {} }
  function activeMedia(kind) { return kind === 'video' ? video : audio; }
  function progressUrl(rowId) { return `/downloads/${rowId}/position/`; }
  function postProgress(state, media, force, reason) {
    if (!state?.rowId || !media) return;
    const seconds = reason === 'mini-ended' ? 0 : Math.max(0, Number(media.currentTime || state.currentTime || 0));
    document.querySelectorAll(`a[data-play-link="1"][data-row-id="${state.rowId}"]`).forEach((link) => { link.dataset.resumeSeconds = String(seconds); });
    if (!force && Math.abs(seconds - lastPersisted) < 5) return;
    lastPersisted = seconds;
    const body = new URLSearchParams();
    body.set('position_seconds', seconds.toFixed(3));
    body.set('reason', reason || 'mini-timeupdate');
    const url = progressUrl(state.rowId);
    if (force && navigator.sendBeacon) {
      const blob = new Blob([body.toString()], { type: 'application/x-www-form-urlencoded' });
      if (navigator.sendBeacon(url, blob)) return;
    }
    fetch(url, { method: 'POST', body, headers: { 'X-CSRFToken': csrf } }).catch(() => {});
  }
  function clearTranscript() { if (transcript) { transcript.textContent = ''; transcript.classList.remove('is-visible'); } }
  function buildTranscript(media) {
    clearTranscript();
    if (!transcript || !media?.textTracks?.length) return;
    const track = media.textTracks[0];
    track.mode = 'hidden';
    const cues = Array.from(track.cues || []);
    if (!cues.length) return;
    cues.forEach((cue, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'mini-player-transcript-line';
      button.textContent = String(cue.text || '').replace(/\s+/g, ' ').trim();
      button.addEventListener('click', () => { media.currentTime = Math.max(0, cue.startTime || 0); media.play().catch(() => {}); });
      transcript.appendChild(button);
      cue._goIndex = index;
    });
    track.addEventListener('cuechange', () => {
      const active = track.activeCues?.[0];
      transcript.querySelectorAll('.mini-player-transcript-line').forEach((line, index) => line.classList.toggle('active', cues[index] === active));
    });
    transcript.classList.add('is-visible');
  }
  function stopMini() {
    [audio, video].forEach((el) => { if (!el) return; el.pause(); el.removeAttribute('src'); while (el.firstChild) el.removeChild(el.firstChild); el.load(); el.style.display = 'none'; el.classList.remove('is-active'); el.onvolumechange = null; el.ontimeupdate = null; el.onpause = null; el.onended = null; });
    clearTranscript();
  }
  function setExpanded(expanded) { miniPlayer?.classList.toggle('is-maximized', expanded); miniBackdrop?.classList.toggle('is-open', expanded); miniBackdrop?.setAttribute('aria-hidden', expanded ? 'false' : 'true'); if (miniOpen) miniOpen.textContent = expanded ? 'Minimize' : 'Maximize'; }
  function closeMini() { try { const state = JSON.parse(localStorage.getItem(stateKey) || 'null'); const media = activeMedia(state?.kind); postProgress(state, media, true, 'mini-close'); } catch (_) {} localStorage.removeItem(stateKey); stopMini(); miniPlayer?.classList.remove('is-visible'); setExpanded(false); }
  function renderMini(state) {
    if (!state?.rowId || !state.src || !miniPlayer) return;
    stopMini();
    const media = activeMedia(state.kind);
    if (!media) return;
    if (miniTitle) miniTitle.textContent = state.title || 'Now playing';
    if (miniSource) miniSource.textContent = state.source || '';
    const source = document.createElement('source'); source.src = state.src; media.appendChild(source);
    if (state.hasSubtitles && state.subtitleUrl) { const track = document.createElement('track'); track.kind = 'subtitles'; track.srclang = 'en'; track.label = 'English'; track.default = true; track.src = state.subtitleUrl; track.addEventListener('load', () => buildTranscript(media)); media.appendChild(track); }
    media.style.display = 'block'; media.classList.add('is-active'); applyMediaSettings(media);
    media.addEventListener('loadedmetadata', () => { const resume = Math.max(0, Number(state.currentTime || 0)); if (resume && (!media.duration || resume < media.duration)) media.currentTime = resume; if (!state.paused) media.play().catch(() => {}); }, { once: true });
    media.ontimeupdate = () => { state.currentTime = media.currentTime || 0; state.paused = media.paused; localStorage.setItem(stateKey, JSON.stringify(state)); if (!media.paused) postProgress(state, media, false, 'mini-timeupdate'); };
    media.onpause = () => { state.currentTime = media.currentTime || 0; state.paused = true; localStorage.setItem(stateKey, JSON.stringify(state)); postProgress(state, media, true, 'mini-pause'); };
    media.onplay = () => { state.paused = false; localStorage.setItem(stateKey, JSON.stringify(state)); };
    media.onvolumechange = () => saveMediaSettings(media);
    media.onended = () => { postProgress(state, media, true, 'mini-ended'); closeMini(); };
    media.load();
    miniPlayer.classList.add('is-visible'); setExpanded(false);
  }
  miniClose?.addEventListener('click', closeMini);
  miniOpen?.addEventListener('click', () => setExpanded(!miniPlayer?.classList.contains('is-maximized')));
  miniBackdrop?.addEventListener('click', (event) => { if (event.target === miniBackdrop) closeMini(); });

  document.querySelectorAll('a[data-play-link="1"]').forEach((link) => {
    link.addEventListener('click', (event) => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
      event.preventDefault();
      const row = link.closest('tr');
      const state = { rowId: Number(link.dataset.rowId || row?.dataset.rowId || 0), title: link.dataset.title || row?.dataset.title || '', source: link.dataset.source || row?.dataset.channel || '', kind: link.dataset.kind || row?.dataset.kind || 'audio', hasSubtitles: link.dataset.hasSubtitles === '1', subtitleUrl: row?.dataset.subtitleUrl || '', src: row?.dataset.mediaUrl || link.href, currentTime: Number(link.dataset.resumeSeconds || row?.dataset.resumeSeconds || 0), paused: false };
      localStorage.setItem(stateKey, JSON.stringify(state));
      renderMini(state);
    });
  });
  try { const saved = JSON.parse(localStorage.getItem(stateKey) || 'null'); if (saved && saved.paused === false) renderMini(saved); } catch (_) {}
})();
