import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

import kitchen_ops_parser as parser
from kitchen_ops_catalog import (
    CatalogApi,
    CatalogActionJournal,
    CatalogIndex,
    CatalogReviewer,
    PendingCatalogQueue,
    QueueCheckpointWriter,
    ToolIndex,
    classify_review_line,
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
        self.tools = [{"id": "tool-air-fryer", "name": "Air Fryer", "slug": "air-fryer"}]
        self.created_tools = []

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

    def list_tools(self):
        return copy.deepcopy(self.tools)

    def create_tool(self, name):
        existing = next(
            (tool for tool in self.tools if tool["name"].casefold() == name.casefold()),
            None,
        )
        if existing:
            return copy.deepcopy(existing)
        tool = {
            "id": f"tool-new-{len(self.created_tools)}",
            "name": name,
            "slug": name.casefold().replace(" ", "-"),
        }
        self.tools.append(tool)
        self.created_tools.append(name)
        return copy.deepcopy(tool)


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


def flagged_record(
    slug="air-fryer-recipe",
    raw="Air Fryer. I use the Breville Smart Oven Air",
    food="Air Fryer. I use the Breville Smart Oven Air",
):
    record = blocked_record(slug, food=food)
    record["sourceIngredients"] = [raw]
    record["proposedIngredients"][0]["food"]["name"] = food
    record["missing"][0]["raw"] = raw
    return record


class LineDetectionTests(unittest.TestCase):
    def test_air_fryer_prose_is_equipment(self):
        result = classify_review_line("Air Fryer. I use the Breville Smart Oven Air")

        self.assertEqual(result["recommendation"], "equipment")
        self.assertEqual(result["toolMatches"], ["Air Fryer"])

    def test_note_prose_is_flagged_but_normal_ingredients_are_not(self):
        self.assertEqual(
            classify_review_line("I prefer to make this a day ahead.")["recommendation"],
            "note",
        )
        self.assertIsNone(classify_review_line("1 skillet steak"))
        self.assertIsNone(classify_review_line("skillet steak"))
        self.assertIsNone(classify_review_line("parsley for serving"))
        self.assertIsNone(classify_review_line("salt and pepper, to taste."))
        self.assertIsNone(classify_review_line("1 cup sugar"))
        self.assertEqual(
            classify_review_line("Let rest before slicing.")["recommendation"], "note"
        )

    def test_multiple_equipment_matches_are_preserved_for_numbered_choice(self):
        result = classify_review_line("Use the wok on the grill.")

        self.assertEqual(result["recommendation"], "equipment")
        self.assertEqual(set(result["toolMatches"]), {"Wok", "Smoker / Grill"})


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

    def test_incremental_upsert_updates_resolution_and_search(self):
        index = CatalogIndex()
        index.replace("food", FOODS)
        updated = copy.deepcopy(FOODS[0])
        updated["aliases"].append({"name": "cacao"})

        index.upsert("food", updated)

        self.assertEqual(index.resolve("food", "cacao")[0]["id"], "food-cocoa")
        self.assertEqual(index.search("food", "cacao", 1)[0]["id"], "food-cocoa")

    def test_rapid_search_meets_large_catalog_budget(self):
        index = CatalogIndex()
        index.replace(
            "food",
            (
                {
                    "id": str(number),
                    "name": f"ingredient {number}",
                    "pluralName": f"ingredients {number}",
                    "aliases": [],
                }
                for number in range(10_000)
            ),
        )

        started = time.perf_counter()
        index.search("food", "no cook lasagna noodles", 5)
        uncached = time.perf_counter() - started
        started = time.perf_counter()
        index.search("food", "no cook lasagna noodles", 5)
        cached = time.perf_counter() - started

        self.assertLess(uncached, 0.1)
        self.assertLess(cached, 0.05)


class QueueAndReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "pending.json"
        self.queue = PendingCatalogQueue(self.path)
        self.index = CatalogIndex()
        self.api = FakeApi()
        self.api.refresh(self.index)
        self.console_path = Path(self.temp.name) / "console.txt"
        self.console = Console(file=open(self.console_path, "w"))

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

    def test_existing_queue_backfills_line_review_and_hides_food_proposal(self):
        self.queue.upsert_recipe(flagged_record())

        entries = self.queue.entries(self.index)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "line")
        self.assertEqual(entries[0]["recommendation"], "equipment")
        self.assertEqual(entries[0]["catalogEntries"][0]["kind"], "food")

    def test_note_disposition_applies_to_identical_raw_text_only(self):
        self.queue.upsert_recipe(flagged_record("one"))
        self.queue.upsert_recipe(flagged_record("two"))
        self.queue.upsert_recipe(
            flagged_record("three", raw="Air Fryer: use the countertop model")
        )
        self.queue.set_line_disposition(
            "Air Fryer. I use the Breville Smart Oven Air", "note"
        )

        entries = self.queue.entries(self.index)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], "Air Fryer: use the countertop model")

    def test_flagged_line_defaults_to_skip_and_uses_context_menu(self):
        self.queue.upsert_recipe(flagged_record())
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)

        with patch("kitchen_ops_catalog.Prompt.ask", side_effect=["8"]):
            reviewer.review()

        self.assertIsNone(
            self.queue.line_disposition("Air Fryer. I use the Breville Smart Oven Air")
        )

    def test_accepting_equipment_uses_existing_tool_and_persists_disposition(self):
        raw = "Air Fryer. I use the Breville Smart Oven Air"
        self.queue.upsert_recipe(flagged_record(raw=raw))
        tools = ToolIndex()
        tools.replace(self.api.list_tools())
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console, tools)

        with patch("kitchen_ops_catalog.Prompt.ask", return_value="1"):
            reviewer.review()

        disposition = self.queue.line_disposition(raw)
        self.assertEqual(disposition["type"], "equipment")
        self.assertEqual(disposition["toolName"], "Air Fryer")
        self.assertEqual(self.api.created_tools, [])
        loaded = PendingCatalogQueue(self.path).load()
        self.assertEqual(loaded.line_disposition(raw)["toolName"], "Air Fryer")

    def test_missing_equipment_tool_requires_confirmation_before_background_create(self):
        raw = "Wok. I use a carbon steel model."
        self.queue.upsert_recipe(flagged_record(raw=raw, food=raw))
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)

        with patch("kitchen_ops_catalog.Prompt.ask", return_value="1"), patch(
            "kitchen_ops_catalog.Confirm.ask", return_value=True
        ) as confirm:
            reviewer.review()

        self.assertEqual(self.api.created_tools, ["Wok"])
        self.assertEqual(self.queue.line_disposition(raw)["toolName"], "Wok")
        self.assertFalse(confirm.call_args.kwargs["default"])

    def test_confirming_flagged_line_as_ingredient_reveals_catalog_proposal(self):
        raw = "Air Fryer. I use the Breville Smart Oven Air"
        self.queue.upsert_recipe(flagged_record(raw=raw))
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)

        with patch("kitchen_ops_catalog.Prompt.ask", side_effect=["7", "5"]):
            reviewer.review()

        self.assertEqual(self.queue.line_disposition(raw)["type"], "ingredient")
        entries = self.queue.entries(self.index)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "food")

    def test_cancelling_flagged_alias_mapping_does_not_classify_the_line(self):
        raw = "Air Fryer. I use the Breville Smart Oven Air"
        self.queue.upsert_recipe(flagged_record(raw=raw, food="air fryer food"))
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)

        with patch(
            "kitchen_ops_catalog.Prompt.ask", side_effect=["5", "1", "8"]
        ), patch("kitchen_ops_catalog.Confirm.ask", return_value=False):
            reviewer.review()

        self.assertIsNone(self.queue.line_disposition(raw))

    def test_failed_flagged_create_does_not_classify_the_line(self):
        raw = "Air Fryer. I use the Breville Smart Oven Air"
        self.queue.upsert_recipe(flagged_record(raw=raw))
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)

        with patch("kitchen_ops_catalog.Prompt.ask", side_effect=["3", "10"]), patch.object(
            self.api, "create_item", side_effect=RuntimeError("API unavailable")
        ):
            reviewer.review()

        self.assertIsNone(self.queue.line_disposition(raw))

    def test_successful_flagged_create_commits_ingredient_classification(self):
        raw = "Air Fryer. I use the Breville Smart Oven Air"
        self.queue.upsert_recipe(flagged_record(raw=raw))
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)

        with patch("kitchen_ops_catalog.Prompt.ask", return_value="3"):
            reviewer.review()

        self.assertEqual(self.queue.line_disposition(raw)["type"], "ingredient")
        self.assertEqual(len(self.api.created), 1)

    def test_bulk_review_excludes_flagged_lines(self):
        raw = "Air Fryer. I use the Breville Smart Oven Air"
        self.queue.upsert_recipe(flagged_record(raw=raw))
        self.queue.upsert_recipe(blocked_record("ordinary", food="malted cocoa"))
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)

        with patch("kitchen_ops_catalog.Prompt.ask", side_effect=["9", "10"]), patch(
            "kitchen_ops_catalog.Confirm.ask", return_value=True
        ):
            reviewer.review()

        self.assertEqual([proposal[1]["name"] for proposal in self.api.created], ["malted cocoa"])
        self.assertIsNone(self.queue.line_disposition(raw))
        self.console.file.flush()
        self.assertIn("Manual classification required", self.console_path.read_text())

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
        with patch("kitchen_ops_catalog.Prompt.ask", return_value="5") as prompt:
            reviewer.review()
        self.assertEqual(len(self.queue.entries(self.index)), 1)
        self.assertEqual(prompt.call_args.kwargs["default"], "5")
        self.assertEqual(prompt.call_args.kwargs["choices"], ["1", "2", "3", "4", "5", "6", "7"])

    def test_confirmed_bulk_create_resolves_all_entries(self):
        self.queue.upsert_recipe(blocked_record("one", food="malted cocoa"))
        self.queue.upsert_recipe(blocked_record("two", food="dark cocoa"))
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)
        with patch("kitchen_ops_catalog.Prompt.ask", return_value="6"), patch(
            "kitchen_ops_catalog.Confirm.ask", return_value=True
        ):
            reviewer.review()
        self.assertEqual(len(self.api.created), 2)
        self.assertEqual(self.queue.entries(self.index), [])

    def test_review_reports_catalog_action_failure(self):
        self.queue.upsert_recipe(blocked_record())
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)
        with patch("kitchen_ops_catalog.Prompt.ask", side_effect=["1", "7"]), patch.object(
            self.api, "create_item", side_effect=RuntimeError("API unavailable")
        ):
            failures = reviewer.review()
        self.assertEqual(failures, 1)
        self.assertEqual(len(self.queue.entries(self.index)), 1)

    def test_review_shows_submitted_fields_and_only_two_usage_examples(self):
        for slug in ("one", "two", "three"):
            record = blocked_record(slug, food="no cook lasagna noodles")
            record["missing"][0]["proposal"] = {
                "name": "no cook lasagna noodles",
                "pluralName": "no cook lasagna noodles",
                "description": "Oven-ready pasta",
                "aliases": [{"name": "oven-ready lasagna"}],
            }
            self.queue.upsert_recipe(record)
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)
        entry = self.queue.entries(self.index)[0]

        reviewer._show_entry(entry, 1, 0)
        self.console.file.flush()
        output = self.console_path.read_text()

        self.assertIn("name: 'no cook lasagna noodles'", output)
        self.assertIn("pluralName: 'no cook lasagna noodles'", output)
        self.assertIn("description: 'Oven-ready pasta'", output)
        self.assertIn("aliases: 'oven-ready lasagna'", output)
        self.assertIn("one: 1 tbsp drinking chocolate", output)
        self.assertIn("two: 1 tbsp drinking chocolate", output)
        self.assertNotIn("three: 1 tbsp drinking chocolate", output)
        self.assertIn("…and 1 more recipe usages", output)

    def test_create_rebuilds_queue_and_resolves_plural_and_alias_entries(self):
        primary = blocked_record("one", food="alpha noodle")
        primary["missing"][0]["proposal"] = {
            "name": "alpha noodle",
            "pluralName": "alpha noodles",
            "aliases": [{"name": "alpha ziti"}],
        }
        self.queue.upsert_recipe(primary)
        self.queue.upsert_recipe(blocked_record("two", food="alpha noodles"))
        self.queue.upsert_recipe(blocked_record("three", food="alpha ziti"))
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)

        with patch("kitchen_ops_catalog.Prompt.ask", return_value="1"):
            reviewer.review()

        self.assertEqual(len(self.api.created), 1)
        self.assertEqual(self.queue.entries(self.index), [])
        self.console.file.flush()
        self.assertIn("Also resolved 2 queued item(s)", self.console_path.read_text())

    def test_numbered_mapping_selection_confirms_and_adds_alias(self):
        self.queue.upsert_recipe(blocked_record(food="cacao"))
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)

        with patch("kitchen_ops_catalog.Prompt.ask", side_effect=["3", "1"]), patch(
            "kitchen_ops_catalog.Confirm.ask", return_value=True
        ) as confirm:
            reviewer.review()

        self.assertIn(("food", "food-cocoa", "cacao"), self.api.aliases)
        self.assertIn("save the proposed name as an alias", confirm.call_args.args[0])
        self.assertFalse(confirm.call_args.kwargs["default"])

    def test_mapping_can_search_after_numbered_suggestions(self):
        self.queue.upsert_recipe(blocked_record(food="cacao"))
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)
        entry = self.queue.entries(self.index)[0]

        with patch("kitchen_ops_catalog.Prompt.ask", side_effect=["3", "sugar", "1"]):
            selected = reviewer.choose_mapping(entry)

        self.assertEqual(selected["id"], "food-sugar")

    def test_bulk_cancel_changes_nothing(self):
        self.queue.upsert_recipe(blocked_record(food="malted cocoa"))
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)

        with patch("kitchen_ops_catalog.Prompt.ask", side_effect=["6", "7"]), patch(
            "kitchen_ops_catalog.Confirm.ask", return_value=False
        ):
            reviewer.review()

        self.assertEqual(self.api.created, [])
        self.assertEqual(len(self.queue.entries(self.index)), 1)

    def test_bulk_excludes_ambiguous_entries(self):
        eligible = blocked_record("one", food="malted cocoa")
        ambiguous = blocked_record("two", food="mystery cocoa")
        ambiguous["missing"][0]["ambiguous"] = True
        self.queue.upsert_recipe(eligible)
        self.queue.upsert_recipe(ambiguous)
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)

        with patch("kitchen_ops_catalog.Prompt.ask", side_effect=["6", "7"]), patch(
            "kitchen_ops_catalog.Confirm.ask", return_value=True
        ):
            reviewer.review()

        self.assertEqual([item[1]["name"] for item in self.api.created], ["malted cocoa"])
        remaining = self.queue.entries(self.index)
        self.assertEqual([entry["name"] for entry in remaining], ["mystery cocoa"])

    def test_bulk_continues_after_an_item_failure(self):
        self.queue.upsert_recipe(blocked_record("one", food="dark cocoa"))
        self.queue.upsert_recipe(blocked_record("two", food="malted cocoa"))
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)
        real_create = self.api.create_item

        def create_with_failure(kind, proposal):
            if proposal["name"] == "dark cocoa":
                raise RuntimeError("API unavailable")
            return real_create(kind, proposal)

        with patch("kitchen_ops_catalog.Prompt.ask", side_effect=["6", "7"]), patch(
            "kitchen_ops_catalog.Confirm.ask", return_value=True
        ), patch.object(self.api, "create_item", side_effect=create_with_failure):
            failures = reviewer.review()

        self.assertEqual(failures, 1)
        self.assertEqual([item[1]["name"] for item in self.api.created], ["malted cocoa"])
        self.assertEqual(
            [entry["name"] for entry in self.queue.entries(self.index)], ["dark cocoa"]
        )

    def test_bulk_revalidates_entries_resolved_earlier_in_the_batch(self):
        primary = blocked_record("one", food="alpha noodle")
        primary["missing"][0]["proposal"] = {
            "name": "alpha noodle",
            "pluralName": "alpha noodles",
        }
        self.queue.upsert_recipe(primary)
        self.queue.upsert_recipe(blocked_record("two", food="alpha noodles"))
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)

        with patch("kitchen_ops_catalog.Prompt.ask", return_value="6"), patch(
            "kitchen_ops_catalog.Confirm.ask", return_value=True
        ):
            reviewer.review()

        self.assertEqual([item[1]["name"] for item in self.api.created], ["alpha noodle"])
        self.assertEqual(self.queue.entries(self.index), [])
        self.console.file.flush()
        self.assertIn("Also resolved 1 queued item(s)", self.console_path.read_text())

    def test_review_advances_while_create_is_still_running(self):
        self.queue.upsert_recipe(blocked_record("one", food="alpha food"))
        self.queue.upsert_recipe(blocked_record("two", food="beta food"))
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)
        api_started = threading.Event()
        release_api = threading.Event()
        real_create = self.api.create_item
        action_times = []

        def delayed_create(kind, proposal):
            api_started.set()
            release_api.wait(timeout=2)
            return real_create(kind, proposal)

        def prompt(question, **kwargs):
            if question != "Choose an action":
                raise AssertionError(f"Unexpected prompt: {question}")
            action_times.append(time.perf_counter())
            if len(action_times) == 1:
                return "1"
            self.assertTrue(api_started.wait(timeout=1))
            release_api.set()
            return "7"

        with patch("kitchen_ops_catalog.Prompt.ask", side_effect=prompt), patch.object(
            self.api, "create_item", side_effect=delayed_create
        ):
            reviewer.review()

        self.assertEqual(len(action_times), 2)
        self.assertLess(action_times[1] - action_times[0], 0.15)
        self.assertEqual([item[1]["name"] for item in self.api.created], ["alpha food"])

    def test_background_catalog_writes_are_serial(self):
        for slug, food in (("one", "alpha food"), ("two", "beta food"), ("three", "gamma food")):
            self.queue.upsert_recipe(blocked_record(slug, food=food))
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)
        real_create = self.api.create_item
        active = 0
        maximum_active = 0
        lock = threading.Lock()

        def tracked_create(kind, proposal):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.03)
            try:
                return real_create(kind, proposal)
            finally:
                with lock:
                    active -= 1

        with patch("kitchen_ops_catalog.Prompt.ask", side_effect=["1", "1", "7"]), patch.object(
            self.api, "create_item", side_effect=tracked_create
        ):
            reviewer.review()

        self.assertEqual(maximum_active, 1)
        self.assertEqual(
            [item[1]["name"] for item in self.api.created], ["alpha food", "beta food"]
        )

    def test_failed_create_restores_related_reserved_entries(self):
        primary = blocked_record("one", food="alpha noodle")
        primary["missing"][0]["proposal"] = {
            "name": "alpha noodle",
            "pluralName": "alpha noodles",
        }
        self.queue.upsert_recipe(primary)
        self.queue.upsert_recipe(blocked_record("two", food="alpha noodles"))
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)

        with patch("kitchen_ops_catalog.Prompt.ask", side_effect=["1", "7"]), patch.object(
            self.api, "create_item", side_effect=RuntimeError("API unavailable")
        ):
            failures = reviewer.review()

        self.assertEqual(failures, 1)
        self.assertEqual(
            [entry["name"] for entry in self.queue.entries(self.index)],
            ["alpha noodle", "alpha noodles"],
        )
        self.console.file.flush()
        output = self.console_path.read_text()
        self.assertIn("failed and returned to review", output)
        self.assertIn("Previous action failed: API unavailable", output)

    def test_review_actions_do_not_refresh_the_full_catalog(self):
        self.queue.upsert_recipe(blocked_record(food="new cocoa"))
        reviewer = CatalogReviewer(self.queue, self.index, self.api, self.console)

        with patch.object(self.api, "refresh", wraps=self.api.refresh) as refresh, patch(
            "kitchen_ops_catalog.Prompt.ask", return_value="1"
        ):
            reviewer.review()

        refresh.assert_not_called()

    def test_corrupt_queue_is_preserved(self):
        self.path.write_text("not json", encoding="utf-8")
        loaded = PendingCatalogQueue(self.path).load()
        self.assertIsNotNone(loaded.corrupt_backup)
        self.assertTrue(loaded.corrupt_backup.exists())
        self.assertFalse(self.path.exists())


class CatalogJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "pending.json"
        self.queue = PendingCatalogQueue(self.path)
        self.queue.upsert_recipe(blocked_record(food="original phrase"))
        self.queue.save()
        self.index = CatalogIndex()
        self.index.replace("food", FOODS)

    def tearDown(self):
        self.temp.cleanup()

    def submit_create(self, journal):
        return journal.append(
            "submitted",
            actionId="action-one",
            operation="create",
            kind="food",
            sourceName="original phrase",
            reservedKeys=["food:original phrase"],
            proposal={"name": "created food"},
            targetItem=None,
        )

    def test_completed_action_replays_after_crash(self):
        journal = CatalogActionJournal(self.path)
        self.submit_create(journal)
        item = {"id": "food-created", "name": "created food", "aliases": []}
        journal.append("completed", actionId="action-one", item=item)
        loaded = PendingCatalogQueue(self.path).load()

        errors = journal.recover(loaded, self.index)

        self.assertEqual(errors, {})
        self.assertEqual(
            loaded.resolution("food", "original phrase", self.index)[0]["id"],
            "food-created",
        )
        self.assertFalse(journal.path.exists())

    def test_uncertain_create_is_reconciled_from_catalog(self):
        journal = CatalogActionJournal(self.path)
        self.submit_create(journal)
        item = {"id": "food-created", "name": "created food", "aliases": []}
        self.index.upsert("food", item)
        loaded = PendingCatalogQueue(self.path).load()

        errors = journal.recover(loaded, self.index)

        self.assertEqual(errors, {})
        self.assertEqual(
            loaded.resolution("food", "original phrase", self.index)[0]["id"],
            "food-created",
        )

    def test_failed_action_returns_recovery_error(self):
        journal = CatalogActionJournal(self.path)
        self.submit_create(journal)
        journal.append("failed", actionId="action-one", error="API unavailable")
        loaded = PendingCatalogQueue(self.path).load()

        errors = journal.recover(loaded, self.index)

        self.assertEqual(errors["food:original phrase"], "API unavailable")

    def test_uncertain_alias_is_reconciled_from_target_item(self):
        target = copy.deepcopy(FOODS[0])
        target["aliases"].append({"name": "original phrase"})
        self.index.upsert("food", target)
        journal = CatalogActionJournal(self.path)
        journal.append(
            "submitted",
            actionId="alias-action",
            operation="map_alias",
            kind="food",
            sourceName="original phrase",
            reservedKeys=["food:original phrase"],
            proposal=None,
            targetItem=target,
        )
        loaded = PendingCatalogQueue(self.path).load()

        errors = journal.recover(loaded, self.index)

        self.assertEqual(errors, {})
        self.assertEqual(
            loaded.resolution("food", "original phrase", self.index)[0]["id"],
            "food-cocoa",
        )

    def test_truncated_final_journal_record_is_ignored_and_repaired(self):
        journal = CatalogActionJournal(self.path)
        self.submit_create(journal)
        with journal.path.open("a", encoding="utf-8") as handle:
            handle.write('{"sequence": 2, "status":')

        records = journal.records()

        self.assertEqual(len(records), 1)
        repaired_lines = journal.path.read_text().splitlines()
        self.assertEqual(len(repaired_lines), 1)
        self.assertEqual(json.loads(repaired_lines[0])["status"], "submitted")

    def test_sequence_continues_after_checkpoint_compaction(self):
        self.queue.data["checkpointSequence"] = 5
        self.queue.save()
        journal = CatalogActionJournal(self.path, checkpoint_sequence=5)

        record = journal.append("failed", actionId="new-action", error="failure")

        self.assertEqual(record["sequence"], 6)

    def test_checkpoint_keeps_records_appended_after_its_snapshot(self):
        journal = CatalogActionJournal(self.path)
        self.submit_create(journal)
        item = {"id": "food-created", "name": "created food", "aliases": []}
        journal.append("completed", actionId="action-one", item=item)
        self.queue.set_resolution("food", "original phrase", item)
        writer = QueueCheckpointWriter(self.queue, journal)
        writer.completed_since_checkpoint = 19

        writer.note_completion()
        newer = journal.append("failed", actionId="newer-action", error="later failure")
        for future in writer.futures:
            future.result()
        remaining = journal.records()
        writer.executor.shutdown(wait=True)

        self.assertEqual([record["sequence"] for record in remaining], [newer["sequence"]])
        checkpointed = PendingCatalogQueue(self.path).load()
        self.assertEqual(checkpointed.data["checkpointSequence"], 2)

    def test_completed_line_disposition_replays_after_crash(self):
        raw = "I prefer to make this a day ahead."
        journal = CatalogActionJournal(self.path)
        journal.append(
            "submitted",
            actionId="line-action",
            operation="classify_note",
            kind="line",
            sourceName=raw,
            reservedKeys=[f"line:{raw.casefold()}"],
            disposition={"type": "note"},
        )
        journal.append(
            "completed",
            actionId="line-action",
            disposition={"type": "note"},
        )
        loaded = PendingCatalogQueue(self.path).load()

        errors = journal.recover(loaded, self.index, ToolIndex())

        self.assertEqual(errors, {})
        self.assertEqual(loaded.line_disposition(raw)["type"], "note")

    def test_completed_flagged_catalog_action_commits_ingredient_disposition(self):
        raw = "Air Fryer. I use the Breville Smart Oven Air"
        journal = CatalogActionJournal(self.path)
        journal.append(
            "submitted",
            actionId="flagged-create",
            operation="create",
            kind="food",
            sourceName="Air Fryer food",
            reservedKeys=["food:air fryer food", f"line:{raw.casefold()}"],
            proposal={"name": "Air Fryer food"},
            disposition={"type": "ingredient"},
            dispositionRaw=raw,
        )
        item = {"id": "food-air-fryer", "name": "Air Fryer food", "aliases": []}
        journal.append("completed", actionId="flagged-create", item=item)
        loaded = PendingCatalogQueue(self.path).load()

        errors = journal.recover(loaded, self.index, ToolIndex())

        self.assertEqual(errors, {})
        self.assertEqual(loaded.line_disposition(raw)["type"], "ingredient")

    def test_uncertain_tool_creation_reconciles_by_exact_tool_name(self):
        raw = "Wok. I use a carbon steel model."
        journal = CatalogActionJournal(self.path)
        journal.append(
            "submitted",
            actionId="tool-action",
            operation="create_tool",
            kind="line",
            sourceName=raw,
            reservedKeys=[f"line:{raw.casefold()}"],
            disposition={"type": "equipment", "toolName": "Wok"},
        )
        tools = ToolIndex()
        tools.replace([{"id": "tool-wok", "name": "Wok"}])
        loaded = PendingCatalogQueue(self.path).load()

        errors = journal.recover(loaded, self.index, tools)

        self.assertEqual(errors, {})
        self.assertEqual(loaded.line_disposition(raw)["toolName"], "Wok")


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

    def test_note_disposition_suppresses_food_and_unit_and_preserves_exact_text(self):
        raw = "I prefer to make this a day ahead."
        record = blocked_record(food="I prefer", unit="day")
        record["sourceIngredients"] = [raw]
        record["missing"][0]["raw"] = raw
        record["missing"][1]["raw"] = raw
        record["lineReviews"] = [
            {"ingredientIndex": 0, **classify_review_line(raw)}
        ]
        self.queue.upsert_recipe(record)
        self.queue.set_line_disposition(raw, "note")
        self.api.recipes[record["slug"]] = {
            "slug": record["slug"],
            "recipeIngredient": [raw],
            "tools": [],
        }

        stats = replay_ready_recipes(self.queue, self.index, self.api, set())

        self.assertEqual(stats["updated"], 1)
        self.assertEqual(
            self.api.recipes[record["slug"]]["recipeIngredient"],
            [{"note": raw, "originalText": raw}],
        )

    def test_equipment_disposition_merges_tool_without_removing_existing_tools(self):
        raw = "Air Fryer. I use the Breville Smart Oven Air"
        record = flagged_record(raw=raw)
        record["lineReviews"] = [
            {"ingredientIndex": 0, **classify_review_line(raw)}
        ]
        self.queue.upsert_recipe(record)
        self.queue.set_line_disposition(raw, "equipment", "Air Fryer")
        existing_tool = {"id": "tool-oven", "name": "Oven", "slug": "oven"}
        self.api.recipes[record["slug"]] = {
            "slug": record["slug"],
            "recipeIngredient": [raw],
            "tools": [existing_tool],
        }
        tools = ToolIndex()
        tools.replace(self.api.list_tools())

        stats = replay_ready_recipes(
            self.queue, self.index, self.api, set(), tool_index=tools
        )

        self.assertEqual(stats["updated"], 1)
        self.assertEqual(
            [tool["name"] for tool in self.api.recipes[record["slug"]]["tools"]],
            ["Oven", "Air Fryer"],
        )

    def test_unresolved_line_blocks_replay_even_when_catalog_name_exists(self):
        raw = "Air Fryer. I use the Breville Smart Oven Air"
        record = flagged_record(raw=raw, food="sugar")
        self.queue.upsert_recipe(record)
        self.api.recipes[record["slug"]] = {
            "slug": record["slug"],
            "recipeIngredient": [raw],
        }

        stats = replay_ready_recipes(self.queue, self.index, self.api, set())

        self.assertEqual(stats["waiting"], 1)
        self.assertEqual(self.api.updated_recipes, [])


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
        self.posts = []

    def get(self, url, timeout=None):
        return FakeResponse(200, self.recipe)

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, copy.deepcopy(json)))
        return FakeResponse(200, self.parsed)

    def put(self, url, json=None, timeout=None):
        self.puts.append((url, json))
        return FakeResponse(200, json)


