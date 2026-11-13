import unittest

from kiwoom_autotrader.api.minute_chart import _bars


class MinuteOhlcTests(unittest.TestCase):
    def test_optional_prices_are_provider_values_not_inferred(self):
        row = {"cntr_tm": "20261030090100", "cur_prc": "+10000",
               "open_pric": "-9900", "high_pric": "+10100", "low_pric": "-9800"}
        bars = _bars({"return_code": 0, "stk_cd": "005930", "stk_min_pole_chart_qry": [row]}, "005930")
        self.assertEqual((bars[0].open_price, bars[0].high_price, bars[0].low_price), (9900, 10100, 9800))

    def test_missing_invalid_and_zero_prices_are_not_replaced_by_close(self):
        row = {"cntr_tm": "20261030090100", "cur_prc": "10000",
               "high_pric": "not-a-price", "low_pric": "0"}
        bars = _bars({"return_code": 0, "stk_cd": "005930", "stk_min_pole_chart_qry": [row]}, "005930")
        self.assertEqual((bars[0].open_price, bars[0].high_price, bars[0].low_price), (None, None, None))


if __name__ == "__main__":
    unittest.main()
