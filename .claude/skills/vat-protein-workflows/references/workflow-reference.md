# Workflow reference

Registered proposals use `vat-workflow/1`. A request remains a complete valid
`protein-tool/1` request before binding; its literal value is a reviewable placeholder. At
execution, the backend replaces that destination with a verified producer reference and
validates the resolved request again.

```json
{
  "id": "score-candidate",
  "tool_id": "esmc",
  "execution_id": "00000000-0000-4000-8000-000000000002",
  "request": {
    "version": "protein-tool/1",
    "tool": "esmc",
    "operation": "score",
    "payload": {"sequence": "AC", "task": "score", "count": 1, "seed": 1, "temperature": 1.0}
  },
  "bindings": [{
    "destination": "/payload/sequence",
    "producer": "generate",
    "selector": {"kind": "return", "pointer": "/candidates/0/sequence", "classification": "output"},
    "allow_intermediate": false
  }],
  "controls": [],
  "authorized": true
}
```

Artifact selectors name the exact captured output and set an empty pointer. Use
`encoding: "utf-8"` when a text artifact feeds a JSON string field; the binding commits both
the decoded input and the original artifact bytes. The default `identity` supplies bytes.
Intermediate
artifacts require both `classification: "intermediate"` and `allow_intermediate: true`.
References must point backward in the node array. A control reference records ordering or an
approval relationship and does not claim that producer data became a consumer input.

Dynamic entrypoints receive `(input, output_dir)`. They return finite JSON and may create regular
files directly beneath `output_dir`. The repository-owned launcher rejects other paths,
symlinks, duplicate names and output over the byte limit.
