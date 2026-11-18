"""Bounded content search and exact child-label comparison, with traceable evidence."""
import hashlib
import json
import re
import unicodedata
from decimal import Decimal

from domain import ComparisonReport, Evidence, ExportNodeReport, Fault, MAX_RESULT_BYTES, RankedRecord, RankingReport, SearchReport

FIELDS = ("id", "name", "value", "meta", "type", "valtype", "link", "link_id", "comment")
SEARCH_FIELDS = ("name", "value", "meta", "link", "comment")
WARNINGS = [
    "Проверен только экспорт указанной глубины, не весь фонд. Отсутствие совпадений не доказывает отсутствие сведений в других ресурсах.",
    "Ссылки на другие узлы не развёрнуты. JSON Pointer относится к этому экспорту, а не к параметру start API."
]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize(value):
    return " ".join(unicodedata.normalize("NFC", str(value)).casefold().split())


def label(node):
    for field in ("name", "value", "link"):
        value = node.get(field)
        if value is not None and value != "":
            return str(value)
    return "[без метки]"


def fields(node):
    result = {k: node[k] for k in FIELDS if k in node}
    for key in ("id", "link_id"):
        if key in result:
            result[key] = str(result[key])
    return result


def index_export(raw):
    rows = {}
    def visit(node, pointer="", trail=()):
        if len(rows) >= 20_000 or len(trail) > 64:
            raise Fault("RESULT_LIMIT", "Поисковый экспорт превышает лимит 20000 узлов.", "Уменьшите глубину.")
        if not isinstance(node, dict) or not isinstance(node.get("successors", []), list):
            raise Fault("EXPORT_FORMAT", "Некорректный узел экспорта.", "Проверьте формат universal.")
        trail = (*trail, label(node))
        rows[pointer] = (node, trail)
        for i, child in enumerate(node.get("successors", [])):
            visit(child, f"{pointer}/successors/{i}", trail)
    visit(raw)
    # Hash includes the source export; reordering/changing any record invalidates pointers.
    digest = hashlib.sha256(canonical(raw).encode("utf-8")).hexdigest()
    return rows, digest


def evidence(rows, pointer, digest, child_limit=20):
    node, trail = rows[pointer]
    children = node.get("successors", [])
    ref = hashlib.sha256((digest + ":" + pointer).encode()).hexdigest()[:20]
    return Evidence(evidence_id="E" + ref, pointer=pointer, trail=list(trail), fields=fields(node),
                    children=[fields(c) for c in children[:child_limit]], children_truncated=len(children) > child_limit)


def bounded(result):
    if len(result.model_dump_json().encode("utf-8")) > MAX_RESULT_BYTES:
        raise Fault("RESULT_LIMIT", "Поисковая выдача больше 128 КБ.", "Уточните query или уменьшите limit.")
    return result


def search_export(resource, raw, query, depth, limit, source):
    if not query.strip():
        raise Fault("INVALID_ARGUMENT", "Нужен непустой поисковый фрагмент.", "Например: водород. Для названий ресурсов используйте list_inforesources.")
    rows, digest = index_export(raw)
    return search_index(resource, rows, digest, query, depth, limit, source)


def search_index(resource, rows, digest, query, depth, limit, source, child_limit=20):
    needle = normalize(query)
    matched = [pointer for pointer, (node, _) in rows.items()
               if any(needle in normalize(node[key]) for key in SEARCH_FIELDS
                      if node.get(key) is not None)]
    return bounded(SearchReport(resource=resource, source=source, requested_depth=depth,
        export_sha256=digest, query=query, scanned_nodes=len(rows), total_matches=len(matched),
        matches_truncated=len(matched) > limit, matches=[evidence(rows, p, digest, child_limit) for p in matched[:limit]],
        warnings=WARNINGS))


def read_node_export(resource, raw, pointer, expected_digest, depth, levels, max_nodes, source):
    """Read a bounded subtree from a freshly verified export, without guessing API paths."""
    rows, digest = index_export(raw)
    if digest != expected_digest:
        raise Fault("EXPORT_CHANGED", "Экспорт изменился после поиска.",
                    "Повторите поиск и используйте pointer и хеш из нового результата.")
    if pointer not in rows:
        raise Fault("INVALID_ARGUMENT", "Указатель не найден в подтверждённом экспорте.",
                    "Скопируйте pointer из search_inforesource или analyze_gas_composition без изменений.")
    selected = []
    truncated = False

    def visit(current, level):
        nonlocal truncated
        if len(selected) >= max_nodes:
            raise Fault("RESULT_LIMIT", "Поддерево превышает лимит вершин.",
                        "Уменьшите levels или выберите более глубокий pointer из поиска.")
        selected.append(evidence(rows, current, digest))
        children = rows[current][0].get("successors", [])
        if level == levels:
            truncated |= bool(children)
            return
        for index, _ in enumerate(children):
            visit(f"{current}/successors/{index}", level + 1)

    visit(pointer, 0)
    return bounded(ExportNodeReport(resource=resource, source=source, requested_depth=depth,
                                   export_sha256=digest, selected=selected[0], descendants=selected[1:],
                                   requested_levels=levels, truncated=truncated,
                                   warnings=[*WARNINGS, "Потомки ограничены levels и глубиной исходного экспорта."]))


