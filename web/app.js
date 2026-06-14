"use strict";
// lifeplanner ui — vanilla. server is source of truth; we refetch on change.

const ACCENTS = [
  "#ff8700", "#ffd700", "#00d75f", "#00d7d7",
  "#5fafff", "#8080ff", "#d75fd7", "#ff5f5f",
];
const DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const MONTHS = ["january", "february", "march", "april", "may", "june",
  "july", "august", "september", "october", "november", "december"];
const VIEWS = ["today", "calendar", "appointments", "achievements", "todos"];
const REPEAT_OPTIONS = [
  { value: "", label: "once" },
  { value: "daily", label: "daily" },
  { value: "weekly", label: "weekly" },
  { value: "weekly:2", label: "every other week" },
  { value: "monthly", label: "monthly" },
];

let state = { achievements: [], todos: [], appointments: [], settings: {}, version: "" };
let slipping = null;   // cached /api/slipping response
let weekReview = null; // cached /api/review?days=7 response
let view = "today";
let sel = -1;                 // selected list index in current section
let editing = null;           // id of the item being edited inline
let calCursor = startOfMonth(new Date());
let selDay = iso(new Date()); // selected calendar day
let pendingDelete = false;    // first 'd' of 'dd'
let showDone = false;         // todos: reveal the collapsed "done" pile
try { showDone = localStorage.getItem("lp-show-done") === "1"; } catch {}
let lastDeleted = null;       // {entity, item} for single-level undo
let search = "";              // active filter query ("" = off)
let searchOpen = false;       // filter bar visible

// ---- helpers ----------------------------------------------------------------

function iso(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }
function startOfMonth(d) { return new Date(d.getFullYear(), d.getMonth(), 1); }
function todayIso() { return iso(new Date()); }
function pad(n) { return String(n).padStart(2, "0"); }
function addDays(ds, n) { const d = new Date(ds + "T00:00"); d.setDate(d.getDate() + n); return iso(d); }
function dayDiff(a, b) { return Math.round((new Date(b + "T00:00") - new Date(a + "T00:00")) / 864e5); }
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
function timeOf(when) { return when && when.length > 10 ? fmtWhen(when).slice(11) : ""; }

// ---- recurrence (mirrors store.py) -----------------------------------------

// concrete when-strings an appointment falls on within [from, to] (inclusive).
// mirrors store.occurrences_in, incl. RRULE monthly skip semantics.
function apptOccurrences(a, fromIso, toIso) {
  const when = a.when || "";
  if (!when) return [];
  const anchor = when.slice(0, 10), timePart = when.slice(10);
  const r = a.recur;
  if (!r || !r.freq) return (anchor >= fromIso && anchor <= toIso) ? [when] : [];
  const iv = Math.max(1, r.interval || 1), until = r.until || "";
  const limit = (until && until < toIso) ? until : toIso;
  const out = [];
  if (r.freq === "monthly") {
    const ay = +anchor.slice(0, 4), am = +anchor.slice(5, 7), ad = +anchor.slice(8, 10);
    for (let k = 0; k < 10000; k++) {
      const tot = (am - 1) + iv * k;
      const y = ay + Math.floor(tot / 12), m = (tot % 12) + 1;
      if (`${y}-${pad(m)}-01` > limit) break;
      if (ad > new Date(y, m, 0).getDate()) continue;  // month lacks this day
      const ds = `${y}-${pad(m)}-${pad(ad)}`;
      if (ds >= fromIso && ds <= limit) out.push(ds + timePart);
    }
  } else {
    const step = r.freq === "daily" ? iv : 7 * iv;
    let d = anchor, guard = 0;
    while (d <= limit && guard < 100000) {
      guard++;
      if (d >= fromIso) out.push(d + timePart);
      d = addDays(d, step);
    }
  }
  return out;
}
function nextOccurrence(a, fromIso) {
  return apptOccurrences(a, fromIso, addDays(fromIso, 366 * 5))[0] || null;
}
function parseRepeat(v, until) {
  if (!v) return "";
  const [freq, iv] = v.split(":");
  const r = { freq, interval: iv ? parseInt(iv, 10) : 1 };
  if (until) r.until = until;   // optional end date; empty clears it
  return r;
}
function repeatValue(r) { return (!r || !r.freq) ? "" : (r.interval > 1 ? `${r.freq}:${r.interval}` : r.freq); }
function recurLabel(r, anchorIso) {
  if (!r || !r.freq) return "";
  const iv = r.interval || 1;
  let base;
  if (r.freq === "weekly") {
    const dow = DOW[(new Date(anchorIso + "T00:00").getDay() + 6) % 7];
    base = (iv === 2 ? "every other " : iv === 1 ? "every " : `every ${iv} weeks · `) + dow;
  } else if (r.freq === "daily") base = iv === 1 ? "every day" : `every ${iv} days`;
  else base = iv === 1 ? "monthly" : `every ${iv} months`;
  return r.until ? `${base} · until ${r.until}` : base;
}

// ---- api --------------------------------------------------------------------

const TOKEN = document.querySelector('meta[name="lp-token"]')?.content || "";

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (TOKEN) opt.headers["Authorization"] = "Bearer " + TOKEN;
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.error || r.statusText); }
  return r.status === 204 ? null : r.json();
}

