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
        self.assertEqual(default_office_region('国内'), '中国大陆')
        self.assertEqual(default_office_region('中国境内'), '中国大陆')
        self.assertEqual(default_office_region('大陆'), '中国大陆')
        self.assertEqual(default_office_region('中国香港'), '中国香港')

    def test_formula_columns_are_copied_from_live_roster(self):
        self.assertIn('BB', FORMULA_COLUMNS)
        self.assertIn('BJ', FORMULA_COLUMNS)
