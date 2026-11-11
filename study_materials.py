"""Collect bounded real exports for studying ontology and material resources."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from uuid import uuid4

import iacpaas_client as api

BASE = Path(__file__).resolve().parent
TARGETS = [
    ("properties-ontology", "Онтология базы свойств материалов", "ontology"),
    ("properties-data", "База свойств материалов", "data"),
    ("characteristics-ontology", "Онтология описания характеристик и их значений", "ontology"),
]


def save_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def tree_lines(root, limit=150):
    lines, pending = [], [(root, 0)]
    while pending and len(lines) < limit:
        node, level = pending.pop()
        if not isinstance(node, dict):
            continue
        label = node.get("name", node.get("value", "[без метки]"))
        suffix = f" [meta={node.get('meta', '—')}; type={node.get('type', '—')}]"
        if "link" in node:
            suffix += " → ссылка: " + str(node["link"])
        lines.append("  " * level + "- " + str(label).replace("\n", " ") + suffix)
        children = node.get("successors", [])
        if isinstance(children, list):
            pending.extend((child, level + 1) for child in reversed(children))
    if pending:
        lines.append("… отображение ограничено; исходный JSON сохранён рядом.")
    return lines


def generate_notes(manifest, directory):
    lines = ["# Фактические данные для разбора к паре", "",
             "Источник: авторизованные GET-запросы экспорта IACPaaS; это ограниченные фрагменты, не полные базы.", "",
             "Время получения UTC: " + manifest["collected_at_utc"], ""]
    for item in manifest["resources"]:
        lines.extend(["## " + item["title"], "", "Тип в исследовании: " + item["role"], ""])
        if not item.get("root_file"):
            lines.extend(["Чтение не подтверждено: " + item.get("error", "неизвестная ошибка"), ""])
            continue
        raw = json.loads((directory / (item.get("fragment_file") or item["root_file"])).read_text(encoding="utf-8"))
        lines.extend(["Код ресурса: `" + item["resource_id"] + "`.", "",
                      "Поле ontology корня: `" + str(raw.get("ontology", "отсутствует")) + "`.", "",
                      "Запрошенная глубина: " + str(item.get("fragment_depth", 0)) + ".", "",
                      "```text", *tree_lines(raw), "```", ""])
        if item.get("fragment_error"):
            lines.extend(["Фрагмент получить не удалось: " + item["fragment_error"], ""])
    lines.extend(["## Что проверить при разборе", "",
                  "1. Сопоставить поле ontology базы с именем выбранной онтологии.",
                  "2. Найти метки meta в базе и соответствующие понятия онтологии.",
                  "3. Выбрать конкретную характеристику/объект из фактического дерева и прочитать её потомков.",
                  "4. Не принимать характеристику-тип за значение свойства конкретного материала.",
                  "5. Указать отсутствующие сведения и внешние ссылки, не подставлять значения из памяти.", ""])
    (directory / "FACTS.md").write_text("\n".join(lines), encoding="utf-8")


def collect(headers, depth, directory, targets=None, *, resource_path=None):
    manifest = {"collected_at_utc": datetime.now(timezone.utc).isoformat(), "source": "live_api", "resources": []}
    catalog_path = BASE / "catalog.json"
    verified = {}
    if resource_path is not None:
        targets = [("selected-resource", resource_path.rsplit("/", 1)[-1], "selected")]
    for slug, title, role in (TARGETS if targets is None else targets):
        print("Чтение: " + title, flush=True)
        item = {"title": title, "role": role, "path": resource_path or "Загрузки/" + title}
        manifest["resources"].append(item)
        try:
            root = api.export_resource(item["path"], depth=0, headers=headers)
            if root.get("title") != title or not root.get("code"):
                raise api.ClientError("Имя/код ответа не соответствует выбранному ресурсу.")
            item["resource_id"] = str(root["code"])
            item["root_file"] = slug + "-root.json"
            save_json(directory / item["root_file"], root)
            row = {"resource_id": item["resource_id"], "title": title, "path": item["path"]}
            verified[item["path"]] = row
            try:
                fragment = api.export_resource(item["path"], depth=depth, headers=headers)
                if str(fragment.get("code")) != item["resource_id"] or fragment.get("title") != title:
                    raise api.ClientError("Ответ фрагмента не совпадает с корнем ресурса.")
                item["fragment_file"] = slug + f"-depth{depth}.json"
                item["fragment_depth"] = depth
                save_json(directory / item["fragment_file"], fragment)
                print("  OK: корень и глубина " + str(depth), flush=True)
            except api.ClientError as exc:
                item["fragment_error"] = str(exc)
                print("  Корень прочитан; фрагмент: " + str(exc), flush=True)
        except api.ClientError as exc:
            item["error"] = str(exc)
            print("  Ошибка: " + str(exc), flush=True)
        save_json(directory / (slug + "-status.json"), item)
    save_json(directory / "manifest.json", manifest)
    generate_notes(manifest, directory)
    # Only identities confirmed by authenticated exports enter the MCP catalog.
    # Preserve the original catalog for rollback; never guess resource codes.
    if verified:
        current_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog_by_path = {row["path"]: row for row in current_catalog}
        catalog_by_path.update(verified)
        updated_catalog = list(catalog_by_path.values())
        if updated_catalog != current_catalog:
            save_json(directory / "catalog-before.json", current_catalog)
            pending = catalog_path.with_name(f".catalog-{uuid4().hex}.tmp")
            try:
                save_json(pending, updated_catalog)
                pending.replace(catalog_path)
            finally:
                pending.unlink(missing_ok=True)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--depth", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--verify-mcp", action="store_true", help="After collection, verify registered resources through stdio and HTTP with the same login")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--gases", action="store_true", help="Collect chemical substances and metalwire materials to locate gas records")
    selection.add_argument("--resource-path", help="Verify and connect an exact resource path inside My Fund")
    args = parser.parse_args()
    if (args.gases or args.resource_path is not None) and args.verify_mcp:
        parser.error("--verify-mcp applies only to the original material properties study")
    try:
        resource_path = api.folder_path(args.resource_path) if args.resource_path is not None else None
        targets = [("chemicals", "База химических элементов и веществ", "data"),
                   ("metalwire", "База металлопроволочных материалов", "data")] if args.gases else None
        headers = api.auth_headers()
        directory = BASE / "data/materials-study" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6])
        directory.mkdir(parents=True)
        manifest = collect(headers, args.depth, directory, targets, resource_path=resource_path)
        complete = all(r.get("fragment_file") for r in manifest["resources"])
        print("\nРезультаты: " + str(directory), flush=True)
        print("Разбор дерева: " + str(directory / "FACTS.md"), flush=True)
        print("Авторизация и заголовки не сохраняются. Исходные экспорты остаются в локальной data/.", flush=True)
        if complete and args.verify_mcp:
            import asyncio
            from materials_demo import verify
            asyncio.run(verify(headers, directory))
        return 0 if complete else 1
    except (api.ClientError, OSError, RuntimeError) as exc:
        print("Ошибка: " + str(exc), file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nЧтение отменено.", file=sys.stderr)
        return 130
    except Exception:
        print("Сбор прерван внутренней ошибкой или ошибкой соединения MCP. Сохранённые JSON остаются в data/materials-study/; авторизация не записана.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
