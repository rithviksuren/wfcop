const USER_ID = "user_admin";
const CURRENT_USER = {
  id: USER_ID,
  name: "Rithvik Kumar",
  email: "rithvikkumar35@gmail.com",
  role: "admin",
};

const state = {
  view: "workflows",
  workflows: [],
  members: [],
  runsByWorkflow: new Map(),
  selectedWorkflow: null,
  selectedFilter: "all",
  search: "",
};

const els = {
  pageTitle: document.querySelector("#page-title"),
  pageCount: document.querySelector("#page-count"),
  topbarActions: document.querySelector("#topbar-actions"),
  workflowContent: document.querySelector("#workflow-content"),
  historyContent: document.querySelector("#history-content"),
  adminList: document.querySelector("#admin-list"),
  memberList: document.querySelector("#member-list"),
  canvas: document.querySelector("#canvas"),
  runSummary: document.querySelector("#run-summary"),
  detailStatus: document.querySelector("#detail-status"),
  createModal: document.querySelector("#create-modal"),
  createForm: document.querySelector("#create-form"),
  toast: document.querySelector("#toast"),
};

const headers = {
  "Content-Type": "application/json",
  "X-User-Id": USER_ID,
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...headers,
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(Array.isArray(error.detail) ? error.detail.map((item) => item.message).join(", ") : error.detail);
  }
  return response.status === 204 ? null : response.json();
}

function icon(name) {
  const paths = {
    sparkle:
      '<path d="m12 3 1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3ZM5 15l.9 2.1L8 18l-2.1.9L5 21l-.9-2.1L2 18l2.1-.9L5 15Z" />',
    clock: '<path d="M12 8v5l3 2M21 12a9 9 0 1 1-9-9 9 9 0 0 1 9 9Z" />',
    workflow: '<path d="M7 7h4v4H7zM13 13h4v4h-4zM11 9h3a2 2 0 0 1 2 2v2" />',
    play: '<path d="M8 5v14l11-7-11-7Z" />',
  };
  return `<svg viewBox="0 0 24 24">${paths[name]}</svg>`;
}

function showToast(message) {
  els.toast.textContent = message;
  els.toast.classList.remove("hidden");
  window.clearTimeout(showToast.timeout);
  showToast.timeout = window.setTimeout(() => els.toast.classList.add("hidden"), 2800);
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.view === view);
  });
  document.querySelectorAll(".view").forEach((panel) => panel.classList.remove("active"));
  const panel = document.querySelector(`#view-${view}`);
  if (panel) panel.classList.add("active");
  render();
}

function updateHeader() {
  const labels = {
    workflows: ["Workflows", String(filteredWorkflows().length)],
    history: ["Run History", `${totalRuns()} runs`],
    team: ["Team", `${state.members.length} member${state.members.length === 1 ? "" : "s"}`],
    detail: [state.selectedWorkflow?.name || "Workflow", state.selectedWorkflow?.status || "draft"],
  };
  const [title, count] = labels[state.view] || labels.workflows;
  els.pageTitle.textContent = title;
  els.pageCount.textContent = count;
  els.topbarActions.classList.toggle("hidden", state.view !== "workflows");
}

function filteredWorkflows() {
  return state.workflows.filter((workflow) => {
    const statusMatch = state.selectedFilter === "all" || workflow.status === state.selectedFilter;
    const query = state.search.trim().toLowerCase();
    const queryMatch = !query || workflow.name.toLowerCase().includes(query);
    return statusMatch && queryMatch;
  });
}

function totalRuns() {
  let count = 0;
  for (const runs of state.runsByWorkflow.values()) count += runs.length;
  return count;
}

async function loadAll() {
  await ensureCurrentMember();
  const [workflows, members] = await Promise.all([api("/workflows"), api("/team/members")]);
  state.workflows = workflows;
  state.members = members;
  await loadRuns();
  render();
}

async function ensureCurrentMember() {
  try {
    await api("/team/invites", {
      method: "POST",
      body: JSON.stringify(CURRENT_USER),
    });
  } catch (error) {
    console.warn(error);
  }
}

async function loadRuns() {
  state.runsByWorkflow.clear();
  await Promise.all(
    state.workflows.map(async (workflow) => {
      try {
        const runs = await api(`/workflows/${workflow.id}/runs`);
        state.runsByWorkflow.set(workflow.id, runs);
      } catch {
        state.runsByWorkflow.set(workflow.id, []);
      }
    })
  );
}

function render() {
  updateHeader();
  renderWorkflows();
  renderHistory();
  renderTeam();
  renderDetail();
}

