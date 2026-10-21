#!/bin/sh
set -eu

mode=user
operation=install
with_languagetool=0
for arg in "$@"; do
    case "$arg" in
        --user) mode=user ;;
        --system) mode=system ;;
        --uninstall) operation=uninstall ;;
        --with-languagetool) with_languagetool=1 ;;
        --help)
            printf '%s\n' 'Usage: ./install.sh [--user|--system] [--uninstall] [--with-languagetool]'
            printf '%s\n' 'Default installation is for the current user.'
            printf '%s\n' '--with-languagetool downloads the local LanguageTool server (~252 MB).'
            exit 0
            ;;
        *) printf 'Unknown option: %s\n' "$arg" >&2; exit 2 ;;
    esac
done

if [ "$mode" = system ] && [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'System installation needs administrator access.' 'Run: sudo ./install.sh --system' >&2
    exit 1
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "$mode" = system ]; then
    app_dir=/usr/local/lib/wordpro
    launcher=/usr/local/bin/wordpro
    desktop_dir=/usr/share/applications
else
    : "${HOME:?HOME must be set for a per-user installation}"
    app_dir=$HOME/.local/lib/wordpro
    launcher=$HOME/.local/bin/wordpro
    desktop_dir=${XDG_DATA_HOME:-$HOME/.local/share}/applications
fi
desktop_file=$desktop_dir/wordpro.desktop

if [ "$operation" = uninstall ]; then
    rm -rf "$app_dir"
    rm -f "$launcher" "$desktop_file"
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$desktop_dir" >/dev/null 2>&1 || true
    fi
    printf 'WordPro removed (%s installation).\n' "$mode"
    exit 0
fi

if [ ! -f "$script_dir/wordpro.py" ] || [ ! -f "$script_dir/wordpro-launch.sh" ]; then
    printf '%s\n' 'wordpro.py and wordpro-launch.sh must be in the same folder as install.sh.' >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import curses' >/dev/null 2>&1; then
    printf '%s\n' 'WordPro needs Python 3 with curses support.' >&2
    exit 1
fi

lt_temp=
if [ "$with_languagetool" -eq 1 ] && [ ! -f "$app_dir/languagetool/languagetool-server.jar" ]; then
    if ! command -v java >/dev/null 2>&1; then
        printf '%s\n' 'LanguageTool needs Java 17 or newer. Install Java, then rerun with --with-languagetool.' >&2
        exit 1
    fi
    java_version=$(java -version 2>&1 | sed -n '1s/.*version "\([^"]*\)".*/\1/p')
    java_major=${java_version%%.*}
    case "$java_major" in
        ''|*[!0-9]*) printf '%s\n' 'Could not determine Java version; LanguageTool needs Java 17 or newer.' >&2; exit 1 ;;
        1) java_major=$(printf '%s' "$java_version" | cut -d. -f2) ;;
    esac
    if [ "$java_major" -lt 17 ]; then
        printf 'LanguageTool needs Java 17 or newer; found Java %s.\n' "$java_version" >&2
        exit 1
    fi
    if ! command -v unzip >/dev/null 2>&1 || { ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; }; then
        printf '%s\n' 'LanguageTool setup needs unzip and either curl or wget.' >&2
        exit 1
    fi
    lt_temp=$(mktemp -d "${TMPDIR:-/tmp}/wordpro-languagetool.XXXXXX")
    trap 'if [ -n "$lt_temp" ]; then rm -rf "$lt_temp"; fi' EXIT HUP INT TERM
    printf '%s\n' 'Downloading LanguageTool standalone (about 252 MB)...'
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 2 'https://languagetool.org/download/LanguageTool-stable.zip' -o "$lt_temp/languagetool.zip"
    else
        wget -O "$lt_temp/languagetool.zip" 'https://languagetool.org/download/LanguageTool-stable.zip'
    fi
    mkdir "$lt_temp/unpacked"
    unzip -q "$lt_temp/languagetool.zip" -d "$lt_temp/unpacked"
    lt_jar=$(find "$lt_temp/unpacked" -type f -name languagetool-server.jar -print -quit)
    if [ -z "$lt_jar" ]; then
        printf '%s\n' 'The LanguageTool archive did not contain languagetool-server.jar.' >&2
        exit 1
    fi
    lt_source=$(dirname -- "$lt_jar")
fi

install -d "$app_dir" "$(dirname -- "$launcher")" "$desktop_dir"
install -m 755 "$script_dir/wordpro.py" "$app_dir/wordpro.py"
install -m 755 "$script_dir/wordpro-launch.sh" "$app_dir/wordpro-launch.sh"
if [ -d "$script_dir/dictionaries" ]; then
    install -d "$app_dir/dictionaries"
    for dictionary_file in "$script_dir"/dictionaries/*; do
        [ -f "$dictionary_file" ] && install -m 644 "$dictionary_file" "$app_dir/dictionaries/"
    done
fi
if [ -n "${lt_source:-}" ]; then
    install -d "$app_dir/languagetool"
    cp -R "$lt_source"/. "$app_dir/languagetool/"
fi
if [ "$mode" = system ]; then
    cat > "$launcher" <<'EOF'
#!/bin/sh
exec /usr/local/lib/wordpro/wordpro-launch.sh "$@"
EOF
else
    cat > "$launcher" <<'EOF'
#!/bin/sh
exec "$HOME/.local/lib/wordpro/wordpro-launch.sh" "$@"
EOF
fi
chmod 755 "$launcher"

cat > "$desktop_file" <<EOF
[Desktop Entry]
Type=Application
Name=WordPro
GenericName=Terminal Word Processor
Comment=Open WordPro, a keyboard-driven terminal word processor
Exec="$launcher"
TryExec=$launcher
Icon=accessories-text-editor
Terminal=true
Categories=Office;WordProcessor;TextEditor;
StartupNotify=false
EOF

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$desktop_dir" >/dev/null 2>&1 || true
fi
printf 'WordPro installed (%s). Launch it from the applications menu or run: %s\n' "$mode" "$launcher"
if [ "$with_languagetool" -eq 1 ]; then
    printf '%s\n' 'The local LanguageTool server will start automatically when WordPro launches.'
fi
