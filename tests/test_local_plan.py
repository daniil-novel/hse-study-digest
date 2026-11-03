import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_plan as plan

RULES = [
    {"type": "references", "phrases": ["оформ* список источников", "включить библиографию"]},
    {"type": "diagram", "phrases": ["прилож* схему решения", "нарисовать блок-схему"]},
    {"type": "demo", "phrases": ["запиш* демонстрацию", "снять демонстрационное видео"]},
    {"type": "defence", "phrases": ["подайте заявку на защиту", "зарегистрироваться на защиту"]},
]


class LocalPlanTests(unittest.TestCase):
    def test_only_public_seed_crosses_model_boundary_and_fixed_labels_reach_table(self):
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(plan, "model_json", return_value={"rules": RULES}) as llm,
        ):
            p = Path(d)
            (p / "dialogs.csv").write_text("private_chat_canary")
            (p / "study.toml").write_text("private_course_canary")
            plan.make_plan("codex", p)
            outgoing = json.dumps(llm.call_args.args[2:], ensure_ascii=False)
            self.assertNotIn("canary", outgoing)
            self.assertIn(plan.LESSONS["references"][1][0], outgoing)
            rules = plan.load_rules(p)
            self.assertEqual(len(rules), 8)
            result = {
                label
                for pattern, label in rules
                if pattern.search("К сроку оформите список источников для автора.")
            }
            self.assertEqual(result, {"Подготовить список источников"})
            self.assertNotIn("автора", str(result))

    def test_invalid_model_response_leaves_previous_plan_intact(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            with patch.object(plan, "model_json", return_value={"rules": RULES}):
                plan.make_plan("codex", p)
            before = (p / "plan-local.toml").read_bytes()
            bad = copy.deepcopy(RULES)
            bad[0]["phrases"] = ["(?s).*", "включить библиографию"]
            with (
                patch.object(plan, "model_json", return_value={"rules": bad}),
                self.assertRaises(ValueError),
            ):
                plan.make_plan("codex", p)
            self.assertEqual((p / "plan-local.toml").read_bytes(), before)
            (p / "plan-local.toml").write_text("version=9")
            with self.assertRaises(ValueError):
                plan.load_rules(p)

    def test_grammar_and_contrast_cases(self):
        for phrase in (
            "за* чёт",
            "приложить .+",
            "все",
            "подать  заявку",
            "подать (?i)",
            "RUN anything",
        ):
            with self.subTest(phrase=phrase), self.assertRaises(ValueError):
                plan.compile_phrase(phrase)
        bad = copy.deepcopy(RULES)
        bad[-1]["phrases"][0] = "подайте заявку"
        with self.assertRaises(ValueError):
            plan.validated(bad)
        good = plan.validated(RULES)
        self.assertFalse(any(rx.search("Не нужно оформить список источников.") for rx, _ in good))
        self.assertFalse(any(rx.search("Нужно оформить список источников?") for rx, _ in good))
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(plan.load_rules(Path(d)), [])


if __name__ == "__main__":
    unittest.main()
