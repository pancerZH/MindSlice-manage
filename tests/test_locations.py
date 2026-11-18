import tempfile
import unittest
from pathlib import Path

from mindslice_manage.locations import (Location, LocationError, bumped, parse_version, read_value, render_version,
                                        rewrite, version_key)


class Versions(unittest.TestCase):
    def test_parse_and_render(self):
        self.assertEqual(parse_version("0.2.3-1"), "0.2.3-1")
        self.assertEqual(parse_version("0, 1, 6", "tuple"), "0.1.6")
        self.assertEqual(render_version("0.1.6", "tuple"), "0, 1, 6")
        for bad in ("v1.0", "1.0rc1", ""):
            with self.assertRaises(ValueError):
                parse_version(bad)

    def test_order(self):
        order = ["0.1.9", "0.2.0", "0.2.0-1", "0.2.0-2", "0.2.3", "0.10.0"]
        self.assertEqual(sorted(reversed(order), key=version_key), order)

    def test_bumped(self):
        self.assertEqual(bumped("0.2.3", "patch"), "0.2.4")
        self.assertEqual(bumped("0.2.3", "minor"), "0.3.0")
        self.assertEqual(bumped("0.2.3", "major"), "1.0.0")
        self.assertEqual(bumped("0.2.3-1", "build"), "0.2.3-2")
        self.assertEqual(bumped("0.2.3", "build"), "0.2.3-1")


class Files(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, data: bytes):
        (self.repo / name).write_bytes(data)

    def test_count_must_match(self):
        self.write("README.md", b"pip install x==1.0\nor x==1.0\n")
        loc = Location("README.md", r"x==([\d.]+)")
        with self.assertRaisesRegex(LocationError, "matches 2 times, expected 1"):
            read_value(self.repo, loc)
        loc.count = 2
        self.assertEqual(read_value(self.repo, loc), "1.0")

    def test_disagreeing_matches(self):
        self.write("README.md", b"x==1.0 and x==1.1\n")
        with self.assertRaisesRegex(LocationError, "differ"):
            read_value(self.repo, Location("README.md", r"x==([\d.]+)", count=2))

    def test_missing_file(self):
        with self.assertRaisesRegex(LocationError, "file not found"):
            read_value(self.repo, Location("nope.txt", "(x)"))

    def test_rewrite_keeps_everything_else(self):
        self.write("a.py", b'# header\r\nMINIMUM = (0, 1, 6)  # note\r\n')
        loc = Location("a.py", r"MINIMUM = \(([\d, ]+)\)", format="tuple")
        self.assertEqual(read_value(self.repo, loc), "0.1.6")
        self.assertTrue(rewrite(self.repo, loc, "0.2.0"))
        self.assertEqual((self.repo / "a.py").read_bytes(), b'# header\r\nMINIMUM = (0, 2, 0)  # note\r\n')
        self.assertFalse(rewrite(self.repo, loc, "0.2.0"))

    def test_values_other_than_versions(self):
        self.write("lock.json", b'{"commit": "' + b"a" * 40 + b'"}')
        loc = Location("lock.json", r'"commit": "([^"]+)"', value="commit")
        self.assertEqual(read_value(self.repo, loc), "a" * 40)
        self.write("lock.json", b'{"commit": "abc"}')
        with self.assertRaisesRegex(LocationError, "not a commit"):
            read_value(self.repo, loc)


if __name__ == "__main__":
    unittest.main()