function renderWorkflows() {
  if (state.view !== "workflows") return;
  const workflows = filteredWorkflows();
  if (!workflows.length) {
    els.workflowContent.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-inner">
          <div class="empty-icon">${icon("sparkle")}</div>
          <h2>No workflows yet</h2>
          <p>Create your first workflow by describing what you want to automate</p>
          <button class="primary-button" type="button" data-action="open-create">${icon("sparkle")}<span>Create with AI</span></button>
        </div>
      </div>
    `;
    return;
  }

  els.workflowContent.innerHTML = `
    <div class="workflow-grid">
      ${workflows
        .map(
          (workflow) => `
            <button class="workflow-card" type="button" data-workflow-id="${workflow.id}">
              <div class="workflow-meta">
                <span class="status-badge ${workflow.status}">${workflow.status}</span>
                <span class="status-badge">${workflow.mode}</span>
                <span class="status-badge">${workflow.permission === "edit_run" ? "Edit & Run" : "Run only"}</span>
              </div>
              <div>
                <h2>${escapeHtml(workflow.name)}</h2>
                <p>${workflow.node_count} step${workflow.node_count === 1 ? "" : "s"}${
            workflow.trigger_schedule ? ` - ${escapeHtml(workflow.trigger_schedule)}` : ""
          }</p>
              </div>
              <div class="workflow-meta">
                <span class="status-badge ${workflow.last_run_status || ""}">Last run: ${workflow.last_run_status || "none"}</span>
              </div>
            </button>
          `
        )
        .join("")}
    </div>
  `;
}

function renderHistory() {
  if (state.view !== "history") return;
  const entries = [];
  for (const workflow of state.workflows) {
    const runs = state.runsByWorkflow.get(workflow.id) || [];
    for (const run of runs) entries.push({ workflow, run });
  }
  entries.sort((a, b) => new Date(b.run.started_at) - new Date(a.run.started_at));

  if (!entries.length) {
    els.historyContent.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-inner">
          <div class="empty-icon">${icon("clock")}</div>
          <h2>No runs yet</h2>
          <p>Run a workflow to see execution history</p>
        </div>
      </div>
    `;
    return;
  }

  els.historyContent.innerHTML = `
    <div class="run-list">
      ${entries
        .map(
          ({ workflow, run }) => `
            <details class="run-card">
              <summary>${escapeHtml(workflow.name)} - ${formatDate(run.started_at)}</summary>
              <div class="run-card-body">
                <div class="run-meta">
                  <span class="status-badge ${run.status}">${run.status}</span>
                  <span class="status-badge">${run.trigger_type}</span>
                  <span class="status-badge">${run.duration_ms || 0} ms</span>
                </div>
                <p>${escapeHtml(run.summary || "")}</p>
                ${run.steps.map(renderRunStep).join("")}
              </div>
            </details>
          `
        )
        .join("")}
    </div>
  `;
}

function renderTeam() {
  if (state.view !== "team") return;
  const admins = state.members.filter((member) => member.role === "admin");
  const members = state.members.filter((member) => member.role !== "admin");
  els.adminList.innerHTML = admins.map(renderMember).join("") || `<div class="member-card">No admins found</div>`;
  els.memberList.innerHTML = members.map(renderMember).join("") || `<div class="member-card">No members yet</div>`;
}

function renderMember(member) {
  const name = member.name || member.email.split("@")[0];
  const initial = name.trim().slice(0, 1).toUpperCase() || "M";
  return `
    <div class="member-card">
      <div class="member-identity">
        <div class="avatar">${escapeHtml(initial)}</div>
        <div>
          <div class="member-name">${escapeHtml(name)}${member.id === USER_ID ? ' <span class="member-email">(you)</span>' : ""}</div>
          <div class="member-email">${escapeHtml(member.email)}</div>
        </div>
      </div>
      <span class="role-pill ${member.role}">${member.role}</span>
    </div>
  `;
}

function renderDetail() {
  if (state.view !== "detail" || !state.selectedWorkflow) return;
  const workflow = state.selectedWorkflow;
  els.detailStatus.textContent = workflow.status;
  els.detailStatus.className = `count-pill ${workflow.status}`;
  els.canvas.innerHTML = workflow.nodes.map(renderNode).join("");
  const runs = state.runsByWorkflow.get(workflow.id) || [];
  if (!runs.length) {
    els.runSummary.innerHTML = `
      <div class="empty-state-inner">
        <div class="empty-icon">${icon("play")}</div>
        <h2>No run selected</h2>
        <p>Run this workflow to see step-level results</p>
      </div>
    `;
    return;
  }
  const latest = runs[0];
  els.runSummary.innerHTML = `
    <div class="run-meta">
      <span class="status-badge ${latest.status}">${latest.status}</span>
      <span class="status-badge">${latest.duration_ms || 0} ms</span>
    </div>
    <p>${escapeHtml(latest.summary || "")}</p>
    ${latest.steps.map(renderRunStep).join("")}
  `;
}

function renderNode(node, index) {
  return `
    <article class="node-card" data-node-id="${node.id}">
      <div class="node-header">
        <span class="node-type"><span class="node-dot ${node.role}"></span>${node.role}</span>
        <span class="status-badge ${node.status}">${node.status || "idle"}</span>
      </div>
      <div class="node-title editable" contenteditable="true" data-node-field="label">${escapeHtml(
        node.label || `Step ${index + 1}`
      )}</div>
      <div class="node-description editable" contenteditable="true" data-node-field="description">${escapeHtml(
        node.description || ""
      )}</div>
    </article>
  `;
}

