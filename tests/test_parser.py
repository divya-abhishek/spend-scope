import tempfile
import unittest
from pathlib import Path

import app.main as spend


class UniversalCsvParserTests(unittest.TestCase):
    def normalize(self, raw: bytes, name: str = "sample.csv"):
        frame, _, _ = spend.decode_csv(raw)
        inferred = spend.infer_mapping(frame)
        self.assertTrue(inferred["requiredOk"], inferred)
        return spend.build_normalized_frame(frame, inferred["mapping"], name)

    def test_money_pro_shape(self):
        raw = (
            b"TIME,TYPE,AMOUNT,CATEGORY,ACCOUNT,NOTES\n"
            b"Jul 01, 2026 3:43 AM,(-) Expense,1061,Shopping,Salary Account,shirt\n"
        )
        # Quoted commas are needed for real CSVs; this mini sample focuses on role inference,
        # so use an ISO timestamp instead to avoid an intentionally malformed fixture.
        raw = (
            b"TIME,TYPE,AMOUNT,CATEGORY,ACCOUNT,NOTES\n"
            b"2026-07-01 03:43,(-) Expense,1061,Shopping,Salary Account,shirt\n"
        )
        out, warnings, currency = self.normalize(raw)
        self.assertEqual(out.iloc[0].TYPE, "expense")
        self.assertEqual(out.iloc[0].AMOUNT, 1061)
        self.assertEqual(out.iloc[0].CATEGORY, "Shopping")
        self.assertEqual(currency, "INR")
        self.assertEqual(warnings, [])

    def test_signed_amount(self):
        raw = (
            b"Date,Merchant,Amount,Category\n"
            b"2026-08-01,Coffee,-250,Food\n"
            b"2026-08-02,Salary,50000,Income\n"
        )
        out, _, _ = self.normalize(raw)
        self.assertEqual(out.TYPE.tolist(), ["expense", "income"])
        self.assertEqual(out.AMOUNT.tolist(), [250.0, 50000.0])

    def test_debit_credit(self):
        raw = (
            b"Posted Date,Description,Debit,Credit,Account Name\n"
            b"13/08/2026,Coffee,250,,Card\n"
            b"14/08/2026,Salary,,50000,Bank\n"
        )
        out, _, _ = self.normalize(raw)
        self.assertEqual(out.TYPE.tolist(), ["expense", "income"])
        self.assertEqual(out.ACCOUNT.tolist(), ["Card", "Bank"])
        self.assertEqual(out.TIME.dt.strftime("%Y-%m-%d").tolist(), ["2026-08-13", "2026-08-14"])

    def test_minimal_positive_spend_file(self):
        raw = b"when;details;value\n2026-08-01;coffee;250\n2026-08-02;taxi;400\n"
        out, warnings, _ = self.normalize(raw)
        self.assertEqual(out.TYPE.tolist(), ["expense", "expense"])
        self.assertEqual(out.CATEGORY.unique().tolist(), ["Uncategorized"])
        self.assertTrue(warnings)

    def test_currency_and_us_date_hint(self):
        raw = (
            b"Date,Description,Amount,Currency\n"
            b"08/01/2026,Coffee,-4.50,USD\n"
            b"08/02/2026,Taxi,-22.00,USD\n"
        )
        out, _, currency = self.normalize(raw)
        self.assertEqual(currency, "USD")
        self.assertEqual(out.TIME.dt.strftime("%Y-%m-%d").tolist(), ["2026-08-01", "2026-08-02"])

    def test_european_and_accounting_number_formats(self):
        values = spend.numeric_series(__import__("pandas").Series(["1.234,56", "1,234.56", "(1,250.50)", "250-"]))
        self.assertEqual(values.tolist(), [1234.56, 1234.56, -1250.5, -250.0])

    def test_duplicate_detection(self):
        old_path = spend.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as td:
                spend.DB_PATH = Path(td) / "test.db"
                spend.init_db()
                raw = b"Date,Description,Amount\n2026-08-01,Coffee,-250\n"
                first = spend.import_bytes(raw, "a.csv")
                second = spend.import_bytes(raw, "a.csv")
                self.assertEqual(first["status"], "imported")
                self.assertEqual(second["status"], "duplicate")
        finally:
            spend.DB_PATH = old_path


if __name__ == "__main__":
    unittest.main()
