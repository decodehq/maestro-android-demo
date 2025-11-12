#!/usr/bin/env bash
set -euo pipefail

# local.sh — minimal & stable for Maestro 2.0.9 + Allure
#
# Commands:
#   ./local.sh test .
#   ./local.sh test "Onboarding flow.yaml"
#   ./local.sh convert            # build allure-results from local logs
#   ./local.sh report             # convert + build HTML report
#   ./local.sh open               # serve & open the report in a browser
#
# Env toggles:
#   DEVICE=emulator-5554          # target a specific device
#   CLEAN=1                       # suppress YAML preview via --no-ansi (no icons)
#   SUITE="Local / Android"       # Allure suite name (for convert/report)
#   SITE_TITLE="Local Allure"     # (optional) some hosts display this
#   PYTHON=/path/to/python        # Python interpreter to run the converter
#
# Outputs:
#   artifacts/
#     ├─ debug-output/<flow>/maestro.log
#     ├─ allure-results/        (JSON produced by converter)
#     └─ allure-report/         (HTML produced by Allure CLI)

ARTIFACTS_DIR="artifacts"
DEBUG_ROOT="${ARTIFACTS_DIR}/debug-output"
RESULTS_DIR="${ARTIFACTS_DIR}/allure-results"
REPORT_DIR="${ARTIFACTS_DIR}/allure-report"
SUITE="${SUITE:-Local / Android}"
CONVERTER=".github/actions/generate-allure-files/maestro_all_to_allure.py"

die() { echo "ERROR: $*" >&2; exit 2; }
have() { command -v "$1" >/dev/null 2>&1; }

# Pick a Python interpreter: $PYTHON > python3 > python
PYTHON_BIN="${PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if have python3; then
    PYTHON_BIN="python3"
  elif have python; then
    PYTHON_BIN="python"
  else
    die "No Python interpreter found. Set PYTHON=/path/to/python or install python3."
  fi
fi

cmd="${1:-}"; shift || true
[[ -n "${cmd:-}" ]] || die "Missing command. Try: ./local.sh test . | convert | report | open"

case "$cmd" in
  test)
    target="${1:-.}"

    # Build the list of flow files
    declare -a files=()
    if [[ -d "$target" ]]; then
      while IFS= read -r -d '' f; do
        base="$(basename "$f")"
        [[ "$base" =~ ^config\.ya?ml$ ]] && continue
        files+=("$f")
      done < <(find "$target" -maxdepth 1 -type f \( -name "*.yaml" -o -name "*.yml" \) -print0)
      (( ${#files[@]} > 0 )) || die "No .yaml/.yml flow files found in: $target"
    else
      [[ -f "$target" ]] || die "Flow file not found: $target"
      files+=("$target")
    fi

    # Clean previous debug-output for a fresh run
    rm -rf "$DEBUG_ROOT"
    mkdir -p "$DEBUG_ROOT" "$ARTIFACTS_DIR"

    echo ">> Found ${#files[@]} flow(s). Running each one with live steps…"
    echo

    last_log_path=""

    for flow in "${files[@]}"; do
      base="$(basename "$flow")"
      flow_name="${base%.*}"
      out_dir="${DEBUG_ROOT}/${flow_name}"
      mkdir -p "$out_dir"

      echo "================================================================"
      echo "== Running: $flow"
      echo "== Debug out: $out_dir"
      echo "================================================================"

      # Build args
      args=( test "$flow" --debug-output "$out_dir" --flatten-debug-output )
      [[ -n "${DEVICE:-}" ]] && args+=( --device "$DEVICE" )

      if [[ "${CLEAN:-0}" == "1" ]]; then
        # CLEAN mode: hide YAML preview, simpler renderer (no icons)
        args+=( --no-ansi )
        maestro "${args[@]}"
      else
        # PRETTY mode: keep icons & rich steps (may show a small YAML preamble)
        maestro "${args[@]}" --ansi
      fi

      # Save logs
      if [[ -f "$out_dir/maestro.log" ]]; then
        ts="$(date +%Y%m%d-%H%M%S)"
        cp "$out_dir/maestro.log" "${DEBUG_ROOT}/maestro-${flow_name}-${ts}.log"
        last_log_path="$out_dir/maestro.log"
        echo ">> Saved driver log: $out_dir/maestro.log"
        echo ">> Also copied: ${DEBUG_ROOT}/maestro-${flow_name}-${ts}.log"
      else
        echo "!! Warning: Driver log not found at $out_dir/maestro.log"
      fi

      echo
    done

    # Convenience: copy the last run’s driver log to repo root
    if [[ -n "${last_log_path:-}" && -f "$last_log_path" ]]; then
      cp "$last_log_path" ./maestro.log
      echo ">> Latest driver log copied to: ./maestro.log"
    fi

    echo ">> Done. Per-flow logs live under: ${DEBUG_ROOT}/<flow>/maestro.log"
    echo ">> Timestamped copies live under: ${DEBUG_ROOT}/maestro-<flow>-<timestamp>.log"
    ;;

  convert)
    [[ -f "$CONVERTER" ]] || die "Converter not found: $CONVERTER"
    [[ -d "$DEBUG_ROOT" ]] || die "No debug logs at ${DEBUG_ROOT}. Run './local.sh test .' first."

    rm -rf "$RESULTS_DIR"
    mkdir -p "$RESULTS_DIR"

    converted=0
    shopt -s nullglob
    for flow_dir in "$DEBUG_ROOT"/*; do
      [[ -d "$flow_dir" ]] || continue
      log="$flow_dir/maestro.log"
      [[ -f "$log" ]] || continue
      flow_name="$(basename "$flow_dir")"
      echo ">> Converting: $log  (test: ${flow_name})"
      "$PYTHON_BIN" "$CONVERTER" \
        --url "$log" \
        --out-dir "$RESULTS_DIR" \
        --suite "$SUITE" \
        --test "$flow_name"
      converted=$((converted+1))
    done
    shopt -u nullglob

    (( converted > 0 )) || die "No per-flow driver logs found under ${DEBUG_ROOT}. Did you run './local.sh test .'?"
    echo ">> Wrote Allure JSON to: $RESULTS_DIR  (tests converted: $converted)"
    ;;

  report)
    "$0" convert
    have allure || die "Allure CLI not found. Install it (e.g., brew install allure)."
    rm -rf "$REPORT_DIR"
    mkdir -p "$REPORT_DIR"
    allure generate "$RESULTS_DIR" -o "$REPORT_DIR" --clean
    echo ">> Allure report ready: ${REPORT_DIR}/index.html"
    ;;

  open)
    if have allure; then
      # Serve over HTTP (fixes 'Loading...' when opening via file://)
      allure open "$REPORT_DIR"
    else
      # Fallback tiny HTTP server
      [[ -d "$REPORT_DIR" ]] || die "Report not found. Run './local.sh report' first."
      echo "Allure CLI not found; serving ${REPORT_DIR} via python's http.server on http://127.0.0.1:8000"
      (cd "$REPORT_DIR" && ${PYTHON_BIN} -m http.server 8000) &
      pid=$!
      sleep 1
      if command -v open >/dev/null 2>&1; then
        open "http://127.0.0.1:8000/index.html"
      else
        echo "Open http://127.0.0.1:8000/index.html in your browser."
      fi
      wait $pid
    fi
    ;;

  *)
    die "Unknown command: $cmd (expected: test | convert | report | open)"
    ;;
esac
