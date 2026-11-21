"""决策点可用性、同场评分与值班采用的规则检查。"""
from pathlib import Path
import unittest

from source_book import load
import forecast_desk as fd

BOOK = load(Path(__file__).resolve().parents[1] / "data" / "samples.json")
NOW = fd.known_seq_ceiling(BOOK)


def make_book(sessions=(), predictions=(), gate=(), crews=(), commitments=(), revisions=()):
    return {"sessions": list(sessions), "predictions": list(predictions),
            "gate": list(gate), "crews": list(crews),
            "commitments": list(commitments), "revisions": list(revisions)}


def session(slot, **over):
    row = {"slot": slot, "date": "11-20", "shift": "上午", "school_kind": "低龄",
           "seats": 80, "peak_threshold": 64, "early_cut": 38, "intraday_cut": 3}
    row.update(over)
    return row


def pred(slot, source, ahead, p10, p50, p90, threshold=64, prob=0.3, seq=1):
    return {"slot": slot, "source": source, "ahead_hours": ahead, "p10": p10,
            "p50": p50, "p90": p90, "threshold": threshold,
            "probability_above": prob, "seq": seq}


class AvailabilityTest(unittest.TestCase):
    def test_late_version_not_usable_at_either_cut(self):
        # s1 售团 v2 距开场 2h：早班(38h)与增援(3h)都不可用，值班表不能建立在它上面
        usable, dropped = fd.usable_predictions(BOOK, "s1", "增援", NOW)
        self.assertEqual(usable["售团"]["seq"], 1)
        self.assertTrue(any(p["seq"] == 2 and "晚于" in why for p, why in dropped))

    def test_newer_version_wins_when_usable(self):
        # s4 票务 v2 提前 5h：增援点可用且取代 v1
        usable, _ = fd.usable_predictions(BOOK, "s4", "增援", NOW)
        self.assertEqual(usable["票务"]["seq"], 2)
        # 早班点 v2 太晚，只能用 v1
        usable, _ = fd.usable_predictions(BOOK, "s4", "早班", NOW)
        self.assertEqual(usable["票务"]["seq"], 1)

    def test_backfilled_forecast_excluded(self):
        book = make_book(
            sessions=[session("x")],
            predictions=[pred("x", "票务", 40, 40, 50, 60, seq=1),
                         pred("x", "票务", 30, 40, 50, 60, seq=5)],
            gate=[{"slot": "x", "entered": 50, "waiting": 0, "closed": "自然结束", "known_seq": 3}])
        usable, dropped = fd.usable_predictions(book, "x", "早班", 9)
        self.assertEqual(usable["票务"]["seq"], 1)
        self.assertTrue(any("补写" in why for _, why in dropped))

    def test_forecast_after_at_seq_invisible(self):
        _, dropped = fd.usable_predictions(BOOK, "s1", "早班", 1)
        self.assertTrue(any("信息时点之后" in why for _, why in dropped))


class ThresholdTest(unittest.TestCase):
    def test_revision_applies_only_after_known(self):
        self.assertEqual(fd.threshold_at(BOOK, "s8", 2), (61, "原始"))
        self.assertEqual(fd.threshold_at(BOOK, "s8", NOW), (68, "修订"))

    def test_direct_probability_lost_after_revision(self):
        usable, _ = fd.usable_predictions(BOOK, "s8", "早班", NOW)
        prob, how = fd.exceedance_probability(usable["票务"], 64)
        self.assertEqual((prob, how), (0.35, fd.DIRECT))
        prob, how = fd.exceedance_probability(usable["票务"], 68)
        self.assertEqual(how, fd.DERIVED)
        self.assertAlmostEqual(prob, 0.2429, places=3)

    def test_exceedance_interpolation_and_edges(self):
        row = pred("x", "票务", 40, 40, 52, 62, threshold=52, prob=0.5)
        self.assertEqual(fd.exceedance_probability(row, 52), (0.5, fd.DIRECT))
        prob, how = fd.exceedance_probability(row, 55)
        self.assertEqual(how, fd.DERIVED)
        self.assertAlmostEqual(prob, 0.38, places=2)
        self.assertEqual(fd.exceedance_probability(row, 70), (0.1, fd.DERIVED_EDGE))
        self.assertEqual(fd.exceedance_probability(row, 10), (0.9, fd.DERIVED_EDGE))


