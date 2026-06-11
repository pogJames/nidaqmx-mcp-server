const chart = document.getElementById("chart");
const filesUl = document.getElementById("files");
const limitsUl = document.getElementById("limits");
const status = document.getElementById("status");
const workspaceTitle = document.getElementById("workspace-title");

const savedTitle = localStorage.getItem("workspace-title");
if (savedTitle) workspaceTitle.textContent = savedTitle;
workspaceTitle.addEventListener("blur", () => {
  const text = workspaceTitle.textContent.trim() || "Workspace";
  workspaceTitle.textContent = text;
  localStorage.setItem("workspace-title", text);
});
workspaceTitle.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); workspaceTitle.blur(); }
});

function setList(el, items, emptyMsg, render) {
  el.innerHTML = "";
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = emptyMsg;
    el.appendChild(li);
    return;
  }
  for (const item of items) el.appendChild(render(item));
}

async function refreshFiles() {
  const r = await fetch("/api/files");
  const files = await r.json();
  setList(filesUl, files, "none", (f) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="id">${f.file_id}</span><span class="path">${f.path}</span>`;
    return li;
  });
}

async function refreshLimits() {
  const r = await fetch("/api/limits");
  const limits = await r.json();
  setList(limitsUl, limits, "none", (l) => {
    const li = document.createElement("li");
    const label = `${l.group}/${l.channel} ${l.kind} ${l.op} ${l.value}`;
    const result = l.status
      ? `<span class="${l.status}">${l.status.toUpperCase()}${l.actual !== undefined ? ` (${Number(l.actual).toPrecision(4)})` : ""}</span>`
      : '<span class="muted">pending</span>';
    li.innerHTML = `<div>${label}</div><div>${result}</div>`;
    return li;
  });
}

function renderFigure(fig) {
  if (!fig || !fig.data) return;
  Plotly.react(chart, fig.data, fig.layout || {}, { responsive: true });
}

function connect() {
  const es = new EventSource("/api/events");
  es.onopen = () => { status.textContent = "live"; };
  es.onerror = () => { status.textContent = "disconnected — retrying…"; };
  es.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "figure") {
      renderFigure(msg.data);
      status.textContent = `updated ${new Date().toLocaleTimeString()}`;
    } else if (msg.type === "refresh_files") {
      refreshFiles();
    } else if (msg.type === "refresh_limits") {
      refreshLimits();
    }
  };
}

refreshFiles();
refreshLimits();
connect();
window.addEventListener("resize", () => Plotly.Plots.resize(chart));
