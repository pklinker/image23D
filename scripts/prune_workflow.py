#!/usr/bin/env python3
"""Prune usain-bolt.api.json down to PLAN.md sec.2's kept node set.

Deletes the bake tail, preview-only nodes, and the dormant TRELLIS.2 branch.
Then verifies no surviving node references a deleted id (PLAN.md notes no
rewiring is needed -- this just confirms that claim against the real graph).
"""
import json
import sys

SRC = "usain-bolt.api.json"
DST = "usain-bolt.pruned.api.json"

BAKE_TAIL = {"147", "196", "210", "224", "233", "260", "261", "285", "288", "322"}
PREVIEW_ONLY = {"164", "207", "208", "226", "235", "246", "262", "323"}
DORMANT_BRANCH = {"40", "299", "314", "315", "316", "318"}
DELETE = BAKE_TAIL | PREVIEW_ONLY | DORMANT_BRANCH

graph = json.load(open(SRC))

missing = DELETE - graph.keys()
if missing:
    sys.exit(f"delete list references ids not in graph: {missing}")

# The dormant-branch ComfySwitchNodes (314/315/318) aren't dead ends: they sit
# between the real sources and live consumers, selected by 316 PrimitiveBoolean
# ("Switch to Trellis2" = False -> on_false is the active Pixal3D path). Deleting
# the switches means rewiring their consumers directly to the on_false source.
REWIRE = {"314": ["298", 0], "315": ["298", 1], "318": ["319", 0]}
assert graph["316"]["inputs"]["value"] is False, "expected Switch to Trellis2 = False"
for switch_id, (target_id, out_idx) in REWIRE.items():
    on_false = graph[switch_id]["inputs"]["on_false"]
    assert on_false == [target_id, out_idx], f"{switch_id} on_false changed: {on_false}"

for node in graph.values():
    for input_name, value in node.get("inputs", {}).items():
        if isinstance(value, list) and len(value) == 2 and value[0] in REWIRE:
            node["inputs"][input_name] = REWIRE[value[0]]

pruned = {k: v for k, v in graph.items() if k not in DELETE}

# 247/282 MeshToFile3D only builds an in-memory File3D object -- it has no
# is_output_node flag and writes nothing to disk. The bake tail we deleted
# included the one node (322 Save3DAdvanced) that anchored this branch to an
# actual output, so without a replacement the executor prunes 247/282 and
# everything upstream of them as unreachable. SaveGLB (is_output_node=True)
# accepts a File3D input directly, so it drops in with no rewiring.
pruned["1001"] = {
    "inputs": {"mesh": ["247", 0], "filename_prefix": "3d/coarse"},
    "class_type": "SaveGLB",
    "_meta": {"title": "Save GLB (coarse preview)"},
}
pruned["1002"] = {
    "inputs": {"mesh": ["282", 0], "filename_prefix": "3d/final"},
    "class_type": "SaveGLB",
    "_meta": {"title": "Save GLB (final)"},
}

# PLAN.md sec.2: stock target (700k) is "too heavy for a browser" by the plan's
# own admission; sec.8 Phase 3 calls for tuning this down to 200-300k against
# real photos. Tuned in PHASE3.md against the one available test photo.
pruned["186"]["inputs"]["target_face_count"] = 250_000

dangling = []
for node_id, node in pruned.items():
    for input_name, value in node.get("inputs", {}).items():
        if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
            ref_id = value[0]
            if ref_id in DELETE:
                dangling.append((node_id, input_name, ref_id))

if dangling:
    for node_id, input_name, ref_id in dangling:
        print(f"DANGLING: node {node_id} input {input_name!r} -> deleted node {ref_id}")
    sys.exit(1)

json.dump(pruned, open(DST, "w"), indent=2)
print(f"{len(graph)} -> {len(pruned)} nodes, no dangling references. Wrote {DST}")
