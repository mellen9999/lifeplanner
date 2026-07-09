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
  { value: "daily:2", label: "every other day" },
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
let hmYear = new Date().getFullYear();  // which year the wins heatmap shows
let pendingDelete = false;    // first 'd' of 'dd'
let armedDelete = null;       // id of the row whose × is armed; needs a 2nd click
let armedTimer = null;        // auto-disarm so a stale armed × can't linger
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
// ---- compact, human date/time for listings (mobile-first: short = title gets room)
// "2pm" / "2:30pm" from a "HH:MM" string
function fmtTime12(t) {
  if (!t) return "";
  let [h, m] = t.split(":").map(Number);
  const ap = h < 12 ? "am" : "pm";
  h = h % 12 || 12;
  return m ? `${h}:${pad(m)}${ap}` : `${h}${ap}`;
}
// "2–3pm" / "2pm" / "" (all-day) — time only, no date
function fmtTimeRange(when, end) {
  const t = timeOf(when);
  if (!t) return "";
  const endT = (end && end.length > 10) ? timeOf(end) : "";
  return endT ? `${fmtTime12(t)}–${fmtTime12(endT)}` : fmtTime12(t);
}
// relative for near days ("today"/"tomorrow"/"yesterday"), else "tue jun 16";
// year shown only when it isn't the current year. drops the noisy YYYY-MM- prefix.
function fmtDateShort(when) {
  if (!when) return "";
  const d = new Date(when.length <= 10 ? when + "T00:00" : when);
  if (isNaN(d)) return when;
  const today = new Date(todayIso() + "T00:00");
  const diff = Math.round((new Date(iso(d) + "T00:00") - today) / 86400000);
  if (diff === 0) return "today";
  if (diff === 1) return "tomorrow";
  if (diff === -1) return "yesterday";
  const base = `${DOW[(d.getDay() + 6) % 7]} ${MONTHS[d.getMonth()].slice(0, 3)} ${d.getDate()}`;
  return d.getFullYear() === today.getFullYear() ? base : `${base} ${d.getFullYear()}`;
}
// date + time for the cross-day appointments list, e.g. "tue jun 16 · 2–3pm"
function fmtWhenList(when, end) {
  const ds = fmtDateShort(when);
  const tr = fmtTimeRange(when, end);
  return tr ? `${ds} · ${tr}` : ds;
}

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
  } else if (r.freq === "daily") base = iv === 1 ? "every day" : iv === 2 ? "every other day" : `every ${iv} days`;
  else base = iv === 1 ? "monthly" : `every ${iv} months`;
  return r.until ? `${base} · until ${r.until}` : base;
}

// label for a routine: plain daily ones just read "routine" (cleaner than "every
// day"); anything with a real cadence (every other day, weekly, until-date) shows it,
// so a weekly weigh-in isn't mistaken for a daily.
function routineLabel(t) {
  const r = t.recur;
  if (r && r.freq === "daily" && (r.interval || 1) === 1 && !r.until) return "routine";
  return recurLabel(r, t.due);
}

// recurring todos (routines) are completed per-day; one-off todos use a global flag.
// these mirror store.todo_done_on / todo_occurrences (anchor = the todo's `due`).
function todoDoneOn(t, dateIso) { return t.recur ? (t.done_dates || []).includes(dateIso) : !!t.done; }
function todoOccursOn(t, dateIso) {
  return t.recur ? apptOccurrences({ when: t.due, recur: t.recur }, dateIso, dateIso).length > 0
    : t.due === dateIso;
}
// toggle completion for the relevant date: a routine ticks that one day, a one-off flips.
function toggleTodo(t, dateIso) {
  if (t.recur) patch("todos", t.id, { done: !todoDoneOn(t, dateIso), date: dateIso });
  else patch("todos", t.id, { done: !t.done });
}

