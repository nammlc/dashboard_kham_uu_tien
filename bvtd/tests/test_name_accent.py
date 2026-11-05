"""Tên khác DẤU THANH là người khác: TRẦN ≠ TRẢN, TÚC ≠ TỤC (trước đây bỏ dấu nên bị nhập làm một → báo "đã tái khám" nhầm,
báo trùng nhầm, có thể xoá nhầm dòng). Tên gõ không dấu / thiếu dấu thanh vẫn được so khớp như cũ."""
import datetime as dt
import unittest

import pandas as pd

import services.reconcile as R
from services import sheets as sh

D = dt.date
VL = "BỆNH NHÂN VÃNG LAI"


def patient(name, birth="1947", phone="", exam=D(2026, 11, 3), row=2):
    return {"sheet_row": row, "stt": str(row), "name": name, "phone": phone, "age": "", "birth_year": birth, "gender": "Nữ",
            "exam_date": exam, "source": VL, "status_now": "", "actual_date": ""}


def visit(name, day="03/11/2026", birth="1947", phone=""):
    return {"HỌ TÊN": name, "NGÀY ĐK": day, "NĂM SINH": birth, "SỐ ĐIỆN THOẠI": phone, "GIỚI TÍNH": "Nữ"}


def recon(sheet_patients, visits):
    return R.reconcile_attendance(sheet_patients, visits, today=D(2026, 11, 5))


class AccentCompare(unittest.TestCase):
    def test_tone_conflicts(self):
        for a, b in [("NGUYỄN THỊ TRẦN", "NGUYỄN THỊ TRẢN"), ("LÊ VĂN TÚC", "LÊ VĂN TỤC"), ("VÕ THỊ LÀNH", "VÕ THỊ LÃNH"),
                     ("PHẠM VĂN HÙNG", "PHẠM VĂN HỨNG")]:
            res, pairs = sh.accent_compare(a, b)
            self.assertEqual(res, "conflict", (a, b)); self.assertTrue(pairs)

    def test_missing_accents_are_only_weak(self):
        for a, b in [("NGUYỄN THỊ TRẦN", "nguyen thi tran"), ("NGUYỄN THỊ TRẦN", "Nguyễn Thị Trân"), ("TRẦN", "TRÂN")]:
            self.assertEqual(sh.accent_compare(a, b)[0], "weak", (a, b))

    def test_same_and_tone_placement(self):
        self.assertEqual(sh.accent_compare("Lê Thị Hòa", "LÊ THỊ HOÀ")[0], "same")       # hòa / hoà: chỉ khác kiểu đặt dấu
        self.assertEqual(sh.accent_compare("  nguyễn   văn   an ", "NGUYỄN VĂN AN")[0], "same")
        self.assertEqual(sh.acc_name("Lê Thị Hòa"), sh.acc_name("LÊ THỊ HOÀ"))
        self.assertNotEqual(sh.acc_name("TRẦN"), sh.acc_name("TRẢN"))


class ComparePerson(unittest.TestCase):
    def cmp(self, a, b, vphone=None, **kw):
        """a = bệnh nhân trên Sheet, b = lượt khám; vphone = SĐT của lượt khám (mặc định trùng SĐT bên Sheet)."""
        sheet = patient(a, **kw)
        v = visit(b, birth=kw.get("birth", "1947"), phone=sheet["phone"] if vphone is None else vphone)
        return R.compare_person(R.person_view(sheet), R.person_view(v, "visit"))

    def test_user_case_same_birth_year_but_different_tone_is_not_same_person(self):
        self.assertIsNone(self.cmp("NGUYỄN THỊ TRẦN", "NGUYỄN THỊ TRẢN", birth="1947"))
        self.assertIsNone(self.cmp("LÊ VĂN TÚC", "LÊ VĂN TỤC", birth="1960"))

    def test_different_tone_and_different_phone_is_not_same_person(self):
        self.assertIsNone(self.cmp("NGUYỄN THỊ TRẦN", "NGUYỄN THỊ TRẢN", phone="0912345678", vphone="0988111222"))

    def test_different_tone_but_same_phone_needs_manual_review(self):
        c = self.cmp("NGUYỄN THỊ TRẦN", "NGUYỄN THỊ TRẢN", phone="0912345678")
        self.assertEqual(c["tier"], 3)
        self.assertTrue(any("Khác dấu thanh" in n for n in c["notes"]))

    def test_identical_name_is_still_sure_and_ranked_first(self):
        c = self.cmp("NGUYỄN THỊ TRẦN", "NGUYỄN THỊ TRẦN", birth="1947")
        self.assertEqual((c["tier"], c["acc"]), (2, 0))

    def test_unaccented_name_still_matches_with_evidence_but_ranks_lower(self):
        c = self.cmp("NGUYEN THI TRAN", "NGUYỄN THỊ TRẦN", birth="1947")
        self.assertEqual((c["tier"], c["acc"]), (2, 1))
        self.assertTrue(any("thiếu dấu thanh" in n for n in c["notes"]))


