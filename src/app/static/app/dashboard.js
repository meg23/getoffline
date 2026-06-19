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

  const backdrop = document.getElementById('library-player-backdrop');
  const modal = document.getElementById('library-player-modal');
  const playerTitle = document.getElementById('library-player-title');
  const playerSource = document.getElementById('library-player-source');
  const playerDescription = document.getElementById('library-player-description');
  const toggleSize = document.getElementById('library-player-toggle-size');
  const audio = document.getElementById('mini-player-audio');
  const video = document.getElementById('mini-player-video');

  function stopLibraryPlayer() {
    [audio, video].forEach((el) => {
      if (!el) return;
      el.pause();
      el.removeAttribute('src');
      el.load();
      el.classList.remove('is-active');
      el.style.display = 'none';
    });
  }

  function closeLibraryPlayer() {
    stopLibraryPlayer();
    backdrop?.classList.remove('is-open');
    backdrop?.setAttribute('aria-hidden', 'true');
    if (backdrop) backdrop.style.display = 'none';
    modal?.classList.remove('is-minimized');
  }

  function openLibraryPlayer(row, link, minimized = false) {
    const target = row?.dataset.kind === 'video' ? video : audio;
    const other = target === video ? audio : video;
    if (!target || !row?.dataset.mediaUrl) return;
    stopLibraryPlayer();
    other?.classList.remove('is-active');
    target.src = row.dataset.mediaUrl;
    target.style.display = 'block';
    target.classList.add('is-active');
    if (playerTitle) playerTitle.textContent = row.dataset.title || link?.dataset.title || 'Media';
    if (playerSource) playerSource.textContent = row.dataset.channel || '';
    if (playerDescription) playerDescription.textContent = link?.dataset.summary || '';
    modal?.classList.toggle('is-minimized', minimized);
    if (toggleSize) toggleSize.textContent = minimized ? 'Maximize' : 'Minimize';
    if (backdrop) {
      backdrop.style.display = 'flex';
      backdrop.classList.add('is-open');
      backdrop.setAttribute('aria-hidden', 'false');
    }
    target.play().catch(() => {});
  }

  document.getElementById('library-player-close')?.addEventListener('click', closeLibraryPlayer);
  toggleSize?.addEventListener('click', () => {
    const minimized = !modal?.classList.contains('is-minimized');
    modal?.classList.toggle('is-minimized', minimized);
    if (toggleSize) toggleSize.textContent = minimized ? 'Maximize' : 'Minimize';
  });
  backdrop?.addEventListener('click', (event) => { if (event.target === backdrop) closeLibraryPlayer(); });

  document.querySelectorAll('a[data-play-link="1"]').forEach((link) => {
    link.addEventListener('click', (event) => {
      if (event.metaKey || event.ctrlKey || event.shiftKey) return;
      event.preventDefault();
      openLibraryPlayer(link.closest('tr'), link, event.altKey);
    });
  });
})();
