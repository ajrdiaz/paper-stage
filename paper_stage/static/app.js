"use strict";

/* =====================================================================
   Teatrito de Papel — interfaz web (sin dependencias ni compilación)
   ===================================================================== */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const view = $("#view");

const STATUS = {
  queued: "En cola", running: "Produciendo", done: "Listo",
  failed: "Falló", cancelled: "Cancelado", interrupted: "Interrumpido",
};
const ACTIVE = new Set(["queued", "running"]);
const LANG = { es: "Español", en: "English" };
const PREFS_KEY = "paper-stage:prefs";
const MAX_HASHTAGS = 5; // límite de TikTok
const AGES = ["3-5", "4-6", "6-9", "8-11", "10-12"];

let CONFIG = null;
let CHARACTERS = [];       // personajes; cada vista que los usa los vuelve a cargar
let cleanup = [];          // funciones a ejecutar al cambiar de vista
const watched = new Map(); // job id → estado conocido (para avisos al terminar)

/* ------------------------------------------------------------------ utilidades */

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, options = {}) {
  const init = { headers: {}, ...options };
  if (init.body && typeof init.body !== "string") {
    init.body = JSON.stringify(init.body);
    init.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, init);
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) throw new Error(data?.error || `Error ${res.status}`);
  return data;
}

