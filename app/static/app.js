const HOURS = [1, 3, 6, 12, 24, 48, 72];

const map = L.map("map", { zoomControl: true }).setView([39.8, -98.6], 4);

function esriBasemap(service) {
  return L.esri.tiledMapLayer({
    url: `https://services.arcgisonline.com/ArcGIS/rest/services/${service}/MapServer`,
    maxZoom: 19,
  });
}

const imagery = esriBasemap("World_Imagery");
const basemaps = {
  Imagery: imagery,
  Topographic: esriBasemap("World_Topo_Map"),
  Streets: esriBasemap("World_Street_Map"),
  Terrain: esriBasemap("World_Terrain_Base"),
  "USA Topo": esriBasemap("USA_Topo_Maps"),
  "Dark gray": esriBasemap("Canvas/World_Dark_Gray_Base"),
};
const placeNames = esriBasemap("Reference/World_Boundaries_and_Places");
imagery.addTo(map);
placeNames.addTo(map);

const BasemapControl = L.Control.extend({
  options: { position: "topright" },
  onAdd() {
    const wrap = L.DomUtil.create("div", "basemap-control");
    const select = L.DomUtil.create("select", "", wrap);
    select.setAttribute("aria-label", "Basemap");
    Object.keys(basemaps).forEach((name) => {
      const option = L.DomUtil.create("option", "", select);
      option.value = name;
      option.textContent = name;
    });
    select.value = "Imagery";
    const label = L.DomUtil.create("label", "", wrap);
    const box = L.DomUtil.create("input", "", label);
    box.type = "checkbox";
    box.checked = true;
    label.append(" Place names");
    L.DomEvent.disableClickPropagation(wrap);
    L.DomEvent.disableScrollPropagation(wrap);
    select.addEventListener("change", () => {
      Object.values(basemaps).forEach((layer) => {
        if (map.hasLayer(layer)) map.removeLayer(layer);
      });
      const next = basemaps[select.value];
      next.addTo(map);
      next.bringToBack();
    });
    box.addEventListener("change", () => {
      if (box.checked) placeNames.addTo(map);
      else map.removeLayer(placeNames);
    });
    return wrap;
  },
});
new BasemapControl().addTo(map);

let bufferLayer = L.geoJSON(null, {
  style: { color: "#38bdf8", weight: 1.5, fillOpacity: 0.04, dashArray: "4 4" },
}).addTo(map);
let perimeterLayer = L.layerGroup().addTo(map);
let stationLayer = L.layerGroup().addTo(map);

let snapshot = null;
let formReady = false;
let selectedId = null;
let lastRevision = -1;
let lastIds = "";
let observed = { id: null, buffer: null, data: null, pending: false, error: "" };
let seriesOpen = false;
let awaitingRefresh = false;
let sawRefreshing = false;
let refreshWaitStarted = 0;
let pollTimer = null;

map.createPane("radar");
map.getPane("radar").style.zIndex = 350;
map.getPane("radar").style.pointerEvents = "none";

const radar = { frames: [], index: 0, timer: null, layer: null, token: 0 };

const firesBody = document.getElementById("fires");
const hourBoxes = document.getElementById("hourBoxes");
hourBoxes.innerHTML = HOURS.map((hour) => (
  `<label><input type="checkbox" name="accum" value="${hour}" /> ${hour}h</label>`
)).join("");

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

function inches(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toFixed(2);
}

function acreText(value) {
  if (value === null || value === undefined) return "—";
  return Math.round(value).toLocaleString("en-US");
}

function rankHour() {
  return String(document.getElementById("rankHours").value || "");
}

function threshold() {
  const value = Number(document.getElementById("highlightInches").value);
  return Number.isFinite(value) ? value : 0.5;
}

function highest(fire) {
  const key = rankHour();
  const values = [fire.mrms_in?.[key], fire.station_in?.[key], fire.forecast_in?.[key]]
    .filter((value) => value !== null && value !== undefined);
  return values.length ? Math.max(...values) : null;
}

