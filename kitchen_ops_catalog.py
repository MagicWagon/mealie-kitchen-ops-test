"""Catalog resolution, durable review queue, and interactive approval for KitchenOps."""

from __future__ import annotations

import copy
import concurrent.futures
import json
import os
import queue as thread_queue
import re
import tempfile
import threading
import unicodedata
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import requests
import yaml
from rapidfuzz import fuzz
from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm, Prompt
from rich.table import Table


QUEUE_VERSION = 1
CATALOG_KINDS = ("food", "unit")


def _load_tool_matches() -> dict[str, list[str]]:
    config_path = Path(__file__).resolve().parent / "config" / "tagging.yaml"
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        configured = data.get("tools_matches") or {}
        return {
            str(name): [str(value) for value in values if value]
            for name, values in configured.items()
            if name and isinstance(values, list)
        }
    except (OSError, yaml.YAMLError):
        return {}


TOOL_MATCHES = _load_tool_matches()
_LEADING_AMOUNT = re.compile(
    r"^\s*(?:\d|[¼½¾⅓⅔⅛⅜⅝⅞]|\d+\s*/\s*\d+|one\b|two\b|three\b|four\b)",
    re.I,
)
_PROSE_SIGNAL = re.compile(
    r"\b(?:i|we|you|your|my|our|use|using|used|need|prefer|recommend|"
    r"note|equipment|make sure|be sure)\b",
    re.I,
)
_NOTE_PREFIX = re.compile(
    r"^\s*(?:note|equipment|for serving|for garnish|to serve)\s*[:.-]?\s*",
    re.I,
)
_INSTRUCTION_PREFIX = re.compile(
    r"^\s*(?:add|stir|mix|cook|bake|preheat|let|place|set|see|reserve|keep)\b",
    re.I,
)


def line_key(raw: Any) -> str:
    return f"line:{normalize_name(raw)}"


