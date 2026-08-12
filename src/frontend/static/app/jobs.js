(() => {
  const nav = document.getElementById("jobs-pagination");
  const list = document.querySelector(".jobs-list");
  if (!nav || !list) return;

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>'"]/g, (character) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[
        character
      ],
    );
  }

  function renderPagination(page, totalPages) {
    nav.replaceChildren();
    if (totalPages <= 1) return;
    const addButton = (label, target, disabled = false) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "pagination-button";
      button.textContent = label;
      button.disabled = disabled;
      button.setAttribute("aria-label", `Page ${target}`);
      button.addEventListener("click", () => loadPage(target));
      nav.appendChild(button);
    };
    addButton("‹", page - 1, page <= 1);
    for (let value = Math.max(1, page - 2); value <= Math.min(totalPages, page + 2); value += 1) {
      addButton(String(value), value, value === page);
      if (value === page) nav.lastElementChild.classList.add("is-current");
    }
    addButton("›", page + 1, page >= totalPages);
  }

  function renderJobs(jobs) {
    list.replaceChildren();
    if (!jobs.length) {
      const empty = document.createElement("li");
      empty.className = "empty-job-state";
      empty.textContent = "No jobs yet.";
      list.appendChild(empty);
      return;
    }
    jobs.forEach((job) => {
      const item = document.createElement("li");
      item.className = "job-card";
      const error = job.error_message
        ? `<details class="job-error"><summary>Error log</summary><pre>${escapeHtml(job.error_message)}</pre></details>`
        : "";
      item.innerHTML = `<div class="job-card-main"><span class="job-id">#${escapeHtml(job.id)}</span><strong>${escapeHtml(job.job_type)}</strong>${error}</div><span class="row-status job-status job-status-${escapeHtml(job.status).toLowerCase()}">${escapeHtml(job.status)}</span>`;
      list.appendChild(item);
    });
  }

  async function loadPage(page) {
    if (page < 1) return;
    nav.setAttribute("aria-busy", "true");
    try {
      const url = new URL("/api/frontend/jobs", window.location.origin);
      url.searchParams.set("page", String(page));
      url.searchParams.set("page_size", "100");
      const response = await fetch(url, { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } });
      if (!response.ok) return;
      const payload = await response.json();
      const pagination = payload.pagination || {};
      renderJobs(Array.isArray(payload.jobs) ? payload.jobs : []);
      renderPagination(Number(pagination.page || page), Number(pagination.total_pages || 1));
      window.history.replaceState({}, "", `?page=${Number(pagination.page || page)}`);
    } finally {
      nav.removeAttribute("aria-busy");
    }
  }

  renderPagination(Number(nav.dataset.page || 1), Number(nav.dataset.totalPages || 1));
})();