function isHot(fire) {
  const value = highest(fire);
  return value !== null && value >= threshold();
}

function fillHourSelect(hours, selected) {
  const select = document.getElementById("rankHours");
  const current = selected || select.value;
  select.innerHTML = hours.map((hour) => `<option value="${hour}">${hour}h max</option>`).join("");
  if (hours.map(String).includes(String(current))) select.value = String(current);
}

function fillForm(settings) {
  document.getElementById("states").value = (settings.states || []).join(", ");
  document.getElementById("minAcres").value = settings.min_acres;
  document.getElementById("bufferMiles").value = settings.buffer_miles;
  document.getElementById("forecastSource").value = settings.forecast_source || "nbm";
  document.getElementById("forecastHours").value = String(settings.forecast_hours);
  document.getElementById("refreshMinutes").value = String(settings.refresh_minutes);
  document.getElementById("wildfireOnly").checked = Boolean(settings.wildfire_only);
  document.getElementById("highlightInches").value = settings.highlight_inches;
  const selected = new Set((settings.accum_hours || []).map(String));
  document.querySelectorAll('input[name="accum"]').forEach((box) => {
    box.checked = selected.has(box.value);
  });
  fillHourSelect(settings.accum_hours || [6], settings.highlight_hours);
  formReady = true;
}

function readForm() {
  const accum = [...document.querySelectorAll('input[name="accum"]:checked')].map((box) => Number(box.value));
  return {
    states: document.getElementById("states").value,
    min_acres: Number(document.getElementById("minAcres").value),
    buffer_miles: Number(document.getElementById("bufferMiles").value),
    accum_hours: accum,
    forecast_source: document.getElementById("forecastSource").value,
    forecast_hours: Number(document.getElementById("forecastHours").value),
    refresh_minutes: Number(document.getElementById("refreshMinutes").value),
    wildfire_only: document.getElementById("wildfireOnly").checked,
    highlight_inches: Number(document.getElementById("highlightInches").value),
    highlight_hours: Number(document.getElementById("rankHours").value),
  };
}

function isBusy() {
  if (!snapshot) return true;
  if (awaitingRefresh || snapshot.refreshing) return true;
  return (snapshot.status || "").includes("while a new one starts");
}

function busyMessage() {
  if (awaitingRefresh && !snapshot?.refreshing && !sawRefreshing) return "Starting a new download…";
  return snapshot?.status || "Loading the latest perimeters and precipitation…";
}

function renderStatus() {
  const status = document.getElementById("status");
  const busy = isBusy();
  const count = snapshot ? (snapshot.fire_count ?? (snapshot.fires || []).length) : 0;
  const when = snapshot?.updated_at ? ` · ${snapshot.updated_at}` : "";
  if (!snapshot) status.textContent = "Loading the latest perimeters and precipitation…";
  else if (busy) status.textContent = busyMessage();
  else status.textContent = `${snapshot.status || "Waiting"}${when} · ${count} fires`;
  document.getElementById("statusBlock").classList.toggle("busy", busy);
  document.getElementById("mapBusy").hidden = !busy;
  document.getElementById("mapBusyText").textContent = status.textContent;
  document.getElementById("apply").disabled = busy;
  document.getElementById("refresh").disabled = busy;
  const errors = document.getElementById("errors");
  errors.innerHTML = ((snapshot && snapshot.errors) || []).map((error) => `<li>${esc(error)}</li>`).join("");
  document.getElementById("forecastNote").textContent = (snapshot && snapshot.forecast_note) || "";
}

function settleRefresh() {
  if (snapshot?.refreshing) sawRefreshing = true;
  const finished = awaitingRefresh && sawRefreshing && !snapshot.refreshing;
  const gaveUp = awaitingRefresh && !sawRefreshing && refreshWaitStarted
    && Date.now() - refreshWaitStarted > 20000 && !snapshot.refreshing;
  if (finished || gaveUp) {
    awaitingRefresh = false;
    sawRefreshing = false;
    if (gaveUp) snapshot.status = "The refresh did not start. Try again.";
  }
}

