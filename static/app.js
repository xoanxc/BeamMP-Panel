/* Panel BeamMP — lógica del navegador. JavaScript sin librerías. */
"use strict";

const $ = (id) => document.getElementById(id);
const CSRF = document.querySelector('meta[name="csrf-token"]').content;
const MAX_UPLOAD = Number(document.querySelector('meta[name="max-upload"]').content);

// ---------------------------------------------------------------------
// Utilidades
// ---------------------------------------------------------------------
async function api(method, url, body) {
    const options = { method, headers: { "X-CSRF-Token": CSRF }, credentials: "same-origin" };
    if (body !== undefined) {
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

function toast(message, isError = false) {
    const node = el("div", { class: "toast" + (isError ? " is-error" : ""), role: isError ? "alert" : "status" }, message);
    $("toasts").append(node);
    setTimeout(() => node.remove(), isError ? 8000 : 4000);
}

function confirmDialog(title, text, okLabel) {
    const dialog = $("confirm");
    $("confirm-title").textContent = title;
    $("confirm-text").textContent = text;
    $("confirm-ok").textContent = okLabel;
    return new Promise((resolve) => {
        dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
        dialog.returnValue = "cancel";
        dialog.showModal();
    });
}

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

// ---------------------------------------------------------------------
// Navegación entre secciones
// ---------------------------------------------------------------------
const VIEWS = ["resumen", "mapas", "mods", "ajustes", "registro", "cuenta"];
const onShow = {};
let currentView = null;

function showView() {
    const wanted = window.location.hash.slice(1);
    currentView = VIEWS.includes(wanted) ? wanted : "resumen";
    for (const name of VIEWS) $("view-" + name).hidden = name !== currentView;
    for (const link of document.querySelectorAll(".nav a")) {
        if (link.dataset.view === currentView) link.setAttribute("aria-current", "page");
        else link.removeAttribute("aria-current");
    }
    if (onShow[currentView]) onShow[currentView]();
}
window.addEventListener("hashchange", showView);

// ---------------------------------------------------------------------
// Resumen: estado del servidor
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

async function refreshStatus() {
    let status;
    try {
        status = await api("GET", "/api/status");
    } catch (err) {
        $("state").dataset.state = "unknown";
        $("state-text").textContent = err.message;
        return;
    }
    lastStatus = status;
    const state = status.service.state;
    $("server-name").textContent = status.server_name || "Servidor BeamMP";
    $("state").dataset.state = state;
    $("state-text").textContent = STATE_TEXT[state] || state;
    $("uptime").textContent = formatUptime(status.service.uptime);

    const running = state === "online" || state === "starting";
    $("btn-start").hidden = running;
    $("btn-restart").hidden = !running;
    $("btn-stop").hidden = !running;

    const { count, max, names } = status.players;
    $("gauge-number").textContent = state === "online" ? `${count}/${max}` : "–";
    const fill = state === "online" && max > 0 ? Math.min(100, (count / max) * 100) : 0;
    $("gauge-value").setAttribute("stroke-dasharray", `${fill} 100`);

    $("current-map").textContent = status.map.name;
    $("current-map-path").textContent = status.map.name === status.map.path ? "" : status.map.path;

    $("players").replaceChildren(...names.map((name) => el("li", {}, name)));
    $("players-empty").hidden = names.length > 0;
    $("players-empty").textContent = state === "online" ? "Nadie conectado ahora mismo." : "El servidor no está en marcha.";

    $("default-credentials").hidden = !status.default_credentials;
    $("config-missing").hidden = status.config_found;
}

const ACTION_TEXT = {
    start: { done: "Arrancando el servidor" },
    restart: { done: "Reiniciando el servidor", title: "¿Reiniciar el servidor?", ok: "Reiniciar" },
    stop: { done: "Deteniendo el servidor", title: "¿Detener el servidor?", ok: "Detener" },
};

async function serverAction(action) {
    const text = ACTION_TEXT[action];
    if (text.title) {
        const players = lastStatus ? lastStatus.players.count : 0;
        const detail = players > 0 ? `Se desconectará a ${players} jugador${players === 1 ? "" : "es"}.` : "Ahora mismo no hay nadie conectado.";
        if (!(await confirmDialog(text.title, detail, text.ok))) return;
    }
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

async function loadMaps() {
    try {
        mapData = await api("GET", "/api/maps");
        renderMaps();
    } catch (err) {
        toast(err.message, true);
    }
}

function renderMaps() {
    if (!mapData) return;
    const query = $("map-filter").value.trim().toLowerCase();
    const matches = (map) => !query || map.name.toLowerCase().includes(query) || map.path.toLowerCase().includes(query);
    const mods = mapData.mods.filter(matches);
    const official = mapData.official.filter(matches);

    $("mod-maps").replaceChildren(...mods.map(modMapCard));
    $("mod-maps-empty").hidden = mapData.mods.length > 0;
    $("mod-map-count").textContent = mapData.mods.length ? `(${mods.length})` : "";

    $("official-maps").replaceChildren(...official.map((map) => {
        const current = map.path === mapData.current;
        return el("button", { class: "chip" + (current ? " is-current" : ""), type: "button", disabled: current, onclick: () => selectMap(map) },
            map.name, current ? el("span", { class: "badge" }, "En uso") : null);
    }));
    $("official-map-count").textContent = `(${official.length})`;
}

function modMapCard(map) {
    const current = map.path === mapData.current;
    const thumb = map.has_preview
        ? el("img", { class: "map-thumb", loading: "lazy", alt: "", src: `/api/maps/preview?mod=${encodeURIComponent(map.mod)}&folder=${encodeURIComponent(map.folder)}` })
        : el("span", { class: "map-thumb" });
    const meta = map.detected
        ? el("span", { class: "map-meta" }, map.mod)
        : el("span", { class: "map-meta warn" }, "No se encuentra su .zip en la carpeta de mods");
    return el("button", { class: "map-card" + (current ? " is-current" : ""), type: "button", disabled: current, onclick: () => selectMap(map) },
        thumb,
        el("span", { class: "map-body" },
            el("span", { class: "map-title" }, map.name, current ? el("span", { class: "badge" }, "En uso") : null),
            meta));
}

async function selectMap(map) {
    const players = lastStatus ? lastStatus.players.count : 0;
    const detail = "El servidor se reiniciará con este mapa." + (players > 0 ? ` Se desconectará a ${players} jugador${players === 1 ? "" : "es"}.` : "");
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

$("map-filter").addEventListener("input", renderMaps);
onShow.mapas = loadMaps;

// ---------------------------------------------------------------------
// Mods: listado, subida y borrado
// ---------------------------------------------------------------------
const KIND_TEXT = { map: "Mapa", vehicle: "Vehículo", other: "Otro", invalid: "Zip dañado" };

function renderMods(data) {
    $("mods-body").replaceChildren(...data.mods.map((mod) => {
        const kind = mod.kind === "map" ? `Mapa: ${mod.levels.join(", ")}` : KIND_TEXT[mod.kind];
        return el("tr", {},
            el("td", {}, mod.name),
            el("td", { class: "kind", dataset: { kind: mod.kind } }, kind),
            el("td", { class: "num" }, formatSize(mod.size)),
            el("td", {}, new Date(mod.modified * 1000).toLocaleDateString("es-ES")),
            el("td", {}, el("button", { class: "btn btn-small btn-danger", type: "button", onclick: () => deleteMod(mod.name) }, "Borrar")));
    }));
    $("mods-table").hidden = data.mods.length === 0;
    $("mods-empty").hidden = data.mods.length > 0;
    const total = data.mods.reduce((sum, mod) => sum + mod.size, 0);
    $("mods-foot").textContent = `${data.mods.length} mods, ${formatSize(total)} en ${data.folder}. Espacio libre: ${formatSize(data.disk.free)}.`;
}

async function loadMods() {
    try { renderMods(await api("GET", "/api/mods")); } catch (err) { toast(err.message, true); }
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

function uploadFile(file) {
    const status = el("span", {}, "En cola");
    const bar = el("progress", { max: "100", value: "0" });
    $("uploads").append(el("li", {}, el("span", {}, `${file.name} (${formatSize(file.size)})`), status, bar));

    const fail = (message) => { status.textContent = message; status.className = "is-error"; bar.remove(); };
    if (!file.name.toLowerCase().endsWith(".zip")) return Promise.resolve(fail("Solo se admiten .zip"));
    if (file.size > MAX_UPLOAD) return Promise.resolve(fail(`Supera el máximo de ${formatSize(MAX_UPLOAD)}`));

    const send = (overwrite) => new Promise((resolve) => {
        const xhr = new XMLHttpRequest();
        xhr.open("PUT", `/api/mods/${encodeURIComponent(file.name)}${overwrite ? "?overwrite=1" : ""}`);
        xhr.setRequestHeader("X-CSRF-Token", CSRF);
        xhr.setRequestHeader("Content-Type", "application/zip");
        xhr.responseType = "json";
        xhr.upload.onprogress = (event) => {
            if (!event.lengthComputable) return;
            const percent = Math.round((event.loaded / event.total) * 100);
            bar.value = percent;
            status.textContent = percent < 100 ? `${percent} %` : "Comprobando…";
        };
        xhr.onerror = () => resolve({ status: 0, body: {} });
        xhr.onload = () => resolve({ status: xhr.status, body: xhr.response || {} });
        xhr.send(file);
    });

    return (async () => {
        let result = await send(false);
        if (result.status === 409 && await confirmDialog(`${file.name} ya existe`, "¿Quieres sustituirlo por el archivo nuevo?", "Sustituir")) {
            result = await send(true);
        }
        if (result.status === 201) {
            status.textContent = "Subido";
            status.className = "is-done";
            bar.remove();
            $("restart-hint").hidden = false;
        } else if (result.status === 401) {
            window.location.href = "/login";
        } else {
            fail(result.body.error || (result.status ? `Error ${result.status}` : "Se ha cortado la conexión"));
        }
    })();
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
$("upload-limit").textContent = `Máximo ${formatSize(MAX_UPLOAD)} por archivo.`;
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
    Private: ["Servidor privado (no aparece en la lista pública)", "checkbox"],
    AllowGuests: ["Permitir invitados sin cuenta", "checkbox"],
    LogChat: ["Guardar el chat en el registro", "checkbox"],
    Debug: ["Registro detallado (debug)", "checkbox"],
};

async function loadConfig() {
    let values;
    try { values = (await api("GET", "/api/config")).values; } catch (err) { toast(err.message, true); return; }
    const keys = Object.keys(CONFIG_LABELS).filter((key) => key in values);
    $("config-fields").replaceChildren(...keys.map((key) => {
        const value = values[key];
        const [label, type, help] = CONFIG_LABELS[key];
        if (type === "checkbox") {
            return el("label", { class: "check" }, el("input", { type, name: key, checked: value === true }), label);
        }
        const input = type === "textarea"
            ? el("textarea", { name: key }, String(value))
            : el("input", { type, name: key, value: String(value), min: type === "number" ? "1" : null });
        return el("label", { class: "field" }, el("span", {}, label), input, help ? el("small", {}, help) : null);
    }));
}

$("config-form").addEventListener("submit", async (event) => {
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
onShow.ajustes = loadConfig;

// ---------------------------------------------------------------------
// Registro
// ---------------------------------------------------------------------
async function refreshLog() {
    let data;
    try { data = await api("GET", "/api/logs?lines=400"); } catch (err) { return; }
    const box = $("log");
    if (!data.found) { box.textContent = "Todavía no existe Server.log. Aparecerá cuando el servidor arranque."; return; }
    box.replaceChildren(...data.lines.map((line) => {
        const level = /\[(WARN|ERROR|FATAL|DEBUG)\]/.exec(line);
        const cls = level ? "lv-" + (level[1] === "FATAL" ? "error" : level[1].toLowerCase()) : "";
        return el("span", { class: cls }, line + "\n");
    }));
    if ($("log-follow").checked) box.scrollTop = box.scrollHeight;
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
        $("whoami").textContent = result.username;
        for (const input of form.querySelectorAll('input[type="password"]')) input.value = "";
        toast("Cuenta guardada");
        refreshStatus();
    } catch (err) {
        toast(err.message, true);
    }
});

// ---------------------------------------------------------------------
// Arranque y refresco periódico (en pausa si la pestaña no está visible)
// ---------------------------------------------------------------------
showView();
refreshStatus();
setInterval(() => {
    if (document.hidden) return;
    refreshStatus();
    if (currentView === "registro") refreshLog();
}, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshStatus(); });