// urgency by deadline pressure (drives row colour): red = due today/overdue,
// yellow = due soon (1-3 days), peaceful = lots of runway (4+ days) or undated.
// routines aren't deadlines, so they get their own neutral tier.
function todoUrgency(t) {
  if (t.recur) return "routine";
  if (!t.due) return "peaceful";
  const days = dayDiff(todayIso(), t.due);
  return days <= 0 ? "red" : days <= 3 ? "yellow" : "peaceful";
}
const URG_RANK = { red: 0, yellow: 1, routine: 2, peaceful: 3 };
// a one-off earns a place on TODAY when it's actionable now — overdue/soon (≤3d)
// or undated ("anytime"). a far-future deadline stays parked off today until it nears.
function todoOnToday(t) { return !t.due || dayDiff(todayIso(), t.due) <= 3; }
// the todos page order: open before done, most-urgent first, then by due date.
function orderedTodos() {
  // open before done, then by urgency tier, then manual order (0 = unset → end),
  // then due. manual order is what gives routines a logical day-sequence.
  return applySearch(visibleTodos()).slice().sort((a, b) =>
    ((a.done ? 1 : 0) - (b.done ? 1 : 0))
    || (URG_RANK[todoUrgency(a)] - URG_RANK[todoUrgency(b)])
    || ((a.order || 1e9) - (b.order || 1e9))
    || ((a.due || "9999") > (b.due || "9999") ? 1 : -1));
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
// arm-then-confirm the × delete: first click highlights "delete?", second click
// (or Enter) within 3s actually deletes. clicking another row's × re-arms that one.
function armDelete(entity, item) {
  if (armedTimer) { clearTimeout(armedTimer); armedTimer = null; }
  if (armedDelete === item.id) {       // second click → confirm
    armedDelete = null;
    deleteWithUndo(entity, item);
    return;
  }
  armedDelete = item.id;               // first click → arm + auto-disarm timer
  armedTimer = setTimeout(() => { armedDelete = null; armedTimer = null; render(); }, 3000);
  render();
}

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
  armedDelete = null;
  // a filter belongs to the list it was typed in — don't carry it across views
  closeSearch(false);
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.view === v));
  document.querySelectorAll(".view").forEach(s => s.classList.toggle("active", s.id === v));
  // reflect the view in the url so it's deep-linkable / bookmarkable and a
  // refresh (or the installed pwa) reopens where you were. guarded: the hashchange
  // listener no-ops when the hash already matches the current view.
  if (location.hash.slice(1) !== v) location.hash = v;
  if (v === "today") { refreshPlannerData().then(render); } else { render(); }
}