function beginRefreshWait() {
  awaitingRefresh = true;
  sawRefreshing = Boolean(snapshot?.refreshing);
  refreshWaitStarted = Date.now();
  renderStatus();
  schedulePoll(800);
}

function sortedFires() {
  return [...(snapshot.fires || [])].sort((a, b) => {
    const left = highest(a);
    const right = highest(b);
    if (left === null && right === null) return (b.acres || 0) - (a.acres || 0);
    if (left === null) return 1;
    if (right === null) return -1;
    return right - left || (b.acres || 0) - (a.acres || 0);
  });
}

function forecastLabel() {
  return snapshot.settings?.forecast_source === "nws" ? "NWS" : "NBM";
}

function renderTable() {
  const key = rankHour();
  document.getElementById("forecastHead").textContent = forecastLabel();
  const rows = sortedFires().map((fire) => {
    const classes = [fire.id === selectedId ? "selected" : "", isHot(fire) ? "hot" : ""].filter(Boolean).join(" ");
    const place = [fire.state, fire.county].filter(Boolean).join(" · ");
    return `<tr data-id="${esc(fire.id)}" class="${classes}">
      <td><button type="button" class="name-btn" data-id="${esc(fire.id)}"><span class="name">${esc(fire.name)}</span><span class="sub">${esc(place)}</span></button></td>
      <td>${acreText(fire.acres)}</td>
      <td>${inches(fire.mrms_in?.[key])}</td>
      <td>${inches(fire.station_in?.[key])}</td>
      <td>${inches(fire.forecast_in?.[key])}</td>
    </tr>`;
  }).join("");
  const empty = snapshot.refreshing
    ? "Perimeters are downloading."
    : "No current perimeters match these filters.";
  firesBody.innerHTML = rows || `<tr><td colspan="5">${empty}</td></tr>`;
}

function renderDetail() {
  const panel = document.getElementById("detail");
  const fire = (snapshot.fires || []).find((item) => item.id === selectedId);
  if (!fire) {
    panel.innerHTML = `<p class="muted">Select a fire to see every interval and the stations inside its buffer.</p>`;
    hideSeries();
    return;
  }
  const hours = snapshot.settings?.accum_hours || [];
  const body = hours.map((hour) => {
    const key = String(hour);
    return `<tr>
      <td>${hour}h</td>
      <td>${inches(fire.mrms_in?.[key])}</td>
      <td>${inches(fire.station_in?.[key])}</td>
      <td>${inches(fire.forecast_in?.[key])}</td>
    </tr>`;
  }).join("");
  const stations = [...(fire.stations || [])]
    .sort((a, b) => (b.precip?.[rankHour()] || 0) - (a.precip?.[rankHour()] || 0))
    .slice(0, 8)
    .map((station) => `<li>${esc(station.stid)} ${esc(station.name)} · ${inches(station.precip?.[rankHour()])} in</li>`)
    .join("");
  const contained = fire.contained_pct === null || fire.contained_pct === undefined
    ? "containment not reported"
    : `${Math.round(fire.contained_pct)}% contained`;
  panel.innerHTML = `
    <h2>${esc(fire.name)}</h2>
    <p class="muted">${esc(fire.state || "")} ${esc(fire.county || "")} · ${acreText(fire.acres)} acres · ${esc(contained)}
      ${fire.discovered ? ` · discovered ${esc(fire.discovered)}` : ""}
      · ${snapshot.settings?.forecast_source === "nws"
        ? `${fire.forecast_cells || 0} forecast cells`
        : "NBM CONUS grid"}
      · ${fire.station_count || 0} stations</p>
    <table>
      <thead><tr><th>Interval</th><th>MRMS</th><th>Station</th><th>Forecast</th></tr></thead>
      <tbody>${body}</tbody>
    </table>
    ${stations ? `<ul class="stations">${stations}</ul>` : `<p class="muted">No Synoptic stations inside this buffer reported precipitation for the request.</p>`}
  `;
  if (seriesOpen) showSeries(fire);
}

