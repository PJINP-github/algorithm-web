# -*- coding: utf-8 -*-
import unittest

from weekly_report import (
    calculate_position_accuracy,
    get_silent_substrings,
    get_zero_accuracy_algorithms,
    is_silent_algo,
)


class ZeroAccuracyAlgorithmsTest(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {
                "算法小类": "有效原始零准确率",
                "点位数量": 3,
                "准确率": "0%",
                "审核后准确率": "80%",
            },
            {
                "算法小类": "有效审核后零准确率",
                "点位数量": 4,
                "准确率": "80%",
                "审核后准确率": "0%",
            },
            {
                "算法小类": "点位为零",
                "点位数量": 0,
                "准确率": "0%",
                "审核后准确率": "0%",
            },
            {
                "算法小类": "点位为空",
                "点位数量": "",
                "准确率": "0%",
                "审核后准确率": "0%",
            },
            {
                "算法小类": "准确率为空",
                "点位数量": 5,
                "准确率": "",
                "审核后准确率": "",
            },
        ]

    def test_raw_source_requires_effective_points_and_raw_zero(self):
        self.assertEqual(
            get_zero_accuracy_algorithms(self.rows, "准确率"),
            ["有效原始零准确率"],
        )

    def test_audited_source_uses_audited_value(self):
        self.assertEqual(
            get_zero_accuracy_algorithms(self.rows, "审核后准确率"),
            ["有效审核后零准确率"],
        )


class AccuracySummaryTest(unittest.TestCase):
    def test_position_accuracy_uses_selected_correct_count(self):
        rows = [
            {"点位数量": 12, "准确数量": 11, "审核后准确数量": 10},
            {"点位数量": 331, "准确数量": 319, "审核后准确数量": 320},
            {"点位数量": 13, "准确数量": 9, "审核后准确数量": 12},
            {"点位数量": "-", "准确数量": "-", "审核后准确数量": 1},
        ]
        self.assertAlmostEqual(
            calculate_position_accuracy(rows, "准确数量"),
            339 / 356 * 100,
        )
        self.assertAlmostEqual(
            calculate_position_accuracy(rows, "审核后准确数量"),
            342 / 356 * 100,
        )

    def test_dash_or_no_data_correct_count_is_zero_for_effective_points(self):
        rows = [
            {"点位数量": 10, "准确数量": "-", "审核后准确数量": 8},
            {"点位数量": 5, "准确数量": 2, "审核后准确数量": "无数据"},
            {"点位数量": "-", "准确数量": 100, "审核后准确数量": 100},
        ]
        self.assertAlmostEqual(
            calculate_position_accuracy(rows, "准确数量"),
            2 / 15 * 100,
        )
        self.assertAlmostEqual(
            calculate_position_accuracy(rows, "审核后准确数量"),
            8 / 15 * 100,
        )

    def test_silent_matching_uses_configured_substrings(self):
        cfg = {
            "Specify_silent_mode_group": [
                {"substring": "吸烟"},
                {"substring": "人员闯入"},
            ]
        }
        self.assertEqual(get_silent_substrings(cfg), ["吸烟", "人员闯入"])
        self.assertTrue(is_silent_algo("烟雾吸烟识别", get_silent_substrings(cfg)))
        self.assertFalse(is_silent_algo("静默算法", get_silent_substrings(cfg)))


if __name__ == "__main__":
    unittest.main()
