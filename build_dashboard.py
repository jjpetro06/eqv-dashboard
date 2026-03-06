#!/usr/bin/env python3
"""
Build the EQV Production Dashboard HTML from Excel files:
- EQV Historical Prod.xlsx (Actuals): production by month by propnum
- EQV Well Monthly CF Export.xlsx (Forecast): forecast by month by lease
- EQV Well Info.xlsx: maps lease to propnum, provides well metadata
- well_routes_export_1.xlsx: maps wells to routes (with API)
- pumper-tech list.xlsx: maps routes to foreman/tech/pumper hierarchy
"""

import pandas as pd
import json
import gzip
import base64
import re


def normalize_well_name(s):
    """Normalize well name for matching."""
    s = str(s).upper().strip()
    s = s.replace('"', '').replace("'", '').replace(',', '').replace('#', '')
    s = re.sub(r'\s*\(.*?\)', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def normalize_aggressive(s):
    """More aggressive normalization: remove dashes, SL, leading zeros."""
    s = normalize_well_name(s)
    s = s.replace('-', '').replace(' SL ', ' ')
    tokens = s.split()
    out = []
    for t in tokens:
        if re.match(r'^0+\d+', t):
            t = t.lstrip('0') or '0'
        out.append(t)
    return ' '.join(out)


def build_data():
    print("Reading Excel files...")
    wi = pd.read_excel("EQV Well Info.xlsx")
    actuals = pd.read_excel("EQV Historical Prod.xlsx")
    forecast = pd.read_excel("EQV Well Monthly CF Export.xlsx")
    route_export = pd.read_excel("well_routes_export_1.xlsx")
    tech_list = pd.read_excel("pumper-tech list.xlsx")

    # --- Well Info: build lookup tables ---
    lease_to_propnum = dict(zip(wi["LEASE"], wi["PROPNUM"]))

    # Filter to wells with valid AREA (exclude special entries like _ABAN, _HEDGES)
    wi_valid = wi[wi["AREA"].notna()].copy()

    propnum_info = {}
    for _, row in wi_valid.iterrows():
        propnum_info[row["PROPNUM"]] = {
            "lease": row["LEASE"],
            "area": str(row["AREA"]),
            "play_area": str(row["PLAY_AREA"]) if pd.notna(row["PLAY_AREA"]) else "Other",
            "op_non": str(row["OP_NON"]) if pd.notna(row["OP_NON"]) else "Unknown",
            "major": str(row["MAJOR"]) if pd.notna(row["MAJOR"]) else "Unknown",
        }

    # --- Build LEASE name lookup (normalized) ---
    lease_norm1 = {}
    lease_norm2 = {}
    for _, row in wi.iterrows():
        n1 = normalize_well_name(row["LEASE"])
        n2 = normalize_aggressive(row["LEASE"])
        lease_norm1[n1] = row["PROPNUM"]
        lease_norm2[n2] = row["PROPNUM"]

    def match_well_to_propnum(well_name):
        """Try to match a well name from route export to a PROPNUM."""
        n1 = normalize_well_name(well_name)
        if n1 in lease_norm1:
            return lease_norm1[n1]
        n2 = normalize_aggressive(well_name)
        if n2 in lease_norm2:
            return lease_norm2[n2]
        return None

    # --- Build route -> foreman/tech/pumper mapping ---
    print("Building pumper hierarchy...")
    route_info = {}
    for _, row in tech_list.iterrows():
        route = str(row["ROUTE"]).strip()
        route_info[route] = {
            "foreman": str(row["FOREMAN"]).strip().title(),
            "tech": str(row["TECH"]).strip().title(),
            "pumper": str(row["PUMPER1"]).strip().title(),
        }

    # --- Build well -> route mapping from route export (EQV company only) ---
    eqv_routes = route_export[route_export["Company"] == "EQV"].copy()

    # --- Actuals ---
    print("Processing actuals...")
    actuals["MONTH"] = actuals["P_DATE"].dt.strftime("%Y-%m")
    actuals_grouped = (
        actuals.groupby(["PROPNUM", "MONTH"])
        .agg({"OIL": "sum", "GAS": "sum", "WATER": "sum"})
        .reset_index()
    )

    well_actuals = {}
    for propnum, group in actuals_grouped.groupby("PROPNUM"):
        if propnum not in propnum_info:
            continue
        data = {}
        for _, row in group.iterrows():
            data[row["MONTH"]] = [row["OIL"], row["GAS"], row["WATER"]]
        well_actuals[propnum] = data

    # --- Forecast ---
    print("Processing forecast...")
    forecast["MONTH"] = forecast["OUTDATE"].dt.strftime("%Y-%m")

    well_forecasts = {}
    for lease, group in forecast.groupby("LEASE"):
        propnum = lease_to_propnum.get(lease)
        if not propnum or propnum not in propnum_info:
            continue
        data = {}
        for _, row in group.iterrows():
            data[row["MONTH"]] = [
                row["Gross Oil, bbl"],
                row["Gross Gas, mcf"],
                0,
            ]
        well_forecasts[propnum] = data

    # --- Date range ---
    date_min = "2015-01"
    all_forecast_months = set()
    for d in well_forecasts.values():
        all_forecast_months.update(d.keys())
    date_max = max(all_forecast_months) if all_forecast_months else "2035-12"

    # --- Per-well combined data ---
    print("Building per-well data...")
    well_data = {}
    for propnum in propnum_info:
        actuals_d = well_actuals.get(propnum, {})
        forecasts_d = well_forecasts.get(propnum, {})

        months = sorted(set(actuals_d.keys()) | set(forecasts_d.keys()))
        months = [m for m in months if date_min <= m <= date_max]

        if not months:
            continue

        records = []
        for m in months:
            a = actuals_d.get(m, [0, 0, 0])
            f = forecasts_d.get(m, [0, 0, 0])
            record = [
                m,
                round(a[0], 1),
                round(a[1], 1),
                round(a[2], 1),
                round(f[0], 1),
                round(f[1], 1),
                round(f[2], 1),
                0,
                0,
                0,
            ]
            if any(v > 0 for v in record[1:7]):
                records.append(record)

        if records:
            well_data[propnum] = records

    # --- Build hierarchy: foreman -> tech -> pumper -> [[propnum, well_name], ...] ---
    print("Building hierarchy...")
    hierarchy = {}
    pumper_wells_map = {}  # pumper_key -> list of [propnum, well_name]

    for _, row in eqv_routes.iterrows():
        well_name = str(row["Well Name"]).strip()
        route = str(row["Route"]).strip()
        propnum = match_well_to_propnum(well_name)

        ri = route_info.get(route)
        if not ri:
            continue

        foreman = ri["foreman"]
        tech = ri["tech"]
        pumper = ri["pumper"]

        if foreman not in hierarchy:
            hierarchy[foreman] = {}
        if tech not in hierarchy[foreman]:
            hierarchy[foreman][tech] = {}
        if pumper not in hierarchy[foreman][tech]:
            hierarchy[foreman][tech][pumper] = []

        # Only add wells that have production data
        if propnum and propnum in well_data:
            hierarchy[foreman][tech][pumper].append([propnum, well_name])
            # Track for pumperWells lookup
            key = f"{foreman}|{tech}|{pumper}"
            if key not in pumper_wells_map:
                pumper_wells_map[key] = {}
            pumper_wells_map[key][propnum] = well_data[propnum]

    # Remove empty pumpers/techs/foremen
    for foreman in list(hierarchy.keys()):
        for tech in list(hierarchy[foreman].keys()):
            for pumper in list(hierarchy[foreman][tech].keys()):
                if not hierarchy[foreman][tech][pumper]:
                    del hierarchy[foreman][tech][pumper]
            if not hierarchy[foreman][tech]:
                del hierarchy[foreman][tech]
        if not hierarchy[foreman]:
            del hierarchy[foreman]

    # Sort wells within each pumper
    for foreman in hierarchy:
        for tech in hierarchy[foreman]:
            for pumper in hierarchy[foreman][tech]:
                hierarchy[foreman][tech][pumper].sort(key=lambda w: w[1])

    # --- Aggregation ---
    def aggregate_wells(propnums):
        monthly = {}
        for pn in propnums:
            if pn not in well_data:
                continue
            for record in well_data[pn]:
                m = record[0]
                if m not in monthly:
                    monthly[m] = [m, 0, 0, 0, 0, 0, 0, 0, 0, 0]
                for i in range(1, 10):
                    monthly[m][i] += record[i]
        result = []
        for m in sorted(monthly.keys()):
            r = monthly[m]
            result.append([r[0]] + [round(v, 1) for v in r[1:]])
        return result

    def compute_scales(data_dict):
        all_vals = []
        for records in data_dict.values():
            for r in records:
                for v in r[1:]:
                    if v > 0:
                        all_vals.append(v)
        if not all_vals:
            return {"min": 1, "max": 100}
        return {"min": round(min(all_vals), 1), "max": round(max(all_vals), 1)}

    # Company level (all wells that are in the hierarchy)
    all_propnums_in_hier = set()
    for foreman in hierarchy:
        for tech in hierarchy[foreman]:
            for pumper in hierarchy[foreman][tech]:
                for w in hierarchy[foreman][tech][pumper]:
                    all_propnums_in_hier.add(w[0])
    all_propnums_in_hier = list(all_propnums_in_hier)
    eqv_data = aggregate_wells(all_propnums_in_hier)

    # Foreman level
    foreman_groups = {}
    for foreman in hierarchy:
        propnums = []
        for tech in hierarchy[foreman]:
            for pumper in hierarchy[foreman][tech]:
                propnums.extend([w[0] for w in hierarchy[foreman][tech][pumper]])
        foreman_groups[foreman] = aggregate_wells(propnums)

    # Tech level
    tech_groups = {}
    for foreman in hierarchy:
        for tech in hierarchy[foreman]:
            propnums = []
            for pumper in hierarchy[foreman][tech]:
                propnums.extend([w[0] for w in hierarchy[foreman][tech][pumper]])
            tech_groups[tech] = aggregate_wells(propnums)

    # Pumper level
    pumper_groups = {}
    for foreman in hierarchy:
        for tech in hierarchy[foreman]:
            for pumper in hierarchy[foreman][tech]:
                propnums = [w[0] for w in hierarchy[foreman][tech][pumper]]
                pumper_groups[pumper] = aggregate_wells(propnums)

    # Build pumperWells (per pumper -> propnum -> data)
    pumper_wells = {}
    for foreman in hierarchy:
        for tech in hierarchy[foreman]:
            for pumper in hierarchy[foreman][tech]:
                if pumper not in pumper_wells:
                    pumper_wells[pumper] = {}
                for propnum, _ in hierarchy[foreman][tech][pumper]:
                    if propnum in well_data:
                        pumper_wells[pumper][propnum] = well_data[propnum]

    # Final JSON
    output = {
        "presidio": {
            "groups": {"EQV Resources": eqv_data},
            "scales": compute_scales({"all": eqv_data}),
        },
        "foreman": {"groups": foreman_groups, "scales": compute_scales(foreman_groups)},
        "tech": {
            "groups": tech_groups,
            "scales": compute_scales(tech_groups),
        },
        "pumper": {
            "groups": pumper_groups,
            "scales": compute_scales(pumper_groups),
        },
        "pumperWells": pumper_wells,
        "wellScales": compute_scales(well_data),
        "hierarchy": hierarchy,
        "dateRange": [date_min, date_max],
    }

    # Stats
    total_wells = sum(
        len(wells)
        for foreman in hierarchy
        for tech in hierarchy[foreman].values()
        for wells in tech.values()
    )
    print(f"Total wells in dashboard: {total_wells}")
    print(f"Foremen: {list(hierarchy.keys())}")
    for fm in hierarchy:
        print(f"  {fm}: Techs = {list(hierarchy[fm].keys())}")
        for tc in hierarchy[fm]:
            pumpers = list(hierarchy[fm][tc].keys())
            well_count = sum(len(hierarchy[fm][tc][p]) for p in pumpers)
            print(f"    {tc}: {len(pumpers)} pumpers, {well_count} wells")
    print(f"Date range: {date_min} to {date_max}")

    # Compress
    json_str = json.dumps(output)
    compressed = gzip.compress(json_str.encode("utf-8"))
    b64 = base64.b64encode(compressed).decode("ascii")

    print(f"JSON size: {len(json_str):,} bytes")
    print(f"Compressed: {len(compressed):,} bytes")
    print(f"Base64: {len(b64):,} chars")

    return b64


def build_html(compressed_data):
    """Generate the complete EQV dashboard HTML."""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EQV Production Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{{--bg:#f3f0ea;--surface:#fff;--surface2:#eae5dc;--border:#d9d2c7;--border-light:#e8e3db;--text:#2d2b28;--text-mid:#5a564f;--text-dim:#8a857d;--oil:#16a34a;--gas:#dc2626;--water:#2563eb;--accent:#c96442;--sidebar-bg:#faf8f5;--sidebar-hover:#f0ece5;--sidebar-active:#e8e0d4}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);height:100vh;overflow:hidden;display:flex;flex-direction:column}}
.app-header{{background:var(--surface);border-bottom:1px solid var(--border);padding:10px 24px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0;z-index:100}}
.app-header h1{{font-size:18px;font-weight:600;letter-spacing:-.02em}}
.app-header h1 span{{color:var(--accent);font-weight:700}}
.header-legend{{display:flex;gap:14px;align-items:center;flex-wrap:wrap}}
.legend-item{{display:flex;align-items:center;gap:4px;font-size:10px;color:var(--text-dim);font-weight:500}}
.legend-dot{{width:14px;height:3px;border-radius:1px}}
.legend-dash{{width:14px;height:0;border-top:2px dashed}}
.main-layout{{display:flex;flex:1;overflow:hidden}}
.sidebar{{width:280px;min-width:280px;background:var(--sidebar-bg);border-right:1px solid var(--border);overflow-y:auto;flex-shrink:0;padding:10px 0}}
.sidebar::-webkit-scrollbar{{width:5px}}.sidebar::-webkit-scrollbar-track{{background:transparent}}.sidebar::-webkit-scrollbar-thumb{{background:var(--border);border-radius:3px}}
.nav-section-label{{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text-dim);padding:10px 14px 5px}}
.nb{{display:flex;align-items:center;gap:7px;cursor:pointer;border:none;background:none;width:100%;text-align:left;transition:all .12s;position:relative;font-family:'DM Sans',sans-serif;color:var(--text-mid)}}
.nb:hover{{background:var(--sidebar-hover);color:var(--text)}}
.nb.active{{background:var(--sidebar-active);color:var(--text);font-weight:600}}
.nb.active::before{{content:'';position:absolute;left:0;top:3px;bottom:3px;width:3px;background:var(--accent);border-radius:0 2px 2px 0}}
.d0{{padding:7px 14px;font-size:12.5px;font-weight:500}}
.d1{{padding:5px 14px 5px 30px;font-size:11.5px}}
.d2{{padding:4px 14px 4px 46px;font-size:11px;color:var(--text-dim)}}
.d3{{padding:3px 14px 3px 62px;font-size:10.5px;color:var(--text-dim)}}
.chv{{margin-left:auto;font-size:9px;color:var(--text-dim);transition:transform .2s;flex-shrink:0}}
.nb.expanded>.chv{{transform:rotate(90deg)}}
.coll{{overflow:hidden;max-height:0;transition:max-height .25s ease}}.coll.open{{max-height:99999px}}
.ndot{{width:5px;height:5px;border-radius:50%;flex-shrink:0}}
.content-area{{flex:1;overflow-y:auto;padding:16px 24px}}
.content-area::-webkit-scrollbar{{width:6px}}.content-area::-webkit-scrollbar-track{{background:transparent}}.content-area::-webkit-scrollbar-thumb{{background:var(--border);border-radius:3px}}
.content-header{{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid var(--border-light)}}
.content-title{{font-size:18px;font-weight:700;letter-spacing:-.02em}}
.breadcrumb{{font-size:11px;color:var(--text-dim);margin-top:1px}}
.controls-row{{display:flex;gap:16px;align-items:center;flex-shrink:0;flex-wrap:wrap}}
.date-controls{{display:flex;gap:6px;align-items:center}}
.date-controls label{{font-size:11px;color:var(--text-dim);font-weight:600}}
.date-controls input[type=month]{{background:var(--surface);border:1px solid var(--border);color:var(--text);padding:3px 6px;border-radius:5px;font-family:'JetBrains Mono',monospace;font-size:11px;width:125px}}
.btn{{background:var(--accent);color:#fff;border:none;padding:4px 12px;border-radius:5px;font-size:11px;cursor:pointer;font-family:'DM Sans',sans-serif;font-weight:600}}
.btn:hover{{opacity:.9}}
.toggle-group{{display:flex;gap:4px;align-items:center}}
.toggle-btn{{padding:4px 10px;border-radius:5px;font-size:11px;font-weight:600;cursor:pointer;border:1px solid var(--border);background:var(--surface);color:var(--text-mid);font-family:'DM Sans',sans-serif;transition:all .15s}}
.toggle-btn.on{{background:var(--accent);color:#fff;border-color:var(--accent)}}
.chart-card{{background:var(--surface);border:1px solid var(--border-light);border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.04);margin-bottom:14px}}
.chart-header{{padding:8px 14px;border-bottom:1px solid var(--border-light);display:flex;align-items:center;justify-content:space-between}}
.chart-title{{font-size:13px;font-weight:600}}.chart-subtitle{{font-size:10px;color:var(--text-dim);font-family:'JetBrains Mono',monospace}}
.status-msg{{display:flex;align-items:center;justify-content:center;height:200px;color:var(--text-dim);font-size:14px}}
.spinner{{width:18px;height:18px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite;display:inline-block;margin-right:8px}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
.tooltip{{position:fixed;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:10px 14px;font-size:12px;pointer-events:none;z-index:1000;box-shadow:0 8px 24px rgba(0,0,0,.12);display:none;min-width:210px}}
.tt-date{{font-family:'JetBrains Mono',monospace;color:var(--text-dim);margin-bottom:6px;font-size:11px}}
.tt-row{{display:flex;justify-content:space-between;gap:16px;padding:2px 0}}
.tt-val{{font-family:'JetBrains Mono',monospace;font-weight:600}}
.tt-sect{{font-size:10px;color:var(--text-dim);margin-top:4px;padding-top:4px;border-top:1px solid var(--border-light);font-weight:600;text-transform:uppercase;letter-spacing:.05em}}
.yr-table{{width:100%;border-collapse:collapse;font-size:11px;font-family:'JetBrains Mono',monospace}}
.yr-table th{{background:var(--surface2);padding:6px 8px;text-align:right;font-weight:600;font-size:10px;color:var(--text-mid);border-bottom:1px solid var(--border);position:sticky;top:0}}
.yr-table th:first-child{{text-align:left}}
.yr-table td{{padding:5px 8px;text-align:right;border-bottom:1px solid var(--border-light)}}
.yr-table td:first-child{{text-align:left;font-weight:600;color:var(--text)}}
.tbl-section{{font-size:12px;font-weight:700;padding:10px 0 4px;color:var(--text);border-bottom:2px solid var(--border)}}
.pct-pos{{color:#16a34a;font-size:10px}}.pct-neg{{color:#dc2626;font-size:10px}}
</style>
</head>
<body>
<div class="app-header">
  <h1><span>EQV</span> Production Dashboard <span style="font-size:12px;color:var(--text-dim);font-weight:400">(2+10 Realistic Forecast)</span></h1>
  <div class="header-legend">
    <div class="legend-item"><div class="legend-dot" style="background:var(--oil)"></div>Oil Actual</div>
    <div class="legend-item"><div class="legend-dash" style="border-color:var(--oil)"></div>Oil Forecast</div>
    <div class="legend-item"><div class="legend-dot" style="background:var(--gas)"></div>Gas Actual</div>
    <div class="legend-item"><div class="legend-dash" style="border-color:var(--gas)"></div>Gas Forecast</div>
    <div class="legend-item"><div class="legend-dot" style="background:var(--water)"></div>Water Actual</div>
  </div>
</div>
<div class="main-layout">
  <div class="sidebar" id="sidebar"><div class="status-msg"><span class="spinner"></span></div></div>
  <div class="content-area" id="ca"><div class="status-msg">Select from sidebar</div></div>
</div>
<div class="tooltip" id="tooltip"><div class="tt-date" id="ttDate"></div><div id="ttC"></div></div>
<script>
const CD="{compressed_data}";
async function dec(b){{const bi=Uint8Array.from(atob(b),c=>c.charCodeAt(0));const d=new DecompressionStream('gzip');const w=d.writable.getWriter();w.write(bi);w.close();const r=d.readable.getReader();const ch=[];while(true){{const{{done,value}}=await r.read();if(done)break;ch.push(value)}}const t=new TextDecoder();return ch.map(c=>t.decode(c,{{stream:true}})).join('')+t.decode()}}
dec(CD).then(s=>{{window.D=JSON.parse(s);init()}});

function init(){{
const DPR=window.devicePixelRatio||1,$=id=>document.getElementById(id);
const sidebar=$('sidebar'),ca=$('ca'),tooltip=$('tooltip'),ttDate=$('ttDate'),ttC=$('ttC');
const hier=D.hierarchy;
let sel={{level:'presidio',group:'EQV Resources'}};
let dMin=D.dateRange[0],dMax=D.dateRange[1];
let showA=true,showF=true,isDaily=false;
const C={{o:'#16a34a',g:'#dc2626',w:'#2563eb'}};

function daysInMonth(ym){{const[y,m]=ym.split('-').map(Number);return new Date(y,m,0).getDate()}}

function esc(s){{return s.replace(/[^a-zA-Z0-9]/g,'_')}}
function clearAc(){{sidebar.querySelectorAll('.active').forEach(e=>e.classList.remove('active'))}}

function buildSidebar(){{
  let h='<div class="nav-section-label">Company</div>';
  h+='<button class="nb d0 active" id="np"><div class="ndot" style="background:var(--accent)"></div>EQV Resources (All)</button>';
  h+='<div class="nav-section-label">Foremen</div>';
  Object.keys(hier).sort().forEach(fm=>{{
    const fid=esc(fm),techs=Object.keys(hier[fm]).sort();
    h+='<button class="nb d0" id="nf-'+fid+'"><div class="ndot" style="background:#8a857d"></div>'+fm;
    if(techs.length)h+='<span class="chv">&#9654;</span>';
    h+='</button>';
    if(techs.length){{
      h+='<div class="coll" id="cf-'+fid+'">';
      techs.forEach(tc=>{{
        const tid=esc(fm+'_'+tc),pumpers=Object.keys(hier[fm][tc]).sort();
        h+='<button class="nb d1" id="nt-'+tid+'"><div class="ndot" style="background:#b8b3aa"></div>'+tc;
        if(pumpers.length)h+='<span class="chv">&#9654;</span>';
        h+='</button>';
        if(pumpers.length){{
          h+='<div class="coll" id="ct-'+tid+'">';
          pumpers.forEach(pm=>{{
            const pid=esc(fm+'_'+tc+'_'+pm),wells=hier[fm][tc][pm]||[];
            h+='<button class="nb d2" id="nm-'+pid+'">'+pm;
            if(wells.length)h+='<span class="chv">&#9654;</span>';
            h+='</button>';
            if(wells.length){{
              h+='<div class="coll" id="cm-'+pid+'">';
              wells.forEach(w=>{{h+='<button class="nb d3" id="nw-'+esc(w[0])+'">'+w[1]+'</button>'}});
              h+='</div>';}}
          }});h+='</div>';}}
      }});h+='</div>';}}
  }});
  sidebar.innerHTML=h;attachNav();
}}

function tog(btn,cont){{if(!cont)return;const p=cont.parentElement;if(p)p.querySelectorAll(':scope > .coll.open').forEach(c=>{{if(c!==cont){{c.classList.remove('open');if(c.previousElementSibling)c.previousElementSibling.classList.remove('expanded')}}}});const o=cont.classList.contains('open');if(!o){{cont.classList.add('open');btn.classList.add('expanded')}}else{{cont.classList.remove('open');btn.classList.remove('expanded')}}}}

function attachNav(){{
  $('np').onclick=()=>{{clearAc();$('np').classList.add('active');sel={{level:'presidio',group:'EQV Resources'}};render()}};
  Object.keys(hier).sort().forEach(fm=>{{
    const fid=esc(fm),fb=$('nf-'+fid),fc=$('cf-'+fid);
    fb.onclick=()=>{{clearAc();fb.classList.add('active');sel={{level:'foreman',group:fm,foreman:fm}};render();tog(fb,fc)}};
    Object.keys(hier[fm]).sort().forEach(tc=>{{
      const tid=esc(fm+'_'+tc),tb=$('nt-'+tid),tc2=$('ct-'+tid);
      tb.onclick=e=>{{e.stopPropagation();clearAc();tb.classList.add('active');fb.classList.add('expanded');if(fc)fc.classList.add('open');sel={{level:'tech',group:tc,foreman:fm,tech:tc}};render();tog(tb,tc2)}};
      Object.keys(hier[fm][tc]).sort().forEach(pm=>{{
        const pid=esc(fm+'_'+tc+'_'+pm),pb=$('nm-'+pid),pc=$('cm-'+pid);
        pb.onclick=e=>{{e.stopPropagation();clearAc();pb.classList.add('active');fb.classList.add('expanded');if(fc)fc.classList.add('open');tb.classList.add('expanded');if(tc2)tc2.classList.add('open');sel={{level:'pumper',group:pm,foreman:fm,tech:tc,pumper:pm}};render();tog(pb,pc)}};
        (hier[fm][tc][pm]||[]).forEach(w=>{{const wb=$('nw-'+esc(w[0]));if(!wb)return;
          wb.onclick=e=>{{e.stopPropagation();clearAc();wb.classList.add('active');fb.classList.add('expanded');if(fc)fc.classList.add('open');tb.classList.add('expanded');if(tc2)tc2.classList.add('open');pb.classList.add('expanded');if(pc)pc.classList.add('open');sel={{level:'well',group:w[0],foreman:fm,tech:tc,pumper:pm,well:w[0],wellName:w[1]}};render()}};
        }});
      }});
    }});
  }});
}}

// Chart
function logTicks(mn,mx){{if(mn<=0)mn=1;if(mx<=0)mx=10;const lo=Math.floor(Math.log10(mn)),hi=Math.ceil(Math.log10(mx)),t=[];for(let p=lo;p<=hi;p++)t.push(Math.pow(10,p));return t}}
function fmt(n){{return Math.round(n).toLocaleString('en-US')}}
function fmtF(n){{return Math.round(n).toLocaleString('en-US')}}
function filt(s){{return s.filter(e=>e[0]>=dMin&&e[0]<=dMax)}}

function drawChart(cvs,raw,scales){{
  const data=filt(raw);
  const ctx=cvs.getContext('2d'),rect=cvs.getBoundingClientRect();
  cvs.width=rect.width*DPR;cvs.height=rect.height*DPR;ctx.scale(DPR,DPR);
  const w=rect.width,h=rect.height,ml=65,mr=10,mt=10,mb=50,pw=w-ml-mr,ph=h-mt-mb;
  ctx.fillStyle='#fff';ctx.fillRect(0,0,w,h);

  const mos=[];
  {{let[y,m]=dMin.split('-').map(Number);const[ey,em]=dMax.split('-').map(Number);
   while(y<ey||(y===ey&&m<=em)){{mos.push(y+'-'+(m<10?'0':'')+m);m++;if(m>12){{m=1;y++}}}}}}
  if(!mos.length){{ctx.fillStyle='#8a857d';ctx.font='14px DM Sans';ctx.textAlign='center';ctx.fillText('Invalid date range',w/2,h/2);return}}

  const byM={{}};data.forEach(e=>{{byM[e[0]]=e}});
  const fd=mos.map(m=>{{
    const e=byM[m]||[m,0,0,0,0,0,0,0,0,0];
    if(!isDaily)return e;
    const d=daysInMonth(m),fc=30.4;
    return[e[0],e[1]/d,e[2]/d,e[3]/d,e[4]/fc,e[5]/fc,e[6]/fc,e[7]/fc,e[8]/fc,e[9]/fc];
  }});

  const sFactor=isDaily?30.4:1;
  const sMin=Math.max(1,scales.min/sFactor),sMax=Math.max(10,scales.max/sFactor);
  const loS=Math.floor(Math.log10(sMin))-(isDaily?1:0),hiS=Math.ceil(Math.log10(sMax*1.2));
  const yV=v=>v<=0?mt+ph+10:mt+ph*(1-(Math.log10(v)-loS)/(hiS-loS));
  const xP=i=>ml+(i/Math.max(1,mos.length-1))*pw;

  ctx.strokeStyle='#eae5dc';ctx.lineWidth=.5;
  const tks=logTicks(Math.pow(10,loS),Math.pow(10,hiS));
  tks.forEach(v=>{{const y=yV(v);if(y>=mt&&y<=mt+ph){{ctx.beginPath();ctx.moveTo(ml,y);ctx.lineTo(ml+pw,y);ctx.stroke()}}}});
  const li=Math.max(1,Math.floor(mos.length/18));
  ctx.font='10px JetBrains Mono';
  for(let i=0;i<mos.length;i+=li){{const x=xP(i);ctx.strokeStyle='#eae5dc';ctx.lineWidth=.5;ctx.beginPath();ctx.moveTo(x,mt);ctx.lineTo(x,mt+ph);ctx.stroke();ctx.save();ctx.translate(x,mt+ph+8);ctx.rotate(-Math.PI/4);ctx.textAlign='right';ctx.fillStyle='#8a857d';ctx.fillText(mos[i],0,0);ctx.restore()}}

  ctx.textAlign='right';ctx.fillStyle='#5a564f';ctx.font='10px JetBrains Mono';
  tks.forEach(v=>{{const y=yV(v);if(y>=mt&&y<=mt+ph)ctx.fillText(fmt(v),ml-5,y+3)}});

  ctx.strokeStyle='#d9d2c7';ctx.lineWidth=1;
  ctx.beginPath();ctx.moveTo(ml,mt);ctx.lineTo(ml,mt+ph);ctx.stroke();
  ctx.beginPath();ctx.moveTo(ml,mt+ph);ctx.lineTo(ml+pw,mt+ph);ctx.stroke();

  ctx.save();ctx.beginPath();ctx.rect(ml,mt,pw,ph);ctx.clip();
  function dL(yFn,idx,color,dash){{
    ctx.strokeStyle=color;ctx.lineWidth=dash?1.3:1.8;ctx.setLineDash(dash||[]);
    ctx.beginPath();let s=false;
    for(let i=0;i<fd.length;i++){{const v=fd[i][idx]||0;if(v>0){{const x=xP(i),y=yFn(v);if(!s){{ctx.moveTo(x,y);s=true}}else ctx.lineTo(x,y)}}else s=false}}
    ctx.stroke();ctx.setLineDash([]);}}

  if(showA){{dL(yV,1,C.o,null);dL(yV,2,C.g,null);dL(yV,3,C.w,null)}}
  if(showF){{dL(yV,4,C.o,[6,3]);dL(yV,5,C.g,[6,3]);dL(yV,6,C.w,[6,3])}}
  ctx.restore();
  cvs._m={{ml,pw,mt,ph,mos,fd,xP}};
}}

// Yearly table
function yearlyTable(data){{
  const f=filt(data);
  const yrs={{}};
  f.forEach(e=>{{
    const yr=e[0].substring(0,4);
    if(!yrs[yr])yrs[yr]={{o:0,g:0,w:0,fo:0,fg:0,fw:0}};
    const y=yrs[yr];
    y.o+=e[1]||0;y.g+=e[2]||0;y.w+=e[3]||0;
    y.fo+=e[4]||0;y.fg+=e[5]||0;y.fw+=e[6]||0;
  }});
  const years=Object.keys(yrs).filter(y=>y>='2020'&&y<='2035').sort();
  if(!years.length)return'';

  function pctVal(cur,prev){{if(!prev||prev===0)return null;return(cur-prev)/prev*100}}
  function pctHtml(p){{if(p===null||p===undefined)return'';const cls=p>=0?'pct-pos':'pct-neg';return'<span class="'+cls+'">'+(p>=0?'+':'')+p.toFixed(1)+'%</span>'}}

  function buildSection(label,aKey,fKey){{
    let h='<div class="tbl-section">'+label+'</div>';
    h+='<table class="yr-table"><thead><tr><th></th>';
    years.forEach(yr=>{{h+='<th>'+yr+'</th>'}});
    if(showA)h+='<th>Avg Decline</th>';
    h+='</tr></thead><tbody>';

    if(showA){{
      h+='<tr><td>Actual</td>';
      years.forEach(yr=>{{const v=yrs[yr][aKey];h+='<td>'+(v?fmtF(v):'')+'</td>'}});
      h+='<td></td></tr>';
      const aPcts=[];
      h+='<tr><td>% YOY</td>';
      years.forEach((yr,i)=>{{
        if(i===0){{h+='<td></td>';return}}
        const cur=yrs[yr][aKey],prev=yrs[years[i-1]][aKey];
        if(yr==='2026'){{h+='<td></td>';return}}
        const p=pctVal(cur,prev);
        if(p!==null)aPcts.push(p);
        h+='<td>'+pctHtml(p)+'</td>';
      }});
      const avg=aPcts.length?aPcts.reduce((a,b)=>a+b,0)/aPcts.length:null;
      h+='<td>'+pctHtml(avg)+'</td>';
      h+='</tr>';
    }}

    if(showF){{
      h+='<tr><td>Forecast</td>';
      years.forEach(yr=>{{const v=yrs[yr][fKey];h+='<td>'+(v?fmtF(v):'')+'</td>'}});
      if(showA)h+='<td></td>';
      h+='</tr>';
      h+='<tr><td>% YOY</td>';
      years.forEach((yr,i)=>{{
        if(i===0){{h+='<td></td>';return}}
        const cur=yrs[yr][fKey];
        let prev;
        if(yr==='2026'&&yrs['2025']){{prev=yrs['2025'][aKey]}}
        else{{prev=yrs[years[i-1]][fKey]}}
        const p=pctVal(cur,prev);
        h+='<td>'+pctHtml(p)+'</td>';
      }});
      if(showA)h+='<td></td>';
      h+='</tr>';
    }}

    h+='</tbody></table>';
    return h;
  }}

  let h='';
  h+=buildSection('Oil (bbl)','o','fo');
  h+=buildSection('Gas Sales (mcf)','g','fg');
  h+=buildSection('Water (bbl)','w','fw');
  return h;
}}

function render(){{
  let data,scales,title;
  if(sel.level==='well'){{
    const pw=D.pumperWells[sel.pumper]||{{}};data=pw[sel.well]||[];scales=D.wellScales;title=sel.wellName||sel.well;
  }}else{{
    const ld=D[sel.level];data=ld.groups[sel.group]||[];scales=ld.scales;title=sel.group;
  }}
  let bc=sel.level.charAt(0).toUpperCase()+sel.level.slice(1);
  if(sel.foreman)bc=sel.foreman;if(sel.tech)bc+=' \\u2192 '+sel.tech;if(sel.pumper)bc+=' \\u2192 '+sel.pumper;if(sel.wellName)bc+=' \\u2192 '+sel.wellName;

  let h='<div class="content-header"><div><div class="content-title">'+title+'</div><div class="breadcrumb">'+bc+'</div></div>';
  h+='<div class="controls-row">';
  h+='<div class="toggle-group"><button class="toggle-btn'+(showA?' on':'')+'" id="tA">Actual</button><button class="toggle-btn'+(showF?' on':'')+'" id="tF">Forecast</button></div>';
  h+='<div class="toggle-group"><button class="toggle-btn'+(!isDaily?' on':'')+'" id="tM">Monthly</button><button class="toggle-btn'+(isDaily?' on':'')+'" id="tD">Daily</button></div>';
  h+='<div class="date-controls"><label>From</label><input type="month" id="ds" value="'+dMin+'"><label>To</label><input type="month" id="de" value="'+dMax+'"><button class="btn" id="ab">Apply</button></div>';
  h+='</div></div>';

  h+='<div class="chart-card"><div class="chart-header"><div class="chart-title">'+title+'</div>';
  h+='<div class="chart-subtitle">Semi-log &middot; '+(isDaily?'Daily Rate':'Monthly Volume')+' &middot; Solid=Actual &middot; Dashed=Forecast</div></div>';
  h+='<div class="chart-body"><canvas id="mc" style="width:100%;height:600px"></canvas></div></div>';

  h+='<div class="chart-card"><div class="chart-header"><div class="chart-title">Annual Comparison (2020\\u20132035)</div></div>';
  h+='<div style="padding:12px 16px">'+yearlyTable(data)+'</div></div>';

  ca.innerHTML=h;
  $('ab').onclick=()=>{{dMin=$('ds').value;dMax=$('de').value;render()}};
  $('tA').onclick=()=>{{showA=!showA;render()}};
  $('tF').onclick=()=>{{showF=!showF;render()}};
  $('tM').onclick=()=>{{isDaily=false;render()}};
  $('tD').onclick=()=>{{isDaily=true;render()}};

  const cvs=$('mc');
  requestAnimationFrame(()=>{{
    drawChart(cvs,data,scales);
    cvs.onmousemove=e=>{{
      const m=cvs._m;if(!m)return;const r=cvs.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;
      if(mx<m.ml||mx>m.ml+m.pw||my<m.mt||my>m.mt+m.ph){{tooltip.style.display='none';return}}
      const idx=Math.round((mx-m.ml)/m.pw*(m.fd.length-1));
      if(idx<0||idx>=m.fd.length){{tooltip.style.display='none';return}}
      const d=m.fd[idx];ttDate.textContent=d[0];let tc='';
      const ou=isDaily?'bbl/d':'bbl',gu=isDaily?'mcf/d':'mcf',wu=isDaily?'bbl/d':'bbl';
      const tfmt=v=>isDaily?(v>=1e3?(v/1e3).toFixed(1)+'K':v.toFixed(1)):fmtF(v);
      if(showA&&(d[1]>0||d[2]>0||d[3]>0)){{tc+='<div class="tt-sect">Actual</div>';
        if(d[1])tc+='<div class="tt-row"><span style="color:var(--oil)">Oil</span><span class="tt-val">'+tfmt(d[1])+' '+ou+'</span></div>';
        if(d[2])tc+='<div class="tt-row"><span style="color:var(--gas)">Gas</span><span class="tt-val">'+tfmt(d[2])+' '+gu+'</span></div>';
        if(d[3])tc+='<div class="tt-row"><span style="color:var(--water)">Water</span><span class="tt-val">'+tfmt(d[3])+' '+wu+'</span></div>'}}
      if(showF&&(d[4]>0||d[5]>0||d[6]>0)){{tc+='<div class="tt-sect">Forecast</div>';
        if(d[4])tc+='<div class="tt-row"><span style="color:var(--oil)">Oil</span><span class="tt-val">'+tfmt(d[4])+' '+ou+'</span></div>';
        if(d[5])tc+='<div class="tt-row"><span style="color:var(--gas)">Gas</span><span class="tt-val">'+tfmt(d[5])+' '+gu+'</span></div>';
        if(d[6])tc+='<div class="tt-row"><span style="color:var(--water)">Water</span><span class="tt-val">'+tfmt(d[6])+' '+wu+'</span></div>'}}
      ttC.innerHTML=tc;tooltip.style.display='block';
      tooltip.style.left=(e.clientX+16)+'px';tooltip.style.top=(e.clientY-10)+'px';
      const tr=tooltip.getBoundingClientRect();
      if(tr.right>window.innerWidth)tooltip.style.left=(e.clientX-tr.width-16)+'px';
      if(tr.bottom>window.innerHeight)tooltip.style.top=(window.innerHeight-tr.height-10)+'px';
    }};
    cvs.onmouseleave=()=>{{tooltip.style.display='none'}};
  }});
}}
let rt;window.addEventListener('resize',()=>{{clearTimeout(rt);rt=setTimeout(render,200)}});
buildSidebar();render();
}}
</script>
</body>
</html>"""
    return html


def main():
    print("Building EQV Production Dashboard...")
    compressed_data = build_data()
    html = build_html(compressed_data)

    output_file = "eqv_production_dashboard.html"
    with open(output_file, "w") as f:
        f.write(html)

    print(f"\nDashboard written to: {output_file}")
    print(f"File size: {len(html):,} bytes")


if __name__ == "__main__":
    main()