function fmtTime(seconds) {
  if (seconds == null || isNaN(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return h ? `${h} h ${String(m).padStart(2, "0")} min` : m ? `${m} min ${String(r).padStart(2, "0")} s` : `${r} s`;
}
const fmtClock = t => `${Math.floor(t / 60)}:${(t % 60).toFixed(1).padStart(4, "0")}`;
const fmtCost = usd => (usd ? `$${usd.toFixed(2)}` : "$0.00");
const fmtDate = ts => new Date(ts * 1000).toLocaleString("es", { dateStyle: "medium", timeStyle: "short" });
const fmtSize = b => (b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1e3))} KB`);

function pill(status) {
  return `<span class="pill ${esc(status)}">${esc(STATUS[status] || status)}</span>`;
}
// Junto al estado de producción: si el video ya está en TikTok (por Buffer o subido a mano).
const tiktokPill = state => (state === "publicado" || state === true ? `<span class="pill tiktok">En TikTok</span>`
  : state === "programado" ? `<span class="pill tiktok soon">Programado en TikTok</span>` : "");

function toast(html, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = html;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), 6000);
}

function modal(title, html) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = html;
  $("#modal").showModal();
}

function confirmAction(message) {
  return window.confirm(message);
}

function loadPrefs() {
  try { return JSON.parse(localStorage.getItem(PREFS_KEY)) || {}; } catch { return {}; }
}
function savePrefs(prefs) {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch { /* sin almacenamiento */ }
}

function onCleanup(fn) { cleanup.push(fn); }

/* ------------------------------------------------------------------ personajes (datos) */

async function loadCharacters() {
  CHARACTERS = await api("/api/characters");
  return CHARACTERS;
}
const charById = id => CHARACTERS.find(c => c.id === id);
// Los trabajos anteriores a los personajes no guardan ninguno: son de Lía.
const reqCharacter = req => req?.personaje?.id ? req.personaje : { id: CONFIG.default_character, nombre: "Lía" };
const charName = id => charById(id)?.nombre || (id === CONFIG.default_character ? "Lía" : id);
const voiceLabel = v => `${v} · ${v[1] === "m" ? "masculina" : "femenina"}`;
const ctaKey = (pid, lang) => `cta_${pid}_${lang}`;

function savedCta(pid, lang) {
  const prefs = loadPrefs();
  // Antes de los personajes, la frase se guardaba solo por idioma (y era de Lía).
  return prefs[ctaKey(pid, lang)] ?? (pid === CONFIG.default_character ? prefs[`cta_${lang}`] : "") ?? "";
}

function swatches(paleta) {
  return `<span class="swatches" aria-hidden="true">${(paleta || []).map(c => `<i style="background:${esc(c)}"></i>`).join("")}</span>`;
}

// Portada de un personaje sin videos: sus colores en tiras de papel y su inicial.
function charArt(c) {
  const [a = "#E9B949", b = "#E8736B", d = "#3FA796"] = c.paleta || [];
  return `<span class="char-art" style="background:linear-gradient(160deg, ${esc(a)} 0 45%, ${esc(b)} 45% 72%, ${esc(d)} 72%)">
    <span>${esc((c.nombre || "?").slice(0, 1))}</span></span>`;
}

function poll(fn, ms) {
  let stopped = false, timer = null;
  const tick = async () => {
    if (stopped) return;
    try { await fn(); } catch (err) { console.warn(err); }
    if (!stopped) timer = setTimeout(tick, ms);
  };
  tick();
  onCleanup(() => { stopped = true; clearTimeout(timer); });
}

/* ------------------------------------------------------------------ markdown mínimo */

function inlineMd(text) {
  return esc(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
}

function markdown(src) {
  const lines = String(src || "").replace(/\r/g, "").split("\n");
  const out = [];
  let i = 0, para = [];
  const flush = () => { if (para.length) { out.push(`<p>${inlineMd(para.join(" "))}</p>`); para = []; } };
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      flush();
      const code = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
      out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
      i++;
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { flush(); const n = Math.min(3, h[1].length); out.push(`<h${n}>${inlineMd(h[2])}</h${n}>`); i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(line) && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1] || "")) {
      flush();
      const cells = l => l.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
      out.push(`<table><thead><tr>${head.map(c => `<th>${inlineMd(c)}</th>`).join("")}</tr></thead><tbody>${
        rows.map(r => `<tr>${r.map(c => `<td>${inlineMd(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
      continue;
    }
    const li = line.match(/^\s*([-*]|\d+[.)])\s+(.*)$/);
    if (li) {
      flush();
      const ordered = /\d/.test(li[1]);
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(/^\s*([-*]|\d+[.)])\s+(.*)$/);
        if (m) { items.push(m[2]); i++; }
        else if (/^\s{2,}\S/.test(lines[i]) && items.length) { items[items.length - 1] += " " + lines[i].trim(); i++; }
        else break;
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag}>${items.map(it => {
        const task = it.match(/^\[([ xX✓])\]\s*(.*)$/);
        return task
          ? `<li class="task"><input type="checkbox" disabled ${task[1] !== " " ? "checked" : ""}>${inlineMd(task[2])}</li>`
          : `<li>${inlineMd(it)}</li>`;
      }).join("")}</${tag}>`);
      continue;
    }
    if (/^>\s?/.test(line)) { flush(); out.push(`<p class="muted">${inlineMd(line.replace(/^>\s?/, ""))}</p>`); i++; continue; }
    if (!line.trim()) { flush(); i++; continue; }
    para.push(line.trim());
    i++;
  }
  flush();
  return `<div class="md">${out.join("")}</div>`;
}

/* ------------------------------------------------------------------ avisos globales */

async function refreshBanner() {
  const parts = [];
  if (CONFIG?.demo) {
    parts.push(`<div class="banner"><div><strong>Modo demostración.</strong> No se llama a Claude ni se gasta crédito; los videos son de ejemplo para probar la interfaz.</div></div>`);
  } else {
    try {
      const health = await api("/api/health");
      if (!health.ok) {
        parts.push(`<div class="banner warn"><div><strong>Faltan dependencias locales.</strong> El agente no podrá terminar el video. <a href="#/sistema">Ver detalles</a></div></div>`);
      }
    } catch { /* sin datos de salud */ }
  }
  $("#banner").innerHTML = parts.join("");
}

function notifyFinished(job) {
  const title = esc(job.request?.tema || job.slug);
  if (job.status === "done") {
    toast(`🎬 <strong>${title}</strong> está listo. <a href="#/video/${esc(job.slug)}">Ver video</a>`, "ok");
  } else {
    toast(`<strong>${title}</strong>: ${esc(STATUS[job.status])}. <a href="#/trabajo/${esc(job.id)}">Ver detalles</a>`, "err");
  }
  if (document.hidden && "Notification" in window && Notification.permission === "granted") {
    new Notification("Teatrito de Papel", {
      body: `${job.request?.tema || job.slug}: ${STATUS[job.status]}`,
    });
  }
}

// Vigila en segundo plano los trabajos activos para avisar cuando terminen, en cualquier vista.
async function watchJobs() {
  try {
    const jobs = await api("/api/jobs");
    for (const job of jobs) {
      const before = watched.get(job.id);
      if (before && ACTIVE.has(before) && !ACTIVE.has(job.status)) notifyFinished(job);
      watched.set(job.id, job.status);
    }
  } catch { /* reintenta en el siguiente ciclo */ }
  setTimeout(watchJobs, 5000);
}

/* ------------------------------------------------------------------ componentes */

function stepsHtml(steps, running) {
  // El agente no siempre sigue el orden: el paso actual es el que sigue al último hecho,
  // y los anteriores sin hacer se marcan como pendientes en lugar de "actuales".
  let last = -1;
  steps.forEach((s, i) => { if (s.done) last = i; });
  const current = running && last + 1 < steps.length ? last + 1 : -1;
  return `<div class="steps">${steps.map((s, i) => {
    const cls = s.done ? "done" : i === current ? "current" : i < last ? "late" : "";
    const tip = cls === "late" ? ` title="Pendiente: el agente ya pasó a pasos posteriores"` : "";
    return `
    <div class="step ${cls}"${tip}>
      <span class="dot">${s.done ? "✓" : i + 1}</span><span>${esc(s.label)}</span>
    </div>`;
  }).join("")}</div>`;
}

function jobRow(job) {
  const pct = job.steps_total ? Math.round((job.steps_done / job.steps_total) * 100) : 0;
  const req = job.request || {};
  const extra = job.status === "queued" && job.queue_position ? `· puesto ${job.queue_position} en la cola` : "";
  return `
    <a class="job" href="#/trabajo/${esc(job.id)}">
      <span class="title">${esc(req.tema)}</span>
      <span class="pills">${tiktokPill(job.tiktok)}${pill(job.status)}</span>
      ${ACTIVE.has(job.status) || job.steps_done ? `<div class="bar" aria-hidden="true"><i style="width:${pct}%"></i></div>` : ""}
      <span class="meta">
        <span class="pill lang">${esc((req.idioma || "").toUpperCase())}</span>
        <span>${esc(reqCharacter(req).nombre)}</span>
        <span>${esc(req.formato === "horizontal" ? "16:9" : "9:16")}</span>
        <span>${job.steps_done}/${job.steps_total} pasos ${extra}</span>
        ${job.cost_usd ? `<span>${fmtCost(job.cost_usd)}</span>` : ""}
        <span>${fmtDate(job.created_at)}</span>
      </span>
    </a>`;
}

/* ------------------------------------------------------------------ vista: estudio */

function studioForm(prefs) {
  const ages = [...AGES];
  if (!ages.includes(prefs.edad)) ages.push(prefs.edad);
  return `
  <form id="new-video" class="card stack" novalidate>
    <div class="row"><h2>Nuevo video</h2><span class="spacer"></span>
      <label class="row small muted"><input type="checkbox" id="batch-toggle"> Varios temas</label></div>
    <div class="field">
      <div class="row"><span class="label">Personaje</span><span class="spacer"></span>
        <a class="small" href="#/personajes/nuevo">+ Crear personaje</a></div>
      <div class="char-picker" role="radiogroup" aria-label="Personaje">
        ${CHARACTERS.map(c => `<label class="char-chip"><input type="radio" name="personaje_id" value="${esc(c.id)}" ${prefs.personaje_id === c.id ? "checked" : ""}>
          <span>${swatches(c.paleta)}<strong>${esc(c.nombre)}</strong><span class="small muted">${esc(c.nicho)}</span></span></label>`).join("")}
      </div>
    </div>
    <div class="field" id="single-field">
      <div class="row"><label for="tema">Tema</label><span class="spacer"></span>
        <button type="button" class="btn small" id="suggest-open" aria-expanded="false" aria-controls="suggest-panel">Sugerir 3 temas</button></div>
      <input id="tema" name="tema" type="text" class="tema-input" maxlength="200" autocomplete="off"
        placeholder="Ej.: ¿Por qué el cielo es azul?" value="${esc(prefs.tema || "")}">
      <span class="hint">Escríbelo en el idioma del video. El agente investiga, escribe el guion, anima y renderiza 60–65 s. Los temas no se repiten dentro de la serie de cada personaje.</span>
      <div id="suggest-panel" class="suggest-panel hidden">
        <div class="row">
          <input id="pista" type="text" maxlength="120" autocomplete="off" aria-label="Sobre qué (opcional)" placeholder="Sobre qué (opcional): animales, espacio…">
          <button type="button" class="btn small" id="suggest-more">Otras 3</button>
        </div>
        <div id="suggest-list" class="suggest-list" role="radiogroup" aria-label="Temas sugeridos" aria-live="polite"></div>
      </div>
    </div>
    <div class="field hidden" id="batch-field">
      <label for="temas">Temas (uno por línea)</label>
      <textarea id="temas" placeholder="Los volcanes&#10;Cómo duermen los delfines&#10;¿Por qué brillan las estrellas?"></textarea>
      <span class="hint">Cada tema entra a la cola como un video aparte, con las mismas opciones.</span>
    </div>
    <div class="fields-2">
      <div class="field">
        <span class="label">Idioma</span>
        <div class="segmented" role="radiogroup" aria-label="Idioma">
          ${CONFIG.languages.map(l => `<label><input type="radio" name="idioma" value="${l}" ${prefs.idioma === l ? "checked" : ""}><span>${LANG[l]}</span></label>`).join("")}
        </div>
      </div>
      <div class="field">
        <span class="label">Formato</span>
        <div class="segmented" role="radiogroup" aria-label="Formato">
          <label><input type="radio" name="formato" value="vertical" ${prefs.formato === "vertical" ? "checked" : ""}><span><i class="ratio v"></i>9:16</span></label>
          <label><input type="radio" name="formato" value="horizontal" ${prefs.formato === "horizontal" ? "checked" : ""}><span><i class="ratio h"></i>16:9</span></label>
        </div>
      </div>
    </div>
    <div class="field">
      <label for="edad">Edad del público</label>
      <select id="edad" name="edad">${ages.map(a => `<option value="${a}" ${prefs.edad === a ? "selected" : ""}>${a} años</option>`).join("")}</select>
    </div>
    <div class="field">
      <label for="cta">Frase final (CTA)</label>
      <input id="cta" name="cta" type="text" maxlength="80" autocomplete="off">
      <span class="hint" id="cta-hint"></span>
    </div>
    <details class="advanced">
      <summary>Opciones del agente</summary>
      <div class="fields-2">
        <div class="field"><label for="model">Modelo</label>
          <select id="model" name="model">${CONFIG.models.map(m => `<option ${prefs.model === m ? "selected" : ""}>${m}</option>`).join("")}</select></div>
        <div class="field"><label for="effort">Esfuerzo</label>
          <select id="effort" name="effort">${CONFIG.efforts.map(e => `<option ${prefs.effort === e ? "selected" : ""}>${e}</option>`).join("")}</select></div>
      </div>
      <div class="fields-2">
        <div class="field"><label for="budget">Tope de gasto (USD)</label>
          <input id="budget" name="max_budget_usd" type="number" min="0.5" max="1000" step="0.5" placeholder="Sin tope" value="${esc(prefs.max_budget_usd ?? "")}">
          <span class="hint">Si se alcanza, el trabajo se detiene y puedes reanudarlo.</span></div>
        <div class="field"><label for="turns">Máximo de turnos</label>
          <input id="turns" name="max_turns" type="number" min="10" max="2000" step="10" value="${esc(prefs.max_turns ?? 400)}"></div>
      </div>
    </details>
    <p id="form-error" class="small" style="color:var(--warn)" role="alert"></p>
    <div class="row">
      <button type="button" class="btn" id="preview-prompt">Ver prompt</button>
      <span class="spacer"></span>
      <button type="submit" class="btn primary" id="submit">Producir video</button>
    </div>
  </form>`;
}

function readForm(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  const prefs = { personaje_id: data.personaje_id, edad: data.edad, idioma: data.idioma, formato: data.formato,
                  model: data.model, effort: data.effort, max_turns: data.max_turns, max_budget_usd: data.max_budget_usd || null };
  const cta = (data.cta || "").trim();
  savePrefs({ ...loadPrefs(), ...prefs, [ctaKey(data.personaje_id, data.idioma)]: cta });
  return { ...prefs, cta, max_turns: Number(data.max_turns) || 400, max_budget_usd: data.max_budget_usd ? Number(data.max_budget_usd) : null };
}

function studioIntro(c) {
  return c ? `Escribe un tema y ${esc(c.nombre)} lo convierte en un video de papel de un minuto. Su nicho: <strong>${esc(c.nicho)}</strong>.` : "";
}

async function renderStudio(params) {
  await loadCharacters();
  const saved = loadPrefs();
  const prefs = { edad: "6-9", idioma: "es", formato: "vertical", model: CONFIG.default_model, effort: "high", ...saved,
                  tema: params.get("tema") || "" };
  const asked = params.get("personaje");
  if (asked && charById(asked)) prefs.personaje_id = asked;
  if (!charById(prefs.personaje_id)) prefs.personaje_id = charById(CONFIG.default_character) ? CONFIG.default_character : CHARACTERS[0]?.id;
  // Al elegir otro personaje (o llegar desde su ficha), el video toma su idioma y su edad.
  const chosen = charById(prefs.personaje_id);
  if (chosen && (asked || saved.personaje_id !== prefs.personaje_id)) Object.assign(prefs, { idioma: chosen.idioma, edad: chosen.edad });
  for (const key of ["idioma", "formato", "edad"]) if (params.get(key)) prefs[key] = params.get(key);
  view.innerHTML = `
    <div class="page-head"><div><h1>Estudio</h1>
      <p id="studio-intro">${studioIntro(chosen)}</p></div></div>
    <div class="grid-studio">
      ${studioForm(prefs)}
      <section class="card stack" aria-labelledby="queue-title">
        <div class="row"><h2 id="queue-title">En producción</h2><span class="spacer"></span>
          <span class="small muted">${CONFIG.concurrency} a la vez</span></div>
        <div id="active-jobs" class="job-list"></div>
        <div class="row"><h2>Recientes</h2><span class="spacer"></span><a class="small" href="#/biblioteca">Ver biblioteca</a></div>
        <div id="recent-jobs" class="job-list"></div>
      </section>
    </div>`;

  const form = $("#new-video");
  const batch = $("#batch-toggle");
  batch.addEventListener("change", () => {
    $("#single-field").classList.toggle("hidden", batch.checked);
    $("#batch-field").classList.toggle("hidden", !batch.checked);
    $("#submit").textContent = batch.checked ? "Poner en cola" : "Producir video";
  });

  const current = () => { const d = new FormData(form); return { pid: d.get("personaje_id"), lang: d.get("idioma") }; };

  // Sugerencias: 3 temas nuevos del nicho del personaje; los ya mostrados no vuelven a salir.
  let shown = [];
  const list = $("#suggest-list");
  async function suggest() {
    const { idioma, edad, personaje_id } = Object.fromEntries(new FormData(form).entries());
    list.innerHTML = `<p class="small muted">${esc(charById(personaje_id)?.nombre || "El personaje")} está pensando temas…</p>`;
    $("#suggest-more").disabled = true;
    try {
      const res = await api("/api/suggest", { method: "POST", body: { idioma, edad, personaje_id, pista: $("#pista").value, evitar: shown } });
      shown.push(...res.temas.map(t => t.tema));
      list.innerHTML = res.temas.map(t => `
        <button type="button" class="suggestion" role="radio" aria-checked="false" data-tema="${esc(t.tema)}">
          <strong>${esc(t.tema)}</strong>${t.gancho ? `<span class="small muted">${esc(t.gancho)}</span>` : ""}</button>`).join("");
    } catch (err) {
      list.innerHTML = `<p class="small" style="color:var(--warn)">${esc(err.message)}</p>`;
    } finally { $("#suggest-more").disabled = false; }
  }
  $("#suggest-open").addEventListener("click", () => {
    const panel = $("#suggest-panel");
    const open = panel.classList.toggle("hidden") === false;
    $("#suggest-open").setAttribute("aria-expanded", open);
    if (open && !list.children.length) suggest();
  });
  $("#suggest-more").addEventListener("click", suggest);
  $("#pista").addEventListener("keydown", ev => { if (ev.key === "Enter") { ev.preventDefault(); suggest(); } });
  // La frase final se recuerda por personaje e idioma: al cambiar uno de los dos,
  // guarda la actual y trae la que corresponda (vacía = la del personaje).
  let ctaCtx = current();
  const showCta = () => {
    const c = charById(ctaCtx.pid);
    $("#cta").value = savedCta(ctaCtx.pid, ctaCtx.lang);
    $("#cta").placeholder = c?.cta?.[ctaCtx.lang] || CONFIG.default_cta[ctaCtx.lang] || "";
    $("#cta-hint").textContent = `${c?.nombre || "El personaje"} la dice al final de cada video. Déjala vacía para usar la suya. Se recuerda por personaje e idioma.`;
  };
  const syncCta = () => {
    savePrefs({ ...loadPrefs(), [ctaKey(ctaCtx.pid, ctaCtx.lang)]: $("#cta").value.trim() });
    ctaCtx = current();
    showCta();
  };
  showCta();
  // Las sugerencias anteriores ya no sirven con otro idioma u otro personaje.
  const resetSuggestions = () => { shown = []; list.innerHTML = ""; if (!$("#suggest-panel").classList.contains("hidden")) suggest(); };
  $$("input[name=idioma]", form).forEach(r => r.addEventListener("change", () => { syncCta(); resetSuggestions(); }));
  $$("input[name=personaje_id]", form).forEach(r => r.addEventListener("change", () => {
    const c = charById(r.value);
    if (!c) return;
    const lang = $(`input[name=idioma][value="${c.idioma}"]`, form);
    if (lang) lang.checked = true;
    const edad = $("#edad");
    if (![...edad.options].some(o => o.value === c.edad)) edad.add(new Option(`${c.edad} años`, c.edad));
    edad.value = c.edad;
    $("#studio-intro").innerHTML = studioIntro(c);
    syncCta();
    resetSuggestions();
  }));
  list.addEventListener("click", ev => {
    const pick = ev.target.closest(".suggestion");
    if (!pick) return;
    $$(".suggestion", list).forEach(b => b.setAttribute("aria-checked", b === pick));
    $("#tema").value = pick.dataset.tema;
    $("#form-error").textContent = "";
  });

  $("#preview-prompt").addEventListener("click", async () => {
    const tema = batch.checked ? $("#temas").value.split("\n").map(s => s.trim()).filter(Boolean)[0] : $("#tema").value;
    try {
      const res = await api("/api/prompt-preview", { method: "POST", body: { ...readForm(form), tema } });
      modal(`Prompt para output/${res.slug}/`, `<p class="small muted">Mensaje inicial: ${esc(res.kickoff)}</p><pre>${esc(res.prompt)}</pre>`);
    } catch (err) { $("#form-error").textContent = err.message; }
  });

  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    $("#form-error").textContent = "";
    const opts = readForm(form);
    const temas = batch.checked
      ? $("#temas").value.split("\n").map(s => s.trim()).filter(Boolean)
      : [$("#tema").value.trim()];
    if (!temas[0]) { $("#form-error").textContent = "Escribe un tema."; return; }
    $("#submit").disabled = true;
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
    const created = [];
    try {
      for (const tema of temas) created.push(await api("/api/jobs", { method: "POST", body: { ...opts, tema } }));
      created.forEach(job => watched.set(job.id, job.status));
      if (created.length === 1) { location.hash = `#/trabajo/${created[0].id}`; return; }
      toast(`${created.length} videos en cola.`, "ok");
      $("#temas").value = "";
      loadJobs();
    } catch (err) {
      $("#form-error").textContent = created.length ? `Se encolaron ${created.length}; luego: ${err.message}` : err.message;
    } finally { $("#submit").disabled = false; }
  });

  async function loadJobs() {
    const jobs = await api("/api/jobs");
    const active = jobs.filter(j => ACTIVE.has(j.status)).reverse();
    const recent = jobs.filter(j => !ACTIVE.has(j.status)).slice(0, 5);
    $("#active-jobs").innerHTML = active.length ? active.map(jobRow).join("")
      : `<div class="empty"><span class="big">El escenario está libre</span>Los videos en producción aparecerán aquí.</div>`;
    $("#recent-jobs").innerHTML = recent.length ? recent.map(jobRow).join("") : `<p class="small muted">Todavía no hay trabajos terminados.</p>`;
  }
  poll(loadJobs, 4000);
  $("#tema")?.focus();
}