async function refresh() {
  try {
    state = await api("GET", "/api/state");
  } catch (e) {
    // boot/manual refresh failure must not leave a silent blank app
    toast("can't reach server — " + e.message);
    return;
  }
  applyTheme();
  // fetch planning-partner data in parallel; failures are non-fatal (stale cache ok)
  if (view === "today") await refreshPlannerData();
  render();
}

async function refreshPlannerData() {
  try {
    [slipping, weekReview] = await Promise.all([
      api("GET", "/api/slipping"),
      api("GET", "/api/review?days=7"),
    ]);
  } catch (e) {
    // silently keep whatever was cached; the blocks will render with stale data
  }
}

async function add(entity, data) {
  try { await api("POST", `/api/${entity}`, data); await refresh(); }
  catch (e) { toast(e.message); }
}
async function patch(entity, id, data) {
  try { await api("PATCH", `/api/${entity}/${id}`, data); await refresh(); }
  catch (e) { toast(e.message); }
}
// delete with a one-shot undo. the undo only arms on a CONFIRMED delete: a
// failed request shows the real error and leaves no stale undo — otherwise
// pressing `u` would re-POST a still-present item and create a duplicate.
async function deleteWithUndo(entity, item) {
  try {
    await api("DELETE", `/api/${entity}/${item.id}`);
  } catch (e) {
    toast(e.message);
    return;
  }
  lastDeleted = { entity, item };
  await refresh();
  toast(`deleted "${item.title}"`, "undo (u)", undoDelete);
}
function undoDelete() {
  if (!lastDeleted) return;
  const { entity, item } = lastDeleted;
  lastDeleted = null;
  add(entity, item);
  toast("restored");
}

// transient error/feedback line — failed actions are never silent
let toastTimer = null;
function toast(msg, actionLabel, actionFn) {
  const t = document.getElementById("toast");
  clear(t);
  t.appendChild(document.createTextNode(msg));
  if (actionLabel && actionFn) {
    const a = el("span", "toast-action", actionLabel);
    a.onclick = () => { t.hidden = true; actionFn(); };
    t.appendChild(a);
  }
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, actionFn ? 7000 : 3500);
}

// honest staleness signal — when appointments come from cache (sync server
// unreachable) the user is told, never shown stale data as if it were current
function renderSyncBanner() {
  const bar = document.getElementById("syncbar");
  if ((state.sync || {}).source === "cache") {
    bar.textContent = "⚠ calendar server unreachable — showing last saved · press r to retry";
    bar.hidden = false;
  } else {
    bar.hidden = true;
  }
}

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
  try {
    state.settings = await api("PUT", "/api/settings", patchObj);
  } catch (e) {
    toast("couldn't save — " + e.message);
    return;
  }
  applyTheme();
}

function toggleTheme() {
  setSetting({ theme: (state.settings.theme === "dark" ? "light" : "dark") });
}

// one-click backup: download the whole data vault as a zip. fetched with the
// token (kept out of the url), named with today's date.
async function exportData() {
  try {
    const r = await fetch("/api/export", { headers: TOKEN ? { Authorization: "Bearer " + TOKEN } : {} });
    if (!r.ok) throw new Error(r.statusText);
    const url = URL.createObjectURL(await r.blob());
    const a = el("a");
    a.href = url; a.download = `lifeplanner-${todayIso()}.zip`;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    toast("exported");
  } catch (e) { toast("export failed — " + e.message); }
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
  editing = null;
  // a half-armed delete must never carry across views and hit the wrong row
  pendingDelete = false;
  // a filter belongs to the list it was typed in — don't carry it across views
  closeSearch(false);
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.view === v));
  document.querySelectorAll(".view").forEach(s => s.classList.toggle("active", s.id === v));
  if (v === "today") { refreshPlannerData().then(render); } else { render(); }
}

function currentList() {
  if (view === "achievements") return applySearch(state.achievements);
  if (view === "todos") return applySearch(visibleTodos());
  if (view === "appointments") return applySearch(state.appointments);
  return [];
}

// live text filter (the `/` search). matches title + the contextual fields so
// "dentist", a date fragment, or a location all find what you'd expect.
function matchesSearch(it) {
  const q = search.toLowerCase();
  return [it.title, it.note, it.location, it.due, it.date, it.when]
    .some(f => (f || "").toLowerCase().includes(q));
}
function applySearch(items) { return search ? items.filter(matchesSearch) : items; }

function sortedTodos() {
  return [...state.todos].sort((a, b) =>
    (a.done - b.done) || ((a.due || "9999") > (b.due || "9999") ? 1 : -1));
}

// what the todos list actually shows: active always, the done pile only when
// expanded. done stays sorted to the bottom so the active/done boundary is clean.
function visibleTodos() {
  const all = sortedTodos();
  return showDone ? all : all.filter(t => !t.done);
}

// ---- render -----------------------------------------------------------------

// only the visible view is (re)built — the other four are display:none, so
// painting them on every poll/keystroke is wasted DOM work. switching views
// calls render() again, so the newly-active view is always fresh.
const RENDERERS = {
  today: renderToday, calendar: renderCalendar, appointments: renderAppointments,
  achievements: renderAchievements, todos: renderTodos,
};
function render() {
  renderSyncBanner();
  (RENDERERS[view] || renderToday)();
  if (searchOpen) {
    const n = currentList().length;
    document.getElementById("search-count").textContent = `${n} match${n === 1 ? "" : "es"}`;
  }
}

