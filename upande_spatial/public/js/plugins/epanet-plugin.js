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
let mapMoveBound = false;

// Mirrors api/epanet.py's SNAP_TOLERANCE_M - a link endpoint drawn within
// this real-world distance of an existing node highlights that node (a
// dashed ring) before the click lands, and the click then uses the node's
// EXACT coordinate rather than wherever the cursor happened to be. That
// makes the connection topologically exact at creation time instead of
// relying on run_simulation's silent best-effort snap to catch it later.
const SNAP_TOLERANCE_M = 5;
let cachedNodes = [];   // [{name, role, title, latlng}] - refreshed alongside the element list
let snapMarker = null;  // the highlight ring layer, when a snap target is active
let snapTarget = null;  // {name, title, latlng} the next link-endpoint click will snap to

// Sequential 4-stop ramp reusing the app's own palette (blue -> green ->
// amber -> red) instead of a foreign one - low values read as "calm", high
// values as "critical", and it matches the exact hex values Spatial Entity
// Config already uses elsewhere in the app for other layers.
const RAMP_STOPS = [
  {t:0,    rgb:[24,95,165]},   // --blue
  {t:0.34, rgb:[59,109,17]},   // --green
  {t:0.67, rgb:[133,79,11]},   // --amber
  {t:1,    rgb:[179,38,30]},   // danger red, used elsewhere for delete/errors
];

function rampColor(t){
  if(t==null || isNaN(t)) return "#8a8780";
  t = Math.max(0, Math.min(1, t));
  let a = RAMP_STOPS[0], b = RAMP_STOPS[RAMP_STOPS.length-1];
  for(let i=0;i<RAMP_STOPS.length-1;i++){
    if(t>=RAMP_STOPS[i].t && t<=RAMP_STOPS[i+1].t){ a=RAMP_STOPS[i]; b=RAMP_STOPS[i+1]; break; }
  }
  const span = (b.t-a.t) || 1;
  const lt = (t-a.t)/span;
  const r = Math.round(a.rgb[0] + (b.rgb[0]-a.rgb[0])*lt);
  const g = Math.round(a.rgb[1] + (b.rgb[1]-a.rgb[1])*lt);
  const bch = Math.round(a.rgb[2] + (b.rgb[2]-a.rgb[2])*lt);
  return `rgb(${r},${g},${bch})`;
}

function rampCssGradient(){
  return `linear-gradient(90deg, ${RAMP_STOPS.map(s=>`rgb(${s.rgb[0]},${s.rgb[1]},${s.rgb[2]}) ${s.t*100}%`).join(", ")})`;
}

// Which result field each color-by choice reads, per role - node results
// carry pressure+head, link results carry flow+velocity (run_simulation
// already returns all four; only pressure/flow were ever wired to the map).
const NODE_COLOR_VARS = {
  pressure: {label:"Pressure", unit:"m",     get:(r)=>r.pressure},
  head:     {label:"Head",     unit:"m",     get:(r)=>r.head},
  quality:  {label:"Quality",  unit:"",      get:(r)=>r.quality},
};
const LINK_COLOR_VARS = {
  flow:     {label:"Flow",     unit:"m³/s", get:(r)=>r.flow!=null ? Math.abs(r.flow) : null},
  velocity: {label:"Velocity", unit:"m/s",       get:(r)=>r.velocity},
};

// ─────────────────────────────  Network options ─────────────────────────────
// Mirrors EPANET Network's Hydraulics/Time/Quality/Energy/Reactions fields
// one-for-one - see api/epanet.py's NETWORK_OPTION_FIELDS and
// _apply_network_options for what each one actually does to the solve.
const OPTION_GROUPS = [
  {
    label: "Hydraulics",
    fields: [
      {key:"hyd_trials", label:"Trials", def:40},
      {key:"hyd_accuracy", label:"Accuracy", def:0.001, step:"any"},
      {key:"hyd_unbalanced", label:"If Unbalanced", type:"select", options:["Stop","Continue"], def:"Continue"},
      {key:"hyd_unbalanced_trials", label:"Extra Trials Before Continuing", def:10, showIf:(v)=>v.hyd_unbalanced==="Continue"},
      {key:"hyd_demand_model", label:"Demand Model", type:"select", options:["DDA","PDA"], def:"DDA"},
      {key:"hyd_minimum_pressure", label:"Minimum Pressure (m)", def:0, showIf:(v)=>v.hyd_demand_model==="PDA"},
      {key:"hyd_required_pressure", label:"Required Pressure (m)", def:0.1, showIf:(v)=>v.hyd_demand_model==="PDA"},
      {key:"hyd_headloss", label:"Headloss Formula", type:"select", options:["H-W","D-W","C-M"], def:"H-W", hint:"Pipe roughness is read as H-W C, D-W mm or C-M n"},
      {key:"hyd_demand_multiplier", label:"Demand Multiplier", def:1, step:"any"},
    ],
  },
  {
    label: "Units & Fluid",
    fields: [
      {key:"hyd_flow_units", label:"Flow Units", type:"select", options:["LPS","LPM","MLD","CMH","CFS","GPM","MGD","IMGD","AFD"], def:"LPS", hint:"Map flow results, the EPANET report and .inp export use these. Element inputs stay in L/s."},
      {key:"hyd_emitter_exponent", label:"Emitter Exponent", def:0.5, step:"any"},
      {key:"hyd_specific_gravity", label:"Specific Gravity", def:1, step:"any"},
      {key:"hyd_viscosity", label:"Relative Viscosity", def:1, step:"any"},
    ],
  },
  {
    label: "Time",
    fields: [
      {key:"time_duration_hours", label:"Duration (hours)", def:0, hint:"0 = a single steady-state snapshot"},
      {key:"time_hydraulic_timestep_min", label:"Hydraulic Timestep (min)", def:60},
      {key:"time_pattern_timestep_min", label:"Pattern Timestep (min)", def:60},
      {key:"time_report_timestep_min", label:"Report Timestep (min)", def:60},
      {key:"time_start_clocktime", label:"Start Clocktime", type:"time", def:"00:00"},
      {key:"time_quality_timestep_min", label:"Quality Timestep (min)", def:5, step:"any"},
      {key:"time_statistic", label:"Statistic", type:"select", options:["None","Averaged","Minimum","Maximum","Range"], def:"None", hint:"Not None: results show this over the whole run (the time slider still shows each step)"},
    ],
  },
  {
    label: "Water Quality",
    fields: [
      {key:"qual_mode", label:"Analysis Type", type:"select", options:["None","Chemical","Age","Trace"], def:"None"},
      {key:"qual_chemical_name", label:"Chemical Name", type:"text", def:"", showIf:(v)=>v.qual_mode==="Chemical"},
      {key:"qual_units", label:"Units", type:"text", def:"mg/L", showIf:(v)=>v.qual_mode==="Chemical"},
      {key:"qual_trace_node", label:"Trace Node", type:"node-select", def:"", showIf:(v)=>v.qual_mode==="Trace"},
      {key:"qual_diffusivity", label:"Relative Diffusivity", def:1, step:"any", showIf:(v)=>v.qual_mode!=="None"},
      {key:"qual_tolerance", label:"Quality Tolerance", def:0.01, step:"any", showIf:(v)=>v.qual_mode!=="None"},
    ],
  },
  {
    label: "Energy",
    fields: [
      {key:"energy_price", label:"Global Energy Price (per kWh)", def:0, step:"any"},
      {key:"energy_efficiency_pct", label:"Global Pump Efficiency (%)", def:75},
    ],
  },
  {
    label: "Reactions",
    fields: [
      {key:"react_bulk_coeff", label:"Global Bulk Coeff. (1/day)", def:0, step:"any", hint:"Negative = decay"},
      {key:"react_wall_coeff", label:"Global Wall Coeff.", def:0, step:"any", hint:"m/day (1st order) or mg/m²/day (0 order)"},
      {key:"react_order_bulk", label:"Bulk Reaction Order", def:1, step:"any"},
      {key:"react_order_tank", label:"Tank Reaction Order", def:1, step:"any"},
      {key:"react_order_wall", label:"Wall Reaction Order", type:"select", options:["0","1"], def:"1"},
      {key:"react_limiting_potential", label:"Limiting Concentration", def:"", step:"any", hint:"Blank = no limit"},
      {key:"react_roughness_correlation", label:"Roughness Correlation", def:"", step:"any", hint:"Blank = off"},
    ],
  },
  {
    label: "Report",
    fields: [
      {key:"report_status", label:"Status Report", type:"select", options:["No","Yes","Full"], def:"Yes"},
      {key:"report_summary", label:"Input Summary", type:"select", options:["No","Yes"], def:"Yes"},
      {key:"report_energy", label:"Pump Energy Table", type:"select", options:["No","Yes"], def:"No"},
    ],
  },
];

