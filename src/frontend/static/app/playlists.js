(() => {
  const list = document.getElementById("playlist-list");
  const page = document.querySelector("[data-playlist-id]");
  const playlistId = page?.dataset.playlistId || "";
  const createForm = document.getElementById("create-playlist-form");
  const createInput = document.getElementById("playlist-name");
  const csrf = document.querySelector("[name=csrfmiddlewaretoken]")?.value || "";

  async function request(url, options = {}) {
    const { headers = {}, ...requestOptions } = options;
    const response = await fetch(url, {
      credentials: "same-origin",
      ...requestOptions,
      headers: { Accept: "application/json", "X-CSRFToken": csrf, ...headers },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "Playlist request failed.");
    return payload;
  }

  function render(playlists) {
    list.replaceChildren();
    if (!playlists.length) {
      list.innerHTML = '<div class="empty-state">No playlists yet.</div>';
      return;
    }
    playlists.forEach((playlist) => {
      const card = document.createElement("article");
      card.className = "playlist-card";
      card.innerHTML = `<div><h2></h2><p></p></div><div class="playlist-card-actions"><a class="button-link button-primary" href="/playlists/${playlist.id}/">Open playlist</a><button type="button" data-delete>Delete</button></div>`;
      card.querySelector("h2").textContent = playlist.name;
      card.querySelector("p").textContent = `${playlist.item_count} item${playlist.item_count === 1 ? "" : "s"}`;
      card.querySelector("[data-delete]").addEventListener("click", async () => {
        if (!window.confirm(`Delete playlist “${playlist.name}”?`)) return;
        await request(`/api/playlists/${playlist.id}/delete`, { method: "POST" });
        load();
      });
      list.appendChild(card);
    });
  }

  function renderDetail(payload) {
    const playlist = payload.playlist;
    const items = payload.items || [];
    const playUrl = items.length ? `/play/${items[0].episode.id}/?playlist=${playlist.id}` : "#";
    list.innerHTML = `<section class="playlist-detail-header"><div><a class="back-link" href="/playlists/">← All playlists</a><h2></h2><p></p></div><a class="button-link button-primary" href="${playUrl}">Play all</a></section><section class="playlist-items-panel"><div class="playlist-items-heading"><h3>Videos</h3><span class="playlist-items-count"></span></div><ol class="playlist-items"></ol></section>`;
    list.querySelector("h2").textContent = playlist.name;
    list.querySelector("p").textContent = `${items.length} item${items.length === 1 ? "" : "s"} · Plays in this order`;
    list.querySelector(".playlist-items-count").textContent = `${items.length} total`;
    const ol = list.querySelector("ol");
    if (!items.length) { ol.innerHTML = '<li class="empty-state">Add videos from the library to start this playlist.</li>'; return; }
    items.forEach((item) => {
      const li = document.createElement("li");
      li.innerHTML = '<span class="playlist-item-position"></span><div class="playlist-item-main"><a class="episode-link"></a><span class="playlist-item-meta"></span></div><div class="playlist-item-actions"><a class="playlist-play-link" aria-label="Play item">▶</a><button type="button" class="playlist-remove-button">Remove</button></div>';
      li.querySelector(".playlist-item-position").textContent = String(item.position + 1).padStart(2, "0");
      const link = li.querySelector(".episode-link");
      link.href = `/play/${item.episode.id}/?playlist=${playlist.id}`;
      link.textContent = item.episode.title;
      const meta = li.querySelector(".playlist-item-meta");
      meta.textContent = `${item.episode.source_name || item.episode.source_type || "Media"} · ${item.episode.download_status || "ready"}`;
      li.querySelector(".playlist-play-link").href = link.href;
      li.querySelector(".playlist-remove-button").addEventListener("click", async () => {
        const title = item.episode.title || "this video";
        if (!window.confirm(`Remove “${title}” from this playlist?`)) return;
        await request(`/api/playlists/${playlist.id}/items/${item.episode.id}/remove`, { method: "POST" });
        loadDetail();
      });
      ol.appendChild(li);
    });
  }

  async function load() {
    try { render((await request("/api/playlists")).playlists || []); }
    catch (error) { list.textContent = error.message; }
  }

  async function loadDetail() {
    try { renderDetail(await request(`/api/playlists/${playlistId}`)); }
    catch (error) { list.textContent = error.message; }
  }

  createForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await request("/api/playlists/create", { method: "POST", body: JSON.stringify({ name: createInput.value }), headers: { "Content-Type": "application/json" } });
      createInput.value = "";
      load();
    } catch (error) { window.alert(error.message); }
  });
  if (playlistId) loadDetail(); else load();
})();