function openSearch() {
  if (!["appointments", "achievements", "todos"].includes(view)) return;
  searchOpen = true;
  document.getElementById("searchbar").hidden = false;
  const inp = document.getElementById("search-input");
  inp.value = search;
  render();
  inp.focus(); inp.select();
}
// rerender=false when the caller (setView) will render anyway — avoids a double paint
function closeSearch(rerender = true) {
  if (!searchOpen && !search) return;
  search = ""; searchOpen = false; sel = -1;
  document.getElementById("searchbar").hidden = true;
  document.getElementById("search-input").blur();
  if (rerender) render();
}

// build one form control from a field spec (input or select). shared by the
// add row and the inline edit row so both support the same field types.
function makeField(f) {
  if (f.type === "select") {
    const s = el("select");
    if (f.cls) s.className = f.cls;
    (f.options || []).forEach(o => {
      const op = el("option", null, o.label);
      op.value = o.value;
      if (o.value === (f.value || "")) op.selected = true;
      s.appendChild(op);
    });
    return s;
  }
  const i = el("input");
  i.type = f.type || "text";
  if (f.cls) i.className = f.cls;
  i.placeholder = f.ph || "";
  if (f.value != null && f.value !== "") i.value = f.value;
  return i;
}

function addRow(fields, onSubmit) {
  const form = el("form", "add");
  const inputs = {};
  fields.forEach(f => {
    const i = makeField(f);
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

// the done-toggle box shared by every todo row. it IS the action — click to
// complete (strikethrough), click again to reopen. not a selection control.
function tickEl(done, onToggle) {
  const t = el("span", "tick", done ? "✓" : "");
  t.title = done ? "mark not done" : "mark done";
  t.setAttribute("role", "checkbox");
  t.setAttribute("aria-checked", done ? "true" : "false");
  t.tabIndex = 0;  // keyboard-reachable (the only way to tick on the today view)
  t.onclick = (e) => { e.stopPropagation(); onToggle(); };
  t.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); onToggle(); } };
  return t;
}

function listRow(item, entity, parts, opts = {}) {
  if (item.id === editing) return editRow(item, entity);
  const row = el("div", "row" + (opts.done ? " done" : ""));
  row.dataset.id = item.id;
  if (opts.tick) {
    row.appendChild(tickEl(item.done, () => patch("todos", item.id, { done: !item.done })));
  }
  parts.forEach(p => row.appendChild(p));
  const editBtn = el("span", "rowedit", "edit");
  editBtn.title = "edit (e)";
  editBtn.setAttribute("role", "button");
  editBtn.tabIndex = 0;
  const doEdit = (e) => { e.stopPropagation(); editing = item.id; render(); focusEdit(entity); };
  editBtn.onclick = doEdit;
  editBtn.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); doEdit(e); } };
  row.appendChild(editBtn);
  const del = el("span", "del", "×");
  del.title = "delete";
  del.setAttribute("role", "button");
  del.setAttribute("aria-label", "delete");
  del.tabIndex = 0;
  const doDel = (e) => { e.stopPropagation(); deleteWithUndo(entity, item); };
  del.onclick = doDel;
  del.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); doDel(e); } };
  row.appendChild(del);
  row.ondblclick = () => { editing = item.id; render(); focusEdit(entity); };
  return row;
}

// inline edit form, pre-filled, mirroring the entity's fields
function editRow(item, entity) {
  const form = el("form", "row editing");
  form.dataset.id = item.id;
  const fields = editFields(entity, item);
  const inputs = {};
  fields.forEach(f => {
    const i = makeField(f);
    inputs[f.name] = i;
    form.appendChild(i);
  });
  const save = el("button", "rowedit", "save"); save.type = "submit";
  const cancel = el("span", "del", "esc");
  cancel.onclick = () => { editing = null; render(); };
  form.append(save, cancel);
  form.onsubmit = (e) => {
    e.preventDefault();
    const v = {}; for (const k in inputs) v[k] = inputs[k].value.trim();
    if (!v.title) return;
    editing = null;
    patch(entity, item.id, buildPatch(entity, v));
  };
  form._first = inputs.title;
  return form;
}

function editFields(entity, item) {
  if (entity === "appointments") return [
    { name: "title", cls: "title", value: item.title },
    { name: "date", type: "date", value: (item.when || "").slice(0, 10) },
    { name: "time", type: "time", value: timeOf(item.when) },
    { name: "location", ph: "where", value: item.location },
    { name: "repeat", type: "select", value: repeatValue(item.recur), options: REPEAT_OPTIONS },
    { name: "until", type: "date", value: (item.recur && item.recur.until) || "" },
  ];
  if (entity === "achievements") return [
    { name: "title", cls: "title", value: item.title },
    { name: "date", type: "date", value: item.date },
    { name: "note", ph: "note", value: item.note },
  ];
  return [ // todos
    { name: "title", cls: "title", value: item.title },
    { name: "due", type: "date", value: item.due },
  ];
}

function buildPatch(entity, v) {
  if (entity === "appointments")
    return { title: v.title, when: v.time ? `${v.date} ${v.time}` : v.date, location: v.location, recur: parseRepeat(v.repeat, v.until) };
  if (entity === "achievements")
    return { title: v.title, date: v.date, note: v.note };
  return { title: v.title, due: v.due };
}

