// Coordinate Tools plugin for Map Viewer - built against the MapViewer
// plugin API (see the "Plugin API" section in the map-viewer Web Page's own
// script). Pure coordinate utilities - click-to-coordinate, coordinate-to-
// zoom, and Decimal Degrees / DMS / UTM conversions - no Spatial Feature
// records are created or touched by this plugin.
(function(){
"use strict";

// ─────────────────────────────  DD <-> DMS  ─────────────────────────────
function ddToDms(deg, isLat){
  const hemi = isLat ? (deg >= 0 ? "N" : "S") : (deg >= 0 ? "E" : "W");
  const abs = Math.abs(deg);
  let d = Math.floor(abs);
  let mFull = (abs - d) * 60;
  let m = Math.floor(mFull);
  let s = (mFull - m) * 60;
  s = Math.round(s * 10) / 10; // carry rounding: 59.98" -> 60.0" bumps m/d
  if(s >= 60){ s -= 60; m += 1; }
  if(m >= 60){ m -= 60; d += 1; }
  return {d, m, s, hemi};
}

function dmsToDd(d, m, s, hemi){
  let dd = d + m/60 + s/3600;
  if(hemi === "S" || hemi === "W") dd = -dd;
  return dd;
}

function fmtDmsPart(deg, isLat){
  const {d, m, s, hemi} = ddToDms(deg, isLat);
  return `${d}°${m}'${s.toFixed(1)}"${hemi}`;
}

function fmtDms(lat, lon){
  return `${fmtDmsPart(lat, true)} ${fmtDmsPart(lon, false)}`;
}

// ─────────────────────────────  WGS84 lat/lon <-> UTM  ─────────────────────────────
// Standard Snyder transverse Mercator forward/inverse series, WGS84 ellipsoid.
// Verified numerically against pyproj/PROJ (Nairobi, Mombasa, Eldoret,
// London, Tokyo all match to within centimeters) and against round-trip
// DD -> UTM -> DD (sub-micrometer residuals) - see project notes.
const WGS84_A = 6378137.0;
const WGS84_F = 1 / 298.257223563;
const UTM_K0 = 0.9996;
const UTM_FALSE_EASTING = 500000.0;
const UTM_FALSE_NORTHING = 10000000.0; // applied for southern hemisphere only

function utmZone(lon){
  return Math.floor((lon + 180) / 6) + 1;
}
function utmHemisphere(lat){
  return lat >= 0 ? "N" : "S";
}

function latLonToUtm(lat, lon){
  const a = WGS84_A, f = WGS84_F;
  const e2 = f * (2 - f);
  const ep2 = e2 / (1 - e2);

  const zone = utmZone(lon);
  const lon0 = (zone - 1) * 6 - 180 + 3; // central meridian, degrees
  const latRad = lat * Math.PI / 180;
  const lonRad = lon * Math.PI / 180;
  const lon0Rad = lon0 * Math.PI / 180;

  const N = a / Math.sqrt(1 - e2 * Math.sin(latRad) ** 2);
  const T = Math.tan(latRad) ** 2;
  const C = ep2 * Math.cos(latRad) ** 2;
  const A = Math.cos(latRad) * (lonRad - lon0Rad);

  const M = a * (
    (1 - e2/4 - 3*e2**2/64 - 5*e2**3/256) * latRad
    - (3*e2/8 + 3*e2**2/32 + 45*e2**3/1024) * Math.sin(2*latRad)
    + (15*e2**2/256 + 45*e2**3/1024) * Math.sin(4*latRad)
    - (35*e2**3/3072) * Math.sin(6*latRad)
  );

  const easting = UTM_K0 * N * (
    A + (1-T+C)*A**3/6 + (5-18*T+T**2+72*C-58*ep2)*A**5/120
  ) + UTM_FALSE_EASTING;

  let northing = UTM_K0 * (
    M + N*Math.tan(latRad) * (
      A**2/2 + (5-T+9*C+4*C**2)*A**4/24 + (61-58*T+T**2+600*C-330*ep2)*A**6/720
    )
  );
  const hemisphere = utmHemisphere(lat);
  if(hemisphere === "S") northing += UTM_FALSE_NORTHING;

  return {zone, hemisphere, easting, northing};
}

function utmToLatLon(zone, hemisphere, easting, northing){
  const a = WGS84_A, f = WGS84_F;
  const e2 = f * (2 - f);
  const ep2 = e2 / (1 - e2);
  const e1 = (1 - Math.sqrt(1-e2)) / (1 + Math.sqrt(1-e2));

  const x = easting - UTM_FALSE_EASTING;
  let y = northing;
  if(hemisphere === "S") y -= UTM_FALSE_NORTHING;

  const M = y / UTM_K0;
  const mu = M / (a * (1 - e2/4 - 3*e2**2/64 - 5*e2**3/256));

  const phi1 = mu
    + (3*e1/2 - 27*e1**3/32) * Math.sin(2*mu)
    + (21*e1**2/16 - 55*e1**4/32) * Math.sin(4*mu)
    + (151*e1**3/96) * Math.sin(6*mu)
    + (1097*e1**4/512) * Math.sin(8*mu);

  const N1 = a / Math.sqrt(1 - e2*Math.sin(phi1)**2);
  const T1 = Math.tan(phi1)**2;
  const C1 = ep2 * Math.cos(phi1)**2;
  const R1 = a * (1-e2) / Math.pow(1 - e2*Math.sin(phi1)**2, 1.5);
  const D = x / (N1 * UTM_K0);

  const lat = phi1 - (N1*Math.tan(phi1)/R1) * (
    D**2/2 - (5+3*T1+10*C1-4*C1**2-9*ep2)*D**4/24
    + (61+90*T1+298*C1+45*T1**2-252*ep2-3*C1**2)*D**6/720
  );

  const lon0 = (zone - 1) * 6 - 180 + 3;
  const lon = lon0 * Math.PI/180 + (
    D - (1+2*T1+C1)*D**3/6
    + (5-2*C1+28*T1-3*C1**2+8*ep2+24*T1**2)*D**5/120
  ) / Math.cos(phi1);

  return {lat: lat * 180/Math.PI, lon: lon * 180/Math.PI};
}

function fmtUtm(lat, lon){
  const u = latLonToUtm(lat, lon);
  return `${u.zone}${u.hemisphere}  ${Math.round(u.easting).toLocaleString()}E  ${Math.round(u.northing).toLocaleString()}N`;
}

// ─────────────────────────────  Formatting dispatcher  ─────────────────────────────
function formatCoord(lat, lon, format){
  if(format === "dms") return fmtDms(lat, lon);
  if(format === "utm") return fmtUtm(lat, lon);
  return `${lat.toFixed(6)}, ${lon.toFixed(6)}`; // dd
}

// ─────────────────────────────  Parsing free-typed coordinate input  ─────────────────────────────
// Accepts "lat, lon" or "lat lon" decimal degrees, and a best-effort DMS
// form like `1°17'27.6"S, 36°49'15.6"E` (also tolerates plain quotes ' " in
// place of the curly ones, and N/S/E/W in any position/case).
function parseDmsToken(token){
  const re = /(-?\d+(?:\.\d+)?)[°\s]+(\d+(?:\.\d+)?)['′\s]+(\d+(?:\.\d+)?)["″]?\s*([NSEWnsew]?)/;
  const m = token.trim().match(re);
  if(!m) return null;
  const d = parseFloat(m[1]);
  const min = parseFloat(m[2]);
  const sec = parseFloat(m[3]);
  const hemi = (m[4] || "").toUpperCase();
  let dd = Math.abs(d) + min/60 + sec/3600;
  if(hemi === "S" || hemi === "W" || d < 0) dd = -dd;
  return dd;
}

function splitCoordInput(text){
  // Prefer a comma split first (keeps DMS's own spaces intact); fall back
  // to splitting on runs of whitespace that sit between a hemisphere
  // letter and the next digit, for space-only DMS pairs.
  let parts = text.split(",");
  if(parts.length !== 2){
    const m = text.trim().match(/^(.*[NSns])\s+(.*[EWew])$/);
    if(m) parts = [m[1], m[2]];
  }
  if(parts.length !== 2){
    // Last resort: plain "lat lon" decimal degrees separated by whitespace.
    const bits = text.trim().split(/\s+/);
    if(bits.length === 2) parts = bits;
  }
  return parts.map(p=>p.trim()).filter(Boolean);
}

function parseCoordInput(text){
  const parts = splitCoordInput(text);
  if(parts.length !== 2) return {error: "Enter two values: latitude and longitude (comma or space separated)."};
  let [latToken, lonToken] = parts;
  let lat, lon;
  if(/[°'"′″]/.test(latToken) || /[NSEWnsew]/.test(latToken)){
    lat = parseDmsToken(latToken);
    lon = parseDmsToken(lonToken);
    if(lat == null || lon == null) return {error: "Could not parse DMS input. Expected e.g. 1°17'27.6\"S, 36°49'15.6\"E"};
  } else {
    lat = parseFloat(latToken);
    lon = parseFloat(lonToken);
    if(isNaN(lat) || isNaN(lon)) return {error: "Could not parse coordinates. Expected decimal degrees like -1.2921, 36.8219."};
  }
  if(lat < -90 || lat > 90) return {error: "Latitude must be between -90 and 90."};
  if(lon < -180 || lon > 180) return {error: "Longitude must be between -180 and 180."};
  return {lat, lon};
}

// ─────────────────────────────  Clipboard  ─────────────────────────────
async function copyToClipboard(text, api){
  try{
    await navigator.clipboard.writeText(text);
    api.toast("Copied to clipboard.");
  }catch(e){
    // Some embedded/browser contexts restrict the Clipboard API - fall back
    // to a temporary offscreen input the user can copy from manually.
    try{
      const input = document.createElement("input");
      input.value = text;
      input.style.position = "fixed";
      input.style.left = "-9999px";
      document.body.appendChild(input);
      input.focus();
      input.select();
      document.execCommand("copy");
      document.body.removeChild(input);
      api.toast("Copied to clipboard.");
    }catch(e2){
      api.toast("Could not copy automatically - value: " + text, true);
    }
  }
}

// ─────────────────────────────  Map interaction state  ─────────────────────────────
let pickActive = false;
let pickedLatLng = null;
let goMarker = null;
let goMarkerTimer = null;
let lastMouseRender = 0;
const MOUSEMOVE_THROTTLE_MS = 100;

function clearGoMarker(api){
  if(goMarkerTimer){ clearTimeout(goMarkerTimer); goMarkerTimer = null; }
  if(goMarker){ api.getMap().removeLayer(goMarker); goMarker = null; }
}

function dropGoMarker(api, lat, lon){
  clearGoMarker(api);
  goMarker = L.marker([lat, lon]).addTo(api.getMap());
  goMarkerTimer = setTimeout(()=>{ clearGoMarker(api); }, 5000);
}

// ─────────────────────────────  Panel  ─────────────────────────────
function renderPanel(container, api){
  container.innerHTML = `
    <div class="proc-card">
      <h4><i class="fa-solid fa-crosshairs" style="color:var(--blue)"></i>Format</h4>
      <div class="field">
        <label>Coordinate format</label>
        <select id="ctFormat">
          <option value="dd">Decimal Degrees</option>
          <option value="dms">DMS</option>
          <option value="utm">UTM</option>
        </select>
      </div>
    </div>

    <div class="proc-card">
      <h4><i class="fa-solid fa-hand-pointer" style="color:var(--green)"></i>Pick on map</h4>
      <label class="check-row"><input type="checkbox" id="ctPickToggle">Pick on map (click the map to read a point)</label>
      <div id="ctPickResult" class="hint">Turn this on, then click anywhere on the map.</div>
      <button class="btn btn-sm" id="ctPickCopy" style="margin-top:6px;display:none"><i class="fa-solid fa-copy"></i>Copy</button>
    </div>

    <div class="proc-card">
      <h4><i class="fa-solid fa-magnifying-glass-location" style="color:var(--blue)"></i>Go to coordinate</h4>
      <div class="field">
        <label>Latitude, Longitude</label>
        <input type="text" id="ctGoInput" placeholder="-1.2921, 36.8219  or  1°17'31.6\"S, 36°49'18.8\"E">
      </div>
      <button class="btn btn-sm btn-primary" id="ctGoBtn" style="width:100%;justify-content:center"><i class="fa-solid fa-location-arrow"></i>Go</button>
      <div id="ctGoError" class="hint" style="color:#b3261e;margin-top:6px;display:none"></div>
    </div>

    <div class="proc-card">
      <h4><i class="fa-solid fa-arrows-up-down-left-right" style="color:var(--ink-mute)"></i>Cursor position</h4>
      <div id="ctCursorReadout" class="hint">Move the mouse over the map.</div>
    </div>
  `;

  const formatSel = container.querySelector("#ctFormat");
  const pickToggle = container.querySelector("#ctPickToggle");
  const pickResult = container.querySelector("#ctPickResult");
  const pickCopyBtn = container.querySelector("#ctPickCopy");
  const goInput = container.querySelector("#ctGoInput");
  const goBtn = container.querySelector("#ctGoBtn");
  const goError = container.querySelector("#ctGoError");
  const cursorReadout = container.querySelector("#ctCursorReadout");

  function currentFormat(){ return formatSel.value; }

  function renderPickedPoint(){
    if(!pickedLatLng){
      pickResult.textContent = "Turn this on, then click anywhere on the map.";
      pickCopyBtn.style.display = "none";
      return;
    }
    pickResult.innerHTML = `<div class="kv-row"><span class="kv-l">Picked point</span><span class="kv-v">${api.escHtml(formatCoord(pickedLatLng.lat, pickedLatLng.lng, currentFormat()))}</span></div>`;
    pickCopyBtn.style.display = "inline-flex";
  }

  formatSel.addEventListener("change", ()=>{
    renderPickedPoint();
    // Cursor readout re-renders on the next mousemove tick on its own.
  });

  pickToggle.addEventListener("change", ()=>{
    pickActive = pickToggle.checked;
    if(!pickActive){
      pickedLatLng = null;
      renderPickedPoint();
    }
  });

  pickCopyBtn.addEventListener("click", ()=>{
    if(!pickedLatLng) return;
    copyToClipboard(formatCoord(pickedLatLng.lat, pickedLatLng.lng, currentFormat()), api);
  });

  goBtn.addEventListener("click", ()=>{
    const text = goInput.value.trim();
    if(!text){ goError.style.display = "none"; return; }
    const parsed = parseCoordInput(text);
    if(parsed.error){
      goError.textContent = parsed.error;
      goError.style.display = "block";
      return;
    }
    goError.style.display = "none";
    api.getMap().flyTo([parsed.lat, parsed.lon], 16);
    dropGoMarker(api, parsed.lat, parsed.lon);
    api.toast("Moved to " + formatCoord(parsed.lat, parsed.lon, currentFormat()));
  });

  goInput.addEventListener("keydown", (e)=>{
    if(e.key === "Enter") goBtn.click();
  });

  const map = api.getMap();

  function onMapClick(e){
    if(!pickActive) return;
    pickedLatLng = e.latlng;
    renderPickedPoint();
  }
  map.on("click", onMapClick);

  function onMouseMove(e){
    const now = Date.now();
    if(now - lastMouseRender < MOUSEMOVE_THROTTLE_MS) return;
    lastMouseRender = now;
    cursorReadout.innerHTML = `<div class="kv-row"><span class="kv-l">Lat/Lon</span><span class="kv-v">${api.escHtml(formatCoord(e.latlng.lat, e.latlng.lng, currentFormat()))}</span></div>`;
  }
  map.on("mousemove", onMouseMove);
}

// ─────────────────────────────  Registration  ─────────────────────────────
window.MapViewer.registerPlugin({
  id: "coordinate-tools",
  onActivate(api){
    api.addToolbarButton({
      id: "btnCoordinateTools",
      icon: "fa-location-crosshairs",
      label: "Coordinates",
      title: "Coordinate Tools - click-to-coordinate, go-to-coordinate, DD/DMS/UTM",
      onClick: ()=> api.openDock("coords"),
    });
    api.addDockTab({
      tab: "coords",
      label: "Coordinates",
      render: (container)=> renderPanel(container, api),
    });
  },
});
})();
