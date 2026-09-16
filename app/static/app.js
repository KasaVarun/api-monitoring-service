const REFRESH_MS = 5000;

const state = {
  endpoints: [],
  alerts: [],
  selectedId: null,
  page: 1,
  pageSize: 10,
  historyTotal: 0,
  pendingDeleteId: null,
  timer: null,
};

const els = {
  refreshState: document.getElementById("refresh-state"),
  refreshBtn: document.getElementById("refresh-btn"),
  registerForm: document.getElementById("register-form"),
  formError: document.getElementById("form-error"),
  listError: document.getElementById("list-error"),
  listEmpty: document.getElementById("list-empty"),
  tableWrap: document.getElementById("table-wrap"),
  rows: document.getElementById("endpoint-rows"),
  alertsError: document.getElementById("alerts-error"),
  alertsEmpty: document.getElementById("alerts-empty"),
  alertsList: document.getElementById("alerts-list"),
  detailPanel: document.getElementById("detail-panel"),
  detailTitle: document.getElementById("detail-title"),
  detailMeta: document.getElementById("detail-meta"),
  uptimeValue: document.getElementById("uptime-value"),
  uptimeCount: document.getElementById("uptime-count"),
  detailConsecutive: document.getElementById("detail-consecutive"),
  detailError: document.getElementById("detail-error"),
  historyRows: document.getElementById("history-rows"),
  prevPage: document.getElementById("prev-page"),
  nextPage: document.getElementById("next-page"),
  pageLabel: document.getElementById("page-label"),
  closeDetail: document.getElementById("close-detail"),
  editDialog: document.getElementById("edit-dialog"),
  editForm: document.getElementById("edit-form"),
  editError: document.getElementById("edit-error"),
  editCancel: document.getElementById("edit-cancel"),
  deleteDialog: document.getElementById("delete-dialog"),
  deleteConfirm: document.getElementById("delete-confirm"),
};

function apiError(payload, fallback) {
  if (!payload) return fallback;
  if (typeof payload.message === "string") return payload.message;
  if (typeof payload.detail === "string") return payload.detail;
  return fallback;
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (response.status === 204) return null;
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { message: text };
    }
  }
  if (!response.ok) {
    throw new Error(apiError(data, `Request failed (${response.status})`));
  }
  return data;
}

function formatLatency(ms) {
  return ms === null || ms === undefined ? "—" : `${ms.toFixed(1)} ms`;
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z");
}

function statusBadge(status) {
  const cls = (status || "UNKNOWN").toLowerCase();
  return `<span class="badge ${cls}">${status || "UNKNOWN"}</span>`;
}

function show(el, visible) {
  el.classList.toggle("hidden", !visible);
}

function setBanner(el, message) {
  if (!message) {
    show(el, false);
    el.textContent = "";
    return;
  }
  el.textContent = message;
  show(el, true);
}

function renderRows() {
  if (!state.endpoints.length) {
    show(els.listEmpty, true);
    show(els.tableWrap, false);
    els.rows.innerHTML = "";
    return;
  }
  show(els.listEmpty, false);
  show(els.tableWrap, true);
  els.rows.innerHTML = state.endpoints
    .map((item) => {
      const checking = item._checking ? "busy" : "";
      return `<tr data-id="${item.id}" class="endpoint-row ${checking}">
        <td data-label="Name" class="cell-name"><strong>${escapeHtml(item.name)}</strong></td>
        <td data-label="URL" class="cell-url"><code class="url-text">${escapeHtml(item.url)}</code></td>
        <td data-label="Status" class="cell-status">${statusBadge(item.availability)}</td>
        <td data-label="HTTP" class="cell-http">${item.last_status_code ?? "—"}</td>
        <td data-label="Latency" class="cell-latency">${formatLatency(item.last_response_time_ms)}</td>
        <td data-label="Last check (UTC)" class="cell-checked">${formatTime(item.last_checked_at)}</td>
        <td data-label="Failures" class="cell-failures">${item.total_failures} total / ${item.consecutive_failures} streak</td>
        <td data-label="Actions" class="cell-actions">
          <div class="row-actions">
            <button type="button" data-action="details">Details</button>
            <button type="button" data-action="check">Check</button>
            <button type="button" data-action="toggle">${item.enabled ? "Disable" : "Enable"}</button>
            <button type="button" data-action="edit">Edit</button>
            <button type="button" data-action="delete" class="danger">Delete</button>
          </div>
        </td>
      </tr>`;
    })
    .join("");
}

