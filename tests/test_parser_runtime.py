import json
import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import kitchen_ops_parser as parser
from kitchen_ops_catalog import PendingCatalogQueue


def queued_record(slug: str) -> dict:
    return {
        "slug": slug,
        "sourceIngredients": ["1 mystery item"],
        "proposedIngredients": [{"food": {"name": "mystery item"}}],
        "missing": [
            {
                "kind": "food",
                "name": "mystery item",
                "proposal": {"name": "mystery item"},
                "ingredientIndex": 0,
                "raw": "1 mystery item",
                "ambiguous": False,
            }
        ],
        "lineReviews": [],
    }


class FakeCursor:
    def __init__(self):
        self.query = ""

    def execute(self, query):
        self.query = query

    def fetchall(self):
        return [("one",)]


class FakeConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()

    def cursor(self):
        return self.cursor_instance


class ParserRuntimeTests(unittest.TestCase):
    def setUp(self):
        parser.SHUTDOWN_REQUESTED = False

    def tearDown(self):
        parser.SHUTDOWN_REQUESTED = False

    def test_history_migrates_atomically_to_configured_state_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy = root / "parse_history.json"
            target = root / "state" / "parse_history.json"
            legacy.write_text(json.dumps(["one", "two"]), encoding="utf-8")

            with patch.object(parser, "HISTORY_FILE", str(target)), patch.object(
                parser, "LEGACY_HISTORY_FILE", str(legacy)
            ):
                history = parser.load_history()

            self.assertEqual(history, {"one", "two"})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), ["one", "two"])
            self.assertTrue(legacy.exists())
            self.assertEqual(list(target.parent.glob(".parse_history.json.*")), [])

    def test_candidate_query_matches_loose_ingredient_shape(self):
        connection = FakeConnection()

        recipes = parser.get_recipes_needing_parsing_db(connection)

        query = " ".join(connection.cursor_instance.query.split()).lower()
        self.assertEqual(recipes, [{"slug": "one"}])
        self.assertIn("ri.food_id is null", query)
        self.assertIn("ri.unit_id is null", query)
        self.assertIn("ri.note", query)
        self.assertIn("ri.original_text", query)

    def test_todo_excludes_history_and_catalog_queue(self):
        candidates = [
            {"slug": "done"},
            {"slug": "waiting"},
            {"slug": "new"},
        ]

        todo = parser.select_todo_recipes(candidates, {"done"}, {"waiting"})

        self.assertEqual(todo, [{"slug": "new"}])

    def test_queue_replay_stops_cooperatively_without_writing(self):
        queue = MagicMock()
        queue.ensure_line_reviews.return_value = False

        stats = parser.replay_ready_recipes(
            queue,
            MagicMock(),
            MagicMock(),
            set(),
            should_stop=lambda: True,
        )

        self.assertEqual(stats["interrupted"], 1)
        queue.save.assert_not_called()

    def test_declining_review_does_not_build_catalog_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            queue = PendingCatalogQueue(Path(temp_dir) / "pending.json")
            queue.upsert_recipe(queued_record("waiting"))
            api = MagicMock()

            with patch.object(parser, "DRY_RUN", False), patch.object(
                parser.sys, "stdin", SimpleNamespace(isatty=lambda: True)
            ), patch.object(parser.Confirm, "ask", return_value=False), patch.object(
                parser, "CatalogReviewer"
            ) as reviewer:
                status = parser.review_pending_catalog(api, queue, ask_first=True)

            self.assertEqual(status, 0)
            reviewer.assert_not_called()

    def _run_main(
        self,
        queue: PendingCatalogQueue,
        candidates: list[dict],
        process_recipe,
        *,
        dry_run: bool = False,
        replay=None,
    ):
        api = MagicMock()
        api.list_tools.return_value = []
        review = MagicMock(return_value=0)
        replay_mock = MagicMock(
            side_effect=replay,
            return_value={"updated": 0, "waiting": len(queue.recipes), "stale": 0, "failed": 0},
        )
        with patch.object(parser, "API_TOKEN", "token"), patch.object(
            parser, "DRY_RUN", dry_run
        ), patch.object(parser, "MAX_WORKERS", 1), patch.object(
            parser, "PendingCatalogQueue", return_value=queue
        ), patch.object(parser, "CatalogApi", return_value=api), patch.object(
            parser, "get_session", return_value=MagicMock()
        ), patch.object(parser, "refresh_catalog"), patch.object(
            parser, "load_history", return_value=set()
        ), patch.object(parser, "connect_db", return_value=None), patch.object(
            parser, "get_all_recipes", return_value=candidates
        ), patch.object(parser, "save_history"), patch.object(
            parser, "review_pending_catalog", review
        ), patch.object(parser, "replay_ready_recipes", replay_mock), patch.object(
            parser, "process_recipe", side_effect=process_recipe
        ) as process_mock, patch.object(parser.sys, "argv", ["kitchen_ops_parser.py"]):
            status = parser.main()
        return status, process_mock, replay_mock, review

    def test_live_run_replays_resolved_then_skips_remaining_queue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            queue = PendingCatalogQueue(Path(temp_dir) / "pending.json")
            for slug in ("resolved", "waiting", "stale"):
                queue.upsert_recipe(queued_record(slug))

            def replay(
                queue_arg,
                index,
                api,
                history,
                logger=None,
                tool_index=None,
                should_stop=None,
            ):
                queue_arg.remove_recipe("resolved")
                queue_arg.remove_recipe("stale")
                history.add("resolved")
                return {"updated": 1, "waiting": 1, "stale": 1, "failed": 0}

            status, process_mock, replay_mock, _ = self._run_main(
                queue,
                [
                    {"slug": "resolved"},
                    {"slug": "waiting"},
                    {"slug": "stale"},
                    {"slug": "new"},
                ],
                lambda slug: parser.ProcessResult("success", slug),
                replay=replay,
            )

            self.assertEqual(status, 0)
            replay_mock.assert_called_once()
            self.assertEqual(
                [call.args[0] for call in process_mock.call_args_list],
                ["stale", "new"],
            )

    def test_dry_run_skips_replay_and_queued_nlp_work(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            queue = PendingCatalogQueue(Path(temp_dir) / "pending.json")
            queue.upsert_recipe(queued_record("waiting"))

            status, process_mock, replay_mock, review = self._run_main(
                queue,
                [{"slug": "waiting"}],
                lambda slug: parser.ProcessResult("dry_run", slug),
                dry_run=True,
            )

            self.assertEqual(status, 0)
            replay_mock.assert_not_called()
            process_mock.assert_not_called()
            review.assert_called_once()

    def test_queue_checkpoints_every_twenty_blocked_results_and_at_exit(self):
        for recipe_count, expected_saves in ((19, 1), (20, 2), (21, 2)):
            with self.subTest(recipe_count=recipe_count), tempfile.TemporaryDirectory() as temp_dir:
                queue = PendingCatalogQueue(Path(temp_dir) / "pending.json")
                queue.save = MagicMock(wraps=queue.save)
                candidates = [{"slug": f"recipe-{index}"} for index in range(recipe_count)]

                def block(slug):
                    return parser.ProcessResult("blocked", slug, queued_record(slug))

                status, _, _, _ = self._run_main(queue, candidates, block)

                self.assertEqual(status, 0)
                self.assertEqual(queue.save.call_count, expected_saves)

    def test_interrupt_checkpoints_skips_review_and_returns_130(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            queue = PendingCatalogQueue(Path(temp_dir) / "pending.json")
            queue.save = MagicMock(wraps=queue.save)

            def interrupt(slug):
                parser.signal_handler(signal.SIGINT, None)
                return parser.ProcessResult("cancelled", slug)

            status, process_mock, _, review = self._run_main(
                queue,
                [{"slug": "one"}, {"slug": "two"}, {"slug": "three"}],
                interrupt,
            )

            self.assertEqual(status, 130)
            self.assertEqual(process_mock.call_count, 1)
            self.assertEqual(queue.save.call_count, 1)
            review.assert_not_called()


if __name__ == "__main__":
    unittest.main()
