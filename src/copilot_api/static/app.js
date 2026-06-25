let CURRENT_USER = null;

const state = {
  view: "workflows",
  workflows: [],
  members: [],
  integrations: [],
  tasks: [],
  runsByWorkflow: new Map(),
  selectedWorkflow: null,
  selectedFilter: "all",
  search: "",
  pendingAnalysis: null,
  pendingPlanSession: null,
  selectedRecommendationId: null,
};

const els = {
  pageTitle: document.querySelector("#page-title"),
  pageCount: document.querySelector("#page-count"),
  topbarActions: document.querySelector("#topbar-actions"),
  workflowContent: document.querySelector("#workflow-content"),
  historyContent: document.querySelector("#history-content"),
  tasksContent: document.querySelector("#tasks-content"),
  adminList: document.querySelector("#admin-list"),
  memberList: document.querySelector("#member-list"),
  integrationGrid: document.querySelector("#integration-grid"),
  canvas: document.querySelector("#canvas"),
  runSummary: document.querySelector("#run-summary"),
  detailStatus: document.querySelector("#detail-status"),
  createModal: document.querySelector("#create-modal"),
  createForm: document.querySelector("#create-form"),
  analysisReview: document.querySelector("#analysis-review"),
  clarificationPanel: document.querySelector("#clarification-panel"),
  clarificationList: document.querySelector("#clarification-list"),
  analysisRequestEditor: document.querySelector("#analysis-request-editor"),
  workflowRefineForm: document.querySelector("#workflow-refine-form"),
  workflowRefinePrompt: document.querySelector("#workflow-refine-prompt"),
  workflowRefineSubmit: document.querySelector("#workflow-refine-submit"),
  workflowRefineResult: document.querySelector("#workflow-refine-result"),
  toast: document.querySelector("#toast"),
  loginScreen: document.querySelector("#login-screen"),
  appShell: document.querySelector("#app-shell"),
  googleSigninButton: document.querySelector("#google-signin-button"),
  loginSetup: document.querySelector("#login-setup"),
  loginError: document.querySelector("#login-error"),
  userMenu: document.querySelector("#user-menu"),
  userName: document.querySelector("#user-name"),
  userEmail: document.querySelector("#user-email"),
  userAvatarImage: document.querySelector("#user-avatar-image"),
  userAvatarFallback: document.querySelector("#user-avatar-fallback"),
};

const headers = {
  "Content-Type": "application/json",
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
    trash:
      '<path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 11v5M14 11v5" />',
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
    tasks: ["Tasks", `${state.tasks.length} task${state.tasks.length === 1 ? "" : "s"}`],
    team: ["Team", `${state.members.length} member${state.members.length === 1 ? "" : "s"}`],
    integrations: [
      "Integrations",
      `${state.integrations.filter((item) => item.connected).length} connected`,
    ],
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
  const [workflows, members, integrations, tasks] = await Promise.all([
    api("/workflows"),
    api("/team/members"),
    api("/integrations"),
    api("/tasks"),
  ]);
  state.workflows = workflows;
  state.members = members;
  state.integrations = integrations;
  state.tasks = tasks;
  await loadRuns();
  render();
}

async function initializeApp() {
  const auth = await api("/auth/me");
  if (!auth.authenticated) {
    showLogin(auth);
    return;
  }
  CURRENT_USER = auth.user;
  showAuthenticatedApp();
  await loadAll();
}

function showLogin(auth) {
  els.loginScreen.classList.remove("hidden");
  els.appShell.classList.add("hidden");
  els.googleSigninButton.classList.toggle("disabled", !auth.google_configured);
  els.googleSigninButton.setAttribute("aria-disabled", String(!auth.google_configured));
  if (!auth.google_configured) {
    els.googleSigninButton.removeAttribute("href");
    els.loginSetup.textContent = auth.setup_message;
    els.loginSetup.classList.remove("hidden");
  }
  const error = new URLSearchParams(window.location.search).get("auth_error");
  if (error) {
    const messages = {
      google_not_configured: "Google sign-in is not configured yet.",
      invalid_state: "That sign-in attempt expired. Please try again.",
      access_denied: "Google sign-in was cancelled.",
      google_sign_in_failed: "Google sign-in could not be completed. Please try again.",
    };
    els.loginError.textContent = messages[error] || "Sign-in could not be completed.";
    els.loginError.classList.remove("hidden");
    window.history.replaceState({}, "", window.location.pathname);
  }
}

