import unittest
from app.hr_defaults import normalize_department, default_office_region
from app.google_sheets_service import FORMULA_COLUMNS

class StandardizationTests(unittest.TestCase):
    def test_departments(self):
        self.assertEqual(normalize_department('运营一部'), '运营1部')
        self.assertEqual(normalize_department('运营二部'), '运营2部')
        self.assertEqual(normalize_department('效能部'), '效能部')

    def test_region(self):
        self.assertEqual(default_office_region('中国'), '中国大陆')

    def test_bb_is_not_copied_as_formula(self):
        self.assertNotIn('BB', FORMULA_COLUMNS)
        self.assertIn('BJ', FORMULA_COLUMNS)