// m3/s (what wntr returns) -> the network's Flow Units, for display.
const FLOW_UNIT_FACTORS = {LPS:1000, LPM:60000, MLD:86.4, CMH:3600, CFS:35.3147, GPM:15850.32, MGD:22.8245, IMGD:19.0053, AFD:70.0457};
const FLOW_UNIT_LABELS = {LPS:"L/s", LPM:"L/min", MLD:"ML/d", CMH:"m³/h", CFS:"ft³/s", GPM:"gpm", MGD:"MGD", IMGD:"IMGD", AFD:"ac·ft/d"};
function flowUnits(result){
  const u = (result && result.settings && result.settings.flow_units) || "";
  return FLOW_UNIT_FACTORS[u] ? u : null;
}
function flowFactor(result){ const u = flowUnits(result); return u ? FLOW_UNIT_FACTORS[u] : 1; }
function flowLabel(result){ const u = flowUnits(result); return u ? FLOW_UNIT_LABELS[u] : "m³/s"; }

function optionFieldHtml(field, values, api){
  const raw = values[field.key];
  const val = (raw === undefined || raw === null || raw === "") ? field.def : raw;
  const id = "epanetOpt_"+field.key;
  let input;
  if(field.type === "select"){
    input = `<select id="${id}">${field.options.map(o=>`<option ${o===val?"selected":""}>${api.escHtml(o)}</option>`).join("")}</select>`;
  } else if(field.type === "node-select"){
    const opts = cachedNodes.map(n=>`<option value="${api.escHtml(n.name)}" ${n.name===val?"selected":""}>${api.escHtml(n.title)}</option>`).join("");
    input = `<select id="${id}"><option value="">— choose a node —</option>${opts}</select>`;
  } else if(field.type === "time"){
    const hhmm = typeof val === "string" ? val.slice(0,5) : "00:00";
    input = `<input type="time" id="${id}" value="${api.escHtml(hhmm)}">`;
  } else if(field.type === "text"){
    input = `<input type="text" id="${id}" value="${api.escHtml(val||"")}">`;
  } else {
    input = `<input type="number" id="${id}" value="${val==null?"":val}" ${field.step?`step="${field.step}"`:""}>`;
  }
  return `<div class="field" data-optfield="${field.key}" style="flex:1 1 220px"><label>${api.escHtml(field.label)}</label>${input}${field.hint?`<div class="hint" style="margin-top:2px">${api.escHtml(field.hint)}</div>`:""}</div>`;
}

function readOptionsForm(){
  const out = {};
  OPTION_GROUPS.forEach(g=>g.fields.forEach(f=>{
    const el = document.getElementById("epanetOpt_"+f.key);
    if(!el) return;
    if(f.type === "select" || f.type === "node-select" || f.type === "text"){
      out[f.key] = el.value || null;
    } else if(f.type === "time"){
      out[f.key] = el.value ? el.value+":00" : null;
    } else {
      out[f.key] = el.value === "" ? null : parseFloat(el.value);
    }
  }));
  return out;
}

function currentOptionsFormValues(){
  const vals = {};
  OPTION_GROUPS.forEach(g=>g.fields.forEach(f=>{
    const el = document.getElementById("epanetOpt_"+f.key);
    if(el) vals[f.key] = el.value;
  }));
  return vals;
}

function updateOptionsVisibility(body){
  const vals = currentOptionsFormValues();
  OPTION_GROUPS.forEach(g=>g.fields.forEach(f=>{
    if(!f.showIf) return;
    const row = body.querySelector(`[data-optfield="${f.key}"]`);
    if(row) row.style.display = f.showIf(vals) ? "" : "none";
  }));
}

function openOptionsModal(api, network){
  return new Promise(async (resolve)=>{
    let values;
    try{
      values = await api.callMethod(API_EPANET+"get_network_options", {network});
    }catch(e){
      api.toast("Could not load options: "+e.message, true);
      resolve(false);
      return;
    }

    const scrim = document.createElement("div");
    scrim.className = "modal-scrim show";
    scrim.innerHTML = `
      <div class="modal" style="max-width:560px">
        <div class="modal-head"><div class="modal-title">Network Options</div><button class="float-close" id="epanetOptClose"><i class="fa-solid fa-xmark"></i></button></div>
        <div class="modal-body" id="epanetOptBody" style="max-height:62vh;overflow-y:auto">
          ${OPTION_GROUPS.map(g=>`
            <div class="proc-card" style="margin-bottom:10px">
              <h4>${api.escHtml(g.label)}</h4>
              <div class="field-row" style="flex-wrap:wrap;row-gap:10px">
                ${g.fields.map(f=>optionFieldHtml(f, values, api)).join("")}
              </div>
            </div>`).join("")}
        </div>
        <div class="modal-foot">
          <button class="btn" id="epanetOptCancel">Cancel</button>
          <button class="btn btn-primary" id="epanetOptSave">Save</button>
        </div>
      </div>`;
    document.body.appendChild(scrim);

    const body = scrim.querySelector("#epanetOptBody");
    updateOptionsVisibility(body);
    body.addEventListener("change", ()=>updateOptionsVisibility(body));

    const close = ()=>{ scrim.remove(); resolve(false); };
    scrim.querySelector("#epanetOptClose").addEventListener("click", close);
    scrim.querySelector("#epanetOptCancel").addEventListener("click", close);
    scrim.querySelector("#epanetOptSave").addEventListener("click", async ()=>{
      const payload = readOptionsForm();
      try{
        await api.callMethod(API_EPANET+"update_network_options", {network, options: JSON.stringify(payload)});
        api.toast("Options saved.");
        scrim.remove();
        resolve(true);
      }catch(e){
        api.toast("Could not save options: "+e.message, true);
      }
    });
  });
}