function showAuthenticatedApp() {
  els.loginScreen.classList.add("hidden");
  els.appShell.classList.remove("hidden");
  els.userMenu.classList.remove("hidden");
  els.userName.textContent = CURRENT_USER.name || CURRENT_USER.email.split("@")[0];
  els.userEmail.textContent = CURRENT_USER.email;
  els.userAvatarFallback.textContent = (CURRENT_USER.name || CURRENT_USER.email).slice(0, 1).toUpperCase();
  if (CURRENT_USER.picture) {
    els.userAvatarImage.src = CURRENT_USER.picture;
    els.userAvatarImage.classList.remove("hidden");
    els.userAvatarFallback.classList.add("hidden");
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
  renderTasks();
  renderTeam();
  renderIntegrations();
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
            <article class="workflow-card">
              <button class="workflow-card-open" type="button" data-workflow-id="${workflow.id}" aria-label="Open ${escapeHtml(
            workflow.name
          )}">
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
                  <span class="status-badge ${workflow.last_run_status || ""}">Last run: ${
            workflow.last_run_status || "none"
          }</span>
                </div>
              </button>
              ${
                workflow.permission === "edit_run"
                  ? `<button class="workflow-delete-button" type="button" data-delete-workflow-id="${
                      workflow.id
                    }" title="Delete workflow" aria-label="Delete ${escapeHtml(workflow.name)}">${icon("trash")}</button>`
                  : ""
              }
            </article>
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
        .map(({ workflow, run }) => {
          const outcome = describeRunOutcome(run);
          return `
            <details class="run-card">
              <summary>${escapeHtml(workflow.name)} - ${formatDate(run.started_at)}</summary>
              <div class="run-card-body">
                <div class="run-meta">
                  <span class="status-badge ${outcome.className}">${escapeHtml(outcome.label)}</span>
                  <span class="status-badge">${run.trigger_type}</span>
                  <span class="status-badge">${formatDuration(run.duration_ms)}</span>
                </div>
                <p>${escapeHtml(outcome.message)}</p>
                ${run.steps.map(renderRunStep).join("")}
              </div>
            </details>
          `;
        })
        .join("")}
    </div>
  `;
}

function renderTasks() {
  if (state.view !== "tasks") return;
  if (!state.tasks.length) {
    els.tasksContent.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-inner">
          <div class="empty-icon">${icon("workflow")}</div>
          <h2>No tasks created yet</h2>
          <p>Tasks created by your workflows will appear here.</p>
        </div>
      </div>
    `;
    return;
  }
  const workflowNames = new Map(state.workflows.map((workflow) => [workflow.id, workflow.name]));
  els.tasksContent.innerHTML = `
    <div class="task-page">
      <div class="task-page-intro">
        <div>
          <h2>Workflow tasks</h2>
          <p>These tasks are stored in FlowMind's local team task list.</p>
        </div>
      </div>
      <div class="task-list">
        ${state.tasks
          .map(
            (task) => `
              <article class="task-item">
                <div class="task-check">✓</div>
                <div class="task-item-copy">
                  <h3>${escapeHtml(task.title)}</h3>
                  <p>${escapeHtml(workflowNames.get(task.workflow_id) || "Workflow")} · ${formatDate(
                    task.created_at
                  )}</p>
                </div>
                <div class="task-item-meta">
                  <span class="status-badge ${task.status === "completed" ? "success" : "draft"}">${
                    task.status
                  }</span>
                  <span class="status-badge">${escapeHtml(task.list_id)}</span>
                </div>
              </article>
            `
          )
          .join("")}
      </div>
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
          <div class="member-name">${escapeHtml(name)}${member.id === CURRENT_USER?.id ? ' <span class="member-email">(you)</span>' : ""}</div>
          <div class="member-email">${escapeHtml(member.email)}</div>
        </div>
      </div>
      <span class="role-pill ${member.role}">${member.role}</span>
    </div>
  `;
}

