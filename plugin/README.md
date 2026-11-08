# dlab5-edge-template

The edge template's own agent skill: making a D-LAB-5 edge (human, IoT or robot) on python-tool-template, and working in one. It lives with the template because it describes the template. This folder is outside the Copier `template/` subdirectory, so generated edges don't copy it.

**Install** from the D-LAB-5 catalogue:

```
/plugin marketplace add d-lab-5/agent-skills
/plugin install dlab5-edge-template@d-lab-5
```

**Change a skill** here, in its one home. Then bump `version` in
`.claude-plugin/plugin.json`, or installed copies never see the change. Try a
working copy with `claude --plugin-dir plugin`. Never copy this skill into
another repository: point to it instead.