function renderAlerts() {
  if (!state.alerts.length) {
    show(els.alertsEmpty, true);
    show(els.alertsList, false);
    els.alertsList.innerHTML = "";
    return;
  }
  show(els.alertsEmpty, false);
  show(els.alertsList, true);
  els.alertsList.innerHTML = state.alerts
    .map((item) => {
      const kind = item.kind.toLowerCase();
      const delivery = item.webhook_delivered === true
        ? " · webhook delivered"
        : item.webhook_delivered === false
          ? ` · webhook failed: ${escapeHtml(item.webhook_error || "unknown error")}`
          : "";
      return `<article class="alert-item ${kind}">
        <div>
          <span class="badge alert-${kind}">${escapeHtml(item.kind)}</span>
          <strong>${escapeHtml(item.endpoint_name)}</strong>
        </div>
        <p>${escapeHtml(item.message)}</p>
        <small>${formatTime(item.created_at)}${delivery}</small>
      </article>`;
    })
    .join("");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function loadEndpoints() {
  els.refreshState.textContent = "Refreshing…";
  try {
    state.endpoints = await request("/api/endpoints");
    setBanner(els.listError, "");
    renderRows();
    els.refreshState.textContent = `Updated ${new Date().toISOString().slice(11, 19)} UTC`;
    if (state.selectedId) await loadDetails();
  } catch (error) {
    setBanner(els.listError, error.message);
    els.refreshState.textContent = "Refresh failed";
  }
}

async function loadAlerts() {
  try {
    state.alerts = await request("/api/alerts?limit=20");
    setBanner(els.alertsError, "");
    renderAlerts();
  } catch (error) {
    setBanner(els.alertsError, error.message);
  }
}

async function loadAll() {
  await Promise.all([loadEndpoints(), loadAlerts()]);
}

async function loadDetails() {
  if (!state.selectedId) {
    show(els.detailPanel, false);
    return;
  }
  try {
    const detail = await request(`/api/endpoints/${state.selectedId}`);
    const history = await request(
      `/api/endpoints/${state.selectedId}/checks?page=${state.page}&page_size=${state.pageSize}`
    );
    els.detailTitle.textContent = detail.name;
    els.detailMeta.textContent = `${detail.url} · every ${detail.interval_seconds}s · ${detail.enabled ? "enabled" : "disabled"}`;
    els.uptimeValue.textContent =
      detail.uptime.uptime_percent === null ? "n/a" : `${detail.uptime.uptime_percent}%`;
    els.uptimeCount.textContent = String(detail.uptime.total_checks);
    els.detailConsecutive.textContent = String(detail.consecutive_failures);
    state.historyTotal = history.total;
    const pages = Math.max(1, Math.ceil(history.total / state.pageSize));
    els.pageLabel.textContent = `Page ${history.page} of ${pages}`;
    els.prevPage.disabled = history.page <= 1;
    els.nextPage.disabled = history.page >= pages;
    els.historyRows.innerHTML = history.items.length
      ? history.items
          .map(
            (item) => `<tr>
              <td data-label="Checked at (UTC)">${formatTime(item.checked_at)}</td>
              <td data-label="Status">${statusBadge(item.availability)}</td>
              <td data-label="HTTP">${item.status_code ?? "—"}</td>
              <td data-label="Latency">${formatLatency(item.response_time_ms)}</td>
              <td data-label="Error">${escapeHtml(item.error_message || "—")}</td>
            </tr>`
          )
          .join("")
      : `<tr><td colspan="5">No checks recorded yet.</td></tr>`;
    setBanner(els.detailError, "");
    show(els.detailPanel, true);
  } catch (error) {
    setBanner(els.detailError, error.message);
    show(els.detailPanel, true);
  }
}

els.registerForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(els.registerForm);
  const intervalRaw = String(form.get("interval_seconds") || "").trim();
  const payload = {
    name: String(form.get("name") || "").trim(),
    url: String(form.get("url") || "").trim(),
    enabled: form.get("enabled") === "on",
  };
  if (intervalRaw) payload.interval_seconds = Number(intervalRaw);
  try {
    await request("/api/endpoints", { method: "POST", body: JSON.stringify(payload) });
    els.registerForm.reset();
    els.registerForm.elements.enabled.checked = true;
    setBanner(els.formError, "");
    await loadAll();
  } catch (error) {
    setBanner(els.formError, error.message);
  }
});