def compare_export(resource, raw, left, right, expected_digest, depth, source):
    rows, digest = index_export(raw)
    if digest != expected_digest:
        raise Fault("EXPORT_CHANGED", "Экспорт изменился после поиска.", "Повторите поиск и выберите указатели из нового экспорта.")
    if left not in rows or right not in rows or left == right:
        raise Fault("INVALID_ARGUMENT", "Нужны два различных узла из найденного экспорта.", "Используйте точные pointer и export_sha256 из search_inforesource.")
    def members(pointer):
        node = rows[pointer][0]
        children = node.get("successors", [])
        if len(children) > 100:
            raise Fault("RESULT_LIMIT", "Сравниваемый список содержит более 100 элементов.", "Выберите более узкую ветку.")
        if not children:
            raise Fault("INSUFFICIENT_DATA", "У выбранного узла нет раскрытого списка.", "Не трактуйте обрезанную или ссылочную вершину как пустой полный список.")
        members = {}
        for i, child in enumerate(children):
            # Reference-only nodes cannot be assigned a chemical identity from their URL.
            if child.get("name") is None and child.get("value") is None:
                raise Fault("UNRESOLVED_REFERENCE", "Список содержит нераскрытую ссылку.", "Сначала получите значения целевых узлов; совпадение ссылок не заменяет сравнение названий.")
            key = normalize(label(child))
            members.setdefault(key, []).append(evidence(rows, f"{pointer}/successors/{i}", digest).model_dump())
        return members
    a, b = members(left), members(right)
    common = [{"label": label(rows[item_a[0]["pointer"]][0]), "left_evidence": item_a,
               "right_evidence": b[key]} for key, item_a in sorted(a.items()) if key in b]
    return bounded(ComparisonReport(resource=resource, source=source, requested_depth=depth,
        export_sha256=digest, left=evidence(rows,left,digest), right=evidence(rows,right,digest),
        common=common, left_only=sorted(set(a)-set(b)), right_only=sorted(set(b)-set(a)),
        rule="Пересечение меток непосредственных потомков: NFC, без учёта регистра и повторных пробелов. Синонимы и формулы автоматически не объединяются.",
        warnings=WARNINGS + ["Правильность выбора двух списков проверяется по их путям. Совпадение меток само по себе не подтверждает химическую классификацию."]))


def rank_export(resource, raw, group_pointer, composition_path, percentage_path, basis, expected_digest, depth, limit):
    rows, digest = index_export(raw)
    if digest != expected_digest:
        raise Fault("EXPORT_CHANGED", "Экспорт изменился после поиска.", "Возьмите указатели из нового экспорта.")
    if group_pointer not in rows:
        raise Fault("INVALID_ARGUMENT", "Указатель на группу не найден.", "Используйте pointer из search_inforesource.")

    def follow(pointer, segments):
        for segment in segments:
            needle = normalize(segment)
            matches = [index for index, child in enumerate(rows[pointer][0].get("successors", []))
                       if needle and needle == normalize(label(child))]
            if len(matches) != 1:
                return None
            pointer = f"{pointer}/successors/{matches[0]}"
        return pointer

    candidates = []
    group = rows[group_pointer][0].get("successors", [])
    for index in range(len(group)):
        record = f"{group_pointer}/successors/{index}"
        composition = follow(record, composition_path)
        if composition is None or not rows[composition][0].get("successors"):
            continue
        percentage = follow(composition, percentage_path)
        if percentage is None:
            continue
        node = rows[percentage][0]
        value = node.get("value")
        if (type(value) not in (str, int, float) or node.get("link") or node.get("link_id")
                or node.get("restrictor") or node.get("specifier")):
            continue
        match = re.fullmatch(r"([0-9]{1,3}(?:[.,][0-9]{1,30})?)\s*%?", str(value).strip())
        if match:
            percent = Decimal(match[1].replace(",", "."))
            if percent <= 100:
                candidates.append((percent, record, composition, percentage))
    if not candidates:
        raise Fault("INSUFFICIENT_DATA", "В выбранной группе нет раскрытого состава с сопоставимыми точными долями.",
                    "Сверьте глубину, пути к составу и доле компонента и единицы измерения.")
    candidates.sort(key=lambda row: (row[0].copy_negate(), normalize(label(rows[row[1]][0])), row[1]))
    ranking = [RankedRecord(record=evidence(rows, record, digest), composition=evidence(rows, composition, digest),
                            percentage=evidence(rows, percentage, digest), percent=format(percent, "f"))
               for percent, record, composition, percentage in candidates[:limit]]
    return bounded(RankingReport(resource=resource, source="live_api", requested_depth=depth, export_sha256=digest,
        group=evidence(rows, group_pointer, digest), percent_basis=basis, matched_records=len(candidates),
        skipped_records=len(group) - len(candidates), ranking=ranking,
        warnings=WARNINGS + ["Ранжируются непосредственные записи выбранной группы.",
                             "Тип доли задан при вызове и должен соответствовать полям исходного графа."]))
