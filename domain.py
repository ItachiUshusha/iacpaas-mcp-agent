"""Transport-independent IACPaaS adapter."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

import iacpaas_client as api

BASE = Path(__file__).resolve().parent
MAX_RESULT_BYTES = 128_000
ResourcePath = Annotated[str, Field(min_length=1, max_length=512, strict=True)]
Depth = Annotated[int, Field(ge=0, le=3, strict=True)]
NodeLimit = Annotated[int, Field(ge=1, le=200, strict=True)]
StartPath = Annotated[str, Field(max_length=512, strict=True)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Fault(Exception):
    def __init__(self, code: str, message: str, hint: str):
        self.code, self.message, self.hint = code, message, hint
        super().__init__(message)


class Error(Model):
    code: str
    message: str
    hint: str


T = TypeVar("T")


class Result(Model, Generic[T]):
    ok: bool
    data: T | None = None
    error: Error | None = None


class Resource(Model):
    resource_id: str
    title: str
    path: ResourcePath


class Listing(Model):
    source: Literal["configured_catalog"] = "configured_catalog"
    resources: list[Resource]
    total_matches: int
    truncated: bool
    access_checked: Literal[False] = False


class FundListing(Model):
    source: Literal["my_fund"] = "my_fund"
    parent: str
    resources: list[Resource]
    scanned_folders: int
    total_matches: int
    truncated: bool
    skipped_folders: list[str]


class Selection(Model):
    path: ResourcePath
    depth: Depth = 0
    start: StartPath = ""
    max_nodes: NodeLimit = 100


class Node(Model):
    id: str | None = None
    name: str | None = None
    type: str | None = None
    value: str | int | float | bool | None = None
    meta: str | None = None
    valtype: str | None = None
    comment: str | None = None
    specifier: str | None = None
    restrictor: str | None = None
    link: str | None = None
    link_id: str | None = None
    successors: list[Node] = Field(default_factory=list)


class Counts(Model):
    node_occurrences: int
    distinct_defined_ids: int
    definitions_without_id: int
    link_occurrences: int
    edge_occurrences: int
    scope: Literal["returned_export_including_ancestors"] = "returned_export_including_ancestors"
    full_graph_count: Literal[False] = False


class Graph(Model):
    resource: Resource
    ontology: str | None = None
    source: Literal["live_api", "saved_snapshot"]
    requested_depth: int
    requested_start: str
    complete_graph: Literal[False] = False
    root: Node
    counts: Counts
    warnings: list[str]


class CountReport(Model):
    resource: Resource
    ontology: str | None = None
    source: Literal["live_api", "saved_snapshot"]
    requested_depth: int
    requested_start: str
    counts: Counts
    warnings: list[str]


class Evidence(Model):
    evidence_id: str
    pointer: str
    trail: list[str]
    fields: dict
    children: list[dict]
    children_truncated: bool


class SearchReport(Model):
    resource: Resource
    source: Literal["live_api", "saved_snapshot"]
    requested_depth: int
    export_sha256: str
    complete_graph: Literal[False] = False
    query: str
    scanned_nodes: int
    total_matches: int
    matches_truncated: bool
    matches: list[Evidence]
    warnings: list[str]


class FundSearchResult(Model):
    resource: Resource
    reports: list[SearchReport]
    error: Error | None = None


class FundSearchReport(Model):
    resources: list[Resource]
    queries: list[str]
    scanned_folders: int
    skipped_folders: list[str]
    truncated: bool
    results: list[FundSearchResult]


class ComparisonReport(Model):
    resource: Resource
    source: Literal["live_api", "saved_snapshot"]
    requested_depth: int
    export_sha256: str
    complete_graph: Literal[False] = False
    left: Evidence
    right: Evidence
    common: list[dict]
    left_only: list[str]
    right_only: list[str]
    rule: str
    warnings: list[str]


class RankedRecord(Model):
    record: Evidence
    composition: Evidence
    percentage: Evidence
    percent: str


class RankingReport(Model):
    resource: Resource
    source: Literal["live_api", "saved_snapshot"]
    requested_depth: int
    export_sha256: str
    complete_graph: Literal[False] = False
    group: Evidence
    percent_basis: str
    matched_records: int
    skipped_records: int
    ranking: list[RankedRecord]
    warnings: list[str]


class GasShare(Model):
    components: list[str]
    relation: Literal["≥", "≤", "="]
    percent: str
    evidence: Evidence


class GasRecord(Model):
    record: Evidence
    base: str | None
    shares: list[GasShare]


class GasClassReport(Model):
    group: Evidence
    matched_records: int
    unranked_records: int
    records: list[GasRecord]


class CommonGasImpurity(Model):
    component: str
    left: Evidence
    right: Evidence


class GasCompositionReport(Model):
    resource: Resource
    source: Literal["live_api"] = "live_api"
    requested_depth: int = 10
    export_sha256: str
    groups: list[GasClassReport]
    common_impurities: list[CommonGasImpurity]


class FolderChange(Model):
    path: ResourcePath
    operation: Literal["created", "moved_to_trash"]
    source: Literal["live_api"] = "live_api"


def project_graph(raw: dict, max_nodes: int) -> tuple[Node, Counts]:
    """Preserve occurrences and references; never infer unique vertices from paths."""
    total = links = missing_ids = 0
    ids: set[str] = set()

    def identifier(value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError("invalid id")
        return str(value)

    def visit(item, nesting=0):
        nonlocal total, links, missing_ids
        total += 1
        if total > max_nodes or nesting > 64:
            raise Fault("RESULT_LIMIT", "Превышен лимит вершин или вложенности.", "Уменьшите depth; max_nodes допускает до 200.")
        if not isinstance(item, dict):
            raise ValueError("node is not an object")
        children = item.get("successors", [])
        if not isinstance(children, list):
            raise ValueError("successors is not an array")
        node_id = identifier(item.get("id"))
        is_link = "link" in item or "link_id" in item
        if is_link:
            links += 1
        elif node_id is None:
            missing_ids += 1
        else:
            ids.add(node_id)
        return Node(
            id=node_id, name=item.get("name"), type=item.get("type"),
            value=item.get("value"), meta=item.get("meta"), link=item.get("link"),
            valtype=item.get("valtype"), comment=item.get("comment"),
            specifier=item.get("specifier"), restrictor=item.get("restrictor"),
            link_id=identifier(item.get("link_id")),
            successors=[visit(child, nesting + 1) for child in children],
        )

    try:
        root = visit(raw)
    except (ValueError, ValidationError, RecursionError):
        raise Fault("EXPORT_FORMAT", "Структура экспорта не поддерживается адаптером.", "Проверьте формат universal и фактический ответ API.") from None
    return root, Counts(node_occurrences=total, distinct_defined_ids=len(ids),
                        definitions_without_id=missing_ids, link_occurrences=links,
                        edge_occurrences=max(0, total - 1))


class Service:
    def __init__(self, mode="live", headers=None, exporter=None, *, fund_session=None):
        if mode not in ("live", "snapshot"):
            raise ValueError("mode must be live or snapshot")
        self.mode = mode
        self.headers = headers
        self.exporter = exporter or api.export_resource
        self.fund_session = fund_session
        self.catalog = [Resource.model_validate(row) for row in json.loads((BASE / "catalog.json").read_text(encoding="utf-8"))]

    def list_resources(self, query: str, limit: int) -> Listing:
        found = [r for r in self.catalog if query.casefold() in (r.title + " " + r.path).casefold()]
        found.sort(key=lambda r: (r.path.casefold(), r.path, r.resource_id))
        return Listing(resources=found[:limit], total_matches=len(found), truncated=len(found) > limit)

    def fund_items(self, parent: str):
        if self.mode != "live" or self.fund_session is None:
            raise Fault("FUND_LOGIN_REQUIRED", "Для просмотра фонда нужна веб-сессия IACPaaS.",
                        "Запустите MCP по HTTP в live-режиме с --login --fund-login.")
        try:
            api.folder_path(parent, allow_root=True)
        except api.ClientError as exc:
            raise Fault("INVALID_ARGUMENT", str(exc), "Укажите относительный путь папки.") from None
        try:
            items = self.fund_session.list_items(parent)
        except api.ClientError as exc:
            raise Fault("UPSTREAM_ERROR", str(exc), "Проверьте веб-вход и доступ к папке.") from None
        for item in items:
            path, title = item.get("path"), item.get("title")
            if not isinstance(path, str) or not isinstance(title, str) or not title.strip():
                continue
            path = "/".join(part.strip() for part in path.removeprefix("/").split("/"))
            try:
                api.folder_path(path)
            except api.ClientError:
                continue
            if path.rpartition("/")[0] == parent and path.split("/", 1)[0] != "Корзина":
                yield path, title, item.get("nodeType"), item.get("id")

    def list_fund_resources(self, parent: str, query: str, limit: int) -> FundListing:
        pending = [parent]
        queued = {parent}
        skipped = []
        found = {}
        scanned = 0
        for folder in pending:
            if scanned + len(skipped) >= 16:
                break
            try:
                items = sorted(self.fund_items(folder), key=lambda row: (row[0].casefold(), row[0]))
            except Fault as exc:
                if folder == parent or exc.code != "UPSTREAM_ERROR":
                    raise
                skipped.append(folder)
                continue
            scanned += 1
            for path, title, kind, resource_id in items:
                if kind == "folder" and path not in queued:
                    queued.add(path)
                    pending.append(path)
                elif (kind == "storage unit" and isinstance(resource_id, str) and resource_id.isdecimal()
                      and query.casefold() in f"{title} {path}".casefold()):
                    found[path] = Resource(title=title, path=path, resource_id=resource_id)
        resources = sorted(found.values(), key=lambda row: (row.path.casefold(), row.path))
        return FundListing(parent=parent, resources=resources[:limit], scanned_folders=scanned,
                           total_matches=len(resources), skipped_folders=skipped,
                           truncated=len(resources) > limit or scanned + len(skipped) < len(pending))

    def resource_for_read(self, path: str) -> Resource:
        resource = next((row for row in self.catalog if row.path == path), None)
        if resource is not None:
            return resource
        if self.mode == "live" and self.fund_session is not None:
            try:
                api.folder_path(path)
            except api.ClientError as exc:
                raise Fault("INVALID_ARGUMENT", str(exc), "Укажите путь из каталога или фонда.") from None
            matches = [Resource(title=title, path=actual, resource_id=resource_id)
                       for actual, title, kind, resource_id in self.fund_items(path.rpartition("/")[0])
                       if actual == path and kind == "storage unit"
                       and isinstance(resource_id, str) and resource_id.isdecimal()]
            if len(matches) == 1:
                return matches[0]
        raise Fault("RESOURCE_NOT_CONFIGURED", "Ресурс не найден по точному пути.",
                    "Используйте list_inforesources или list_fund_resources.")

    @staticmethod
    def match_export(resource: Resource, raw: dict) -> Resource:
        code = raw.get("code")
        if (not isinstance(code, (str, int)) or isinstance(code, bool) or not str(code)
                or raw.get("title") != resource.title
                or str(code) != resource.resource_id):
            raise Fault("RESOURCE_MISMATCH", "Ответ API не соответствует выбранному ресурсу.",
                        "Сверьте путь и название ресурса в каталоге или фонде.")
        return resource

    def folder_headers(self):
        if self.mode != "live":
            raise Fault("LIVE_REQUIRED", "Операции с каталогами доступны только в live-режиме.", "Запустите сервер с --mode live и войдите в IACPaaS.")
        try:
            return self.headers if self.headers is not None else api.auth_headers(interactive=False)
        except api.ClientError:
            raise Fault("AUTH_REQUIRED", "Нужна авторизация IACPaaS.", "Войдите в терминале; не передавайте пароль в чате.") from None

    def create_folder(self, parent: str, name: str, description: str = "") -> FolderChange:
        try:
            api.folder_path(parent, allow_root=True)
            api.folder_path(name)
            if "/" in name or len(name) > 200 or len(description) > 2000:
                raise api.ClientError("Имя — один компонент до 200 символов; описание — до 2000.")
            path = api.folder_path(f"{parent}/{name}" if parent else name)
        except api.ClientError as exc:
            raise Fault("INVALID_ARGUMENT", str(exc), "Укажите родительскую папку и отдельное имя нового каталога.") from None
        headers = self.folder_headers()
        try:
            api.create_folder(parent, name, description=description, headers=headers)
        except api.ClientError as exc:
            raise Fault("UPSTREAM_ERROR", str(exc), "Проверьте папку перед повтором: при сетевом сбое операция могла выполниться.") from None
        return FolderChange(path=path, operation="created")

    def delete_folder(self, path: str, confirm_path: str) -> FolderChange:
        try:
            api.folder_path(path)
        except api.ClientError as exc:
            raise Fault("INVALID_ARGUMENT", str(exc), "Нужен точный путь удаляемой папки.") from None
        if confirm_path != path:
            raise Fault("CONFIRMATION_REQUIRED", "Подтверждённый путь не совпадает с удаляемым.", "Уточните путь; confirm_path должен точно совпадать с path.")
        if (path == "Загрузки" or path.split("/", 1)[0] == "Корзина"
                or any(r.path == path or r.path.startswith(path + "/") for r in self.catalog)):
            raise Fault("PROTECTED_PATH", "Этот путь защищён от удаления.", "Выберите отдельную учебную папку без настроенных ресурсов.")
        if self.mode != "live":
            self.folder_headers()  # Preserve the explicit LIVE_REQUIRED error in snapshot mode.
        if self.fund_session is None:
            raise Fault("FUND_LOGIN_REQUIRED", "Для перемещения папки в корзину нужна веб-сессия IACPaaS.",
                        "Запустите MCP по HTTP с --login --fund-login.")
        try:
            self.fund_session.delete_folder(path)
        except api.ClientError as exc:
            raise Fault("UPSTREAM_ERROR", str(exc),
                        "Проверьте исходную папку и корзину перед повтором операции.") from None
        return FolderChange(path=path, operation="moved_to_trash")

    def read(self, selection: Selection) -> Graph:
        resource = self.resource_for_read(selection.path)
        if self.mode == "snapshot":
            if selection.depth != 0 or selection.start:
                raise Fault("SNAPSHOT_SCOPE", "Сохранён только корень с depth=0.", "Для подграфа запустите live-режим; не считайте отсутствие потомков свойством полного графа.")
            raw = api.normalize_export(json.loads((BASE / "fixtures/root.json").read_text(encoding="utf-8")))
            if str(raw.get("code")) != resource.resource_id:
                raise Fault("SNAPSHOT_SCOPE", "Для этого ресурса снимок ещё не сохранён.", "Используйте live-режим; существующий снимок относится только к системе единиц.")
        else:
            try:
                headers = self.headers if self.headers is not None else api.auth_headers(interactive=False)
            except api.ClientError:
                raise Fault("AUTH_REQUIRED", "Серверу нужны учётные данные IACPaaS.", "Запустите HTTP-сервер с --login либо задайте IACPAAS_TOKEN/IACPAAS_API_KEY в окружении процесса сервера.") from None
            try:
                raw = self.exporter(resource.path, depth=selection.depth, start=selection.start or None, headers=headers)
            except api.ExportFormatError:
                raise Fault("EXPORT_FORMAT", "Ответ API не распознан как universal.", "Проверьте контракт API; ответ не записывается в MCP-журнал.") from None
            except api.ClientError as exc:
                # The stage-1 client emits only curated messages, never response bodies.
                raise Fault("UPSTREAM_ERROR", str(exc), "Проверьте доступ к ресурсу, срок токена и ограничения запроса.") from None
        resource = self.match_export(resource, raw)
        root, counts = project_graph(raw, selection.max_nodes)
        graph = Graph(resource=resource, source="live_api" if self.mode == "live" else "saved_snapshot",
                      ontology=raw.get("ontology"),
                      requested_depth=selection.depth, requested_start=selection.start, root=root, counts=counts,
                      warnings=["Экспорт ограничен глубиной; статистика относится только к полученному фрагменту.",
                                "При start API также возвращает предков. Ссылки не разворачиваются и не считаются новыми уникальными вершинами."])
        if len(graph.model_dump_json().encode("utf-8")) > MAX_RESULT_BYTES:
            raise Fault("RESULT_LIMIT", "Результат больше 128 КБ.", "Уменьшите depth или выберите меньшую ветку через start.")
        return graph

    def count(self, selection: Selection) -> CountReport:
        graph = self.read(selection)
        return CountReport(**graph.model_dump(exclude={"root", "complete_graph"}))


    def search_raw(self, path: str, depth: int):
        resource = self.resource_for_read(path)
        if self.mode == "snapshot":
            if depth != 0:
                raise Fault("SNAPSHOT_SCOPE", "В snapshot доступен только корень.", "Укажите depth=0 или используйте live.")
            raw = api.normalize_export(json.loads((BASE / "fixtures/root.json").read_text(encoding="utf-8")))
        else:
            headers = self.folder_headers()
            try:
                raw = self.exporter(path, depth=depth, start=None, headers=headers)
            except api.ClientError:
                raise Fault("UPSTREAM_ERROR", "Не удалось получить поисковый экспорт.", "Проверьте доступ; при превышении 2 МБ уменьшите depth. Не делайте вывод об отсутствии сведений.") from None
        return self.match_export(resource, raw), raw

    def search(self, path: str, query: str, depth: int = 10, limit: int = 20) -> SearchReport:
        from retrieval import search_export
        resource, raw = self.search_raw(path, depth)
        return search_export(resource, raw, query, depth, limit,
                             "live_api" if self.mode == "live" else "saved_snapshot")

    def search_fund_content(self, parent: str, queries: list[str], depth: int = 10) -> FundSearchReport:
        from retrieval import bounded, index_export, normalize, search_index
        if any(not normalize(query) for query in queries):
            raise Fault("INVALID_ARGUMENT", "Поисковые слова должны быть непустыми.", "Передайте отдельные основы слов.")
        queries = list({normalize(query): query for query in queries}.values())
        listing = self.list_fund_resources(parent, "", 12)
        headers = self.folder_headers()

        def scan(resource):
            try:
                raw = self.exporter(resource.path, depth=depth, start=None, headers=headers)
                self.match_export(resource, raw)
                rows, digest = index_export(raw)
                reports = [search_index(resource, rows, digest, query, depth, 2, "live_api", child_limit=3)
                           for query in queries]
                return FundSearchResult(resource=resource, reports=reports)
            except api.ClientError as exc:
                error = Error(code="UPSTREAM_ERROR", message=str(exc),
                              hint="Проверьте права и размер экспорта. Эта база не была проверена.")
            except Fault as exc:
                error = Error(code=exc.code, message=exc.message, hint=exc.hint)
            return FundSearchResult(resource=resource, reports=[], error=error)

        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(scan, listing.resources))
        return bounded(FundSearchReport(resources=listing.resources, queries=queries, results=results,
                                        scanned_folders=listing.scanned_folders,
                                        skipped_folders=listing.skipped_folders, truncated=listing.truncated))

    def compare_children(self, path: str, left_pointer: str, right_pointer: str,
                         export_sha256: str, depth: int = 10) -> ComparisonReport:
        from retrieval import compare_export
        resource, raw = self.search_raw(path, depth)
        return compare_export(resource, raw, left_pointer, right_pointer, export_sha256, depth,
                              "live_api" if self.mode == "live" else "saved_snapshot")

    def rank_percentages(self, path: str, group_pointer: str, composition_path: list[str],
                         percentage_path: list[str], percent_basis: str,
                         export_sha256: str, depth: int, limit: int) -> RankingReport:
        from retrieval import rank_export
        if self.mode != "live":
            raise Fault("LIVE_REQUIRED", "Рейтинг требует живого экспорта IACPaaS.", "Подключите live MCP.")
        resource, raw = self.search_raw(path, depth)
        return rank_export(resource, raw, group_pointer, composition_path, percentage_path,
                           percent_basis, export_sha256, depth, limit)

    def gas_composition(self, path: str, base: str, compare_with: str, limit: int) -> GasCompositionReport:
        from gas_composition import analyze_gas_export
        if self.mode != "live":
            raise Fault("LIVE_REQUIRED", "Для состава газов нужен живой экспорт IACPaaS.", "Подключите live MCP.")
        resource, raw = self.search_raw(path, 10)
        return analyze_gas_export(resource, raw, base, compare_with, limit)
