"""Catalog resolution, durable review queue, and interactive approval for KitchenOps."""

from __future__ import annotations

import copy
import difflib
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import requests
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table


QUEUE_VERSION = 1
CATALOG_KINDS = ("food", "unit")


def normalize_name(value: Any) -> str:
    """Normalize catalog lookup text without collapsing meaningful punctuation."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return re.sub(r"\s+", " ", text)


def catalog_key(kind: str, name: str) -> str:
    return f"{kind}:{normalize_name(name)}"


def _aliases(item: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for alias in item.get("aliases") or []:
        name = alias.get("name") if isinstance(alias, dict) else alias
        if name:
            values.append(str(name))
    return values


class CatalogIndex:
    """Canonical and alternate-name indexes for Mealie foods and units."""

    def __init__(self) -> None:
        self.items: dict[str, list[dict[str, Any]]] = {kind: [] for kind in CATALOG_KINDS}
        self.canonical: dict[str, dict[str, list[dict[str, Any]]]] = {
            kind: {} for kind in CATALOG_KINDS
        }
        self.alternates: dict[str, dict[str, list[dict[str, Any]]]] = {
            kind: {} for kind in CATALOG_KINDS
        }

    def replace(self, kind: str, items: Iterable[dict[str, Any]]) -> None:
        if kind not in CATALOG_KINDS:
            raise ValueError(f"Unsupported catalog kind: {kind}")
        records = [copy.deepcopy(item) for item in items if item.get("id") and item.get("name")]
        self.items[kind] = records
        self.canonical[kind] = {}
        self.alternates[kind] = {}
        for item in records:
            self._add(self.canonical[kind], item.get("name"), item)
            alternate_names = [item.get("pluralName"), *_aliases(item)]
            if kind == "unit":
                alternate_names.extend(
                    [item.get("abbreviation"), item.get("pluralAbbreviation")]
                )
            for name in alternate_names:
                self._add(self.alternates[kind], name, item)

    @staticmethod
    def _add(index: dict[str, list[dict[str, Any]]], name: Any, item: dict[str, Any]) -> None:
        key = normalize_name(name)
        if not key:
            return
        values = index.setdefault(key, [])
        if not any(existing["id"] == item["id"] for existing in values):
            values.append(item)

    def resolve(self, kind: str, name: str) -> tuple[Optional[dict[str, Any]], bool]:
        """Return a unique match and whether the lookup is ambiguous."""
        key = normalize_name(name)
        canonical = self.canonical[kind].get(key, [])
        if len(canonical) == 1:
            return copy.deepcopy(canonical[0]), False
        if len(canonical) > 1:
            return None, True
        alternates = self.alternates[kind].get(key, [])
        if len(alternates) == 1:
            return copy.deepcopy(alternates[0]), False
        return None, len(alternates) > 1

    def by_id(self, kind: str, item_id: str) -> Optional[dict[str, Any]]:
        for item in self.items[kind]:
            if item.get("id") == item_id:
                return copy.deepcopy(item)
        return None

    def search(self, kind: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        normalized_query = normalize_name(query)
        ranked: list[tuple[float, str, dict[str, Any]]] = []
        for item in self.items[kind]:
            names = [item.get("name"), item.get("pluralName"), *_aliases(item)]
            if kind == "unit":
                names.extend([item.get("abbreviation"), item.get("pluralAbbreviation")])
            normalized_names = [normalize_name(name) for name in names if name]
            substring = any(normalized_query and normalized_query in name for name in normalized_names)
            score = max(
                (difflib.SequenceMatcher(None, normalized_query, name).ratio() for name in normalized_names),
                default=0.0,
            )
            if substring:
                score += 1.0
            ranked.append((score, normalize_name(item["name"]), item))
        ranked.sort(key=lambda row: (-row[0], row[1]))
        return [copy.deepcopy(row[2]) for row in ranked[:limit] if row[0] > 0]


class CatalogApi:
    """Small Mealie API adapter used by parser and review workflows."""

    def __init__(self, base_url: str, token: str, session: Optional[requests.Session] = None):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )

    @staticmethod
    def endpoint(kind: str) -> str:
        return "foods" if kind == "food" else "units"

    @staticmethod
    def _require_success(response: requests.Response, operation: str) -> dict[str, Any]:
        if not 200 <= response.status_code < 300:
            detail = response.text[:500] if getattr(response, "text", None) else ""
            raise RuntimeError(f"{operation} failed ({response.status_code}): {detail}")
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"{operation} returned an unexpected response")
        return data

    def list_items(self, kind: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self.session.get(
                f"{self.base_url}/api/{self.endpoint(kind)}",
                params={"page": page, "perPage": 2000},
                timeout=15,
            )
            data = self._require_success(response, f"list {kind}s")
            page_items = data.get("items") or []
            items.extend(page_items)
            if not page_items or not data.get("next"):
                break
            page += 1
        return items

    def refresh(self, index: CatalogIndex, kind: Optional[str] = None) -> None:
        kinds = (kind,) if kind else CATALOG_KINDS
        for current_kind in kinds:
            index.replace(current_kind, self.list_items(current_kind))

    def get_item(self, kind: str, item_id: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}/api/{self.endpoint(kind)}/{item_id}", timeout=15
        )
        return self._require_success(response, f"get {kind}")

    def create_item(self, kind: str, proposal: dict[str, Any]) -> dict[str, Any]:
        allowed = (
            ("name", "pluralName", "description", "aliases")
            if kind == "food"
            else (
                "name",
                "pluralName",
                "description",
                "fraction",
                "abbreviation",
                "pluralAbbreviation",
                "useAbbreviation",
                "aliases",
            )
        )
        payload = {key: proposal[key] for key in allowed if key in proposal and proposal[key] is not None}
        response = self.session.post(
            f"{self.base_url}/api/{self.endpoint(kind)}", json=payload, timeout=15
        )
        return self._require_success(response, f"create {kind}")

    def add_alias(self, kind: str, item_id: str, alias_name: str) -> dict[str, Any]:
        latest = self.get_item(kind, item_id)
        alias_key = normalize_name(alias_name)
        existing_names = {normalize_name(name) for name in _aliases(latest)}
        canonical_names = {
            normalize_name(latest.get("name")),
            normalize_name(latest.get("pluralName")),
        }
        if kind == "unit":
            canonical_names.update(
                {
                    normalize_name(latest.get("abbreviation")),
                    normalize_name(latest.get("pluralAbbreviation")),
                }
            )
        if alias_key and alias_key not in existing_names | canonical_names:
            latest["aliases"] = [*(latest.get("aliases") or []), {"name": alias_name}]
            response = self.session.put(
                f"{self.base_url}/api/{self.endpoint(kind)}/{item_id}",
                json=latest,
                timeout=15,
            )
            latest = self._require_success(response, f"add {kind} alias")
        return latest

    def get_recipe(self, slug: str) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}/api/recipes/{slug}", timeout=15)
        return self._require_success(response, "get recipe")

    def update_recipe(self, slug: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.put(
            f"{self.base_url}/api/recipes/{slug}", json=payload, timeout=20
        )
        return self._require_success(response, "update recipe")


class PendingCatalogQueue:
    """Versioned durable queue of recipes blocked on catalog references."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {
            "version": QUEUE_VERSION,
            "recipes": {},
            "resolutions": {},
        }
        self.corrupt_backup: Optional[Path] = None

    def load(self) -> "PendingCatalogQueue":
        if not self.path.exists():
            return self
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if loaded.get("version") != QUEUE_VERSION or not isinstance(loaded.get("recipes"), dict):
                raise ValueError("unsupported or invalid queue schema")
            loaded.setdefault("resolutions", {})
            self.data = loaded
        except (OSError, ValueError, json.JSONDecodeError):
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            backup = self.path.with_name(f"{self.path.name}.corrupt-{timestamp}")
            os.replace(self.path, backup)
            self.corrupt_backup = backup
        return self

    @property
    def recipes(self) -> dict[str, dict[str, Any]]:
        return self.data["recipes"]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def upsert_recipe(self, record: dict[str, Any]) -> None:
        self.recipes[record["slug"]] = copy.deepcopy(record)

    def remove_recipe(self, slug: str) -> None:
        self.recipes.pop(slug, None)
        self._prune_resolutions()

    def _prune_resolutions(self) -> None:
        used = {
            catalog_key(missing["kind"], missing.get("name", ""))
            for recipe in self.recipes.values()
            for missing in recipe.get("missing", [])
        }
        self.data["resolutions"] = {
            key: value for key, value in self.data.get("resolutions", {}).items() if key in used
        }

    def set_resolution(self, kind: str, source_name: str, item: dict[str, Any]) -> None:
        self.data["resolutions"][catalog_key(kind, source_name)] = {
            "id": item["id"],
            "name": item["name"],
            "pluralName": item.get("pluralName"),
        }

    def resolution(
        self, kind: str, source_name: str, index: CatalogIndex
    ) -> tuple[Optional[dict[str, Any]], bool]:
        saved = self.data.get("resolutions", {}).get(catalog_key(kind, source_name))
        if saved:
            current = index.by_id(kind, saved.get("id", ""))
            if current:
                return current, False
        return index.resolve(kind, source_name)

    def entries(self, index: CatalogIndex) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for recipe in self.recipes.values():
            for missing in recipe.get("missing", []):
                kind = missing["kind"]
                name = missing.get("name", "")
                resolved, _ = self.resolution(kind, name, index)
                if resolved:
                    continue
                key = catalog_key(kind, name)
                entry = grouped.setdefault(
                    key,
                    {
                        "key": key,
                        "kind": kind,
                        "name": name,
                        "proposal": copy.deepcopy(missing.get("proposal") or {"name": name}),
                        "ambiguous": bool(missing.get("ambiguous")),
                        "occurrences": [],
                    },
                )
                entry["ambiguous"] = entry["ambiguous"] or bool(missing.get("ambiguous"))
                entry["occurrences"].append(
                    {
                        "slug": recipe["slug"],
                        "ingredientIndex": missing.get("ingredientIndex"),
                        "raw": missing.get("raw", ""),
                    }
                )
        return sorted(grouped.values(), key=lambda item: (item["kind"], normalize_name(item["name"])))