class ActualSupportTest(unittest.TestCase):
    def test_waiting_never_counted_as_entered(self):
        actual = fd.actual_support(BOOK, "s1", NOW)
        self.assertEqual(actual["entered"], 61)
        self.assertEqual(actual["waiting"], 4)

    def test_support_by_session_kind(self):
        self.assertIn("点误差", fd.actual_support(BOOK, "s1", NOW)["support"])
        self.assertNotIn("点误差", fd.actual_support(BOOK, "s2", NOW)["support"])  # 满员截断
        self.assertEqual(fd.actual_support(BOOK, "s5", NOW)["support"], ())      # 取消
        self.assertEqual(fd.actual_support(BOOK, "s7", NOW)["support"], ())      # 迟报
        self.assertEqual(fd.actual_support(BOOK, "s8", NOW)["status"], "未开场")  # 新开

    def test_censored_verdicts(self):
        usable, _ = fd.usable_predictions(BOOK, "s2", "早班", NOW)
        self.assertEqual(fd.censored_coverage(usable["票务"], 42), "确定漏报(进场已高出区间上限)")
        self.assertEqual(fd.censored_coverage(usable["售团"], 42), "不可判定(需求被截断)")
        self.assertIs(fd.censored_above(42, 35), True)
        self.assertIsNone(fd.censored_above(30, 35))


class ScoringTest(unittest.TestCase):
    def test_scores_use_entered_only(self):
        scored = fd.score_source(BOOK, "票务", "早班", NOW)["scored"]
        self.assertEqual(scored["s1"]["abs_err"], 1)   # |60-61|，等候区 4 不计入
        self.assertEqual(scored["s4"]["abs_err"], 0)   # 取最新确认 62（known_seq 4）

    def test_censored_and_excluded_separated(self):
        result = fd.score_source(BOOK, "售团", "早班", NOW)
        self.assertIn("s2", result["censored"])
        self.assertNotIn("s2", result["scored"])       # 截断场不给点误差满分
        self.assertIn("s7", result["excluded"])        # 迟报
        self.assertIn("s8", result["excluded"])        # 未开场
        weather = fd.score_source(BOOK, "天气席", "早班", NOW)
        self.assertIn("s5", weather["excluded"])       # 取消

    def test_common_support_comparison(self):
        cmp = fd.compare_sources(BOOK, "早班", NOW)
        pair = next(p for p in cmp["pairs"] if p["pair"] == ("售团", "票务"))
        self.assertEqual(pair["common"], {"s1", "s4", "s6"})
        self.assertEqual(pair["better"], "票务")
        self.assertAlmostEqual(pair["mae"]["票务"], 3.0)
        self.assertAlmostEqual(pair["mae"]["售团"], 11 / 3)

    def test_no_common_support_keeps_scopes_separate(self):
        cmp = fd.compare_sources(BOOK, "早班", NOW)
        pair = next(p for p in cmp["pairs"] if "天气席" in p["pair"])
        self.assertEqual(pair["common"], set())
        self.assertIn("无共同支撑", pair["note"])

    def test_segments_reported(self):
        sm = fd.summarize(fd.score_source(BOOK, "票务", "早班", NOW))
        self.assertIn("中需求段", sm["segments"])
        self.assertEqual(sm["segments"]["中需求段"]["n"], 3)


class RoundComparisonTest(unittest.TestCase):
    def test_real_change_vs_changed_session_set(self):
        early = fd.score_source(BOOK, "票务", "早班", NOW)
        intraday = fd.score_source(BOOK, "票务", "增援", NOW)
        rounds = fd.compare_rounds(early, intraday, "早班", "增援")
        self.assertEqual(rounds["common"], {"s1", "s4", "s6"})
        self.assertEqual(rounds["only_b"], {"s3"})
        self.assertIn("换了一批场次", rounds["note"])
        # s4 增援轮用了 v2，同场版本对照可见变差
        self.assertEqual(rounds["version_changes"]["s4"][:2], (1, 2))
        self.assertEqual(rounds["verdict"], "变差")


