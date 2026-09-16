from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from interactive_mode import (  # noqa: E402
    DEFAULT_WEB_HOTKEYS,
    ReviewSession,
    ReviewSessionOptions,
)


class OfflineSortingTest(unittest.TestCase):
    def test_review_session_clusters_recognition_in_complete_package(self) -> None:
        with tempfile.TemporaryDirectory(prefix="offline_sort_test_") as temp_name:
            root = Path(temp_name)
            accuracy_zip = root / "accuracy_1.zip"
            image_zip = root / "image_result_1.zip"
            accuracy = {
                "data": {
                    "statistic": [
                        {
                            "algoType": "algo",
                            "imagesData": [
                                {"imageName": "a1.jpg", "recognition": "alpha", "auditStatus": 0},
                                {"imageName": "b.jpg", "recognition": "beta", "auditStatus": 0},
                                {"imageName": "a2.jpg", "recognition": "alpha", "auditStatus": 0},
                                {"imageName": "empty.jpg", "recognition": "", "auditStatus": 0},
                            ],
                        }
                    ]
                }
            }
            with zipfile.ZipFile(accuracy_zip, "w") as archive:
                archive.writestr("accuracy.json", json.dumps(accuracy))
            with zipfile.ZipFile(image_zip, "w") as archive:
                archive.writestr("image.json", "{}")
                for name in ("a1.jpg", "b.jpg", "a2.jpg", "empty.jpg"):
                    archive.writestr(f"image/algo/{name}", b"image")

            package = root / "package.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.write(accuracy_zip, accuracy_zip.name)
                archive.write(image_zip, image_zip.name)

            options = ReviewSessionOptions(
                config_path=ROOT / "config-1.yaml",
                database_path=None,
                save_problem_images=True,
                recognition_red_keywords=[],
                point_name_red_keywords=[],
                point_name_end_open=False,
                point_name_end_characters=0,
                point_name_end_character_color="",
                default_zoom=1.0,
                auto_click_times_ms=[1000],
                show_auto_click=False,
                show_mark_all_correct=False,
                show_mark_same_recognition=False,
                exit_after_export=False,
                web_hotkeys=DEFAULT_WEB_HOTKEYS.copy(),
                newkeywords=[],
                default_wrong_keywords=[],
                default_problem_keywords=[],
                extra_xlsx_recognition_enabled=False,
                extra_xlsx_recognition_keywords=[],
                show_algorithm_normal=False,
                checklist_image_width_enabled=False,
                algo_type_labels={},
                solution_rules=[],
                solution_options=[],
                responsible_rules=[],
            )
            session = ReviewSession(
                package,
                root / "output",
                root / "session",
                **options.session_kwargs(),
            )

            names = [
                item["imageName"]
                for item in session.accuracy["data"]["statistic"][0]["imagesData"]
            ]
            self.assertEqual(names, ["a1.jpg", "a2.jpg", "b.jpg", "empty.jpg"])
            self.assertEqual(session.category_index, 0)
            self.assertEqual(session.image_index, 0)


if __name__ == "__main__":
    unittest.main()