function hideSeries() {
  seriesOpen = false;
  document.getElementById("seriesPopup").hidden = true;
}

function showSeries(fire) {
  seriesOpen = true;
  const popup = document.getElementById("seriesPopup");
  popup.hidden = false;
  const place = [fire.state, fire.county].filter(Boolean).join(" · ");
  document.getElementById("seriesTitle").textContent = place ? `${fire.name} · ${place}` : fire.name;
  drawChart(fire);
  loadObserved(fire);
}

function qpfPoints(fire) {
  const series = fire.qpf_series || [];
  const hourly = series.filter((point) => point.hours === 1);
  const chosen = hourly.length ? hourly : series.filter((point) => point.hours === 6);
  const hours = chosen[0]?.hours || 1;
  return {
    label: `${forecastLabel()} ${hours}h QPF`,
    color: "#fb923c",
    points: chosen,
  };
}

function parseValid(value) {
  const direct = Date.parse(value);
  if (Number.isFinite(direct)) return direct;
  const match = String(value ?? "").match(/(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  if (!match) return NaN;
  return Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]), Number(match[4]), Number(match[5]));
}

function hourlyMax(points) {
  const byHour = new Map();
  points.forEach((point) => {
    const time = parseValid(point.valid);
    if (!Number.isFinite(time) || point.inches === null || point.inches === undefined) return;
    const hour = Math.floor(time / 3600000) * 3600000;
    const inches = Number(point.inches);
    const current = byHour.get(hour);
    if (current === undefined || inches > current) byHour.set(hour, inches);
  });
  return [...byHour.entries()]
    .sort((left, right) => left[0] - right[0])
    .map(([time, inches]) => ({ valid: new Date(time).toISOString(), inches }));
}