els.rows.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  const row = button.closest("[data-id]");
  const id = row?.dataset.id;
  if (!id) return;
  const action = button.dataset.action;
  const endpoint = state.endpoints.find((item) => item.id === id);
  if (action === "details") {
    state.selectedId = id;
    state.page = 1;
    history.replaceState({}, "", `/endpoints/${id}`);
    await loadDetails();
  } else if (action === "toggle") {
    try {
      await request(`/api/endpoints/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ enabled: !endpoint.enabled }),
      });
      await loadAll();
    } catch (error) {
      setBanner(els.listError, error.message);
    }
  } else if (action === "check") {
    endpoint._checking = true;
    renderRows();
    try {
      await request(`/api/endpoints/${id}/check`, { method: "POST" });
      setBanner(els.listError, "");
    } catch (error) {
      setBanner(els.listError, error.message);
    } finally {
      endpoint._checking = false;
      await loadAll();
    }
  } else if (action === "edit") {
    els.editForm.elements.id.value = endpoint.id;
    els.editForm.elements.name.value = endpoint.name;
    els.editForm.elements.url.value = endpoint.url;
    els.editForm.elements.interval_seconds.value = endpoint.interval_seconds;
    setBanner(els.editError, "");
    els.editDialog.showModal();
  } else if (action === "delete") {
    state.pendingDeleteId = id;
    els.deleteDialog.showModal();
  }
});

els.editCancel.addEventListener("click", () => els.editDialog.close());

els.editForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const id = els.editForm.elements.id.value;
  const payload = {
    name: els.editForm.elements.name.value.trim(),
    url: els.editForm.elements.url.value.trim(),
    interval_seconds: Number(els.editForm.elements.interval_seconds.value),
  };
  try {
    await request(`/api/endpoints/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
    els.editDialog.close();
    await loadAll();
  } catch (error) {
    setBanner(els.editError, error.message);
  }
});

els.deleteConfirm.addEventListener("click", async (event) => {
  event.preventDefault();
  const id = state.pendingDeleteId;
  els.deleteDialog.close();
  if (!id) return;
  try {
    await request(`/api/endpoints/${id}`, { method: "DELETE" });
    if (state.selectedId === id) {
      state.selectedId = null;
      history.replaceState({}, "", "/");
      show(els.detailPanel, false);
    }
    await loadEndpoints();
  } catch (error) {
    setBanner(els.listError, error.message);
  }
});

els.closeDetail.addEventListener("click", () => {
  state.selectedId = null;
  history.replaceState({}, "", "/");
  show(els.detailPanel, false);
});

els.prevPage.addEventListener("click", async () => {
  if (state.page > 1) {
    state.page -= 1;
    await loadDetails();
  }
});

els.nextPage.addEventListener("click", async () => {
  state.page += 1;
  await loadDetails();
});

els.refreshBtn.addEventListener("click", loadAll);

function startTimer() {
  if (state.timer) clearInterval(state.timer);
  state.timer = setInterval(loadAll, REFRESH_MS);
}

function bootFromPath() {
  const match = window.location.pathname.match(/^\/endpoints\/([^/]+)$/);
  if (match) {
    state.selectedId = match[1];
  }
}

bootFromPath();
loadAll();
startTimer();
