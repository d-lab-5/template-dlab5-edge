#!/usr/bin/env bash
# Test the edge layer by using it: for each edge kind, make a tool from the
# base template, add this layer on top, and prove the result works.
#
#   ./test.sh               human, iot and robot
#   ./test.sh human         one kind
#   KEEP=1 ./test.sh        leave the made tools behind to inspect
#   BASE=gh:ERP-LAB-5/python-tool-template ./test.sh    another base
#
# For each kind:
#   1. copier copy the base, then this layer (from the working tree, so
#      uncommitted changes are tested too)
#   2. edge-wire.py wired pyproject, requirements, app.py, mcp_server.py, page
#   3. the tool's own ./test.sh passes: its tests and the layer's
#   4. ./run.sh starts it; /api/cloud and the panel answer; a foreign Host is
#      refused; ./run.sh --stop frees the port
set -euo pipefail
cd "$(dirname "$0")"
unset PYTHONPATH
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1

COPIER=${COPIER:-$(command -v copier || echo "$HOME/.local/share/dlab5-tools/copier/bin/copier")}
[ -x "$COPIER" ] || { echo "copier is needed: python3 -m venv ~/.local/share/dlab5-tools/copier && ~/.local/share/dlab5-tools/copier/bin/pip install copier"; exit 1; }
BASE=${BASE:-$HOME/ERP-LAB-5/python-tool-template}
[ -e "$BASE" ] || BASE=gh:ERP-LAB-5/python-tool-template
KINDS=("${@:-human iot robot}")
read -r -a KINDS <<< "${KINDS[*]}"

WORK=$(mktemp -d "${TMPDIR:-/tmp}/edge-template-test.XXXXXX")
cleanup() {
  for port in 18790 18791 18792; do curl -fsS -X POST "http://127.0.0.1:$port/api/shutdown" >/dev/null 2>&1 || true; done
  if [ "${KEEP:-0}" = 1 ]; then echo "  kept: $WORK"; else rm -rf "$WORK"; fi
}
trap cleanup EXIT

port=18790
for kind in "${KINDS[@]}"; do
  echo "== $kind"
  dst="$WORK/demo-$kind-edge"
  "$COPIER" copy --trust --defaults --quiet \
    --data tool_name="demo-$kind-edge" --data title="Demo $kind edge" --data port=$port \
    --data with_windows=false --data with_plugin=false \
    "$BASE" "$dst"
  (cd "$dst" && git init -q -b main && git add -A && git -c user.email=t@example.com -c user.name=t commit -qm base)
  "$COPIER" copy --trust --defaults --quiet --vcs-ref HEAD \
    --data-file "$dst/.copier-answers.yml" --data edge_kind="$kind" \
    -a .copier-answers.edge.yml "$PWD" "$dst" | sed 's/^/   /'
  pkg="demo_${kind}_edge"
  grep -q "edge_routes.install(app)" "$dst/$pkg/app.py"
  grep -q "add_edge_tools(server)" "$dst/$pkg/mcp_server.py"
  grep -q "edge-connections" "$dst/$pkg/templates/index.html"
  grep -q "pyshacl" "$dst/requirements.txt"
  grep -q "\"$pkg.edge\"" "$dst/pyproject.toml"
  [ -f "$dst/.copier-answers.edge.yml" ] || { echo "   no .copier-answers.edge.yml"; exit 1; }
  echo "   wired"
  if [ "$port" = 18790 ]; then
    # an update of the layer keeps what the tool owns and wires nothing twice
    echo "# mine" >> "$dst/$pkg/edge/settings.py"
    (cd "$dst" && git add -A && git -c user.email=t@example.com -c user.name=t commit -qm "edge layer")
    (cd "$dst" && "$COPIER" update --trust --defaults --quiet --vcs-ref HEAD -a .copier-answers.edge.yml >/dev/null)
    grep -q "^# mine" "$dst/$pkg/edge/settings.py" || { echo "   update overwrote settings.py"; exit 1; }
    [ "$(grep -c 'edge_routes.install(app)' "$dst/$pkg/app.py")" = 1 ] || { echo "   wired twice"; exit 1; }
    echo "   update keeps settings.py and wires nothing twice"
  fi
  (cd "$dst" && ./test.sh -q 2>&1 | tail -3)
  (cd "$dst" && ./run.sh --no-update-check >"$WORK/$kind.log" 2>&1 &)
  for _ in $(seq 1 60); do curl -fsS "http://127.0.0.1:$port/api/health" >/dev/null 2>&1 && break; sleep 1; done
  curl -fsS "http://127.0.0.1:$port/api/cloud" | grep -q '"linked": *false'
  curl -fsS "http://127.0.0.1:$port/edge-static/edge.js" >/dev/null
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: evil.example:$port" "http://127.0.0.1:$port/api/cloud")
  [ "$code" = 403 ] || { echo "   a foreign Host got $code"; exit 1; }
  (cd "$dst" && ./run.sh --stop >/dev/null 2>&1) || true
  echo "   runs, answers, refuses a foreign Host, stops"
  port=$((port + 1))
done
echo "all good: ${KINDS[*]}"