function focusEdit(entity) {
  const f = document.querySelector(`#${entity} .row.editing .title`);
  if (f) { f.focus(); f.select(); }
}

function mountList(container, items, builder, emptyMsg) {
  clear(container);
  if (!items.length) { container.appendChild(el("div", "empty", emptyMsg)); return; }
  const list = el("div", "list");
  items.forEach((it, i) => {
    const row = builder(it, i);
    if (i === sel && it.id !== editing) row.classList.add("sel");
    if (it.id !== editing) row.onclick = () => { sel = i; render(); };
    list.appendChild(row);
  });
  container.appendChild(list);
}

// ---- planning partner blocks -----------------------------------------------

function renderSlipping() {
  const d = slipping;
  const wrap = el("div", "pp-block");

  if (!d) return wrap; // not yet loaded

  const overdue = d.overdue_todos || [];
  const stale = d.stale_todos || [];
  const dsw = d.days_since_win;

  // win gap line
  const winLine = el("div", "pp-row");
  const winMk = el("span", "mk ach");
  let winText;
  if (dsw === null || dsw === undefined) winText = "no wins logged yet";
  else if (dsw === 0) winText = "logged a win today";
  else winText = `${dsw}d since last win`;
  winLine.append(winMk, el("span", null, winText));
  wrap.appendChild(winLine);

  if (!overdue.length && !stale.length) {
    const ok = el("div", "pp-row pp-ok");
    ok.appendChild(el("span", "mk ach"));
    ok.appendChild(el("span", null, "nothing slipping"));
    wrap.appendChild(ok);
    return wrap;
  }

  if (overdue.length) {
    const hdr = el("div", "pp-hdr");
    hdr.appendChild(el("span", "pp-badge pp-red", String(overdue.length)));
    hdr.appendChild(el("span", null, " overdue"));
    wrap.appendChild(hdr);
    overdue.forEach(t => {
      const row = el("div", "pp-row pp-overdue");
      row.appendChild(el("span", "mk todo"));
      row.appendChild(el("span", "pp-title", t.title));
      row.appendChild(el("span", "pp-meta", `${t.days_late}d late`));
      wrap.appendChild(row);
    });
  }

  if (stale.length) {
    const hdr = el("div", "pp-hdr");
    hdr.appendChild(el("span", "pp-badge pp-yellow", String(stale.length)));
    hdr.appendChild(el("span", null, " stale"));
    wrap.appendChild(hdr);
    stale.forEach(t => {
      const row = el("div", "pp-row pp-stale");
      row.appendChild(el("span", "mk todo"));
      row.appendChild(el("span", "pp-title", t.title));
      row.appendChild(el("span", "pp-meta", `${t.age_days}d old`));
      wrap.appendChild(row);
    });
  }

  return wrap;
}

function renderWeekRecap() {
  const d = weekReview;
  const wrap = el("div", "pp-recap");

  if (!d) return wrap;

  const rate = d.completion_rate != null
    ? `${Math.round(d.completion_rate * 100)}%`
    : "—";
  const xy = `${d.completed_due ?? 0}/${d.due_in_window ?? 0} done`;
  const winsCount = d.wins_count ?? 0;
  const busiest = d.busiest_day;

  const row = el("div", "pp-recap-row");

  const rateEl = el("span", "pp-recap-item");
  rateEl.appendChild(el("span", "pp-recap-n", rate));
  rateEl.appendChild(el("span", "pp-recap-l", ` ${xy}`));
  row.appendChild(rateEl);

  const winsEl = el("span", "pp-recap-item");
  winsEl.appendChild(el("span", "pp-recap-n", String(winsCount)));
  winsEl.appendChild(el("span", "pp-recap-l", " wins"));
  row.appendChild(winsEl);

  if (busiest) {
    const busyEl = el("span", "pp-recap-item");
    busyEl.appendChild(el("span", "pp-recap-l", `busiest: `));
    busyEl.appendChild(el("span", "pp-recap-n", busiest.date.slice(5)));
    busyEl.appendChild(el("span", "pp-recap-l", ` (${busiest.items})`));
    row.appendChild(busyEl);
  }

  wrap.appendChild(row);
  return wrap;
}

// ---- today / agenda ---------------------------------------------------------