// ─────────────────────────────  Property forms  ─────────────────────────────
// Field set per role, modelled on the WN (Utilities) doctypes' EPANET
// sections plus their asset/physical details. Everything lands in the
// element's Spatial Feature `properties` JSON; the keys under "Hydraulics"
// and "Water quality" are exactly what api/epanet.py's _build_model reads,
// "Asset details" are record-keeping only (the solver never looks at them).
// `library` fields are dropdowns of EPANET Patterns / Curves (fetched per
// network via list_library when the modal opens); blank = none.
// `blank:true` numbers may be left empty = "use the network's global value".
const SECT_HYD = "Hydraulics", SECT_QUAL = "Water quality", SECT_ASSET = "Asset details";
const QUALITY_FIELDS = [
  {key:"initial_quality", label:"Initial quality (mg/L, hrs or %)", blank:true, section:SECT_QUAL},
  {key:"source_type", label:"Quality source", def:"", options:["","CONCEN","MASS","FLOWPACED","SETPOINT"], section:SECT_QUAL},
  {key:"source_strength", label:"Source strength (mg/L, MASS: mg/min)", blank:true, section:SECT_QUAL},
  {key:"source_pattern", label:"Source pattern", library:"patterns", section:SECT_QUAL},
];
const COMMON_HEAD = [
  {key:"in_model", label:"Include in model", def:"1", options:["1","0"], section:SECT_HYD},
];
const ROLE_FIELDS = {
  Junction: [
    ...COMMON_HEAD,
    {key:"elevation_m", label:"Elevation (m)", def:0, section:SECT_HYD},
    {key:"base_demand_lps", label:"Base demand (L/s)", def:0, section:SECT_HYD},
    {key:"demand_pattern", label:"Demand pattern", library:"patterns", section:SECT_HYD},
    {key:"demand_category", label:"Demand category", type:"text", section:SECT_HYD},
    {key:"emitter_coeff", label:"Emitter coeff. (L/s per m½)", def:0, section:SECT_HYD},
    ...QUALITY_FIELDS,
    {key:"status", label:"Status", def:"Active", options:["Active","Inactive","Proposed","Decommissioned"], section:SECT_ASSET},
    {key:"elevation_source", label:"Elevation source", def:"", options:["","Manual","DEM Auto","GPS Survey","LiDAR"], section:SECT_ASSET},
    {key:"description", label:"Description", type:"text", section:SECT_ASSET},
    {key:"mergin_feature_id", label:"Mergin feature ID", type:"text", section:SECT_ASSET},
  ],
  Tank: [
    ...COMMON_HEAD,
    {key:"elevation_m", label:"Base elevation (m)", def:0, section:SECT_HYD},
    {key:"init_level_m", label:"Initial level (m)", def:1, section:SECT_HYD},
    {key:"min_level_m", label:"Min level (m)", def:0, section:SECT_HYD},
    {key:"max_level_m", label:"Max level (m)", def:10, section:SECT_HYD},
    {key:"diameter_m", label:"Diameter (m)", def:5, section:SECT_HYD},
    {key:"min_vol_m3", label:"Min volume (m³)", def:0, section:SECT_HYD},
    {key:"vol_curve", label:"Volume curve", library:"curves.Volume", section:SECT_HYD},
    {key:"overflow", label:"Can overflow", def:"0", options:["0","1"], section:SECT_HYD},
    ...QUALITY_FIELDS,
    {key:"mixing_model", label:"Mixing model", def:"", options:["","MIXED","2COMP","FIFO","LIFO"], section:SECT_QUAL},
    {key:"mixing_fraction", label:"Mixing fraction (2COMP)", blank:true, section:SECT_QUAL},
    {key:"bulk_coeff", label:"Bulk coeff. (1/day, blank = global)", blank:true, section:SECT_QUAL},
    {key:"status", label:"Status", def:"In Service", options:["In Service","Offline","Under Maintenance","Decommissioned"], section:SECT_ASSET},
    {key:"description", label:"Description", type:"text", section:SECT_ASSET},
    {key:"mergin_feature_id", label:"Mergin feature ID", type:"text", section:SECT_ASSET},
  ],
  Reservoir: [
    ...COMMON_HEAD,
    {key:"base_head_m", label:"Total head (m)", def:100, section:SECT_HYD},
    {key:"head_pattern", label:"Head pattern", library:"patterns", section:SECT_HYD},
    ...QUALITY_FIELDS,
    {key:"status", label:"Status", def:"In Service", options:["In Service","Offline","Under Maintenance","Decommissioned"], section:SECT_ASSET},
    {key:"reservoir_type", label:"Reservoir type", def:"", options:["","Source Intake","Service Reservoir","Break Pressure Tank","External Network Connection"], section:SECT_ASSET},
    {key:"capacity_m3", label:"Capacity (m³)", blank:true, section:SECT_ASSET},
    {key:"water_source", label:"Water source", type:"text", section:SECT_ASSET},
    {key:"description", label:"Description", type:"text", section:SECT_ASSET},
    {key:"mergin_feature_id", label:"Mergin feature ID", type:"text", section:SECT_ASSET},
  ],
  Pipe: [
    ...COMMON_HEAD,
    {key:"diameter_mm", label:"Diameter (mm)", def:150, section:SECT_HYD},
    {key:"roughness", label:"Roughness (H-W C / D-W mm / C-M n)", def:100, section:SECT_HYD},
    {key:"minor_loss", label:"Minor loss coeff.", def:0, section:SECT_HYD},
    {key:"initial_status", label:"Initial status", def:"Open", options:["Open","Closed","CV"], section:SECT_HYD},
    {key:"length_override_m", label:"Length (m) - blank = from map", blank:true, section:SECT_HYD},
    {key:"bulk_coeff", label:"Bulk coeff. (1/day, blank = global)", blank:true, section:SECT_QUAL},
    {key:"wall_coeff", label:"Wall coeff. (blank = global)", blank:true, section:SECT_QUAL},
    {key:"operational_status", label:"Operational status", def:"In Service", options:["In Service","Isolated","Abandoned","Proposed"], section:SECT_ASSET},
    {key:"material", label:"Material", def:"", options:["","uPVC","HDPE","Ductile Iron","Cast Iron","Asbestos Cement","Steel","GRP","Other"], section:SECT_ASSET},
    {key:"pressure_class", label:"Pressure class", type:"text", section:SECT_ASSET},
    {key:"installation_year", label:"Installation year", blank:true, section:SECT_ASSET},
    {key:"condition_grade", label:"Condition grade", def:"", options:["","1-Good","2-Fair","3-Poor","4-Critical"], section:SECT_ASSET},
    {key:"description", label:"Description", type:"text", section:SECT_ASSET},
    {key:"mergin_feature_id", label:"Mergin feature ID", type:"text", section:SECT_ASSET},
  ],
  Pump: [
    ...COMMON_HEAD,
    {key:"pump_curve", label:"Pump curve (blank = fixed power)", library:"curves.Pump", section:SECT_HYD},
    {key:"power_kw", label:"Power (kW) - used when no curve", def:5, section:SECT_HYD},
    {key:"speed", label:"Relative speed", def:1, section:SECT_HYD},
    {key:"speed_pattern", label:"Speed pattern", library:"patterns", section:SECT_HYD},
    {key:"efficiency_curve", label:"Efficiency curve", library:"curves.Efficiency", section:SECT_HYD},
    {key:"energy_price", label:"Energy price (per kWh, 0 = global)", def:0, section:SECT_HYD},
    {key:"energy_pattern", label:"Energy price pattern", library:"patterns", section:SECT_HYD},
    {key:"initial_status", label:"Initial status", def:"Open", options:["Open","Closed"], section:SECT_HYD},
    {key:"network_status", label:"Network status", def:"In Service", options:["In Service","Standby","Under Maintenance","Decommissioned"], section:SECT_ASSET},
    {key:"pump_type", label:"Pump type", def:"", options:["","Centrifugal","Submersible","Borehole","Booster","End-Suction"], section:SECT_ASSET},
    {key:"design_flow_m3h", label:"Design flow (m³/h)", blank:true, section:SECT_ASSET},
    {key:"design_head_m", label:"Design head (m)", blank:true, section:SECT_ASSET},
    {key:"motor_power_kw", label:"Motor power (kW)", blank:true, section:SECT_ASSET},
    {key:"control_type", label:"Control type", def:"", options:["","Manual","Float Switch","Pressure Switch","VFD","SCADA"], section:SECT_ASSET},
    {key:"description", label:"Description", type:"text", section:SECT_ASSET},
    {key:"mergin_feature_id", label:"Mergin feature ID", type:"text", section:SECT_ASSET},
  ],
  Valve: [
    ...COMMON_HEAD,
    {key:"valve_type", label:"Valve type", def:"PRV", options:["PRV","PSV","PBV","FCV","TCV","GPV"], section:SECT_HYD},
    {key:"diameter_mm", label:"Diameter (mm)", def:150, section:SECT_HYD},
    {key:"initial_setting", label:"Setting (m, or L/s for FCV)", def:0, section:SECT_HYD},
    {key:"headloss_curve", label:"Headloss curve (GPV only)", library:"curves.Headloss", section:SECT_HYD},
    {key:"minor_loss", label:"Minor loss coeff.", def:0, section:SECT_HYD},
    {key:"initial_status", label:"Initial status", def:"Active", options:["Active","Open","Closed"], section:SECT_HYD},
    {key:"network_status", label:"Network status", def:"In Service", options:["In Service","Closed-Locked","Under Maintenance","Decommissioned"], section:SECT_ASSET},
    {key:"valve_function", label:"Valve function", def:"", options:["","Isolating","Pressure Reducing","Pressure Sustaining","Flow Control","Air Release","Scour","Check"], section:SECT_ASSET},
    {key:"actuation", label:"Actuation", def:"", options:["","Manual","Electric","Hydraulic","Pneumatic","Self-Actuating"], section:SECT_ASSET},
    {key:"body_material", label:"Body material", def:"", options:["","Cast Iron","Ductile Iron","Brass","Stainless Steel","PVC","Bronze"], section:SECT_ASSET},
    {key:"installation_depth_m", label:"Installation depth (m)", blank:true, section:SECT_ASSET},
    {key:"chamber_type", label:"Chamber type", def:"", options:["","None","Surface Box","Valve Chamber","Chamber with Access Hatch"], section:SECT_ASSET},
    {key:"description", label:"Description", type:"text", section:SECT_ASSET},
    {key:"mergin_feature_id", label:"Mergin feature ID", type:"text", section:SECT_ASSET},
  ],
};
let libraryCache = {patterns: [], curves: {}};