/* ------------------------------------------------------------------ vista: trabajo */

async function renderJob(jobId) {
  let job;
  try { job = await api(`/api/jobs/${encodeURIComponent(jobId)}`); }
  catch (err) { view.innerHTML = `<div class="card empty"><span class="big">No encontré ese trabajo</span>${esc(err.message)}</div>`; return; }
  const req = job.request;
  view.innerHTML = `
    <div class="page-head">
      <div><p class="small"><a href="#/">← Estudio</a></p><h1>${esc(req.tema)}</h1>
        <p>${esc(reqCharacter(req).nombre)} · ${esc(LANG[req.idioma])} · ${req.formato === "horizontal" ? "16:9" : "9:16"} · ${esc(req.edad)} años · ${esc(req.model)} (${esc(req.effort)}) · <code>output/${esc(job.slug)}/</code></p></div>
      <div class="row" id="job-actions"></div>
    </div>
    <div class="stack">
      <section class="card stack">
        <div class="row"><span id="job-status"></span><span class="spacer"></span>
          <span class="small muted" id="job-meta"></span></div>
        <div id="job-steps"></div>
        <div id="job-error" class="hidden" role="alert"></div>
        <div id="job-result" class="hidden"></div>
      </section>
      <section class="card stack">
        <div class="row"><h2>Qué está haciendo el agente</h2><span class="spacer"></span>
          <label class="row small muted"><input type="checkbox" id="show-stderr"> Diagnóstico</label>
          <label class="row small muted"><input type="checkbox" id="follow" checked> Seguir</label></div>
        <div class="log hide-stderr" id="log" role="log" aria-live="off"></div>
      </section>
    </div>`;

  const log = $("#log");
  $("#show-stderr").addEventListener("change", e => log.classList.toggle("hide-stderr", !e.target.checked));
  let startTs = null;

  function paint(j) {
    job = j;
    $("#job-status").innerHTML = pill(j.status) + tiktokPill(j.tiktok) + (j.queue_position ? ` <span class="small muted">puesto ${j.queue_position} en la cola</span>` : "");
    const end = j.finished_at || (ACTIVE.has(j.status) ? Date.now() / 1000 : null);
    const elapsed = j.started_at && end ? fmtTime(end - j.started_at) : "—";
    $("#job-meta").textContent = `Tiempo: ${elapsed} · Turnos: ${j.turns || 0} · Costo: ${fmtCost(j.cost_usd)}`;
    $("#job-steps").innerHTML = stepsHtml(j.steps, j.status === "running");
    const err = $("#job-error");
    err.classList.toggle("hidden", !j.error);
    err.innerHTML = j.error ? `<div class="banner warn" style="padding:0;margin:0"><div>${esc(j.error)}</div></div>` : "";
    const res = $("#job-result");
    res.classList.toggle("hidden", !j.result);
    res.innerHTML = j.result ? `<h3>Resumen del agente</h3>${markdown(j.result)}` : "";
    const actions = [];
    if (j.status === "done" || j.steps.some(s => s.done)) actions.push(`<a class="btn" href="#/video/${esc(j.slug)}">${j.status === "done" ? "Ver video" : "Ver archivos"}</a>`);
    if (ACTIVE.has(j.status)) actions.push(`<button class="btn danger" data-act="cancel">Cancelar</button>`);
    if (["failed", "cancelled", "interrupted"].includes(j.status)) {
      actions.push(`<button class="btn primary" data-act="resume">Reanudar</button>`);
      actions.push(`<button class="btn danger" data-act="delete">Quitar de la lista</button>`);
    }
    if (j.status === "done") {
      const other = req.idioma === "es" ? "en" : "es";
      actions.push(`<a class="btn" href="#/?tema=${encodeURIComponent(req.tema)}&personaje=${encodeURIComponent(reqCharacter(req).id)}&idioma=${other}&formato=${req.formato}&edad=${encodeURIComponent(req.edad)}">Versión en ${LANG[other]}</a>`);
    }
    $("#job-actions").innerHTML = actions.join("");
  }

  $("#job-actions").addEventListener("click", async ev => {
    const act = ev.target.closest("[data-act]")?.dataset.act;
    if (!act) return;
    if (act === "cancel" && !confirmAction("¿Cancelar este trabajo? Los archivos ya generados se conservan.")) return;
    if (act === "delete" && !confirmAction("¿Quitar este trabajo de la lista? Los archivos del video no se borran.")) return;
    try {
      if (act === "delete") { await api(`/api/jobs/${job.id}`, { method: "DELETE" }); location.hash = "#/"; return; }
      const updated = await api(`/api/jobs/${job.id}/${act}`, { method: "POST" });
      watched.set(updated.id, updated.status);
      if (act === "resume") { renderJob(job.id); return; }
      paint(updated);
    } catch (err) { toast(esc(err.message), "err"); }
  });

  function addLine(ev) {
    if (startTs == null) startTs = ev.ts;
    const t = ev.ts - startTs;
    let tag, msg, cls = ev.kind;
    switch (ev.kind) {
      case "text": tag = "agente"; msg = ev.text; break;
      case "tool": tag = ev.tool; msg = ev.detail; break;
      case "stderr": tag = "cli"; msg = ev.text; break;
      case "status": tag = "estado"; msg = STATUS[ev.status] + (ev.error ? ` — ${ev.error}` : ""); break;
      case "result": tag = "fin"; msg = `${ev.subtype} · ${ev.turns} turnos · ${fmtCost(ev.cost_usd)}`; break;
      case "session": tag = "sesión"; msg = ev.session_id; cls = "stderr"; break;
      default: return;
    }
    const el = document.createElement("div");
    el.className = `log-line ${cls}`;
    el.innerHTML = `<span class="t">${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}</span><span class="tag">${esc(tag)}</span><span class="msg">${esc(msg)}</span>`;
    log.append(el);
    if ($("#follow").checked) log.scrollTop = log.scrollHeight;
  }

  paint(job);
  const source = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`);
  source.onmessage = async e => {
    const ev = JSON.parse(e.data);
    if (ev.kind === "steps") { job.steps = ev.steps; paint(job); return; }
    addLine(ev);
    if (ev.kind === "status" || ev.kind === "result") paint(await api(`/api/jobs/${encodeURIComponent(jobId)}`));
  };
  source.addEventListener("end", () => source.close());
  onCleanup(() => source.close());
  poll(async () => { if (ACTIVE.has(job.status)) paint(await api(`/api/jobs/${encodeURIComponent(jobId)}`)); }, 5000);
}

/* ------------------------------------------------------------------ vista: biblioteca */

async function renderLibrary(params) {
  const [videos] = await Promise.all([api("/api/videos"), loadCharacters()]);
  const who = params.get("personaje") || "";
  view.innerHTML = `
    <div class="page-head"><div><h1>Biblioteca</h1><p>Todos los videos en <code>output/</code>, también los hechos desde la terminal.</p></div>
      <div class="row">
        <select id="lib-pub" class="search" aria-label="Publicación"><option value="">Publicados y sin publicar</option>
          <option value="no">Sin publicar en TikTok</option><option value="si">Ya en TikTok</option></select>
        <select id="lib-char" class="search" aria-label="Personaje"><option value="">Todos los personajes</option>
          ${CHARACTERS.map(c => `<option value="${esc(c.id)}" ${who === c.id ? "selected" : ""}>${esc(c.nombre)}</option>`).join("")}</select>
        <input type="text" class="search" id="search" placeholder="Buscar…" aria-label="Buscar videos"></div></div>
    <div id="gallery" class="gallery"></div>`;
  const draw = () => {
    const filter = $("#search").value.trim().toLowerCase(), pid = $("#lib-char").value, pub = $("#lib-pub").value;
    const list = videos.filter(v => (!pid || v.personaje === pid) && (!pub || (pub === "si") === !!v.en_tiktok)
      && (!filter || `${v.title} ${v.slug} ${v.gancho || ""} ${charName(v.personaje)}`.toLowerCase().includes(filter)));
    $("#gallery").innerHTML = list.length ? list.map(v => `
      <a class="vcard" href="#/video/${esc(v.slug)}">
        <div class="thumb">${v.has_video ? `<img src="/poster/${esc(v.slug)}.jpg" alt="" loading="lazy" onerror="this.remove()">` : ""}
          <span class="ph">${esc(v.title)}</span>
          ${v.duration ? `<span class="dur">${v.duration.toFixed(1)} s</span>` : ""}</div>
        <div class="body"><strong>${esc(v.title)}</strong>
          <div class="row small">
            ${v.idioma ? `<span class="pill lang">${esc(v.idioma.toUpperCase())}</span>` : ""}
            <span class="muted">${esc(charName(v.personaje))}</span>
            ${tiktokPill(v.tiktok)}
            ${v.has_video ? `<span class="pill done">Video listo</span>` : `<span class="pill">${v.steps_done}/${v.steps_total} pasos</span>`}
            ${v.qa ? `<span class="pill plain ${v.qa.failed ? "failed" : "done"}">QA ${v.qa.passed}/${v.qa.total}</span>` : ""}
            ${v.verify && !v.verify.ok ? `<span class="pill plain failed" title="La revisión automática encontró problemas">Revisar</span>` : ""}
          </div></div>
      </a>`).join("")
      : `<div class="card empty" style="grid-column:1/-1"><span class="big">${filter ? "Sin resultados" : "Aún no hay videos"}</span>${filter ? "" : `<a href="#/">Produce el primero</a>`}</div>`;
  };
  draw();
  $("#search").addEventListener("input", draw);
  $("#lib-char").addEventListener("change", draw);
  $("#lib-pub").addEventListener("change", draw);
}

/* ------------------------------------------------------------------ vista previa interactiva */

function stagePreview(container, v) {
  const [W, H] = (v.formato || v.json?.["script.json"]?.formato) === "horizontal" ? [1920, 1080] : [1080, 1920];
  const timeline = v.json?.["timeline.json"];
  const scenes = Array.isArray(timeline) ? timeline : timeline?.scenes || [];
  const titles = v.json?.["script.json"]?.escenas || [];
  container.innerHTML = `
    <div class="preview-frame"><iframe title="Vista previa de stage.html" src="/files/${esc(v.slug)}/stage.html" width="${W}" height="${H}" tabindex="-1"></iframe></div>
    <div class="controls">
      <button class="icon-btn" data-c="play" aria-label="Reproducir">▶</button>
      <button class="icon-btn" data-c="prev" aria-label="Frame anterior">⟨</button>
      <button class="icon-btn" data-c="next" aria-label="Frame siguiente">⟩</button>
      <div class="scrub"><div class="marks"></div><input type="range" min="0" step="${1 / 24}" value="0" aria-label="Tiempo"></div>
      <span class="time">0:00.0</span>
      <span class="scene-label"></span>
    </div>
    ${v.has_mix ? `<audio preload="auto" src="/files/${esc(v.slug)}/mix.wav"></audio>` : ""}`;
  const frame = $(".preview-frame", container), iframe = $("iframe", container);
  const range = $("input[type=range]", container), audio = $("audio", container);
  let duration = 60, t = 0, playing = false, raf = 0, t0 = 0;

  const fit = () => {
    const scale = frame.clientWidth / W;
    iframe.style.transform = `scale(${scale})`;
    frame.style.height = `${H * scale}px`;
  };
  const ro = new ResizeObserver(fit);
  ro.observe(frame);
  onCleanup(() => { ro.disconnect(); cancelAnimationFrame(raf); audio?.pause(); });

  const draw = () => {
    try { iframe.contentWindow.renderAt?.(t); } catch { /* aún cargando */ }
    range.value = t;
    $(".time", container).textContent = `${fmtClock(t)} / ${fmtClock(duration)}`;
    const i = scenes.findIndex(s => t >= s.start && t < s.end);
    $(".scene-label", container).textContent = i >= 0 ? `Escena ${i + 1}${titles[i]?.titulo ? ` · ${titles[i].titulo}` : ""}${titles[i]?.vo ? ` — “${titles[i].vo}”` : ""}` : "";
  };
  const seek = to => { t = Math.max(0, Math.min(duration, to)); if (audio) audio.currentTime = t; t0 = performance.now() - t * 1000; draw(); };
  const loop = () => {
    t = audio ? audio.currentTime : (performance.now() - t0) / 1000;
    if (t >= duration) { t = duration; stop(); }
    draw();
    if (playing) raf = requestAnimationFrame(loop);
  };
  const play = () => { if (t >= duration) seek(0); playing = true; t0 = performance.now() - t * 1000; audio?.play().catch(() => {}); $("[data-c=play]", container).textContent = "❚❚"; loop(); };
  const stop = () => { playing = false; audio?.pause(); $("[data-c=play]", container).textContent = "▶"; };

  iframe.addEventListener("load", () => {
    try { duration = Number(iframe.contentWindow.DURATION) || duration; } catch { /* sin DURATION */ }
    range.max = duration;
    $(".marks", container).innerHTML = scenes.slice(1).map(s => `<i style="left:${(s.start / duration) * 100}%"></i>`).join("");
    fit(); draw();
  });
  range.addEventListener("input", () => seek(Number(range.value)));
  container.addEventListener("click", e => {
    const c = e.target.closest("[data-c]")?.dataset.c;
    if (c === "play") playing ? stop() : play();
    if (c === "prev") { stop(); seek(t - 1 / 24); }
    if (c === "next") { stop(); seek(t + 1 / 24); }
  });
}

/* ------------------------------------------------------------------ vista: video */

async function renderVideo(slug) {
  let v;
  try { [v] = await Promise.all([api(`/api/videos/${encodeURIComponent(slug)}`), loadCharacters()]); }
  catch (err) { view.innerHTML = `<div class="card empty"><span class="big">No encontré ese video</span>${esc(err.message)}</div>`; return; }
  const script = v.json["script.json"] || {};
  const publish = v.json["publish.json"] || {};
  const lastJob = v.jobs[0];
  const running = v.jobs.find(j => ACTIVE.has(j.status));
  const req = lastJob?.request || { tema: v.title, idioma: v.idioma || "es", formato: v.formato || "vertical", edad: "6-9" };
  const other = (v.idioma || req.idioma) === "es" ? "en" : "es";
  const who = `personaje=${encodeURIComponent(v.personaje)}`;

  const tabs = [];
  if (publish.titulos || publish.descripcion || publish.descripcion_corta) tabs.push(["publish", "Publicación"]);
  if (script.escenas) tabs.push(["script", "Guion"]);
  if (v.texts["research.md"]) tabs.push(["research", "Investigación"]);
  if (v.texts["qa.md"] || v.texts["qa_previo.md"] || v.verify) tabs.push(["qa", "QA"]);
  if (v.texts["bible.md"]) tabs.push(["bible", "Biblia visual"]);
  tabs.push(["files", "Archivos"]);
  if (v.jobs.length) tabs.push(["history", "Historial"]);

  view.innerHTML = `
    <div class="page-head">
      <div><p class="small"><a href="#/biblioteca">← Biblioteca</a></p><h1>${esc(v.title)}</h1>
        <p>${esc(charName(v.personaje))} · ${v.duration ? `${v.duration.toFixed(1)} s · ` : ""}${esc(LANG[v.idioma] || "")} · <code>output/${esc(v.slug)}/</code></p></div>
      <div class="row">
        ${running ? `<a class="btn" href="#/trabajo/${esc(running.id)}">Ver producción en curso</a>` : ""}
        ${!running && lastJob && lastJob.status !== "done" ? `<a class="btn primary" href="#/trabajo/${esc(lastJob.id)}">Continuar producción</a>` : ""}
        <a class="btn" href="#/?tema=${encodeURIComponent(req.tema)}&${who}&idioma=${other}&formato=${esc(req.formato)}&edad=${encodeURIComponent(req.edad)}">Versión en ${LANG[other]}</a>
        <button class="btn danger" id="delete-video" ${running ? "disabled" : ""}>Borrar</button>
      </div>
    </div>
    ${v.verify && !v.verify.ok ? `<div class="banner warn"><div><strong>La revisión automática encontró problemas:</strong>
      ${esc(v.verify.checks.filter(c => !c.ok).map(c => `${c.label} (${c.detail})`).join("; "))}. Detalles en la pestaña QA.</div></div>` : ""}
    <div class="video-layout">
      <div class="stack">
        ${v.has_video && v.has_stage ? `<div class="segmented" role="tablist" aria-label="Reproductor">
          <label><input type="radio" name="pv" value="video" checked><span>Video final</span></label>
          <label><input type="radio" name="pv" value="stage"><span>Vista interactiva</span></label></div>` : ""}
        <div class="player" id="player"></div>
        ${v.has_video ? `<div class="row"><a class="btn small" href="/files/${esc(v.slug)}/final.mp4" download="${esc(v.slug)}.mp4">Descargar MP4</a>
          ${v.files.some(f => f.name === "subtitles.srt") ? `<a class="btn small" href="/files/${esc(v.slug)}/subtitles.srt" download>Subtítulos .srt</a>` : ""}</div>` : ""}
        <section class="card stack">
          <h2>Progreso</h2>${stepsHtml(v.steps, !!running)}
        </section>
      </div>
      <section class="card">
        <div class="tabbar" role="tablist">${tabs.map(([k, label], i) => `<button role="tab" data-tab="${k}" aria-selected="${i === 0}">${label}</button>`).join("")}</div>
        <div id="tab-body"></div>
      </section>
    </div>`;

  const player = $("#player");
  const showVideo = () => { player.innerHTML = `<video controls playsinline preload="metadata" poster="/poster/${esc(v.slug)}.jpg" src="/files/${esc(v.slug)}/final.mp4"></video>`; };
  if (v.has_video) showVideo();
  else if (v.has_stage) stagePreview(player, v);
  else player.innerHTML = `<div class="empty" style="color:var(--cream)"><span class="big" style="color:var(--cream)">Todavía no hay imagen</span>Aparecerá cuando el agente escriba stage.html.</div>`;
  $$("input[name=pv]").forEach(r => r.addEventListener("change", () => (r.value === "video" ? showVideo() : stagePreview(player, v))));

  const bodies = {
    publish: () => `
      <div class="stack">
        ${v.has_video ? `<section class="pub-box" id="pub-box" aria-live="polite"><p class="small muted">Cargando…</p></section>` : ""}
        ${(publish.titulos || []).length ? `<h3>Títulos</h3>${publish.titulos.map(t => copyRow(t)).join("")}` : ""}
        ${publish.descripcion_corta ? `<h3>Descripción corta <span class="small muted">TikTok y Reels</span></h3>${copyRow(publish.descripcion_corta)}` : ""}
        ${publish.descripcion ? `<h3>Descripción${publish.descripcion_corta ? ` <span class="small muted">YouTube</span>` : ""}</h3>${copyRow(publish.descripcion)}` : ""}
        ${(publish.hashtags || []).length ? `<h3>Hashtags</h3>${copyRow(publish.hashtags.slice(0, MAX_HASHTAGS).join(" "))}` : ""}
        ${v.has_video ? `<h3>Portada</h3>
          <div class="cover-row"><img class="cover" src="/cover/${esc(v.slug)}.jpg" alt="Portada del video" loading="lazy" onerror="this.parentElement.remove()">
            <div class="stack"><a class="btn small" href="/cover/${esc(v.slug)}.jpg" download="${esc(v.slug)}-portada.jpg">Descargar portada</a>
            ${publish.texto_portada ? `<p class="small muted">“${esc(publish.texto_portada)}” sobre el fotograma de ${esc(publish.frame_portada_s)} s</p>` : ""}</div></div>` : ""}
        ${publish.hecho_para_ninos ? `<p class="small muted">Marcar como «Hecho para niños»</p>` : ""}
        ${(publish.ideas_siguientes || []).length ? `<h3>Ideas para los siguientes videos</h3><div class="chips">${publish.ideas_siguientes.map(i =>
          `<a class="chip" href="#/?tema=${encodeURIComponent(i)}&${who}&idioma=${esc(v.idioma || "es")}&formato=${esc(req.formato)}&edad=${encodeURIComponent(req.edad)}">+ ${esc(i)}</a>`).join("")}</div>` : ""}
      </div>`,
    script: () => `
      ${script.gancho ? `<p><strong>Gancho${script.tipo_gancho ? ` (${esc(script.tipo_gancho)})` : ""}:</strong> ${esc(script.gancho)}</p>` : ""}
      ${script.texto_gancho ? `<p class="small muted">Escrito en pantalla desde el frame 1: «${esc(script.texto_gancho)}»</p>` : ""}
      ${script.bucle_abierto ? `<p class="small muted">Bucle abierto: ${esc(script.bucle_abierto)}</p>` : ""}
      ${(script.escenas || []).map(s => `
        <div class="scene"><span class="n">${esc(s.n)}</span><div>
          <strong>${esc(s.titulo || "")}</strong><p class="vo">“${esc(s.vo || "")}”</p>
          ${s.visual ? `<div class="extra"><strong>Visual:</strong> ${esc(s.visual)}</div>` : ""}
          ${s.transition ? `<div class="extra"><strong>Transición:</strong> ${esc(s.transition)}</div>` : ""}
          ${(s.sfx || []).length ? `<div class="extra"><strong>Efectos:</strong> ${s.sfx.map(esc).join(", ")}</div>` : ""}
        </div></div>`).join("")}`,
    research: () => markdown(v.texts["research.md"]),
    bible: () => markdown(v.texts["bible.md"]),
    qa: () => `
      ${v.verify ? `<h3>Revisión automática de la app</h3>
        <p class="small muted">Medida por la app sobre final.mp4 y publish.json, sin fiarse del informe del agente.</p>
        <ul class="checks">${v.verify.checks.map(c => `<li class="${c.ok ? "ok" : "bad"}"><span aria-hidden="true">${c.ok ? "✓" : "✗"}</span>
          <span>${esc(c.label)}</span><span class="small muted">${esc(c.detail)}</span></li>`).join("")}</ul>` : ""}
      ${v.texts["qa.md"] ? `<h3>QA final del agente</h3>${v.qa ? `<div class="qa-score">${v.qa.failed ? `<span class="pill failed">${v.qa.failed} sin cumplir</span>` : `<span class="pill done">Todo cumple</span>`}
        <span class="small muted">${v.qa.passed} de ${v.qa.total} puntos</span></div>` : ""}${markdown(v.texts["qa.md"])}` : ""}
      ${v.texts["qa_previo.md"] ? `<details class="advanced"><summary>QA previo del agente (antes del render)</summary>${markdown(v.texts["qa_previo.md"])}</details>` : ""}`,
    files: () => `<table class="files"><tbody>${v.files.map(f => `
      <tr><td><a href="/files/${esc(v.slug)}/${f.name.split("/").map(encodeURIComponent).join("/")}" target="_blank" rel="noopener">${esc(f.name)}</a></td><td>${fmtSize(f.size)}</td></tr>`).join("")}</tbody></table>`,
    history: () => `<div class="job-list">${v.jobs.map(j => jobRow({ ...j, steps_done: v.steps_done, steps_total: v.steps_total })).join("")}</div>`,
  };
  const showTab = key => {
    $$(".tabbar button").forEach(b => b.setAttribute("aria-selected", b.dataset.tab === key));
    $("#tab-body").innerHTML = bodies[key]();
    if ($("#pub-box")) mountPublish($("#pub-box"), v.slug);
  };
  $(".tabbar").addEventListener("click", e => { const b = e.target.closest("[data-tab]"); if (b) showTab(b.dataset.tab); });
  showTab(tabs[0][0]);

  $("#tab-body").addEventListener("click", async e => {
    const btn = e.target.closest("[data-copy]");
    if (!btn) return;
    try { await navigator.clipboard.writeText(btn.dataset.copy); btn.textContent = "Copiado"; setTimeout(() => (btn.textContent = "Copiar"), 1500); }
    catch { toast("No se pudo copiar.", "err"); }
  });
  $("#delete-video").addEventListener("click", async () => {
    if (!confirmAction(`¿Borrar output/${v.slug}/ con todos sus archivos? No se puede deshacer.`)) return;
    try { await api(`/api/videos/${encodeURIComponent(v.slug)}`, { method: "DELETE" }); toast("Video borrado.", "ok"); location.hash = "#/biblioteca"; }
    catch (err) { toast(esc(err.message), "err"); }
  });
}

function copyRow(text) {
  return `<div class="copy-row"><span>${esc(text)}</span><button class="btn small" data-copy="${esc(text)}">Copiar</button></div>`;
}

/* ------------------------------------------------------------------ vista: personajes */

async function renderCharacters() {
  await loadCharacters();
  view.innerHTML = `
    <div class="page-head"><div><h1>Personajes</h1>
      <p>Cada personaje presenta su propia serie, con su nicho, su escenario, su paleta y su voz. Todos sus videos giran alrededor de su nicho.</p></div>
      <a class="btn primary" href="#/personajes/nuevo">Crear personaje</a></div>
    <div class="gallery chars">${CHARACTERS.map(c => `
      <article class="vcard char-card">
        <a class="thumb" href="#/personajes/${esc(c.id)}" aria-label="Editar a ${esc(c.nombre)}">
          ${charArt(c)}
          ${c.ultimo_video ? `<img src="/poster/${esc(c.ultimo_video)}.jpg" alt="" loading="lazy" onerror="this.remove()">` : ""}</a>
        <div class="body">
          <strong>${esc(c.nombre)}</strong>
          <span class="small">${esc(c.nicho)}</span>
          ${swatches(c.paleta)}
          <div class="row small"><span class="pill lang">${esc(c.idioma.toUpperCase())}</span>
            <span class="muted">${esc(c.edad)} años · ${c.videos === 1 ? "1 video" : `${c.videos} videos`}</span></div>
          <div class="row">
            <a class="btn small primary" href="#/?personaje=${encodeURIComponent(c.id)}">Producir video</a>
            ${c.videos ? `<a class="btn small" href="#/biblioteca?personaje=${encodeURIComponent(c.id)}">Videos</a>` : ""}
            <a class="btn small" href="#/personajes/${encodeURIComponent(c.id)}">Editar</a>
          </div>
        </div>
      </article>`).join("")}</div>`;
}

function characterForm(c, isNew) {
  const ages = [...AGES];
  if (!ages.includes(c.edad)) ages.push(c.edad);
  const voiceSelect = lang => `<select id="voz-${lang}" name="voz_${lang}">${CONFIG.voices[lang].map(v =>
    `<option value="${v}" ${c.voces?.[lang] === v ? "selected" : ""}>${voiceLabel(v)}</option>`).join("")}</select>`;
  return `
  <form id="char-form" class="card stack" novalidate>
    <h2>${isNew ? "2. Ficha del personaje" : "Ficha del personaje"}</h2>
    <div class="fields-2">
      <div class="field"><label for="c-nombre">Nombre</label>
        <input id="c-nombre" name="nombre" type="text" maxlength="30" autocomplete="off" value="${esc(c.nombre || "")}"></div>
      <div class="field"><label for="c-serie">Nombre de la serie</label>
        <input id="c-serie" name="serie" type="text" maxlength="40" autocomplete="off" value="${esc(c.serie || "")}" placeholder="Por defecto, el del personaje">
        <span class="hint">Va en la publicación y en su hashtag fijo.</span></div>
    </div>
    <div class="field"><label for="c-nicho">Nicho</label>
      <input id="c-nicho" name="nicho" type="text" maxlength="120" autocomplete="off" value="${esc(c.nicho || "")}" placeholder="Ej.: Animales del océano profundo">
      <span class="hint">Concreto: todos sus videos y las sugerencias de temas serán de esto. «Dinosaurios y fósiles», no «ciencia».</span></div>
    <div class="fields-2">
      <div class="field"><span class="label">Idioma principal</span>
        <div class="segmented" role="radiogroup" aria-label="Idioma principal">
          ${CONFIG.languages.map(l => `<label><input type="radio" name="idioma" value="${l}" ${c.idioma === l ? "checked" : ""}><span>${LANG[l]}</span></label>`).join("")}
        </div>
        <span class="hint">El de sus videos por defecto; siempre puedes hacer la versión en el otro.</span></div>
      <div class="field"><label for="c-edad">Edad del público</label>
        <select id="c-edad" name="edad">${ages.map(a => `<option value="${a}" ${c.edad === a ? "selected" : ""}>${a} años</option>`).join("")}</select></div>
    </div>
    <div class="field"><label for="c-apariencia">Apariencia</label>
      <textarea id="c-apariencia" name="apariencia" maxlength="800">${esc(c.apariencia || "")}</textarea>
      <span class="hint">Formas simples que se puedan recortar en papel, ojos y boca visibles (la boca se anima al hablar) y un objeto con el que señala.</span></div>
    <div class="fields-2">
      <div class="field"><label for="c-personalidad">Personalidad</label>
        <textarea id="c-personalidad" name="personalidad" maxlength="400" rows="3">${esc(c.personalidad || "")}</textarea>
        <span class="hint">Cómo es y cómo habla.</span></div>
      <div class="field"><label for="c-escenario">Escenario</label>
        <textarea id="c-escenario" name="escenario" maxlength="400" rows="3">${esc(c.escenario || "")}</textarea>
        <span class="hint">El lugar fijo, de papel, que abre y cierra cada video.</span></div>
    </div>
    <div class="field"><span class="label">Paleta</span>
      <div class="palette-editor" id="palette"></div>
      <span class="hint">De 3 a 7 colores. Incluye uno oscuro: será el fondo de los subtítulos.</span></div>
    <div class="fields-2">
      <div class="field"><label for="voz-es">Voz en español</label>${voiceSelect("es")}</div>
      <div class="field"><label for="voz-en">Voz en inglés</label>${voiceSelect("en")}</div>
    </div>
    <div class="field"><label for="c-voz-estilo">Cómo suena</label>
      <input id="c-voz-estilo" name="voz_estilo" type="text" maxlength="200" autocomplete="off" value="${esc(c.voz_estilo || "")}" placeholder="Ej.: juvenil, curiosa y un poco susurrada"></div>
    <div class="fields-2">
      <div class="field"><label for="c-cta-es">Frase final en español</label>
        <input id="c-cta-es" name="cta_es" type="text" maxlength="80" autocomplete="off" value="${esc(c.cta?.es || "")}" placeholder="${esc(CONFIG.default_cta.es)}"></div>
      <div class="field"><label for="c-cta-en">Frase final en inglés</label>
        <input id="c-cta-en" name="cta_en" type="text" maxlength="80" autocomplete="off" value="${esc(c.cta?.en || "")}" placeholder="${esc(CONFIG.default_cta.en)}"></div>
    </div>
    <p id="char-error" class="small" style="color:var(--warn)" role="alert"></p>
    <div class="row">
      ${isNew ? "" : `<button type="button" class="btn danger" id="char-delete">Borrar personaje</button>`}
      <span class="spacer"></span>
      <button type="submit" class="btn primary" id="char-save">${isNew ? "Crear y producir su primer video" : "Guardar cambios"}</button>
    </div>
  </form>`;
}

async function renderCharacterEditor(id) {
  await loadCharacters();
  const isNew = !id;
  const existing = isNew ? null : charById(id);
  if (!isNew && !existing) {
    view.innerHTML = `<div class="card empty"><span class="big">No encontré ese personaje</span><a href="#/personajes">Ver personajes</a></div>`;
    return;
  }
  const prefs = loadPrefs();
  const blank = { idioma: prefs.idioma || "es", edad: prefs.edad || "6-9", voces: { es: "ef_dora", en: "af_heart" }, cta: {},
                  paleta: ["#E9B949", "#E8736B", "#3FA796", "#F3E9D2", "#1E2A4F"] };
  let palette = [...(existing || blank).paleta];
  view.innerHTML = `
    <div class="page-head"><div><p class="small"><a href="#/personajes">← Personajes</a></p>
      <h1>${isNew ? "Nuevo personaje" : esc(existing.nombre)}</h1>
      <p>${isNew ? "Elige una idea para empezar y ajústala, o rellena la ficha desde cero. El nicho define de qué tratarán todos sus videos."
        : "Los cambios valen para los próximos videos: los que ya están en producción siguen con la ficha con la que empezaron."}</p></div></div>
    <div class="stack">
      ${isNew ? `<section class="card stack" aria-labelledby="ideas-title">
        <div class="row"><h2 id="ideas-title">1. Ideas</h2><span class="spacer"></span>
          <span class="small muted">Cada idea trae su nicho, su escenario, su paleta y su voz</span></div>
        <div class="fields-3 ideas-controls">
          <div class="field"><span class="label">Idioma</span>
            <div class="segmented" role="radiogroup" aria-label="Idioma de las ideas">
              ${CONFIG.languages.map(l => `<label><input type="radio" name="idea_idioma" value="${l}" ${blank.idioma === l ? "checked" : ""}><span>${LANG[l]}</span></label>`).join("")}
            </div></div>
          <div class="field"><label for="idea-edad">Edad del público</label>
            <select id="idea-edad">${AGES.map(a => `<option value="${a}" ${blank.edad === a ? "selected" : ""}>${a} años</option>`).join("")}</select></div>
          <div class="field"><label for="idea-pista">Sobre qué (opcional)</label>
            <input id="idea-pista" type="text" maxlength="160" autocomplete="off" placeholder="Dinosaurios, cocina, emociones…"></div>
        </div>
        <div class="row"><button type="button" class="btn primary" id="ideas-go">Proponer 3 personajes</button>
          <span class="small muted" id="ideas-note"></span></div>
        <div id="ideas" class="ideas" role="radiogroup" aria-label="Personajes propuestos" aria-live="polite"></div>
      </section>` : ""}
      ${characterForm(existing || blank, isNew)}
    </div>`;

  const form = $("#char-form");
  const drawPalette = () => {
    $("#palette").innerHTML = palette.map((c, i) => `
      <span class="swatch-input"><input type="color" value="${esc(c.toLowerCase())}" data-i="${i}" aria-label="Color ${i + 1}">
        ${palette.length > 3 ? `<button type="button" class="icon-btn" data-remove="${i}" aria-label="Quitar color ${i + 1}">✕</button>` : ""}</span>`).join("")
      + (palette.length < 7 ? `<button type="button" class="btn small" id="palette-add">+ Color</button>` : "");
  };
  drawPalette();
  $("#palette").addEventListener("input", e => { if (e.target.dataset.i) palette[Number(e.target.dataset.i)] = e.target.value.toUpperCase(); });
  $("#palette").addEventListener("click", e => {
    const rm = e.target.closest("[data-remove]");
    if (rm) { palette.splice(Number(rm.dataset.remove), 1); drawPalette(); }
    if (e.target.id === "palette-add") { palette.push("#C9A57A"); drawPalette(); }
  });

  const fill = c => {
    const set = (name, value) => { const el = form.elements[name]; if (el) el.value = value ?? ""; };
    ["nombre", "serie", "nicho", "apariencia", "personalidad", "escenario", "voz_estilo"].forEach(k => set(k, c[k]));
    set("cta_es", c.cta?.es); set("cta_en", c.cta?.en);
    set("voz_es", c.voces?.es); set("voz_en", c.voces?.en);
    const lang = $(`input[name=idioma][value="${c.idioma}"]`, form);
    if (lang) lang.checked = true;
    const edad = $("#c-edad");
    if (![...edad.options].some(o => o.value === c.edad)) edad.add(new Option(`${c.edad} años`, c.edad));
    edad.value = c.edad;
    palette = [...c.paleta];
    drawPalette();
  };

  if (isNew) {
    // Ideas: 3 cada vez; las ya mostradas (y los personajes existentes) no se repiten.
    let ideas = [], shown = [];
    const box = $("#ideas");
    const propose = async () => {
      const idioma = $("input[name=idea_idioma]:checked").value, edad = $("#idea-edad").value;
      $("#ideas-go").disabled = true;
      box.innerHTML = `<p class="small muted">Pensando personajes…</p>`;
      try {
        const res = await api("/api/characters/suggest", { method: "POST", body: { idioma, edad, pista: $("#idea-pista").value, evitar: shown } });
        ideas = res.personajes;
        shown.push(...ideas.map(i => `${i.personaje.nombre} (${i.personaje.nicho})`));
        box.innerHTML = ideas.map((idea, i) => {
          const c = idea.personaje;
          return `<button type="button" class="suggestion idea" role="radio" aria-checked="false" data-i="${i}">
            ${charArt(c)}
            <span class="idea-body"><strong>${esc(c.nombre)}</strong><span class="small">${esc(c.nicho)}</span>
              ${idea.gancho ? `<span class="small muted">${esc(idea.gancho)}</span>` : ""}
              <span class="small muted clamp">${esc(c.apariencia)}</span>${swatches(c.paleta)}</span></button>`;
        }).join("");
        $("#ideas-go").textContent = "Otros 3";
        $("#ideas-note").textContent = "Elige una para rellenar la ficha; luego puedes cambiar lo que quieras.";
      } catch (err) {
        box.innerHTML = `<p class="small" style="color:var(--warn)">${esc(err.message)}</p>`;
      } finally { $("#ideas-go").disabled = false; }
    };
    $("#ideas-go").addEventListener("click", propose);
    $("#idea-pista").addEventListener("keydown", ev => { if (ev.key === "Enter") { ev.preventDefault(); propose(); } });
    box.addEventListener("click", ev => {
      const pick = ev.target.closest(".idea");
      if (!pick) return;
      $$(".idea", box).forEach(b => b.setAttribute("aria-checked", b === pick));
      fill(ideas[Number(pick.dataset.i)].personaje);
      $("#char-error").textContent = "";
    });
  }

  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const d = Object.fromEntries(new FormData(form).entries());
    const body = {
      nombre: d.nombre, serie: d.serie, nicho: d.nicho, idioma: d.idioma, edad: d.edad,
      apariencia: d.apariencia, personalidad: d.personalidad, escenario: d.escenario,
      paleta: palette, voces: { es: d.voz_es, en: d.voz_en }, voz_estilo: d.voz_estilo,
      cta: { es: d.cta_es, en: d.cta_en },
    };
    $("#char-save").disabled = true;
    $("#char-error").textContent = "";
    try {
      const saved = isNew
        ? await api("/api/characters", { method: "POST", body })
        : await api(`/api/characters/${encodeURIComponent(existing.id)}`, { method: "PUT", body });
      toast(isNew ? `${esc(saved.nombre)} ya está en el estudio. Elige el tema de su primer video.` : "Cambios guardados.", "ok");
      location.hash = isNew ? `#/?personaje=${encodeURIComponent(saved.id)}` : "#/personajes";
    } catch (err) {
      $("#char-error").textContent = err.message;
    } finally { $("#char-save").disabled = false; }
  });

  $("#char-delete")?.addEventListener("click", async () => {
    const extra = existing.videos ? ` Sus ${existing.videos} video(s) se conservan en la biblioteca.` : "";
    if (!confirmAction(`¿Borrar a ${existing.nombre}?${extra}`)) return;
    try {
      await api(`/api/characters/${encodeURIComponent(existing.id)}`, { method: "DELETE" });
      toast("Personaje borrado.", "ok");
      location.hash = "#/personajes";
    } catch (err) { toast(esc(err.message), "err"); }
  });
  if (isNew) $("#ideas-go").focus();
}

