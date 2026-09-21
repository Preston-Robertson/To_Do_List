"""Assistant task classification contracts using synthetic records only."""
from unittest import TestCase
from unittest.mock import patch

from luigi_web.modules.assistant import tools


class AssistantTaskDefaultsTests(TestCase):
    def setUp(self):
        self.defaults = self.enterContext(patch.object(tools.db, "suggest_task_defaults", return_value={"matches": 0}))
        self.enterContext(patch.object(tools.db, "find_existing_categorical", return_value=None))
        self.create = self.enterContext(patch.object(tools.db, "create_task", return_value="synthetic-task"))

    def test_unknown_priority_is_normal_and_category_is_inferred_by_model(self):
        result = tools.handle_create_task({"task": "Review example notes", "catagory": "work"})
        self.assertEqual(self.create.call_args.args[0]["priority"], 3)
        self.assertEqual(result["priority"], 3)
        self.assertEqual(result["catagory"], "Work")
        self.assertIsNone(self.create.call_args.args[0]["due_date"])

    def test_recent_history_supplies_missing_fields(self):
        self.defaults.return_value = {"matches": 3, "priority": 4, "catagory": "Home"}
        result = tools.handle_create_task({"task": "Fix example shelf"})
        self.assertEqual((result["priority"], result["catagory"]), (4, "Home"))

    def test_historical_zero_is_not_an_inferred_priority(self):
        self.defaults.return_value = {"matches": 3, "priority": 0, "catagory": "Home"}
        result = tools.handle_create_task({"task": "Check example shelf"})
        self.assertEqual(result["priority"], 3)

    def test_explicit_values_including_zero_are_preserved(self):
        for priority in range(6):
            with self.subTest(priority=priority):
                result = tools.handle_create_task({"task": "Example task", "priority": priority, "catagory": "Errands"})
                self.assertEqual((result["priority"], result["catagory"]), (priority, "Errands"))
        self.defaults.assert_not_called()

    def test_invalid_or_missing_category_requires_model_retry_before_write(self):
        for category in (None, "", "0", "Unknown", "uncategorized"):
            with self.subTest(category=category), self.assertRaisesRegex(ValueError, "meaningful catagory"):
                tools.handle_create_task({"task": "Example task", "catagory": category})
        self.create.assert_not_called()

    def test_invalid_priority_never_writes(self):
        for priority in (-1, 6, "high"):
            with self.subTest(priority=priority), self.assertRaises(ValueError):
                tools.handle_create_task({"task": "Example task", "catagory": "Work", "priority": priority})
        self.create.assert_not_called()

    def test_tool_contract_requires_deliberate_classification(self):
        contract = tools.build_registry()["create_task"].parameters
        self.assertEqual(contract["required"], ["task", "priority", "catagory"])
        self.assertIn("5 urgent/critical", contract["properties"]["priority"]["description"])
        self.assertIn("explicit user request", tools.SYSTEM_PROMPT)
