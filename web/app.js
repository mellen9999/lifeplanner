"use strict";
// lifeplanner ui — vanilla. server is source of truth; we refetch on change.

const ACCENTS = [
  "#ff8700", "#ffd700", "#00d75f", "#00d7d7",
  "#5fafff", "#8080ff", "#d75fd7", "#ff5f5f",
];
const DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const MONTHS = ["january", "february", "march", "april", "may", "june",
  "july", "august", "september", "october", "november", "december"];

let state = { achievements: [], todos: [], appointments: [], settings: {}, version: "" };
let view = "calendar";
let sel = -1;                 // selected list index in current section
let calCursor = startOfMonth(new Date());
let selDay = iso(new Date()); // selected calendar day
let pendingDelete = false;    // first 'd' of 'dd'

// ---- helpers ----------------------------------------------------------------

function iso(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }
function startOfMonth(d) { return new Date(d.getFullYear(), d.getMonth(), 1); }
function todayIso() { return iso(new Date()); }
function pad(n) { return String(n).padStart(2, "0"); }
function el(tag, cls, txt) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
}
function clear(node) { node.replaceChildren(); }

function fmtWhen(when) {
  if (!when) return "";
  const d = new Date(when.length <= 10 ? when + "T00:00" : when);
  if (isNaN(d)) return when;
  const base = iso(d);
  return when.length <= 10 ? base : `${base} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// ---- api --------------------------------------------------------------------

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.error || r.statusText); }
  return r.status === 204 ? null : r.json();
}

async function refresh() {
  state = await api("GET", "/api/state");
  applyTheme();
  render();
}

async function add(entity, data) {
  try { await api("POST", `/api/${entity}`, data); await refresh(); }
  catch (e) { console.warn("[lifeplanner]", e.message); }
}
async function patch(entity, id, data) { await api("PATCH", `/api/${entity}/${id}`, data); await refresh(); }
async function remove(entity, id) { await api("DELETE", `/api/${entity}/${id}`); await refresh(); }

// ---- theme ------------------------------------------------------------------

function applyTheme() {
  const s = state.settings || {};
  const theme = s.theme || "dark";
  const accent = s.accent || ACCENTS[0];
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.setProperty("--accent", accent);
  document.getElementById("theme-btn").textContent = theme;
  try { localStorage.setItem("lp-theme", theme); localStorage.setItem("lp-accent", accent); } catch {}
  renderAccents(accent);
}

async function setSetting(patchObj) {
  state.settings = await api("PUT", "/api/settings", patchObj);
  applyTheme();
}

function toggleTheme() {
  setSetting({ theme: (state.settings.theme === "dark" ? "light" : "dark") });
}

function renderAccents(active) {
  const box = document.getElementById("accents");
  clear(box);
  ACCENTS.forEach(c => {
    const b = el("button", "swatch" + (c === active ? " active" : ""));
    b.style.background = c;
    b.title = c;
    b.onclick = () => setSetting({ accent: c });
    box.appendChild(b);
  });
}

// ---- routing ----------------------------------------------------------------

function setView(v) {
  view = v;
  sel = -1;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.view === v));
  document.querySelectorAll(".view").forEach(s => s.classList.toggle("active", s.id === v));
  render();
}

function currentList() {
  if (view === "achievements") return state.achievements;
  if (view === "todos") return state.todos;
  if (view === "appointments") return state.appointments;
  return [];
}

// ---- render -----------------------------------------------------------------

function render() {
  renderCalendar();
  renderAppointments();
  renderAchievements();
  renderTodos();
}

function addRow(fields, onSubmit) {
  const form = el("form", "add");
  const inputs = {};
  fields.forEach(f => {
    const i = el("input");
    i.type = f.type || "text";
    i.placeholder = f.ph || "";
    if (f.cls) i.className = f.cls;
    if (f.value) i.value = f.value;
    inputs[f.name] = i;
    form.appendChild(i);
  });
  const btn = el("button", null, "add"); btn.type = "submit";
  form.appendChild(btn);
  form.onsubmit = (e) => {
    e.preventDefault();
    const data = {};
    for (const k in inputs) data[k] = inputs[k].value.trim();
    if (!data[fields[0].name]) return;
    onSubmit(data);
    fields.forEach(f => { if (f.type !== "date") inputs[f.name].value = ""; });
    inputs[fields[0].name].focus();
  };
  form._first = inputs[fields[0].name];
  return form;
}

function buildBody(title, sub) {
  const b = el("div", "body");
  b.appendChild(el("div", "title", title));
  if (sub) b.appendChild(el("div", "sub", sub));
  return b;
}

function listRow(item, parts, opts = {}) {
  const row = el("div", "row" + (opts.done ? " done" : ""));
  row.dataset.id = item.id;
  if (opts.tick) {
    const t = el("span", "tick", item.done ? "x" : "");
    t.onclick = (e) => { e.stopPropagation(); patch("todos", item.id, { done: !item.done }); };
    row.appendChild(t);
  }
  parts.forEach(p => row.appendChild(p));
  const del = el("span", "del", "×");
  del.title = "delete";
  del.onclick = (e) => { e.stopPropagation(); remove(opts.entity, item.id); };
  row.appendChild(del);
  return row;
}

function mountList(container, items, builder, emptyMsg) {
  clear(container);
  if (!items.length) { container.appendChild(el("div", "empty", emptyMsg)); return; }
  const list = el("div", "list");
  items.forEach((it, i) => {
    const row = builder(it, i);
    if (i === sel) row.classList.add("sel");
    row.onclick = () => { sel = i; render(); };
    list.appendChild(row);
  });
  container.appendChild(list);
}

// appointments
function renderAppointments() {
  const root = document.getElementById("appointments");
  clear(root);
  const h = el("h2", "section-h", "appointments");
  h.appendChild(el("span", "count", `${state.appointments.length}`));
  root.appendChild(h);
  root.appendChild(addRow([
    { name: "title", ph: "what", cls: "title" },
    { name: "when", type: "date", value: selDay },
    { name: "time", type: "time" },
    { name: "location", ph: "where (optional)" },
  ], d => {
    const when = d.time ? `${d.when} ${d.time}` : d.when;
    add("appointments", { title: d.title, when, location: d.location });
  }));
  const body = el("div");
  mountList(body, state.appointments, (a) => listRow(a, [
    el("span", "when", fmtWhen(a.when)),
    buildBody(a.title, a.location),
  ], { entity: "appointments" }), "no appointments. press n to add one.");
  root.appendChild(body);
}

// achievements
function renderAchievements() {
  const root = document.getElementById("achievements");
  clear(root);
  const h = el("h2", "section-h", "achievements");
  h.appendChild(el("span", "count", `${state.achievements.length} logged`));
  root.appendChild(h);
  root.appendChild(addRow([
    { name: "title", ph: "what you did", cls: "title" },
    { name: "date", type: "date", value: todayIso() },
    { name: "note", ph: "note (optional)" },
  ], d => add("achievements", d)));
  const body = el("div");
  mountList(body, state.achievements, (a) => listRow(a, [
    el("span", "when", a.date),
    buildBody(a.title, a.note),
  ], { entity: "achievements" }), "no wins logged yet. log your first one — press n.");
  root.appendChild(body);
}

// todos
function renderTodos() {
  const root = document.getElementById("todos");
  clear(root);
  const open = state.todos.filter(t => !t.done).length;
  const h = el("h2", "section-h", "todos / reminders");
  h.appendChild(el("span", "count", `${open} open`));
  root.appendChild(h);
  root.appendChild(addRow([
    { name: "title", ph: "to do", cls: "title" },
    { name: "due", type: "date" },
  ], d => add("todos", d)));
  const body = el("div");
  const sorted = [...state.todos].sort((a, b) =>
    (a.done - b.done) || ((a.due || "9999") > (b.due || "9999") ? 1 : -1));
  mountList(body, sorted, (t) => listRow(t, [
    el("span", "when", t.due ? t.due : "—"),
    buildBody(t.title, t.due && !t.done && t.due < todayIso() ? "overdue" : ""),
  ], { entity: "todos", tick: true, done: t.done }), "nothing to do. press n to add.");
  root.appendChild(body);
}

// calendar
function renderCalendar() {
  const root = document.getElementById("calendar");
  clear(root);
  const head = el("div"); head.id = "cal-head";
  const prev = el("button", null, "‹ h"); prev.onclick = () => shiftMonth(-1);
  const next = el("button", null, "l ›"); next.onclick = () => shiftMonth(1);
  const tod = el("button", null, "today");
  tod.onclick = () => { calCursor = startOfMonth(new Date()); selDay = todayIso(); render(); };
  const title = el("div", null, `${MONTHS[calCursor.getMonth()]} ${calCursor.getFullYear()}`);
  title.id = "cal-title";
  head.append(prev, title, next, tod);
  root.appendChild(head);

  const wrap = el("div", "cal-wrap");
  const grid = el("div", "grid");
  DOW.forEach(d => grid.appendChild(el("div", "dow", d)));

  const byDay = indexByDay();
  const lead = (startOfMonth(calCursor).getDay() + 6) % 7; // monday = 0
  const daysInMonth = new Date(calCursor.getFullYear(), calCursor.getMonth() + 1, 0).getDate();
  const prevDays = new Date(calCursor.getFullYear(), calCursor.getMonth(), 0).getDate();

  for (let i = 0; i < lead; i++) {
    const c = el("div", "cell pad");
    c.appendChild(el("div", "num", String(prevDays - lead + i + 1)));
    grid.appendChild(c);
  }
  for (let day = 1; day <= daysInMonth; day++) {
    const ds = `${calCursor.getFullYear()}-${pad(calCursor.getMonth() + 1)}-${pad(day)}`;
    const c = el("div", "cell");
    if (ds === todayIso()) c.classList.add("today");
    if (ds === selDay) c.classList.add("sel");
    c.appendChild(el("div", "num", String(day)));
    const marks = el("div", "marks");
    const info = byDay[ds];
    if (info) {
      if (info.ach) marks.appendChild(el("span", "mk ach"));
      if (info.appt) marks.appendChild(el("span", "mk appt"));
      if (info.todo) marks.appendChild(el("span", "mk todo"));
    }
    c.appendChild(marks);
    c.onclick = () => { selDay = ds; render(); };
    grid.appendChild(c);
  }
  const total = lead + daysInMonth;
  for (let i = total; i % 7 !== 0; i++) {
    const c = el("div", "cell pad");
    c.appendChild(el("div", "num", String(i - total + 1)));
    grid.appendChild(c);
  }
  wrap.appendChild(grid);
  wrap.appendChild(renderDayPanel());
  root.appendChild(wrap);
}

function renderDayPanel() {
  const panel = el("div"); panel.id = "day-panel";
  panel.appendChild(el("h3", null, selDay));
  const items = [];
  state.appointments.filter(a => (a.when || "").slice(0, 10) === selDay)
    .forEach(a => items.push(["appt", `${fmtWhen(a.when).slice(11) || ""} ${a.title}`.trim()]));
  state.todos.filter(t => t.due === selDay)
    .forEach(t => items.push(["todo", (t.done ? "✓ " : "") + t.title]));
  state.achievements.filter(a => a.date === selDay)
    .forEach(a => items.push(["ach", a.title]));
  if (!items.length) panel.appendChild(el("div", "muted small", "nothing on this day"));
  items.forEach(([kind, text]) => {
    const li = el("div", "li");
    li.appendChild(el("span", "mk " + kind));
    li.appendChild(el("span", null, text));
    panel.appendChild(li);
  });
  const q = el("div", "quick");
  const inp = el("input"); inp.placeholder = "+ appointment";
  const b = el("button", null, "add");
  const submit = () => { const v = inp.value.trim(); if (v) { add("appointments", { title: v, when: selDay }); inp.value = ""; } };
  b.onclick = submit;
  inp.onkeydown = (e) => { if (e.key === "Enter") submit(); };
  q.append(inp, b);
  panel.appendChild(q);
  return panel;
}

function indexByDay() {
  const m = {};
  const touch = (ds, k) => { (m[ds] = m[ds] || {})[k] = true; };
  state.appointments.forEach(a => { if (a.when) touch(a.when.slice(0, 10), "appt"); });
  state.todos.forEach(t => { if (t.due) touch(t.due, "todo"); });
  state.achievements.forEach(a => { if (a.date) touch(a.date, "ach"); });
  return m;
}

function shiftMonth(n) {
  calCursor = new Date(calCursor.getFullYear(), calCursor.getMonth() + n, 1);
  render();
}

// ---- keyboard ---------------------------------------------------------------

function focusAdd() {
  const v = document.getElementById(view);
  const form = v.querySelector(".add");
  if (form && form._first) {
    form._first.focus();
    form._first.classList.add("flash");
    setTimeout(() => form._first.classList.remove("flash"), 400);
  }
}

function moveSel(d) {
  const list = currentList();
  if (!list.length) return;
  sel = sel < 0 ? (d > 0 ? 0 : list.length - 1) : Math.max(0, Math.min(list.length - 1, sel + d));
  render();
  const elRow = document.querySelector(`#${view} .row.sel`);
  if (elRow) elRow.scrollIntoView({ block: "nearest" });
}

