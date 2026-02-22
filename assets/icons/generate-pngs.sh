#!/bin/bash
# Generate PNG emblems from SVG sources
# Requires: rsvg-convert (from librsvg2-bin package)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EMBLEMS_DIR="$SCRIPT_DIR/emblems"

# Check for rsvg-convert
if ! command -v rsvg-convert &> /dev/null; then
    echo "Error: rsvg-convert not found."
    echo "Install with: sudo apt install librsvg2-bin"
    exit 1
fi

SIZES="16 22 24 32 48"
ICONS="emblem-proton-synced emblem-proton-syncing emblem-proton-pending emblem-proton-error"

for icon in $ICONS; do
    svg="$EMBLEMS_DIR/scalable/${icon}.svg"
    if [ ! -f "$svg" ]; then
        echo "Warning: $svg not found, skipping"
        continue
    fi

    for size in $SIZES; do
        dir="$EMBLEMS_DIR/${size}x${size}"
        mkdir -p "$dir"
        png="$dir/${icon}.png"
        echo "Generating $png..."
        rsvg-convert -w "$size" -h "$size" "$svg" -o "$png"
    done
done

echo "Done! PNG emblems generated."