/* ------------------------------------------------------------------ vista: sistema */

async function renderSystem() {
  view.innerHTML = `<div class="page-head"><div><h1>Sistema</h1><p>Lo que el agente necesita en este equipo para producir los videos.</p></div></div>
    <div class="grid-studio"><section class="card stack" id="deps"><p class="muted">Revisando…</p></section>
    <section class="card stack" id="about"></section></div>`;
  view.insertAdjacentHTML("beforeend", `<section class="card stack" id="publishing" style="margin-top:24px"></section>`);
  mountPublishingSetup($("#publishing"));
  const health = await api("/api/health");
  const group = (kind, title) => `<h2>${title}</h2><ul class="checklist">${health.items.filter(i => i.kind === kind).map(i => `
    <li class="${i.ok ? "ok" : i.optional ? "" : "bad"}"><span class="mark">${i.ok ? "✓" : i.optional ? "·" : "✗"}</span>
      <code>${esc(i.name)}</code><span class="why small muted">${esc(i.why)}${!i.ok && i.optional ? " (opcional)" : ""}</span></li>`).join("")}</ul>`;
  $("#deps").innerHTML = `
    <div class="row">${health.ok ? `<span class="pill done">Todo listo</span>` : `<span class="pill failed">Faltan dependencias</span>`}</div>
    ${group("bin", "Programas")}${group("module", "Módulos de Python")}
    <h2>Fuentes</h2><p class="small">${health.fonts.length ? health.fonts.map(esc).join(", ") : "Ninguna en assets/fonts/ (el agente las descargará)."}</p>
    <h2>Animación (GSAP)</h2><p class="small">${health.vendor.length === health.vendor_expected.length
      ? `${health.vendor.map(esc).join(", ")} en assets/vendor/.`
      : `Falta ${health.vendor_expected.filter(n => !health.vendor.includes(n)).map(esc).join(", ")} en assets/vendor/. Ejecuta <code>./setup.sh</code>; sin GSAP, el agente anima a mano.`}</p>
    ${health.ok ? "" : `<p class="small">Para instalar lo que falta:</p><pre class="cmd">./setup.sh</pre>`}`;
  $("#about").innerHTML = `
    <h2>Cómo funciona</h2>
    <p>Cada video es un trabajo en cola. El agente de Claude sigue el pipeline de <code>prompts/system_prompt.md</code> y
      escribe todo en <code>output/&lt;slug&gt;/</code>. Voz, música, animación y render corren en este equipo.</p>
    <p>Se producen <strong>${CONFIG.concurrency}</strong> video(s) a la vez. Cambia el número con <code>--concurrency</code>.</p>
    <h2>Modelo</h2>
    <p>Por defecto <code>${esc(CONFIG.default_model)}</code>. Puedes elegir otro en «Opciones del agente».</p>
    <h2>Seguridad</h2>
    <p class="small">El agente ejecuta comandos y edita archivos sin pedir confirmación. La app escucha solo en este equipo;
      si la expones con <code>--host 0.0.0.0</code>, pide un token de acceso.</p>
    ${CONFIG.demo ? `<p class="pill running">Modo demostración activo</p>` : ""}`;
}