class ReconcileAttendance(unittest.TestCase):
    def test_other_patient_with_same_folded_name_is_not_marked_attended(self):
        """Đúng ca của người dùng: NGUYỄN THỊ TRẦN (hẹn) ↔ NGUYỄN THỊ TRẢN (khám) cùng năm sinh 1947."""
        r = recon([patient("NGUYỄN THỊ TRẦN")], [visit("NGUYỄN THỊ TRẢN")])[0]
        self.assertEqual(r["status"], "not_attended"); self.assertIsNone(r["visit"])
        self.assertIsNone(r["near_miss"])                                    # cũng không gợi ý "nghi bị sót" nhầm người

    def test_same_tone_is_attended(self):
        r = recon([patient("NGUYỄN THỊ TRẦN")], [visit("NGUYỄN THỊ TRẦN")])[0]
        self.assertEqual((r["status"], r["match_tier"]), ("attended_sure", 2))

    def test_picks_the_right_person_when_both_visited(self):
        visits = [visit("NGUYỄN THỊ TRẢN", phone="0988000111"), visit("NGUYỄN THỊ TRẦN", phone="0912345678")]
        for name, want in [("NGUYỄN THỊ TRẦN", "NGUYỄN THỊ TRẦN"), ("NGUYỄN THỊ TRẢN", "NGUYỄN THỊ TRẢN")]:
            r = recon([patient(name)], visits)[0]
            self.assertEqual(r["status"], "attended_sure"); self.assertEqual(r["visit"]["HỌ TÊN"], want)

    def test_unaccented_patient_prefers_exact_accent_match_when_ranking_ties(self):
        r = recon([patient("NGUYỄN THỊ TRẦN")], [visit("NGUYEN THI TRAN"), visit("NGUYỄN THỊ TRẦN")])[0]
        self.assertEqual(r["visit"]["HỌ TÊN"], "NGUYỄN THỊ TRẦN")

    def test_same_phone_different_tone_goes_to_manual_review(self):
        r = recon([patient("LÊ VĂN TÚC", phone="0912345678", birth="1960")],
                  [visit("LÊ VĂN TỤC", phone="0912345678", birth="1960")])[0]
        self.assertEqual(r["status"], "attended_unsure")
        self.assertTrue(any("Khác dấu thanh" in n for n in r["match_notes"]))

    def test_unaccented_sheet_name_still_matches_accented_visit(self):
        r = recon([patient("nguyen thi tran")], [visit("NGUYỄN THỊ TRẦN")])[0]
        self.assertEqual(r["status"], "attended_sure")


class Duplicates(unittest.TestCase):
    def test_tk_form_pair_is_not_made_for_different_tones(self):
        khoa = [patient("LÊ VĂN TÚC", birth="1960", row=2)]
        form = [patient("LÊ VĂN TỤC", birth="1960", row=3)]
        self.assertEqual(R.find_duplicate_tk_form(khoa, form), [])
        form = [patient("Lê Văn Túc", birth="1960", row=3)]
        self.assertEqual(len(R.find_duplicate_tk_form(khoa, form)), 1)       # cùng người (khác hoa/thường) vẫn ghép

    def test_import_duplicate_check_keeps_different_tone_patients(self):
        df = pd.DataFrame({"1. HỌ VÀ TÊN BỆNH NHÂN": ["NGUYỄN THỊ TRẦN"], "5. SỐ ĐIÊN THOẠI": [""], "STT": ["7"],
                           "_date": [pd.Timestamp("2026-11-03")]})
        base = {"SỐ ĐIỆN THOẠI": "N/A", "NGÀY HẸN": "03/11/2026"}
        cand = [{**base, "HỌ TÊN": "NGUYỄN THỊ TRẢN"},     # người khác → phải được import
                {**base, "HỌ TÊN": "NGUYỄN THỊ TRẦN"},     # cùng người → trùng
                {**base, "HỌ TÊN": "nguyen thi tran"}]     # gõ không dấu → vẫn coi là trùng như cũ
        new, dup = sh.check_sheet_duplicates(df, cand)
        self.assertEqual([r["HỌ TÊN"] for r in new], ["NGUYỄN THỊ TRẢN"])
        self.assertEqual(sorted(r["HỌ TÊN"] for r in dup), ["NGUYỄN THỊ TRẦN", "nguyen thi tran"])

    def test_import_duplicates_inside_the_file(self):
        base = {"SỐ ĐIỆN THOẠI": "N/A", "NGÀY HẸN": "03/11/2026", "NGÀY LẬP": "20/10/2026"}
        kept, dropped = sh.drop_file_duplicates([{**base, "HỌ TÊN": "LÊ VĂN TÚC"}, {**base, "HỌ TÊN": "LÊ VĂN TỤC"},
                                                  {**base, "HỌ TÊN": "LÊ VĂN TÚC"}])
        self.assertEqual([r["HỌ TÊN"] for r in kept], ["LÊ VĂN TÚC", "LÊ VĂN TỤC"])
        self.assertEqual(len(dropped), 1)


if __name__ == "__main__":
    unittest.main()