const integrationDefinitions = {
  gmail: {
    logo: "https://api.iconify.design/logos:google-gmail.svg",
    description: "Read matching unread messages from your Gmail inbox.",
    help: "Your normal Google password will not work. Turn on 2-Step Verification, then create a 16-character app password.",
    helpUrl: "https://myaccount.google.com/apppasswords",
    helpLink: "Create a Google app password",
    fields: [
      { name: "email", label: "Gmail address", type: "email", placeholder: "you@gmail.com" },
      {
        name: "app_password",
        label: "Google app password",
        type: "password",
        placeholder: "16-character app password - not your account password",
      },
    ],
  },
  slack: {
    logo: "https://api.iconify.design/logos:slack-icon.svg",
    description: "Send workflow messages to a Slack channel.",
    help: "Create an incoming webhook in your Slack app settings.",
    fields: [
      { name: "webhook_url", label: "Incoming webhook URL", type: "password", placeholder: "https://hooks.slack.com/services/..." },
    ],
  },
  teams: {
    logo: "https://api.iconify.design/logos:microsoft-teams.svg",
    description: "Post workflow messages to Microsoft Teams.",
    help: "Use a Teams workflow or incoming webhook URL.",
    fields: [
      { name: "webhook_url", label: "Webhook URL", type: "password", placeholder: "https://..." },
    ],
  },
  notion: {
    logo: "https://api.iconify.design/logos:notion-icon.svg",
    description: "Create pages in a Notion database.",
    help: "Share the database with your integration, then open Manage data sources and copy the data source ID.",
    fields: [
      { name: "api_token", label: "Integration token", type: "password", placeholder: "secret_..." },
      { name: "data_source_id", label: "Data source ID", type: "text", placeholder: "UUID or Notion data source URL" },
      { name: "title_property", label: "Title property", type: "text", placeholder: "Name" },
    ],
  },
  jira: {
    logo: "https://api.iconify.design/logos:jira.svg",
    description: "Create and track work in a Jira Cloud project.",
    help: "Use your Jira Cloud URL, account email, and an Atlassian API token.",
    fields: [
      { name: "base_url", label: "Jira site URL", type: "url", placeholder: "https://company.atlassian.net" },
      { name: "email", label: "Jira account email", type: "email", placeholder: "you@company.com" },
      { name: "api_token", label: "API token", type: "password", placeholder: "Atlassian API token" },
      { name: "project_key", label: "Default project key", type: "text", placeholder: "LEADS" },
    ],
  },
  hubspot: {
    logo: "https://api.iconify.design/logos:hubspot.svg",
    description: "Create or update customer contacts in HubSpot CRM.",
    help: "Create a HubSpot private app with CRM contact write access.",
    fields: [
      {
        name: "private_app_token",
        label: "Private app token",
        type: "password",
        placeholder: "pat-na1-...",
      },
    ],
  },
  google_calendar: {
    logo: "https://api.iconify.design/logos:google-calendar.svg",
    description: "Trigger workflows from upcoming Google Calendar events.",
    help: "Create a Google Cloud service account, enable Calendar API access, and share the calendar with its service account email.",
    helpUrl: "https://console.cloud.google.com/iam-admin/serviceaccounts",
    helpLink: "Open Google Cloud service accounts",
    fields: [
      {
        name: "service_account_json",
        label: "Service account JSON",
        type: "password",
        placeholder: "Paste the full service account JSON",
      },
      { name: "calendar_id", label: "Calendar ID", type: "text", placeholder: "primary or calendar ID" },
    ],
  },
  google_drive: {
    logo: "https://api.iconify.design/logos:google-drive.svg",
    description: "Create, find, and organize files in Google Drive.",
    help: "Enable the Google Drive API and share the target folder with your service account.",
    fields: [
      {
        name: "service_account_json",
        label: "Service account JSON",
        type: "password",
        placeholder: "Paste the full service account JSON",
      },
      { name: "folder_id", label: "Default folder ID", type: "text", placeholder: "Google Drive folder ID" },
    ],
  },
  google_sheets: {
    logo: "https://api.iconify.design/simple-icons:googlesheets.svg?color=%2334A853",
    description: "Read rows and append workflow results to Google Sheets.",
    help: "Enable the Google Sheets API and share the spreadsheet with your service account.",
    fields: [
      {
        name: "service_account_json",
        label: "Service account JSON",
        type: "password",
        placeholder: "Paste the full service account JSON",
      },
      { name: "spreadsheet_id", label: "Spreadsheet ID", type: "text", placeholder: "Google Sheets spreadsheet ID" },
    ],
  },
  github: {
    logo: "https://api.iconify.design/logos:github-icon.svg",
    description: "Create issues and automate repository workflows in GitHub.",
    help: "Use a fine-grained personal access token with access to the selected repository.",
    fields: [
      {
        name: "personal_access_token",
        label: "Personal access token",
        type: "password",
        placeholder: "github_pat_...",
      },
      { name: "repository", label: "Repository", type: "text", placeholder: "owner/repository" },
    ],
  },
  discord: {
    logo: "https://api.iconify.design/logos:discord-icon.svg",
    description: "Send workflow notifications to a Discord channel.",
    help: "Create a webhook from the target channel's Integrations settings.",
    fields: [
      { name: "webhook_url", label: "Webhook URL", type: "password", placeholder: "https://discord.com/api/webhooks/..." },
    ],
  },
  airtable: {
    logo: "https://api.iconify.design/logos:airtable.svg",
    description: "Create and update records in an Airtable base.",
    help: "Create a personal access token with record read and write scopes.",
    fields: [
      {
        name: "personal_access_token",
        label: "Personal access token",
        type: "password",
        placeholder: "pat...",
      },
      { name: "base_id", label: "Base ID", type: "text", placeholder: "app..." },
      { name: "table_name", label: "Default table", type: "text", placeholder: "Leads" },
    ],
  },
  stripe: {
    logo: "https://api.iconify.design/logos:stripe.svg",
    description: "React to payments and manage Stripe customer workflows.",
    help: "Use a restricted API key with only the permissions your workflows need.",
    fields: [
      { name: "secret_key", label: "Restricted API key", type: "password", placeholder: "rk_live_..." },
    ],
  },
  salesforce: {
    logo: "https://api.iconify.design/logos:salesforce.svg",
    description: "Create and update leads, contacts, and opportunities in Salesforce.",
    help: "Provide your Salesforce instance URL and an OAuth access token.",
    fields: [
      { name: "instance_url", label: "Instance URL", type: "url", placeholder: "https://your-domain.my.salesforce.com" },
      { name: "access_token", label: "Access token", type: "password", placeholder: "Salesforce OAuth access token" },
    ],
  },
};