function hourLabel(time) {
  const date = new Date(time);
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getUTCMonth() + 1}/${date.getUTCDate()} ${pad(date.getUTCHours())}Z`;
}

function drawChart(fire) {
  const host = document.getElementById("seriesChart");
  const note = document.getElementById("seriesNote");
  if (!host || !note) return;
  const waiting = observed.pending && observed.id === fire.id;
  document.getElementById("seriesBusy").hidden = !waiting;
  const observedMatch = observed.id === fire.id && observed.buffer === snapshot.settings?.buffer_miles;
  const mrms = observedMatch && observed.data ? observed.data.mrms || [] : [];
  const stationRows = observedMatch && observed.data ? observed.data.stations || [] : [];
  const station = hourlyMax(observedMatch && observed.data ? observed.data.station || [] : []);
  const now = Date.now();
  const qpf = qpfPoints(fire);
  const groups = [
    { label: qpf.label, color: "#fb923c", points: qpf.points, loading: false },
    { label: "MRMS 1h QPE", color: "#38bdf8", points: mrms, loading: waiting },
    ...stationRows.map((row) => ({
      label: row.stid || row.name || "Station",
      detail: row.name && row.name !== row.stid ? row.name : "",
      color: "#86efac",
      points: row.points || [],
      thin: true,
      legend: false,
    })),
    {
      label: stationRows.length ? `Station max (${stationRows.length})` : "Station max",
      color: "#4ade80",
      points: station,
      loading: waiting,
    },
  ];
  const lookback = 24 * 60 * 60 * 1000;
  const pastStart = now - lookback;
  const forecastTimes = qpf.points.map((point) => parseValid(point.valid)).filter((value) => Number.isFinite(value));
  const minT = forecastTimes.length ? Math.min(pastStart, ...forecastTimes) : pastStart;
  const maxT = Math.max(now, ...(forecastTimes.length ? forecastTimes : [now]));
  const forecastAhead = forecastTimes.some((time) => time > now + 30 * 60 * 1000);
  const amounts = groups.flatMap((group) => group.points.map((point) => point.inches)).filter((value) => value !== null && value !== undefined);
  const maxY = Math.max(0.1, ...amounts.map(Number));
  const width = 560;
  const height = 180;
  const pad = { left: 36, right: 12, top: 18, bottom: 28 };
  const span = Math.max(maxT - minT, 1);
  const xOf = (time) => pad.left + ((time - minT) / span) * (width - pad.left - pad.right);
  const yOf = (value) => pad.top + (1 - value / maxY) * (height - pad.top - pad.bottom);
  const plotBottom = height - pad.bottom;
  const xNow = xOf(Math.min(Math.max(now, minT), maxT));
  const hits = [];
  const marks = groups.map((group) => {
    let path = "";
    let drawing = false;
    const dots = [];
    group.points.forEach((point) => {
      const time = parseValid(point.valid);
      if (!Number.isFinite(time) || time < minT - 60 * 60 * 1000 || time > maxT + 60 * 60 * 1000) {
        drawing = false;
        return;
      }
      if (point.inches === null || point.inches === undefined) {
        drawing = false;
        return;
      }
      const x = xOf(time).toFixed(1);
      const y = yOf(Number(point.inches)).toFixed(1);
      path += `${drawing ? "L" : "M"}${x},${y}`;
      drawing = true;
      hits.push({
        label: group.label,
        detail: group.detail || "",
        color: group.color,
        time,
        inches: Number(point.inches),
        x: Number(x),
        y: Number(y),
      });
      if (!group.thin) dots.push(`<circle cx="${x}" cy="${y}" r="1.8" fill="${group.color}" />`);
    });
    const stroke = group.thin ? 1 : 1.8;
    const opacity = group.thin ? 0.45 : 1;
    return `<path d="${path}" fill="none" stroke="${group.color}" stroke-width="${stroke}" stroke-opacity="${opacity}" />${dots.join("")}`;
  }).join("");
  const legend = groups.filter((group) => group.legend !== false).map((group) => {
    const suffix = group.points.length ? "" : (group.loading ? " · loading" : " · none");
    return `<span><i style="background:${group.color}"></i>${esc(group.label + suffix)}</span>`;
  }).join("");
  host.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Past 24 hours and forecast precipitation">
      <rect x="${pad.left}" y="${pad.top}" width="${Math.max(xNow - pad.left, 0)}" height="${plotBottom - pad.top}" fill="#1c3a4a" />
      <line x1="${pad.left}" y1="${yOf(0)}" x2="${width - pad.right}" y2="${yOf(0)}" stroke="#44403c" />
      <line x1="${xNow}" y1="${pad.top}" x2="${xNow}" y2="${plotBottom}" stroke="#a8a29e" stroke-dasharray="3 3" />
      <text x="4" y="${yOf(maxY) + 4}" fill="#a8a29e" font-size="11">${maxY.toFixed(2)}</text>
      <text x="4" y="${yOf(0)}" fill="#a8a29e" font-size="11">0</text>
      <text x="${pad.left}" y="${pad.top - 5}" fill="#7dd3fc" font-size="11">${minT < pastStart - 60 * 60 * 1000 ? "Past" : "Past 24 h"}</text>
      ${forecastAhead ? `<text x="${Math.min(xNow + 6, width - 78)}" y="${pad.top - 5}" fill="#fdba74" font-size="11">Forecast</text>` : ""}
      <text x="${pad.left}" y="${height - 8}" fill="#a8a29e" font-size="11">${hourLabel(minT)}</text>
      <text x="${xNow}" y="${height - 8}" fill="#a8a29e" font-size="11" text-anchor="${forecastAhead ? "middle" : "end"}">Now</text>
      ${forecastAhead ? `<text x="${width - pad.right}" y="${height - 8}" fill="#a8a29e" font-size="11" text-anchor="end">${hourLabel(maxT)}</text>` : ""}
      ${marks}
      <line class="hover-line" y1="${pad.top}" y2="${plotBottom}" stroke="#e7e5e4" stroke-dasharray="2 2" visibility="hidden" pointer-events="none" />
      <circle class="hover-dot" r="3.6" visibility="hidden" pointer-events="none" />
    </svg>
    <div class="series-tip" hidden></div>
    <div class="legend-row">${legend}</div>
  `;
  bindChartHover(host, hits, { width, height });
  const problems = observedMatch ? (observed.data?.errors || []) : [];
  note.textContent = waiting
    ? "Loading the past 24 hours of MRMS and station precipitation. The orange line is the NBM forecast."
    : `The shaded side is before now. MRMS is the radar-only 1-hour total for the past 24 hours. Each faint green line is one station inside the buffer, and the bright green line is the highest station each hour. The orange line is NBM at the valid time of each hour. Grid values are the maximum in the buffer, in inches.${problems.length ? " " + problems.join(" ") : ""}`;
}