/* ------------------------------------------------------------------ publicar en TikTok (Buffer + Drive) */

// La primera es la opción por defecto. (La API también admite la cola de Buffer, pero pone el
// video en la siguiente franja del horario del canal, que puede ser dentro de días.)
const PUB_MODES = { now: "Publicar ahora", schedule: "Programar" };
const PUB_HINT = {
  now: "Buffer lo publica en TikTok en cuanto lo recibe.",
  schedule: "Sale en la fecha y hora que elijas.",
};
// Estado real de un envío, según Buffer (se consulta al abrir la ficha).
function pubState(h) {
  const at = iso => iso ? new Date(iso).toLocaleString("es", { dateStyle: "medium", timeStyle: "short" }) : "";
  if (h.mode === "manual") return "Subido a mano";
  return ({ sent: `Publicado ${at(h.sent_at)}`, sending: "Enviándose a TikTok ahora", error: "Falló en Buffer (revísalo allí)",
            deleted: "Borrado en Buffer", draft: "Borrador en Buffer", needs_approval: "Pendiente de aprobación en Buffer",
            scheduled: `Programado para ${at(h.due_at)}` })[h.status] || `Programado para ${at(h.due_at)}`;
}
const fmtUnits = text => text.length; // Buffer cuenta unidades UTF-16, igual que .length en JS