function renderToday() {
  const root = document.getElementById("today");
  clear(root);
  const t = todayIso();
  const now = new Date();
  const head = el("h2", "section-h", `today — ${MONTHS[now.getMonth()]} ${now.getDate()}, ${now.getFullYear()}`);
  root.appendChild(head);

  // planning partner: needs-attention + week recap
  const ppWrap = el("div", "pp-wrap");
  const ppAttn = el("div", "pp-section");
  ppAttn.appendChild(el("div", "pp-label", "needs attention"));
  ppAttn.appendChild(renderSlipping());
  ppWrap.appendChild(ppAttn);
  const ppRecap = el("div", "pp-section");
  ppRecap.appendChild(el("div", "pp-label", "this week"));
  ppRecap.appendChild(renderWeekRecap());
  ppWrap.appendChild(ppRecap);
  root.appendChild(ppWrap);

  const grid = el("div", "agenda");

  // appointments today (expand recurring series to today's occurrence)
  const appts = [];
  state.appointments.forEach(a =>
    apptOccurrences(a, t, t).forEach(w => appts.push({ ...a, when: w })));
  appts.sort((a, b) => (a.when > b.when ? 1 : -1));
  grid.appendChild(agendaCard("appointments today", appts.length
    ? appts.map(a => agendaLine("appt", (timeOf(a.when) ? timeOf(a.when) + "  " : "") + a.title, a.location))
    : [el("div", "muted small", "nothing scheduled")]));

  // todos: overdue + due today
  const overdue = state.todos.filter(x => !x.done && x.due && x.due < t)
    .sort((a, b) => (a.due > b.due ? 1 : -1));
  const dueToday = state.todos.filter(x => !x.done && x.due === t);
  const todoLines = [];
  overdue.forEach(x => todoLines.push(agendaTodo(x, `overdue · ${x.due}`)));
  dueToday.forEach(x => todoLines.push(agendaTodo(x, "due today")));
  grid.appendChild(agendaCard("todos due", todoLines.length ? todoLines
    : [el("div", "muted small", "nothing due — nice")]));

  // wins today + quick logger (the daily habit)
  const wins = state.achievements.filter(a => a.date === t);
  const winCard = el("div", "card2");
  winCard.appendChild(el("h3", null, "wins today"));
  wins.forEach(w => winCard.appendChild(agendaLine("ach", w.title, w.note)));
  if (!wins.length) winCard.appendChild(el("div", "muted small", "log one below — even a small one counts"));
  const q = el("form", "quick");
  const inp = el("input"); inp.placeholder = "+ log a win"; inp.id = "today-win";
  const b = el("button", null, "log"); b.type = "submit";
  q.append(inp, b);
  q.onsubmit = (e) => { e.preventDefault(); const v = inp.value.trim(); if (v) { inp.value = ""; add("achievements", { title: v }); } };
  winCard.appendChild(q);
  grid.appendChild(winCard);

  // next 7 days peek (occurrences after today, recurring series included)
  const soon = [];
  state.appointments.forEach(a =>
    apptOccurrences(a, addDays(t, 1), addDays(t, 7)).forEach(w => soon.push({ when: w, title: a.title })));
  soon.sort((a, b) => (a.when > b.when ? 1 : -1));
  grid.appendChild(agendaCard("next 7 days", soon.length
    ? soon.map(a => agendaLine("appt", `${a.when.slice(5, 10)}  ${a.title}`, ""))
    : [el("div", "muted small", "clear")]));

  root.appendChild(grid);

  // streak ribbon
  root.appendChild(streakRibbon());
}

function agendaCard(title, children) {
  const c = el("div", "card2");
  c.appendChild(el("h3", null, title));
  children.forEach(ch => c.appendChild(ch));
  return c;
}
function agendaLine(kind, text, sub) {
  const li = el("div", "li");
  li.appendChild(el("span", "mk " + kind));
  const body = el("div");
  body.appendChild(el("span", null, text));
  if (sub) body.appendChild(el("div", "sub", sub));
  li.appendChild(body);
  return li;
}
function agendaTodo(x, label) {
  const li = el("div", "li" + (label.startsWith("overdue") ? " overdue" : ""));
  li.appendChild(tickEl(false, () => patch("todos", x.id, { done: true })));
  const body = el("div");
  body.appendChild(el("span", null, x.title));
  body.appendChild(el("div", "sub", label));
  li.appendChild(body);
  return li;
}

// ---- achievements heatmap + streaks ----------------------------------------

function winCounts() {
  const m = {};
  state.achievements.forEach(a => { if (a.date) m[a.date] = (m[a.date] || 0) + 1; });
  return m;
}

// arcade streak — honest, no hidden saves. each logged day extends the streak
// and every 7th banks a "shield" (max 3, you start a run with 1). a missed day
// spends a shield to bridge the gap so the streak lives on; miss with zero
// shields and it's GAME OVER — the streak resets to 0. today not logged yet is
// grace (the day isn't over), flagged at-risk when it's your last life.
const STREAK_START = 1, STREAK_EARN = 7, STREAK_MAX = 3;
function arcadeStreak(counts) {
  const today = todayIso();
  const keys = Object.keys(counts).filter(d => d <= today).sort();
  if (!keys.length) return { streak: 0, lives: 0, max: 0, atRisk: false };
  // walk the logged days (O(wins)), bridging the gaps between them in one step
  // each — not day-by-day from the first win to today (which was O(days)).
  let streak = 0, lives = 0, inRun = 0, longest = 0;
  const win = () => {
    if (streak === 0) lives = STREAK_START;     // a fresh run gets its starter shield
    streak++; inRun++;
    if (inRun % STREAK_EARN === 0) lives = Math.min(STREAK_MAX, lives + 1);
    if (streak > longest) longest = streak;
  };
  const miss = (gap) => {                        // `gap` consecutive missed days
    if (gap <= 0 || streak === 0) return;        // once reset, more misses are no-ops
    if (gap <= lives) lives -= gap;              // shields bridge the gap
    else { streak = 0; inRun = 0; lives = 0; }   // out of shields → game over
  };
  win();  // keys[0]: the first logged day
  for (let i = 1; i < keys.length; i++) {
    miss(dayDiff(keys[i - 1], keys[i]) - 1);     // missed days strictly between
    win();
  }
  miss(dayDiff(keys[keys.length - 1], today) - 1);  // trailing gap; today unlogged = grace
  const atRisk = !counts[today] && streak > 0 && lives === 0;
  return { streak, lives, max: longest, atRisk };
}

