/* Panel BeamMP — lógica del navegador. JavaScript sin librerías. */
"use strict";

const $ = (id) => document.getElementById(id);
const meta = (name) => document.querySelector(`meta[name="${name}"]`).content;
const CSRF = meta("csrf-token");
const MAX_UPLOAD = Number(meta("max-upload"));
const MAX_IMAGE = Number(meta("max-image"));
const IS_ADMIN = document.body.dataset.role === "admin";
const POLL_MS = 5000;
const LOG_MAX_LINES = 2000;
const SVG_NS = "http://www.w3.org/2000/svg";

// ---------------------------------------------------------------------
// Utilidades
// ---------------------------------------------------------------------
/** Petición a la API. `body` puede ser un objeto (JSON) o un Blob/File (se envía tal cual). */
async function api(method, url, body) {
    const options = { method, headers: { "X-CSRF-Token": CSRF }, credentials: "same-origin" };
    if (body instanceof Blob) {
        options.body = body;
    } else if (body !== undefined) {
        options.headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(body);
    }
    let response;
    try {
        response = await fetch(url, options);
    } catch (err) {
        throw new Error("No se puede contactar con el panel.");
    }
    if (response.status === 401) {
        window.location.href = "/login";
        throw new Error("Sesión caducada.");
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Error ${response.status}`);
    return data;
}

function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
        if (key === "class") node.className = value;
        else if (key === "dataset") Object.assign(node.dataset, value);
        else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
        else if (value !== false && value != null) node.setAttribute(key, value === true ? "" : value);
    }
    node.append(...children.filter((child) => child != null));
    return node;
}

function icon(id) {
    const svg = document.createElementNS(SVG_NS, "svg");
    const use = document.createElementNS(SVG_NS, "use");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    use.setAttribute("href", "#" + id);
    svg.append(use);
    return svg;
}

/** Escribe texto solo si ha cambiado: evita trabajo del navegador en cada refresco. */
function setText(id, text) {
    const node = $(id);
    if (node.textContent !== text) node.textContent = text;
}

/** Ejecuta `task` salvo que ya haya otra igual en curso (los refrescos no se solapan). */
const running = new Set();
async function once(key, task) {
    if (running.has(key)) return;
    running.add(key);
    try { await task(); } finally { running.delete(key); }
}

function toast(message, isError = false) {
    const node = el("div", { class: "toast" + (isError ? " is-error" : ""), role: isError ? "alert" : "status" }, message);
    $("toasts").append(node);
    setTimeout(() => node.remove(), isError ? 8000 : 4000);
}

/** Diálogo modal. Con `input` pide además un texto. Devuelve null si se cancela, o el texto (vacío sin `input`). */
function askDialog({ title, text, ok, input }) {
    const dialog = $("confirm");
    const field = $("confirm-input");
    $("confirm-title").textContent = title;
    $("confirm-text").textContent = text;
    $("confirm-ok").textContent = ok;
    $("confirm-field").hidden = !input;
    field.value = "";
    if (input) {
        $("confirm-field-label").textContent = input.label;
        field.placeholder = input.placeholder || "";
    }
    return new Promise((resolve) => {
        dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok" ? field.value.trim() : null), { once: true });
        dialog.returnValue = "cancel";
        dialog.showModal();
    });
}

const confirmDialog = async (title, text, ok) => (await askDialog({ title, text, ok })) !== null;

function formatSize(bytes) {
    if (bytes == null) return "–";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
    return `${bytes.toFixed(i === 0 || bytes >= 100 ? 0 : 1)} ${units[i]}`;
}

function formatUptime(seconds) {
    if (seconds == null) return "";
    const d = Math.floor(seconds / 86400), h = Math.floor((seconds % 86400) / 3600), m = Math.floor((seconds % 3600) / 60);
    if (d) return `Encendido desde hace ${d} d ${h} h`;
    if (h) return `Encendido desde hace ${h} h ${m} min`;
    return m ? `Encendido desde hace ${m} min` : "Recién arrancado";
}

const plural = (count) => `${count} jugador${count === 1 ? "" : "es"}`;
const formatDate = (seconds) => new Date(seconds * 1000).toLocaleString("es-ES", { dateStyle: "short", timeStyle: "short" });

/** Ejecuta una acción de la API y avisa del resultado. Devuelve la respuesta, o null si falla. */
async function act(request, done) {
    try {
        const data = await request();
        if (done) toast(done);
        return data;
    } catch (err) {
        toast(err.message, true);
        return null;
    }
}

/** Interruptor naranja suelto (fuera de una fila de opciones). */
function switchControl(checked, label, onchange) {
    return el("label", { class: "switch", title: label },
        el("input", { type: "checkbox", checked, "aria-label": label, onchange }),
        el("span", { class: "switch-track", "aria-hidden": "true" }));
}

const smallButton = (label, onclick, cls = "") => el("button", { class: `btn btn-small ${cls}`, type: "button", onclick }, label);

// ---------------------------------------------------------------------
// Navegación entre secciones
// ---------------------------------------------------------------------
const VIEWS = [...document.querySelectorAll(".tabs a")].map((link) => link.dataset.view);
const onShow = {};
let currentView = null;

function showView() {
    const wanted = window.location.hash.slice(1);
    currentView = VIEWS.includes(wanted) ? wanted : "resumen";
    for (const name of VIEWS) $("view-" + name).hidden = name !== currentView;
    for (const link of document.querySelectorAll(".tabs a")) {
        if (link.dataset.view === currentView) link.setAttribute("aria-current", "page");
        else link.removeAttribute("aria-current");
    }
    if (onShow[currentView]) onShow[currentView]();
}
window.addEventListener("hashchange", showView);

// ---------------------------------------------------------------------
// Resumen: estado del servidor y jugadores
// ---------------------------------------------------------------------
const STATE_TEXT = {
    online: "En marcha",
    starting: "Arrancando…",
    stopping: "Deteniéndose…",
    offline: "Detenido",
    failed: "Ha fallado al arrancar. Mira el registro",
    missing: "El servicio systemd no existe",
    unknown: "Estado desconocido",
};
let lastStatus = null;
let shownPlayers = null;
let shownPreview;

function refreshStatus() {
    return once("status", async () => {
        let status;
        try {
            status = await api("GET", "/api/status");
        } catch (err) {
            $("state").dataset.state = $("bar-state").dataset.state = "unknown";
            setText("state-text", err.message);
            setText("bar-state-text", "Sin conexión");
            return;
        }
        lastStatus = status;
        renderStatus(status);
    });
}

function renderStatus(status) {
    const state = status.service.state;
    const online = state === "online";
    const running = online || state === "starting";
    const { count, max, list } = status.players;

    setText("server-name", status.server_name || "Servidor BeamMP");
    $("state").dataset.state = $("bar-state").dataset.state = state;
    setText("state-text", STATE_TEXT[state] || state);
    setText("bar-state-text", STATE_TEXT[state] || state);
    setText("uptime", formatUptime(status.service.uptime));
    $("btn-start").hidden = running;
    $("btn-restart").hidden = !running;
    $("btn-stop").hidden = !running;

    setText("gauge-number", online ? `${count}/${max}` : "–");
    const fill = online && max > 0 ? Math.min(100, (count / max) * 100) : 0;
    $("gauge-value").setAttribute("stroke-dasharray", `${fill} 100`);
    setText("bar-players", online ? `${count}/${max} jugadores` : "");

    setText("current-map", status.map.name);
    setText("current-map-path", status.map.name === status.map.path ? "" : status.map.path);
    setText("bar-map", status.map.name);
    if (status.map.preview !== shownPreview) {
        shownPreview = status.map.preview;
        for (const img of [$("current-map-img"), $("backdrop")]) {
            img.hidden = !shownPreview;
            if (shownPreview) img.src = shownPreview;
        }
    }

    const playersKey = JSON.stringify(list);
    if (playersKey !== shownPlayers) {
        shownPlayers = playersKey;
        $("players").replaceChildren(...list.map(playerRow));
    }
    $("players-empty").hidden = list.length > 0;
    $("chat-form").hidden = !online;
    $("bridge-missing").hidden = status.bridge !== false;
    renderMetrics(status.metrics);
    setText("players-empty", online ? "Nadie conectado ahora mismo." : "El servidor no está en marcha.");

    $("default-credentials").hidden = !status.default_credentials;
    $("config-missing").hidden = status.config_found;
}

function playerRow(player) {
    const details = [player.id != null ? `ID ${player.id}` : null].filter(Boolean).join(" · ");
    return el("li", { class: "row" },
        el("span", { class: "row-main" }, player.name,
            player.guest ? el("span", { class: "tag tag-muted" }, "Invitado") : null,
            details ? el("small", {}, details) : null),
        el("span", { class: "row-actions" },
            smallButton("Expulsar", () => kickPlayer(player.name), "btn-danger"),
            IS_ADMIN ? smallButton("Bloquear", () => banPlayer(player.name)) : null));
}

function setMeter(id, used, total, text) {
    const percent = used != null && total ? Math.min(100, (used / total) * 100) : 0;
    const fill = $("meter-" + id);
    fill.style.width = `${percent}%`;
    fill.classList.toggle("is-high", percent >= 90);
    setText(`meter-${id}-text`, text);
}

function renderMetrics(m) {
    setMeter("cpu", m.cpu, 100, m.cpu == null ? "–" : `${m.cpu.toFixed(0)} %`);
    setMeter("mem", m.mem_used, m.mem_total, m.mem_total ? `${formatSize(m.mem_used)} de ${formatSize(m.mem_total)}` : "–");
    setMeter("disk", m.disk_used, m.disk_total, m.disk_total ? `${formatSize(m.disk_used)} de ${formatSize(m.disk_total)}` : "–");
    const parts = [];
    if (m.process_cpu != null) parts.push(`${m.process_cpu.toFixed(0)} % de CPU`);
    if (m.process_mem != null) parts.push(`${formatSize(m.process_mem)} de memoria`);
    setText("process-usage", parts.length ? `BeamMP Server usa ${parts.join(" y ")}.` : "");
}

async function banPlayer(name) {
    const reason = await askDialog({
        title: `¿Bloquear a ${name}?`,
        text: "Se le expulsa y no podrá volver a entrar hasta que lo desbloquees en Jugadores.",
        ok: "Bloquear",
        input: { label: "Motivo (opcional, lo verá el jugador)", placeholder: "Bloqueado por un administrador" },
    });
    if (reason === null) return;
    await act(() => api("POST", "/api/access/ban", { name, reason }), `${name} ha sido bloqueado`);
    refreshStatus();
}

$("chat-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("chat-message");
    if (await act(() => api("POST", "/api/chat", { message: input.value }), "Mensaje enviado al chat")) input.value = "";
});

async function kickPlayer(name) {
    const reason = await askDialog({
        title: `¿Expulsar a ${name}?`,
        text: "Se le desconecta del servidor. Podrá volver a entrar.",
        ok: "Expulsar",
        input: { label: "Motivo (opcional, lo verá el jugador)", placeholder: "Expulsado por un administrador" },
    });
    if (reason === null) return;
    try {
        await api("POST", "/api/players/kick", { name, reason });
        toast(`${name} ha sido expulsado`);
    } catch (err) {
        toast(err.message, true);
    }
    refreshStatus();
}

const ACTION_TEXT = {
    start: { done: "Arrancando el servidor" },
    restart: { done: "Reiniciando el servidor", title: "¿Reiniciar el servidor?", ok: "Reiniciar" },
    stop: { done: "Deteniendo el servidor", title: "¿Detener el servidor?", ok: "Detener" },
};

function disconnectWarning() {
    const players = lastStatus ? lastStatus.players.count : 0;
    return players > 0 ? `Se desconectará a ${plural(players)}.` : "";
}

async function serverAction(action) {
    const text = ACTION_TEXT[action];
    if (text.title && !(await confirmDialog(text.title, disconnectWarning() || "Ahora mismo no hay nadie conectado.", text.ok))) return;
    try {
        await api("POST", `/api/server/${action}`);
        toast(text.done);
        $("restart-hint").hidden = true;
    } catch (err) {
        toast(err.message, true);
    }
    setTimeout(refreshStatus, 800);
}

document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-server-action]");
    if (button) serverAction(button.dataset.serverAction);
});

// ---------------------------------------------------------------------
// Mapas
// ---------------------------------------------------------------------
let mapData = null;
let mapDataKey = "";
let imageTarget = null;

function loadMaps() {
    return once("maps", async () => {
        try {
            const data = await api("GET", "/api/maps");
            const key = JSON.stringify(data);
            if (key === mapDataKey) return; // nada ha cambiado: no se toca el DOM
            mapData = data;
            mapDataKey = key;
            renderMaps();
        } catch (err) {
            toast(err.message, true);
        }
    });
}

function renderMaps() {
    $("mod-maps").replaceChildren(...mapData.mods.map(mapTile));
    $("official-maps").replaceChildren(...mapData.official.map(mapTile));
    $("mod-maps-empty").hidden = mapData.mods.length > 0;
    filterMaps();
}

/** El buscador solo oculta o muestra mosaicos ya creados. */
function filterMaps() {
    const query = $("map-filter").value.trim().toLowerCase();
    for (const [grid, counter, total] of [["mod-maps", "mod-map-count", mapData.mods.length], ["official-maps", "official-map-count", mapData.official.length]]) {
        let visible = 0;
        for (const tile of $(grid).children) {
            tile.hidden = query !== "" && !tile.dataset.search.includes(query);
            if (!tile.hidden) visible++;
        }
        setText(counter, total ? `(${visible})` : "");
    }
}

function mapTile(map) {
    const current = map.path === mapData.current;
    let note = null;
    if (map.detected === false) note = el("small", { class: "warn" }, "No se encuentra su .zip en la carpeta de mods");
    else if (map.mod) note = el("small", {}, map.mod);

    const tool = (iconId, label, onclick) => el("button", { class: "tile-tool", type: "button", title: label, "aria-label": `${label}: ${map.name}`, onclick }, icon(iconId));
    const tools = map.folder ? el("span", { class: "tile-tools" },
        tool("i-imagen", "Cambiar imagen", () => { imageTarget = map; $("image-input").click(); }),
        map.custom_preview ? tool("i-quitar", "Quitar imagen propia", () => removeMapImage(map)) : null) : null;

    return el("div", { class: "tile" + (current ? " is-current" : ""), dataset: { search: `${map.name} ${map.path}`.toLowerCase() } },
        el("button", { class: "tile-main", type: "button", disabled: current, "aria-label": current ? `${map.name}, en uso` : `Cambiar a ${map.name}`, onclick: () => selectMap(map) },
            map.preview ? el("img", { class: "tile-img", loading: "lazy", decoding: "async", alt: "", src: map.preview }) : null),
        current ? el("span", { class: "tile-tag" }, "En uso") : null,
        el("span", { class: "tile-label" }, map.name, note),
        tools);
}

async function selectMap(map) {
    const detail = `El servidor se reiniciará con este mapa. ${disconnectWarning()}`.trim();
    if (!(await confirmDialog(`¿Cambiar a ${map.name}?`, detail, "Cambiar mapa"))) return;
    try {
        await api("POST", "/api/maps/select", { path: map.path });
        toast(`Mapa cambiado a ${map.name}. Reiniciando el servidor`);
    } catch (err) {
        toast(err.message, true);
    }
    loadMaps();
    refreshStatus();
}

async function changeMapImage(method, map, body, done) {
    try {
        await api(method, `/api/maps/image/${encodeURIComponent(map.folder)}`, body);
        toast(done);
    } catch (err) {
        toast(err.message, true);
    }
    loadMaps();
    refreshStatus();
}

async function removeMapImage(map) {
    if (await confirmDialog(`¿Quitar la imagen de ${map.name}?`, "Volverá a mostrarse la imagen original del mapa.", "Quitar imagen")) {
        changeMapImage("DELETE", map, undefined, "Imagen quitada");
    }
}

$("image-input").addEventListener("change", (event) => {
    const [file] = event.target.files;
    event.target.value = "";
    if (!file || !imageTarget) return;
    if (file.size > MAX_IMAGE) { toast(`La imagen supera el máximo de ${formatSize(MAX_IMAGE)}.`, true); return; }
    changeMapImage("PUT", imageTarget, file, `Imagen de ${imageTarget.name} actualizada`);
});
$("map-filter").addEventListener("input", () => { if (mapData) filterMaps(); });
onShow.mapas = loadMaps;

// ---------------------------------------------------------------------
// Mods: listado, subida y borrado
// ---------------------------------------------------------------------
const KIND_TEXT = { map: "Mapa", vehicle: "Vehículo", other: "Otro", invalid: "Zip dañado" };

function renderMods(data) {
    $("mods-body").replaceChildren(...data.mods.map((mod) => {
        const kind = mod.kind === "map" ? `Mapa: ${mod.levels.join(", ")}` : KIND_TEXT[mod.kind];
        return el("tr", { class: mod.enabled ? "" : "is-off" },
            el("td", {}, mod.name),
            el("td", { class: "kind", dataset: { kind: mod.kind } }, kind),
            el("td", { class: "num" }, formatSize(mod.size)),
            el("td", {}, new Date(mod.modified * 1000).toLocaleDateString("es-ES")),
            el("td", {}, switchControl(mod.enabled, `${mod.name} activo`, (event) => toggleMod(mod.name, event.target.checked))),
            el("td", {}, el("button", { class: "btn btn-small btn-danger", type: "button", onclick: () => deleteMod(mod.name) }, "Borrar")));
    }));
    $("mods-table").parentElement.hidden = data.mods.length === 0;
    $("mods-empty").hidden = data.mods.length > 0;
    const total = data.mods.reduce((sum, mod) => sum + mod.size, 0);
    const off = data.mods.filter((mod) => !mod.enabled).length;
    setText("mods-foot", `${data.mods.length} mods${off ? ` (${off} desactivados)` : ""}, ${formatSize(total)} en ${data.folder}. Espacio libre: ${formatSize(data.disk.free)}.`);
}

async function loadMods() {
    try { renderMods(await api("GET", "/api/mods")); } catch (err) { toast(err.message, true); }
}

async function toggleMod(name, enabled) {
    if (await act(() => api("POST", `/api/mods/${encodeURIComponent(name)}/enabled`, { enabled }), `${name} ${enabled ? "activado" : "desactivado"}`)) {
        $("restart-hint").hidden = false;
    }
    loadMods();
}

async function deleteMod(name) {
    if (!(await confirmDialog(`¿Borrar ${name}?`, "El archivo se elimina del servidor. No se puede deshacer.", "Borrar mod"))) return;
    try {
        await api("DELETE", `/api/mods/${encodeURIComponent(name)}`);
        toast(`${name} borrado`);
        $("restart-hint").hidden = false;
    } catch (err) {
        toast(err.message, true);
    }
    loadMods();
}

/** Sube un .zip con XMLHttpRequest (fetch no informa del progreso de subida). */
function sendMod(file, overwrite, onProgress) {
    return new Promise((resolve) => {
        const xhr = new XMLHttpRequest();
        xhr.open("PUT", `/api/mods/${encodeURIComponent(file.name)}${overwrite ? "?overwrite=1" : ""}`);
        xhr.setRequestHeader("X-CSRF-Token", CSRF);
        xhr.setRequestHeader("Content-Type", "application/zip");
        xhr.responseType = "json";
        xhr.upload.onprogress = (event) => { if (event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 100)); };
        xhr.onerror = () => resolve({ status: 0, body: {} });
        xhr.onload = () => resolve({ status: xhr.status, body: xhr.response || {} });
        xhr.send(file);
    });
}

async function uploadFile(file) {
    const status = el("span", {}, "En cola");
    const bar = el("progress", { max: "100", value: "0" });
    $("uploads").append(el("li", {}, el("span", {}, `${file.name} (${formatSize(file.size)})`), status, bar));
    const finish = (message, cls) => { status.textContent = message; status.className = cls; bar.remove(); };

    if (!file.name.toLowerCase().endsWith(".zip")) return finish("Solo se admiten .zip", "is-error");
    if (file.size > MAX_UPLOAD) return finish(`Supera el máximo de ${formatSize(MAX_UPLOAD)}`, "is-error");

    const onProgress = (percent) => {
        bar.value = percent;
        status.textContent = percent < 100 ? `${percent} %` : "Comprobando…";
    };
    let result = await sendMod(file, false, onProgress);
    if (result.status === 409 && await confirmDialog(`${file.name} ya existe`, "¿Quieres sustituirlo por el archivo nuevo?", "Sustituir")) {
        result = await sendMod(file, true, onProgress);
    }
    if (result.status === 201) {
        finish("Subido", "is-done");
        $("restart-hint").hidden = false;
    } else if (result.status === 401) {
        window.location.href = "/login";
    } else {
        finish(result.body.error || (result.status ? `Error ${result.status}` : "Se ha cortado la conexión"), "is-error");
    }
}

async function uploadFiles(files) {
    // De uno en uno: los mapas pesan mucho y así el progreso es fiable.
    for (const file of files) await uploadFile(file);
    loadMods();
}

const dropzone = $("dropzone");
for (const type of ["dragenter", "dragover"]) {
    dropzone.addEventListener(type, (event) => { event.preventDefault(); dropzone.classList.add("is-over"); });
}
for (const type of ["dragleave", "drop"]) {
    dropzone.addEventListener(type, (event) => { event.preventDefault(); dropzone.classList.remove("is-over"); });
}
dropzone.addEventListener("drop", (event) => uploadFiles([...event.dataTransfer.files]));
$("file-input").addEventListener("change", (event) => { uploadFiles([...event.target.files]); event.target.value = ""; });
$("rescan").addEventListener("click", async () => {
    try { renderMods(await api("POST", "/api/mods/rescan")); toast("Mods analizados de nuevo"); } catch (err) { toast(err.message, true); }
});
setText("upload-limit", `Máximo ${formatSize(MAX_UPLOAD)} por archivo.`);
onShow.mods = loadMods;

// ---------------------------------------------------------------------
// Ajustes del servidor
// ---------------------------------------------------------------------
const CONFIG_LABELS = {
    Name: ["Nombre del servidor", "text"],
    Description: ["Descripción", "textarea"],
    Tags: ["Etiquetas", "text", "Separadas por comas. Por ejemplo: Freeroam, Racing"],
    MaxPlayers: ["Jugadores máximos", "number"],
    MaxCars: ["Coches por jugador", "number"],
    Private: ["Servidor privado", "checkbox", "No aparece en la lista pública"],
    AllowGuests: ["Permitir invitados sin cuenta", "checkbox"],
    LogChat: ["Guardar el chat en el registro", "checkbox"],
    Debug: ["Registro detallado (debug)", "checkbox"],
};

function configRow(key, value) {
    const [label, type, help] = CONFIG_LABELS[key];
    const caption = el("span", { class: "row-label" }, label, help ? el("small", {}, help) : null);
    if (type === "checkbox") {
        return el("label", { class: "row" }, caption,
            el("input", { type, name: key, checked: value === true }),
            el("span", { class: "switch-track", "aria-hidden": "true" }));
    }
    const input = type === "textarea"
        ? el("textarea", { name: key }, String(value))
        : el("input", { type, name: key, value: String(value), min: type === "number" ? "1" : null });
    return el("label", { class: "row" }, caption, el("span", { class: "row-control" }, input));
}

async function loadConfig() {
    let values;
    try { values = (await api("GET", "/api/config")).values; } catch (err) { toast(err.message, true); return; }
    $("config-fields").replaceChildren(...Object.keys(CONFIG_LABELS).filter((key) => key in values).map((key) => configRow(key, values[key])));
}

$("sub-servidor").addEventListener("submit", async (event) => {
    event.preventDefault();
    const restart = event.submitter && event.submitter.dataset.restart === "1";
    const values = {};
    for (const input of $("config-fields").querySelectorAll("[name]")) {
        if (input.type === "checkbox") values[input.name] = input.checked;
        else if (input.type === "number") values[input.name] = Number.parseInt(input.value, 10);
        else values[input.name] = input.value;
    }
    try {
        await api("POST", "/api/config", { values, restart });
        toast(restart ? "Ajustes guardados. Reiniciando el servidor" : "Ajustes guardados");
    } catch (err) {
        toast(err.message, true);
    }
    refreshStatus();
});
// Apartados de Ajustes (lista lateral)
const SUB_LOADERS = { servidor: loadConfig, programacion: () => loadSchedule(), actualizacion: () => loadUpdate(false), copias: () => loadBackups() };
let currentSub = "servidor";

function showSub(name) {
    currentSub = name;
    for (const button of document.querySelectorAll(".subnav button")) {
        const active = button.dataset.sub === name;
        button.setAttribute("aria-current", String(active));
        $("sub-" + button.dataset.sub).hidden = !active;
    }
    SUB_LOADERS[name]();
}
for (const button of document.querySelectorAll(".subnav button")) button.addEventListener("click", () => showSub(button.dataset.sub));
onShow.ajustes = () => showSub(currentSub);

// ---------------------------------------------------------------------
// Programación: reinicios y rotación de mapas
// ---------------------------------------------------------------------
let scheduleTimes = [];
let rotationMaps = [];
let mapNames = new Map();

function renderTimes() {
    $("restart-times").replaceChildren(...scheduleTimes.map((time) => el("span", { class: "chip" }, time,
        el("button", { type: "button", "aria-label": `Quitar ${time}`, onclick: () => { scheduleTimes = scheduleTimes.filter((t) => t !== time); renderTimes(); } }, icon("i-quitar")))));
}

function renderRotation() {
    const move = (index, step) => {
        const [item] = rotationMaps.splice(index, 1);
        rotationMaps.splice(index + step, 0, item);
        renderRotation();
    };
    $("rotation-maps").replaceChildren(...rotationMaps.map((path, index) => el("li", { class: "row" },
        el("span", { class: "row-main" }, mapNames.get(path) || path),
        el("span", { class: "row-actions" },
            index > 0 ? smallButton("Subir", () => move(index, -1)) : null,
            index < rotationMaps.length - 1 ? smallButton("Bajar", () => move(index, 1)) : null,
            smallButton("Quitar", () => { rotationMaps.splice(index, 1); renderRotation(); }, "btn-danger")))));
    $("rotation-maps-empty").hidden = rotationMaps.length >= 2;
    const free = [...mapNames].filter(([path]) => !rotationMaps.includes(path));
    $("rotation-map-select").replaceChildren(...free.map(([path, name]) => el("option", { value: path }, name)));
    $("rotation-map-add").disabled = free.length === 0;
}

function renderSchedule(data) {
    const { restart, rotation } = data.schedule;
    scheduleTimes = [...restart.times];
    rotationMaps = rotation.maps.filter((path) => mapNames.has(path));
    $("restart-enabled").checked = restart.enabled;
    $("restart-warn").value = restart.warn_minutes;
    $("rotation-enabled").checked = rotation.enabled;
    $("rotation-hours").value = rotation.every_hours;
    $("rotation-empty").checked = rotation.only_when_empty;
    setText("server-time", data.server_time);
    setText("next-restart", data.upcoming.restart ? `Próximo reinicio: ${data.upcoming.restart}` : "");
    setText("next-rotation", data.upcoming.rotation
        ? `Próximo cambio: ${data.upcoming.rotation}${data.upcoming.rotation_map ? `, a ${data.upcoming.rotation_map}` : ""}`
        : "");
    renderTimes();
    renderRotation();
}

async function loadSchedule() {
    const [maps, data] = await Promise.all([act(() => api("GET", "/api/maps")), act(() => api("GET", "/api/schedule"))]);
    if (!maps || !data) return;
    mapNames = new Map([...maps.official, ...maps.mods].map((map) => [map.path, map.name]));
    renderSchedule(data);
}

$("restart-time-add").addEventListener("click", () => {
    const value = $("restart-time-input").value;
    if (!value || scheduleTimes.includes(value)) return;
    scheduleTimes = [...scheduleTimes, value].sort();
    $("restart-time-input").value = "";
    renderTimes();
});
$("rotation-map-add").addEventListener("click", () => {
    const path = $("rotation-map-select").value;
    if (path) { rotationMaps.push(path); renderRotation(); }
});
$("sub-programacion").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = await act(() => api("POST", "/api/schedule", {
        restart: { enabled: $("restart-enabled").checked, times: scheduleTimes, warn_minutes: Number.parseInt($("restart-warn").value, 10) || 0 },
        rotation: { enabled: $("rotation-enabled").checked, maps: rotationMaps, every_hours: Number.parseInt($("rotation-hours").value, 10) || 0, only_when_empty: $("rotation-empty").checked },
    }), "Programación guardada");
    if (data) renderSchedule(data);
});

// ---------------------------------------------------------------------
// Actualización de BeamMP Server
// ---------------------------------------------------------------------
async function loadUpdate(force) {
    setText("update-note", "Consultando…");
    const info = await act(() => api("GET", "/api/update" + (force ? "?force=1" : "")));
    if (!info) { setText("update-note", ""); return; }
    setText("update-current", info.current ? `v${info.current}` : "Desconocida");
    setText("update-latest", info.latest ? `v${info.latest}` : "–");
    let note = "BeamMP Server está al día.";
    if (info.error) note = info.error;
    else if (!info.asset) note = "No hay un binario publicado para el sistema de esta máquina.";
    else if (info.update_available) note = info.current ? "Hay una versión nueva." : "No se conoce la versión instalada (aparece en el registro al arrancar). Puedes instalar la última igualmente.";
    setText("update-note", note);
    const button = $("update-apply");
    button.hidden = !info.update_available;
    button.textContent = `Actualizar a v${info.latest}`;
}

$("update-check").addEventListener("click", () => loadUpdate(true));
$("update-apply").addEventListener("click", async () => {
    if (!(await confirmDialog("¿Actualizar BeamMP Server?", `Se descargará la última versión y el servidor se reiniciará. ${disconnectWarning()}`.trim(), "Actualizar"))) return;
    $("update-apply").disabled = true;
    const result = await act(() => api("POST", "/api/update"));
    $("update-apply").disabled = false;
    if (result) toast(`BeamMP Server actualizado a v${result.version}`);
    setTimeout(() => { loadUpdate(false); refreshStatus(); }, 3000);
});

// ---------------------------------------------------------------------
// Copias de seguridad
// ---------------------------------------------------------------------
async function loadBackups() {
    const data = await act(() => api("GET", "/api/backups"));
    if (!data) return;
    $("backups").replaceChildren(...data.backups.map((backup) => el("li", { class: "row" },
        el("span", { class: "row-main" }, backup.name,
            backup.includes_mods ? el("span", { class: "tag" }, "Con mods") : null,
            el("small", {}, `${formatDate(backup.created)} · ${formatSize(backup.size)}`)),
        el("span", { class: "row-actions" },
            el("a", { class: "btn btn-small", href: `/api/backups/${encodeURIComponent(backup.name)}`, download: backup.name }, "Descargar"),
            smallButton("Restaurar", () => restoreBackup(backup)),
            smallButton("Borrar", () => deleteBackup(backup), "btn-danger")))));
    $("backups-empty").hidden = data.backups.length > 0;
}

async function restoreBackup(backup) {
    const detail = `Se sobrescriben los ajustes actuales${backup.includes_mods ? " y los mods que estén en la copia" : ""}, y el servidor se reinicia. ${disconnectWarning()}`.trim();
    if (!(await confirmDialog(`¿Restaurar ${backup.name}?`, detail, "Restaurar"))) return;
    const result = await act(() => api("POST", `/api/backups/${encodeURIComponent(backup.name)}/restore`, { restart: true }));
    if (result) {
        const r = result.restored;
        toast(`Copia restaurada: ${r.config ? "configuración, " : ""}${r.panel + r.images} archivos del panel y ${r.mods} mods`);
        mapDataKey = "";
    }
    refreshStatus();
}

async function deleteBackup(backup) {
    if (!(await confirmDialog(`¿Borrar ${backup.name}?`, "La copia se elimina del servidor. No se puede deshacer.", "Borrar copia"))) return;
    await act(() => api("DELETE", `/api/backups/${encodeURIComponent(backup.name)}`), "Copia borrada");
    loadBackups();
}

$("backup-create").addEventListener("click", async (event) => {
    event.target.disabled = true;
    await act(() => api("POST", "/api/backups", { include_mods: $("backup-mods").checked }), "Copia creada");
    event.target.disabled = false;
    loadBackups();
});
$("backup-input").addEventListener("change", async (event) => {
    const [file] = event.target.files;
    event.target.value = "";
    if (!file) return;
    if (file.size > MAX_UPLOAD) { toast(`La copia supera el máximo de ${formatSize(MAX_UPLOAD)}.`, true); return; }
    toast(`Subiendo ${file.name}…`);
    await act(() => api("PUT", `/api/backups/${encodeURIComponent(file.name)}`, file), "Copia subida");
    loadBackups();
});

// ---------------------------------------------------------------------
// Jugadores: bloqueos y lista blanca
// ---------------------------------------------------------------------
let accessData = null;

function renderAccess(data) {
    accessData = data;
    $("bans").replaceChildren(...data.bans.map((ban) => el("li", { class: "row" },
        el("span", { class: "row-main" }, ban.name, el("small", {}, `${ban.reason}${ban.date ? ` · ${formatDate(ban.date)}` : ""}`)),
        smallButton("Desbloquear", async () => { await act(() => api("POST", "/api/access/unban", { name: ban.name }), `${ban.name} desbloqueado`); loadAccess(); }))));
    $("bans-empty").hidden = data.bans.length > 0;
    setText("ban-count", data.bans.length ? `(${data.bans.length})` : "");
    $("whitelist").replaceChildren(...data.whitelist.map((name) => el("li", { class: "row" },
        el("span", { class: "row-main" }, name),
        smallButton("Quitar", () => saveWhitelist(accessData.whitelist_enabled, accessData.whitelist.filter((n) => n !== name)), "btn-danger"))));
    $("whitelist-empty").hidden = data.whitelist.length > 0;
    setText("whitelist-count", data.whitelist.length ? `(${data.whitelist.length})` : "");
    $("whitelist-enabled").checked = data.whitelist_enabled;
}

async function loadAccess() {
    const data = await act(() => api("GET", "/api/access"));
    if (data) renderAccess(data);
}

async function saveWhitelist(enabled, names) {
    const data = await act(() => api("POST", "/api/access/whitelist", { enabled, names }));
    if (data) renderAccess(data);
    else if (accessData) renderAccess(accessData); // deshace el interruptor
}

if (IS_ADMIN) {
    $("ban-form").addEventListener("submit", async (event) => {
        event.preventDefault();
        const { name, reason } = Object.fromEntries(new FormData(event.target));
        if (await act(() => api("POST", "/api/access/ban", { name: name.trim(), reason }), `${name.trim()} ha sido bloqueado`)) event.target.reset();
        loadAccess();
        refreshStatus();
    });
    $("whitelist-form").addEventListener("submit", (event) => {
        event.preventDefault();
        const name = new FormData(event.target).get("name").trim();
        event.target.reset();
        if (name && accessData) saveWhitelist(accessData.whitelist_enabled, [...accessData.whitelist, name]);
    });
    $("whitelist-enabled").addEventListener("change", (event) => {
        if (accessData) saveWhitelist(event.target.checked, accessData.whitelist);
    });
    onShow.jugadores = loadAccess;
}

// ---------------------------------------------------------------------
// Registro: solo se piden y se pintan las líneas nuevas
// ---------------------------------------------------------------------
let logCursor = null;

function logLine(line) {
    const level = /\[(WARN|ERROR|FATAL|DEBUG)\]/.exec(line);
    const cls = level ? "lv-" + (level[1] === "FATAL" ? "error" : level[1].toLowerCase()) : "";
    return el("span", { class: cls }, line + "\n");
}

function refreshLog() {
    return once("log", async () => {
        let data;
        try {
            data = await api("GET", "/api/logs" + (logCursor ? `?cursor=${encodeURIComponent(logCursor)}` : ""));
        } catch (err) {
            return;
        }
        const box = $("log");
        logCursor = data.cursor;
        if (!data.found) {
            box.textContent = "Todavía no existe Server.log. Aparecerá cuando el servidor arranque.";
            return;
        }
        if (data.reset) box.replaceChildren();
        if (!data.lines.length) return;
        box.append(...data.lines.map(logLine));
        while (box.childElementCount > LOG_MAX_LINES) box.firstElementChild.remove();
        if ($("log-follow").checked) box.scrollTop = box.scrollHeight;
    });
}
onShow.registro = refreshLog;

// ---------------------------------------------------------------------
// Cuenta
// ---------------------------------------------------------------------
$("account-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const data = Object.fromEntries(new FormData(form));
    if (data.new_password !== data.repeat_password) { toast("Las contraseñas nuevas no coinciden.", true); return; }
    try {
        const result = await api("POST", "/api/account", data);
        setText("whoami", result.username);
        for (const input of form.querySelectorAll('input[type="password"]')) input.value = "";
        toast("Cuenta guardada");
        refreshStatus();
        if (IS_ADMIN) loadUsers();
    } catch (err) {
        toast(err.message, true);
    }
});

const ROLE_TEXT = { admin: "Administrador", moderator: "Moderador" };

async function loadUsers() {
    const data = await act(() => api("GET", "/api/users"));
    if (!data) return;
    $("users").replaceChildren(...data.users.map((user) => {
        const mine = user.username === data.me;
        const patch = (body, done) => act(() => api("PATCH", `/api/users/${encodeURIComponent(user.username)}`, body), done).then(loadUsers);
        const role = el("select", { "aria-label": `Rol de ${user.username}`, onchange: (event) => patch({ role: event.target.value }, "Rol cambiado") },
            ...Object.entries(ROLE_TEXT).map(([id, label]) => el("option", { value: id, selected: id === user.role }, label)));
        return el("li", { class: "row" },
            el("span", { class: "row-main" }, user.username, mine ? el("span", { class: "tag" }, "Tú") : null, mine ? el("small", {}, ROLE_TEXT[user.role]) : null),
            mine ? null : el("span", { class: "row-actions" },
                role,
                smallButton("Contraseña", async () => {
                    const password = await askDialog({ title: `Nueva contraseña para ${user.username}`, text: "Sus sesiones abiertas se cerrarán.", ok: "Cambiar", input: { label: "Contraseña nueva (mínimo 8 caracteres)" } });
                    if (password !== null) patch({ password }, "Contraseña cambiada");
                }),
                smallButton("Borrar", async () => {
                    if (await confirmDialog(`¿Borrar a ${user.username}?`, "Dejará de poder entrar al panel.", "Borrar usuario")) {
                        await act(() => api("DELETE", `/api/users/${encodeURIComponent(user.username)}`), "Usuario borrado");
                        loadUsers();
                    }
                }, "btn-danger")));
    }));
}

if (IS_ADMIN) {
    $("user-form").addEventListener("submit", async (event) => {
        event.preventDefault();
        if (await act(() => api("POST", "/api/users", Object.fromEntries(new FormData(event.target))), "Usuario creado")) event.target.reset();
        loadUsers();
    });
    onShow.cuenta = loadUsers;
}

// ---------------------------------------------------------------------
// Arranque y refresco periódico (en pausa si la pestaña no está visible)
// ---------------------------------------------------------------------
function refresh() {
    if (document.hidden) return;
    refreshStatus();
    if (currentView === "registro") refreshLog();
    else if (currentView === "mapas") loadMaps();
}

showView();
refreshStatus();
setInterval(refresh, POLL_MS);
document.addEventListener("visibilitychange", refresh);
