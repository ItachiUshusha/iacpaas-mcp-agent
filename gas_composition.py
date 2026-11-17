"""Read the component and percentage structures of the IACPaaS gas ontology."""
from decimal import Decimal

from domain import CommonGasImpurity, Fault, GasClassReport, GasCompositionReport, GasRecord, GasShare
from retrieval import bounded, evidence, index_export, label, normalize


def analyze_gas_export(resource, raw, base, compare_with, limit):
    rows, digest = index_export(raw)

    def children(pointer):
        return [f"{pointer}/successors/{i}" for i, _ in enumerate(rows[pointer][0].get("successors", []))]

    def by_meta(pointer, *names):
        return [p for p in children(pointer) if rows[p][0].get("meta") in names]

    def ref(pointer):
        return evidence(rows, pointer, digest, child_limit=3)

    def designation(pointer):
        symbols = by_meta(pointer, "Химическое обозначение")
        return label(rows[symbols[0]][0]) if len(symbols) == 1 else None

    def shares_for(record):
        shares = []
        sections = by_meta(record, "Объемные доли компонентов", "Объёмные доли компонентов")
        for section in sections:
            for entry in by_meta(section, "Компонент"):
                for component in children(entry):
                    symbol = label(rows[component][0])
                    grouped = rows[component][0].get("meta") == "Совокупность химических веществ"
                    components = ([label(rows[p][0]) for p in by_meta(component, "Химическое обозначение")]
                                  if grouped else [symbol])
                    containers = by_meta(component, "Содержание") if grouped else [component]
                    for container in containers:
                        units = by_meta(container, "%")
                        if len(units) != 1 or rows[units[0]][0].get("value") != "%" or not components:
                            continue
                        bounds = {"Не менее": "≥", "Не более": "≤"}
                        values = [(p, bounds[rows[b][0]["meta"]]) for b in by_meta(container, *bounds)
                                  for p in by_meta(b, "Числовое значение")]
                        values.extend((p, "=") for p in by_meta(container, "Числовое значение"))
                        for pointer, relation in values:
                            value = rows[pointer][0].get("value")
                            if type(value) in (int, float) and 0 <= value <= 100:
                                shares.append(GasShare(components=components, relation=relation,
                                                       percent=format(Decimal(str(value)).normalize(), "f"),
                                                       evidence=ref(pointer)))
        return shares

    groups = [p for p, (node, _) in rows.items() if node.get("meta") == "Класс газов"]

    def analyze_group(query):
        needle = normalize(query)
        if not needle:
            raise Fault("INVALID_ARGUMENT", "Нужно название класса или обозначение основного газа.",
                        "Например: Газы на основе Аргона или Ar.")
        candidates = [p for p in groups if (
            needle in {normalize(label(rows[p][0])), normalize(label(rows[p][0])).removeprefix("газы на основе ")}
            or (len(needle) >= 3 and normalize(label(rows[p][0])).removeprefix("газы на основе ").startswith(needle))
            or any(needle == normalize(symbol) for record in by_meta(p, "Газ")
                   if (symbol := designation(record)))
        )]
        if len(candidates) != 1:
            raise Fault("GAS_CLASS_NOT_FOUND", "Не найден единственный подходящий класс газов.",
                        "Выберите точное название класса из search_inforesource.")
        group = candidates[0]
        records = []
        impurities = {}
        for record in by_meta(group, "Газ"):
            symbol = designation(record)
            shares = shares_for(record)
            if symbol is None or not shares:
                continue
            for share in shares:
                if share.relation == "≤":
                    for component in share.components:
                        if normalize(component) != normalize(symbol):
                            impurities.setdefault(normalize(component), (component, share.evidence))
            main = [Decimal(share.percent) for share in shares
                    if share.components == [symbol] and share.relation in {"=", "≥"}]
            if len(main) == 1:
                records.append((main[0], GasRecord(record=ref(record), base=symbol, shares=shares)))
        total = len(by_meta(group, "Газ"))
        records.sort(key=lambda pair: (pair[0].copy_negate(), normalize(pair[1].record.trail[-1]), pair[1].record.pointer))
        return GasClassReport(group=ref(group), matched_records=len(records), unranked_records=total-len(records),
                              records=[record for _, record in records[:limit]]), impurities

    first, left = analyze_group(base)
    results = [first]
    common = []
    if compare_with:
        second, right = analyze_group(compare_with)
        if first.group.pointer == second.group.pointer:
            raise Fault("INVALID_ARGUMENT", "Для сравнения нужны два разных класса.", "Укажите другой класс газа.")
        if not left or not right:
            raise Fault("INSUFFICIENT_DATA", "Для сравнения нужны записи примесей в обоих классах.",
                        "Откройте составы марок в источнике и проверьте полноту экспорта.")
        # Only the summary and witnesses are needed for comparison.
        first.records = []
        second.records = []
        results.append(second)
        common = [CommonGasImpurity(component=left[key][0], left=left[key][1], right=right[key][1])
                  for key in sorted(left.keys() & right.keys())]
    return bounded(GasCompositionReport(resource=resource, export_sha256=digest, groups=results,
                                        common_impurities=common))