function streakRibbon() {
  const counts = winCounts();
  const ribbon = el("div", "ribbon");
  const s = arcadeStreak(counts);
  const t = todayIso();
  const week = state.achievements.filter(a => a.date > addDays(t, -7) && a.date <= t).length;
  ribbon.append(
    streakStat(s),
    stat(s.max, "longest"),
    stat(week, "this week"),
    stat(state.achievements.length, "total wins"),
  );
  return ribbon;
}

// the streak stat carries the arcade HUD: number (yellow when at-risk) + a row
// of shield pips (filled = banked lives) so the grace is always visible, never hidden.
function streakStat(s) {
  const box = el("div", "stat");
  const n = el("div", "stat-n" + (s.atRisk ? " risk" : ""), String(s.streak));
  box.appendChild(n);
  box.appendChild(el("div", "stat-l", "day streak"));
  const pips = el("div", "pips");
  for (let i = 0; i < STREAK_MAX; i++) pips.appendChild(el("div", "pip" + (i < s.lives ? " on" : "")));
  pips.title = `${s.lives} of ${STREAK_MAX} shields — a shield saves one missed day; earn one every ${STREAK_EARN} days`;
  box.appendChild(pips);
  box.title = s.atRisk
    ? "at risk — no shields left. log a win today or the streak resets."
    : "streak survives a missed day by spending a shield. out of shields + a miss = reset.";
  return box;
}
function stat(n, label) {
  const s = el("div", "stat");
  s.appendChild(el("div", "stat-n", String(n)));
  s.appendChild(el("div", "stat-l", label));
  return s;
}

function renderHeatmap() {
  const counts = winCounts();
  const WEEKS = 26;
  const today = new Date();
  const start = new Date(today);
  start.setDate(start.getDate() - ((today.getDay() + 6) % 7) - (WEEKS - 1) * 7); // monday, 26 wks back
  const grid = el("div", "hm-grid");
  for (let w = 0; w < WEEKS; w++) {
    const col = el("div", "hm-col");
    for (let dN = 0; dN < 7; dN++) {
      const dt = new Date(start); dt.setDate(start.getDate() + w * 7 + dN);
      const ds = iso(dt);
      const c = counts[ds] || 0;
      const cell = el("div", "hm lvl" + Math.min(3, c));
      if (dt > today) cell.classList.add("future");
      cell.title = `${ds}: ${c} win${c === 1 ? "" : "s"}`;
      col.appendChild(cell);
    }
    grid.appendChild(col);
  }
  const wrap = el("div", "heatmap");
  // colorblind/screen-reader fallback: the green scale isn't perceivable to all,
  // so summarise the window (each cell still carries an exact title tooltip).
  const total = Object.values(counts).reduce((s, n) => s + n, 0);
  wrap.setAttribute("role", "img");
  wrap.setAttribute("aria-label", `wins over the last ${WEEKS} weeks: ${total} total`);
  wrap.appendChild(grid);
  return wrap;
}

// ---- appointments / achievements / todos -----------------------------------

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
    { name: "repeat", type: "select", options: REPEAT_OPTIONS },
    { name: "until", type: "date" },
  ], d => add("appointments", { title: d.title, when: d.time ? `${d.when} ${d.time}` : d.when, location: d.location, recur: parseRepeat(d.repeat, d.until) })));
  const body = el("div");
  // recurring series show their next occurrence in the when column; the label
  // (e.g. "every other thu") makes the repetition explicit.
  mountList(body, applySearch(state.appointments), (a) => {
    const rec = recurLabel(a.recur, (a.when || "").slice(0, 10));
    const shown = rec ? (nextOccurrence(a, todayIso()) || a.when) : a.when;
    const sub = [a.location, rec].filter(Boolean).join("  ·  ");
    return listRow(a, "appointments", [
      el("span", "when", fmtWhen(shown)),
      buildBody(a.title, sub),
    ]);
  }, "no appointments. press n to add one.");
  root.appendChild(body);
}

function renderAchievements() {
  const root = document.getElementById("achievements");
  clear(root);
  const h = el("h2", "section-h", "achievements");
  h.appendChild(el("span", "count", `${state.achievements.length} logged`));
  root.appendChild(h);
  root.appendChild(streakRibbon());
  root.appendChild(renderHeatmap());
  root.appendChild(addRow([
    { name: "title", ph: "what you did", cls: "title" },
    { name: "date", type: "date", value: todayIso() },
    { name: "note", ph: "note (optional)" },
  ], d => add("achievements", d)));
  const body = el("div");
  mountList(body, applySearch(state.achievements), (a) => listRow(a, "achievements", [
    el("span", "when", a.date),
    buildBody(a.title, a.note),
  ]), "no wins logged yet. log your first one — press n.");
  root.appendChild(body);
}