function currentList() {
  if (view === "achievements") return applySearch(state.achievements);
  if (view === "todos") return orderedTodos();
  if (view === "appointments") { const o = orderedAppointments(applySearch(state.appointments)); return [...o.up, ...o.past]; }
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

// one-tap relative date setters next to a date input — so "in 2 days" is a tap,
// not a calendar hunt (matters most on the phone, where there's no claude to parse
// "before thursday"). the native picker stays for exact dates.
function dateChips(input) {
  const wrap = el("div", "datechips");
  [["today", 0], ["tmrw", 1], ["+2d", 2], ["+3d", 3], ["+4d", 4], ["+1wk", 7], ["✕", null]].forEach(([label, n]) => {
    const b = el("button", null, label); b.type = "button";
    b.title = n === null ? "clear date" : (n === 0 ? "due today" : `due in ${n} day${n > 1 ? "s" : ""}`);
    b.onclick = () => { input.value = n === null ? "" : addDays(todayIso(), n); };
    wrap.appendChild(b);
  });
  return wrap;
}

function addRow(fields, onSubmit) {
  const form = el("form", "add");
  const inputs = {};
  fields.forEach(f => {
    const i = makeField(f);
    inputs[f.name] = i;
    form.appendChild(i);
    if (f.chips) form.appendChild(dateChips(i));  // relative quick-set next to the field
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

// pointer-events drag-reorder for routine rows — works with mouse AND touch (html5
// drag doesn't do touch). live-reorders the dom as you drag, persists on drop. only
// routine rows ([data-routine]) participate, so they stay a contiguous block.
let dragging = false;
function makeRoutinesSortable(list) {
  const rows = () => [...list.querySelectorAll('.row[data-routine]')];
  list.querySelectorAll('.drag-handle').forEach(h => {
    const dragEl = () => h.closest('.row');
    let el0 = null;
    h.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      el0 = dragEl(); dragging = true;
      el0.classList.add('dragging');
      try { h.setPointerCapture(e.pointerId); } catch {}
    });
    h.addEventListener('pointermove', (e) => {
      if (!el0) return;
      const others = rows().filter(r => r !== el0);
      const after = others.find(r => {
        const b = r.getBoundingClientRect();
        return e.clientY < b.top + b.height / 2;
      });
      if (after) list.insertBefore(el0, after);
      else { const last = others[others.length - 1]; if (last) list.insertBefore(el0, last.nextSibling); }
    });
    const end = async (e) => {
      if (!el0) return;
      el0.classList.remove('dragging');
      try { h.releasePointerCapture(e.pointerId); } catch {}
      const ids = rows().map(r => r.dataset.id);
      el0 = null;
      await api('POST', '/api/todos/reorder', { ids });
      dragging = false;
      await refresh();
    };
    h.addEventListener('pointerup', end);
    h.addEventListener('pointercancel', () => { if (el0) { el0.classList.remove('dragging'); el0 = null; } dragging = false; });
  });
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
  if (opts.dragHandle) {  // grip to drag-reorder (routines) — leftmost on the row
    const g = el("span", "drag-handle", "⠿");
    g.title = "drag to reorder";
    g.onclick = (e) => e.stopPropagation();
    row.appendChild(g);
  }
  if (opts.tick) {
    const dd = opts.tickDate || todayIso();  // which day's completion this tick toggles
    row.appendChild(tickEl(todoDoneOn(item, dd), () => toggleTodo(item, dd)));
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
  // two-step delete: first click arms (× → "delete?"), second click within the
  // window confirms. no more one-click-and-it's-gone; undo (u) is still a backstop.
  const armed = armedDelete === item.id;
  const del = el("span", "del" + (armed ? " armed" : ""), armed ? "delete?" : "×");
  del.title = armed ? "click again to delete" : "delete";
  del.setAttribute("role", "button");
  del.setAttribute("aria-label", armed ? "confirm delete" : "delete");
  del.tabIndex = 0;
  const doDel = (e) => { e.stopPropagation(); armDelete(entity, item); };
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
    { name: "endtime", type: "time", value: timeOf(item.end) },
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
    { name: "repeat", type: "select", value: repeatValue(item.recur), options: REPEAT_OPTIONS },
    { name: "until", type: "date", value: (item.recur && item.recur.until) || "" },
  ];
}

function buildPatch(entity, v) {
  if (entity === "appointments")
    return { title: v.title, when: v.time ? `${v.date} ${v.time}` : v.date, end: v.endtime ? `${v.date} ${v.endtime}` : "", location: v.location, recur: parseRepeat(v.repeat, v.until) };
  if (entity === "achievements")
    return { title: v.title, date: v.date, note: v.note };
  return { title: v.title, due: v.due, recur: parseRepeat(v.repeat, v.until) };
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

  if (d.routine_total) {
    const rEl = el("span", "pp-recap-item");
    rEl.appendChild(el("span", "pp-recap-n", `${d.routine_completions ?? 0}/${d.routine_total}`));
    rEl.appendChild(el("span", "pp-recap-l", " routines"));
    row.appendChild(rEl);
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
    ? appts.map(a => agendaLine("appt", (timeOf(a.when) ? fmtTimeRange(a.when, a.end) + "  " : "") + a.title, a.location))
    : [el("div", "muted small", "nothing scheduled")]));

  // today's actionable one-offs (overdue / soon / anytime — far-future stays parked),
  // most-urgent first and colour-coded, then today's routines. this is the "what's
  // next" list: the top red item is literally the next thing to do.
  const actionable = state.todos.filter(x => !x.recur && !x.done && todoOnToday(x))
    .sort((a, b) => (URG_RANK[todoUrgency(a)] - URG_RANK[todoUrgency(b)])
      || ((a.due || "9999") > (b.due || "9999") ? 1 : -1));
  const routines = state.todos.filter(x => x.recur && todoOccursOn(x, t))
    .sort((a, b) => (a.order || 1e9) - (b.order || 1e9));  // logical day-order
  const todoLines = [];
  actionable.forEach(x => todoLines.push(agendaTodo(x,
    !x.due ? "anytime" : x.due < t ? `overdue · ${x.due}` : x.due === t ? "due today" : `due ${x.due}`)));
  routines.forEach(x => todoLines.push(agendaTodo(x, routineLabel(x))));
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
  const ti = todayIso();
  const doneNow = todoDoneOn(x, ti);  // routine → done today; one-off → global flag
  const li = el("div", "li urg-" + todoUrgency(x) + (doneNow ? " done" : ""));
  li.appendChild(tickEl(doneNow, () => toggleTodo(x, ti)));
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

// a full calendar-year wins heatmap you can page back through, year by year — so
// the whole history is visible forever, not just a rolling 6-month window. data is
// kept indefinitely, so any past year graphs exactly as it happened.
function renderHeatmap() {
  const counts = winCounts();
  const today = new Date();
  const curYear = today.getFullYear();
  const wrap = el("div", "heatmap");

  // ◀ year ▶ + that year's total
  let yearTotal = 0;
  for (const ds in counts) if (ds.slice(0, 4) === String(hmYear)) yearTotal += counts[ds];
  const head = el("div", "hm-head");
  const prev = el("button", "hm-nav", "◀"); prev.title = "previous year";
  prev.onclick = () => { hmYear--; render(); };
  const next = el("button", "hm-nav", "▶"); next.title = "next year";
  next.disabled = hmYear >= curYear;
  next.onclick = () => { if (hmYear < curYear) { hmYear++; render(); } };
  head.append(prev, el("span", "hm-year", String(hmYear)), next,
    el("span", "hm-total", `${yearTotal} win${yearTotal === 1 ? "" : "s"}`));
  wrap.appendChild(head);

  // columns = weeks (mon–sun) spanning jan 1 → dec 31 of hmYear; days outside the
  // year are padding so the calendar lines up. a month-label row sits on top.
  const jan1 = new Date(hmYear, 0, 1);
  const start = new Date(jan1);
  start.setDate(jan1.getDate() - ((jan1.getDay() + 6) % 7)); // back to that week's monday
  const dec31 = new Date(hmYear, 11, 31);
  const months = el("div", "hm-months");
  const grid = el("div", "hm-grid");
  let lastMonth = -1;
  for (let wk = 0; ; wk++) {
    const colStart = new Date(start); colStart.setDate(start.getDate() + wk * 7);
    if (colStart > dec31) break;
    const lbl = el("div", "hm-mlabel");
    if (colStart.getFullYear() === hmYear && colStart.getMonth() !== lastMonth) {
      lbl.textContent = MONTHS[colStart.getMonth()].slice(0, 3); lastMonth = colStart.getMonth();
    }
    months.appendChild(lbl);
    const col = el("div", "hm-col");
    for (let dN = 0; dN < 7; dN++) {
      const dt = new Date(colStart); dt.setDate(colStart.getDate() + dN);
      if (dt.getFullYear() !== hmYear) { col.appendChild(el("div", "hm pad")); continue; }
      const ds = iso(dt);
      const c = counts[ds] || 0;
      const cell = el("div", "hm lvl" + Math.min(3, c));
      if (dt > today) cell.classList.add("future");
      cell.title = `${ds}: ${c} win${c === 1 ? "" : "s"}`;
      col.appendChild(cell);
    }
    grid.appendChild(col);
  }
  wrap.setAttribute("role", "img");
  wrap.setAttribute("aria-label", `wins in ${hmYear}: ${yearTotal} total`);
  wrap.append(months, grid);
  return wrap;
}

// all-time remembrance line: totals that never reset, so the long arc is visible.
function allTimeStats() {
  const counts = winCounts();
  const dates = Object.keys(counts).sort();
  const total = state.achievements.length;
  const activeDays = dates.length;
  const since = dates[0];
  const row = el("div", "alltime");
  const add = (n, l) => { const s = el("span", "at-item"); s.append(el("span", "at-n", String(n)), el("span", "at-l", " " + l)); row.appendChild(s); };
  add(total, "wins all-time");
  add(activeDays, "active days");
  if (since) { const s = el("span", "at-item"); s.append(el("span", "at-l", "since "), el("span", "at-n", since)); row.appendChild(s); }
  return row;
}

// ---- appointments / achievements / todos -----------------------------------

// the full appointment add form — identical controls everywhere it's offered
// (the appointments page AND the calendar day panel), pre-filled to a day. one
// definition so the calendar's add can never drift from the appointments page's.
function appointmentAddForm(defaultWhen) {
  return addRow([
    { name: "title", ph: "what", cls: "title" },
    { name: "when", type: "date", value: defaultWhen },
    { name: "time", type: "time" },
    { name: "endtime", type: "time", ph: "to" },
    { name: "location", ph: "where (optional)" },
    { name: "repeat", type: "select", options: REPEAT_OPTIONS },
    { name: "until", type: "date" },
  ], d => add("appointments", {
    title: d.title, when: d.time ? `${d.when} ${d.time}` : d.when,
    end: d.endtime ? `${d.when} ${d.endtime}` : "",
    location: d.location, recur: parseRepeat(d.repeat, d.until),
  }));
}

// split appointments into upcoming (next occurrence today-or-later, soonest
// first) and past (one-time, already gone, most recent first). recurring series
// resolve to their next hit. ONE ordering, shared by the view + keyboard nav so
// j/k always lands on the row you see.
function orderedAppointments(list) {
  const today = todayIso();
  const keyOf = a => {
    const rec = a.recur && a.recur.freq;
    return (rec ? (nextOccurrence(a, today) || a.when) : a.when) || "";
  };
  const up = [], past = [];
  list.forEach(a => (keyOf(a) >= today ? up : past).push(a));
  up.sort((x, y) => keyOf(x) < keyOf(y) ? -1 : 1);
  past.sort((x, y) => keyOf(x) > keyOf(y) ? -1 : 1);
  return { up, past };
}

function apptRow(a) {
  const rec = recurLabel(a.recur, (a.when || "").slice(0, 10));
  const shown = rec ? (nextOccurrence(a, todayIso()) || a.when) : a.when;
  const sub = [a.location, rec].filter(Boolean).join("  ·  ");
  return listRow(a, "appointments", [
    el("span", "when", fmtWhenList(shown, a.end)),
    buildBody(a.title, sub),
  ]);
}

function renderAppointments() {
  const root = document.getElementById("appointments");
  clear(root);
  const h = el("h2", "section-h", "appointments");
  h.appendChild(el("span", "count", `${state.appointments.length}`));
  root.appendChild(h);
  root.appendChild(appointmentAddForm(selDay));

  const { up, past } = orderedAppointments(applySearch(state.appointments));
  const body = el("div");
  if (!up.length && !past.length) {
    body.appendChild(el("div", "empty", "no appointments. press n to add one."));
    root.appendChild(body);
    return;
  }
  // one flat list with group headers; the running index mirrors currentList()
  // ([...up, ...past]) so the selection model and keyboard nav stay in sync.
  const list = el("div", "list");
  let i = 0;
  const group = (items, label) => {
    if (!items.length) return;
    list.appendChild(el("div", "grp-h", label));
    items.forEach(a => {
      const idx = i++;
      const row = apptRow(a);
      if (idx === sel && a.id !== editing) row.classList.add("sel");
      if (a.id !== editing) row.onclick = () => { sel = idx; render(); };
      list.appendChild(row);
    });
  };
  group(up, "upcoming");
  group(past, "past");
  body.appendChild(list);
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
  root.appendChild(allTimeStats());
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
    { name: "due", type: "date", chips: true },
    { name: "repeat", type: "select", options: REPEAT_OPTIONS },
    { name: "until", type: "date" },
  ], d => add("todos", { title: d.title, due: d.due, recur: parseRepeat(d.repeat, d.until) })));
  const body = el("div");
  const ti = todayIso();
  // sorted most-urgent-first, with a colour bar per row (red/yellow/peaceful) so the
  // top of the list literally is "what's next". everything shows here (the page is
  // the full picture); the today view is the filtered actionable subset.
  mountList(body, orderedTodos(), (t) => {
    const shown = t.recur ? (nextOccurrence({ when: t.due, recur: t.recur }, ti) || t.due) : t.due;
    const sub = t.recur ? routineLabel(t)
      : (t.done ? (t.done_at ? `done ${t.done_at}` : "done")
        : (t.due && t.due < ti ? "overdue" : ""));
    const row = listRow(t, "todos", [
      el("span", "when", shown ? shown.slice(0, 10) : "—"),
      buildBody(t.title, sub),
    ], { tick: true, tickDate: ti, done: t.recur ? todoDoneOn(t, ti) : t.done, dragHandle: t.recur });
    if (!t.done) row.classList.add("urg-" + todoUrgency(t));
    if (t.recur) row.dataset.routine = "1";
    return row;
  }, "nothing to do. press n to add.");
  const listEl = body.querySelector(".list");
  if (listEl) makeRoutinesSortable(listEl);
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
    .forEach(w => items.push(["appt", `${timeOf(w) ? fmtTimeRange(w, a.end) + " " : ""}${a.title}`.trim()])));
  state.todos.forEach(t => { if (todoOccursOn(t, selDay))
    items.push(["todo", (todoDoneOn(t, selDay) ? "✓ " : "") + t.title]); });
  state.achievements.filter(a => a.date === selDay)
    .forEach(a => items.push(["ach", a.title]));
  if (!items.length) panel.appendChild(el("div", "muted small", "nothing on this day"));
  items.forEach(([kind, text]) => {
    const li = el("div", "li");
    li.appendChild(el("span", "mk " + kind));
    li.appendChild(el("span", null, text));
    panel.appendChild(li);
  });
  // full controls right where you're looking — time, place, repeat, end-date —
  // pre-filled to the clicked day. same form as the appointments page.
  panel.appendChild(appointmentAddForm(selDay));
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
    get(w.slice(0, 10)).appts.push({ when: w, time: fmtTimeRange(w, a.end), title: a.title })));
  state.todos.forEach(t => {
    if (t.recur) apptOccurrences({ when: t.due, recur: t.recur }, from, to)
      .forEach(w => { const ds = w.slice(0, 10); get(ds).todos.push({ ...t, due: ds, done: todoDoneOn(t, ds) }); });
    else if (t.due) get(t.due).todos.push(t);
  });
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
    if (armedDelete) { armedDelete = null; if (armedTimer) clearTimeout(armedTimer); render(); return; }
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
  if (t) toggleTodo(t, todayIso());  // routine → toggles today; one-off → flips
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
  // a background refresh repaints the active view, replacing its DOM — so never
  // poll while the user is mid-input. `editing` guards edit rows; this guards the
  // quick win-logger and every add-form (none of which set `editing`), which would
  // otherwise lose focus + their half-typed text the instant a poll lands.
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
  if (polling || editing || dragging || typing || document.visibilityState !== "visible") return;
  polling = true;
  try {
    const { version } = await api("GET", "/api/version");
    if (version !== state.version) await refresh();
  } catch {} finally { polling = false; }
}

wireBar();
const boot = location.hash.slice(1);
setView(VIEWS.includes(boot) ? boot : "today");
refresh();
setInterval(poll, 4000);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") poll(); });
// back/forward or a hand-typed #view switches the section
window.addEventListener("hashchange", () => {
  const v = location.hash.slice(1);
  if (VIEWS.includes(v) && v !== view) setView(v);
});