function libraryOptions(source){
  if(source === "patterns") return libraryCache.patterns || [];
  const type = source.split(".")[1];
  return (libraryCache.curves || {})[type] || [];
}

function fieldHtml(f, values, api){
  const id = "epanetField_"+f.key;
  const has = values && Object.prototype.hasOwnProperty.call(values, f.key) && values[f.key] !== null;
  const val = has ? String(values[f.key]) : (f.def === undefined ? "" : String(f.def));
  const label = `<label>${api.escHtml(f.label)}</label>`;
  if(f.library){
    const opts = libraryOptions(f.library).map(o=>`<option value="${api.escHtml(o)}" ${o===val?"selected":""}>${api.escHtml(o)}</option>`).join("");
    return `<div class="field">${label}<select id="${id}"><option value="">— none —</option>${opts}</select></div>`;
  }
  if(f.options){
    const opts = f.options.map(o=>`<option value="${api.escHtml(o)}" ${o===val?"selected":""}>${o===""?"—":api.escHtml(f.key==="in_model"||f.key==="overflow" ? (o==="1"?"Yes":"No") : o)}</option>`).join("");
    return `<div class="field">${label}<select id="${id}">${opts}</select></div>`;
  }
  if(f.type === "text"){
    return `<div class="field">${label}<input type="text" id="${id}" value="${api.escHtml(val)}"></div>`;
  }
  return `<div class="field">${label}<input type="number" id="${id}" value="${api.escHtml(val)}" step="any"></div>`;
}

function fieldsHtml(role, api, values){
  const sections = [];
  ROLE_FIELDS[role].forEach(f=>{
    let sec = sections.find(s=>s.name===f.section);
    if(!sec){ sec = {name:f.section, fields:[]}; sections.push(sec); }
    sec.fields.push(f);
  });
  // Hydraulics open; quality and asset details folded away until needed.
  return sections.map((sec, i)=>`
    <details ${i===0?"open":""} style="margin-bottom:8px">
      <summary style="cursor:pointer;font-weight:600;font-size:12.5px;margin-bottom:6px">${api.escHtml(sec.name)}</summary>
      ${sec.fields.map(f=>fieldHtml(f, values, api)).join("")}
    </details>`).join("");
}

// `previous` keeps keys this form doesn't know about (e.g. ones the network
// generator or an older version wrote), so editing never drops data.
function readFields(role, previous){
  const props = Object.assign({}, previous || {});
  ROLE_FIELDS[role].forEach(f=>{
    const el = document.getElementById("epanetField_"+f.key);
    if(!el) return;
    const raw = el.value;
    if(f.library || f.type === "text" || (f.options && f.options.includes(""))){
      if(raw === "") delete props[f.key]; else props[f.key] = raw;
    } else if(f.options){
      props[f.key] = raw;
    } else if(raw === "" || isNaN(parseFloat(raw))){
      if(f.blank) delete props[f.key]; else props[f.key] = f.def;
    } else {
      props[f.key] = parseFloat(raw);
    }
  });
  return props;
}

// ─────────────────────────────  Placement (click-to-build) ─────────────────────────────
function ensureMapClickHandler(api){
  if(mapClickBound) return;
  mapClickBound = true;
  api.getMap().on("click", onMapClickForPlacement);
}

// Live snap preview - only meaningful for link endpoints (a brand-new node
// placement has nothing to snap to). Runs on every mousemove but is a no-op
// unless a link placement is in progress, mirroring how onMapClickForPlacement
// already gates itself on `placement` being set.
function ensureMapMoveHandler(api){
  if(mapMoveBound) return;
  mapMoveBound = true;
  api.getMap().on("mousemove", (e)=>onMapMoveForSnap(e, api));
}