function renderTodos() {
  const root = document.getElementById("todos");
  clear(root);
  const open = state.todos.filter(t => !t.done).length;
  const done = state.todos.length - open;
  const h = el("h2", "section-h", "todos / reminders");
  h.appendChild(el("span", "count", `${open} open`));
  if (done) {
    const toggle = el("span", "count toggle-done",
      `· ${done} done ${showDone ? "▾" : "▸"}`);
    toggle.setAttribute("role", "button");
    toggle.setAttribute("aria-expanded", showDone ? "true" : "false");
    toggle.tabIndex = 0;
    toggle.title = showDone ? "hide completed (X)" : "show completed (X)";
    toggle.onclick = toggleDoneVisibility;
    toggle.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleDoneVisibility(); } };
    h.appendChild(toggle);
  }
  root.appendChild(h);
  root.appendChild(addRow([
    { name: "title", ph: "to do", cls: "title" },
    { name: "due", type: "date" },
  ], d => add("todos", d)));
  const body = el("div");
  mountList(body, applySearch(visibleTodos()), (t) => listRow(t, "todos", [
    el("span", "when", t.due ? t.due : "—"),
    buildBody(t.title, t.done ? (t.done_at ? `done ${t.done_at}` : "done")
      : (t.due && t.due < todayIso() ? "overdue" : "")),
  ], { tick: true, done: t.done }), "nothing to do. press n to add.");
  root.appendChild(body);
}

// ---- calendar ---------------------------------------------------------------

function renderCalendar() {
  const root = document.getElementById("calendar");
  clear(root);
  const head = el("div"); head.id = "cal-head";
  const prev = el("button"); prev.append("‹ ", el("span", "k", "H")); prev.title = "prev month (H)"; prev.onclick = () => shiftMonth(-1);
  const next = el("button"); next.append(el("span", "k", "L"), " ›"); next.title = "next month (L)"; next.onclick = () => shiftMonth(1);
  const tod = el("button", null, "today");
  tod.onclick = () => { calCursor = startOfMonth(new Date()); selDay = todayIso(); render(); };
  const title = el("div", null, `${MONTHS[calCursor.getMonth()]} ${calCursor.getFullYear()}`);
  title.id = "cal-title";
  head.append(prev, title, next, tod);
  root.appendChild(head);

  const wrap = el("div", "cal-wrap");
  const grid = el("div", "grid");
  DOW.forEach(d => grid.appendChild(el("div", "dow", d)));

  const byDay = itemsByDay();
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
    const info = byDay[ds];
    if (info) {
      const evs = el("div", "cell-events");
      const lines = [];
      info.appts.forEach(a => lines.push(["appt", a.time, a.title, false]));
      info.todos.forEach(t => lines.push(["todo", "", t.title, t.done]));
      info.wins.forEach(w => lines.push(["ach", "", w.title, false]));
      const CAP = 4;
      lines.slice(0, CAP).forEach(([k, tm, ti, dn]) => evs.appendChild(eventLine(k, tm, ti, dn)));
      if (lines.length > CAP) evs.appendChild(el("div", "cell-more", `+${lines.length - CAP} more`));
      c.appendChild(evs);
    }
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
  const dp = new Date(selDay + "T00:00");
  panel.appendChild(el("h3", null, `${MONTHS[dp.getMonth()]} ${dp.getDate()}`));
  const items = [];
  state.appointments.forEach(a => apptOccurrences(a, selDay, selDay)
    .forEach(w => items.push(["appt", `${timeOf(w) ? timeOf(w) + " " : ""}${a.title}`.trim()])));
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
  const q = el("form", "quick");
  const inp = el("input"); inp.placeholder = "+ appointment";
  const b = el("button", null, "add"); b.type = "submit";
  q.append(inp, b);
  q.onsubmit = (e) => {
    e.preventDefault();
    const v = inp.value.trim();
    if (v) { add("appointments", { title: v, when: selDay }); inp.value = ""; }
  };
  panel.appendChild(q);
  return panel;
}

// one compact labeled line in a calendar cell: [colored mark] [time] title
function eventLine(kind, time, title, done) {
  const li = el("div", "cell-ev" + (done ? " done" : ""));
  li.appendChild(el("span", "mk " + kind));
  if (time) li.appendChild(el("span", "cell-time", time));
  li.appendChild(el("span", "cell-title", title));
  return li;
}

// actual items per day for the visible month grid (appointment occurrences
// expanded, with their times) — so the calendar shows what's on, not just dots.
function itemsByDay() {
  const m = {};
  const get = ds => (m[ds] = m[ds] || { appts: [], todos: [], wins: [] });
  const from = addDays(iso(startOfMonth(calCursor)), -7);
  const to = addDays(iso(new Date(calCursor.getFullYear(), calCursor.getMonth() + 1, 0)), 7);
  state.appointments.forEach(a => apptOccurrences(a, from, to).forEach(w =>
    get(w.slice(0, 10)).appts.push({ when: w, time: timeOf(w), title: a.title })));
  state.todos.forEach(t => { if (t.due) get(t.due).todos.push(t); });
  state.achievements.forEach(a => { if (a.date) get(a.date).wins.push(a); });
  Object.values(m).forEach(d => d.appts.sort((a, b) => (a.when > b.when ? 1 : -1)));
  return m;
}

function shiftMonth(n) {
  calCursor = new Date(calCursor.getFullYear(), calCursor.getMonth() + n, 1);
  // keep the selected day inside the viewed month so quick-add pre-fills the
  // month you're looking at, not a stale day from before you navigated.
  const sd = new Date(selDay + "T00:00");
  if (sd.getMonth() !== calCursor.getMonth() || sd.getFullYear() !== calCursor.getFullYear())
    selDay = iso(calCursor);
  render();
}

// ---- keyboard ---------------------------------------------------------------

