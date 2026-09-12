import unittest

from app.employee_profile import (
    has_onboarding_keyword,
    parse_extra_profile_fields,
    parse_profile_message,
    person_name_keys,
    validate_profile,
)
from app.bot import _candidate_profile_id
from app.google_sheets_service import onboarding_identity_keys


MESSAGE = """效能中心【入职信息确认】
候选人编码：WTLKX00077
候选人姓名：姜先生
入职编制组织：效能中心
入职服务单位：恒睿
人员性质：派驻
入职部门：效能部
岗位类型：技术
职位：测试工程师
建议职级：P4-1
管理序列：个人
办公方式：远程
招聘渠道：万天招聘部
简历来源：林可心
招聘通道：tg
入职日期：2026年9月8日"""


class OnboardingRegressionTests(unittest.TestCase):
    def test_chinese_flower_name_is_an_onboarding_trigger(self) -> None:
        mode, values = parse_profile_message("中文花名：夏华\n姓名/简历名：姜先生")
        self.assertTrue(has_onboarding_keyword("中文花名：夏华"))
        self.assertEqual(mode, "new")
        self.assertEqual(values["chinese_name"], "夏华")

    def test_onboarding_identity_works_before_employee_code_exists(self) -> None:
        candidate = {"employee_code": "", "resume_name": "姜先生", "chinese_name": "夏华"}
        existing_change = {"employee_code": "姜先生", "chinese_name": "夏华"}
        self.assertTrue(
            onboarding_identity_keys(candidate) & onboarding_identity_keys(existing_change)
        )

    def test_candidate_code_is_not_employee_code(self) -> None:
        mode, values = parse_profile_message(MESSAGE)
        self.assertEqual(mode, "new")
        self.assertNotIn("employee_code", values)

    def test_chinese_date_is_normalized(self) -> None:
        _, values = parse_profile_message(MESSAGE)
        self.assertEqual(values["effective_date"], "2026-09-08")

    def test_short_date_uses_current_year(self) -> None:
        _, values = parse_profile_message("入职信息确认\n入职日期：9/8")
        self.assertEqual(values["effective_date"], "2026-09-08")

    def test_candidates_get_stable_separate_ids(self) -> None:
        daniel = _candidate_profile_id("Daniel丁丹阳")
        self.assertLess(daniel, 0)
        self.assertEqual(daniel, _candidate_profile_id("Daniel丁丹阳"))
        self.assertNotEqual(daniel, _candidate_profile_id("姜先生"))

    def test_bilingual_and_chinese_resume_names_match(self) -> None:
        self.assertTrue(
            person_name_keys("Daniel丁丹阳") & person_name_keys("丁丹阳")
        )

    def test_flower_name_is_not_required_for_identity(self) -> None:
        self.assertFalse(person_name_keys("迪 丹尼") & person_name_keys("丁丹阳"))

    def test_explicit_roster_fields_are_mapped(self) -> None:
        fields = parse_extra_profile_fields(MESSAGE)
        self.assertEqual(fields["org_unit"], "效能中心")
        self.assertEqual(fields["position_title"], "测试工程师")
        self.assertEqual(fields["job_level"], "P4")
        self.assertEqual(fields["job_grade"], "1")
        self.assertEqual(fields["mgmt_sequence"], "个人贡献者")
        self.assertEqual(fields["recruitment_channel"], "万天渠道")

    def test_combined_department_and_team_are_split(self) -> None:
        fields = parse_extra_profile_fields(
            "入职信息确认\n入职部门：ACFan特战队-app运营组"
        )
        self.assertEqual(fields["department"], "ACFAN特战队")
        self.assertEqual(fields["team"], "app运营组")

    def test_employee_code_must_be_six_characters(self) -> None:
        self.assertTrue(validate_profile({"employee_code": "WTLKX00077"}, False))
        self.assertFalse(validate_profile({"employee_code": "NX5223"}, False))


if __name__ == "__main__":
    unittest.main()
