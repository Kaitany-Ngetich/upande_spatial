// Geometry Tools plugin for Map Viewer - built against the MapViewer plugin
// API (see the "Plugin API" section in the map-viewer Web Page's own
// script, and epanet-plugin.js for the reference plugin pattern).
//
// Two independent cards, both under one "Geometry" dock tab:
//   1. Difference & Symmetric Difference - extends the core's own
//      Clip/Union/Merge vector-op pattern with two ops the core doesn't
//      have. Same shared preview-layer/save convention as
//      gpClipPreview/gpUnionPreview/gpMergePreview: run an op, preview it
//      as a dashed overlay, then "Save as feature" writes it as a brand
//      new standalone Spatial Feature (never touches the sources).
//   2. Geometry Validation & Repair - checks every feature on a chosen
//      layer for self-intersection / unclosed rings / duplicate
//      consecutive vertices / empty geometry, and can repair each one
//      in place (upsert_feature by `name`, which IS safe to update-by-
//      name - see upsert_feature's own docstring in api/spatial.py).
//
// This plugin fetches its own data via api.callMethod() - it has no
// access to the core's internal `state` (masterFC/layers/map layers), by
// design (see the plugin-API contract).
(function(){
"use strict";
const API_SPATIAL = "upande_spatial.api.spatial.";
const SOURCE_MODULE = "Map Viewer Geometry Tools";

// ─────────────────────────────  Geometry helpers  ─────────────────────────────
function isPolygonal(type){ return type === "Polygon" || type === "MultiPolygon"; }

function coordsEqual(a, b){ return a && b && a[0] === b[0] && a[1] === b[1]; }

// All rings across a Polygon or MultiPolygon, as a flat array of ring
// coordinate arrays - a Polygon's own coordinates already are that; a
// MultiPolygon's coordinates are one level deeper (array of polygons,
// each an array of rings), so flatten one level.
function polygonRings(geometry){
  if(geometry.type === "Polygon") return geometry.coordinates || [];
  if(geometry.type === "MultiPolygon") return (geometry.coordinates || []).flat(1);
  return [];
}

// All line coordinate arrays for a LineString/MultiLineString.
function lineArrays(geometry){
  if(geometry.type === "LineString") return [geometry.coordinates || []];
  if(geometry.type === "MultiLineString") return geometry.coordinates || [];
  return [];
}

function hasUnclosedRing(geometry){
  return polygonRings(geometry).some(ring=>{
    if(!ring || ring.length < 2) return false; // empty/degenerate handled by the empty-geometry check
    return !coordsEqual(ring[0], ring[ring.length-1]);
  });
}

function hasDuplicateConsecutiveVertices(geometry){
  let arrays = [];
  if(isPolygonal(geometry.type)) arrays = polygonRings(geometry);
  else if(geometry.type === "LineString" || geometry.type === "MultiLineString") arrays = lineArrays(geometry);
  else if(geometry.type === "MultiPoint") arrays = [geometry.coordinates || []];
  else return false;
  return arrays.some(arr=>{
    for(let i=1;i<arr.length;i++){
      if(coordsEqual(arr[i], arr[i-1])) return true;
    }
    return false;
  });
}

function isEmptyGeometry(geometry){
  if(!geometry || !geometry.coordinates) return true;
  switch(geometry.type){
    case "Point": return (geometry.coordinates || []).length < 2;
    case "MultiPoint": return (geometry.coordinates || []).length === 0;
    case "LineString": return (geometry.coordinates || []).length < 2;
    case "MultiLineString": return (geometry.coordinates||[]).length === 0 || geometry.coordinates.every(l=>(l||[]).length < 2);
    case "Polygon": return (geometry.coordinates||[]).length === 0 || geometry.coordinates.every(r=>(r||[]).length < 4);
    case "MultiPolygon": return (geometry.coordinates||[]).length === 0 || geometry.coordinates.every(poly=>(poly||[]).every(r=>(r||[]).length < 4));
    default: return false;
  }
}

// turf.kinks applies to Polygon/MultiPolygon/LineString only (self
// intersections aren't a meaningful concept for points). turf.booleanValid
// doesn't exist in the turf v6 bundle this page loads - confirmed by
// `typeof turf.booleanValid === "undefined"` in the Node verification
// script, so this plugin doesn't reference it anywhere.
function hasSelfIntersection(feature){
  const t = feature.geometry && feature.geometry.type;
  if(t !== "Polygon" && t !== "MultiPolygon" && t !== "LineString") return false;
  try{ return turf.kinks(feature).features.length > 0; }
  catch(e){ return false; } // malformed enough that kinks() itself throws - empty/unclosed checks will already flag it
}

function runChecks(feature){
  const geom = feature.geometry;
  if(!geom) return ["Empty geometry"];
  const failed = [];
  if(isEmptyGeometry(geom)) failed.push("Empty geometry");
  if(hasSelfIntersection(feature)) failed.push("Self-intersection");
  if(isPolygonal(geom.type) && hasUnclosedRing(geom)) failed.push("Unclosed ring");
  if(hasDuplicateConsecutiveVertices(geom)) failed.push("Duplicate consecutive vertices");
  return failed;
}

// turf v6 has no @turf/symmetric-difference package (confirmed: both
// https://unpkg.com/@turf/symmetric-difference@6/ and its package.json
// 404). Composed here the standard way - (A-B) unioned with (B-A) -
// handling the two degenerate cases turf.difference itself uses null for:
// full overlap (both diffs null -> no symmetric difference at all) and
// one polygon wholly containing the other (one diff null -> the other
// diff IS the whole symmetric difference, nothing to union).
function symmetricDifference(a, b){
  const ab = turf.difference(a, b);
  const ba = turf.difference(b, a);
  if(!ab && !ba) return null;
  if(!ab) return ba;
  if(!ba) return ab;
  return turf.union(ab, ba);
}

// Repair pipeline, in this order: close unclosed rings first (cleanCoords
// and kinks both assume well-formed rings), then dedupe consecutive
// vertices via turf.cleanCoords (verified in Node to both remove the dupes
// and leave the ring closed), then - only for Polygon/MultiPolygon, where
// it's the well-known self-heal trick - buffer(0) to resolve self
// intersections, falling back to a microscopic epsilon buffer if buffer(0)
// alone doesn't clear it (verified in Node: buffer(0) alone already
// cleared a deliberate bowtie polygon, so the epsilon path is a
// documented-but-normally-unused fallback, not the primary fix).
// LineString self-intersections are flagged but not force-fixed here -
// buffering a line into "fixing" it would silently turn it into a
// Polygon, changing its geometry type, which is worse than leaving it
// flagged for a human to redraw.
function repairGeometry(feature){
  let geom = JSON.parse(JSON.stringify(feature.geometry));

  if(isPolygonal(geom.type)){
    polygonRings(geom).forEach(ring=>{
      if(ring.length && !coordsEqual(ring[0], ring[ring.length-1])){
        ring.push([ring[0][0], ring[0][1]]);
      }
    });
  }

  let f = {type:"Feature", properties:{}, geometry: geom};
  try{ f = turf.cleanCoords(f); }catch(e){ /* leave as closed-but-uncleaned rather than fail the whole repair */ }

  if(isPolygonal(f.geometry.type) && hasSelfIntersection(f)){
    let healed = null;
    try{ healed = turf.buffer(f, 0); }catch(e){ /* fall through to epsilon */ }
    if(!healed || hasSelfIntersection(healed)){
      try{ healed = turf.buffer(f, 0.0000001); }catch(e){ /* nothing more we can safely try */ }
    }
    if(healed) f = healed;
  }

  return f.geometry;
}

// ─────────────────────────────  Card 1: Difference / Symmetric Difference  ─────────────────────────────
let diffFeatureCache = []; // Polygon/MultiPolygon features only, keyed by _spatial_feature_name
let diffPreviewLayer = null;
let diffPreviewGeom = null;
let diffPreviewTitle = "";
let diffPreviewSources = [];

function findDiffFeature(name){
  return diffFeatureCache.find(f=>f.properties._spatial_feature_name === name);
}

async function loadDiffFeatures(api, selA, selB){
  const fc = await api.callMethod(API_SPATIAL+"get_features_geojson", {});
  diffFeatureCache = (fc.features||[]).filter(f=>f.geometry && isPolygonal(f.geometry.type));
  const opts = diffFeatureCache.map(f=>{
    const name = f.properties._spatial_feature_name;
    const label = (f.properties._title||name)+" ("+f.geometry.type+")";
    return `<option value="${api.escHtml(name)}">${api.escHtml(label)}</option>`;
  }).join("");
  const keepA = selA.value, keepB = selB.value;
  selA.innerHTML = opts || '<option value="">No polygon features available</option>';
  selB.innerHTML = opts || '<option value="">No polygon features available</option>';
  if(diffFeatureCache.some(f=>f.properties._spatial_feature_name===keepA)) selA.value = keepA;
  if(diffFeatureCache.some(f=>f.properties._spatial_feature_name===keepB)) selB.value = keepB;
}

function clearDiffPreview(api){
  if(diffPreviewLayer) api.getMap().removeLayer(diffPreviewLayer);
  diffPreviewLayer = null;
  diffPreviewGeom = null;
}

function showDiffPreview(api, geometry, color){
  clearDiffPreview(api);
  diffPreviewLayer = L.geoJSON(geometry, {
    style:{color, weight:2, dashArray:"6,4", fillOpacity:.15},
    pointToLayer:(f,latlng)=>L.circleMarker(latlng, {radius:7, color}),
  }).addTo(api.getMap());
  diffPreviewGeom = geometry;
}

function runDiffOp(api, resultEl, saveBtn, op){
  saveBtn.disabled = true;
  const a = findDiffFeature(document.getElementById("gtDiffA").value);
  const b = findDiffFeature(document.getElementById("gtDiffB").value);
  resultEl.innerHTML = "";
  if(!a || !b) { resultEl.innerHTML = '<div class="import-fail">Pick both Feature A and Feature B.</div>'; return; }
  if(a === b || (a.properties._spatial_feature_name === b.properties._spatial_feature_name)){
    resultEl.innerHTML = '<div class="import-fail">Feature A and Feature B must be different features.</div>';
    return;
  }
  const titleA = a.properties._title || a.properties._spatial_feature_name;
  const titleB = b.properties._title || b.properties._spatial_feature_name;
  try{
    let result, color, label;
    if(op === "diff"){
      result = turf.difference(a, b);
      if(!result) throw new Error("A is fully covered by B - nothing left of A.");
      color = "#b3261e";
      label = `${titleA} minus ${titleB}`;
    } else {
      result = symmetricDifference(a, b);
      if(!result) throw new Error("A and B are identical (or both empty) - no symmetric difference.");
      color = "#7B3FA0";
      label = `${titleA} △ ${titleB}`;
    }
    showDiffPreview(api, result.geometry, color);
    diffPreviewTitle = label;
    diffPreviewSources = [a.properties._spatial_feature_name, b.properties._spatial_feature_name];
    resultEl.innerHTML = `<div class="hint">Preview ready: <b>${api.escHtml(label)}</b>. Click "Save as feature" to keep it.</div>`;
    saveBtn.disabled = false;
  }catch(e){
    resultEl.innerHTML = `<div class="import-fail">${api.escHtml((op==="diff"?"Difference":"Symmetric difference")+" failed: "+e.message)}</div>`;
  }
}

async function saveDiffResult(api, resultEl, saveBtn){
  if(!diffPreviewGeom) return;
  saveBtn.disabled = true;
  try{
    await api.callMethod(API_SPATIAL+"upsert_feature", {
      geometry: JSON.stringify(diffPreviewGeom),
      title: diffPreviewTitle,
      source_module: SOURCE_MODULE,
      properties: JSON.stringify({derived_from: diffPreviewSources}),
    });
    api.toast(diffPreviewTitle+" saved as a new feature.");
    resultEl.innerHTML += '<div class="hint">Saved.</div>';
    clearDiffPreview(api);
    await api.refreshLayers();
    await loadDiffFeatures(api, document.getElementById("gtDiffA"), document.getElementById("gtDiffB"));
  }catch(e){
    api.toast("Save failed: "+e.message, true);
    saveBtn.disabled = false;
  }
}

// ─────────────────────────────  Card 2: Validation & Repair  ─────────────────────────────
let validateFeatureCache = []; // all features for the currently-checked layer, keyed by name

function findValidateFeature(name){
  return validateFeatureCache.find(f=>f.properties._spatial_feature_name === name);
}

async function loadValidateLayers(api, selEl){
  const layers = await api.callMethod(API_SPATIAL+"get_layers", {});
  const keep = selEl.value;
  selEl.innerHTML = layers.length
    ? layers.map(l=>`<option value="${api.escHtml(l.layer_name)}">${api.escHtml(l.layer_name)}</option>`).join("")
    : '<option value="">No layers yet</option>';
  if(layers.some(l=>l.layer_name===keep)) selEl.value = keep;
}

function renderFailRow(api, item){
  const name = item.name;
  return `
    <div class="layer-row" data-row="${api.escHtml(name)}" style="align-items:flex-start;flex-wrap:wrap">
      <div style="flex:1;min-width:140px">
        <div class="layer-name" title="${api.escHtml(item.title)}">${api.escHtml(item.title)}</div>
        <div class="import-fail">${api.escHtml(item.failed.join(", "))}</div>
      </div>
      <button class="btn btn-sm" data-repair="${api.escHtml(name)}"><i class="fa-solid fa-wrench"></i>Repair</button>
    </div>
    <div class="hint" data-repair-status="${api.escHtml(name)}" style="margin:0 0 8px"></div>`;
}

async function checkLayer(api, layerName, listEl, summaryEl){
  summaryEl.innerHTML = '<div class="empty-note">Checking…</div>';
  listEl.innerHTML = "";
  const fc = await api.callMethod(API_SPATIAL+"get_features_geojson", {});
  const all = fc.features || [];
  validateFeatureCache = all.filter(f=>(f.properties._layer||"Uncategorized") === layerName);

  const results = validateFeatureCache.map(f=>({
    name: f.properties._spatial_feature_name,
    title: f.properties._title || f.properties._spatial_feature_name,
    failed: runChecks(f),
  }));
  const failing = results.filter(r=>r.failed.length);

  summaryEl.innerHTML = `<div class="import-summary">${validateFeatureCache.length} feature(s) checked in "${api.escHtml(layerName)}" — <b>${failing.length}</b> with issues.</div>`;
  listEl.innerHTML = failing.length
    ? failing.map(item=>renderFailRow(api, item)).join("")
    : '<div class="empty-note">No issues found.</div>';

  listEl.querySelectorAll("[data-repair]").forEach(btn=>{
    btn.addEventListener("click", ()=>repairOne(api, btn.dataset.repair, listEl));
  });
}

async function repairOne(api, name, listEl){
  const feature = findValidateFeature(name);
  const statusEl = listEl.querySelector(`[data-repair-status="${CSS.escape(name)}"]`);
  const btn = listEl.querySelector(`[data-repair="${CSS.escape(name)}"]`);
  if(!feature) return;
  if(btn) btn.disabled = true;
  if(statusEl) statusEl.textContent = "Repairing…";
  try{
    const repaired = repairGeometry(feature);
    await api.callMethod(API_SPATIAL+"upsert_feature", {
      name,
      geometry: JSON.stringify(repaired),
    });
    const stillFailing = runChecks({type:"Feature", geometry: repaired});
    if(statusEl){
      statusEl.innerHTML = stillFailing.length
        ? `<span class="import-fail">Repaired, but still failing: ${api.escHtml(stillFailing.join(", "))}</span>`
        : `<span style="color:var(--green)">Fixed — all checks now pass.</span>`;
    }
    api.toast(stillFailing.length ? "Repaired with remaining issues — see details." : "Feature repaired.", !!stillFailing.length);
    await api.refreshLayers();
  }catch(e){
    if(statusEl) statusEl.innerHTML = `<span class="import-fail">Repair failed: ${api.escHtml(e.message)}</span>`;
    api.toast("Repair failed: "+e.message, true);
  }finally{
    if(btn) btn.disabled = false;
  }
}

// ─────────────────────────────  Panel  ─────────────────────────────
function renderPanel(container, api){
  container.innerHTML = `
    <div class="proc-card">
      <h4><i class="fa-solid fa-shapes" style="color:var(--amber)"></i>Difference &amp; Symmetric Difference</h4>
      <div class="hint">Both features must be Polygon or MultiPolygon.</div>
      <div class="field" style="margin-top:8px"><label>Feature A</label><select id="gtDiffA"></select></div>
      <div class="field"><label>Feature B</label><select id="gtDiffB"></select></div>
      <div style="display:flex;gap:7px">
        <button class="btn btn-sm" id="gtDiffBtn" style="flex:1;justify-content:center">Difference (A − B)</button>
        <button class="btn btn-sm" id="gtSymDiffBtn" style="flex:1;justify-content:center">Symmetric Diff (A △ B)</button>
      </div>
      <div id="gtDiffResult" style="margin-top:8px"></div>
      <button class="btn btn-sm btn-primary" id="gtDiffSaveBtn" style="width:100%;justify-content:center;margin-top:8px" disabled>Save as feature</button>
    </div>

    <div class="divider-line"></div>

    <div class="proc-card">
      <h4><i class="fa-solid fa-check-double" style="color:var(--blue)"></i>Geometry Validation &amp; Repair</h4>
      <div class="hint">Checks: self-intersection, unclosed rings, duplicate consecutive vertices, empty geometry.</div>
      <div class="field" style="margin-top:8px"><label>Layer</label><select id="gtValidateLayer"></select></div>
      <button class="btn btn-sm" id="gtCheckBtn" style="width:100%;justify-content:center">Check layer</button>
      <div id="gtValidateSummary" style="margin-top:8px"></div>
      <div id="gtValidateList" style="margin-top:4px"></div>
    </div>
  `;

  const diffA = container.querySelector("#gtDiffA");
  const diffB = container.querySelector("#gtDiffB");
  const diffResult = container.querySelector("#gtDiffResult");
  const diffSaveBtn = container.querySelector("#gtDiffSaveBtn");

  loadDiffFeatures(api, diffA, diffB);
  container.querySelector("#gtDiffBtn").addEventListener("click", ()=>runDiffOp(api, diffResult, diffSaveBtn, "diff"));
  container.querySelector("#gtSymDiffBtn").addEventListener("click", ()=>runDiffOp(api, diffResult, diffSaveBtn, "symdiff"));
  diffSaveBtn.addEventListener("click", ()=>saveDiffResult(api, diffResult, diffSaveBtn));

  const validateLayer = container.querySelector("#gtValidateLayer");
  const validateSummary = container.querySelector("#gtValidateSummary");
  const validateList = container.querySelector("#gtValidateList");

  loadValidateLayers(api, validateLayer);
  container.querySelector("#gtCheckBtn").addEventListener("click", ()=>{
    if(!validateLayer.value) return api.toast("No layer selected.", true);
    checkLayer(api, validateLayer.value, validateList, validateSummary);
  });
}

window.MapViewer.registerPlugin({
  id: "geometry-tools",
  onActivate(api){
    api.addToolbarButton({
      id: "btnGeometryTools",
      icon: "fa-shapes",
      label: "Geometry",
      title: "Geometry Tools — difference, symmetric difference, validation & repair",
      onClick: ()=> api.openDock("geomtools"),
    });
    api.addDockTab({
      tab: "geomtools",
      label: "Geometry",
      render: (container)=> renderPanel(container, api),
    });
  },
});
})();
