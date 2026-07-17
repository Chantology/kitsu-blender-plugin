#!/bin/bash
# Installs the Kitsu Blender Plugin into Blender's user "scripts/addons"
# folder(s) on macOS.
#
# Usage: ./install_mac.command [custom scripts folder]
#   Double-click to run with no arguments, auto-detecting every
#   "~/Library/Application Support/Blender/<major>.<minor>" folder and
#   installing into all of them. If you use a non-standard Blender config
#   directory, pass that version folder as an argument instead.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$SCRIPT_DIR/kitsu_blender_plugin"
BASE="$HOME/Library/Application Support/Blender"

if [ ! -f "$SOURCE/__init__.py" ]; then
    echo "ERROR: $SOURCE/__init__.py not found."
    echo "Run this script from the repository root, or check your checkout."
    read -r -p "Press Enter to close this window..." _
    exit 1
fi

TARGETS=()
if [ "${1:-}" != "" ]; then
    TARGETS=("$1")
elif [ -d "$BASE" ]; then
    for d in "$BASE"/*/; do
        [ -d "$d" ] && TARGETS+=("${d%/}")
    done
fi

if [ "${#TARGETS[@]}" -eq 0 ]; then
    echo "ERROR: No \"$BASE/<version>\" folder found."
    echo "Launch Blender at least once to create it, then re-run this script -"
    echo "or pass your Blender version config folder as an argument."
    read -r -p "Press Enter to close this window..." _
    exit 1
fi

for version_dir in "${TARGETS[@]}"; do
    TARGET="$version_dir/scripts/addons"
    echo "Installing Kitsu Blender Plugin"
    echo "  From: $SOURCE"
    echo "  To:   $TARGET/kitsu_blender_plugin"
    echo

    mkdir -p "$TARGET"

    # Remove any previous install of this plugin's own folder first, for a
    # clean, idempotent install that never leaves stale files from an older
    # version behind.
    rm -rf "$TARGET/kitsu_blender_plugin"
    cp -R "$SOURCE" "$TARGET/kitsu_blender_plugin"
done

echo "Done. Restart Blender, then enable \"Kitsu\" in"
echo "Edit > Preferences > Add-ons (search \"Kitsu\"). The panel appears in"
echo "the 3D Viewport's sidebar (press N), under the \"Kitsu\" tab."
read -r -p "Press Enter to close this window..." _
