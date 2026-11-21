"""交换样册的结构和原始引用检查。"""
from pathlib import Path
import tempfile
import unittest

from source_book import load


class SourceBookTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = load(Path(__file__).resolve().parents[1] / "data" / "samples.json")

    def test_references(self):
        for detail, key, master, master_key in REFERENCES:
            with self.subTest(detail=detail, field=key):
                acceptable = {row[master_key] for row in self.book[master]}
                for row in self.book[detail]:
                    value = row[key]
                    if value is not None:
                        values = value if isinstance(value, list) else [value]
                        self.assertTrue(set(values) <= acceptable)

    def test_bad_row_width_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            malformed = Path(directory) / "bad.json"
            malformed.write_text('{"tables":{"demo":{"fields":["ref"],"rows":[[1,2]]}}}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load(malformed)


REFERENCES = [('predictions', 'slot', 'sessions', 'slot'), ('gate', 'slot', 'sessions', 'slot'), ('commitments', 'pool', 'crews', 'pool')]
