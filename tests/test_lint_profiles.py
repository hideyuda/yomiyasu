#!/usr/bin/env python3
"""yomiyasu_lint.py の用途プロファイル（--profile）の検査

実行: python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))

from yomiyasu_lint import lint_text, load_profiles, x_weighted_length  # noqa: E402

PROFILES = load_profiles()


def rules(result, rule=None, severity=None):
    return [f for f in result["findings"]
            if (rule is None or f["rule"] == rule) and (severity is None or f["severity"] == severity)]


class DefaultProfileTest(unittest.TestCase):
    def test_default_scores_match_benchmark_results(self):
        """プロファイルなしの検査結果が、同梱のベンチマーク結果（従来のリンター）と同じ点数になる"""
        path = os.path.join(BASE_DIR, "tests", "corpus", "benchmark_results.json")
        with open(path, encoding="utf-8") as f:
            details = json.load(f)["details"]
        checked = 0
        for group, files in details.items():
            for item in files:
                with open(os.path.join(BASE_DIR, "tests", "corpus", group, item["file"]), encoding="utf-8") as f:
                    result = lint_text(f.read())
                self.assertEqual(result["score"], item["score"], f"{group}/{item['file']}")
                checked += 1
        self.assertGreater(checked, 100)

    def test_default_has_no_profile_checks(self):
        text = "インフラチームは作業します。インフラチームは連絡します。確認を行います。"
        result = lint_text(text)
        self.assertEqual(result["profile"], "default")
        self.assertNotIn("register_metrics", result)
        self.assertFalse(rules(result, "subject_repetition"))
        self.assertFalse(rules(result, "stiff_expression"))

    def test_default_prohibits_emoji(self):
        self.assertTrue(rules(lint_text("確認お願いします🙏"), "emoji_prohibited", "warn"))


class EmojiAndColonTest(unittest.TestCase):
    def test_chat_allows_one_listed_emoji(self):
        self.assertFalse(rules(lint_text("確認お願いします🙏", "chat", PROFILES), "emoji_prohibited"))

    def test_chat_flags_second_emoji(self):
        result = lint_text("確認お願いします🙏\nよろしくお願いします🙇", "chat", PROFILES)
        found = rules(result, "emoji_prohibited", "warn")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["line"], 2)

    def test_chat_flags_unlisted_emoji_as_info(self):
        self.assertTrue(rules(lint_text("リリースしました🚀", "chat", PROFILES), "emoji_not_allowed", "info"))

    def test_chat_external_prohibits_emoji(self):
        self.assertTrue(rules(lint_text("ご確認ください🙏", "chat_external", PROFILES), "emoji_prohibited", "warn"))

    def test_chat_trailing_colon_is_info(self):
        result = lint_text("お願いが3点あります：\n- 保存してください", "chat", PROFILES)
        self.assertEqual([f["severity"] for f in rules(result, "trailing_colon")], ["info"])


class SnsTest(unittest.TestCase):
    def test_x_weighted_length(self):
        self.assertEqual(x_weighted_length("あいう"), 6)
        self.assertEqual(x_weighted_length("abc"), 3)
        self.assertEqual(x_weighted_length("https://example.com/very/long/path"), 23)

    def test_x_length_over_limit(self):
        self.assertTrue(rules(lint_text("あ" * 141, "sns", PROFILES), "x_length"))
        self.assertFalse(rules(lint_text("あ" * 140, "sns", PROFILES), "x_length"))

    def test_x_length_per_segment(self):
        text = "あ" * 100 + "\n---\n" + "い" * 100
        self.assertFalse(rules(lint_text(text, "sns", PROFILES), "x_length"))

    def test_hashtags(self):
        self.assertTrue(rules(lint_text("新機能を出しました #a #b #c", "sns", PROFILES), "too_many_hashtags"))
        self.assertFalse(rules(lint_text("新機能を出しました #a #b", "sns", PROFILES), "too_many_hashtags"))

    def test_markdown_bold_is_not_rendered(self):
        self.assertTrue(rules(lint_text("これは**大事**です。", "sns", PROFILES), "markdown_unrendered", "warn"))


class StiffnessTest(unittest.TestCase):
    def test_level1_in_proposal(self):
        result = lint_text("- 集計作業の確認を行う\n- 自動化することが可能", "proposal", PROFILES)
        self.assertEqual(len(rules(result, "stiff_expression", "warn")), 2)

    def test_level2_not_in_proposal(self):
        self.assertFalse(rules(lint_text("- 本番環境においてテストする", "proposal", PROFILES), "stiff_expression"))

    def test_level2_in_article(self):
        result = lint_text("本番環境においてテストします。", "article", PROFILES)
        self.assertEqual([f["severity"] for f in rules(result, "stiff_expression")], ["info"])

    def test_level3_only_in_chat(self):
        text = "したがって、明日対応します。"
        self.assertTrue(rules(lint_text(text, "chat", PROFILES), "stiff_expression"))
        self.assertFalse(rules(lint_text(text, "article", PROFILES), "stiff_expression"))

    def test_repeated_polite_form(self):
        text = "確認させていただきます。共有させていただきます。"
        self.assertTrue(rules(lint_text(text, "chat", PROFILES), "stiff_repetition"))
        self.assertFalse(rules(lint_text("確認させていただきます。", "chat", PROFILES), "stiff_repetition"))

    def test_quoted_examples_are_skipped(self):
        self.assertFalse(rules(lint_text("> 確認を行います。", "article", PROFILES), "stiff_expression"))


class HumanizeSlopTest(unittest.TestCase):
    def test_exclaim_cap(self):
        self.assertFalse(rules(lint_text("リリースしました！", "article", PROFILES), "humanize_slop"))
        self.assertTrue(rules(lint_text("リリースしました！使ってください！", "article", PROFILES), "humanize_slop", "warn"))

    def test_proposal_allows_no_exclaim(self):
        self.assertTrue(rules(lint_text("- 売上が伸びる！", "proposal", PROFILES), "humanize_slop"))

    def test_fabricated_experience_is_info(self):
        result = lint_text("先日、私も同じ設定で試しました。", "article", PROFILES)
        self.assertEqual([f["severity"] for f in rules(result, "humanize_slop")], ["info"])

    def test_casual_phrases(self):
        result = lint_text("正直、ぶっちゃけ便利です。", "sns", PROFILES)
        self.assertEqual(len(rules(result, "humanize_slop")), 2)

    def test_laugh_does_not_match_words(self):
        self.assertFalse(rules(lint_text("笑顔で迎えた。", "sns", PROFILES), "humanize_slop"))


class StructureTest(unittest.TestCase):
    def test_subject_repetition(self):
        text = "インフラチームは週末に作業します。インフラチームは開始時に連絡します。"
        self.assertTrue(rules(lint_text(text, "chat", PROFILES), "subject_repetition", "info"))

    def test_subject_change_is_fine(self):
        text = "インフラチームは週末に作業します。開発チームは待機します。"
        self.assertFalse(rules(lint_text(text, "chat", PROFILES), "subject_repetition"))

    def test_short_burst(self):
        text = "速い。安い。うまい。そう言われて久しいこの店に、ようやく行ってきました。"
        self.assertTrue(rules(lint_text(text, "article", PROFILES), "short_burst"))

    def test_bullet_polite_in_proposal(self):
        self.assertTrue(rules(lint_text("- 集計を自動化します\n- 工数を減らします", "proposal", PROFILES), "bullet_polite"))

    def test_bullet_mixed_endings(self):
        result = lint_text("- 集計の自動化\n- 工数を減らす", "proposal", PROFILES)
        self.assertTrue(rules(result, "bullet_mixed_endings"))
        self.assertFalse(rules(lint_text("- 集計の自動化\n- 工数の削減", "proposal", PROFILES), "bullet_mixed_endings"))

    def test_list_item_too_long(self):
        self.assertTrue(rules(lint_text("- " + "あ" * 51, "proposal", PROFILES), "list_item_too_long"))

    def test_placeholders(self):
        two = "導入しました。[ここに一言]\n効果が出ました。[ここに数字]"
        result = lint_text(two, "article", PROFILES)
        self.assertEqual(len(result["placeholders"]), 2)
        self.assertFalse(rules(result, "too_many_placeholders"))
        self.assertTrue(rules(lint_text(two + "[ここに感想]", "article", PROFILES), "too_many_placeholders"))
        self.assertTrue(rules(lint_text("[ここに一言]", "proposal", PROFILES), "too_many_placeholders"))


class DotBulletTest(unittest.TestCase):
    TEXT = "お願いが3点あります。\n・バッチを移してください\n・共有してください\n・保存してください"

    def test_dot_bullets_are_list_items_in_profiles(self):
        result = lint_text(self.TEXT, "chat", PROFILES)
        self.assertFalse(rules(result, "sentence_end_repetition"))
        self.assertEqual(result["register_metrics"]["list_item_count"], 3)
        self.assertEqual(result["metrics"]["list_lines"], 3)

    def test_default_keeps_old_behavior(self):
        result = lint_text(self.TEXT)
        self.assertEqual(result["metrics"]["list_lines"], 0)


class ReportMetricsTest(unittest.TestCase):
    def test_x_lengths_per_segment(self):
        result = lint_text("あいう\n---\nabc", "sns", PROFILES)
        self.assertEqual(result["register_metrics"]["x_weighted_lengths"], [6, 3])

    def test_title_too_long(self):
        self.assertTrue(rules(lint_text("# " + "あ" * 41 + "\n- 項目", "proposal", PROFILES), "title_too_long"))
        self.assertFalse(rules(lint_text("# " + "あ" * 40 + "\n- 項目", "proposal", PROFILES), "title_too_long"))


class SkillCopyTest(unittest.TestCase):
    def test_plugin_copy_matches_root(self):
        """skills/yomiyasu/ はプラグイン配布用のコピーで、ルートと同じ内容にしておく"""
        import filecmp
        copy_dir = os.path.join(BASE_DIR, "skills", "yomiyasu")
        pairs = [("SKILL.md", "SKILL.md")] + [(os.path.join("scripts", n), os.path.join("scripts", n))
                                              for n in ("yomiyasu_lint.py", "yomiyasu_diff.py", "profiles.json")]
        for root_rel, copy_rel in pairs:
            self.assertTrue(filecmp.cmp(os.path.join(BASE_DIR, root_rel), os.path.join(copy_dir, copy_rel), shallow=False), root_rel)
        cmp = filecmp.dircmp(os.path.join(BASE_DIR, "references"), os.path.join(copy_dir, "references"))

        def walk(c):
            self.assertFalse(c.left_only or c.right_only or c.diff_files, f"{c.left}: {c.left_only} {c.right_only} {c.diff_files}")
            for sub in c.subdirs.values():
                walk(sub)
        walk(cmp)


class ProfilesFileTest(unittest.TestCase):
    def test_custom_profile_extends(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump({"my_chat": {"extends": "chat", "humanize_slop": {"exclaim": 3}}}, f)
            path = f.name
        try:
            profiles = load_profiles(path)
            self.assertEqual(profiles["my_chat"]["stiffness_level"], 3)
            self.assertFalse(rules(lint_text("了解です！ありがとうございます！", "my_chat", profiles), "humanize_slop"))
        finally:
            os.unlink(path)

    def test_unknown_profile(self):
        with self.assertRaises(ValueError):
            lint_text("テスト", "unknown", PROFILES)


if __name__ == "__main__":
    unittest.main()
