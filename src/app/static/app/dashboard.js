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
