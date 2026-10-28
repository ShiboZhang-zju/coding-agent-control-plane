"""Extract compact operation summaries from the saved OpenAPI spec (PoC helper)."""
import json
import io
import sys

SPEC = "openapi/codebuddy-openapi.json"


def main(prefixes):
    d = json.load(io.open(SPEC, encoding="utf-8"))
    for path, ops in d["paths"].items():
        if prefixes and not any(path.startswith(p) for p in prefixes):
            continue
        for method, op in ops.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            summary = (op.get("summary") or "").strip()
            print(f"\n=== {method.upper()} {path} :: {summary}")
            params = op.get("parameters") or []
            for p in params:
                print(f"    param {p.get('in')}:{p.get('name')} req={p.get('required')} "
                      f"schema={json.dumps(p.get('schema'), ensure_ascii=False)[:160]}")
            rb = op.get("requestBody")
            if rb:
                for ct, media in rb.get("content", {}).items():
                    sch = media.get("schema", {})
                    print(f"    body[{ct}] -> {json.dumps(sch, ensure_ascii=False)[:400]}")
            resp = op.get("responses", {})
            print("    responses: " + ",".join(str(k) for k in resp.keys()))


if __name__ == "__main__":
    main(sys.argv[1:])