document.addEventListener("keydown", (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  const help = document.getElementById("help");

  if (e.key === "Escape") {
    if (!help.hidden) { help.hidden = true; return; }
    if (typing) { document.activeElement.blur(); return; }
  }
  if (typing) return;
  if (!help.hidden && e.key !== "?") return;

  switch (e.key) {
    case "1": setView("calendar"); break;
    case "2": setView("appointments"); break;
    case "3": setView("achievements"); break;
    case "4": setView("todos"); break;
    case "n": e.preventDefault(); focusAdd(); break;
    case "t": toggleTheme(); break;
    case "r": refresh(); break;
    case "?": help.hidden = !help.hidden; break;
    case "j": if (view === "calendar") moveCalDay(7); else moveSel(1); break;
    case "k": if (view === "calendar") moveCalDay(-7); else moveSel(-1); break;
    case "h": if (view === "calendar") shiftMonth(-1); break;
    case "l": if (view === "calendar") shiftMonth(1); break;
    case "x": toggleSelTodo(); break;
    case "d":
      if (pendingDelete) { deleteSel(); pendingDelete = false; }
      else { pendingDelete = true; setTimeout(() => pendingDelete = false, 600); }
      break;
    default: pendingDelete = false;
  }
});

function moveCalDay(delta) {
  const d = new Date(selDay + "T00:00");
  d.setDate(d.getDate() + delta);
  selDay = iso(d);
  if (d.getMonth() !== calCursor.getMonth() || d.getFullYear() !== calCursor.getFullYear())
    calCursor = startOfMonth(d);
  render();
}

