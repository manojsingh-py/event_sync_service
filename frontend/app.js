// Event Sync frontend. Vanilla JS, no build step.
// All upstream data is untrusted: it is only ever inserted via textContent (never innerHTML).
"use strict";

const state = { tz: "America/New_York", selectedId: null };
const $ = (sel) => document.querySelector(sel);

// ---------- tiny DOM helper ----------
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${await res.text()}`);
  return res.json();
}

// ---------- formatting ----------
function fmtDateTime(iso, withDate = true) {
  if (!iso) return null;
  const opts = { timeZone: state.tz, hour: "2-digit", minute: "2-digit", hour12: false, timeZoneName: "short" };
  if (withDate) Object.assign(opts, { weekday: "short", month: "short", day: "numeric" });
  return new Intl.DateTimeFormat("en-US", opts).format(new Date(iso));
}
function fmtDate(isoDate) {
  if (!isoDate) return "Unknown date";
  const [y, m, d] = isoDate.split("-").map(Number);
  return new Intl.DateTimeFormat("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" }).format(Date.UTC(y, m - 1, d));
}
const FORMAT_LABEL = { in_person: "In person", virtual: "Virtual" };
function fmtValue(field, value) {
  if (value === null || value === undefined || value === "") return null;
  if (field === "start" || field === "end") return fmtDateTime(value);
  if (field === "format") return FORMAT_LABEL[value] || value;
  if (field === "recurring") return value ? "Yes" : "No";
  if (field === "attendees" && typeof value === "number") return `${value} invitee${value === 1 ? "" : "s"}`;
  return String(value);
}
const sourceChip = (source, id) => el("span", { class: `chip ${source}`, title: source === "crm" ? "CRM record" : "Calendar record" }, id || (source === "crm" ? "CRM" : "CAL"));
const sevBadge = (sev, text) => el("span", { class: `sev ${sev}` }, text || sev);

// ---------- stats ----------
function renderStats(s) {
  const items = [
    ["Unified meetings", s.unified_meetings],
    ["In both sources", s.matched_both],
    ["CRM only", s.crm_only],
    ["Calendar only", s.calendar_only],
    ["With conflicts", s.meetings_with_conflicts],
    ["Need review", s.needs_review],
    ["Duplicates merged", s.duplicates_merged],
    ["Data warnings", s.data_quality_by_severity.warning + s.data_quality_by_severity.error],
  ];
  $("#stats").replaceChildren(...items.map(([label, n]) => el("div", { class: "stat" }, el("b", {}, n), el("span", {}, label))));
  $("#sync-info").textContent =
    `${s.source_records.crm} CRM + ${s.source_records.calendar} calendar records → ${s.unified_meetings} meetings · ` +
    `last synced ${new Date(s.synced_at).toLocaleString()} · times shown in ${s.settings.timezone}`;
}

// ---------- list ----------
async function loadList() {
  const params = new URLSearchParams();
  const source = $("#f-source").value;
  if (source) params.set("source", source);
  if ($("#f-conflicts").checked) params.set("conflicts", "true");
  if ($("#f-review").checked) params.set("needs_review", "true");
  const q = $("#f-q").value.trim();
  if (q) params.set("q", q);
  const data = await api(`/api/meetings?${params}`);
  renderRows(data.items);
}

function renderRows(items) {
  $("#empty").hidden = items.length > 0;
  $("#rows").replaceChildren(...items.map((m) => {
    const when = m.start ? fmtDateTime(m.start) : `${fmtDate(m.date)} · time unknown`;
    const sources = [
      ...m.source_ids.crm.map((id) => sourceChip("crm", id)),
      ...m.source_ids.calendar.map((id) => sourceChip("calendar", id)),
    ];
    const conflicts = m.conflict_count
      ? [sevBadge(m.max_severity, `${m.conflict_count}`), el("span", { class: "sub" }, m.conflict_fields.join(", "))]
      : m.max_severity ? [sevBadge("low", "notes"), el("span", { class: "sub" }, m.conflict_fields.join(", "))]
      : [el("span", { class: "sub" }, "—")];
    const row = el("tr", { class: m.id === state.selectedId ? "selected" : null, "data-id": m.id },
      el("td", {}, when),
      el("td", {},
        el("button", { type: "button", class: "row-btn", onclick: () => selectMeeting(m.id), "aria-label": `Show details for ${m.title}` }, m.title),
        el("span", { class: "sub" }, [m.owner, m.category === "internal" ? "internal" : null].filter(Boolean).join(" · ")),
      ),
      el("td", {}, m.client || "—", m.company && m.company !== m.client ? el("span", { class: "sub" }, m.company) : null),
      el("td", {}, m.location || "—", m.format ? el("span", { class: "sub" }, FORMAT_LABEL[m.format]) : null),
      el("td", {}, el("span", { class: `status-pill ${m.status || ""}` }, m.status || "—"), m.needs_review ? el("span", { class: "sub" }, "needs review") : null),
      el("td", {}, sources, m.has_duplicates ? el("span", { class: "sub" }, "duplicate merged") : null),
      el("td", {}, conflicts),
    );
    return row;
  }));
}

// ---------- detail ----------
async function selectMeeting(id) {
  state.selectedId = id;
  document.querySelectorAll("#rows tr").forEach((tr) => tr.classList.toggle("selected", tr.dataset.id === id));
  const m = await api(`/api/meetings/${encodeURIComponent(id)}`);
  renderDetail(m);
  $("#detail").focus();
}

function candidateCell(field, f, source) {
  const cands = f.candidates.filter((c) => c.source === source);
  if (!cands.length) return el("td", { class: "empty-cell" }, "—");
  const cls = f.status === "conflict" ? "conflict" : f.status === "variant" ? "variant" : null;
  return el("td", { class: cls },
    cands.map((c) => el("span", { class: "cand" },
      cands.length > 1 || source === "calendar" ? sourceChip(source, c.source_id) : null,
      fmtValue(field, c.value) ?? el("span", { class: "empty-cell" }, "(empty)"),
      c.note ? el("span", { class: "prov" }, c.note) : null,
    )),
  );
}

const STATUS_TEXT = {
  agreed: "sources agree", single_source: "single source", complementary: "combined from sources",
  variant: "wording differs", conflict: "CONFLICT", missing: "not provided",
};

function fieldsTable(m) {
  const order = ["title", "start", "end", "status", "format", "location", "category", "client", "company", "owner", "recurring", "crm_notes", "calendar_description"];
  const hasCrm = m.sources.includes("crm"), hasCal = m.sources.includes("calendar");
  return el("table", { class: "fields" },
    el("caption", { class: "sr-only" }, "Field-level comparison between sources"),
    el("thead", {}, el("tr", {},
      el("th", { scope: "col" }, "Field"),
      el("th", { scope: "col" }, "Unified value"),
      hasCrm ? el("th", { scope: "col" }, sourceChip("crm"), " value") : null,
      hasCal ? el("th", { scope: "col" }, sourceChip("calendar"), " value") : null,
    )),
    el("tbody", {}, order.filter((k) => m.fields[k]).map((k) => {
      const f = m.fields[k];
      return el("tr", {},
        el("td", {}, f.label),
        el("td", { class: `unified ${f.status === "conflict" ? "conflict" : ""}` },
          fmtValue(k, f.value) ?? el("span", { class: "empty-cell" }, "—"),
          el("span", { class: "prov" },
            f.status === "conflict" ? sevBadge("high", "conflict") : null,
            f.source_id ? ["from ", sourceChip(f.source, f.source_id), ` · ${STATUS_TEXT[f.status] || f.status}`] : STATUS_TEXT[f.status] || f.status,
          ),
          f.rule && f.status !== "missing" ? el("span", { class: "prov" }, `Rule: ${f.rule}`) : null,
        ),
        hasCrm ? candidateCell(k, f, "crm") : null,
        hasCal ? candidateCell(k, f, "calendar") : null,
      );
    })),
  );
}

function renderDetail(m) {
  const f = m.fields;
  const parts = [
    el("h2", {}, f.title.value),
    el("p", { class: "muted" }, f.start.value ? fmtDateTime(f.start.value) : `${fmtDate(m.date)} · time unknown`,
      f.end.value ? ` – ${fmtDateTime(f.end.value, false)}` : ""),
    el("p", {}, "Records: ", m.source_ids.crm.map((id) => sourceChip("crm", id)), m.source_ids.calendar.map((id) => sourceChip("calendar", id)),
      m.sources.length === 1 ? el("span", { class: "sub" }, `Only found in ${m.sources[0] === "crm" ? "CRM" : "Calendar"}`) : null),
    m.needs_review
      ? el("div", { class: "banner review", role: "status" }, "Needs review: ", m.review_reasons.join("; "))
      : el("div", { class: "banner clean", role: "status" }, "No blocking conflicts."),
  ];

  if (m.conflicts.length) {
    parts.push(el("h3", {}, `Conflicts (${m.conflicts.length})`),
      el("ul", { class: "plain" }, m.conflicts.map((c) => el("li", {}, sevBadge(c.severity), el("b", {}, `${c.field}: `), c.message))));
  }

  parts.push(el("h3", {}, "Field provenance"), fieldsTable(m));

  const people = f.attendees.value || [];
  parts.push(el("h3", {}, `People (${people.length})`),
    people.length ? el("ul", { class: "plain" }, people.map((p) => el("li", {},
      p.name, p.email ? el("span", { class: "sub" }, p.email) : null,
      p.unresolved ? sevBadge("medium", "unresolved") : p.internal ? sevBadge("low", "internal") : sevBadge("ok", "external"),
      " ", p.sources.map((id) => sourceChip(id.startsWith("CRM") ? "crm" : "calendar", id)),
    ))) : el("p", { class: "muted" }, "Nobody listed in any source."));

  if (m.match) {
    parts.push(el("h3", {}, `Why these records were matched (score ${m.match.score.toFixed(2)}, ${m.match.confidence} confidence)`),
      ...Object.entries(m.match.components).map(([k, v]) => el("div", { class: "meter" },
        el("span", {}, k), el("progress", { max: "1", value: String(v), "aria-label": `${k} similarity` }), el("span", {}, v.toFixed(2)))),
      el("ul", { class: "plain" }, m.match.reasons.map((r) => el("li", {}, r))));
  }

  if (m.possible_matches.length) {
    parts.push(el("h3", {}, "Near misses (below match threshold)"),
      el("ul", { class: "plain" }, m.possible_matches.map((p) => el("li", {}, p.source_ids.join(" + "), ` — score ${p.score.toFixed(2)}`,
        el("span", { class: "sub" }, p.reasons.join(" · "))))));
  }

  for (const d of m.duplicates) {
    parts.push(el("h3", {}, `Duplicate ${d.source} records merged`),
      el("p", {}, "Kept ", sourceChip(d.source, d.kept), " merged ", d.merged.map((id) => sourceChip(d.source, id))),
      el("p", { class: "muted" }, d.rule),
      el("ul", { class: "plain" }, d.links.map((l) => el("li", {}, `${l.a} ↔ ${l.b}: score ${l.score.toFixed(2)}`, el("span", { class: "sub" }, l.reasons.join(" · "))))));
  }

  if (m.data_quality.length) {
    parts.push(el("h3", {}, `Data quality (${m.data_quality.length})`),
      el("ul", { class: "plain" }, m.data_quality.map((i) => el("li", {},
        sevBadge(i.severity === "error" ? "high" : i.severity === "warning" ? "medium" : "low", i.severity),
        sourceChip(i.source, i.source_id), el("b", {}, ` ${i.field}: `), i.message))));
  }

  parts.push(el("h3", {}, "Raw source records"),
    ...m.records.map((r) => el("details", {}, el("summary", {}, sourceChip(r.source, r.source_id)), el("pre", {}, JSON.stringify(r.raw, null, 2)))));

  $("#detail").replaceChildren(...parts);
}

// ---------- wiring ----------
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

async function refreshAll() {
  const stats = await api("/api/stats");
  state.tz = stats.settings.timezone;
  renderStats(stats);
  await loadList();
  if (state.selectedId) {
    try { await selectMeeting(state.selectedId); } catch { state.selectedId = null; }
  }
}

function showError(err) {
  $("#sync-info").textContent = `Error: ${err.message}`;
}

$("#f-source").addEventListener("change", () => loadList().catch(showError));
$("#f-conflicts").addEventListener("change", () => loadList().catch(showError));
$("#f-review").addEventListener("change", () => loadList().catch(showError));
$("#f-q").addEventListener("input", debounce(() => loadList().catch(showError), 200));
$("#resync").addEventListener("click", async () => {
  $("#resync").disabled = true;
  try { await api("/api/sync", { method: "POST" }); await refreshAll(); } catch (e) { showError(e); }
  finally { $("#resync").disabled = false; }
});

refreshAll().catch(showError);
