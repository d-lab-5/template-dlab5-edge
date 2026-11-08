---
name: dlab5-edge-template
description: Making and working in a D-LAB-5 EDGE: the local Python app (Flask page, MCP server, AI chat) that a person, a home server or a robot runs and links to a cloud made from template-dlab5-cloud. An edge is ERP-LAB-5/python-tool-template plus the template-dlab5-edge Copier layer (cloud link with the RFC 8628 device flow, token renewal, A-Box sync, jobs, first start, chat proposals, a localhost-only guard, installers, edge_kind human | iot (Node-RED) | robot (ROS 2)). Use to start a new edge, add the edge layer to a tool, update either template, or when a repo shows the tells: a `<package>/edge/` folder with cloud.py, sync.py, routes.py and settings.py, a `.copier-answers.edge.yml`, scripts/edge-wire.py. Covers who owns which file, the two-step generation, where product decisions go (settings.py), the wire rules (426, 410 vs Gone, tenant from the token), and the traps already hit (ffmpeg/claude stderr deadlocks, tokens lapsing while unused).
---

# D-LAB-5 edges (template-dlab5-edge)

An **edge** is the local half of a cloud-and-edge product: it makes or holds
what the cloud shows, and keeps what must stay private. Examples: the
Mediathek edge (a person's PC: photos, films, faces), DigitalHome's
`digitalhome-edge` (a home server with Node-RED; Node.js, same wire). The
cloud half is template-dlab5-cloud (skill **dlab5-cloud-template**); the
patterns both follow are the blueprint (skill **dlab5-cloud-edge-blueprint**).

## Making one

```bash
COPIER=~/.local/share/dlab5-tools/copier/bin/copier      # see pytool-kit:new-tool
"$COPIER" copy --trust --defaults --data tool_name=fleet-edge --data title="Fleet Edge" \
  --data port=8772 gh:ERP-LAB-5/python-tool-template ~/D-LAB-5/fleet-edge
cd ~/D-LAB-5/fleet-edge && git init -b main && git add -A && git commit -m "Start fleet-edge from the D-LAB-5 tool template"
"$COPIER" copy --trust --data-file .copier-answers.yml -a .copier-answers.edge.yml \
  --data edge_kind=human --data cloud_name="Fleet" --data tenant_noun=team --data space_noun=board \
  gh:d-lab-5/template-dlab5-edge .
./test.sh && ./run.sh
```

Ports: check `ss -ltn` and other tools' answers (8765, 8766, 8770, 8771 taken).
`scripts/edge-wire.py` runs as a Copier task and adds, once, the layer's
lines to the tool's own files (pyproject, requirements.txt, app.py,
mcp_server.py, the page). It prints what it could NOT wire: do those by hand
(docs/edge.md in the tool says how).

## Who owns what

| | Owner | On `copier update` |
|---|---|---|
| `<pkg>/core/` | base template | rewritten — never edit; fix upstream (python-tool-template) |
| `app.py`, `mcp_server.py`, `templates/`, `static/`, `pyproject.toml`, `requirements.txt` | the tool | kept |
| `<pkg>/edge/*.py`, `edge/static/`, `edge/bridges/`, `scripts/install*`, `scripts/publish-to-cloud.sh`, `tests/test_edge_*.py` | edge layer | rewritten — change `settings.py` instead |
| `<pkg>/edge/settings.py`, `edge/ontology/`, `__main__.py` | the tool | written once, kept |

```bash
copier update --trust                                   # the base
copier update --trust -a .copier-answers.edge.yml       # the edge layer
```

**Product decisions go in `edge/settings.py`**: the chat's prompt and
`CHAT_ALLOWED` (what a chat proposal may call), `SINGLE_VALUED` facts (a
merge conflict when both sides change them), `FIRST_START` steps (models,
downloads). Register the tool's own background work with
`edge.jobs.JOBS.register(kind, run)`, and its changing MCP tools with
`edge.mcp_tools.act()` so the chat panel turns them into proposals.

Copy the cloud's `packages/ontology` (tbox/, shapes/) into `edge/ontology/`
so the edge validates A-Boxes exactly as the cloud does.

**Decisions and diagrams** of a product with a cloud and edges live in its
public `<product>-shared` repository, made from `d-lab-5/template-dlab5-shared`
and built into docs.dlab5.net; the edge cites them as "ADR-00NN in
`<product>-shared`". See the `dlab5-shared-docs` skill.

## Wire rules (docs/specs/edge-cloud-api.md in the cloud)

- Every call carries `X-Edge-Version`; **426** means update the edge.
- **410 bare = revoked** (forget the token, re-pair). **410 with a product
  error code = one item gone** (`cloud.Gone`): never treat it as a revoke.
- 403 `no_tenant`: linked before it had a tenant; link again.
- The cloud takes the tenant from the **token row**, never from a body.
- Spaces are matched by **name** (`cloud.space_slug`): ids differ between sites.
- Several clouds at once: each is a **connection** (sandbox, stage, prod) with
  its own token in `~/.config/<tool>/cloud.json` (0600).

## Traps already hit

- **A subprocess's stderr must be drained while it runs.** ffmpeg logging an
  error per frame filled the pipe and hung publishing for nine hours at 0 %
  CPU. The layer's chat drains it; do the same for any tool subprocess, and
  add a no-progress watchdog.
- **ffmpeg exits 0 without writing an image** (a seek past the last frame, an
  fps filter on a sub-second clip): check the output file, fall back.
- **Tokens renewed only "on the next call" lapse on an unused edge.** The
  layer's keeper renews at start and twice a day and shows failures
  (`rotate_error`). Do not remove it.
- **Restart the edge together with a page change**: a cached template with a
  new app.js breaks the page ("connections gone").
- **`core/` fixes belong upstream**: the localhost-only guard (DNS rebinding)
  was first made in a tool's core/ and would have been lost on update; the
  layer now installs it itself, and python-tool-template got a PR.
- On a ROS 2 workstation, run tests with `./test.sh` (it unsets PYTHONPATH),
  never bare pytest.

## Releasing to the cloud

`scripts/publish-to-cloud.sh ../the-cloud-repo` builds the wheel, fills the
installers with its SHA-256, writes `backend/edge-release/` there; commit and
push the cloud repo. Raise the cloud's `MIN_EDGE_VERSION` only afterwards.

## Testing the layer itself

In template-dlab5-edge: `./test.sh` makes a human, iot and robot edge from the
base and the working tree, runs each tool's tests, starts it, checks the
guard, and round-trips an update.