function bindChartHover(host, hits, plot) {
  const svg = host.querySelector("svg");
  const tip = host.querySelector(".series-tip");
  const guide = svg.querySelector(".hover-line");
  const dot = svg.querySelector(".hover-dot");
  const hide = () => {
    tip.hidden = true;
    guide.setAttribute("visibility", "hidden");
    dot.setAttribute("visibility", "hidden");
  };
  svg.addEventListener("mousemove", (event) => {
    const rect = svg.getBoundingClientRect();
    if (!rect.width || !rect.height || !hits.length) {
      hide();
      return;
    }
    const x = ((event.clientX - rect.left) / rect.width) * plot.width;
    const y = ((event.clientY - rect.top) / rect.height) * plot.height;
    let best = null;
    let bestDistance = 22;
    hits.forEach((hit) => {
      const distance = Math.hypot(hit.x - x, hit.y - y);
      if (distance < bestDistance) {
        best = hit;
        bestDistance = distance;
      }
    });
    if (!best) {
      hide();
      return;
    }
    guide.setAttribute("x1", String(best.x));
    guide.setAttribute("x2", String(best.x));
    guide.setAttribute("visibility", "visible");
    dot.setAttribute("cx", String(best.x));
    dot.setAttribute("cy", String(best.y));
    dot.setAttribute("fill", best.color);
    dot.setAttribute("visibility", "visible");
    tip.replaceChildren();
    const name = document.createElement("strong");
    name.textContent = best.label;
    name.style.color = best.color;
    tip.append(name);
    if (best.detail) {
      const detail = document.createElement("span");
      detail.textContent = best.detail;
      tip.append(detail);
    }
    const value = document.createElement("span");
    value.textContent = `${hourLabel(best.time)} · ${inches(best.inches)} in`;
    tip.append(value);
    tip.hidden = false;
    const hostRect = host.getBoundingClientRect();
    const tipWidth = Math.min(240, hostRect.width - 8);
    let left = event.clientX - hostRect.left + 12;
    let top = event.clientY - hostRect.top - tip.offsetHeight - 8;
    if (left + tipWidth > hostRect.width) left = Math.max(4, event.clientX - hostRect.left - tipWidth - 8);
    if (top < 0) top = event.clientY - hostRect.top + 14;
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
  });
  svg.addEventListener("mouseleave", hide);
}