async function mountPublishingSetup(box) {
  let cfg = await api("/api/publishing");
  const draw = () => {
    box.innerHTML = `
      <div class="row"><h2>Publicar en TikTok</h2><span class="spacer"></span>
        ${cfg.ready ? `<span class="pill done">Configurado</span>` : `<span class="pill">Sin configurar</span>`}
        ${cfg.demo ? `<span class="pill running">Simulado en modo demo</span>` : ""}</div>
      <p class="small muted">Buffer publica el video en tu TikTok. Como Buffer no acepta archivos, la app copia el video a una carpeta
        de Google Drive compartida con enlace y le pasa a Buffer ese enlace. Se configura una sola vez.</p>
      <ol class="setup-steps">
        <li><strong>Carpeta de Google Drive</strong>
          <div class="row"><input type="text" id="pub-folder" value="${esc(cfg.drive_folder)}" aria-label="Carpeta de Drive">
            <button class="btn small" id="pub-folder-save">${cfg.drive_folder_exists ? "Guardar" : "Crear carpeta"}</button></div>
          ${cfg.drive_folder_url ? `<a class="btn small" href="${esc(cfg.drive_folder_url)}" target="_blank" rel="noopener">Abrir la carpeta en Drive</a>` : ""}
          <span class="hint">${cfg.drive_folder_exists ? "✓ La carpeta existe." : cfg.drive_roots.length ? "Aún no existe: pulsa «Crear carpeta»." : "No encontré Google Drive para escritorio: instálalo e inicia sesión."}
            Luego, en <a href="https://drive.google.com" target="_blank" rel="noopener">drive.google.com</a>: clic derecho en la carpeta → Compartir →
            Acceso general: <strong>«Cualquier persona con el enlace»</strong> (Lector). Todo lo que pongas en esa carpeta será visible con su enlace.</span></li>
        <li><strong>Clave de API de Buffer</strong>
          <div class="row"><input type="password" id="pub-key" autocomplete="off" aria-label="Clave de API de Buffer"
              placeholder="${cfg.buffer_key ? `Guardada (${esc(cfg.buffer_key_hint)})` : "Pega aquí tu clave"}" ${cfg.buffer_key_env ? "disabled" : ""}>
            <button class="btn small" id="pub-key-save" ${cfg.buffer_key_env ? "disabled" : ""}>Guardar</button></div>
          <span class="hint">${cfg.buffer_key_env ? "Viene de la variable PAPER_STAGE_BUFFER_KEY." :
            "En Buffer, conecta tu cuenta de TikTok y crea una clave en la sección API de la configuración (solo la persona dueña de la organización puede). Se guarda solo en este equipo, en data/."}</span></li>
        <li><strong>Cuenta de TikTok</strong>
          <div class="row"><select id="pub-channel" aria-label="Cuenta de TikTok">
              ${cfg.channel_id ? `<option value="${esc(cfg.channel_id)}">${esc(cfg.channel_name || cfg.channel_id)}</option>` : `<option value="">(ninguna)</option>`}</select>
            <button class="btn small" id="pub-channels" ${cfg.buffer_key ? "" : "disabled"}>Cargar cuentas de Buffer</button></div></li>
        <li><label class="row"><input type="checkbox" id="pub-ai" ${cfg.ai_label ? "checked" : ""}>
          Declarar en TikTok que el contenido está generado con IA</label>
          <span class="hint">Recomendado: el guion, la voz y la animación los hace la IA, y TikTok pide declararlo.</span></li>
      </ol>
      <p id="pub-error" class="small" style="color:var(--warn)" role="alert"></p>
      <div class="row"><button class="btn primary" id="pub-test" ${cfg.buffer_key && cfg.drive_folder_exists ? "" : "disabled"}>Probar la configuración</button>
        <span class="small muted" id="pub-test-status">Sube un video de 1 s a la carpeta y comprueba que Drive lo sirve en público y que Buffer responde.</span></div>`;
  };
  const save = async body => {
    $("#pub-error", box).textContent = "";
    try { cfg = await api("/api/publishing", { method: "PUT", body }); draw(); toast("Guardado.", "ok"); }
    catch (err) { $("#pub-error", box).textContent = err.message; }
  };
  // Mientras corre la prueba, su botón queda bloqueado (también si se vuelve a esta página).
  const followTest = () => followStatus("/api/publishing/test", st => {
    const button = $("#pub-test", box), running = st.status?.state === "running";
    $("#pub-test-status", box).textContent = st.status?.step || $("#pub-test-status", box).textContent;
    button.disabled = running || !(cfg.buffer_key && cfg.drive_folder_exists);
    button.textContent = running ? "Probando…" : "Probar la configuración";
  }, box);
  draw();
  api("/api/publishing/test").then(st => { if (st.status?.state === "running") followTest(); }).catch(() => {});
  box.addEventListener("click", async ev => {
    const id = ev.target.id;
    if (id === "pub-folder-save") save({ drive_folder: $("#pub-folder", box).value, create_folder: true });
    if (id === "pub-key-save") save({ buffer_key: $("#pub-key", box).value });
    if (id === "pub-channels") {
      ev.target.disabled = true;
      try {
        const channels = await api("/api/publishing/channels");
        const sel = $("#pub-channel", box);
        sel.innerHTML = channels.length ? channels.map(c => `<option value="${esc(c.id)}" ${c.id === cfg.channel_id ? "selected" : ""}>${esc(c.nombre)}</option>`).join("")
          : `<option value="">No hay cuentas de TikTok conectadas en Buffer</option>`;
        if (channels.length && !cfg.channel_id) save({ channel_id: channels[0].id, channel_name: channels[0].nombre });
      } catch (err) { $("#pub-error", box).textContent = err.message; }
      finally { ev.target.disabled = false; }
    }
    if (id === "pub-test") {
      ev.target.disabled = true;
      ev.target.textContent = "Probando…";
      try {
        await api("/api/publishing/test", { method: "POST" });
        followTest();
      } catch (err) { $("#pub-error", box).textContent = err.message; draw(); }
    }
  });
  box.addEventListener("change", ev => {
    if (ev.target.id === "pub-channel" && ev.target.value) save({ channel_id: ev.target.value, channel_name: ev.target.selectedOptions[0].textContent });
    if (ev.target.id === "pub-ai") save({ ai_label: ev.target.checked });
  });
}