function renderIntegrations() {
  if (state.view !== "integrations") return;
  els.integrationGrid.innerHTML = state.integrations
    .map((integration) => {
      const definition = integrationDefinitions[integration.provider];
      const fields = definition.fields
        .map((field) => {
          const saved = integration.configured_fields.includes(field.name);
          const value = field.type === "password" ? "" : integration.values[field.name] || "";
          const placeholder =
            field.type === "password" && saved ? "Saved - enter a new value to replace" : field.placeholder;
          return `
            <label class="integration-field">
              <span>${escapeHtml(field.label)}${saved ? ' <small>saved</small>' : ""}</span>
              <input
                name="${field.name}"
                type="${field.type}"
                value="${escapeHtml(value)}"
                placeholder="${escapeHtml(placeholder)}"
                autocomplete="off"
              />
            </label>
          `;
        })
        .join("");
      return `
        <form class="integration-card" data-integration-form="${integration.provider}">
          <div class="integration-card-header">
            <div class="integration-logo">
              <span class="integration-logo-fallback">${escapeHtml(integration.name.slice(0, 1))}</span>
              <img
                src="${escapeHtml(definition.logo)}"
                alt="${escapeHtml(integration.name)} logo"
                loading="lazy"
                onerror="this.hidden=true"
              />
            </div>
            <div>
              <div class="integration-title-row">
                <h3>${escapeHtml(integration.name)}</h3>
                <span class="status-badge ${integration.connected ? "success" : ""}">
                  ${integration.connected ? "Connected" : "Not connected"}
                </span>
              </div>
              <p>${escapeHtml(definition.description)}</p>
            </div>
          </div>
          <div class="integration-fields">${fields}</div>
          <p class="integration-help">
            ${escapeHtml(definition.help)}
            ${
              definition.helpUrl
                ? `<a href="${definition.helpUrl}" target="_blank" rel="noreferrer">${escapeHtml(
                    definition.helpLink
                  )}</a>`
                : ""
            }
          </p>
          <div class="integration-actions">
            ${
              integration.connected
                ? `<button class="danger-button" type="button" data-disconnect-integration="${integration.provider}">Disconnect</button>`
                : "<span></span>"
            }
            <button class="primary-button" type="submit"><span>Save connection</span></button>
          </div>
        </form>
      `;
    })
    .join("");
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
  const outcome = describeRunOutcome(latest);
  els.runSummary.innerHTML = `
    <div class="run-meta">
      <span class="status-badge ${outcome.className}">${escapeHtml(outcome.label)}</span>
      <span class="status-badge">${formatDuration(latest.duration_ms)}</span>
    </div>
    <p class="run-outcome-message">${escapeHtml(outcome.message)}</p>
    ${latest.steps.map(renderRunStep).join("")}
  `;
}

function operationSummary(operation) {
  const labels = {
    add_node: "Added",
    update_node: "Updated",
    remove_node: "Removed",
    connect_nodes: "Connected",
    disconnect_nodes: "Disconnected",
    update_workflow: "Updated workflow",
  };
  const subject =
    operation.node?.label ||
    operation.node?.type?.replaceAll("_", " ") ||
    operation.node_id ||
    operation.reason ||
    "workflow";
  return `${labels[operation.op] || "Changed"} ${subject}`;
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
  const presentation = describeRunStep(step);
  return `
    <div class="run-step ${presentation.className}">
      <div class="run-step-top">
        <span>${escapeHtml(step.label)}</span>
        <span class="status-badge ${presentation.badgeClass}">${escapeHtml(presentation.badge)}</span>
      </div>
      <div class="run-step-summary">
        <strong>${escapeHtml(presentation.title)}</strong>
        ${presentation.description ? `<p>${escapeHtml(presentation.description)}</p>` : ""}
        ${
          presentation.details.length
            ? `<dl class="run-step-details">${presentation.details
                .map(
                  ([label, value]) =>
                    `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`
                )
                .join("")}</dl>`
            : ""
        }
      </div>
    </div>
  `;
}

function describeRunOutcome(run) {
  if (run.status === "failed") {
    return {
      label: "Needs attention",
      className: "failed",
      message: friendlyError(run.summary || "The workflow could not finish."),
    };
  }
  if (run.steps.some((step) => step.output?.condition?.matched === false)) {
    return {
      label: "No match",
      className: "paused",
      message: "The email was checked, but it did not meet the filter. No task was created.",
    };
  }
  if (run.steps.some((step) => step.output?.triggered === false)) {
    return {
      label: "Nothing new",
      className: "paused",
      message: "No new item matched the workflow trigger.",
    };
  }
  return {
    label: "Completed",
    className: "success",
    message: run.summary || "The workflow completed successfully.",
  };
}

