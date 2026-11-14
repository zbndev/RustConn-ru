#!/bin/bash
# Install RustConnRu desktop entry, icon, and locale files

set -e

if ! command -v msgfmt >/dev/null 2>&1; then
    echo "Error: msgfmt not found. Install gettext first." >&2
    exit 1
fi

# Determine install prefix
PREFIX="${PREFIX:-$HOME/.local}"

# Install binaries (built by `cargo build --release -p rustconn -p rustconn-cli`)
BIN_DIR="$PREFIX/bin"
installed_bin=false
for bin in rustconn rustconn-cli; do
    if [ -f "target/release/$bin" ]; then
        install -Dm755 "target/release/$bin" "$BIN_DIR/$bin"
        echo "Installed binary: $bin -> $BIN_DIR/$bin"
        installed_bin=true
    fi
done
if [ "$installed_bin" = false ]; then
    echo "Note: no release binaries found in target/release/."
    echo "      Build them first: cargo build --release -p rustconn -p rustconn-cli"
fi

# Install icon
ICON_DIR="$PREFIX/share/icons/hicolor/scalable/apps"
mkdir -p "$ICON_DIR"
cp rustconn/assets/icons/hicolor/scalable/apps/io.github.zbndev.RustConnRu.svg "$ICON_DIR/"

# Install desktop file
DESKTOP_DIR="$PREFIX/share/applications"
mkdir -p "$DESKTOP_DIR"
msgfmt --desktop --template rustconn/assets/io.github.zbndev.RustConnRu.desktop \
    -d po -o "$DESKTOP_DIR/io.github.zbndev.RustConnRu.desktop"

# Install MIME type definition for .rdp files
MIME_DIR="$PREFIX/share/mime/packages"
mkdir -p "$MIME_DIR"
cp rustconn/assets/io.github.zbndev.RustConnRu-rdp.xml "$MIME_DIR/"
cp rustconn/assets/io.github.zbndev.RustConnRu-vv.xml "$MIME_DIR/"

# Compile and install locale files
if [ -d "po" ]; then
    for po_file in po/*.po; do
        [ -f "$po_file" ] || continue
        lang=$(basename "$po_file" .po)
        LOCALE_DIR="$PREFIX/share/locale/$lang/LC_MESSAGES"
        mkdir -p "$LOCALE_DIR"
        msgfmt -o "$LOCALE_DIR/rustconn.mo" "$po_file"
        echo "Installed locale: $lang"
    done
fi

# Update icon cache
if command -v gtk-update-icon-cache &> /dev/null; then
    gtk-update-icon-cache -f -t "$PREFIX/share/icons/hicolor" 2>/dev/null || true
fi

# Update MIME database
if command -v update-mime-database &> /dev/null; then
    update-mime-database "$PREFIX/share/mime" 2>/dev/null || true
fi

echo "Desktop entry, icon, and locales installed to $PREFIX"
if [ "$installed_bin" = true ]; then
    echo "Binaries installed to $BIN_DIR — ensure it is on your PATH."
fi
echo "You may need to log out and log back in for changes to take effect."
