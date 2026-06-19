(() => {
  const target = document.getElementById('android_sync_target');
  const directoryFields = document.getElementById('directory-sync-fields');
  const androidFields = document.getElementById('android-sync-fields');
  function syncVisibility() {
    const isDirectory = target?.value === 'directory';
    directoryFields?.classList.toggle('is-hidden', !isDirectory);
    androidFields?.classList.toggle('is-hidden', isDirectory);
  }
  target?.addEventListener('change', syncVisibility);
  syncVisibility();
})();
