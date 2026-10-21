#!/bin/sh
# Start the optional bundled local LanguageTool server before WordPro.
set -u

app_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
lt_dir=$app_dir/languagetool
server_jar=$lt_dir/languagetool-server.jar
if [ -f "$server_jar" ] && command -v java >/dev/null 2>&1; then
    python_bin=$(command -v python3 || true)
    if [ -n "$python_bin" ] && ! "$python_bin" -c 'from urllib.request import urlopen; urlopen("http://127.0.0.1:8081/v2/languages", timeout=1)' >/dev/null 2>&1; then
        state_root=${XDG_STATE_HOME:-${HOME:-/tmp}/.local/state}
        state_dir=$state_root/wordpro
        mkdir -p "$state_dir"
        log_file=$state_dir/languagetool.log
        pid_file=$state_dir/languagetool.pid
        if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
            : # The app's server is starting; allow the wait below to finish.
        else
            (cd "$lt_dir" && nohup java -cp "$server_jar" org.languagetool.server.HTTPServer --port 8081 >"$log_file" 2>&1 </dev/null & echo $! >"$pid_file")
        fi
        attempt=0
        while [ "$attempt" -lt 20 ]; do
            if "$python_bin" -c 'from urllib.request import urlopen; urlopen("http://127.0.0.1:8081/v2/languages", timeout=1)' >/dev/null 2>&1; then
                break
            fi
            attempt=$((attempt+1))
            sleep 1
        done
    fi
fi
exec python3 "$app_dir/wordpro.py" "$@"