function describeRunStep(step) {
  if (step.status === "failed") {
    return {
      className: "failed",
      badge: "Needs attention",
      badgeClass: "failed",
      title: "This step could not be completed",
      description: friendlyError(step.error),
      details: [],
    };
  }

  const output = step.output || {};
  switch (output.node_type) {
    case "gmail_trigger": {
      if (output.triggered === false) {
        return {
          className: "no-match",
          badge: "No new email",
          badgeClass: "paused",
          title: "No matching email was found",
          description: output.reason || "There were no unread emails that matched this trigger.",
          details: [],
        };
      }
      return {
        className: "success",
        badge: "Email found",
        badgeClass: "success",
        title: "Found a matching email",
        description: cleanPreview(output.body),
        details: compactDetails([
          ["From", readableSender(output.from)],
          ["Subject", output.subject],
          ["Received", formatEmailDate(output.date)],
        ]),
      };
    }
    case "filter_condition": {
      const condition = output.condition || {};
      const matched = condition.matched !== false;
      return {
        className: matched ? "success" : "no-match",
        badge: matched ? "Matched" : "No match",
        badgeClass: matched ? "success" : "paused",
        title: matched ? "The email matched your filter" : "The email did not match your filter",
        description: matched
          ? "The workflow continued to the next step."
          : "The workflow stopped here, so no action was taken.",
        details: compactDetails([
          ["Checked", humanizeField(condition.field)],
          ["Email value", readableValue(condition.actual, 90)],
          ["Required value", readableValue(condition.expected, 60)],
        ]),
      };
    }
    case "task_create": {
      const task = output.created_task || {};
      return {
        className: "success",
        badge: "Task created",
        badgeClass: "success",
        title: task.title ? `Created “${task.title}”` : "Created a new task",
        description: "The task was added to your team task list.",
        details: compactDetails([["List", task.list_id], ["Status", task.status]]),
      };
    }
    case "reminder_create": {
      const reminder = output.created_reminder || {};
      return {
        className: "success",
        badge: "Reminder created",
        badgeClass: "success",
        title: reminder.title || "Created a reminder",
        description: "The reminder is ready.",
        details: [],
      };
    }
    case "slack_message":
    case "teams_message": {
      const message = output.sent_message || {};
      return {
        className: "success",
        badge: "Message sent",
        badgeClass: "success",
        title: `Sent a message to ${message.provider === "teams" ? "Microsoft Teams" : "Slack"}`,
        description: cleanPreview(message.text),
        details: [],
      };
    }
    case "notion_create_page": {
      const page = output.created_page || {};
      return {
        className: "success",
        badge: "Page created",
        badgeClass: "success",
        title: page.title ? `Created “${page.title}”` : "Created a Notion page",
        description: "The page was added to your connected Notion database.",
        details: [],
      };
    }
    case "form_submission_trigger":
      return {
        className: "success",
        badge: "Form received",
        badgeClass: "success",
        title: "Received a customer form submission",
        description: "The submitted customer details started the workflow.",
        details: compactDetails([["Email", output.email], ["Name", output.name]]),
      };
    case "jira_ticket_create": {
      const ticket = output.created_jira_ticket || {};
      return {
        className: "success",
        badge: "Ticket created",
        badgeClass: "success",
        title: ticket.key ? `Created Jira ticket ${ticket.key}` : "Created a Jira ticket",
        description: ticket.summary || "",
        details: [],
      };
    }
    case "crm_update": {
      const crm = output.updated_crm || {};
      return {
        className: "success",
        badge: "CRM updated",
        badgeClass: "success",
        title: "Updated the HubSpot contact",
        description: crm.email || "",
        details: [],
      };
    }
    case "email_send": {
      const sent = output.sent_email || {};
      return {
        className: "success",
        badge: "Email sent",
        badgeClass: "success",
        title: `Sent a follow-up email${sent.to ? ` to ${sent.to}` : ""}`,
        description: sent.subject || "",
        details: [],
      };
    }
    case "calendar_event_trigger":
      return {
        className: "success",
        badge: "Event found",
        badgeClass: "success",
        title: output.title || output.summary || "Found a calendar event",
        description: "The calendar event started the workflow.",
        details: compactDetails([["Starts", output.start]]),
      };
    case "webhook":
      return {
        className: "success",
        badge: "Request received",
        badgeClass: "success",
        title: "Received the webhook request",
        description: "The request started the workflow.",
        details: compactDetails([["Path", output.path]]),
      };
    default:
      return {
        className: "success",
        badge: "Completed",
        badgeClass: "success",
        title: "Step completed",
        description: "This step finished successfully.",
        details: [],
      };
  }
}

function compactDetails(items) {
  return items.filter(([, value]) => value !== undefined && value !== null && String(value).trim() !== "");
}

function cleanPreview(value, limit = 180) {
  if (!value) return "";
  const clean = String(value)
    .replace(/https?:\/\/\S+/gi, "")
    .replace(/\s+/g, " ")
    .trim();
  if (!clean) return "";
  return clean.length > limit ? `${clean.slice(0, limit).trim()}…` : clean;
}

function readableSender(value) {
  if (!value) return "";
  return String(value).replace(/^"+|"+$/g, "").replace(/\s*<([^>]+)>/, " ($1)");
}

function readableValue(value, limit = 100) {
  if (value === undefined || value === null || value === "") return "Not provided";
  const text = Array.isArray(value) ? (value.length ? value.join(", ") : "None") : String(value);
  const clean = text
    .replace(/https?:\/\/\S+/gi, "[link]")
    .replace(/\s+/g, " ")
    .trim();
  return clean.length > limit ? `${clean.slice(0, limit).trim()}…` : clean;
}