function clearSnapMarker(api){
  const map = api.getMap();
  if(snapMarker && map) map.removeLayer(snapMarker);
  snapMarker = null;
  snapTarget = null;
}

function onMapMoveForSnap(e, api){
  if(!placement || placement.kind !== "link" || !cachedNodes.length){
    if(snapMarker) clearSnapMarker(api);
    return;
  }
  const map = api.getMap();
  let nearest = null, nearestD = null;
  for(const node of cachedNodes){
    const d = map.distance(e.latlng, node.latlng);
    if(nearestD === null || d < nearestD){ nearest = node; nearestD = d; }
  }
  if(nearest && nearestD <= SNAP_TOLERANCE_M){
    snapTarget = nearest;
    if(!snapMarker){
      snapMarker = L.circleMarker(nearest.latlng, {radius:13, color:"#228883", weight:2.5, fill:false, dashArray:"3,3", interactive:false}).addTo(map);
    } else {
      snapMarker.setLatLng(nearest.latlng);
    }
  } else if(snapMarker){
    clearSnapMarker(api);
  }
}

async function onMapClickForPlacement(e){
  if(!placement) return;
  const api = window.MapViewer;
  const snapped = placement.kind === "link" ? snapTarget : null;
  const coord = snapped ? [snapped.latlng.lng, snapped.latlng.lat] : [e.latlng.lng, e.latlng.lat];
  clearSnapMarker(api);
  placement.clicks.push(coord);
  if(placement.kind === "node"){
    await openPropertyModal(api, placement.role, async (props, title)=>{
      await createElement(api, placement.role, {type:"Point", coordinates: placement.clicks[0]}, props, title);
    });
    placement = null;
  } else if(placement.clicks.length < 2){
    api.toast(snapped
      ? `Snapped to ${snapped.title}. Now click the ${placement.role.toLowerCase()}'s end point.`
      : "Now click the "+placement.role.toLowerCase()+"'s end point (near the far node).");
  } else {
    await openPropertyModal(api, placement.role, async (props, title)=>{
      await createElement(api, placement.role, {type:"LineString", coordinates: placement.clicks.slice()}, props, title);
    });
    placement = null;
  }
}

function startPlacement(api, kind, role){
  placement = {kind, role, clicks: []};
  ensureMapClickHandler(api);
  ensureMapMoveHandler(api);
  api.toast(kind==="node"
    ? `Click the map to place the new ${role}.`
    : `Click the ${role}'s start point (at/near an existing node), then its end point.`);
}

