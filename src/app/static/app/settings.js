(() => {
  const target = document.getElementById("android_sync_target");
  const directoryFields = document.getElementById("directory-transfer-fields");
  const androidFields = document.getElementById("android-transfer-fields");
  function transferVisibility() {
    const isDirectory = target?.value === "directory";
    directoryFields?.classList.toggle("is-hidden", !isDirectory);
    androidFields?.classList.toggle("is-hidden", isDirectory);
  }
  target?.addEventListener("change", transferVisibility);
  transferVisibility();
})();
