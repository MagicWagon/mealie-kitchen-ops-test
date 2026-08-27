import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

import kitchen_ops_parser as parser
from kitchen_ops_catalog import (
    CatalogApi,
    CatalogIndex,
    CatalogReviewer,
    PendingCatalogQueue,
    replay_ready_recipes,
)


FOODS = [
    {
        "id": "food-cocoa",
        "name": "cocoa powder",
        "pluralName": "cocoa powder",
        "aliases": [{"name": "chocolate powder"}],
    },
    {"id": "food-sugar", "name": "sugar", "aliases": []},
]

UNITS = [
    {
        "id": "unit-tbsp",
        "name": "tablespoon",
        "pluralName": "tablespoons",
        "abbreviation": "tbsp",
        "aliases": [{"name": "T"}],
    }
]


class FakeApi:
    def __init__(self):
        self.catalog = {"food": copy.deepcopy(FOODS), "unit": copy.deepcopy(UNITS)}
        self.created = []
        self.aliases = []
        self.recipes = {}
        self.updated_recipes = []

    def refresh(self, index, kind=None):
        for current in ((kind,) if kind else ("food", "unit")):
            index.replace(current, self.catalog[current])

    def create_item(self, kind, proposal):
        item = {**copy.deepcopy(proposal), "id": f"{kind}-new-{len(self.created)}"}
        item.setdefault("aliases", [])
        self.catalog[kind].append(item)
        self.created.append((kind, copy.deepcopy(proposal)))
        return copy.deepcopy(item)

    def add_alias(self, kind, item_id, alias_name):
        item = next(item for item in self.catalog[kind] if item["id"] == item_id)
        if alias_name.casefold() not in {alias["name"].casefold() for alias in item.get("aliases", [])}:
            item.setdefault("aliases", []).append({"name": alias_name})
        self.aliases.append((kind, item_id, alias_name))
        return copy.deepcopy(item)

    def get_recipe(self, slug):
        return copy.deepcopy(self.recipes[slug])

    def update_recipe(self, slug, payload):
        self.recipes[slug] = copy.deepcopy(payload)
        self.updated_recipes.append(slug)
        return copy.deepcopy(payload)


def blocked_record(slug="recipe-one", food="drinking chocolate", unit=None):
    source = ["1 tbsp drinking chocolate"]
    proposed = [{"quantity": 1, "food": {"name": food}, "note": ""}]
    missing = [
        {
            "kind": "food",
            "name": food,
            "proposal": {"name": food},
            "ingredientIndex": 0,
            "raw": source[0],
            "ambiguous": False,
        }
    ]
    if unit:
        proposed[0]["unit"] = {"name": unit}
        missing.append(
            {
                "kind": "unit",
                "name": unit,
                "proposal": {"name": unit},
                "ingredientIndex": 0,
                "raw": source[0],
                "ambiguous": False,
            }
        )
    return {
        "slug": slug,
        "sourceIngredients": source,
        "proposedIngredients": proposed,
        "missing": missing,
    }


class CatalogIndexTests(unittest.TestCase):
    def test_resolves_canonical_alias_plural_and_abbreviation(self):
        index = CatalogIndex()
        index.replace("food", FOODS)
        index.replace("unit", UNITS)

        self.assertEqual(index.resolve("food", "Chocolate Powder")[0]["id"], "food-cocoa")
        self.assertEqual(index.resolve("unit", "tablespoons")[0]["id"], "unit-tbsp")
        self.assertEqual(index.resolve("unit", "TBSP")[0]["id"], "unit-tbsp")

    def test_duplicate_alias_is_ambiguous(self):
        index = CatalogIndex()
        foods = copy.deepcopy(FOODS)
        foods[1]["aliases"] = [{"name": "chocolate powder"}]
        index.replace("food", foods)

        item, ambiguous = index.resolve("food", "chocolate powder")
        self.assertIsNone(item)
        self.assertTrue(ambiguous)


class QueueAndReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "pending.json"
        self.queue = PendingCatalogQueue(self.path)
        self.index = CatalogIndex()
        self.api = FakeApi()
        self.api.refresh(self.index)
        self.console = Console(file=open(Path(self.temp.name) / "console.txt", "w"))

    def tearDown(self):
        self.console.file.close()
        self.temp.cleanup()

    def test_queue_deduplicates_catalog_entry_and_keeps_occurrences(self):
        first = blocked_record("one")
        second = blocked_record("two")
        self.queue.upsert_recipe(first)
        self.queue.upsert_recipe(second)
        self.queue.save()

        loaded = PendingCatalogQueue(self.path).load()
        entries = loaded.entries(self.index)
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0]["occurrences"]), 2)

    def test_create_and_map_with_alias_persist_resolutions(self):
        self.queue.upsert_recipe(blocked_record())
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)
        entry = self.queue.entries(self.index)[0]
        created = reviewer.create(entry)
        self.assertEqual(created["name"], "drinking chocolate")
        self.assertEqual(self.api.created[0][0], "food")

        second = blocked_record("recipe-two", food="cacao")
        self.queue.upsert_recipe(second)
        entry = next(item for item in self.queue.entries(self.index) if item["name"] == "cacao")
        reviewer.map(entry, FOODS[0], add_alias=True)
        self.assertIn(("food", "food-cocoa", "cacao"), self.api.aliases)
        self.assertEqual(self.queue.resolution("food", "cacao", self.index)[0]["id"], "food-cocoa")

    def test_edit_create_and_one_time_map(self):
        self.queue.upsert_recipe(blocked_record())
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)
        entry = self.queue.entries(self.index)[0]
        reviewer.create(entry, {"name": "drinking cocoa", "pluralName": "drinking cocoa"})
        self.assertEqual(self.api.created[0][1]["name"], "drinking cocoa")

        self.queue.upsert_recipe(blocked_record("two", food="cacao nib powder"))
        entry = next(
            item for item in self.queue.entries(self.index) if item["name"] == "cacao nib powder"
        )
        reviewer.map(entry, FOODS[0], add_alias=False)
        self.assertNotIn(("food", "food-cocoa", "cacao nib powder"), self.api.aliases)
        self.assertEqual(
            self.queue.resolution("food", "cacao nib powder", self.index)[0]["id"],
            "food-cocoa",
        )

    def test_deferred_entry_remains_queued(self):
        self.queue.upsert_recipe(blocked_record())
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)
        with patch("kitchen_ops_catalog.Prompt.ask", return_value="s"):
            reviewer.review()
        self.assertEqual(len(self.queue.entries(self.index)), 1)

    def test_confirmed_bulk_create_resolves_all_entries(self):
        self.queue.upsert_recipe(blocked_record("one", food="malted cocoa"))
        self.queue.upsert_recipe(blocked_record("two", food="dark cocoa"))
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)
        with patch("kitchen_ops_catalog.Prompt.ask", return_value="a"), patch(
            "kitchen_ops_catalog.Confirm.ask", return_value=True
        ):
            reviewer.review()
        self.assertEqual(len(self.api.created), 2)
        self.assertEqual(self.queue.entries(self.index), [])

    def test_review_reports_catalog_action_failure(self):
        self.queue.upsert_recipe(blocked_record())
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)
        with patch("kitchen_ops_catalog.Prompt.ask", side_effect=["c", "q"]), patch.object(
            reviewer, "create", side_effect=RuntimeError("API unavailable")
        ):
            failures = reviewer.review()
        self.assertEqual(failures, 1)
        self.assertEqual(len(self.queue.entries(self.index)), 1)

    def test_corrupt_queue_is_preserved(self):
        self.path.write_text("not json", encoding="utf-8")
        loaded = PendingCatalogQueue(self.path).load()
        self.assertIsNotNone(loaded.corrupt_backup)
        self.assertTrue(loaded.corrupt_backup.exists())
        self.assertFalse(self.path.exists())


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.queue = PendingCatalogQueue(Path(self.temp.name) / "pending.json")
        self.index = CatalogIndex()
        self.api = FakeApi()
        self.api.refresh(self.index)

    def tearDown(self):
        self.temp.cleanup()

    def test_retries_only_fully_resolved_recipe(self):
        record = blocked_record(unit="scoop")
        self.queue.upsert_recipe(record)
        self.api.recipes[record["slug"]] = {
            "slug": record["slug"],
            "recipeIngredient": copy.deepcopy(record["sourceIngredients"]),
        }
        self.queue.set_resolution("food", "drinking chocolate", FOODS[0])

        stats = replay_ready_recipes(self.queue, self.index, self.api, set())
        self.assertEqual(stats["waiting"], 1)
        self.assertEqual(self.api.updated_recipes, [])

        new_unit = {"id": "unit-scoop", "name": "scoop", "aliases": []}
        self.api.catalog["unit"].append(new_unit)
        self.api.refresh(self.index, "unit")
        stats = replay_ready_recipes(self.queue, self.index, self.api, set())
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(self.api.updated_recipes, [record["slug"]])

    def test_stale_recipe_is_not_overwritten(self):
        record = blocked_record()
        self.queue.upsert_recipe(record)
        self.queue.set_resolution("food", "drinking chocolate", FOODS[0])
        self.api.recipes[record["slug"]] = {
            "slug": record["slug"],
            "recipeIngredient": ["recipe changed"],
        }

        history = {record["slug"]}
        stats = replay_ready_recipes(self.queue, self.index, self.api, history)
        self.assertEqual(stats["stale"], 1)
        self.assertEqual(self.api.updated_recipes, [])
        self.assertNotIn(record["slug"], self.queue.recipes)
        self.assertNotIn(record["slug"], history)