function toggleSelTodo() {
  if (view !== "todos") return;
  const row = document.querySelector("#todos .row.sel");
  if (!row) return;
  const t = state.todos.find(x => x.id === row.dataset.id);
  if (t) patch("todos", t.id, { done: !t.done });
}

function deleteSel() {
  if (view === "calendar") return;
  const row = document.querySelector(`#${view} .row.sel`);
  if (row) remove(view, row.dataset.id);
}

// ---- boot + polling ---------------------------------------------------------

function wireBar() {
  document.querySelectorAll(".tab").forEach(t => t.onclick = () => setView(t.dataset.view));
  document.getElementById("theme-btn").onclick = toggleTheme;
  document.getElementById("help-btn").onclick = () => { const h = document.getElementById("help"); h.hidden = !h.hidden; };
  document.getElementById("help").onclick = (e) => { if (e.target.id === "help") e.target.hidden = true; };
}

// kill load flash before the first fetch returns
(function preTheme() {
  try {
    const t = localStorage.getItem("lp-theme"); if (t) document.documentElement.dataset.theme = t;
    const a = localStorage.getItem("lp-accent"); if (a) document.documentElement.style.setProperty("--accent", a);
  } catch {}
})();

let polling = false;
async function poll() {
  if (polling || document.visibilityState !== "visible") return;
  polling = true;
  try {
    const { version } = await api("GET", "/api/version");
    if (version !== state.version) await refresh();
  } catch {} finally { polling = false; }
}

wireBar();
setView("calendar");
refresh();
setInterval(poll, 4000);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") poll(); });