def classify_review_line(raw: Any) -> Optional[dict[str, Any]]:
    """Conservatively identify prose and equipment without deciding for the user."""
    text = str(raw or "").strip()
    normalized = normalize_name(text)
    if not normalized:
        return None
    has_amount = bool(_LEADING_AMOUNT.search(text))
    prose = bool(_PROSE_SIGNAL.search(text))
    tool_names: list[str] = []
    for tool_name, phrases in TOOL_MATCHES.items():
        candidates = [tool_name, *phrases]
        if any(
            re.search(rf"(?<!\w){re.escape(normalize_name(phrase))}(?!\w)", normalized)
            for phrase in candidates
            if normalize_name(phrase)
        ):
            tool_names.append(tool_name)
    exact_tool = any(
        normalized.strip(" .:;-")
        in {normalize_name(name), *(normalize_name(value) for value in TOOL_MATCHES.get(name, []))}
        for name in tool_names
    )
    if tool_names and has_amount:
        return {
            "raw": text,
            "normalizedRaw": normalized,
            "recommendation": "ingredient",
            "toolMatches": tool_names,
            "reasons": [
                "mentions configured equipment but starts like an ingredient line"
            ],
        }
    if tool_names and (exact_tool or prose):
        return {
            "raw": text,
            "normalizedRaw": normalized,
            "recommendation": "equipment",
            "toolMatches": tool_names,
            "reasons": ["matched configured equipment phrase"],
        }
    word_count = len(re.findall(r"\b\w+\b", text))
    sentence_like = (
        prose
        or bool(_NOTE_PREFIX.search(text))
        or (word_count >= 4 and bool(_INSTRUCTION_PREFIX.search(text)))
        or (word_count >= 8 and bool(re.search(r"[.!?]\s*$", text)))
    )
    if not has_amount and sentence_like:
        return {
            "raw": text,
            "normalizedRaw": normalized,
            "recommendation": "note",
            "toolMatches": [],
            "reasons": ["looks like prose rather than a measured ingredient"],
        }
    return None


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
        self.items_by_id: dict[str, dict[str, dict[str, Any]]] = {
            kind: {} for kind in CATALOG_KINDS
        }
        self.canonical: dict[str, dict[str, list[dict[str, Any]]]] = {
            kind: {} for kind in CATALOG_KINDS
        }
        self.alternates: dict[str, dict[str, list[dict[str, Any]]]] = {
            kind: {} for kind in CATALOG_KINDS
        }
        self.search_names: dict[str, dict[str, list[str]]] = {
            kind: {} for kind in CATALOG_KINDS
        }
        self.generation: dict[str, int] = {kind: 0 for kind in CATALOG_KINDS}
        self.search_cache: dict[tuple[str, str, int, int], list[dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def replace(self, kind: str, items: Iterable[dict[str, Any]]) -> None:
        if kind not in CATALOG_KINDS:
            raise ValueError(f"Unsupported catalog kind: {kind}")
        records = [copy.deepcopy(item) for item in items if item.get("id") and item.get("name")]
        with self._lock:
            self.items[kind] = records
            self.items_by_id[kind] = {item["id"]: item for item in records}
            self.canonical[kind] = {}
            self.alternates[kind] = {}
            self.search_names[kind] = {}
            for item in records:
                self._index_item(kind, item)
            self._bump_generation(kind)

    def _index_item(self, kind: str, item: dict[str, Any]) -> None:
        self._add(self.canonical[kind], item.get("name"), item)
        alternate_names = [item.get("pluralName"), *_aliases(item)]
        if kind == "unit":
            alternate_names.extend(
                [item.get("abbreviation"), item.get("pluralAbbreviation")]
            )
        for name in alternate_names:
            self._add(self.alternates[kind], name, item)
        names = [item.get("name"), *alternate_names]
        self.search_names[kind][item["id"]] = [
            normalize_name(name) for name in names if normalize_name(name)
        ]

    @staticmethod
    def _remove(index: dict[str, list[dict[str, Any]]], name: Any, item_id: str) -> None:
        key = normalize_name(name)
        if not key or key not in index:
            return
        remaining = [item for item in index[key] if item.get("id") != item_id]
        if remaining:
            index[key] = remaining
        else:
            index.pop(key, None)

    def _unindex_item(self, kind: str, item: dict[str, Any]) -> None:
        item_id = item["id"]
        self._remove(self.canonical[kind], item.get("name"), item_id)
        alternate_names = [item.get("pluralName"), *_aliases(item)]
        if kind == "unit":
            alternate_names.extend(
                [item.get("abbreviation"), item.get("pluralAbbreviation")]
            )
        for name in alternate_names:
            self._remove(self.alternates[kind], name, item_id)
        self.search_names[kind].pop(item_id, None)

    def _bump_generation(self, kind: str) -> None:
        self.generation[kind] += 1
        self.search_cache = {
            key: value for key, value in self.search_cache.items() if key[0] != kind
        }

    def upsert(self, kind: str, item: dict[str, Any]) -> None:
        """Incrementally add or replace one catalog item and invalidate search caches."""
        if kind not in CATALOG_KINDS or not item.get("id") or not item.get("name"):
            raise ValueError("Catalog item requires a supported kind, id, and name")
        record = copy.deepcopy(item)
        with self._lock:
            previous = self.items_by_id[kind].get(record["id"])
            if previous:
                self._unindex_item(kind, previous)
                self.items[kind] = [
                    record if current.get("id") == record["id"] else current
                    for current in self.items[kind]
                ]
            else:
                self.items[kind].append(record)
            self.items_by_id[kind][record["id"]] = record
            self._index_item(kind, record)
            self._bump_generation(kind)

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
        with self._lock:
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
        with self._lock:
            item = self.items_by_id[kind].get(item_id)
            return copy.deepcopy(item) if item else None

    def search(self, kind: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        normalized_query = normalize_name(query)
        with self._lock:
            cache_key = (kind, normalized_query, limit, self.generation[kind])
            cached = self.search_cache.get(cache_key)
            if cached is not None:
                return copy.deepcopy(cached)
            ranked: list[tuple[float, str, dict[str, Any]]] = []
            for item_id, normalized_names in self.search_names[kind].items():
                substring = any(
                    normalized_query and normalized_query in name for name in normalized_names
                )
                score = max(
                    (fuzz.ratio(normalized_query, name) / 100.0 for name in normalized_names),
                    default=0.0,
                )
                if substring:
                    score += 1.0
                item = self.items_by_id[kind][item_id]
                ranked.append((score, normalize_name(item["name"]), item))
            ranked.sort(key=lambda row: (-row[0], row[1]))
            result = [copy.deepcopy(row[2]) for row in ranked[:limit] if row[0] > 0]
            self.search_cache[cache_key] = copy.deepcopy(result)
            return result


class ToolIndex:
    """Small normalized index for Mealie recipe tools."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.by_name: dict[str, dict[str, Any]] = {}
        self.by_id: dict[str, dict[str, Any]] = {}

    def replace(self, items: Iterable[dict[str, Any]]) -> None:
        self.items = [
            copy.deepcopy(item) for item in items if item.get("id") and item.get("name")
        ]
        self.by_name = {normalize_name(item["name"]): item for item in self.items}
        self.by_id = {str(item["id"]): item for item in self.items}

    def upsert(self, item: dict[str, Any]) -> None:
        if not item.get("id") or not item.get("name"):
            raise ValueError("Tool requires an id and name")
        record = copy.deepcopy(item)
        previous = self.by_id.get(str(record["id"]))
        if previous:
            self.items = [
                record if str(value.get("id")) == str(record["id"]) else value
                for value in self.items
            ]
            self.by_name.pop(normalize_name(previous.get("name")), None)
        else:
            self.items.append(record)
        self.by_id[str(record["id"])] = record
        self.by_name[normalize_name(record["name"])] = record

    def resolve(self, name: str) -> Optional[dict[str, Any]]:
        item = self.by_name.get(normalize_name(name))
        return copy.deepcopy(item) if item else None

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        normalized = normalize_name(query)
        ranked = sorted(
            self.items,
            key=lambda item: (
                -fuzz.ratio(normalized, normalize_name(item.get("name"))),
                normalize_name(item.get("name")),
            ),
        )
        return [copy.deepcopy(item) for item in ranked[:limit]]


class CatalogApi:
    """Small Mealie API adapter used by parser and review workflows."""

    def __init__(self, base_url: str, token: str, session: Optional[requests.Session] = None):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )

    def clone(self) -> "CatalogApi":
        """Create an API adapter with an independent requests session for a worker thread."""
        headers = dict(self.session.headers)
        session = requests.Session()
        cloned = CatalogApi(self.base_url, "", session=session)
        cloned.session.headers.update(headers)
        return cloned

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

    def list_tools(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self.session.get(
                f"{self.base_url}/api/organizers/tools",
                params={"page": page, "perPage": 500},
                timeout=15,
            )
            data = self._require_success(response, "list tools")
            page_items = data.get("items") or []
            items.extend(page_items)
            total_pages = data.get("total_pages") or data.get("totalPages")
            if not page_items or (total_pages and page >= int(total_pages)) or (
                not total_pages and len(page_items) < 500
            ):
                break
            page += 1
        return items

    def create_tool(self, name: str) -> dict[str, Any]:
        response = self.session.post(
            f"{self.base_url}/api/organizers/tools", json={"name": name}, timeout=15
        )
        if response.status_code == 409:
            match = next(
                (
                    item
                    for item in self.list_tools()
                    if normalize_name(item.get("name")) == normalize_name(name)
                ),
                None,
            )
            if match:
                return match
        return self._require_success(response, "create tool")

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
            "lineDispositions": {},
            "checkpointSequence": 0,
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
            loaded.setdefault("lineDispositions", {})
            loaded.setdefault("checkpointSequence", 0)
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
        self.write_snapshot(copy.deepcopy(self.data))

    def snapshot(self, checkpoint_sequence: Optional[int] = None) -> dict[str, Any]:
        snapshot = copy.deepcopy(self.data)
        if checkpoint_sequence is not None:
            snapshot["checkpointSequence"] = checkpoint_sequence
        return snapshot

    def write_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, ensure_ascii=False, indent=2, sort_keys=True)
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

    def line_disposition(self, raw: Any) -> Optional[dict[str, Any]]:
        disposition = self.data.get("lineDispositions", {}).get(normalize_name(raw))
        return copy.deepcopy(disposition) if disposition else None

    def set_line_disposition(
        self, raw: Any, disposition_type: str, tool_name: Optional[str] = None
    ) -> None:
        if disposition_type not in ("note", "equipment", "ingredient"):
            raise ValueError(f"Unsupported line disposition: {disposition_type}")
        value: dict[str, Any] = {
            "type": disposition_type,
            "raw": str(raw or ""),
            "decidedAt": datetime.now().isoformat(timespec="seconds"),
        }
        if disposition_type == "equipment":
            if not tool_name:
                raise ValueError("Equipment disposition requires a tool name")
            value["toolName"] = tool_name
        self.data.setdefault("lineDispositions", {})[normalize_name(raw)] = value

    def ensure_line_reviews(
        self, should_stop: Optional[Callable[[], bool]] = None
    ) -> bool:
        """Backfill conservative line reviews for existing version-1 queue records."""
        for recipe in self.recipes.values():
            if should_stop and should_stop():
                return False
            reviews = recipe.setdefault("lineReviews", [])
            existing = {
                (int(review.get("ingredientIndex", -1)), normalize_name(review.get("raw")))
                for review in reviews
            }
            for ingredient_index, raw_item in enumerate(recipe.get("sourceIngredients", [])):
                raw = (
                    raw_item
                    if isinstance(raw_item, str)
                    else raw_item.get("note", "") if isinstance(raw_item, dict) else str(raw_item or "")
                )
                if self.line_disposition(raw):
                    continue
                classification = classify_review_line(raw)
                marker = (ingredient_index, normalize_name(raw))
                if classification and marker not in existing:
                    reviews.append({"ingredientIndex": ingredient_index, **classification})
                    existing.add(marker)
        return True

    def unresolved_line_reviews(self, recipe: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            review
            for review in recipe.get("lineReviews", [])
            if not self.line_disposition(review.get("raw", ""))
        ]

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
        self.ensure_line_reviews()
        grouped: dict[str, dict[str, Any]] = {}
        hidden_indexes: dict[str, set[int]] = {}
        for recipe in self.recipes.values():
            for review in self.unresolved_line_reviews(recipe):
                raw = review.get("raw", "")
                key = line_key(raw)
                entry = grouped.setdefault(
                    key,
                    {
                        "key": key,
                        "kind": "line",
                        "name": raw,
                        "raw": raw,
                        "recommendation": review.get("recommendation", "note"),
                        "toolMatches": list(review.get("toolMatches") or []),
                        "reasons": list(review.get("reasons") or []),
                        "ambiguous": True,
                        "occurrences": [],
                        "catalogEntries": [],
                    },
                )
                for name in review.get("toolMatches") or []:
                    if name not in entry["toolMatches"]:
                        entry["toolMatches"].append(name)
                ingredient_index = int(review.get("ingredientIndex", -1))
                hidden_indexes.setdefault(recipe["slug"], set()).add(ingredient_index)
                entry["occurrences"].append(
                    {
                        "slug": recipe["slug"],
                        "ingredientIndex": ingredient_index,
                        "raw": raw,
                    }
                )
        for recipe in self.recipes.values():
            for missing in recipe.get("missing", []):
                ingredient_index = int(missing.get("ingredientIndex", -1))
                raw = missing.get("raw", "")
                disposition = self.line_disposition(raw)
                if disposition and disposition.get("type") in ("note", "equipment"):
                    continue
                if ingredient_index in hidden_indexes.get(recipe["slug"], set()):
                    line_entry = grouped.get(line_key(raw))
                    if line_entry is not None:
                        candidate_key = catalog_key(missing["kind"], missing.get("name", ""))
                        if not any(
                            value.get("key") == candidate_key
                            for value in line_entry["catalogEntries"]
                        ):
                            line_entry["catalogEntries"].append(
                                {
                                    "key": candidate_key,
                                    "kind": missing["kind"],
                                    "name": missing.get("name", ""),
                                    "proposal": copy.deepcopy(
                                        missing.get("proposal") or {"name": missing.get("name", "")}
                                    ),
                                    "ambiguous": bool(missing.get("ambiguous")),
                                }
                            )
                    continue
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
        group_rank = {group: index for index, group in enumerate(REVIEW_GROUPS)}
        return sorted(
            grouped.values(),
            key=lambda item: (
                group_rank[review_group(item)],
                normalize_name(item.get("name", "")),
            ),
        )


class CatalogActionJournal:
    """Crash-safe append-only intent and completion log for interactive decisions."""

    def __init__(self, queue_path: str | Path, checkpoint_sequence: int = 0):
        queue_path = Path(queue_path)
        self.path = queue_path.with_name(f"{queue_path.name}.journal")
        self._lock = threading.Lock()
        self._sequence = int(checkpoint_sequence)
        self.corrupt_backup: Optional[Path] = None
        records = self.records()
        if records:
            self._sequence = max(
                self._sequence,
                max(int(record.get("sequence", 0)) for record in records),
            )

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict) or not record.get("sequence"):
                    raise ValueError("invalid journal record")
                records.append(record)
            except (ValueError, json.JSONDecodeError):
                if index == len(lines) - 1:
                    self._replace_records(records)
                    break
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                backup = self.path.with_name(f"{self.path.name}.corrupt-{timestamp}")
                os.replace(self.path, backup)
                self.corrupt_backup = backup
                return records
        return records

    def _replace_records(self, records: list[dict[str, Any]]) -> None:
        if not records:
            if self.path.exists():
                self.path.unlink()
            return
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @property
    def last_sequence(self) -> int:
        with self._lock:
            return self._sequence

    def append(self, status: str, **values: Any) -> dict[str, Any]:
        with self._lock:
            self._sequence += 1
            record = {
                "sequence": self._sequence,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "status": status,
                **copy.deepcopy(values),
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return record

    def compact(self, checkpoint_sequence: int) -> None:
        with self._lock:
            records = [
                record
                for record in self.records()
                if int(record.get("sequence", 0)) > checkpoint_sequence
            ]
            self._replace_records(records)

    def recover(
        self,
        queue: PendingCatalogQueue,
        index: CatalogIndex,
        tool_index: Optional[ToolIndex] = None,
    ) -> dict[str, str]:
        checkpoint = int(queue.data.get("checkpointSequence", 0))
        records = [
            record for record in self.records() if int(record["sequence"]) > checkpoint
        ]
        if not records:
            return {}
        actions: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            actions.setdefault(str(record.get("actionId", "")), []).append(record)
        errors: dict[str, str] = {}
        for action_records in actions.values():
            submitted = next(
                (record for record in action_records if record.get("status") == "submitted"),
                None,
            )
            if not submitted:
                continue
            terminal = next(
                (
                    record
                    for record in reversed(action_records)
                    if record.get("status") in ("completed", "failed")
                ),
                None,
            )
            source_key = (
                line_key(submitted["sourceName"])
                if submitted.get("kind") == "line"
                else catalog_key(submitted["kind"], submitted["sourceName"])
            )
            if submitted.get("kind") == "line":
                disposition = (terminal or {}).get("disposition")
                if terminal and terminal.get("status") == "completed" and disposition:
                    queue.set_line_disposition(
                        submitted["sourceName"],
                        disposition["type"],
                        disposition.get("toolName"),
                    )
                    tool = terminal.get("item")
                    if tool and tool_index:
                        tool_index.upsert(tool)
                    continue
                if terminal and terminal.get("status") == "failed":
                    errors[source_key] = str(terminal.get("error") or "Line action failed")
                    continue
                if submitted.get("operation") == "create_tool" and tool_index:
                    tool_name = (submitted.get("disposition") or {}).get("toolName", "")
                    tool = tool_index.resolve(tool_name)
                    if tool:
                        queue.set_line_disposition(
                            submitted["sourceName"], "equipment", tool_name
                        )
                        continue
                if submitted.get("operation") in {
                    "classify_note",
                    "classify_equipment",
                    "classify_ingredient",
                }:
                    disposition = submitted.get("disposition")
                    if disposition:
                        queue.set_line_disposition(
                            submitted["sourceName"],
                            disposition["type"],
                            disposition.get("toolName"),
                        )
                        continue
                errors[source_key] = "Previous line action had an uncertain outcome; review it again"
                continue
            if terminal and terminal.get("status") == "completed":
                item = terminal.get("item") or submitted.get("targetItem")
                if item and item.get("id") and item.get("name"):
                    index.upsert(submitted["kind"], item)
                    queue.set_resolution(submitted["kind"], submitted["sourceName"], item)
                    disposition = submitted.get("disposition")
                    disposition_raw = submitted.get("dispositionRaw")
                    if disposition and disposition_raw:
                        queue.set_line_disposition(
                            disposition_raw,
                            disposition["type"],
                            disposition.get("toolName"),
                        )
                continue
            if terminal and terminal.get("status") == "failed":
                errors[source_key] = str(terminal.get("error") or "Catalog action failed")
                continue

            operation = submitted.get("operation")
            item: Optional[dict[str, Any]] = None
            if operation == "create":
                proposal_name = (submitted.get("proposal") or {}).get("name", "")
                item, ambiguous = index.resolve(submitted["kind"], proposal_name)
                if ambiguous:
                    item = None
            elif operation in ("map_alias", "map_once"):
                target = submitted.get("targetItem") or {}
                current = index.by_id(submitted["kind"], target.get("id", ""))
                if operation == "map_once":
                    item = current
                elif current and normalize_name(submitted["sourceName"]) in {
                    normalize_name(name)
                    for name in [current.get("name"), current.get("pluralName"), *_aliases(current)]
                    if name
                }:
                    item = current
            if item:
                queue.set_resolution(submitted["kind"], submitted["sourceName"], item)
                disposition = submitted.get("disposition")
                disposition_raw = submitted.get("dispositionRaw")
                if disposition and disposition_raw:
                    queue.set_line_disposition(
                        disposition_raw,
                        disposition["type"],
                        disposition.get("toolName"),
                    )
            else:
                errors[source_key] = "Previous catalog action had an uncertain outcome; review it again"

        terminal_sequence = max(int(record["sequence"]) for record in records)
        queue.data["checkpointSequence"] = terminal_sequence
        queue.save()
        self.compact(terminal_sequence)
        return errors


class QueueCheckpointWriter:
    """Serialize immutable queue snapshots without blocking the review prompt."""

    def __init__(self, queue: PendingCatalogQueue, journal: CatalogActionJournal):
        self.queue = queue
        self.journal = journal
        self.base_snapshot = queue.snapshot()
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="catalog-checkpoint"
        )
        self.futures: list[concurrent.futures.Future[None]] = []
        self.completed_since_checkpoint = 0
        self.latest_snapshot: Optional[tuple[dict[str, Any], int]] = None
        self.timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def note_completion(self) -> None:
        sequence = self.journal.last_sequence
        self.queue.data["checkpointSequence"] = sequence
        snapshot = dict(self.base_snapshot)
        snapshot["resolutions"] = copy.deepcopy(self.queue.data.get("resolutions", {}))
        snapshot["lineDispositions"] = copy.deepcopy(
            self.queue.data.get("lineDispositions", {})
        )
        snapshot["checkpointSequence"] = sequence
        with self._lock:
            self.latest_snapshot = (snapshot, sequence)
            self.completed_since_checkpoint += 1
            if self.completed_since_checkpoint >= 20:
                self._submit_latest_locked()
            elif not self.timer:
                self.timer = threading.Timer(2.0, self._submit_idle)
                self.timer.daemon = True
                self.timer.start()

    def _submit_idle(self) -> None:
        with self._lock:
            self.timer = None
            self._submit_latest_locked()

    def _submit_latest_locked(self) -> None:
        if not self.latest_snapshot:
            return
        snapshot, sequence = self.latest_snapshot
        self.latest_snapshot = None
        self.completed_since_checkpoint = 0
        if self.timer:
            self.timer.cancel()
            self.timer = None
        self.futures.append(
            self.executor.submit(self._write_checkpoint, snapshot, sequence)
        )

    def _write_checkpoint(self, snapshot: dict[str, Any], sequence: int) -> None:
        self.queue.write_snapshot(snapshot)
        self.journal.compact(sequence)

    def errors(self) -> list[Exception]:
        failures: list[Exception] = []
        with self._lock:
            remaining: list[concurrent.futures.Future[None]] = []
            for future in self.futures:
                if future.done():
                    try:
                        future.result()
                    except Exception as exc:
                        failures.append(exc)
                else:
                    remaining.append(future)
            self.futures = remaining
        return failures

    def flush(self) -> None:
        with self._lock:
            if self.timer:
                self.timer.cancel()
                self.timer = None
            self._submit_latest_locked()
            futures = list(self.futures)
        for future in futures:
            future.result()
        sequence = self.journal.last_sequence
        self.queue.data["checkpointSequence"] = sequence
        snapshot = dict(self.base_snapshot)
        snapshot["resolutions"] = copy.deepcopy(self.queue.data.get("resolutions", {}))
        snapshot["lineDispositions"] = copy.deepcopy(
            self.queue.data.get("lineDispositions", {})
        )
        snapshot["checkpointSequence"] = sequence
        self.queue.write_snapshot(snapshot)
        self.journal.compact(sequence)
        self.executor.shutdown(wait=True)


@dataclass(frozen=True)
class CatalogAction:
    action_id: str
    operation: str
    kind: str
    source_name: str
    source_key: str
    reserved_keys: tuple[str, ...]
    proposal: Optional[dict[str, Any]] = None
    target_item: Optional[dict[str, Any]] = None
    disposition: Optional[dict[str, Any]] = None
    disposition_raw: Optional[str] = None
    local_item: Optional[dict[str, Any]] = None
    line_catalog_entries: tuple[dict[str, Any], ...] = ()
    line_occurrences: tuple[dict[str, Any], ...] = ()


@dataclass
class CatalogActionResult:
    action: CatalogAction
    item: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    refreshed_items: Optional[list[dict[str, Any]]] = None
    disposition: Optional[dict[str, Any]] = None
    created: bool = False


class CatalogActionRunner:
    """Execute Mealie catalog mutations in submission order on one worker."""

    def __init__(self, api: CatalogApi, journal: CatalogActionJournal):
        clone = getattr(api, "clone", None)
        self.api = clone() if callable(clone) else api
        self.journal = journal
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="catalog-actions"
        )
        self.results: thread_queue.Queue[CatalogActionResult] = thread_queue.Queue()
        self._pending = 0
        self._lock = threading.Lock()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return self._pending

    def submit(self, action: CatalogAction) -> None:
        self.journal.append(
            "submitted",
            actionId=action.action_id,
            operation=action.operation,
            kind=action.kind,
            sourceName=action.source_name,
            reservedKeys=list(action.reserved_keys),
            proposal=action.proposal,
            targetItem=action.target_item,
            disposition=action.disposition,
            dispositionRaw=action.disposition_raw,
        )
        with self._lock:
            self._pending += 1
        self.executor.submit(self._run, action)

    def submit_local(
        self,
        action: CatalogAction,
        item: Optional[dict[str, Any]] = None,
        disposition: Optional[dict[str, Any]] = None,
    ) -> None:
        """Queue a journaled local decision without contacting Mealie."""
        queued_action = replace(
            action,
            local_item=copy.deepcopy(item),
            disposition=copy.deepcopy(disposition or action.disposition),
        )
        self.submit(queued_action)

    def complete_local(
        self,
        action: CatalogAction,
        item: Optional[dict[str, Any]] = None,
        disposition: Optional[dict[str, Any]] = None,
    ) -> CatalogActionResult:
        self.journal.append(
            "submitted",
            actionId=action.action_id,
            operation=action.operation,
            kind=action.kind,
            sourceName=action.source_name,
            reservedKeys=list(action.reserved_keys),
            proposal=action.proposal,
            targetItem=action.target_item,
            disposition=action.disposition,
            dispositionRaw=action.disposition_raw,
        )
        self.journal.append(
            "completed",
            actionId=action.action_id,
            item=copy.deepcopy(item),
            disposition=copy.deepcopy(disposition or action.disposition),
        )
        return CatalogActionResult(
            action=action,
            item=copy.deepcopy(item),
            disposition=copy.deepcopy(disposition or action.disposition),
            created=False,
        )

    def _run(self, action: CatalogAction) -> None:
        try:
            if action.operation in {
                "classify_note",
                "classify_equipment",
                "classify_ingredient",
            }:
                item = copy.deepcopy(action.local_item)
            elif action.operation == "create":
                item = self.api.create_item(action.kind, action.proposal or {})
            elif action.operation == "map_alias":
                if not action.target_item:
                    raise RuntimeError("Alias mapping target is missing")
                item = self.api.add_alias(
                    action.kind, action.target_item["id"], action.source_name
                )
            elif action.operation == "create_tool":
                tool_name = (action.disposition or {}).get("toolName")
                if not tool_name:
                    raise RuntimeError("Tool creation name is missing")
                item = self.api.create_tool(tool_name)
            else:
                raise RuntimeError(f"Unsupported background operation: {action.operation}")
            self.journal.append(
                "completed",
                actionId=action.action_id,
                item=copy.deepcopy(item),
                disposition=copy.deepcopy(action.disposition),
            )
            self.results.put(
                CatalogActionResult(
                    action=action,
                    item=item,
                    disposition=copy.deepcopy(action.disposition),
                    created=action.operation == "create",
                )
            )
        except Exception as exc:
            if action.operation == "create_tool":
                try:
                    tool_name = (action.disposition or {}).get("toolName", "")
                    item = next(
                        (
                            tool
                            for tool in self.api.list_tools()
                            if normalize_name(tool.get("name")) == normalize_name(tool_name)
                        ),
                        None,
                    )
                    if item:
                        self.journal.append(
                            "completed",
                            actionId=action.action_id,
                            item=copy.deepcopy(item),
                            disposition=copy.deepcopy(action.disposition),
                        )
                        self.results.put(
                            CatalogActionResult(
                                action=action,
                                item=item,
                                disposition=copy.deepcopy(action.disposition),
                                created=False,
                            )
                        )
                        return
                except Exception:
                    pass
            refreshed_items: Optional[list[dict[str, Any]]] = None
            try:
                list_items = getattr(self.api, "list_items", None)
                if callable(list_items) and action.kind in CATALOG_KINDS:
                    refreshed_items = list_items(action.kind)
            except Exception:
                refreshed_items = None
            self.journal.append("failed", actionId=action.action_id, error=str(exc))
            self.results.put(
                CatalogActionResult(
                    action=action,
                    error=str(exc),
                    refreshed_items=refreshed_items,
                )
            )
        finally:
            with self._lock:
                self._pending -= 1

    def drain(self) -> list[CatalogActionResult]:
        results: list[CatalogActionResult] = []
        while True:
            try:
                results.append(self.results.get_nowait())
            except thread_queue.Empty:
                return results

    def wait_for_result(self, timeout: float = 0.1) -> Optional[CatalogActionResult]:
        try:
            return self.results.get(timeout=timeout)
        except thread_queue.Empty:
            return None

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True)


class CatalogSuggestionPrefetcher:
    def __init__(self, index: CatalogIndex):
        self.index = index
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="catalog-suggestions"
        )
        self.future: Optional[concurrent.futures.Future[None]] = None

    def prefetch(self, entries: list[dict[str, Any]]) -> None:
        if self.future and not self.future.done():
            return
        targets = [(entry["kind"], entry["name"]) for entry in entries[:20]]
        self.future = self.executor.submit(self._run, targets)

    def _run(self, targets: list[tuple[str, str]]) -> None:
        for kind, name in targets:
            self.index.search(kind, name, limit=5)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True)


def item_reference(item: dict[str, Any]) -> dict[str, Any]:
    return {"id": item["id"], "name": item["name"]}


def replay_ready_recipes(
    queue: PendingCatalogQueue,
    index: CatalogIndex,
    api: CatalogApi,
    history: set[str],
    logger: Optional[Callable[[str], None]] = None,
    tool_index: Optional[ToolIndex] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> dict[str, int]:
    """Apply stored proposals for recipes whose references are now all resolved."""
    stats = {"updated": 0, "waiting": 0, "stale": 0, "failed": 0, "interrupted": 0}
    if not queue.ensure_line_reviews(should_stop):
        stats["interrupted"] = 1
        return stats
    for slug, record in list(queue.recipes.items()):
        if should_stop and should_stop():
            stats["interrupted"] = 1
            break
        resolved_by_key: dict[str, dict[str, Any]] = {}
        blocked = False
        if queue.unresolved_line_reviews(record):
            stats["waiting"] += 1
            continue
        equipment_by_index: dict[int, dict[str, Any]] = {}
        dispositions_by_index: dict[int, dict[str, Any]] = {}
        for ingredient_index, raw_item in enumerate(record.get("sourceIngredients", [])):
            raw = (
                raw_item
                if isinstance(raw_item, str)
                else raw_item.get("note", "") if isinstance(raw_item, dict) else str(raw_item or "")
            )
            disposition = queue.line_disposition(raw)
            if not disposition:
                continue
            dispositions_by_index[ingredient_index] = disposition
            if disposition.get("type") == "equipment":
                tool = tool_index.resolve(disposition.get("toolName", "")) if tool_index else None
                if not tool:
                    blocked = True
                    break
                equipment_by_index[ingredient_index] = tool
        if blocked:
            stats["waiting"] += 1
            continue
        for missing in record.get("missing", []):
            disposition = dispositions_by_index.get(int(missing.get("ingredientIndex", -1)))
            if disposition and disposition.get("type") in ("note", "equipment"):
                continue
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
            for ingredient_index, disposition in dispositions_by_index.items():
                if disposition.get("type") in ("note", "equipment"):
                    raw = record.get("sourceIngredients", [])[ingredient_index]
                    raw_text = (
                        raw if isinstance(raw, str) else raw.get("note", "") if isinstance(raw, dict) else str(raw or "")
                    )
                    proposed[ingredient_index] = {
                        "note": raw_text,
                        "originalText": raw_text,
                    }
            for missing in record.get("missing", []):
                disposition = dispositions_by_index.get(int(missing.get("ingredientIndex", -1)))
                if disposition and disposition.get("type") in ("note", "equipment"):
                    continue
                replacement = resolved_by_key[catalog_key(missing["kind"], missing.get("name", ""))]
                proposed[missing["ingredientIndex"]][missing["kind"]] = item_reference(replacement)
            current["recipeIngredient"] = proposed
            desired_tools = list(current.get("tools") or [])
            known_tool_ids = {str(tool.get("id")) for tool in desired_tools if tool.get("id")}
            known_tool_names = {normalize_name(tool.get("name")) for tool in desired_tools}
            for tool in equipment_by_index.values():
                if str(tool.get("id")) not in known_tool_ids and normalize_name(tool.get("name")) not in known_tool_names:
                    desired_tools.append(copy.deepcopy(tool))
                    known_tool_ids.add(str(tool.get("id")))
                    known_tool_names.add(normalize_name(tool.get("name")))
            if equipment_by_index:
                current["tools"] = desired_tools
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
    if not stats["interrupted"]:
        queue.save()
    return stats


REVIEW_GROUPS = ("ingredient", "unit", "note")
REVIEW_GROUP_LABELS = {
    "ingredient": "Ingredients",
    "unit": "Units",
    "note": "Notes",
}


def review_group(entry: dict[str, Any]) -> str:
    """Return the user-facing review group for a queue entry."""
    if entry.get("kind") == "food":
        return "ingredient"
    if entry.get("kind") == "unit":
        return "unit"
    if entry.get("kind") == "line" and entry.get("recommendation") == "ingredient":
        return "ingredient"
    return "note"


@dataclass
class ReviewSummary:
    """Human-readable outcomes collected during one catalog review session."""

    created_ingredients: list[str] = field(default_factory=list)
    created_units: list[str] = field(default_factory=list)
    mapped_with_alias: list[str] = field(default_factory=list)
    mapped_once: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    confirmed_ingredients: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    automatically_matched: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @staticmethod
    def _add_unique(values: list[str], value: str) -> None:
        if value and value not in values:
            values.append(value)

    def add(self, field_name: str, value: Any) -> None:
        self._add_unique(getattr(self, field_name), str(value))


@dataclass
class CatalogReviewer:
    queue: PendingCatalogQueue
    index: CatalogIndex
    api: CatalogApi
    console: Console
    tool_index: ToolIndex = field(default_factory=ToolIndex)

    def create(self, entry: dict[str, Any], proposal: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        proposal = copy.deepcopy(proposal or entry["proposal"])
        existing, ambiguous = self.index.resolve(entry["kind"], proposal.get("name", ""))
        if ambiguous:
            raise RuntimeError("Catalog name or alias is ambiguous; edit the proposal or map it explicitly")
        if existing:
            item = existing
        else:
            item = self.api.create_item(entry["kind"], proposal)
            self.index.upsert(entry["kind"], item)
        self.queue.set_resolution(entry["kind"], entry["name"], item)
        self.queue.save()
        return item

    def map(self, entry: dict[str, Any], item: dict[str, Any], add_alias: bool = True) -> dict[str, Any]:
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
                self.index.upsert(entry["kind"], current)
        self.queue.set_resolution(entry["kind"], entry["name"], current)
        self.queue.save()
        return current

    def choose_mapping(self, entry: dict[str, Any]) -> Optional[dict[str, Any]]:
        candidates = self.index.search(entry["kind"], entry["name"], limit=5)
        search_label = "Search for another item"
        while True:
            if candidates:
                self._candidate_table(candidates)
            search_choice = len(candidates) + 1
            cancel_choice = len(candidates) + 2
            self.console.print(f"{search_choice}) {search_label}", markup=False)
            self.console.print(f"{cancel_choice}) Cancel", markup=False)
            choice = Prompt.ask(
                "Choose an existing item",
                choices=[str(number) for number in range(1, cancel_choice + 1)],
                default=str(cancel_choice),
                console=self.console,
            )
            selected = int(choice)
            if selected <= len(candidates):
                return candidates[selected - 1]
            if selected == cancel_choice:
                return None

            query = Prompt.ask(
                "Search existing items", default=entry["name"], console=self.console
            )
            candidates = self.index.search(entry["kind"], query, limit=10)
            search_label = "Search again"
            if not candidates:
                self.console.print("[warning]No existing items found.[/warning]")

    def _candidate_table(self, candidates: list[dict[str, Any]]) -> None:
        table = Table(title="Possible existing items")
        table.add_column("#", justify="right")
        table.add_column("Name")
        table.add_column("Aliases")
        for index, item in enumerate(candidates, 1):
            table.add_row(str(index), item["name"], ", ".join(_aliases(item)[:4]))
        self.console.print(table)

    @staticmethod
    def _entry_group(entry: dict[str, Any]) -> str:
        return review_group(entry)

    @classmethod
    def _entry_group_label(cls, entry: dict[str, Any]) -> str:
        return REVIEW_GROUP_LABELS[cls._entry_group(entry)]

    @classmethod
    def _sort_entries(cls, entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        rank = {group: index for index, group in enumerate(REVIEW_GROUPS)}
        return sorted(
            entries,
            key=lambda item: (
                rank[cls._entry_group(item)],
                normalize_name(item.get("name", "")),
            ),
        )

    @staticmethod
    def _plain_reason(reason: str) -> str:
        if reason == "matched configured equipment phrase":
            return "This looks like a note rather than a measured ingredient."
        if reason == "mentions configured equipment but starts like an ingredient line":
            return "This starts like an ingredient but includes extra instructions."
        if reason == "looks like prose rather than a measured ingredient":
            return "This looks like a note rather than a measured ingredient."
        return reason

    @staticmethod
    def _display_value(value: Any) -> str:
        if isinstance(value, list):
            values = [
                item.get("name") if isinstance(item, dict) else item
                for item in value
            ]
            return ", ".join(str(item) for item in values if item not in (None, ""))
        return str(value)

    @classmethod
    def _proposal_fields(cls, entry: dict[str, Any]) -> list[tuple[str, str]]:
        proposal = entry.get("proposal") or {}
        proposed_name = proposal.get("name") or entry.get("name")
        labels = {
            "name": "Name",
            "pluralName": "Plural name",
            "description": "Description",
            "aliases": "Other names",
            "abbreviation": "Short form",
            "pluralAbbreviation": "Plural short form",
            "fraction": "Supports fractions",
            "useAbbreviation": "Uses short form",
        }
        fields: list[tuple[str, str]] = [("Name", cls._display_value(proposed_name))]
        for field, value in proposal.items():
            if field == "name" or value is None or value == "" or value == [] or value == {}:
                continue
            fields.append((labels.get(field, field.replace("_", " ").title()), cls._display_value(value)))
        return fields

    @staticmethod
    def _usage_examples(entry: dict[str, Any]) -> list[str]:
        occurrences = entry.get("occurrences") or []
        examples = [
            f"{occurrence['slug']}: {occurrence['raw']}" for occurrence in occurrences[:2]
        ]
        if len(occurrences) > 2:
            examples.append(f"…and {len(occurrences) - 2} more recipe(s)")
        return examples

    def _show_entry(
        self,
        entry: dict[str, Any],
        remaining: int,
        skipped: int,
        pending: int = 0,
        error: Optional[str] = None,
        group_position: Optional[int] = None,
        group_total: Optional[int] = None,
    ) -> None:
        group_label = self._entry_group_label(entry)
        title = group_label
        if group_position is not None and group_total is not None:
            title += f" — {group_position} of {group_total}"
        self.console.rule(f"[bold cyan]{title}: {escape(str(entry['name']))}[/bold cyan]")
        status = f"{remaining} item(s) remain in this review"
        if skipped:
            status += f"; {skipped} deferred"
        if pending:
            status += f"; {pending} item(s) are being saved"
        self.console.print(status, markup=False)
        if error:
            self.console.print(f"[error]This item could not be saved: {escape(error)}[/error]")
        if entry["kind"] == "line":
            recommendation = entry.get("recommendation", "note")
            if recommendation == "ingredient":
                headline = "This starts like an ingredient but includes extra instructions."
                self.console.print(
                    f"[warning]{headline}[/warning]"
                )
            else:
                headline = "This looks like a note rather than a measured ingredient."
                self.console.print(
                    f"[warning]{headline}[/warning]"
                )
            for reason in entry.get("reasons") or []:
                plain_reason = self._plain_reason(str(reason))
                if plain_reason != headline:
                    self.console.print(f"  {plain_reason}", markup=False)
            catalog_entries = entry.get("catalogEntries") or []
            if catalog_entries:
                candidate = catalog_entries[0]
                self.console.print(
                    f"Suggested {('ingredient' if candidate['kind'] == 'food' else 'unit')} if this line is an ingredient:",
                    style="bold",
                )
                for field_name, value in self._proposal_fields(candidate):
                    self.console.print(f"  {field_name}: {value}", markup=False)
            self.console.print(
                f"Appears in [cyan]{len(entry['occurrences'])}[/cyan] recipe(s)"
            )
            for example in self._usage_examples(entry):
                self.console.print(f"  • {example}", markup=False)
            return
        self.console.print(
            f"Suggested {('ingredient' if entry['kind'] == 'food' else 'unit')} details:",
            style="bold",
        )
        for field, value in self._proposal_fields(entry):
            self.console.print(f"  {field}: {value}", markup=False)
        self.console.print(
            f"Appears in [cyan]{len(entry['occurrences'])}[/cyan] recipe(s)"
        )
        for example in self._usage_examples(entry):
            self.console.print(f"  • {example}", markup=False)
        if entry.get("ambiguous"):
            self.console.print(
                "[warning]More than one existing item could match this name; choose one or edit the suggestion.[/warning]"
            )
        suggestions = self.index.search(entry["kind"], entry["name"], limit=5)
        if suggestions:
            self.console.print(
                "Possible existing matches: " + ", ".join(item["name"] for item in suggestions),
                markup=False,
            )

    def _show_actions(self) -> None:
        self.console.print("1) Create the suggested item", markup=False)
        self.console.print("2) Change details, then create", markup=False)
        self.console.print("3) Use an existing item and remember this name", markup=False)
        self.console.print("4) Use an existing item this time only", markup=False)
        self.console.print("7) Defer until later", markup=False)
        self.console.print("0) Finish review", markup=False)

    def _show_line_actions(self, entry: dict[str, Any]) -> None:
        if entry.get("catalogEntries"):
            self.console.print("1) Create the suggested item", markup=False)
            self.console.print("2) Change details, then create", markup=False)
            self.console.print("3) Use an existing item and remember this name", markup=False)
            self.console.print("4) Use an existing item this time only", markup=False)
        if entry.get("recommendation") == "ingredient":
            self.console.print("5) Treat this line as an ingredient", markup=False)
            self.console.print("6) Keep this line as a note", markup=False)
        else:
            self.console.print("5) Treat this line as an ingredient", markup=False)
            self.console.print("6) Keep this line as a note (recommended)", markup=False)
        self.console.print("7) Defer until later", markup=False)
        self.console.print("0) Finish review", markup=False)

    @staticmethod
    def _proposal_names(kind: str, proposal: dict[str, Any]) -> list[str]:
        names = [proposal.get("name"), proposal.get("pluralName")]
        names.extend(_aliases(proposal))
        if kind == "unit":
            names.extend(
                [proposal.get("abbreviation"), proposal.get("pluralAbbreviation")]
            )
        return [str(name) for name in names if name]

    def _rebuild_entries(self, entries_by_key: dict[str, dict[str, Any]]) -> None:
        entries_by_key.clear()
        entries_by_key.update(
            {entry["key"]: entry for entry in self.queue.entries(self.index)}
        )

    def _line_action(
        self, entry: dict[str, Any], disposition: dict[str, Any], operation: str
    ) -> CatalogAction:
        return CatalogAction(
            action_id=str(uuid.uuid4()),
            operation=operation,
            kind="line",
            source_name=entry["raw"],
            source_key=entry["key"],
            reserved_keys=(entry["key"],),
            disposition=copy.deepcopy(disposition),
            line_catalog_entries=tuple(copy.deepcopy(entry.get("catalogEntries") or [])),
            line_occurrences=tuple(copy.deepcopy(entry.get("occurrences") or [])),
        )

    @staticmethod
    def _update_line_entries(
        entries_by_key: dict[str, dict[str, Any]],
        action: CatalogAction,
        disposition: dict[str, Any],
    ) -> None:
        """Apply a line decision without rescanning the entire pending queue."""
        entries_by_key.pop(action.source_key, None)
        if disposition.get("type") != "ingredient":
            return
        for candidate in action.line_catalog_entries:
            key = candidate["key"]
            entry = entries_by_key.get(key)
            if entry is None:
                entries_by_key[key] = {
                    "key": key,
                    "kind": candidate["kind"],
                    "name": candidate["name"],
                    "proposal": copy.deepcopy(candidate["proposal"]),
                    "ambiguous": bool(candidate.get("ambiguous")),
                    "occurrences": copy.deepcopy(action.line_occurrences),
                }
            else:
                entry["ambiguous"] = entry["ambiguous"] or bool(
                    candidate.get("ambiguous")
                )

    def _complete_line_locally(
        self,
        entry: dict[str, Any],
        disposition: dict[str, Any],
        runner: CatalogActionRunner,
        pending_by_key: dict[str, str],
    ) -> None:
        action = self._line_action(
            entry, disposition, f"classify_{disposition['type']}"
        )
        pending_by_key[entry["key"]] = action.action_id
        runner.submit_local(action, disposition=disposition)

    def _new_action(
        self,
        operation: str,
        entry: dict[str, Any],
        entries_by_key: dict[str, dict[str, Any]],
        *,
        proposal: Optional[dict[str, Any]] = None,
        target_item: Optional[dict[str, Any]] = None,
        disposition: Optional[dict[str, Any]] = None,
        disposition_raw: Optional[str] = None,
    ) -> CatalogAction:
        reserved = {entry["key"]}
        if disposition_raw:
            reserved.add(line_key(disposition_raw))
        if operation == "create" and proposal:
            reserved.update(
                key
                for key in (
                    catalog_key(entry["kind"], name)
                    for name in self._proposal_names(entry["kind"], proposal)
                )
                if key in entries_by_key
            )
        return CatalogAction(
            action_id=str(uuid.uuid4()),
            operation=operation,
            kind=entry["kind"],
            source_name=entry["name"],
            source_key=entry["key"],
            reserved_keys=tuple(sorted(reserved)),
            proposal=copy.deepcopy(proposal),
            target_item=copy.deepcopy(target_item),
            disposition=copy.deepcopy(disposition),
            disposition_raw=disposition_raw,
        )

    def _start_create(
        self,
        entry: dict[str, Any],
        proposal: dict[str, Any],
        entries_by_key: dict[str, dict[str, Any]],
        pending_by_key: dict[str, str],
        runner: CatalogActionRunner,
        *,
        disposition: Optional[dict[str, Any]] = None,
        disposition_raw: Optional[str] = None,
    ) -> Optional[CatalogActionResult]:
        existing, ambiguous = self.index.resolve(entry["kind"], proposal.get("name", ""))
        if ambiguous:
            raise RuntimeError("Catalog name or alias is ambiguous; edit the proposal or map it explicitly")
        action = self._new_action(
            "create",
            entry,
            entries_by_key,
            proposal=proposal,
            disposition=disposition,
            disposition_raw=disposition_raw,
        )
        if any(key in pending_by_key for key in action.reserved_keys):
            return None
        if existing:
            for key in action.reserved_keys:
                pending_by_key[key] = action.action_id
            return runner.complete_local(action, existing)
        runner.submit(action)
        for key in action.reserved_keys:
            pending_by_key[key] = action.action_id
        return None

    def _apply_action_result(
        self,
        result: CatalogActionResult,
        entries_by_key: dict[str, dict[str, Any]],
        pending_by_key: dict[str, str],
        deferred: set[str],
        errors: dict[str, str],
        checkpoint: QueueCheckpointWriter,
        summary: Optional[ReviewSummary] = None,
    ) -> int:
        summary = summary or ReviewSummary()
        action = result.action
        for key in action.reserved_keys:
            if pending_by_key.get(key) == action.action_id:
                pending_by_key.pop(key, None)
        if result.error:
            if result.refreshed_items is not None:
                self.index.replace(action.kind, result.refreshed_items)
            errors[action.source_key] = result.error
            if action.disposition_raw:
                errors[line_key(action.disposition_raw)] = result.error
            checkpoint.note_completion()
            return 1

        if action.kind == "line":
            disposition = result.disposition or action.disposition
            if not disposition:
                errors[action.source_key] = "This line did not have a saved decision"
                checkpoint.note_completion()
                return 1
            if disposition.get("type") == "equipment" and result.item:
                self.tool_index.upsert(result.item)
            self.queue.set_line_disposition(
                action.source_name,
                disposition["type"],
                disposition.get("toolName"),
            )
            errors.pop(action.source_key, None)
            deferred.discard(action.source_key)
            self._update_line_entries(entries_by_key, action, disposition)
            checkpoint.note_completion()
            if disposition["type"] == "note":
                summary.add("notes", action.source_name)
            elif disposition["type"] == "ingredient":
                summary.add("confirmed_ingredients", action.source_name)
            return 0

        if not result.item:
            errors[action.source_key] = "No item was returned after saving this decision"
            if action.disposition_raw:
                errors[line_key(action.disposition_raw)] = errors[action.source_key]
            checkpoint.note_completion()
            return 1
        self.index.upsert(action.kind, result.item)
        self.queue.set_resolution(action.kind, action.source_name, result.item)
        errors.pop(action.source_key, None)
        resolved: list[str] = []
        candidates = set(action.reserved_keys)
        candidates.update(
            catalog_key(action.kind, name)
            for name in self._proposal_names(action.kind, result.item)
        )
        for key in candidates:
            entry = entries_by_key.get(key)
            if not entry:
                continue
            if entry.get("kind") == "line":
                continue
            item, ambiguous = self.queue.resolution(entry["kind"], entry["name"], self.index)
            if item and not ambiguous:
                entries_by_key.pop(key, None)
                pending_by_key.pop(key, None)
                deferred.discard(key)
                errors.pop(key, None)
                if key != action.source_key:
                    resolved.append(f"{entry['name']} → {item['name']}")
        if action.operation == "create":
            if result.created:
                summary.add(
                    "created_ingredients" if action.kind == "food" else "created_units",
                    result.item["name"],
                )
            else:
                summary.add(
                    "automatically_matched",
                    f"{action.source_name} → {result.item['name']}",
                )
        elif action.operation == "map_alias":
            summary.add(
                "mapped_with_alias",
                f"{action.source_name} → {result.item['name']}",
            )
        elif action.operation == "map_once":
            summary.add("mapped_once", f"{action.source_name} → {result.item['name']}")
        for resolved_name in resolved:
            summary.add("automatically_matched", resolved_name)
        if action.disposition and action.disposition_raw:
            self.queue.set_line_disposition(
                action.disposition_raw,
                action.disposition["type"],
                action.disposition.get("toolName"),
            )
            errors.pop(line_key(action.disposition_raw), None)
            deferred.discard(line_key(action.disposition_raw))
            self._rebuild_entries(entries_by_key)
            if action.disposition.get("type") == "ingredient":
                summary.add("confirmed_ingredients", action.disposition_raw)
        checkpoint.note_completion()
        return 0

    def _edit_proposal(self, entry: dict[str, Any]) -> dict[str, Any]:
        proposal = copy.deepcopy(entry["proposal"])
        proposal["name"] = Prompt.ask("Name", default=proposal.get("name") or entry["name"], console=self.console)
        proposal["pluralName"] = Prompt.ask(
            "Plural name", default=proposal.get("pluralName") or "", console=self.console
        ) or None
        if entry["kind"] == "unit":
            proposal["abbreviation"] = Prompt.ask(
                "Short form", default=proposal.get("abbreviation") or "", console=self.console
            )
            proposal["pluralAbbreviation"] = Prompt.ask(
                "Plural short form",
                default=proposal.get("pluralAbbreviation") or "",
                console=self.console,
            ) or None
        return proposal

    def _show_summary(
        self,
        summary: ReviewSummary,
        entries_by_key: dict[str, dict[str, Any]],
        errors: dict[str, str],
    ) -> None:
        self.console.rule("[bold cyan]Review summary[/bold cyan]")
        sections = (
            ("Created ingredients", summary.created_ingredients),
            ("Created units", summary.created_units),
            ("Remembered names", summary.mapped_with_alias),
            ("Used existing items once", summary.mapped_once),
            ("Kept as notes", summary.notes),
            ("Confirmed as ingredients", summary.confirmed_ingredients),
            ("Deferred until later", summary.deferred),
            ("Automatically matched", summary.automatically_matched),
        )
        shown = False
        for label, values in sections:
            if not values:
                continue
            shown = True
            self.console.print(f"{label} ({len(values)}):", style="bold")
            for value in values:
                self.console.print(f"  • {value}", markup=False)

        failures: list[str] = []
        seen_failures: set[str] = set()
        visible_failure_messages = {
            message for key, message in errors.items() if key in entries_by_key
        }
        for key, message in errors.items():
            entry = entries_by_key.get(key)
            if entry is None and message in visible_failure_messages:
                continue
            name = str(entry.get("name") if entry else key.split(":", 1)[-1])
            value = f"{name}: {message}"
            if value not in seen_failures:
                seen_failures.add(value)
                failures.append(value)
        for value in summary.failures:
            if value not in seen_failures:
                seen_failures.add(value)
                failures.append(value)
        if failures:
            shown = True
            self.console.print(f"Could not save ({len(failures)}):", style="bold red")
            for value in failures:
                self.console.print(f"  • {value}", markup=False)

        remaining = len(entries_by_key)
        if remaining:
            shown = True
            self.console.print(
                f"{remaining} item(s) remain queued for a future review.", markup=False
            )
        if not shown:
            self.console.print("No review decisions were made.", markup=False)

    def review(self) -> int:
        list_tools = getattr(self.api, "list_tools", None)
        if callable(list_tools):
            try:
                self.tool_index.replace(list_tools())
            except Exception:
                # Existing saved equipment decisions are still replayed when possible;
                # the review itself no longer asks users to manage equipment.
                pass
        journal = CatalogActionJournal(
            self.queue.path, int(self.queue.data.get("checkpointSequence", 0))
        )
        recovery_errors = journal.recover(self.queue, self.index, self.tool_index)
        if journal.corrupt_backup:
            self.console.print(
                f"[warning]A corrupt catalog action journal was preserved at "
                f"{escape(str(journal.corrupt_backup))}.[/warning]"
            )
        initial_entries = self.queue.entries(self.index)
        if not initial_entries:
            self.console.print("[success]Nothing needs ingredient, unit, or note review.[/success]")
            return 0
        entries_by_key = {entry["key"]: entry for entry in initial_entries}
        group_counts = {
            group: sum(1 for entry in initial_entries if self._entry_group(entry) == group)
            for group in REVIEW_GROUPS
        }
        self.console.print(
            "Review groups: "
            + " | ".join(
                f"{REVIEW_GROUP_LABELS[group]} {group_counts[group]}"
                for group in REVIEW_GROUPS
            ),
            markup=False,
        )
        deferred: set[str] = set()
        pending_by_key: dict[str, str] = {}
        errors = {
            key: value for key, value in recovery_errors.items() if key in entries_by_key
        }
        failures = 0
        summary = ReviewSummary()
        runner = CatalogActionRunner(self.api, journal)
        checkpoint = QueueCheckpointWriter(self.queue, journal)
        prefetcher = CatalogSuggestionPrefetcher(self.index)
        completed_groups: set[str] = set()
        pass_number = 1
        group_totals = {
            group: sum(1 for entry in initial_entries if self._entry_group(entry) == group)
            for group in REVIEW_GROUPS
        }

        def apply_results(results: list[CatalogActionResult]) -> None:
            nonlocal failures
            for result in results:
                failures += self._apply_action_result(
                    result,
                    entries_by_key,
                    pending_by_key,
                    deferred,
                    errors,
                    checkpoint,
                    summary,
                )

        try:
            while True:
                apply_results(runner.drain())
                for checkpoint_error in checkpoint.errors():
                    failures += 1
                    summary.add("failures", f"Queue checkpoint: {checkpoint_error}")
                active = [
                    entry
                    for key, entry in entries_by_key.items()
                    if key not in deferred and key not in pending_by_key
                ]
                active = self._sort_entries(active)

                selected_group: Optional[str] = None
                waiting_for_group = False
                for group in REVIEW_GROUPS:
                    if group in completed_groups:
                        continue
                    group_active = [
                        entry for entry in active if self._entry_group(entry) == group
                    ]
                    if group_active:
                        selected_group = group
                        break
                    group_pending = any(
                        self._entry_group(entry) == group
                        and entry["key"] in pending_by_key
                        for entry in entries_by_key.values()
                    )
                    if group_pending:
                        waiting_for_group = True
                        break
                    completed_groups.add(group)

                if selected_group is None:
                    if waiting_for_group or runner.pending_count:
                        result = runner.wait_for_result(timeout=0.1)
                        if result:
                            apply_results([result])
                        continue
                    if active:
                        # A later decision can reveal a new ingredient/unit after
                        # that group was already completed. Keep categories contiguous
                        # and announce the new pass instead of interleaving it.
                        completed_groups.clear()
                        pass_number += 1
                        self.console.print(
                            f"Additional items were found; starting review pass {pass_number}.",
                            markup=False,
                        )
                        continue
                    late_results = runner.drain()
                    if late_results:
                        apply_results(late_results)
                        continue
                    break

                group_entries = [
                    entry for entry in active if self._entry_group(entry) == selected_group
                ]
                entry = group_entries[0]
                group_total = sum(
                    1
                    for value in entries_by_key.values()
                    if self._entry_group(value) == selected_group
                )
                group_totals[selected_group] = max(
                    group_totals[selected_group], group_total
                )
                group_total = group_totals[selected_group]
                group_position = group_total - len(group_entries) + 1
                prefetcher.prefetch(
                    [item for item in active[1:21] if item["kind"] in CATALOG_KINDS]
                )
                self._show_entry(
                    entry,
                    len(entries_by_key),
                    len(deferred),
                    runner.pending_count,
                    errors.get(entry["key"]),
                    group_position=group_position,
                    group_total=group_total,
                )
                review_entry = entry
                if entry["kind"] == "line":
                    self._show_line_actions(entry)
                    catalog_entries = entry.get("catalogEntries") or []
                    line_choices = ["5", "6", "7", "0"]
                    if catalog_entries:
                        line_choices = ["1", "2", "3", "4", *line_choices]
                    action_choice = Prompt.ask(
                        "Choose an action",
                        choices=line_choices,
                        default="7",
                        console=self.console,
                    )
                else:
                    self._show_actions()
                    action_choice = Prompt.ask(
                        "Choose an action",
                        choices=["1", "2", "3", "4", "7", "0"],
                        default="7",
                        console=self.console,
                    )
                catalog_disposition: Optional[dict[str, Any]] = None
                catalog_disposition_raw: Optional[str] = None
                try:
                    if action_choice == "0":
                        break
                    if action_choice == "7":
                        deferred.add(entry["key"])
                        summary.add("deferred", entry["name"])
                        continue
                    if entry["kind"] == "line":
                        if action_choice == "5":
                            self._complete_line_locally(
                                entry, {"type": "ingredient"}, runner, pending_by_key
                            )
                            continue
                        if action_choice == "6":
                            self._complete_line_locally(
                                entry, {"type": "note"}, runner, pending_by_key
                            )
                            continue
                        if action_choice not in ("1", "2", "3", "4"):
                            continue
                        catalog_entries = entry.get("catalogEntries") or []
                        if not catalog_entries:
                            self.console.print(
                                "No suggested ingredient or unit is available for this line. "
                                "Choose 5 to treat it as an ingredient or 6 to keep it as a note.",
                                markup=False,
                            )
                            continue
                        raw_line = entry["raw"]
                        occurrences = copy.deepcopy(entry.get("occurrences") or [])
                        entry = copy.deepcopy(catalog_entries[0])
                        entry["occurrences"] = occurrences
                        catalog_disposition = {"type": "ingredient"}
                        catalog_disposition_raw = raw_line

                    if action_choice in ("1", "2"):
                        proposal = (
                            self._edit_proposal(entry)
                            if action_choice == "2"
                            else copy.deepcopy(entry["proposal"])
                        )
                        errors.pop(entry["key"], None)
                        local_result = self._start_create(
                            entry,
                            proposal,
                            entries_by_key,
                            pending_by_key,
                            runner,
                            disposition=catalog_disposition,
                            disposition_raw=catalog_disposition_raw,
                        )
                        if local_result:
                            apply_results([local_result])
                    elif action_choice in ("3", "4"):
                        selected = self.choose_mapping(entry)
                        if not selected:
                            continue
                        if action_choice == "3" and not Confirm.ask(
                            f"Remember {entry['name']!r} as another name for "
                            f"{selected['name']!r}?",
                            default=False,
                            console=self.console,
                        ):
                            self.console.print(
                                "No mapping was saved.",
                                markup=False,
                            )
                            continue
                        operation = "map_alias" if action_choice == "3" else "map_once"
                        catalog_action = self._new_action(
                            operation,
                            entry,
                            entries_by_key,
                            target_item=selected,
                            disposition=catalog_disposition,
                            disposition_raw=catalog_disposition_raw,
                        )
                        errors.pop(entry["key"], None)
                        if operation == "map_alias":
                            runner.submit(catalog_action)
                            for key in catalog_action.reserved_keys:
                                pending_by_key[key] = catalog_action.action_id
                        else:
                            for key in catalog_action.reserved_keys:
                                pending_by_key[key] = catalog_action.action_id
                            apply_results(
                                [runner.complete_local(catalog_action, selected)]
                            )
                except Exception as exc:
                    failures += 1
                    errors[review_entry["key"]] = str(exc)
                    if catalog_disposition_raw:
                        errors[line_key(catalog_disposition_raw)] = str(exc)

            if runner.pending_count:
                self.console.print(
                    f"Saving {runner.pending_count} item(s) before finishing...",
                    markup=False,
                )
            while runner.pending_count:
                result = runner.wait_for_result(timeout=0.1)
                if result:
                    apply_results([result])
            apply_results(runner.drain())
            self._show_summary(summary, entries_by_key, errors)
        finally:
            runner.shutdown()
            prefetcher.shutdown()
            checkpoint.flush()
        return failures


def pending_summary(console: Console, queue: PendingCatalogQueue, index: CatalogIndex) -> None:
    entries = queue.entries(index)
    counts = {group: 0 for group in REVIEW_GROUPS}
    for entry in entries:
        counts[review_group(entry)] += 1
    console.print("Pending review:", markup=False)
    for group in REVIEW_GROUPS:
        console.print(
            f"  {REVIEW_GROUP_LABELS[group]}: {counts[group]}", markup=False
        )
