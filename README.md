# D-LAB-5 edge template

The **edge** half of a D-LAB-5 cloud-and-edge product: the local app a
person, a home server or a robot runs, linked to a cloud made from
[template-dlab5-cloud](https://github.com/d-lab-5/template-dlab5-cloud).

It is a [Copier](https://copier.readthedocs.io) **layer** over
[ERP-LAB-5/python-tool-template](https://github.com/ERP-LAB-5/python-tool-template):
the base makes a local Python tool (Flask page, MCP server, workspace,
releases); this layer adds what makes it an edge. Both stay updatable.

```bash
copier copy gh:ERP-LAB-5/python-tool-template my-edge          # the base
cd my-edge && git init -b main && git add -A && git commit -m "Start my-edge"
copier copy --trust --data-file .copier-answers.yml \
            -a .copier-answers.edge.yml gh:d-lab-5/template-dlab5-edge .
./test.sh && ./run.sh
```

## What the layer adds

- **Linking** with the device flow of DigitalHome's edges (RFC 8628): the
  edge shows a code, an admin of the tenant approves it on the cloud's
  `/link`, the edge gets a revocable token for that tenant. Several clouds at
  once (sandbox, stage, prod). A version header the cloud gates on (426), and
  tokens **renewed on a timer**, with failures shown instead of swallowed.
- **Knowledge**: one A-Box per space, checked with the cloud's SHACL shapes,
  three-way merged with every linked cloud.
- **Background jobs** with progress and Cancel, and **first start**: what a
  new edge fetches by itself (models from the cloud, SHA-256 checked; the
  tenant's knowledge after linking).
- **A chat panel**: the person's own Claude Code with only this edge's MCP
  tools; every change is a proposal they confirm.
- **A guard**: the edge answers to this machine only (DNS rebinding), refuses
  writes from other sites, cannot be framed.
- **Installers** a cloud offers in its Settings → Edges (`python install_….py`
  on any OS, or a shell script), SHA-256 checked, no admin rights.
- **Edge kinds**: `edge_kind` = `human` (a PC app), `iot` (a Node-RED bridge
  and a compose file) or `robot` (a ROS 2 bridge, imported only when ROS is
  there).

What belongs to the base, to this layer and to the tool:
[`template/docs/edge.md.jinja`](template/docs/edge.md.jinja), rendered into
every tool as `docs/edge.md`.

The patterns are an ArchiMate blueprint,
[`cloud-edge-platform.ttl`](https://github.com/d-lab-5/blueprinting-dlab5-net/blob/stage/docs/patterns/cloud-edge-platform.ttl)
in blueprinting-dlab5-net. Lifted from the Mediathek edge, which is the first
instance; DigitalHome's `digitalhome-edge` (Node-RED, Node.js) speaks the
same wire.

## Testing the template

```bash
./test.sh             # human, iot and robot: make each, run its tests, start it
./test.sh human       # one kind
KEEP=1 ./test.sh      # keep the made tools to look at
```

## Licence

**GPL-3.0-or-later**, as the base template; see [LICENSE](LICENSE). Anything
made from it inherits it.