class ParserSafetyTests(unittest.TestCase):
    def setUp(self):
        parser.LINE_DISPOSITIONS = {}
        parser.TOOL_INDEX = ToolIndex()

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

    def test_fraction_placeholder_is_restored_before_catalog_review(self):
        recipe = {
            "slug": "grilled-flat-iron-steak-fajitas",
            "recipeIngredient": ['3/4" Thick Flat Iron Steaks'],
        }
        parsed = [
            {
                "confidence": {"average": 1.0},
                "ingredient": {
                    "quantity": 1,
                    "food": {"name": '#3$4" Thick Flat Iron Steaks'},
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
            result = parser.process_recipe("grilled-flat-iron-steak-fajitas")

        self.assertEqual(result.status, "blocked")
        missing = result.blocked_record["missing"][0]
        self.assertEqual(missing["name"], '3/4" Thick Flat Iron Steaks')
        self.assertEqual(
            result.blocked_record["proposedIngredients"][0]["food"]["name"],
            '3/4" Thick Flat Iron Steaks',
        )

    def test_likely_equipment_requires_review_even_with_confident_parser_result(self):
        raw = "Air Fryer. I use the Breville Smart Oven Air"
        recipe = {"slug": "air-fryer-note", "recipeIngredient": [raw]}
        parsed = [
            {
                "confidence": {"average": 1.0},
                "ingredient": {"food": {"id": "food-existing", "name": "Air Fryer"}},
            }
        ]
        session = FakeParserSession(recipe, parsed)
        index = CatalogIndex()
        index.replace("food", [{"id": "food-existing", "name": "Air Fryer"}])
        index.replace("unit", [])

        with patch.object(parser, "get_session", return_value=session), patch.object(
            parser, "CATALOG_INDEX", index
        ), patch.object(parser, "DRY_RUN", False):
            result = parser.process_recipe("air-fryer-note")

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.blocked_record["missing"], [])
        self.assertEqual(
            result.blocked_record["lineReviews"][0]["recommendation"], "equipment"
        )
        self.assertEqual(session.puts, [])

    def test_persisted_note_skips_parser_and_preserves_original_text(self):
        raw = "I prefer to make this a day ahead."
        recipe = {"slug": "saved-note", "recipeIngredient": [raw], "tools": []}
        session = FakeParserSession(recipe, [])
        parser.LINE_DISPOSITIONS = {
            raw.casefold(): {"type": "note", "raw": raw}
        }

        with patch.object(parser, "get_session", return_value=session), patch.object(
            parser, "DRY_RUN", False
        ):
            result = parser.process_recipe("saved-note")

        self.assertEqual(result.status, "success")
        self.assertEqual(session.posts, [])
        self.assertEqual(
            session.puts[0][1]["recipeIngredient"],
            [{"note": raw, "originalText": raw}],
        )

    def test_persisted_equipment_skips_parser_and_merges_tool(self):
        raw = "Air Fryer. I use the Breville Smart Oven Air"
        recipe = {
            "slug": "saved-equipment",
            "recipeIngredient": [raw],
            "tools": [{"id": "tool-oven", "name": "Oven"}],
        }
        session = FakeParserSession(recipe, [])
        parser.LINE_DISPOSITIONS = {
            raw.casefold(): {"type": "equipment", "raw": raw, "toolName": "Air Fryer"}
        }
        parser.TOOL_INDEX.replace(
            [{"id": "tool-air-fryer", "name": "Air Fryer", "slug": "air-fryer"}]
        )

        with patch.object(parser, "get_session", return_value=session), patch.object(
            parser, "DRY_RUN", False
        ):
            result = parser.process_recipe("saved-equipment")

        self.assertEqual(result.status, "success")
        self.assertEqual(
            [tool["name"] for tool in session.puts[0][1]["tools"]],
            ["Oven", "Air Fryer"],
        )


if __name__ == "__main__":
    unittest.main()