async function loadObserved(fire) {
  const buffer = snapshot.settings?.buffer_miles;
  if (observed.id === fire.id && observed.buffer === buffer && (observed.data || observed.pending)) return;
  observed = { id: fire.id, buffer, data: null, pending: true, error: "" };
  drawChart(fire);
  try {
    const response = await fetch(`/api/series?fire_id=${encodeURIComponent(fire.id)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "The time series request failed.");
    if (observed.id !== fire.id) return;
    observed = { id: fire.id, buffer, data, pending: false, error: "" };
  } catch (error) {
    if (observed.id !== fire.id) return;
    observed = { id: fire.id, buffer, data: { mrms: [], station: [], errors: [error.message] }, pending: false, error: error.message };
  }
  if (selectedId === fire.id) drawChart(fire);
}

function featureCollection(fires, key) {
  return {
    type: "FeatureCollection",
    features: fires.filter((fire) => fire[key]).map((fire) => ({
      type: "Feature",
      properties: { id: fire.id, name: fire.name, hot: isHot(fire) },
      geometry: fire[key],
    })),
  };
}

function renderMap() {
  const fires = snapshot.fires || [];
  bufferLayer.clearLayers();
  bufferLayer.addData(featureCollection(fires, "buffer"));
  perimeterLayer.clearLayers();
  stationLayer.clearLayers();
  const perimeters = featureCollection(fires, "perimeter");
  L.geoJSON(perimeters, {
    style: (feature) => ({
      color: feature.properties.hot ? "#ef4444" : "#c2410c",
      weight: feature.properties.id === selectedId ? 3 : 1.4,
      fillColor: feature.properties.hot ? "#ef4444" : "#f97316",
      fillOpacity: 0.38,
    }),
    onEachFeature: (feature, layer) => {
      layer.on("click", () => selectFire(feature.properties.id, false));
    },
  }).addTo(perimeterLayer);

  const selected = fires.find((fire) => fire.id === selectedId);
  (selected?.stations || []).forEach((station) => {
    const amount = station.precip?.[rankHour()];
    L.circleMarker([station.lat, station.lon], {
      radius: 5,
      color: "#e0f2fe",
      weight: 1,
      fillColor: "#0284c7",
      fillOpacity: 0.9,
    }).bindTooltip(`${station.stid}: ${inches(amount)} in`).addTo(stationLayer);
  });

  const ids = fires.map((fire) => fire.id).join("|");
  if (ids !== lastIds) {
    lastIds = ids;
    if (fires.length) {
      const bounds = bufferLayer.getBounds();
      if (bounds.isValid()) map.fitBounds(bounds.pad(0.2));
    }
  }
}

function selectFire(id, zoom) {
  selectedId = id;
  seriesOpen = true;
  renderTable();
  renderDetail();
  renderMap();
  if (!zoom) return;
  const fire = (snapshot.fires || []).find((item) => item.id === id);
  if (!fire?.buffer) return;
  const layer = L.geoJSON(fire.buffer);
  const bounds = layer.getBounds();
  if (bounds.isValid()) map.fitBounds(bounds.pad(0.3));
}

firesBody.addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-id]");
  if (row) selectFire(row.dataset.id, true);
});

["rankHours", "highlightInches"].forEach((id) => {
  document.getElementById(id).addEventListener("input", () => {
    if (snapshot) {
      renderTable();
      renderDetail();
      renderMap();
    }
  });
});

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: payload ? JSON.stringify(payload) : "{}",
  });
  const body = await response.json().catch(() => ({}));
  const error = document.getElementById("formError");
  if (!response.ok) {
    error.hidden = false;
    error.textContent = body.detail || "The settings were not accepted.";
    return false;
  }
  error.hidden = true;
  return true;
}

document.getElementById("filters").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (isBusy()) return;
  beginRefreshWait();
  const payload = readForm();
  fillHourSelect(payload.accum_hours, payload.highlight_hours);
  const ok = await postJson("/api/settings", payload);
  if (!ok) {
    awaitingRefresh = false;
    renderStatus();
  }
});

document.getElementById("refresh").addEventListener("click", async () => {
  if (isBusy()) return;
  beginRefreshWait();
  const ok = await postJson("/api/refresh");
  if (!ok) {
    awaitingRefresh = false;
    renderStatus();
  }
});

document.getElementById("seriesClose").addEventListener("click", hideSeries);

function radarStamp(iso) {
  const date = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getUTCFullYear()}${pad(date.getUTCMonth() + 1)}${pad(date.getUTCDate())}${pad(date.getUTCHours())}${pad(date.getUTCMinutes())}`;
}

function radarUrl(iso) {
  return `https://mesonet.agron.iastate.edu/c/tile.py/1.0.0/ridge::USCOMP-N0Q-${radarStamp(iso)}/{z}/{x}/{y}.png`;
}

function formatRadarTime(iso) {
  const date = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())} UTC`;
}

function stopRadarLoop() {
  if (radar.timer) {
    clearInterval(radar.timer);
    radar.timer = null;
  }
  document.getElementById("radarPlay").textContent = "Play";
}

function showRadarFrame(index) {
  if (!radar.frames.length) return;
  radar.index = (index + radar.frames.length) % radar.frames.length;
  const stamp = radar.frames[radar.index];
  const url = radarUrl(stamp);
  if (!radar.layer) {
    radar.layer = L.tileLayer(url, {
      pane: "radar",
      opacity: 0.62,
      maxZoom: 12,
      attribution: "NEXRAD composite © Iowa Environmental Mesonet",
    });
  } else {
    radar.layer.setUrl(url);
  }
  if (!map.hasLayer(radar.layer)) radar.layer.addTo(map);
  const slider = document.getElementById("radarSlider");
  slider.max = String(radar.frames.length - 1);
  slider.value = String(radar.index);
  document.getElementById("radarTime").textContent = formatRadarTime(stamp);
}

async function enableRadar() {
  const token = ++radar.token;
  const time = document.getElementById("radarTime");
  time.textContent = "Loading…";
  const end = new Date();
  const start = new Date(end.getTime() - 2 * 60 * 60 * 1000);
  const iso = (date) => `${date.toISOString().slice(0, 16)}Z`;
  try {
    const response = await fetch(
      `https://mesonet.agron.iastate.edu/json/radar.py?operation=list&radar=USCOMP&product=N0Q&start=${iso(start)}&end=${iso(end)}`
    );
    const data = await response.json();
    if (token !== radar.token) return;
    radar.frames = (data.scans || []).map((scan) => scan.ts).slice(-24);
    if (!radar.frames.length) throw new Error("empty");
    document.getElementById("radarPlay").disabled = false;
    document.getElementById("radarSlider").disabled = false;
    showRadarFrame(radar.frames.length - 1);
  } catch (error) {
    if (token !== radar.token) return;
    time.textContent = "Unavailable";
    document.getElementById("radarPlay").disabled = true;
    document.getElementById("radarSlider").disabled = true;
  }
}