// Consulta el estado de una publicación cada 2 s mientras siga en curso y el elemento esté en pantalla.
function followStatus(path, paint, el) {
  const tick = async () => {
    if (!document.body.contains(el)) return;
    const st = await api(path);
    paint(st);
    if (st.status?.state === "running") setTimeout(tick, 2000);
  };
  tick();
}

async function mountPublish(box, slug) {
  const path = `/api/videos/${encodeURIComponent(slug)}/publish`;
  let data;
  try { data = await api(path); } catch (err) { box.innerHTML = `<p class="small">${esc(err.message)}</p>`; return; }
  const cfg = data.config;
  const manual = data.history.some(h => h.mode === "manual");
  const history = () => data.history.length ? `<ul class="pub-history">${data.history.map(h => `
    <li>${h.status === "error" ? "✗" : "✓"} ${esc(h.channel_name || "TikTok")} · ${esc(pubState(h))}
      <span class="small muted">${h.mode === "manual" ? "marcado" : "enviado a Buffer"} ${fmtDate(h.created_at)}</span>
      ${h.mode === "manual" ? `<button type="button" class="btn small" data-unmark="${h.id}">Quitar marca</button>` : ""}</li>`).join("")}</ul>` : "";
  const markButton = manual ? "" : `<button type="button" class="btn small" id="pub-mark">Ya lo subí a TikTok: marcar como publicado</button>`;
  box.onclick = async ev => {
    const unmark = ev.target.closest("[data-unmark]");
    if (ev.target.id !== "pub-mark" && !unmark) return;
    if (unmark && !confirmAction("¿Quitar la marca de «subido a mano»?")) return;
    try {
      await api(unmark ? `${path}/${unmark.dataset.unmark}` : `${path}/manual`, { method: unmark ? "DELETE" : "POST" });
      mountPublish(box, slug);
    } catch (err) { toast(esc(err.message), "err"); }
  };
  const status = () => {
    const st = data.status;
    if (!st) return "";
    const cls = st.state === "failed" ? "failed" : st.state === "done" ? "done" : "running";
    const share = st.state === "failed" && cfg.drive_folder_url && /Cualquier persona con el enlace/.test(st.step);
    return `<div class="pub-status ${cls}"><span class="pill ${cls}">${st.state === "running" ? "Publicando" : st.state === "done" ? "Listo" : "Falló"}</span>
      <span>${esc(st.step)}${share ? ` <a class="btn small" href="${esc(cfg.drive_folder_url)}" target="_blank" rel="noopener">Abrir la carpeta en Drive</a>` : ""}</span></div>`;
  };
  if (!cfg.ready) {
    box.innerHTML = `<div class="row"><h3>Publicar en TikTok</h3>${data.history.length ? `<span class="pill done">En TikTok</span>` : ""}</div>${history()}
      <p class="small muted">Para publicar desde aquí, configura Buffer y Google Drive en <a href="#/sistema">Sistema</a>.</p>
      <div class="row">${markButton}</div>`;
    return;
  }
  const running = data.status?.state === "running";
  const sent = data.history.some(h => h.channel_id === cfg.channel_id) || manual;
  box.innerHTML = `
    <div class="row"><h3>Publicar en TikTok</h3>${tiktokPill(data.history.some(h => h.mode === "manual" || h.status === "sent") ? "publicado"
      : data.history.some(h => !["error", "deleted"].includes(h.status)) ? "programado" : null)}
      <span class="spacer"></span><span class="small muted">${esc(cfg.channel_name)}</span></div>
    ${history()}${status()}
    ${manual && !running ? `<p class="small muted">Ya está en TikTok. Si de verdad quieres enviarlo otra vez por Buffer,
      <button type="button" class="btn small" id="pub-show">muestra el formulario</button>.</p>` : ""}
    <div class="row">${running ? "" : markButton}</div>
    <form id="pub-form" ${manual && !running ? "hidden" : ""}>
     <fieldset class="stack plain" ${running ? "disabled" : ""}>
      <div class="field"><label for="pub-text">Texto</label>
        <textarea id="pub-text" rows="4">${esc(data.default_text)}</textarea>
        <span class="hint" id="pub-count"></span></div>
      <div class="segmented" role="radiogroup" aria-label="Cuándo">
        ${Object.entries(PUB_MODES).map(([k, label], i) => `<label><input type="radio" name="pub-mode" value="${k}" ${i === 0 ? "checked" : ""}><span>${label}</span></label>`).join("")}</div>
      <span class="hint" id="pub-hint">${PUB_HINT.now}</span>
      <input type="datetime-local" id="pub-when" class="hidden" aria-label="Fecha y hora">
      <label class="row small"><input type="checkbox" id="pub-ai2" ${cfg.ai_label ? "checked" : ""}> Declarar contenido generado con IA</label>
      <p id="pub-err" class="small" style="color:var(--warn)" role="alert"></p>
      <div class="row"><span class="small muted">La portada será el fotograma de <code>frame_portada_s</code>. El video debe quedarse en Drive hasta que se publique.</span>
        <span class="spacer"></span><button class="btn primary" type="submit" id="pub-submit">${running ? "Publicando…" : sent ? "Publicar otra vez" : "Publicar en TikTok"}</button></div>
     </fieldset>
    </form>`;
  $("#pub-show", box)?.addEventListener("click", () => { $("#pub-form", box).hidden = false; $("#pub-show", box).closest("p").remove(); });
  const count = () => {
    const n = fmtUnits($("#pub-text", box).value);
    $("#pub-count", box).textContent = `${n} / 2200 caracteres`;
    $("#pub-count", box).style.color = n > 2200 ? "var(--warn)" : "";
  };
  count();
  $("#pub-text", box).addEventListener("input", count);
  $$("input[name=pub-mode]", box).forEach(r => r.addEventListener("change", () => {
    $("#pub-when", box).classList.toggle("hidden", r.value !== "schedule" || !r.checked);
    $("#pub-hint", box).textContent = PUB_HINT[r.value];
  }));
  $("#pub-form", box).addEventListener("submit", async ev => {
    ev.preventDefault();
    const mode = $("input[name=pub-mode]:checked", box).value, when = $("#pub-when", box).value;
    if (mode === "now" && !confirmAction("¿Publicar este video en TikTok ahora mismo?")) return;
    if (sent && !confirmAction(manual ? "Este video ya está en TikTok (subido a mano). ¿Enviarlo otra vez por Buffer?"
      : "Este video ya se envió a esa cuenta. ¿Enviarlo otra vez?")) return;
    // Bloquea el formulario desde el clic: un segundo clic no debe lanzar otro envío.
    const fields = $("fieldset", ev.target), button = $("#pub-submit", box), label = button.textContent;
    fields.disabled = true;
    button.textContent = "Publicando…";
    try {
      await api(path, { method: "POST", body: { text: $("#pub-text", box).value, mode, ai_label: $("#pub-ai2", box).checked, again: sent,
        due_at: mode === "schedule" && when ? new Date(when).toISOString() : null } });
      mountPublish(box, slug);
    } catch (err) {
      fields.disabled = false;
      button.textContent = label;
      $("#pub-err", box).textContent = err.message;
    }
  });
  if (running) followStatus(path, st => {
    if (st.status?.state !== "running") { mountPublish(box, slug); return; }
    data = { ...data, ...st };
    box.querySelector(".pub-status")?.replaceWith(Object.assign(document.createElement("div"), { innerHTML: status() }).firstElementChild);
  }, box);
}