async function createElement(api, role, geometry, properties, title){
  try{
    await api.callMethod(API_EPANET+"add_element", {
      network: currentNetwork, feature_role: role,
      geometry: JSON.stringify(geometry), properties: JSON.stringify(properties),
      title: title || undefined,
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
// `existing` = {title, props} when editing an element, omitted for a new one.
async function openPropertyModal(api, role, onSave, existing){
  try{ libraryCache = await api.callMethod(API_EPANET+"list_library", {network: currentNetwork}) || libraryCache; }
  catch(e){ /* dropdowns just stay empty - the element can still be created */ }
  return new Promise((resolve)=>{
    const scrim = document.createElement("div");
    scrim.className = "modal-scrim show";
    const heading = existing ? `Edit ${api.escHtml(role)}` : `New ${api.escHtml(role)}`;
    scrim.innerHTML = `
      <div class="modal">
        <div class="modal-head"><div class="modal-title">${heading}</div><button class="float-close" id="epanetModalClose"><i class="fa-solid fa-xmark"></i></button></div>
        <div class="modal-body" style="max-height:66vh;overflow-y:auto">
          <div class="field"><label>Name</label><input type="text" id="epanetFieldTitle" value="${api.escHtml((existing && existing.title) || "")}" placeholder="e.g. J-12 or Main line A"></div>
          ${fieldsHtml(role, api, existing && existing.props)}
        </div>
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
      const props = readFields(role, existing && existing.props);
      const title = scrim.querySelector("#epanetFieldTitle").value.trim();
      await onSave(props, title);
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
  cachedNodes = feats
    .filter(f=>NODE_ROLES.includes(f.properties._feature_role) && f.geometry && f.geometry.type === "Point")
    .map(f=>({
      name: f.properties._spatial_feature_name,
      title: f.properties._title || f.properties._spatial_feature_name,
      latlng: L.latLng(f.geometry.coordinates[1], f.geometry.coordinates[0]),
    }));
  const counts = {};
  feats.forEach(f=>{ const r = f.properties._feature_role; counts[r] = (counts[r]||0)+1; });
  if(hintEl) hintEl.textContent = `Junctions ${counts.Junction||0} · Tanks ${counts.Tank||0} · Reservoirs ${counts.Reservoir||0} · Pipes ${counts.Pipe||0} · Pumps ${counts.Pump||0} · Valves ${counts.Valve||0}`;
  listEl.innerHTML = feats.length ? feats.map(f=>`
    <div class="layer-row">
      <span class="badge b-blue" style="flex-shrink:0">${api.escHtml(f.properties._feature_role)}</span>
      <span class="layer-name" title="${api.escHtml(f.properties._title||f.properties._spatial_feature_name)}">${api.escHtml(f.properties._title||f.properties._spatial_feature_name)}</span>
      ${f.properties._props && f.properties._props.in_model !== undefined && String(f.properties._props.in_model)==="0" ? '<span class="badge" title="Excluded from the model">off</span>' : ""}
      <button class="float-close" data-edit="${api.escHtml(f.properties._spatial_feature_name)}" title="Edit"><i class="fa-solid fa-pen"></i></button>
      <button class="float-close" data-del="${api.escHtml(f.properties._spatial_feature_name)}" title="Delete"><i class="fa-solid fa-trash"></i></button>
    </div>`).join("") : '<div class="empty-note">No elements yet - use Add Node / Add Link above.</div>';
  listEl.querySelectorAll("[data-edit]").forEach(btn=>{
    btn.addEventListener("click", async ()=>{
      const f = feats.find(x=>x.properties._spatial_feature_name===btn.dataset.edit);
      if(!f) return;
      const role = f.properties._feature_role;
      await openPropertyModal(api, role, async (props, title)=>{
        try{
          await api.callMethod(API_EPANET+"update_element", {name: btn.dataset.edit, properties: JSON.stringify(props), title: title || undefined});
          api.toast(role+" updated.");
          await api.refreshLayers();
          await refreshElementList(api);
        }catch(e){ api.toast("Could not update: "+e.message, true); }
      }, {title: f.properties._title, props: f.properties._props || {}});
    });
  });
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

// node_results/link_results for one report timestep of an extended-period
// run, rebuilt from result.timeseries in the same {name: {pressure,...}}
// shape as the run's own final-step results. step == null -> final step.
function resultsAtStep(result, step){
  const ts = result.timeseries;
  if(step == null || !ts || !ts.times || !ts.times.length){
    return {nodeResults: result.node_results || {}, linkResults: result.link_results || {}};
  }
  const at = (table, name)=>{ const v = table && table[name]; return v ? v[step] : null; };
  const node = ts.node || {}, link = ts.link || {};
  const nodeResults = {}, linkResults = {};
  Object.keys(node.pressure || {}).forEach(n=>{
    nodeResults[n] = {pressure: at(node.pressure, n), head: at(node.head, n), quality: at(node.quality, n)};
  });
  Object.keys(link.flow || {}).forEach(n=>{
    linkResults[n] = {flow: at(link.flow, n), velocity: at(link.velocity, n)};
  });
  return {nodeResults, linkResults};
}

function clockLabel(result, seconds){
  const start = (result.settings && result.settings.start_clocktime_s) || 0;
  const total = Math.round(start + seconds);
  const day = Math.floor(total / 86400), rem = total % 86400;
  const hhmm = String(Math.floor(rem/3600)).padStart(2,"0")+":"+String(Math.floor(rem%3600/60)).padStart(2,"0");
  return day ? `Day ${day+1} ${hhmm}` : hhmm;
}

async function drawResultLayer(api, network, result, nodeVarKey, linkVarKey, step){
  clearResultLayer(api);
  const map = api.getMap();
  const fc = await api.callMethod(API_EPANET+"get_network_geojson", {network});
  const {nodeResults, linkResults} = resultsAtStep(result, step);
  let nodeVar = NODE_COLOR_VARS[nodeVarKey] || NODE_COLOR_VARS.pressure;
  let linkVar = LINK_COLOR_VARS[linkVarKey] || LINK_COLOR_VARS.flow;
  if(nodeVar === NODE_COLOR_VARS.quality){
    nodeVar = Object.assign({}, nodeVar, {unit: (result.settings && result.settings.quality_units) || ""});
  }
  if(linkVar === LINK_COLOR_VARS.flow){
    const k = flowFactor(result);
    linkVar = Object.assign({}, linkVar, {unit: flowLabel(result), get:(r)=>r.flow!=null ? Math.abs(r.flow)*k : null});
  }

  const nodeVals = Object.values(nodeResults).map(nodeVar.get).filter(v=>v!=null);
  const linkVals = Object.values(linkResults).map(linkVar.get).filter(v=>v!=null);
  const nodeMin = nodeVals.length ? Math.min(...nodeVals) : 0;
  const nodeMax = nodeVals.length ? Math.max(...nodeVals) : 0;
  const linkMin = linkVals.length ? Math.min(...linkVals) : 0;
  const linkMax = linkVals.length ? Math.max(...linkVals) : 0;

  resultLayerGroup = L.geoJSON(fc, {
    pointToLayer: (feature, latlng)=>{
      const r = nodeResults[feature.properties._spatial_feature_name];
      const val = r ? nodeVar.get(r) : null;
      const t = val==null ? null : (nodeMax>nodeMin ? (val-nodeMin)/(nodeMax-nodeMin) : 0.5);
      const color = t==null ? "#8a8780" : rampColor(t);
      const marker = L.circleMarker(latlng, {radius:7, color:"#fff", weight:1.5, fillColor:color, fillOpacity:0.95});
      marker.bindTooltip(val!=null
        ? `${api.escHtml(feature.properties._title||feature.properties._spatial_feature_name)}<br>${nodeVar.label}: ${api.fmtNum(val,2)} ${nodeVar.unit}`
        : "No result");
      return marker;
    },
    style: (feature)=>{
      const r = linkResults[feature.properties._spatial_feature_name];
      const val = r ? linkVar.get(r) : null;
      const t = val==null ? null : (linkMax>linkMin ? (val-linkMin)/(linkMax-linkMin) : 0.5);
      const flowMag = r ? Math.abs(r.flow||0) : 0;
      return {color: t==null ? "#8a8780" : rampColor(t), weight: r ? Math.max(2, Math.min(9, flowMag*400+2)) : 2, opacity:0.9};
    },
    onEachFeature: (feature, layer)=>{
      const r = linkResults[feature.properties._spatial_feature_name];
      const val = r ? linkVar.get(r) : null;
      if(val!=null) layer.bindTooltip(`${api.escHtml(feature.properties._title||feature.properties._spatial_feature_name)}<br>${linkVar.label}: ${api.fmtNum(val,4)} ${linkVar.unit}`);
    },
  }).addTo(map);

  return {nodeMin, nodeMax, nodeVar, linkMin, linkMax, linkVar};
}

function renderLegend(legendEl, api, ranges){
  if(!ranges){ legendEl.innerHTML = ""; return; }
  const {nodeMin, nodeMax, nodeVar, linkMin, linkMax, linkVar} = ranges;
  const row = (label, min, max, unit)=>`
    <div style="margin-top:8px">
      <div style="font-size:11px;font-weight:600;color:var(--ink-4);margin-bottom:3px">${api.escHtml(label)}</div>
      <div style="height:8px;border-radius:4px;background:${rampCssGradient()}"></div>
      <div style="display:flex;justify-content:space-between;font-size:10.5px;color:var(--ink-mute);margin-top:2px">
        <span>${api.fmtNum(min,2)}</span><span>${api.fmtNum(max,2)} ${api.escHtml(unit)}</span>
      </div>
    </div>`;
  legendEl.innerHTML = row("Nodes — "+nodeVar.label, nodeMin, nodeMax, nodeVar.unit)
    + row("Links — "+linkVar.label, linkMin, linkMax, linkVar.unit);
}

function renderResult(result, api, resultEl){
  const nodeResults = result.node_results || {};
  const linkResults = result.link_results || {};
  const heads = Object.values(nodeResults).map(v=>v.head).filter(v=>v!=null);
  const velocities = Object.values(linkResults).map(v=>v.velocity).filter(v=>v!=null);
  const s = result.summary || {};
  let html = `<div class="kv-row"><span class="kv-l">Pressure</span><span class="kv-v">${api.fmtNum(s.min_pressure,1)} – ${api.fmtNum(s.max_pressure,1)} m</span></div>`;
  if(heads.length) html += `<div class="kv-row"><span class="kv-l">Head</span><span class="kv-v">${api.fmtNum(Math.min(...heads),1)} – ${api.fmtNum(Math.max(...heads),1)} m</span></div>`;
  const fk = flowFactor(result);
  html += `<div class="kv-row"><span class="kv-l">Flow</span><span class="kv-v">${api.fmtNum(s.min_flow==null?null:s.min_flow*fk,3)} – ${api.fmtNum(s.max_flow==null?null:s.max_flow*fk,3)} ${api.escHtml(flowLabel(result))}</span></div>`;
  if(velocities.length) html += `<div class="kv-row"><span class="kv-l">Velocity</span><span class="kv-v">${api.fmtNum(Math.min(...velocities),3)} – ${api.fmtNum(Math.max(...velocities),3)} m/s</span></div>`;
  if(s.min_junction_pressure_all_steps != null && (result.timeseries && (result.timeseries.times||[]).length > 1)){
    html += `<div class="kv-row"><span class="kv-l">Pressure (whole run)</span><span class="kv-v">${api.fmtNum(s.min_junction_pressure_all_steps,1)} – ${api.fmtNum(s.max_junction_pressure_all_steps,1)} m</span></div>`;
  }
  const energy = Object.values(s.pump_energy_kwh || {});
  if(energy.length){
    const cost = Object.values(s.pump_cost || {}).reduce((a,b)=>a+(b||0), 0);
    html += `<div class="kv-row"><span class="kv-l">Pump energy</span><span class="kv-v">${api.fmtNum(energy.reduce((a,b)=>a+(b||0),0),1)} kWh · cost ${api.fmtNum(cost,2)}</span></div>`;
  }
  const st = result.settings || {};
  if(st.statistic && st.statistic !== "NONE"){
    html += `<div class="hint" style="margin-top:4px">Values above are the run's ${api.escHtml(st.statistic.toLowerCase())} per element.</div>`;
  }
  if(st.excluded){
    html += `<div class="hint" style="margin-top:4px">${st.excluded} element(s) excluded from the model.</div>`;
  }
  if(result.run){
    html += `<div style="margin-top:6px;display:flex;gap:10px;font-size:12px"><a href="/app/epanet-simulation-run/${encodeURIComponent(result.run)}" target="_blank">EPANET report &amp; run record</a></div>`;
  }
  if(result.warnings && result.warnings.length){
    html += `<div class="hint" style="color:#8a5a00;margin-top:6px">${result.warnings.map(w=>api.escHtml(w)).join("<br>")}</div>`;
  }
  if(result.skipped_links && result.skipped_links.length){
    html += `<div class="hint" style="color:#b3261e;margin-top:6px">${result.skipped_links.length} link(s) skipped — `
      + result.skipped_links.map(sk=>api.escHtml(sk.name+": "+sk.reason)).join("; ") + "</div>";
  }
  resultEl.innerHTML = html;
}

// ─────────────────────────────  Panel ─────────────────────────────
// A network's "owner" is a Dynamic Link (reference_doctype/reference_name),
// same pattern Spatial Feature itself already uses - a network can belong
// to a Farm, a Warehouse, a Location, or anything else registered in
// Spatial Entity Config, or nothing at all. Not hardcoded to farms: the
// map's global farm filter (Farm-only, a pre-existing app-wide concept)
// only ever narrows Farm-owned networks - every other owner type, and
// every unowned network, always stays visible regardless of it.
let ownerDoctypeOptions = null; // cached - doesn't change during a session

function currentGlobalFarm(){
  const el = document.getElementById("farmFilter");
  return el ? el.value : "";
}

async function getOwnerDoctypeOptions(api){
  if(!ownerDoctypeOptions){
    const all = await api.callMethod(API_EPANET+"list_owner_doctypes", {});
    ownerDoctypeOptions = all.filter(d=>d!=="EPANET Network");
  }
  return ownerDoctypeOptions;
}

async function ownerFieldsHtml(api){
  const doctypes = await getOwnerDoctypeOptions(api);
  const typeOpts = '<option value="">No specific owner</option>'
    + doctypes.map(d=>`<option value="${api.escHtml(d)}">${api.escHtml(d)}</option>`).join("");
  return `
    <div class="field" style="flex:1"><label>New network's owner type</label><select id="epanetNewOwnerType">${typeOpts}</select></div>
    <div class="field" style="flex:1"><label>Owner</label><select id="epanetNewOwnerName" disabled><option value="">— pick a type first —</option></select></div>`;
}

async function refreshOwnerCandidates(api, doctype, selectEl){
  if(!doctype){
    selectEl.innerHTML = '<option value="">— pick a type first —</option>';
    selectEl.disabled = true;
    return;
  }
  selectEl.disabled = false;
  const candidates = await api.callMethod(API_EPANET+"list_owner_candidates", {doctype});
  const preselect = doctype === "Farm" ? currentGlobalFarm() : "";
  selectEl.innerHTML = '<option value="">— none —</option>'
    + candidates.map(c=>`<option value="${api.escHtml(c.name)}" ${c.name===preselect?"selected":""}>${api.escHtml(c.title)}</option>`).join("");
}

async function refreshNetworks(api, selectEl){
  const networks = await api.callMethod(API_EPANET+"list_networks", {});
  const activeFarm = currentGlobalFarm();
  const visible = activeFarm
    ? networks.filter(n=>n.reference_doctype!=="Farm" || n.reference_name===activeFarm)
    : networks;
  selectEl.innerHTML = visible.length
    ? visible.map(n=>{
        const total = Object.values(n.element_counts||{}).reduce((a,b)=>a+b,0);
        const ownerTag = n.reference_name ? ` — ${n.reference_doctype}: ${n.reference_name}` : "";
        return `<option value="${api.escHtml(n.name)}">${api.escHtml(n.network_name)}${api.escHtml(ownerTag)} (${total} elements)</option>`;
      }).join("")
    : '<option value="">No networks yet - create one below</option>';
  return visible;
}

function renderPanel(container, api){
  container.innerHTML = `
    <div class="field-row">
      <div class="field" style="flex:1"><label>Network</label><select id="epanetNetworkSelect"></select></div>
      <div class="field" style="flex:0 0 auto"><label>&nbsp;</label><button class="btn btn-sm" id="epanetOptionsBtn" title="Network options (Hydraulics, Time, Quality, Energy, Reactions)" disabled><i class="fa-solid fa-sliders"></i></button></div>
    </div>
    <div class="field-row" id="epanetOwnerFields" style="margin-bottom:6px"></div>
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
      <div style="display:flex;gap:7px">
        <button class="btn btn-sm btn-primary" id="epanetRunBtn" style="flex:1;justify-content:center" disabled><i class="fa-solid fa-play"></i>Run simulation</button>
        <button class="btn btn-sm" id="epanetExportBtn" title="Download as an EPANET .inp file (opens in EPANET, QGIS, WNTR)" disabled><i class="fa-solid fa-file-export"></i>.inp</button>
      </div>
      <label class="check-row" style="margin-top:8px"><input type="checkbox" id="epanetShowResults">Show results on map</label>
      <div class="field-row" id="epanetColorByRow" style="margin-top:6px;display:none">
        <div class="field"><label>Node color</label><select id="epanetNodeColorVar">
          <option value="pressure">Pressure</option><option value="head">Head</option><option value="quality">Quality</option>
        </select></div>
        <div class="field"><label>Link color</label><select id="epanetLinkColorVar">
          <option value="flow">Flow</option><option value="velocity">Velocity</option>
        </select></div>
      </div>
      <div class="field" id="epanetTimeRow" style="margin-top:6px;display:none">
        <label>Time: <b id="epanetTimeLabel"></b></label>
        <input type="range" id="epanetTimeStep" min="0" max="0" step="1" value="0" style="width:100%">
      </div>
      <div id="epanetLegend"></div>
      <div id="epanetResult"></div>
    </div>
  `;

  const sel = container.querySelector("#epanetNetworkSelect");
  const optionsBtn = container.querySelector("#epanetOptionsBtn");
  const runBtn = container.querySelector("#epanetRunBtn");
  const exportBtn = container.querySelector("#epanetExportBtn");
  const placeNodeBtn = container.querySelector("#epanetPlaceNodeBtn");
  const placeLinkBtn = container.querySelector("#epanetPlaceLinkBtn");
  const resultEl = container.querySelector("#epanetResult");
  const showResultsCb = container.querySelector("#epanetShowResults");
  const colorByRow = container.querySelector("#epanetColorByRow");
  const nodeColorSel = container.querySelector("#epanetNodeColorVar");
  const linkColorSel = container.querySelector("#epanetLinkColorVar");
  const legendEl = container.querySelector("#epanetLegend");
  const timeRow = container.querySelector("#epanetTimeRow");
  const timeSlider = container.querySelector("#epanetTimeStep");
  const timeLabel = container.querySelector("#epanetTimeLabel");

  // Slider over the run's report timesteps - only shown for an
  // extended-period run (more than one timestep). Defaults to the last
  // step, which is what node_results/link_results have always been.
  // With a Statistic set, the slider gets one extra position past the last
  // step showing that statistic (node_results/link_results), its default.
  function statisticLabel(){
    const st = lastResult && lastResult.settings && lastResult.settings.statistic;
    return st && st !== "NONE" ? st.charAt(0)+st.slice(1).toLowerCase()+" (whole run)" : null;
  }
  function syncTimeSlider(){
    const times = (lastResult && lastResult.timeseries && lastResult.timeseries.times) || [];
    if(times.length < 2 || !showResultsCb.checked){ timeRow.style.display = "none"; return; }
    timeRow.style.display = "block";
    const max = times.length - 1 + (statisticLabel() ? 1 : 0);
    if(Number(timeSlider.max) !== max){
      timeSlider.max = max;
      timeSlider.value = max;
    }
    const i = Number(timeSlider.value);
    timeLabel.textContent = i >= times.length ? statisticLabel() : clockLabel(lastResult, times[i]);
  }
  function currentStep(){
    const times = (lastResult && lastResult.timeseries && lastResult.timeseries.times) || [];
    if(times.length < 2) return null;
    const i = Number(timeSlider.value);
    return i >= times.length ? null : i;
  }

  async function updateResultsDisplay(){
    if(!showResultsCb.checked) return;
    if(!lastResult){
      try{ lastResult = await api.callMethod(API_EPANET+"get_last_result", {network: currentNetwork}); }
      catch(e){ /* fall through to the no-result toast below */ }
    }
    if(lastResult){
      syncTimeSlider();
      const ranges = await drawResultLayer(api, currentNetwork, lastResult, nodeColorSel.value, linkColorSel.value, currentStep());
      renderLegend(legendEl, api, ranges);
    } else {
      api.toast("Run a simulation first.", true);
      showResultsCb.checked = false;
      colorByRow.style.display = "none";
    }
  }

  function onNetworkChange(networks){
    currentNetwork = sel.value;
    const has = !!currentNetwork;
    optionsBtn.disabled = !has;
    runBtn.disabled = !has;
    exportBtn.disabled = !has;
    placeNodeBtn.disabled = !has;
    placeLinkBtn.disabled = !has;
    lastResult = null;
    timeSlider.max = 0;
    timeRow.style.display = "none";
    resultEl.innerHTML = "";
    showResultsCb.checked = false;
    colorByRow.style.display = "none";
    legendEl.innerHTML = "";
    clearResultLayer(api);
    clearSnapMarker(api);
    cachedNodes = [];
    if(has) refreshElementList(api);
  }

  refreshNetworks(api, sel).then(networks=>{
    sel.addEventListener("change", ()=>onNetworkChange(networks));
    if(networks.length) onNetworkChange(networks);
  });

  // Owner type/name selects are built async (list_owner_doctypes is an API
  // call) - the field-row starts empty and fills in right after, same
  // fire-and-forget pattern as everything else in this panel that needs a
  // round-trip before it can render.
  const ownerFieldsEl = container.querySelector("#epanetOwnerFields");
  ownerFieldsHtml(api).then(html=>{
    ownerFieldsEl.innerHTML = html;
    const ownerTypeSel = container.querySelector("#epanetNewOwnerType");
    const ownerNameSel = container.querySelector("#epanetNewOwnerName");
    ownerTypeSel.addEventListener("change", ()=>refreshOwnerCandidates(api, ownerTypeSel.value, ownerNameSel));
  });

  // The map's global farm filter re-scopes which networks show up here too -
  // this listener is added once (renderPanel only runs once, at plugin
  // activation) and just keeps living alongside the global select for the
  // rest of the page's life. Only Farm-owned networks are ever narrowed by
  // it; every other owner type (or no owner) stays visible regardless.
  const globalFarmFilter = document.getElementById("farmFilter");
  if(globalFarmFilter){
    globalFarmFilter.addEventListener("change", async ()=>{
      const networks = await refreshNetworks(api, sel);
      const stillVisible = networks.some(n=>n.name===currentNetwork);
      if(!stillVisible) onNetworkChange(networks);
      const ownerTypeSel = container.querySelector("#epanetNewOwnerType");
      const ownerNameSel = container.querySelector("#epanetNewOwnerName");
      if(ownerTypeSel && ownerTypeSel.value === "Farm") refreshOwnerCandidates(api, "Farm", ownerNameSel);
    });
  }

  container.querySelector("#epanetCreateBtn").addEventListener("click", async ()=>{
    const nameInput = container.querySelector("#epanetNewName");
    const ownerTypeSel = container.querySelector("#epanetNewOwnerType");
    const ownerNameSel = container.querySelector("#epanetNewOwnerName");
    const name = nameInput.value.trim();
    if(!name) return;
    try{
      await api.callMethod(API_EPANET+"create_network", {
        network_name: name,
        reference_doctype: (ownerTypeSel && ownerTypeSel.value) || undefined,
        reference_name: (ownerNameSel && ownerNameSel.value) || undefined,
      });
      api.toast("Network created — add its elements below.");
      nameInput.value = "";
      const networks = await refreshNetworks(api, sel);
      const created = networks.find(n=>n.network_name===name);
      if(created) sel.value = created.name;
      onNetworkChange(networks);
    }catch(e){ api.toast("Could not create network: "+e.message, true); }
  });

  optionsBtn.addEventListener("click", ()=>{
    if(currentNetwork) openOptionsModal(api, currentNetwork);
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
      timeSlider.max = 0; // forces syncTimeSlider to jump to the new run's last step
      renderResult(result, api, resultEl);
      api.toast("Simulation complete.");
      await updateResultsDisplay();
    }catch(e){
      resultEl.innerHTML = '<div class="import-fail">'+api.escHtml(e.message)+'</div>';
    }finally{
      runBtn.disabled = false;
    }
  });

  exportBtn.addEventListener("click", ()=>{
    if(!currentNetwork) return;
    // A plain navigation: the endpoint answers with a file download.
    window.open("/api/method/"+API_EPANET+"export_inp?network="+encodeURIComponent(currentNetwork), "_blank");
  });

  showResultsCb.addEventListener("change", async ()=>{
    if(!showResultsCb.checked){
      clearResultLayer(api);
      colorByRow.style.display = "none";
      timeRow.style.display = "none";
      legendEl.innerHTML = "";
      return;
    }
    colorByRow.style.display = "flex";
    await updateResultsDisplay();
  });

  nodeColorSel.addEventListener("change", updateResultsDisplay);
  linkColorSel.addEventListener("change", updateResultsDisplay);
  timeSlider.addEventListener("input", ()=>{ syncTimeSlider(); });
  timeSlider.addEventListener("change", updateResultsDisplay);
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
