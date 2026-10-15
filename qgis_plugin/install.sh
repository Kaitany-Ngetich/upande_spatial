#!/usr/bin/env bash
# Links the Upande Spatial plugin into your QGIS profile (default profile),
# so edits in this repo show up after restarting QGIS / Plugin Reloader.
# Then enable it in QGIS: Plugins > Manage and Install Plugins > Installed.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)/upande_spatial_qgis"
for base in "$HOME/.local/share/QGIS/QGIS4" "$HOME/.local/share/QGIS/QGIS3"; do
  if [ -d "$base/profiles/default" ]; then
    mkdir -p "$base/profiles/default/python/plugins"
    ln -sfn "$SRC" "$base/profiles/default/python/plugins/upande_spatial_qgis"
    echo "Linked into $base/profiles/default/python/plugins/"
    exit 0
  fi
done
echo "No QGIS profile found - start QGIS once first." >&2; exit 1