class AdoptionTest(unittest.TestCase):
    def test_weights_from_common_support(self):
        weights = fd.adoption_weights(BOOK, "早班", NOW)
        self.assertGreater(weights["票务"]["weight"], weights["售团"]["weight"])
        self.assertIsNone(weights["天气席"]["weight"])
        self.assertIn("无共同支撑", weights["天气席"]["note"])

    def test_scope_limits_recorded(self):
        weights = fd.adoption_weights(BOOK, "早班", NOW)
        self.assertEqual(weights["售团"]["scope"], {("低龄", "上午")})


class RosterTest(unittest.TestCase):
    def test_commitments_locked_and_margins_real(self):
        plans = fd.roster_plan(BOOK, "增援", NOW, weights={"票务": 1, "售团": 1})
        plan = plans[("11-25", "上午")]
        margins = {m["pool"]: m for m in plan["margins"]}
        self.assertEqual(margins["C1"]["real_free"], 0)
        self.assertEqual(margins["C1"]["note"], "最低保留已被已通知占用")
        self.assertEqual(margins["C2"]["real_free"], 0)  # 2 可用 - 1 已通知 - 1 保留
        self.assertIn("s9", {u["slot"] for u in plan["unmet"]})

    def test_scope_blocks_weight_outside_support(self):
        scope = fd.adoption_weights(BOOK, "早班", NOW)
        plans = fd.roster_plan(BOOK, "增援", NOW, weights={"售团": 1}, scope=scope)
        warnings = plans[("11-25", "上午")]["warnings"]
        # 售团只在（低龄，上午）有点误差支撑，s9 是混龄 → 权重不采用
        self.assertTrue(any("售团" in w and "无点误差核对支撑" in w for w in warnings))

    def test_min_keep_and_pool_exclusivity(self):
        book = make_book(
            sessions=[session("a"), session("b", school_kind="混龄")],
            predictions=[pred("a", "票务", 40, 50, 70, 90, prob=0.8),
                         pred("b", "票务", 40, 50, 70, 90, prob=0.8)],
            crews=[{"shift": "上午", "pool": "C1", "type": "通用",
                    "available": 2, "min_keep": 1}])
        plans = fd.roster_plan(book, "早班", 9, weights={"票务": 1})
        plan = plans[("11-20", "上午")]
        # 可派只有 2-1=1 个单位；pool 被 a 独占后 b 什么也拿不到
        self.assertEqual(len(plan["assignments"]), 1)
        self.assertEqual(plan["assignments"][0]["slot"], "a")
        self.assertEqual(plan["margins"][0]["real_free"], 0)
        short = {u["slot"]: u["short"] for u in plan["unmet"]}
        self.assertEqual(short, {"a": 1, "b": 2})

    def test_different_weights_per_decision_allowed(self):
        early = fd.roster_plan(BOOK, "早班", NOW, weights={"票务": 1})
        intraday = fd.roster_plan(BOOK, "增援", NOW, weights={"售团": 1})
        self.assertIn(("11-25", "上午"), early)
        self.assertIn(("11-25", "上午"), intraday)


class ReportTest(unittest.TestCase):
    def test_report_covers_usability_reason_and_feedback(self):
        text = fd.render_report(BOOK, NOW)
        for marker in ("当时能用什么" if False else "各决策点当时能用什么",
                       "结束后补写" if False else "晚于早班决策点",
                       "无共同支撑", "真实可派", "不可释放", "下一轮采用",
                       "阈值已修订", "推算"):
            self.assertIn(marker, text)

    def test_report_at_earlier_seq_hides_later_knowledge(self):
        text = fd.render_report(BOOK, 2)
        self.assertIn("拥挤线 61（原始）", text)   # s8 修订 seq3 尚不可见
        self.assertNotIn("阈值已修订", text)


if __name__ == "__main__":
    unittest.main()
