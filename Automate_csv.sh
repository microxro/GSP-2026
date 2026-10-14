#!/usr/bin/env bash
# log2csv.sh - live-convert sound-analyzer terminal output into a CSV for Google Sheets
#
# Follows the log like `tail -F` and appends one CSV row per reading, forever (Ctrl+C to stop).
#
# Usage:
#   ./log2csv.sh session.log results.csv [names.csv]
#
#   session.log  terminal output being written by the analyzer, e.g.
#                your_analyzer_command | tee -a /home/pranav/recordings/session.log
#   results.csv  output file (created with a header if missing, otherwise appended)
#   names.csv    optional ID -> insect map, one per line:  a7,Rice Weevil
#
# Restart-safe: readings already written are remembered in results.csv.seen and skipped.
#
# Columns match the "2026 Pranav GSP" sheet:
# Insect Name, ID of sound, Average (dB), Lowest (dB), Highest (dB),
# Lowest (Hz), Highest (Hz), Dominant (Hz), Spectral Center (Hz), Top Peaks (Hz)

set -u
LOG="${1:?usage: $0 session.log results.csv [names.csv]}"
OUT="${2:?usage: $0 session.log results.csv [names.csv]}"
MAP="${3:-}"
SEEN="$OUT.seen"

[ -f "$LOG" ]  || touch "$LOG"
[ -f "$SEEN" ] || touch "$SEEN"
if [ ! -s "$OUT" ]; then
  echo "Insect Name,ID of sound,Average (dB),Lowest (dB),Highest (dB),Lowest (Hz),Highest (Hz),Dominant (Hz),Spectral Center (Hz),Top Peaks (Hz)" > "$OUT"
fi

# mawk (Pi OS default) buffers piped input; make it read line by line
AWK=awk
if awk -W version 2>&1 | grep -qi mawk; then AWK="awk -W interactive"; fi

echo "Watching $LOG -> appending to $OUT (Ctrl+C to stop)" >&2

# -n +1 reads the whole file first (old readings are skipped via .seen), then keeps following
tail -n +1 -F "$LOG" 2>/dev/null \
| sed -u -e 's/\x1b\[[0-9;]*[A-Za-z]//g' -e 's/\r//g' \
| $AWK -v mapfile="$MAP" -v out="$OUT" -v seenfile="$SEEN" '
BEGIN {
  if (mapfile != "") {
    while ((getline line < mapfile) > 0) {
      n = split(line, p, ",")
      if (n >= 2) names[tolower(p[1])] = p[2]
    }
    close(mapfile)
  }
  while ((getline line < seenfile) > 0) seen[line] = 1
  close(seenfile)
}

function val(   rest) {
  rest = substr($0, index($0, ":") + 1)
  if (match(rest, /-?[0-9]+(\.[0-9]+)?/)) return clean(substr(rest, RSTART, RLENGTH))
  return ""
}
function clean(n) { sub(/\.0$/, "", n); return n }
function reset()  { avg = lodb = hidb = lohz = hihz = dom = cen = peaks = "" }

function flush(   nm, key) {
  if (avg == "") { reset(); return }
  key = (stamp != "") ? stamp : id "|" avg "|" dom
  if (!(key in seen)) {
    nm = (tolower(id) in names) ? names[tolower(id)] : ""
    printf "%s,%s,%s,%s,%s,%s,%s,%s,%s,\"%s\"\n", nm, id, avg, lodb, hidb, lohz, hihz, dom, cen, peaks >> out
    fflush(out)
    print key >> seenfile; fflush(seenfile)
    seen[key] = 1
    printf "+ %s %s  avg %s dB  dom %s Hz\n", (nm == "" ? "?" : nm), id, avg, dom > "/dev/stderr"
  }
  reset(); id = ""; stamp = ""
}

/^[[:space:]]*~[[:space:]]+[^[:space:]]+/ { flush(); id = $2; next }          # "~ a7" = sound ID
/^[[:space:]]*###/ {                                                           # new recording block
  if (avg != "") flush()
  stamp = $0; sub(/^[[:space:]]*###[[:space:]]*/, "", stamp); gsub(/,/, " ", stamp)
  next
}
/Average \(whole/               { avg  = val(); next }
/Lowest[[:space:]]+\(quietest/  { lodb = val(); next }
/Highest[[:space:]]+\(loudest/  { hidb = val(); next }
/Lowest freq/                   { lohz = val(); next }
/Highest freq/                  { hihz = val(); next }
/Dominant freq/                 { dom  = val(); next }
/Spectral center/               { cen  = val(); next }
/Top peaks/ {
  rest = substr($0, index($0, ":") + 1); peaks = ""
  while (match(rest, /[0-9]+(\.[0-9]+)? Hz/)) {
    peaks = peaks (peaks == "" ? "" : ", ") clean(substr(rest, RSTART, RLENGTH - 3))
    rest = substr(rest, RSTART + RLENGTH)
  }
  next
}
/High[[:space:]]*\([[:space:]]*4000/ { flush(); next }   # last line of a reading -> write row now
END { flush() }
'
