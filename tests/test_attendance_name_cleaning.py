import unittest

from app.vision_service import clean_attendance_name


class AttendanceNameCleaningTests(unittest.TestCase):
    def test_keeps_text_before_ascii_hyphen(self):
        self.assertEqual(clean_attendance_name("比尔-HRGS-TH"), "比尔")

    def test_supports_fullwidth_and_long_dashes(self):
        self.assertEqual(clean_attendance_name("比尔－HRGS－TH"), "比尔")
        self.assertEqual(clean_attendance_name("比尔—HRGS—TH"), "比尔")

    def test_name_without_suffix_is_unchanged(self):
        self.assertEqual(clean_attendance_name("黛西"), "黛西")

    def test_leading_list_marker_is_removed_before_suffix_split(self):
        self.assertEqual(clean_attendance_name("- 比尔-HRGS-TH"), "比尔")


if __name__ == "__main__":
    unittest.main()


class ParseNamesPayloadTests(__import__("unittest").TestCase):
    def test_complete_and_truncated_json(self):
        from app.vision_service import parse_names_payload
        self.assertEqual(parse_names_payload('{"names":["达刀","淼淼"]}'), ["达刀", "淼淼"])
        self.assertEqual(parse_names_payload('```json\n{"names":["达刀","淼淼"]}\n```'), ["达刀", "淼淼"])
        self.assertEqual(parse_names_payload('{"names":["达刀","淼淼","白砚'), ["达刀", "淼淼"])
        self.assertEqual(parse_names_payload("没有识别到"), [])
