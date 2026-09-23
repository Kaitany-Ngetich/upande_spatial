// EPANET plugin for Map Viewer - built against the MapViewer plugin API
// (see the "Plugin API" section in the map-viewer Web Page's own script).
// Water network elements are ordinary Spatial Features - reference_doctype
// "EPANET Network", feature_role Junction/Tank/Reservoir/Pipe/Pump/Valve -
// created via epanet.add_element (never spatial.upsert_feature, which
// upserts by role and would silently merge every Junction into one record;
// see add_element's docstring). This plugin only adds the toolbar/panel to
// build, run and visualize a network, not a new way of storing one.
(function(){
"use strict";
const API_EPANET = "upande_spatial.api.epanet.";
const NODE_ROLES = ["Junction", "Tank", "Reservoir"];
const LINK_ROLES = ["Pipe", "Pump", "Valve"];

let resultLayerGroup = null;
let lastResult = null;
let currentNetwork = null;
let placement = null; // {kind:'node'|'link', role, clicks:[[lon,lat],...]}
let mapClickBound = false;

function colorForPressure(p, min, max){
  if(p==null || min==null || max==null || min===max) return "#8a8780";
  const t = Math.max(0, Math.min(1, (p-min)/(max-min)));
  const r = Math.round(200 - t*140);
  const g = Math.round(60 + t*150);
  return `rgb(${r},${g},60)`;
}

// ─────────────────────────────  Property forms  ─────────────────────────────
// Deliberately a fixed, small field set per role rather than a fully
// dynamic schema - covers what EPANET actually needs to solve, everything
// else stays at a sane default.
const ROLE_FIELDS = {
  Junction: [{key:"elevation_m", label:"Elevation (m)", def:0}, {key:"base_demand_lps", label:"Base demand (L/s)", def:0}],
  Tank: [{key:"elevation_m", label:"Elevation (m)", def:0}, {key:"init_level_m", label:"Initial level (m)", def:1}, {key:"min_level_m", label:"Min level (m)", def:0}, {key:"max_level_m", label:"Max level (m)", def:10}, {key:"diameter_m", label:"Diameter (m)", def:5}],
  Reservoir: [{key:"base_head_m", label:"Base head (m)", def:100}],
  Pipe: [{key:"diameter_mm", label:"Diameter (mm)", def:150}, {key:"roughness", label:"Roughness (Hazen-Williams C)", def:100}],
  Pump: [{key:"power_kw", label:"Power (kW)", def:5}],
  Valve: [{key:"valve_type", label:"Valve type", def:"PRV", options:["PRV","PSV","PBV","FCV","TCV","GPV"]}, {key:"diameter_mm", label:"Diameter (mm)", def:150}, {key:"initial_setting", label:"Initial setting", def:0}],
};

function fieldsHtml(role, api){
  return ROLE_FIELDS[role].map(f=>{
    const id = "epanetField_"+f.key;
    if(f.options){
      return `<div class="field"><label>${api.escHtml(f.label)}</label><select id="${id}">${f.options.map(o=>`<option ${o===f.def?"selected":""}>${o}</option>`).join("")}</select></div>`;
    }
    return `<div class="field"><label>${api.escHtml(f.label)}</label><input type="number" id="${id}" value="${f.def}" step="any"></div>`;
  }).join("");
}

function readFields(role){
  const props = {};
  ROLE_FIELDS[role].forEach(f=>{
    const el = document.getElementById("epanetField_"+f.key);
    if(!el) return;
    props[f.key] = f.options ? el.value : parseFloat(el.value);
  });
  return props;
}

// ─────────────────────────────  Placement (click-to-build) ─────────────────────────────
function ensureMapClickHandler(api){
  if(mapClickBound) return;
  mapClickBound = true;
  api.getMap().on("click", onMapClickForPlacement);
}

async function onMapClickForPlacement(e){
  if(!placement) return;
  const api = window.MapViewer;
  placement.clicks.push([e.latlng.lng, e.latlng.lat]);
  if(placement.kind === "node"){
    await openPropertyModal(api, placement.role, async (props)=>{
      await createElement(api, placement.role, {type:"Point", coordinates: placement.clicks[0]}, props);
    });
    placement = null;
  } else if(placement.clicks.length < 2){
    api.toast("Now click the "+placement.role.toLowerCase()+"'s end point (near the far node).");
  } else {
    await openPropertyModal(api, placement.role, async (props)=>{
      await createElement(api, placement.role, {type:"LineString", coordinates: placement.clicks.slice()}, props);
    });
    placement = null;
  }
}

function startPlacement(api, kind, role){
  placement = {kind, role, clicks: []};
  ensureMapClickHandler(api);
  api.toast(kind==="node"
    ? `Click the map to place the new ${role}.`
    : `Click the ${role}'s start point (at/near an existing node), then its end point.`);
}

async function createElement(api, role, geometry, properties){
  try{
    await api.callMethod(API_EPANET+"add_element", {
      network: currentNetwork, feature_role: role,
      geometry: JSON.stringify(geometry), properties: JSON.stringify(properties),
    });
    api.toast(role+" added.");
    await api.refreshLayers();
    await refreshElementList(api);
  }catch(e){
    api.toast("Could not add "+role+": "+e.message, true);
  }
}

// Small inline modal (built fresh each time, appended to <body>) so this
// plugin doesn't need to touch Map Viewer's own modal markup at all.
function openPropertyModal(api, role, onSave){
  return new Promise((resolve)=>{
    const scrim = document.createElement("div");
    scrim.className = "modal-scrim show";
    scrim.innerHTML = `
      <div class="modal">
        <div class="modal-head"><div class="modal-title">New ${api.escHtml(role)}</div><button class="float-close" id="epanetModalClose"><i class="fa-solid fa-xmark"></i></button></div>
        <div class="modal-body">${fieldsHtml(role, api)}</div>
        <div class="modal-foot">
          <button class="btn" id="epanetModalCancel">Cancel</button>
          <button class="btn btn-primary" id="epanetModalSave">Save</button>
        </div>
      </div>`;
    document.body.appendChild(scrim);
    const close = ()=>{ scrim.remove(); resolve(); };
    scrim.querySelector("#epanetModalClose").addEventListener("click", close);
    scrim.querySelector("#epanetModalCancel").addEventListener("click", close);
    scrim.querySelector("#epanetModalSave").addEventListener("click", async ()=>{
      const props = readFields(role);
      await onSave(props);
      scrim.remove();
      resolve();
    });
  });
}

// ─────────────────────────────  Element list  ─────────────────────────────
async function refreshElementList(api){
  const listEl = document.getElementById("epanetElementList");
  const hintEl = document.getElementById("epanetElementHint");
  if(!listEl || !currentNetwork) return;
  const fc = await api.callMethod(API_EPANET+"get_network_geojson", {network: currentNetwork});
  const feats = fc.features || [];
  const counts = {};
  feats.forEach(f=>{ const r = f.properties._feature_role; counts[r] = (counts[r]||0)+1; });
  if(hintEl) hintEl.textContent = `Junctions ${counts.Junction||0} · Tanks ${counts.Tank||0} · Reservoirs ${counts.Reservoir||0} · Pipes ${counts.Pipe||0} · Pumps ${counts.Pump||0} · Valves ${counts.Valve||0}`;
  listEl.innerHTML = feats.length ? feats.map(f=>`
    <div class="layer-row">
      <span class="badge b-blue" style="flex-shrink:0">${api.escHtml(f.properties._feature_role)}</span>
      <span class="layer-name" title="${api.escHtml(f.properties._title||f.properties._spatial_feature_name)}">${api.escHtml(f.properties._title||f.properties._spatial_feature_name)}</span>
      <button class="float-close" data-del="${api.escHtml(f.properties._spatial_feature_name)}" title="Delete"><i class="fa-solid fa-trash"></i></button>
    </div>`).join("") : '<div class="empty-note">No elements yet - use Add Node / Add Link above.</div>';
  listEl.querySelectorAll("[data-del]").forEach(btn=>{
    btn.addEventListener("click", async ()=>{
      try{
        await api.callMethod(API_EPANET+"delete_element", {name: btn.dataset.del});
        api.toast("Element deleted.");
        await api.refreshLayers();
        await refreshElementList(api);
      }catch(e){ api.toast("Could not delete: "+e.message, true); }
    });
  });
}

// ─────────────────────────────  Results ─────────────────────────────
function clearResultLayer(api){
  const map = api.getMap();
  if(resultLayerGroup && map) map.removeLayer(resultLayerGroup);
  resultLayerGroup = null;
}

async function drawResultLayer(api, network, result){
  clearResultLayer(api);
  const map = api.getMap();
  const fc = await api.callMethod(API_EPANET+"get_network_geojson", {network});
  const nodeResults = result.node_results || {};
  const linkResults = result.link_results || {};
  const pressures = Object.values(nodeResults).map(v=>v.pressure).filter(v=>v!=null);
  const minP = pressures.length ? Math.min(...pressures) : 0;
  const maxP = pressures.length ? Math.max(...pressures) : 0;

  resultLayerGroup = L.geoJSON(fc, {
    pointToLayer: (feature, latlng)=>{
      const r = nodeResults[feature.properties._spatial_feature_name];
      const color = r ? colorForPressure(r.pressure, minP, maxP) : "#8a8780";
      const marker = L.circleMarker(latlng, {radius:7, color:"#fff", weight:1.5, fillColor:color, fillOpacity:0.95});
      marker.bindTooltip(r ? `${api.escHtml(feature.properties._title||feature.properties._spatial_feature_name)}<br>Pressure: ${api.fmtNum(r.pressure,1)} m` : "No result");
      return marker;
    },
    style: (feature)=>{
      const r = linkResults[feature.properties._spatial_feature_name];
      const flow = r ? Math.abs(r.flow||0) : 0;
      return {color: r ? "#185FA5" : "#8a8780", weight: r ? Math.max(2, Math.min(9, flow*400+2)) : 2, opacity:0.9};
    },
    onEachFeature: (feature, layer)=>{
      const r = linkResults[feature.properties._spatial_feature_name];
      if(r) layer.bindTooltip(`${api.escHtml(feature.properties._title||feature.properties._spatial_feature_name)}<br>Flow: ${api.fmtNum(r.flow,4)} m³/s`);
    },
  }).addTo(map);
}

function renderResult(result, api, resultEl){
  const s = result.summary || {};
  let html = `<div class="kv-row"><span class="kv-l">Pressure</span><span class="kv-v">${api.fmtNum(s.min_pressure,1)} – ${api.fmtNum(s.max_pressure,1)} m</span></div>`;
  html += `<div class="kv-row"><span class="kv-l">Flow</span><span class="kv-v">${api.fmtNum(s.min_flow,4)} – ${api.fmtNum(s.max_flow,4)} m³/s</span></div>`;
  if(result.skipped_links && result.skipped_links.length){
    html += `<div class="hint" style="color:#b3261e;margin-top:6px">${result.skipped_links.length} link(s) skipped — `
      + result.skipped_links.map(sk=>api.escHtml(sk.name+": "+sk.reason)).join("; ") + "</div>";
  }
  resultEl.innerHTML = html;
}

// ─────────────────────────────  Panel ─────────────────────────────
async function refreshNetworks(api, selectEl){
  const networks = await api.callMethod(API_EPANET+"list_networks", {});
  selectEl.innerHTML = networks.length
    ? networks.map(n=>{
        const total = Object.values(n.element_counts||{}).reduce((a,b)=>a+b,0);
        return `<option value="${api.escHtml(n.name)}">${api.escHtml(n.network_name)} (${total} elements)</option>`;
      }).join("")
    : '<option value="">No networks yet - create one below</option>';
  return networks;
}

function renderPanel(container, api){
  container.innerHTML = `
    <div class="field">
      <label>Network</label>
      <select id="epanetNetworkSelect"></select>
    </div>
    <div style="display:flex;gap:7px;margin-bottom:10px">
      <input type="text" id="epanetNewName" placeholder="New network name" style="flex:1;font-size:12.5px;padding:7px 9px;border:1px solid var(--hairline);border-radius:var(--radius-sm);background:var(--surface);color:var(--ink-3)">
      <button class="btn btn-sm" id="epanetCreateBtn">Create</button>
    </div>
    <div class="divider-line"></div>

    <div class="proc-card">
      <h4><i class="fa-solid fa-diagram-project" style="color:var(--green)"></i>Build</h4>
      <div class="hint" id="epanetElementHint">Select a network to see its elements.</div>
      <div class="field-row" style="margin-top:8px">
        <div class="field"><label>Add node</label><select id="epanetNodeRole">${NODE_ROLES.map(r=>`<option>${r}</option>`).join("")}</select></div>
        <div class="field"><label>&nbsp;</label><button class="btn btn-sm" id="epanetPlaceNodeBtn" style="width:100%;justify-content:center" disabled><i class="fa-solid fa-location-dot"></i>Place</button></div>
      </div>
      <div class="field-row">
        <div class="field"><label>Add link</label><select id="epanetLinkRole">${LINK_ROLES.map(r=>`<option>${r}</option>`).join("")}</select></div>
        <div class="field"><label>&nbsp;</label><button class="btn btn-sm" id="epanetPlaceLinkBtn" style="width:100%;justify-content:center" disabled><i class="fa-solid fa-timeline"></i>Draw</button></div>
      </div>
      <div id="epanetElementList" style="margin-top:6px"></div>
    </div>

    <div class="proc-card">
      <h4><i class="fa-solid fa-droplet" style="color:var(--blue)"></i>Simulation</h4>
      <button class="btn btn-sm btn-primary" id="epanetRunBtn" style="width:100%;justify-content:center" disabled><i class="fa-solid fa-play"></i>Run simulation</button>
      <label class="check-row" style="margin-top:8px"><input type="checkbox" id="epanetShowResults">Color map by pressure / flow</label>
      <div id="epanetResult"></div>
    </div>
  `;

  const sel = container.querySelector("#epanetNetworkSelect");
  const runBtn = container.querySelector("#epanetRunBtn");
  const placeNodeBtn = container.querySelector("#epanetPlaceNodeBtn");
  const placeLinkBtn = container.querySelector("#epanetPlaceLinkBtn");
  const resultEl = container.querySelector("#epanetResult");
  const showResultsCb = container.querySelector("#epanetShowResults");

  function onNetworkChange(networks){
    currentNetwork = sel.value;
    const has = !!currentNetwork;
    runBtn.disabled = !has;
    placeNodeBtn.disabled = !has;
    placeLinkBtn.disabled = !has;
    lastResult = null;
    resultEl.innerHTML = "";
    showResultsCb.checked = false;
    clearResultLayer(api);
    if(has) refreshElementList(api);
  }

  refreshNetworks(api, sel).then(networks=>{
    sel.addEventListener("change", ()=>onNetworkChange(networks));
    if(networks.length) onNetworkChange(networks);
  });

  container.querySelector("#epanetCreateBtn").addEventListener("click", async ()=>{
    const nameInput = container.querySelector("#epanetNewName");
    const name = nameInput.value.trim();
    if(!name) return;
    try{
      await api.callMethod(API_EPANET+"create_network", {network_name: name});
      api.toast("Network created — add its elements below.");
      nameInput.value = "";
      const networks = await refreshNetworks(api, sel);
      const created = networks.find(n=>n.network_name===name);
      if(created) sel.value = created.name;
      onNetworkChange(networks);
    }catch(e){ api.toast("Could not create network: "+e.message, true); }
  });

  placeNodeBtn.addEventListener("click", ()=>{
    startPlacement(api, "node", container.querySelector("#epanetNodeRole").value);
  });
  placeLinkBtn.addEventListener("click", ()=>{
    startPlacement(api, "link", container.querySelector("#epanetLinkRole").value);
  });

  runBtn.addEventListener("click", async ()=>{
    if(!currentNetwork) return;
    runBtn.disabled = true;
    resultEl.innerHTML = '<div class="empty-note">Running EPANET…</div>';
    try{
      const result = await api.callMethod(API_EPANET+"run_simulation", {network: currentNetwork});
      lastResult = result;
      renderResult(result, api, resultEl);
      api.toast("Simulation complete.");
    }catch(e){
      resultEl.innerHTML = '<div class="import-fail">'+api.escHtml(e.message)+'</div>';
    }finally{
      runBtn.disabled = false;
    }
  });

  showResultsCb.addEventListener("change", async ()=>{
    if(!showResultsCb.checked){ clearResultLayer(api); return; }
    if(!lastResult){
      try{ lastResult = await api.callMethod(API_EPANET+"get_last_result", {network: currentNetwork}); }
      catch(e){ /* fall through to the no-result toast below */ }
    }
    if(lastResult){
      await drawResultLayer(api, currentNetwork, lastResult);
    } else {
      api.toast("Run a simulation first.", true);
      showResultsCb.checked = false;
    }
  });
}

window.MapViewer.registerPlugin({
  id: "epanet",
  onActivate(api){
    api.addToolbarButton({
      id: "btnEpanet",
      icon: "fa-droplet",
      label: "EPANET",
      title: "EPANET water network simulation",
      onClick: ()=> api.openDock("epanet"),
    });
    api.addDockTab({
      tab: "epanet",
      label: "EPANET",
      render: (container)=> renderPanel(container, api),
    });
  },
});
})();
