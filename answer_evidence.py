"""Trace sources from tool responses; citation validity is not a semantic verdict."""
import hashlib
import json
import re

from retrieval import fields, label

CITATION = re.compile(r"\[(E[A-Za-z0-9]+)\]")


def collect_evidence(data):
    if not isinstance(data, dict):
        return []
    if "results" in data and "queries" in data:
        return [source for result in data["results"] for report in result["reports"]
                for source in collect_evidence(report)]
    if "resource" not in data or "source" not in data:
        return []
    common = {key: data.get(key) for key in ("resource", "source", "requested_depth", "export_sha256")}
    records = []
    def add(node):
        records.append({**common, **node})
    for hit in data.get("matches", []):
        add(hit)
    if "matches" in data:
        fields_used = {key: data[key] for key in ("query", "scanned_nodes", "total_matches", "matches_truncated")}
        identity = json.dumps([common, fields_used], ensure_ascii=False, sort_keys=True)
        ref = "E" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        add({"evidence_id": ref, "pointer": "/matches",
             "trail": [data["resource"]["title"], "Результат поиска"],
             "fields": fields_used, "children": [], "children_truncated": False})
    if "common" in data and "left" in data and "right" in data:
        add(data["left"])
        add(data["right"])
        for match in data["common"]:
            for item in match["left_evidence"] + match["right_evidence"]:
                add(item)
    if "ranking" in data and "group" in data:
        add(data["group"])
        for row in data["ranking"]:
            for key in ("record", "composition", "percentage"):
                add(row[key])
    if "groups" in data and "common_impurities" in data:
        for group in data["groups"]:
            add(group["group"])
            for row in group["records"]:
                add(row["record"])
                for share in row["shares"]:
                    add(share["evidence"])
        for component in data["common_impurities"]:
            add(component["left"])
            add(component["right"])
    if "root" in data:
        digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        def walk(node, pointer="/root", trail=()):
            path = [*trail, label(node)]
            ref = "E" + hashlib.sha256((digest + pointer).encode()).hexdigest()[:20]
            children = node.get("successors", [])
            add({"evidence_id": ref, "pointer": pointer, "trail": path, "fields": fields(node),
                 "children": [fields(c) for c in children[:20]], "children_truncated": len(children)>20})
            for i, child in enumerate(children):
                walk(child, pointer + "/successors/" + str(i), path)
        walk(data["root"])
    elif "counts" in data:
        counts = data["counts"]
        ref = "E" + hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
        add({"evidence_id": ref, "pointer": "/counts", "trail": [data["resource"]["title"], "Статистика фрагмента"],
             "fields": counts, "children": [], "children_truncated": False})
    return records


def audit_answer(text, evidence):
    cited = list(dict.fromkeys(CITATION.findall(text)))
    invalid = [ref for ref in cited if ref not in evidence]
    sources = [evidence[ref] for ref in cited if ref in evidence]
    status = "invalid_citations" if invalid else "cited" if sources else "uncited"
    notes = {
        "cited": "Ссылки существуют в результатах инструментов. Это проверка источников, не автоматическая проверка смысла каждого утверждения.",
        "uncited": "Ответ не содержит проверяемых ссылок на записи. Успешный вызов MCP сам по себе не подтверждает вывод модели.",
        "invalid_citations": "Модель сослалась на записи, которых нет среди полученных доказательств. Не используйте этот ответ как подтверждённый."
    }
    return {"status": status, "note": notes[status], "invalid_citations": invalid, "sources": sources,
            "available_evidence_count": len(evidence)}