function renderRunStep(step) {
  return `
    <div class="run-step">
      <div class="run-step-top">
        <span>${escapeHtml(step.label)}</span>
        <span class="status-badge ${step.status}">${step.status}</span>
      </div>
      <pre>${escapeHtml(JSON.stringify(step.output || step.error || {}, null, 2))}</pre>
    </div>
  `;
}

async function openWorkflow(id) {
  state.selectedWorkflow = await api(`/workflows/${id}`);
  setView("detail");
}

async function createWorkflow(event) {
  event.preventDefault();
  const submit = document.querySelector("#create-submit");
  const prompt = document.querySelector("#workflow-prompt").value.trim();
  const mode = document.querySelector("#workflow-mode").value;
  const schedule = document.querySelector("#workflow-schedule").value;
  if (!prompt) return;

  submit.disabled = true;
  submit.querySelector("span").textContent = "Creating...";
  try {
    const result = await api("/copilot/create", {
      method: "POST",
      body: JSON.stringify({ instruction: prompt, context: {} }),
    });
    let workflow = result.workflow;
    if (mode === "scheduled" || schedule) {
      workflow = await api(`/workflows/${workflow.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          mode,
          trigger_schedule: schedule || "daily at 09:00",
          status: "active",
        }),
      });
    }
    els.createModal.classList.add("hidden");
    await loadAll();
    await openWorkflow(workflow.id);
    showToast("Workflow created");
  } catch (error) {
    showToast(error.message);
  } finally {
    submit.disabled = false;
    submit.querySelector("span").textContent = "Create Workflow";
  }
}

async function saveWorkflowEdits() {
  if (!state.selectedWorkflow) return;
  const nodes = state.selectedWorkflow.nodes.map((node) => {
    const card = els.canvas.querySelector(`[data-node-id="${node.id}"]`);
    return {
      ...node,
      label: card.querySelector('[data-node-field="label"]').textContent.trim(),
      description: card.querySelector('[data-node-field="description"]').textContent.trim(),
    };
  });
  try {
    state.selectedWorkflow = await api(`/workflows/${state.selectedWorkflow.id}`, {
      method: "PATCH",
      body: JSON.stringify({ nodes }),
    });
    await loadAll();
    state.selectedWorkflow = await api(`/workflows/${state.selectedWorkflow.id}`);
    render();
    showToast("Workflow saved");
  } catch (error) {
    showToast(error.message);
  }
}

async function runSelectedWorkflow() {
  if (!state.selectedWorkflow) return;
  try {
    const run = await api(`/workflows/${state.selectedWorkflow.id}/run`, {
      method: "POST",
      body: JSON.stringify({ trigger_type: "manual", input: { source: "FlowMind UI" } }),
    });
    const existing = state.runsByWorkflow.get(state.selectedWorkflow.id) || [];
    state.runsByWorkflow.set(state.selectedWorkflow.id, [run, ...existing]);
    render();
    showToast("Workflow run completed");
  } catch (error) {
    showToast(error.message);
  }
}

async function inviteMember(event) {
  event.preventDefault();
  const email = document.querySelector("#invite-email").value.trim();
  const role = document.querySelector("#invite-role").value;
  if (!email) return;
  try {
    await api("/team/invites", {
      method: "POST",
      body: JSON.stringify({ email, role }),
    });
    document.querySelector("#invite-email").value = "";
    await loadAll();
    showToast("Team member invited");
  } catch (error) {
    showToast(error.message);
  }
}

function formatDate(value) {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

document.querySelectorAll(".nav-item").forEach((item) => {
  item.addEventListener("click", () => setView(item.dataset.view));
});

document.querySelector("#new-workflow-button").addEventListener("click", () => {
  els.createModal.classList.remove("hidden");
  document.querySelector("#workflow-prompt").focus();
});

document.querySelector("#refresh-button").addEventListener("click", () => loadAll().then(() => showToast("Refreshed")));
document.querySelector("#workflow-search").addEventListener("input", (event) => {
  state.search = event.target.value;
  renderWorkflows();
  updateHeader();
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    state.selectedFilter = tab.dataset.filter;
    renderWorkflows();
    updateHeader();
  });
});

document.body.addEventListener("click", (event) => {
  const createAction = event.target.closest('[data-action="open-create"]');
  if (createAction) els.createModal.classList.remove("hidden");
  const workflowCard = event.target.closest("[data-workflow-id]");
  if (workflowCard) openWorkflow(workflowCard.dataset.workflowId);
});

document.querySelector("#close-create").addEventListener("click", () => els.createModal.classList.add("hidden"));
document.querySelector("#cancel-create").addEventListener("click", () => els.createModal.classList.add("hidden"));
document.querySelector("#create-form").addEventListener("submit", createWorkflow);
document.querySelector("#invite-form").addEventListener("submit", inviteMember);
document.querySelector("#back-button").addEventListener("click", () => setView("workflows"));
document.querySelector("#save-workflow-button").addEventListener("click", saveWorkflowEdits);
document.querySelector("#run-now-button").addEventListener("click", runSelectedWorkflow);

loadAll().catch((error) => {
  showToast(error.message);
  render();
});