function disableRadar() {
  radar.token += 1;
  stopRadarLoop();
  if (radar.layer && map.hasLayer(radar.layer)) map.removeLayer(radar.layer);
  document.getElementById("radarPlay").disabled = true;
  document.getElementById("radarSlider").disabled = true;
  document.getElementById("radarTime").textContent = "Off";
}

document.getElementById("radarToggle").addEventListener("change", (event) => {
  if (event.target.checked) enableRadar();
  else disableRadar();
});

document.getElementById("radarPlay").addEventListener("click", () => {
  if (!radar.frames.length) return;
  if (radar.timer) {
    stopRadarLoop();
    return;
  }
  document.getElementById("radarPlay").textContent = "Pause";
  radar.timer = setInterval(() => showRadarFrame(radar.index + 1), 500);
});

document.getElementById("radarSlider").addEventListener("input", (event) => {
  showRadarFrame(Number(event.target.value));
});

function schedulePoll(delay) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(poll, delay);
}

async function poll() {
  try {
    const response = await fetch("/api/snapshot");
    const data = await response.json();
    const first = !formReady;
    snapshot = data;
    settleRefresh();
    if (first && data.settings) fillForm(data.settings);
    renderStatus();
    if (data.revision !== lastRevision) {
      lastRevision = data.revision;
      renderTable();
      renderDetail();
      renderMap();
    }
  } catch (error) {
    document.getElementById("status").textContent = "The local app is not responding.";
    document.getElementById("mapBusyText").textContent = "The local app is not responding.";
    document.getElementById("statusBlock").classList.add("busy");
    document.getElementById("mapBusy").hidden = false;
  }
  schedulePoll(isBusy() ? 2000 : 15000);
}

poll();