def item_reference(item: dict[str, Any]) -> dict[str, Any]:
    return {"id": item["id"], "name": item["name"]}


def replay_ready_recipes(
    queue: PendingCatalogQueue,
    index: CatalogIndex,
    api: CatalogApi,
    history: set[str],
    logger: Optional[Callable[[str], None]] = None,
) -> dict[str, int]:
    """Apply stored proposals for recipes whose references are now all resolved."""
    stats = {"updated": 0, "waiting": 0, "stale": 0, "failed": 0}
    for slug, record in list(queue.recipes.items()):
        resolved_by_key: dict[str, dict[str, Any]] = {}
        blocked = False
        for missing in record.get("missing", []):
            item, ambiguous = queue.resolution(missing["kind"], missing.get("name", ""), index)
            if not item or ambiguous:
                blocked = True
                break
            resolved_by_key[catalog_key(missing["kind"], missing.get("name", ""))] = item
        if blocked:
            stats["waiting"] += 1
            continue

        try:
            current = api.get_recipe(slug)
            if current.get("recipeIngredient", []) != record.get("sourceIngredients", []):
                stats["stale"] += 1
                queue.remove_recipe(slug)
                history.discard(slug)
                if logger:
                    logger(f"STALE: {slug} changed after parsing; queued proposal discarded")
                continue

            proposed = copy.deepcopy(record.get("proposedIngredients", []))
            for missing in record.get("missing", []):
                replacement = resolved_by_key[catalog_key(missing["kind"], missing.get("name", ""))]
                proposed[missing["ingredientIndex"]][missing["kind"]] = item_reference(replacement)
            current["recipeIngredient"] = proposed
            api.update_recipe(slug, current)
            queue.remove_recipe(slug)
            history.add(slug)
            stats["updated"] += 1
            if logger:
                logger(f"OK after catalog review: {slug}")
        except Exception as exc:  # keep durable work for later retry
            stats["failed"] += 1
            if logger:
                logger(f"RETRY FAIL: {slug} — {exc}")
    queue.save()
    return stats


