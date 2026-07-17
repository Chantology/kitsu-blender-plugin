#!/bin/bash
# Installs the Kitsu Blender Plugin into Blender's user "scripts/addons"
# folder(s) on Linux.
#
# Usage: ./install_linux.sh [custom scripts folder]
#   Blender keys its config folder by major.minor version, so a single
#   install already covers every point release of that line (e.g. 5.2.0
#   and 5.2.1 both use "5.2"), but a different major.minor (e.g. 5.3) needs
#   its own folder, which this script picks up automatically as long as
#   you've launched that Blender version at least once (so it exists). If
#   you use a non-standard config directory (e.g. a portable install,
#   Flatpak, or Snap not auto-detected below), pass that version folder as
#   an argument instead, e.g. ./install_linux.sh ~/.config/blender/5.2
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$SCRIPT_DIR/kitsu_blender_plugin"

if [ ! -f "$SOURCE/__init__.py" ]; then
    echo "ERROR: $SOURCE/__init__.py not found."
    echo "Run this script from the repository root, or check your checkout."
    exit 1
fi

TARGETS=()
if [ "${1:-}" != "" ]; then
    TARGETS=("$1")
else
    for base in \
        "$HOME/.config/blender" \
        "$HOME/.var/app/org.blender.Blender/config/blender" \
        "$HOME/snap/blender/current/.config/blender"
    do
        if [ -d "$base" ]; then
            for d in "$base"/*/; do
                [ -d "$d" ] && TARGETS+=("${d%/}")
            done
        fi
    done
fi

if [ "${#TARGETS[@]}" -eq 0 ]; then
    echo "ERROR: No Blender version config folder found under ~/.config/blender"
    echo "(or the Flatpak/Snap equivalents)."
    echo "Launch Blender at least once to create it, then re-run this script -"
    echo "or pass your Blender version config folder as an argument."
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
