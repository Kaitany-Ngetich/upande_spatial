// Selection Engine plugin for Map Viewer - built against the MapViewer
// plugin API (see the "Plugin API" section in the map-viewer Web Page's
// own script). Adds spatial selection (click/box/polygon/radius) across
// whatever features are currently loaded, plus a handful of bulk
// operations (buffer/export/delete/merge/tag-as-layer) run on the current
// selection. Selection state lives entirely in this file's own module
// scope and is rendered into this plugin's own overlay layer - the core's
// internal `state` object and its rendered layers are never touched.
(function(){
"use strict";
const API_SPATIAL = "upande_spatial.api.spatial.";
const SOURCE_MODULE = "Map Viewer Selection Tools";

// ─────────────────────────────  Module state  ─────────────────────────────
let allFeatures = [];        // last-fetched FeatureCollection.features (all loaded features)
let selected = [];           // subset of allFeatures currently selected
let selectionLayer = null;   // our own L.geoJSON highlight overlay
let mode = null;             // 'click' | 'box' | 'polygon' | 'radius' | null
let mapClickBound = false;
let mapMouseDownBound = false;

// box-select drag state
let boxDrag = null;          // {startLatLng, rect(L.Rectangle)}
// polygon-select state
let polyPoints = [];         // array of [lon,lat]
let polyPreviewLayer = null;
// radius-select state
let radiusCenter = null;     // [lon,lat]
let radiusMarker = null;
let radiusPreviewCircle = null;

// ─────────────────────────────  Geometry helpers  ─────────────────────────────
function geomFamily(type){
  if(!type) return "other";
  if(type.indexOf("Multi") === 0) type = type.slice(5);
  if(type === "Point") return "point";
  if(type === "LineString") return "line";
  if(type === "Polygon") return "polygon";
  return "other";
}

function featureKey(f){ return f.properties && f.properties._spatial_feature_name; }

function nearestVertexDistanceMeters(clickTurfPt, coords, depth){
  // coords may be a flat list of positions (LineString/MultiPoint ring) or
  // nested one level deeper (Polygon ring list / MultiLineString) - walk
  // down until we hit actual [lon,lat] pairs.
  let min = Infinity;
  if(!coords || !coords.length) return min;
  if(typeof coords[0][0] === "number"){
    coords.forEach(c=>{
      const d = turf.distance(clickTurfPt, turf.point(c), {units:"meters"});
      if(d < min) min = d;
    });
  } else {
    coords.forEach(c=>{
      const d = nearestVertexDistanceMeters(clickTurfPt, c);
      if(d < min) min = d;
    });
  }
  return min;
}

// Rough click-to-feature distance in meters - exact for points, nearest
// vertex for lines/polygon rings, 0 if the click actually falls inside a
// polygon. Good enough for "which feature did the user mean to click",
// not meant to be pixel-perfect line/edge distance.
function distanceToFeatureMeters(clickLonLat, feature){
  const geom = feature.geometry;
  if(!geom) return Infinity;
  const clickPt = turf.point(clickLonLat);
  const family = geomFamily(geom.type);
  try{
    if(family === "point"){
      const coords = geom.type === "MultiPoint" ? geom.coordinates : [geom.coordinates];
      return nearestVertexDistanceMeters(clickPt, coords);
    }
    if(family === "polygon"){
      if(turf.booleanPointInPolygon(clickPt, feature)) return 0;
      // outside - fall back to nearest ring vertex as a distance proxy
      const rings = geom.type === "MultiPolygon" ? geom.coordinates : [geom.coordinates];
      return nearestVertexDistanceMeters(clickPt, rings);
    }
    // line
    const lines = geom.type === "MultiLineString" ? geom.coordinates : [geom.coordinates];
    return nearestVertexDistanceMeters(clickPt, lines);
  }catch(e){
    return Infinity;
  }
}

function featureIntersectsGeom(feature, otherGeoJson){
  try{
    if(geomFamily(feature.geometry.type) === "polygon" || geomFamily(feature.geometry.type) === "line"){
      return turf.booleanIntersects(feature, otherGeoJson);
    }
    // points: booleanIntersects works for Point vs Polygon in turf6 too,
    // but booleanPointInPolygon is the more literal/robust check.
    if(feature.geometry.type === "Point"){
      return turf.booleanPointInPolygon(feature, otherGeoJson);
    }
    if(feature.geometry.type === "MultiPoint"){
      return feature.geometry.coordinates.some(c=>turf.booleanPointInPolygon(turf.point(c), otherGeoJson));
    }
    return turf.booleanIntersects(feature, otherGeoJson);
  }catch(e){
    return false;
  }
}

function featureWithinRadius(feature, centerLonLat, radiusMeters){
  const geom = feature.geometry;
  if(!geom) return false;
  const family = geomFamily(geom.type);
  try{
    if(family === "point"){
      const coords = geom.type === "MultiPoint" ? geom.coordinates : [geom.coordinates];
      return coords.some(c=>turf.distance(turf.point(centerLonLat), turf.point(c), {units:"meters"}) <= radiusMeters);
    }
    // For lines/polygons: build the circle as a polygon and test intersection.
    const circle = turf.circle(centerLonLat, radiusMeters/1000, {units:"kilometers", steps:64});
    return featureIntersectsGeom(feature, circle);
  }catch(e){
    return false;
  }
}

// ─────────────────────────────  Data loading  ─────────────────────────────
async function reloadFeatures(api){
  try{
    const fc = await api.callMethod(API_SPATIAL+"get_features_geojson", {});
    allFeatures = (fc && fc.features) || [];
  }catch(e){
    api.toast("Could not load features: "+e.message, true);
    allFeatures = [];
  }
}

// ─────────────────────────────  Selection rendering  ─────────────────────────────
function clearSelectionLayer(api){
  const map = api.getMap();
  if(selectionLayer && map) map.removeLayer(selectionLayer);
  selectionLayer = null;
}

function drawSelectionLayer(api){
  clearSelectionLayer(api);
  if(!selected.length) return;
  const map = api.getMap();
  selectionLayer = L.geoJSON({type:"FeatureCollection", features: selected}, {
    pointToLayer: (feature, latlng)=> L.circleMarker(latlng, {
      radius: 9, color: "#e0402d", weight: 3, fillColor: "#ffce8a", fillOpacity: 0.9,
    }),
    style: ()=>({color:"#e0402d", weight:4, dashArray:"6,5", fillOpacity:0.15, fillColor:"#e0402d"}),
  }).addTo(map);
}

function setSelected(feats, api){
  // de-dupe by _spatial_feature_name, drop anything without one (shouldn't
  // happen for loaded features, but be defensive)
  const seen = new Set();
  selected = feats.filter(f=>{
    const k = featureKey(f);
    if(!k || seen.has(k)) return false;
    seen.add(k);
    return true;
  });
  drawSelectionLayer(api);
  refreshSelectionUI();
}

function addToSelection(feats, api){
  const byKey = new Map(selected.map(f=>[featureKey(f), f]));
  feats.forEach(f=>{ const k = featureKey(f); if(k) byKey.set(k, f); });
  setSelected(Array.from(byKey.values()), api);
}

// ─────────────────────────────  Map interaction per mode  ─────────────────────────────
function ensureMapHandlers(api){
  const map = api.getMap();
  if(!mapClickBound){
    mapClickBound = true;
    map.on("click", (e)=>onMapClick(e, api));
  }
  if(!mapMouseDownBound){
    mapMouseDownBound = true;
    map.on("mousedown", (e)=>onMapMouseDown(e, api));
  }
}

function onMapClick(e, api){
  if(mode === "click"){
    const clickLonLat = [e.latlng.lng, e.latlng.lat];
    let best = null, bestD = Infinity;
    allFeatures.forEach(f=>{
      const d = distanceToFeatureMeters(clickLonLat, f);
      if(d < bestD){ bestD = d; best = f; }
    });
    // Tolerance scales a little with zoom so it stays roughly "a few
    // pixels" at any zoom level, without needing pixel<->meter conversion.
    const map = api.getMap();
    const zoom = map.getZoom();
    const toleranceMeters = Math.max(15, 40000 / Math.pow(2, zoom));
    if(best && bestD <= toleranceMeters){
      addToSelection([best], api);
      api.toast((best.properties._title||best.properties._spatial_feature_name)+" selected.");
    } else {
      api.toast("No feature near that click.", true);
    }
  } else if(mode === "polygon"){
    polyPoints.push([e.latlng.lng, e.latlng.lat]);
    drawPolyPreview(api);
  } else if(mode === "radius"){
    if(!radiusCenter){
      radiusCenter = [e.latlng.lng, e.latlng.lat];
      if(radiusMarker) api.getMap().removeLayer(radiusMarker);
      radiusMarker = L.circleMarker(e.latlng, {radius:6, color:"#185FA5", weight:2, fillColor:"#185FA5", fillOpacity:0.9}).addTo(api.getMap());
      api.toast("Center set. Enter a radius and click Apply, or click again to re-pick the center.");
      const input = document.getElementById("selRadiusInput");
      if(input) input.focus();
    } else {
      // clicking again before Apply just re-picks the center
      radiusCenter = [e.latlng.lng, e.latlng.lat];
      radiusMarker.setLatLng(e.latlng);
    }
  }
}

function onMapMouseDown(e, api){
  if(mode !== "box") return;
  const map = api.getMap();
  map.dragging.disable();
  boxDrag = {start: e.latlng, rect: L.rectangle([e.latlng, e.latlng], {color:"#185FA5", weight:2, fillOpacity:0.08}).addTo(map)};
  const onMove = (ev)=>{
    if(!boxDrag) return;
    boxDrag.rect.setBounds(L.latLngBounds(boxDrag.start, ev.latlng));
  };
  const onUp = (ev)=>{
    map.off("mousemove", onMove);
    map.off("mouseup", onUp);
    map.dragging.enable();
    if(!boxDrag) return;
    const bounds = L.latLngBounds(boxDrag.start, ev.latlng);
    map.removeLayer(boxDrag.rect);
    boxDrag = null;
    const bbox = [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()];
    if(Math.abs(bbox[2]-bbox[0]) < 1e-9 || Math.abs(bbox[3]-bbox[1]) < 1e-9){
      api.toast("Drag to draw a selection box.", true);
      return;
    }
    const boxPoly = turf.bboxPolygon(bbox);
    const hits = allFeatures.filter(f=>featureIntersectsGeom(f, boxPoly));
    addToSelection(hits, api);
    api.toast(hits.length+" feature(s) selected in box.");
  };
  map.on("mousemove", onMove);
  map.on("mouseup", onUp);
}

function drawPolyPreview(api){
  const map = api.getMap();
  if(polyPreviewLayer) map.removeLayer(polyPreviewLayer);
  if(polyPoints.length < 1) return;
  const latlngs = polyPoints.map(([lon,lat])=>[lat,lon]);
  polyPreviewLayer = polyPoints.length < 3
    ? L.polyline(latlngs, {color:"#185FA5", weight:2, dashArray:"4,4"}).addTo(map)
    : L.polygon(latlngs, {color:"#185FA5", weight:2, dashArray:"4,4", fillOpacity:0.08}).addTo(map);
}

function finishPolygonSelect(api){
  if(polyPoints.length < 3){
    api.toast("Add at least 3 points before finishing.", true);
    return;
  }
  const ring = polyPoints.slice();
  ring.push(ring[0]); // close it
  let poly;
  try{ poly = turf.polygon([ring]); }
  catch(e){ api.toast("Invalid polygon: "+e.message, true); return; }
  const hits = allFeatures.filter(f=>featureIntersectsGeom(f, poly));
  addToSelection(hits, api);
  api.toast(hits.length+" feature(s) selected in polygon.");
  resetPolygonDrawing(api);
}

function resetPolygonDrawing(api){
  polyPoints = [];
  const map = api.getMap();
  if(polyPreviewLayer){ map.removeLayer(polyPreviewLayer); polyPreviewLayer = null; }
}

function applyRadiusSelect(api, radiusMeters){
  if(!radiusCenter){
    api.toast("Click the map to set a center point first.", true);
    return;
  }
  if(!(radiusMeters > 0)){
    api.toast("Enter a radius in meters greater than 0.", true);
    return;
  }
  const map = api.getMap();
  if(radiusPreviewCircle){ map.removeLayer(radiusPreviewCircle); radiusPreviewCircle = null; }
  radiusPreviewCircle = L.circle([radiusCenter[1], radiusCenter[0]], {radius: radiusMeters, color:"#185FA5", weight:2, fillOpacity:0.07}).addTo(map);
  const hits = allFeatures.filter(f=>featureWithinRadius(f, radiusCenter, radiusMeters));
  addToSelection(hits, api);
  api.toast(hits.length+" feature(s) selected within "+radiusMeters+"m.");
}

function resetRadiusDrawing(api){
  const map = api.getMap();
  if(radiusMarker){ map.removeLayer(radiusMarker); radiusMarker = null; }
  if(radiusPreviewCircle){ map.removeLayer(radiusPreviewCircle); radiusPreviewCircle = null; }
  radiusCenter = null;
}

function clearAllModeDrawings(api){
  resetPolygonDrawing(api);
  resetRadiusDrawing(api);
  if(boxDrag){
    api.getMap().removeLayer(boxDrag.rect);
    boxDrag = null;
    api.getMap().dragging.enable();
  }
}

function setMode(newMode, api){
  clearAllModeDrawings(api);
  mode = newMode;
  ensureMapHandlers(api);
  refreshModeUI();
}

// ─────────────────────────────  Bulk operations  ─────────────────────────────
async function bufferSelected(api, distanceMeters, refreshList){
  if(!selected.length) return;
  if(!(distanceMeters > 0)){ api.toast("Enter a buffer distance greater than 0.", true); return; }
  let ok = 0, fail = 0;
  for(const f of selected){
    try{
      const buffered = turf.buffer(f, distanceMeters/1000, {units:"kilometers"});
      if(!buffered) throw new Error("buffer produced no geometry");
      await api.callMethod(API_SPATIAL+"upsert_feature", {
        geometry: JSON.stringify(buffered.geometry),
        title: (f.properties._title||f.properties._spatial_feature_name)+" (buffer "+distanceMeters+"m)",
        source_module: SOURCE_MODULE,
      });
      ok++;
    }catch(e){ fail++; console.error("Buffer failed for", featureKey(f), e); }
  }
  api.toast(`Buffered ${ok} feature(s)`+(fail?`, ${fail} failed`:"")+".", fail>0 && ok===0);
  await api.refreshLayers();
  if(refreshList) refreshList();
}

function exportSelected(api){
  if(!selected.length) return;
  const fc = {type:"FeatureCollection", features: selected};
  const blob = new Blob([JSON.stringify(fc, null, 2)], {type:"application/geo+json"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "selection-"+new Date().toISOString().slice(0,19).replace(/[:T]/g,"-")+".geojson";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  api.toast("Exported "+selected.length+" feature(s).");
}

async function deleteSelected(api, refreshList){
  if(!selected.length) return;
  if(!confirm(`Delete ${selected.length} selected feature(s)? This cannot be undone.`)) return;
  let ok = 0, fail = 0;
  for(const f of selected){
    try{
      await api.callMethod(API_SPATIAL+"delete_feature", {name: featureKey(f)});
      ok++;
    }catch(e){ fail++; console.error("Delete failed for", featureKey(f), e); }
  }
  api.toast(`Deleted ${ok} feature(s)`+(fail?`, ${fail} failed`:"")+".", fail>0 && ok===0);
  setSelected([], api);
  await reloadFeatures(api);
  await api.refreshLayers();
  if(refreshList) refreshList();
}

function selectionFamilies(){
  return new Set(selected.map(f=>geomFamily(f.geometry && f.geometry.type)));
}

async function mergeSelected(api, refreshList){
  const families = selectionFamilies();
  if(selected.length < 2 || families.size !== 1 || families.has("other")){
    api.toast("Merge needs 2+ selected features of the same geometry family (all points, all lines, or all polygons).", true);
    return;
  }
  try{
    const combined = turf.combine(turf.featureCollection(selected));
    const mergedGeom = combined.features[0].geometry;
    await api.callMethod(API_SPATIAL+"upsert_feature", {
      geometry: JSON.stringify(mergedGeom),
      title: "Merged selection ("+selected.length+" features)",
      source_module: SOURCE_MODULE,
    });
    api.toast("Merged "+selected.length+" features into one.");
    setSelected([], api);
    await reloadFeatures(api);
    await api.refreshLayers();
    if(refreshList) refreshList();
  }catch(e){
    api.toast("Merge failed: "+e.message, true);
  }
}

async function createLayerFromSelection(api, label, refreshList){
  if(!label || !label.trim()) return;
  const lbl = label.trim();
  let ok = 0, fail = 0;
  for(const f of selected){
    try{
      const props = Object.assign({}, f.properties||{}, {custom_layer_label: lbl});
      // Strip both the underscore-prefixed read-only fields AND the derived
      // styling fields (color/marker_icon/line_width/fill_opacity)
      // get_features_geojson stamps on - those describe the SOURCE
      // feature/layer, not this new standalone copy. Mirrors api/spatial.py's
      // own _ROUNDTRIP_PROPERTY_KEYS set, which exists precisely because
      // these aren't underscore-prefixed and would otherwise leak through.
      const DERIVED_STYLE_KEYS = ["color", "marker_icon", "line_width", "fill_opacity"];
      Object.keys(props).forEach(k=>{ if(k.indexOf("_")===0 || DERIVED_STYLE_KEYS.includes(k)) delete props[k]; });
      await api.callMethod(API_SPATIAL+"upsert_feature", {
        geometry: JSON.stringify(f.geometry),
        title: (f.properties._title||f.properties._spatial_feature_name)+" ["+lbl+"]",
        properties: props,
        source_module: SOURCE_MODULE,
      });
      ok++;
    }catch(e){ fail++; console.error("Copy failed for", featureKey(f), e); }
  }
  api.toast(`Tagged/copied ${ok} feature(s) as "${lbl}"`+(fail?`, ${fail} failed`:"")+".", fail>0 && ok===0);
  await api.refreshLayers();
  if(refreshList) refreshList();
}

// ─────────────────────────────  Panel (dock tab)  ─────────────────────────────
let ui = {}; // cached DOM refs, populated by renderPanel

function refreshModeUI(){
  if(!ui.modeButtons) return;
  ui.modeButtons.forEach(btn=>btn.classList.toggle("btn-primary", btn.dataset.mode === mode));
  if(ui.radiusRow) ui.radiusRow.style.display = mode === "radius" ? "flex" : "none";
  if(ui.polyFinishBtn) ui.polyFinishBtn.style.display = mode === "polygon" ? "inline-flex" : "none";
  if(ui.modeHint){
    const hints = {
      click: "Click a feature on the map to select it.",
      box: "Click-drag a rectangle on the map to select everything inside it.",
      polygon: "Click to add vertices, then Finish to select everything inside.",
      radius: "Click the map to set a center point, enter a radius, then Apply.",
    };
    ui.modeHint.textContent = mode ? hints[mode] : "Choose a selection mode to begin.";
  }
}

function refreshSelectionUI(){
  if(!ui.countEl) return;
  ui.countEl.textContent = selected.length + " feature" + (selected.length===1?"":"s") + " selected";
  ui.listEl.innerHTML = selected.length
    ? selected.map(f=>`<div class="layer-row"><span class="layer-name" title="${window.MapViewer.escHtml(f.properties._title||f.properties._spatial_feature_name)}">${window.MapViewer.escHtml(f.properties._title||f.properties._spatial_feature_name)}</span></div>`).join("")
    : '<div class="empty-note">Nothing selected yet.</div>';
  const has = selected.length > 0;
  ui.opButtons.forEach(btn=>{ btn.disabled = !has; });
  const families = selectionFamilies();
  ui.mergeBtn.disabled = !(selected.length >= 2 && families.size === 1 && !families.has("other"));
}

function renderPanel(container, api){
  container.innerHTML = `
    <div class="proc-card">
      <h4><i class="fa-solid fa-object-ungroup" style="color:var(--blue)"></i>Selection mode</h4>
      <div class="field-row" style="flex-wrap:wrap">
        <button class="btn btn-sm" id="selModeClick" data-mode="click"><i class="fa-solid fa-arrow-pointer"></i>Click</button>
        <button class="btn btn-sm" id="selModeBox" data-mode="box"><i class="fa-regular fa-square"></i>Box</button>
        <button class="btn btn-sm" id="selModePolygon" data-mode="polygon"><i class="fa-solid fa-draw-polygon"></i>Polygon</button>
        <button class="btn btn-sm" id="selModeRadius" data-mode="radius"><i class="fa-solid fa-circle-dot"></i>Radius</button>
      </div>
      <div class="hint" id="selModeHint" style="margin-top:6px">Choose a selection mode to begin.</div>
      <div class="field-row" id="selPolyFinishRow" style="margin-top:6px">
        <button class="btn btn-sm" id="selPolyFinish" style="display:none"><i class="fa-solid fa-check"></i>Finish polygon</button>
      </div>
      <div class="field-row" id="selRadiusRow" style="display:none;margin-top:6px;align-items:flex-end">
        <div class="field" style="flex:1;margin-bottom:0"><label>Radius (m)</label><input type="number" id="selRadiusInput" value="100" min="1" step="any"></div>
        <button class="btn btn-sm" id="selRadiusApply" style="justify-content:center">Apply</button>
      </div>
      <div class="divider-line"></div>
      <div class="field-row">
        <button class="btn btn-sm" id="selAllBtn" style="flex:1;justify-content:center">Select all</button>
        <button class="btn btn-sm" id="selInvertBtn" style="flex:1;justify-content:center">Invert</button>
        <button class="btn btn-sm" id="selClearBtn" style="flex:1;justify-content:center">Clear</button>
      </div>
    </div>

    <div class="proc-card">
      <h4><i class="fa-solid fa-list-check" style="color:var(--green)"></i>Current selection</h4>
      <div class="hint" id="selCount">0 features selected</div>
      <div id="selList" style="margin-top:6px;max-height:160px;overflow-y:auto"></div>
    </div>

    <div class="proc-card">
      <h4><i class="fa-solid fa-gears" style="color:var(--ink)"></i>Run on selection</h4>
      <div class="field-row" style="align-items:flex-end">
        <div class="field" style="flex:1;margin-bottom:0"><label>Buffer distance (m)</label><input type="number" id="selBufferInput" value="10" min="0.01" step="any"></div>
        <button class="btn btn-sm sel-op" id="selBufferBtn" style="justify-content:center">Buffer selected</button>
      </div>
      <div class="field-row" style="margin-top:8px">
        <button class="btn btn-sm sel-op" id="selExportBtn" style="flex:1;justify-content:center"><i class="fa-solid fa-download"></i>Export .geojson</button>
        <button class="btn btn-sm sel-op" id="selDeleteBtn" style="flex:1;justify-content:center"><i class="fa-solid fa-trash"></i>Delete</button>
      </div>
      <div class="field-row" style="margin-top:8px">
        <button class="btn btn-sm sel-op" id="selMergeBtn" style="flex:1;justify-content:center"><i class="fa-solid fa-object-group"></i>Merge selected</button>
      </div>
      <div class="hint" style="margin-top:4px">Merge combines 2+ selected features of the same geometry family (all points/lines/polygons) into a single multi-part feature.</div>
      <div class="divider-line"></div>
      <div class="field-row" style="align-items:flex-end">
        <div class="field" style="flex:1;margin-bottom:0"><label>Layer label</label><input type="text" id="selLayerLabelInput" placeholder="e.g. Site visit 2026-09-15"></div>
        <button class="btn btn-sm sel-op" id="selLayerBtn" style="justify-content:center">Create layer</button>
      </div>
      <div class="hint" style="margin-top:4px">This does not create a real governed map layer (that requires Spatial Entity Config, which this tool can't touch) - it saves a tagged copy of each selected feature carrying a <code>custom_layer_label</code> property, so treat it as a lightweight grouping, not a new layer type.</div>
    </div>
  `;

  ui = {
    modeButtons: [
      container.querySelector("#selModeClick"),
      container.querySelector("#selModeBox"),
      container.querySelector("#selModePolygon"),
      container.querySelector("#selModeRadius"),
    ],
    modeHint: container.querySelector("#selModeHint"),
    polyFinishBtn: container.querySelector("#selPolyFinish"),
    radiusRow: container.querySelector("#selRadiusRow"),
    countEl: container.querySelector("#selCount"),
    listEl: container.querySelector("#selList"),
    mergeBtn: container.querySelector("#selMergeBtn"),
    opButtons: Array.from(container.querySelectorAll(".sel-op")),
  };

  ui.modeButtons.forEach(btn=>{
    btn.addEventListener("click", ()=>{
      const clicked = btn.dataset.mode;
      setMode(mode === clicked ? null : clicked, api);
    });
  });

  container.querySelector("#selPolyFinish").addEventListener("click", ()=> finishPolygonSelect(api));
  container.querySelector("#selRadiusApply").addEventListener("click", ()=>{
    const val = parseFloat(container.querySelector("#selRadiusInput").value);
    applyRadiusSelect(api, val);
  });

  container.querySelector("#selAllBtn").addEventListener("click", ()=>{
    setSelected(allFeatures.slice(), api);
    api.toast(allFeatures.length+" feature(s) selected.");
  });
  container.querySelector("#selInvertBtn").addEventListener("click", ()=>{
    const selKeys = new Set(selected.map(featureKey));
    setSelected(allFeatures.filter(f=>!selKeys.has(featureKey(f))), api);
  });
  container.querySelector("#selClearBtn").addEventListener("click", ()=>{
    setSelected([], api);
    clearAllModeDrawings(api);
  });

  container.querySelector("#selBufferBtn").addEventListener("click", ()=>{
    const dist = parseFloat(container.querySelector("#selBufferInput").value);
    bufferSelected(api, dist, ()=>refreshSelectionUI());
  });
  container.querySelector("#selExportBtn").addEventListener("click", ()=> exportSelected(api));
  container.querySelector("#selDeleteBtn").addEventListener("click", ()=> deleteSelected(api, ()=>refreshSelectionUI()));
  container.querySelector("#selMergeBtn").addEventListener("click", ()=> mergeSelected(api, ()=>refreshSelectionUI()));
  container.querySelector("#selLayerBtn").addEventListener("click", ()=>{
    const label = container.querySelector("#selLayerLabelInput").value;
    createLayerFromSelection(api, label, ()=>refreshSelectionUI());
  });

  refreshModeUI();
  refreshSelectionUI();
  reloadFeatures(api);
}

// ─────────────────────────────  Registration  ─────────────────────────────
window.MapViewer.registerPlugin({
  id: "selection",
  onActivate(api){
    api.addToolbarButton({
      id: "btnSelection",
      icon: "fa-object-ungroup",
      label: "Select",
      title: "Selection Engine - click/box/polygon/radius select, then buffer/export/delete/merge",
      onClick: ()=> api.openDock("selection"),
    });
    api.addDockTab({
      tab: "selection",
      label: "Selection",
      render: (container)=> renderPanel(container, api),
    });
  },
});
})();
