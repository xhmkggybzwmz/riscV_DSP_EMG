"""E1b helpers: record keys, shared folds, NN validation split, int16 quantisation."""

import unittest

import numpy as np

from emgml.experiment import make_folds, quantise, record_keys, val_split
from emgml.splits import check_fold

CFG = {"data": {"participants": list(range(1, 11)), "sessions": [1, 2, 3], "gestures": [4, 11, 12, 15, 16, 17],
                "trials": list(range(1, 8))}}


class TestExperimentHelpers(unittest.TestCase):
    def setUp(self):
        self.keys = record_keys(CFG)
        self.folds = make_folds(self.keys)

    def test_g1_shape(self):
        self.assertEqual(len(self.keys), 1260)
        self.assertEqual(len(set(self.keys)), 1260)
        self.assertEqual(len(self.folds["P1"]), 10 * 3 * 7)
        self.assertEqual(len(self.folds["P2"]), 10 * 3)

    def test_val_split_inside_training(self):
        for proto in ("P1", "P2"):
            for f in self.folds[proto]:
                check_fold(self.keys, f)
                fit, val = val_split(self.keys, f.train)
                self.assertEqual(set(fit) | set(val), set(f.train))
                self.assertFalse(set(fit) & set(val))
                self.assertFalse(set(val) & set(f.test))
                vt = {self.keys[i][3] for i in val}
                self.assertEqual(len(vt), 1)
                self.assertFalse(vt & {self.keys[i][3] for i in fit})          # whole trial held out
                if proto == "P2":                                              # never the test day
                    self.assertFalse({self.keys[i][2] for i in val} & {self.keys[i][2] for i in f.test})
                self.assertEqual({self.keys[i][4] for i in val}, set(range(6)))  # all classes present

    def test_folds_deterministic(self):
        again = make_folds(record_keys(CFG))
        for p in ("P1", "P2"):
            self.assertEqual([(f.name, f.train, f.test) for f in again[p]],
                             [(f.name, f.train, f.test) for f in self.folds[p]])

    def test_quantise(self):
        x = np.array([[0.0, 1e-4], [0.5, -0.5], [10.0, -10.0]])
        q, clipped = quantise(x, 0.5 / 16384)
        self.assertEqual(clipped, 2)                       # +-10 exceed +-32767 LSB
        np.testing.assert_allclose(q[1], [0.5, -0.5])
        self.assertAlmostEqual(q[2, 0], 32767 * 0.5 / 16384)
        self.assertLessEqual(abs(q[0, 1] - 1e-4), 0.5 * 0.5 / 16384)


if __name__ == "__main__":
    unittest.main()
