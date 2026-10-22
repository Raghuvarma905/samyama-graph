"""Offline regressions for the published-SDK compatibility window."""
import unittest
from unittest.mock import patch

import run


class VersionSelectionTests(unittest.TestCase):
    def test_latest_patch_of_each_required_adjacent_minor(self):
        published = [(1, 11, 0), (1, 10, 9), (1, 12, 0), (1, 11, 3), (1, 13, 0), (2, 0, 0)]
        self.assertEqual(run.latest_of_recent_minors(published, (1, 12, 0), 2),
                         [(1, 12, 0), (1, 11, 3)])

    def test_missing_previous_minor_cannot_be_replaced_by_an_older_one(self):
        with self.assertRaisesRegex(ValueError, r"missing: 1\.11"):
            run.latest_of_recent_minors([(1, 12, 0), (1, 10, 0)], (1, 12, 0), 2)

    def test_missing_current_minor_cannot_shift_the_window_back(self):
        with self.assertRaisesRegex(ValueError, r"missing: 1\.12"):
            run.latest_of_recent_minors([(1, 11, 0), (1, 10, 0)], (1, 12, 0), 2)

    def test_deprecated_npm_releases_remain_in_the_window(self):
        metadata = {"versions": {"1.12.0": {}, "1.11.0": {"deprecated": "Upgrade"},
                                 "1.10.0": {}, "1.13.0-beta.1": {}}}
        with patch.object(run, "fetch_json", return_value=metadata):
            self.assertEqual(run.latest_of_recent_minors(run.npm_versions(), (1, 12, 0), 2),
                             [(1, 12, 0), (1, 11, 0)])

    def test_yanked_pypi_patch_uses_an_installable_patch_in_the_same_minor(self):
        metadata = {"releases": {"1.12.0": [{"yanked": False}],
                                 "1.11.1": [{"yanked": True}],
                                 "1.11.0": [{"yanked": False}]}}
        with patch.object(run, "fetch_json", return_value=metadata):
            self.assertEqual(run.latest_of_recent_minors(run.pypi_versions(), (1, 12, 0), 2),
                             [(1, 12, 0), (1, 11, 0)])

    def test_entirely_yanked_required_minor_fails_closed(self):
        metadata = {"releases": {"1.12.0": [{"yanked": False}],
                                 "1.11.0": [{"yanked": True}],
                                 "1.10.0": [{"yanked": False}]}}
        with patch.object(run, "fetch_json", return_value=metadata):
            with self.assertRaisesRegex(ValueError, r"missing: 1\.11"):
                run.latest_of_recent_minors(run.pypi_versions(), (1, 12, 0), 2)

    def test_major_boundary_is_not_guessed_from_available_versions(self):
        with self.assertRaisesRegex(ValueError, "explicit compatibility window"):
            run.latest_of_recent_minors([(2, 0, 0), (1, 15, 0)], (2, 0, 0), 2)

    def test_resolution_reports_which_sdk_is_missing_a_required_minor(self):
        with patch.object(run, "version_of_record", return_value=(1, 12, 0)), \
             patch.object(run, "pypi_versions", return_value=[(1, 12, 0), (1, 11, 0)]), \
             patch.object(run, "npm_versions", return_value=[(1, 12, 0), (1, 10, 0)]):
            with self.assertRaisesRegex(SystemExit, r"typescript:.*missing: 1\.11"):
                run.resolve()


if __name__ == "__main__":
    unittest.main()