function focusAdd() {
  const v = document.getElementById(view);
  if (view === "today") {  // today has no add-row — n logs a win
    const win = document.getElementById("today-win");
    if (win) { win.focus(); win.classList.add("flash"); setTimeout(() => win.classList.remove("flash"), 400); }
    return;
  }
  const form = v && v.querySelector(".add");
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

function editSel() {
  if (!["appointments", "achievements", "todos"].includes(view)) return;
  const list = currentList();
  if (sel < 0 || sel >= list.length) return;
  editing = list[sel].id;
  render();
  focusEdit(view);
}

document.addEventListener("keydown", (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  const help = document.getElementById("help");

  // search bar owns its own keys while focused: esc closes+clears, enter applies
  // the filter and drops to the first match so j/k/dd act on the filtered list.
  if (document.activeElement.id === "search-input") {
    if (e.key === "Escape") { closeSearch(); return; }
    if (e.key === "Enter") {
      e.preventDefault();
      document.activeElement.blur();
      if (currentList().length) {
        sel = 0; render();
        document.querySelector(`#${view} .row.sel`)?.scrollIntoView({ block: "nearest" });
      }
      return;
    }
    return;
  }

  if (e.key === "Escape") {
    if (!help.hidden) { help.hidden = true; return; }
    if (editing) { editing = null; render(); return; }
    if (typing) { document.activeElement.blur(); return; }
  }
  if (typing) return;
  if (!help.hidden && e.key !== "?") return;

  // any key other than a follow-up 'd' disarms a pending delete, so moving the
  // selection then pressing 'd' can never delete the wrong (no-longer-armed) row.
  if (e.key !== "d" && pendingDelete) {
    pendingDelete = false;
    document.querySelectorAll(".row.arming").forEach(r => r.classList.remove("arming"));
  }

  switch (e.key) {
    case "1": setView("today"); break;
    case "2": setView("calendar"); break;
    case "3": setView("appointments"); break;
    case "4": setView("achievements"); break;
    case "5": setView("todos"); break;
    case "n": e.preventDefault(); focusAdd(); break;
    case "/": e.preventDefault(); openSearch(); break;
    case "u": undoDelete(); break;
    case "e": e.preventDefault(); editSel(); break;
    case "Enter": e.preventDefault(); editSel(); break;
    case "t": toggleTheme(); break;
    case "r": refresh(); break;
    case "?": help.hidden = !help.hidden; break;
    case "j": if (view === "calendar") moveCalDay(7); else moveSel(1); break;
    case "k": if (view === "calendar") moveCalDay(-7); else moveSel(-1); break;
    case "h": if (view === "calendar") moveCalDay(-1); break;   // prev day (crosses months)
    case "l": if (view === "calendar") moveCalDay(1); break;    // next day (crosses months)
    case "H": if (view === "calendar") shiftMonth(-1); break;   // jump a whole month back
    case "L": if (view === "calendar") shiftMonth(1); break;    // jump a whole month forward
    case "x": toggleSelTodo(); break;
    case "X": if (view === "todos") toggleDoneVisibility(); break;
    case "d": {
      const armed = document.querySelector(`#${view} .row.sel`);
      if (pendingDelete) { deleteSel(); pendingDelete = false; }
      else if (armed) {  // only arm when a deletable row is actually selected
        pendingDelete = true;
        // show the armed state so the second 'd' isn't a blind destructive action
        armed.classList.add("arming");
        setTimeout(() => {
          pendingDelete = false;
          document.querySelectorAll(".row.arming").forEach(r => r.classList.remove("arming"));
        }, 600);
      }
      break;
    }
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

// show/hide the collapsed "done" pile (header chip + the X key share this).
function toggleDoneVisibility() {
  showDone = !showDone; sel = -1;
  try { localStorage.setItem("lp-show-done", showDone ? "1" : "0"); } catch {}
  render();
}

function toggleSelTodo() {
  if (view !== "todos") return;
  const row = document.querySelector("#todos .row.sel");
  if (!row) return;
  const t = state.todos.find(x => x.id === row.dataset.id);
  if (t) patch("todos", t.id, { done: !t.done });
}

async function deleteSel() {
  if (!["appointments", "achievements", "todos"].includes(view)) return;
  const row = document.querySelector(`#${view} .row.sel`);
  if (!row) return;
  const item = currentList().find(x => x.id === row.dataset.id);
  if (!item) return;
  const keep = sel;
  await deleteWithUndo(view, item);
  // keep the selection on the next row (or the new last one) instead of losing it
  sel = Math.min(keep, currentList().length - 1);
  render();
}

// ---- boot + polling ---------------------------------------------------------

function wireBar() {
  document.querySelectorAll(".tab").forEach(t => t.onclick = () => setView(t.dataset.view));
  document.getElementById("theme-btn").onclick = toggleTheme;
  document.getElementById("export-btn").onclick = exportData;
  document.getElementById("help-btn").onclick = () => { const h = document.getElementById("help"); h.hidden = !h.hidden; };
  document.getElementById("help").onclick = (e) => { if (e.target.id === "help") e.target.hidden = true; };
  const si = document.getElementById("search-input");
  si.oninput = () => { search = si.value; sel = -1; render(); };
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
  if (polling || editing || document.visibilityState !== "visible") return;
  polling = true;
  try {
    const { version } = await api("GET", "/api/version");
    if (version !== state.version) await refresh();
  } catch {} finally { polling = false; }
}

wireBar();
setView("today");
refresh();
setInterval(poll, 4000);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") poll(); });
