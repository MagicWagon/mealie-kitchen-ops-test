import copy
import unittest
from unittest.mock import patch

import kitchen_ops_tagger as tagger


class FakeResponse:
    def __init__(self, status=200, data=None, text=""):
        self.status_code = status
        self._data = data or {}
        self.ok = status < 400
        self.text = text or str(self._data)

    def json(self):
        return copy.deepcopy(self._data)

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(self.status_code)


def recipe(slug="recipe-one", recipe_id="recipe-id", name="Recipe"):
    return {
        "id": recipe_id,
        "slug": slug,
        "name": name,
        "recipeIngredients": [],
        "recipeInstructions": [],
        "tags": [],
        "recipeCategory": [],
        "tools": [],
    }


class OrganizerCatalogTests(unittest.TestCase):
    def test_fetch_catalog_uses_organizer_route_and_follows_pages(self):
        responses = [
            FakeResponse(200, {"items": [{"id": "1", "name": "Soup", "slug": "soup"}], "total_pages": 2}),
            FakeResponse(200, {"items": [{"id": "2", "name": "Pasta", "slug": "pasta"}], "total_pages": 2}),
        ]
        with patch.object(tagger.requests, "get", side_effect=responses) as get:
            catalog = tagger._fetch_catalog("tags", {})

        self.assertEqual(set(catalog), {"soup", "pasta"})
        self.assertEqual(get.call_args_list[0].args[0], "http://localhost:9000/api/organizers/tags?page=1&perPage=500")
        self.assertEqual(get.call_args_list[1].args[0], "http://localhost:9000/api/organizers/tags?page=2&perPage=500")

    def test_missing_organizer_is_created_then_catalog_is_refreshed(self):
        refreshed = FakeResponse(200, {"items": [{"id": "tag-id", "name": "Soup", "slug": "soup"}]})
        with patch.object(tagger.requests, "post", return_value=FakeResponse(201, {})) as post, patch.object(
            tagger, "_fetch_catalog", return_value={"soup": refreshed.json()["items"][0]}
        ) as fetch:
            catalog, created = tagger._ensure_missing_organizers("tags", ["Soup"], {}, {})

        self.assertEqual(created, ["Soup"])
        self.assertIn("soup", catalog)
        post.assert_called_once()
        fetch.assert_called_once_with("tags", {})


class TaggerProposalTests(unittest.TestCase):
    def test_category_proposal_reads_recipe_category(self):
        original_waterfall = tagger.CATEGORY_WATERFALL
        original_text = tagger.TEXT_ONLY_TAGS
        try:
            tagger.CATEGORY_WATERFALL = [("Soup", ["soup"])]
            tagger.TEXT_ONLY_TAGS = {}
            with patch.object(tagger.requests, "get", return_value=FakeResponse(200, recipe(name="Soup"))):
                proposal = tagger.process_single_recipe({"id": "recipe-id", "slug": "recipe-one"}, {})
        finally:
            tagger.CATEGORY_WATERFALL = original_waterfall
            tagger.TEXT_ONLY_TAGS = original_text

        self.assertFalse(proposal["error"])
        self.assertEqual(proposal["categories_added"], ["Soup"])
        self.assertEqual(proposal["desired_categories"], ["Soup"])

    def test_existing_relationship_name_is_compared_case_insensitively(self):
        original_text = tagger.TEXT_ONLY_TAGS
        try:
            tagger.TEXT_ONLY_TAGS = {"Soup": ["soup"]}
            existing = recipe(name="soup")
            existing["tags"] = [{"name": "soup", "slug": "soup"}]
            with patch.object(tagger.requests, "get", return_value=FakeResponse(200, existing)):
                proposal = tagger.process_single_recipe({"id": "recipe-id", "slug": "recipe-one"}, {})
        finally:
            tagger.TEXT_ONLY_TAGS = original_text

        self.assertFalse(proposal["error"])
        self.assertEqual(proposal["tags_added"], [])


class TaggerWriteTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {
            "tags": {"soup": {"id": "tag-id", "name": "Soup", "slug": "soup"}},
            "categories": {"dinner": {"id": "cat-id", "name": "Dinner", "slug": "dinner"}},
            "tools": {
                "smoker / grill": {
                    "id": "tool-id",
                    "name": "Smoker / Grill",
                    "slug": "smoker-grill",
                }
            },
        }

    def test_bulk_custom_route_chunks_by_batch_size(self):
        proposals = [
            {
                "id": f"recipe-{i}",
                "slug": f"recipe-{i}",
                "tags_added": ["Soup"],
                "categories_added": ["Dinner"],
                "error": False,
            }
            for i in range(3)
        ]
        with patch.object(tagger.requests, "post", return_value=FakeResponse(200, [])) as post, patch.object(
            tagger, "BULK_BATCH_SIZE", 2
        ):
            stats = tagger.apply_tag_category_updates(proposals, self.catalog, {})

        self.assertEqual(stats["tags"], 3)
        self.assertEqual(stats["categories"], 3)
        self.assertEqual(post.call_count, 4)
        first_payload = post.call_args_list[0].kwargs["json"]
        self.assertEqual(first_payload["operation"], "add")
        self.assertEqual(first_payload["recipes"], ["recipe-0", "recipe-1"])
        self.assertEqual(first_payload["tags"][0]["slug"], "soup")
        self.assertEqual(first_payload["categories"], [])

    def test_standard_bulk_fallback_uses_recipe_slugs(self):
        proposal = {
            "id": "recipe-id",
            "slug": "recipe-one",
            "tags_added": ["Soup"],
            "categories_added": [],
            "error": False,
        }
        with patch.object(
            tagger.requests,
            "post",
            side_effect=[FakeResponse(404), FakeResponse(200)],
        ) as post:
            stats = tagger.apply_tag_category_updates([proposal], self.catalog, {})

        self.assertEqual(stats["tags"], 1)
        self.assertEqual(post.call_args_list[1].args[0], "http://localhost:9000/api/recipes/bulk-actions/tag")
        self.assertEqual(post.call_args_list[1].kwargs["json"]["recipes"], ["recipe-one"])

    def test_tool_patch_contains_complete_tool_relationship_only(self):
        proposal = {
            "slug": "recipe-one",
            "desired_tools": ["smoker / grill"],
            "original_tools": [],
            "tools_added": ["Smoker / Grill"],
            "error": False,
        }
        with patch.object(tagger.requests, "patch", return_value=FakeResponse(200, {})) as patch_request:
            stats = tagger.apply_tool_updates([proposal], self.catalog["tools"], {})

        self.assertEqual(stats["tools"], 1)
        payload = patch_request.call_args.kwargs["json"]
        self.assertEqual(set(payload), {"tools"})
        self.assertEqual(payload["tools"], [{"id": "tool-id", "name": "Smoker / Grill", "slug": "smoker-grill"}])

    def test_final_fallback_patch_uses_recipe_category_field(self):
        proposal = {
            "id": "recipe-id",
            "slug": "recipe-one",
            "tags_added": ["Soup"],
            "categories_added": [],
            "desired_tags": ["Soup"],
            "desired_categories": [],
            "original_tags": [],
            "original_categories": [],
            "error": False,
        }
        with patch.object(tagger.requests, "post", side_effect=[FakeResponse(404), FakeResponse(404)]), patch.object(
            tagger.requests, "patch", return_value=FakeResponse(200, {})
        ) as patch_request:
            stats = tagger.apply_tag_category_updates([proposal], self.catalog, {})

        self.assertEqual(stats["tags"], 1)
        self.assertEqual(set(patch_request.call_args.kwargs["json"]), {"tags"})
        self.assertNotIn("categories", patch_request.call_args.kwargs["json"])

    def test_missing_recipe_id_skips_custom_bulk_route(self):
        proposal = {
            "id": None,
            "slug": "recipe-one",
            "tags_added": ["Soup"],
            "categories_added": [],
            "error": False,
        }
        with patch.object(tagger.requests, "post", return_value=FakeResponse(200, [])) as post:
            tagger.apply_tag_category_updates([proposal], self.catalog, {})

        self.assertEqual(post.call_args.args[0], "http://localhost:9000/api/recipes/bulk-actions/tag")

    def test_unresolved_tool_is_reported_without_patch(self):
        proposal = {
            "slug": "recipe-one",
            "desired_tools": ["Unknown Tool"],
            "original_tools": [],
            "tools_added": ["Unknown Tool"],
            "error": False,
        }
        with patch.object(tagger.requests, "patch") as patch_request:
            stats = tagger.apply_tool_updates([proposal], self.catalog["tools"], {})

        self.assertEqual(stats["errors"], 1)
        patch_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