@dataclass
class CatalogReviewer:
    queue: PendingCatalogQueue
    index: CatalogIndex
    api: CatalogApi
    console: Console

    def refresh(self, kind: Optional[str] = None) -> None:
        self.api.refresh(self.index, kind)

    def create(self, entry: dict[str, Any], proposal: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        proposal = copy.deepcopy(proposal or entry["proposal"])
        self.refresh(entry["kind"])
        existing, ambiguous = self.index.resolve(entry["kind"], proposal.get("name", ""))
        if ambiguous:
            raise RuntimeError("Catalog name or alias is ambiguous; edit the proposal or map it explicitly")
        if existing:
            item = existing
        else:
            item = self.api.create_item(entry["kind"], proposal)
            self.refresh(entry["kind"])
        self.queue.set_resolution(entry["kind"], entry["name"], item)
        self.queue.save()
        return item

    def map(self, entry: dict[str, Any], item: dict[str, Any], add_alias: bool = True) -> dict[str, Any]:
        self.refresh(entry["kind"])
        current = self.index.by_id(entry["kind"], item["id"])
        if not current:
            raise RuntimeError("Selected catalog item no longer exists")
        if add_alias:
            existing, ambiguous = self.index.resolve(entry["kind"], entry["name"])
            if ambiguous:
                raise RuntimeError("Alias became ambiguous; no catalog item was changed")
            if existing and existing["id"] != current["id"]:
                raise RuntimeError(
                    f"Alias now belongs to {existing['name']}; no catalog item was changed"
                )
            if not existing:
                current = self.api.add_alias(entry["kind"], current["id"], entry["name"])
                self.refresh(entry["kind"])
        self.queue.set_resolution(entry["kind"], entry["name"], current)
        self.queue.save()
        return current

    def choose_mapping(self, entry: dict[str, Any]) -> Optional[dict[str, Any]]:
        suggestions = self.index.search(entry["kind"], entry["name"], limit=5)
        if suggestions:
            self._candidate_table(suggestions)
        query = Prompt.ask("Search catalog", default=entry["name"], console=self.console)
        candidates = self.index.search(entry["kind"], query, limit=10)
        if not candidates:
            self.console.print("[warning]No catalog matches found.[/warning]")
            return None
        self._candidate_table(candidates)
        choice = Prompt.ask(
            f"Select 1-{len(candidates)} (blank to cancel)", default="", console=self.console
        ).strip()
        if not choice.isdigit() or not 1 <= int(choice) <= len(candidates):
            return None
        return candidates[int(choice) - 1]

    def _candidate_table(self, candidates: list[dict[str, Any]]) -> None:
        table = Table(title="Catalog candidates")
        table.add_column("#", justify="right")
        table.add_column("Name")
        table.add_column("Aliases")
        for index, item in enumerate(candidates, 1):
            table.add_row(str(index), item["name"], ", ".join(_aliases(item)[:4]))
        self.console.print(table)

    def _show_entry(self, entry: dict[str, Any], number: int, total: int) -> None:
        proposal = entry["proposal"]
        self.console.rule(f"[bold cyan]{entry['kind'].title()} {number}/{total}: {entry['name']}[/bold cyan]")
        self.console.print(
            f"Used by [cyan]{len(entry['occurrences'])}[/cyan] recipe ingredient(s)"
        )
        fields = ["pluralName", "abbreviation", "pluralAbbreviation"]
        details = [f"{field}={proposal[field]!r}" for field in fields if proposal.get(field)]
        if details:
            self.console.print("Proposal: " + ", ".join(details))
        for occurrence in entry["occurrences"][:3]:
            self.console.print(f"  • {occurrence['slug']}: {occurrence['raw']}")
        suggestions = self.index.search(entry["kind"], entry["name"], limit=5)
        if suggestions:
            self.console.print("Closest existing: " + ", ".join(item["name"] for item in suggestions))

    def _edit_proposal(self, entry: dict[str, Any]) -> dict[str, Any]:
        proposal = copy.deepcopy(entry["proposal"])
        proposal["name"] = Prompt.ask("Name", default=proposal.get("name") or entry["name"], console=self.console)
        proposal["pluralName"] = Prompt.ask(
            "Plural name", default=proposal.get("pluralName") or "", console=self.console
        ) or None
        if entry["kind"] == "unit":
            proposal["abbreviation"] = Prompt.ask(
                "Abbreviation", default=proposal.get("abbreviation") or "", console=self.console
            )
            proposal["pluralAbbreviation"] = Prompt.ask(
                "Plural abbreviation",
                default=proposal.get("pluralAbbreviation") or "",
                console=self.console,
            ) or None
        return proposal

    def review(self) -> int:
        entries = self.queue.entries(self.index)
        total = len(entries)
        if not entries:
            self.console.print("[success]No unresolved catalog items.[/success]")
            return 0
        position = 0
        failures = 0
        while position < len(entries):
            entry = entries[position]
            self._show_entry(entry, position + 1, total)
            action = Prompt.ask(
                "[c]reate, [e]dit/create, [m]ap+alias, [o]ne-time map, "
                "[s]kip, create [a]ll, [q]uit",
                choices=["c", "e", "m", "o", "s", "a", "q"],
                default="s",
                console=self.console,
            )
            try:
                if action == "q":
                    break
                if action == "s":
                    position += 1
                    continue
                if action == "c":
                    created = self.create(entry)
                    self.console.print(f"[success]Resolved as {created['name']}.[/success]")
                elif action == "e":
                    created = self.create(entry, self._edit_proposal(entry))
                    self.console.print(f"[success]Resolved as {created['name']}.[/success]")
                elif action in ("m", "o"):
                    selected = self.choose_mapping(entry)
                    if not selected:
                        continue
                    mapped = self.map(entry, selected, add_alias=action == "m")
                    suffix = " and added alias" if action == "m" else " for this queue only"
                    self.console.print(f"[success]Mapped to {mapped['name']}{suffix}.[/success]")
                elif action == "a":
                    remaining = entries[position:]
                    safe = [item for item in remaining if not item.get("ambiguous") and item.get("name")]
                    if Confirm.ask(
                        f"Create {len(safe)} remaining non-conflicting catalog items?",
                        default=False,
                        console=self.console,
                    ):
                        for bulk_entry in safe:
                            self.create(bulk_entry)
                        self.console.print(f"[success]Created/resolved {len(safe)} catalog items.[/success]")
                        break
                    continue
                position += 1
            except Exception as exc:
                failures += 1
                self.console.print(f"[error]Catalog action failed: {exc}[/error]")
        self.queue.save()
        return failures


def pending_summary(console: Console, queue: PendingCatalogQueue, index: CatalogIndex) -> None:
    entries = queue.entries(index)
    table = Table(title="Pending catalog review")
    table.add_column("Type")
    table.add_column("Proposed name")
    table.add_column("Occurrences", justify="right")
    table.add_column("Examples")
    for entry in entries:
        examples = ", ".join(item["slug"] for item in entry["occurrences"][:3])
        table.add_row(entry["kind"], entry["name"] or "<missing name>", str(len(entry["occurrences"])), examples)
    console.print(table)