function humanizeField(value) {
  if (!value) return "Email information";
  return String(value).replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatEmailDate(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : formatDate(date);
}

function formatDuration(milliseconds) {
  const value = Number(milliseconds || 0);
  return value >= 1000 ? `${(value / 1000).toFixed(1)} sec` : `${value} ms`;
}

function friendlyError(error) {
  if (!error) return "Please review this step's configuration and try again.";
  const message = String(error);
  if (/application-specific password|required.*app password|invalid credentials/i.test(message)) {
    return "Google rejected the Gmail login. Open Integrations and reconnect Gmail with a Google app password.";
  }
  if (/(?:http 404|object_not_found|could not find data_source)[\s\S]*(?:data_source|notion)|notion[\s\S]*(?:http 404|object_not_found|could not find data_source)/i.test(message)) {
    return "Notion cannot access the selected data source. Open the database in Notion, choose ••• → Connections, and add the same Notion integration whose token is saved in FlowMind. If the database is in another workspace, use an integration token from that workspace. Then verify the Data source ID under Integrations → Notion and run the workflow again.";
  }
  if (/notion[\s\S]*(?:http 401|unauthorized)/i.test(message)) {
    return "The saved Notion integration token is invalid or expired. Save the correct internal integration secret under Integrations → Notion and retry.";
  }
  return message
    .replace(/^Workflow failed at step \d+:\s*/i, "")
    .replace(/\s+/g, " ")
    .trim();
}

async function openWorkflow(id) {
  state.selectedWorkflow = await api(`/workflows/${id}`);
  setView("detail");
}

async function deleteWorkflow(id) {
  const workflow = state.workflows.find((item) => item.id === id);
  if (!workflow) return;
  if (!window.confirm(`Delete "${workflow.name}"? This cannot be undone.`)) return;

  try {
    await api(`/workflows/${id}`, { method: "DELETE" });
    state.workflows = state.workflows.filter((item) => item.id !== id);
    state.runsByWorkflow.delete(id);
    if (state.selectedWorkflow?.id === id) state.selectedWorkflow = null;
    render();
    showToast("Workflow deleted");
  } catch (error) {
    showToast(error.message);
  }
}

async function createWorkflow(event) {
  event.preventDefault();
  await advanceWorkflowPlan();
}

async function advanceWorkflowPlan() {
  const submit = document.querySelector("#create-submit");
  const prompt = document.querySelector("#workflow-prompt").value.trim();
  const mode = document.querySelector("#workflow-mode").value;
  const schedule = document.querySelector("#workflow-schedule").value;
  if (!prompt) return;

  submit.disabled = true;
  submit.querySelector("span").textContent = state.pendingAnalysis
    ? "Building..."
    : state.pendingPlanSession
    ? "Continuing..."
    : "Planning...";
  try {
    if (!state.pendingAnalysis) {
      let result;
      if (state.pendingPlanSession) {
        const answers = Object.fromEntries(
          [...els.clarificationList.querySelectorAll("[data-question-id]")].map((input) => [
            input.dataset.questionId,
            input.value.trim(),
          ])
        );
        result = await api(`/copilot/plans/${state.pendingPlanSession.id}/answers`, {
          method: "POST",
          body: JSON.stringify({ answers }),
        });
      } else {
        result = await api("/copilot/plans", {
          method: "POST",
          body: JSON.stringify({ instruction: prompt, context: {} }),
        });
      }
      state.pendingPlanSession = result.session;
      if (!result.analysis) {
        renderClarifyingQuestions(result.session);
        return;
      }
      state.pendingAnalysis = result.analysis;
      state.selectedRecommendationId = state.pendingAnalysis.recommendations[0]?.id || null;
      renderWorkflowAnalysis(state.pendingAnalysis);
      submit.querySelector("span").textContent = "Build Workflow";
      return;
    }

    const proposed = structuredClone(state.pendingAnalysis.proposed_workflow);
    if (mode === "scheduled" || schedule) {
      proposed.mode = "scheduled";
      proposed.trigger_schedule = schedule || proposed.trigger_schedule || "daily at 09:00";
      proposed.status = "active";
    }
    const workflow = await api("/copilot/build", {
      method: "POST",
      body: JSON.stringify({
        instruction: prompt,
        workflow: proposed,
        selected_recommendation_id: state.selectedRecommendationId,
      }),
    });
    els.createModal.classList.add("hidden");
    resetWorkflowAnalysis();
    await loadAll();
    await openWorkflow(workflow.id);
    showToast("Workflow built from the approved plan");
  } catch (error) {
    showToast(error.message);
  } finally {
    submit.disabled = Boolean(state.pendingAnalysis?.unsupported_tasks?.length);
    submit.querySelector("span").textContent = state.pendingAnalysis
      ? "Build Workflow"
      : state.pendingPlanSession?.status === "awaiting_clarification"
      ? "Continue planning"
      : "Plan workflow";
  }
}

function renderClarifyingQuestions(session) {
  document.querySelector("#workflow-input-panel").classList.add("hidden");
  document.querySelector("#workflow-options").classList.add("hidden");
  els.analysisReview.classList.add("hidden");
  els.clarificationPanel.classList.remove("hidden");
  document.querySelector("#edit-analysis").classList.remove("hidden");
  els.clarificationList.innerHTML = session.questions
    .map((question) => {
      const choices = question.choices || [];
      const input = choices.length
        ? `<select data-question-id="${escapeHtml(question.id)}" required>
            <option value="">Choose an answer</option>
            ${choices
              .map((choice) => `<option value="${escapeHtml(choice)}">${escapeHtml(choice)}</option>`)
              .join("")}
          </select>`
        : `<input data-question-id="${escapeHtml(question.id)}" type="text" required />`;
      return `
        <label class="clarification-question">
          <strong>${escapeHtml(question.question)}</strong>
          <small>${escapeHtml(question.reason)}</small>
          ${input}
        </label>
      `;
    })
    .join("");
  document.querySelector("#create-submit span").textContent = "Continue planning";
}

function renderWorkflowAnalysis(analysis) {
  document.querySelector("#workflow-input-panel").classList.add("hidden");
  document.querySelector("#workflow-options").classList.remove("hidden");
  els.clarificationPanel.classList.add("hidden");
  els.analysisReview.classList.remove("hidden");
  document.querySelector("#edit-analysis").classList.remove("hidden");
  els.analysisRequestEditor.value = analysis.instruction;
  document.querySelector("#analysis-trigger").textContent = analysis.extracted.trigger;
  document.querySelector("#analysis-goal").textContent = analysis.extracted.goal;
  document.querySelector("#analysis-tasks").innerHTML = analysis.extracted.tasks
    .map((task, index) => `<div><span>${index + 1}</span><p>${escapeHtml(task)}</p></div>`)
    .join("");

  const intent = analysis.intent;
  document.querySelector("#intent-grid").innerHTML = [
    ["Industry", intent.industry],
    ["Workflow type", intent.workflow_type.replaceAll("_", " ")],
    ["Priority", intent.priority],
    ["Apps", intent.apps.join(", ") || "No external apps"],
  ]
    .map(
      ([label, value]) =>
        `<div><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong></div>`
    )
    .join("");

  document.querySelector("#recommendation-list").innerHTML = analysis.recommendations
    .map(
      (recommendation, index) => `
        <div class="recommendation-card ${index === 0 ? "selected" : ""}">
          <div>
            <div class="recommendation-top">
              <strong>${escapeHtml(recommendation.name)}</strong>
              <span>${index === 0 ? "Best match · " : ""}${Math.round(
                recommendation.match_score * 100
              )}%</span>
            </div>
            <p>${escapeHtml(recommendation.description)}</p>
            <small>${escapeHtml(recommendation.reason)}</small>
            ${
              recommendation.steps?.length
                ? `<ol>${recommendation.steps
                    .map((step) => `<li>${escapeHtml(step)}</li>`)
                    .join("")}</ol>`
                : ""
            }
          </div>
        </div>
      `
    )
    .join("");

  document.querySelector("#proposed-step-list").innerHTML = analysis.proposed_workflow.nodes
    .map(
      (node, index) => `
        <div class="proposed-step">
          <span>${index + 1}</span>
          <div>
            <strong>${escapeHtml(node.label || node.type.replaceAll("_", " "))}</strong>
            <p>${escapeHtml(node.description || "")}</p>
          </div>
        </div>
      `
    )
    .join("");

  const warning = document.querySelector("#analysis-integrations");
  const unsupported = analysis.unsupported_tasks || [];
  const warnings = analysis.planning_warnings || [];
  if (analysis.missing_integrations.length || unsupported.length || warnings.length) {
    const messages = [];
    if (unsupported.length) {
      messages.push(
        `<strong>Cannot build unsupported actions</strong><p>${escapeHtml(
          unsupported.join("; ")
        )}</p>`
      );
    }
    if (analysis.missing_integrations.length) {
      messages.push(
        `<strong>Connections required before running</strong><p>${escapeHtml(
          analysis.missing_integrations.join(", ")
        )}</p>`
      );
    }
    if (warnings.length) {
      messages.push(`<p>${escapeHtml(warnings.join(" "))}</p>`);
    }
    warning.innerHTML = messages.join("");
    warning.classList.remove("hidden");
  } else {
    warning.classList.add("hidden");
  }
  document.querySelector("#create-submit").disabled = unsupported.length > 0;
}

function resetWorkflowAnalysis() {
  state.pendingAnalysis = null;
  state.pendingPlanSession = null;
  state.selectedRecommendationId = null;
  document.querySelector("#workflow-input-panel").classList.remove("hidden");
  document.querySelector("#workflow-options").classList.remove("hidden");
  els.clarificationPanel.classList.add("hidden");
  els.analysisReview.classList.add("hidden");
  document.querySelector("#edit-analysis").classList.add("hidden");
  document.querySelector("#create-submit").disabled = false;
  document.querySelector("#create-submit span").textContent = "Analyze Request";
}

async function reanalyzeRequest() {
  const revised = els.analysisRequestEditor.value.trim();
  if (!revised) return;
  document.querySelector("#workflow-prompt").value = revised;
  resetWorkflowAnalysis();
  await advanceWorkflowPlan();
}

async function refineSelectedWorkflow(event) {
  event.preventDefault();
  if (!state.selectedWorkflow) return;
  const instruction = els.workflowRefinePrompt.value.trim();
  if (!instruction) return;

  els.workflowRefineSubmit.disabled = true;
  els.workflowRefineSubmit.querySelector("span").textContent = "Applying...";
  els.workflowRefineResult.classList.add("hidden");
  try {
    const result = await api("/copilot/modify", {
      method: "POST",
      body: JSON.stringify({
        workflow: state.selectedWorkflow,
        instruction,
        context: {},
      }),
    });
    state.selectedWorkflow = result.workflow;
    state.workflows = await api("/workflows");
    els.workflowRefinePrompt.value = "";
    els.workflowRefineResult.innerHTML = `
      <strong>Workflow updated</strong>
      <p>${escapeHtml(result.explanation || "Your requested change was applied and validated.")}</p>
      ${
        result.operations?.length
          ? `<ul>${result.operations
              .map((operation) => `<li>${escapeHtml(operationSummary(operation))}</li>`)
              .join("")}</ul>`
          : ""
      }
    `;
    els.workflowRefineResult.classList.remove("hidden");
    render();
    showToast("AI changes applied");
  } catch (error) {
    els.workflowRefineResult.innerHTML = `<strong>Could not apply that change</strong><p>${escapeHtml(
      error.message
    )}</p>`;
    els.workflowRefineResult.classList.remove("hidden");
  } finally {
    els.workflowRefineSubmit.disabled = false;
    els.workflowRefineSubmit.querySelector("span").textContent = "Apply AI change";
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
    state.tasks = await api("/tasks");
    render();
    const createdTask = run.steps.find((step) => step.output?.created_task)?.output?.created_task;
    showToast(
      createdTask
        ? `Task created: ${createdTask.title}`
        : run.status === "success"
        ? "Workflow run completed"
        : run.summary || "Workflow run failed"
    );
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

async function saveIntegration(event) {
  event.preventDefault();
  const form = event.target;
  const provider = form.dataset.integrationForm;
  const submit = form.querySelector('[type="submit"]');
  const config = Object.fromEntries(new FormData(form).entries());
  submit.disabled = true;
  submit.querySelector("span").textContent = "Saving...";
  try {
    const saved = await api(`/integrations/${provider}`, {
      method: "PUT",
      body: JSON.stringify({ config }),
    });
    state.integrations = state.integrations.map((item) => (item.provider === provider ? saved : item));
    render();
    showToast(`${saved.name} connected`);
  } catch (error) {
    showToast(error.message);
  } finally {
    submit.disabled = false;
    submit.querySelector("span").textContent = "Save connection";
  }
}

async function disconnectIntegration(provider) {
  const integration = state.integrations.find((item) => item.provider === provider);
  if (!integration || !window.confirm(`Disconnect ${integration.name}? Workflows using it will fail until reconnected.`)) {
    return;
  }
  try {
    await api(`/integrations/${provider}`, { method: "DELETE" });
    state.integrations = await api("/integrations");
    render();
    showToast(`${integration.name} disconnected`);
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

document.querySelector("#home-button").addEventListener("click", () => setView("workflows"));

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
  const deleteAction = event.target.closest("[data-delete-workflow-id]");
  if (deleteAction) {
    deleteWorkflow(deleteAction.dataset.deleteWorkflowId);
    return;
  }
  const workflowCard = event.target.closest("[data-workflow-id]");
  if (workflowCard) openWorkflow(workflowCard.dataset.workflowId);
  const disconnectAction = event.target.closest("[data-disconnect-integration]");
  if (disconnectAction) disconnectIntegration(disconnectAction.dataset.disconnectIntegration);
});

document.body.addEventListener("submit", (event) => {
  if (event.target.matches("[data-integration-form]")) saveIntegration(event);
});

document.querySelector("#close-create").addEventListener("click", () => {
  els.createModal.classList.add("hidden");
  resetWorkflowAnalysis();
});
document.querySelector("#cancel-create").addEventListener("click", () => {
  els.createModal.classList.add("hidden");
  resetWorkflowAnalysis();
});
document.querySelector("#edit-analysis").addEventListener("click", resetWorkflowAnalysis);
document.querySelector("#reanalyze-request").addEventListener("click", reanalyzeRequest);
document.querySelector("#create-form").addEventListener("submit", createWorkflow);
els.workflowRefineForm.addEventListener("submit", refineSelectedWorkflow);
document.querySelector("#invite-form").addEventListener("submit", inviteMember);
document.querySelector("#back-button").addEventListener("click", () => setView("workflows"));
document.querySelector("#save-workflow-button").addEventListener("click", saveWorkflowEdits);
document.querySelector("#run-now-button").addEventListener("click", runSelectedWorkflow);
document.querySelector("#logout-button").addEventListener("click", async () => {
  await api("/auth/logout", { method: "POST" });
  window.location.assign("/");
});

initializeApp().catch((error) => {
  showToast(error.message);
  showLogin({ authenticated: false, google_configured: true });
});