/* ------------------------------------------------------------------ enrutador */

async function route() {
  cleanup.forEach(fn => fn());
  cleanup = [];
  const [path, query = ""] = location.hash.replace(/^#/, "").split("?");
  const parts = path.split("/").filter(Boolean);
  const params = new URLSearchParams(query);
  const section = parts[0] === "biblioteca" || parts[0] === "video" ? "library" : parts[0] === "sistema" ? "system"
    : parts[0] === "personajes" ? "characters" : "studio";
  $$("[data-nav]").forEach(a => (a.dataset.nav === section ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  try {
    if (parts[0] === "trabajo" && parts[1]) await renderJob(decodeURIComponent(parts[1]));
    else if (parts[0] === "video" && parts[1]) await renderVideo(decodeURIComponent(parts[1]));
    else if (parts[0] === "personajes" && parts[1] === "nuevo") await renderCharacterEditor(null);
    else if (parts[0] === "personajes" && parts[1]) await renderCharacterEditor(decodeURIComponent(parts[1]));
    else if (parts[0] === "personajes") await renderCharacters();
    else if (parts[0] === "biblioteca") await renderLibrary(params);
    else if (parts[0] === "sistema") await renderSystem();
    else await renderStudio(params);
  } catch (err) {
    view.innerHTML = `<div class="card empty"><span class="big">Algo salió mal</span>${esc(err.message)}</div>`;
  }
  window.scrollTo(0, 0);
}

(async function init() {
  try {
    CONFIG = await api("/api/config");
  } catch (err) {
    view.innerHTML = `<div class="card empty"><span class="big">No hay conexión con el servidor</span>${esc(err.message)}</div>`;
    return;
  }
  window.addEventListener("hashchange", route);
  refreshBanner();
  watchJobs();
  route();
})();