class FakeResponse:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return copy.deepcopy(self._data)


class RecordingHttpSession:
    def __init__(self, item):
        self.item = copy.deepcopy(item)
        self.put_payload = None
        self.headers = {}

    def get(self, url, timeout=None, params=None):
        return FakeResponse(200, self.item)

    def put(self, url, json=None, timeout=None):
        self.put_payload = copy.deepcopy(json)
        return FakeResponse(200, json)


class CatalogApiTests(unittest.TestCase):
    def test_alias_update_preserves_existing_food_fields(self):
        existing = {
            "id": "food-cocoa",
            "name": "cocoa powder",
            "pluralName": "cocoa powder",
            "description": "Unsweetened cocoa",
            "aliases": [{"name": "cacao powder"}],
            "extras": {"source": "manual"},
        }
        session = RecordingHttpSession(existing)
        api = CatalogApi("http://mealie", "token", session=session)

        api.add_alias("food", "food-cocoa", "chocolate powder")

        self.assertEqual(session.put_payload["description"], "Unsweetened cocoa")
        self.assertEqual(session.put_payload["extras"], {"source": "manual"})
        self.assertEqual(
            [alias["name"] for alias in session.put_payload["aliases"]],
            ["cacao powder", "chocolate powder"],
        )


class FakeParserSession:
    def __init__(self, recipe, parsed):
        self.recipe = recipe
        self.parsed = parsed
        self.puts = []

    def get(self, url, timeout=None):
        return FakeResponse(200, self.recipe)

    def post(self, url, json=None, timeout=None):
        return FakeResponse(200, self.parsed)

    def put(self, url, json=None, timeout=None):
        self.puts.append((url, json))
        return FakeResponse(200, json)


class ParserSafetyTests(unittest.TestCase):
    def test_missing_food_and_unit_never_put_recipe(self):
        recipe = {"slug": "test", "recipeIngredient": ["1 scoop drinking chocolate"]}
        parsed = [
            {
                "confidence": {"average": 1.0},
                "ingredient": {
                    "quantity": 1,
                    "food": {"name": "drinking chocolate"},
                    "unit": {"name": "scoop"},
                    "note": "",
                },
            }
        ]
        session = FakeParserSession(recipe, parsed)
        empty_index = CatalogIndex()
        empty_index.replace("food", [])
        empty_index.replace("unit", [])

        with patch.object(parser, "get_session", return_value=session), patch.object(
            parser, "CATALOG_INDEX", empty_index
        ), patch.object(parser, "DRY_RUN", False):
            result = parser.process_recipe("test")

        self.assertEqual(result.status, "blocked")
        self.assertEqual({item["kind"] for item in result.blocked_record["missing"]}, {"food", "unit"})
        self.assertEqual(session.puts, [])

    def test_dry_run_never_puts_recipe(self):
        recipe = {"slug": "test", "recipeIngredient": ["1 tbsp chocolate powder"]}
        parsed = [
            {
                "confidence": {"average": 1.0},
                "ingredient": {
                    "quantity": 1,
                    "food": {"name": "chocolate powder"},
                    "unit": {"name": "tbsp"},
                },
            }
        ]
        session = FakeParserSession(recipe, parsed)
        index = CatalogIndex()
        index.replace("food", FOODS)
        index.replace("unit", UNITS)

        with patch.object(parser, "get_session", return_value=session), patch.object(
            parser, "CATALOG_INDEX", index
        ), patch.object(parser, "DRY_RUN", True):
            result = parser.process_recipe("test")

        self.assertEqual(result.status, "dry_run")
        self.assertEqual(session.puts, [])


if __name__ == "__main__":
    unittest.main()
