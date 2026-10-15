# Upande Spatial for QGIS

Work on Upande ERP spatial data in QGIS: load layers, analyse and edit them
with any QGIS tool, and save straight back to ERP.

Targets QGIS 3.34+ and QGIS 4 (Qt6). Talks to ERP over its normal web API,
so it works with a local bench and Frappe Cloud sites alike.

## Install

    ./install.sh        # links the plugin into your QGIS profile

Restart QGIS, then enable **Upande Spatial** under *Plugins → Manage and
Install Plugins → Installed*. To share it, zip the `upande_spatial_qgis`
folder and install with *Install from ZIP*.

## Logging in

*Upande Spatial → Log in to ERP…* with your ERP site, email and password -
the same as the ERP web login, including the verification code if your
account has two-factor authentication. Your password is only sent to ERP to
sign in and is never saved; the plugin keeps the session in memory, and
logging out (or closing QGIS) ends it. You only see and change what your ERP
user is allowed to. If the session expires while you work, the plugin asks
you to log in again and then sends any edits it was holding.

## Using it

- **Browse / load** - the Upande Spatial panel lists every layer by geometry
  type (filter by farm). Double-click to load.
- **Edit live** - toggle editing on an ERP layer, change anything (geometry,
  attributes, add, delete), then *Save Layer Edits*: the changes go to ERP in
  one go. `_`-prefixed attributes are ERP's own fields; `_name`, area, length
  and the modified stamp are filled in by ERP. New features in a layer whose
  features all share an owner (e.g. one farm's pipes) inherit that owner.
- **Conflicts** - if someone changed a feature in ERP after you loaded it,
  your save of that feature is refused and kept in QGIS. *Reload active ERP
  layer* to see ERP's version, or select it and *Force-push*.
- **Publish layer to ERP** - send any layer (an analysis result, a shapefile,
  a buffer...) to ERP as new spatial features, optionally attached to an
  owner record (Farm, Warehouse...) and role. Reprojected to WGS84.
- **Check out / Release** - lock selected features so nobody else edits them.
- **EPANET** - load a network's latest simulation results (pressure, head,
  quality, demand on nodes; flow, velocity, headloss on links) as styled
  read-only layers, or download its `.inp` model.

Deleting features needs ERP's delete permission on Spatial Feature (System
Manager by default).

## Server side

The plugin uses `upande_spatial.api.qgis` (catalog, get_layer,
apply_changes, epanet_results) plus `spatial.checkout_feature` /
`release_feature` and `epanet.export_inp` from the Upande Spatial app - the
site needs that app at a version that includes `api/qgis.py`.
