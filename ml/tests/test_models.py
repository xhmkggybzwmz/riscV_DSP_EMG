"""MLP / CNN: cost model, folding equivalence, CNN geometry and window-vs-stream equivalence."""

import unittest

import numpy as np
import torch

from emgml.models.cnn import (CausalCNN, CNNSpec, StreamingCNN, check_geometry, cnn_cost, fold_cnn,
                              forward_window_folded, train_cnn)
from emgml.models.mlp import fold_standardisation, forward_folded, mlp_cost, predict_mlp, train_mlp


def random_cnn(spec, seed=0):
    torch.manual_seed(seed)
    m = CausalCNN(spec)
    for mod in m.features:
        if isinstance(mod, torch.nn.BatchNorm1d):
            mod.running_mean.uniform_(-0.2, 0.2)
            mod.running_var.uniform_(0.5, 2.0)
            mod.weight.data.uniform_(0.5, 1.5)
            mod.bias.data.uniform_(-0.1, 0.1)
    m.eval()
    return m


class TestMLP(unittest.TestCase):
    def test_cost(self):
        c = mlp_cost(20, (32, 16), 6)
        self.assertEqual(c["macs_per_window"], 20 * 32 + 32 * 16 + 16 * 6)
        self.assertEqual(c["params"], 20 * 32 + 32 + 32 * 16 + 16 + 16 * 6 + 6)

    def test_train_and_fold_equivalence(self):
        rng = np.random.default_rng(0)
        y = rng.integers(0, 3, size=600)
        X = rng.normal(size=(600, 20)) * rng.uniform(0.01, 50, size=20) + y[:, None] * 0.5 + 7.0
        res = train_mlp(X[:400], y[:400], X[400:], y[400:], (32, 16), 3, seed=1, max_epochs=30, patience=5)
        self.assertTrue(0 <= res.best_epoch < len(res.history))
        self.assertTrue(all({"train_loss", "val_loss", "val_bal_acc"} <= set(h) for h in res.history))
        layers = fold_standardisation(res)
        with torch.no_grad():
            ref = res.model(torch.tensor((X - res.mu) / res.sigma, dtype=torch.float32)).double().numpy()
        np.testing.assert_allclose(forward_folded(layers, X), ref, rtol=1e-4, atol=1e-4)
        np.testing.assert_array_equal(forward_folded(layers, X).argmax(1), predict_mlp(res, X))

    def test_seed_reproducible(self):
        rng = np.random.default_rng(1)
        X, y = rng.normal(size=(300, 8)), rng.integers(0, 2, size=300)
        a = train_mlp(X[:200], y[:200], X[200:], y[200:], (8,), 2, seed=5, max_epochs=10)
        b = train_mlp(X[:200], y[:200], X[200:], y[200:], (8,), 2, seed=5, max_epochs=10)
        for pa, pb in zip(a.model.parameters(), b.model.parameters()):
            torch.testing.assert_close(pa, pb, rtol=0, atol=0)


class TestCNNGeometry(unittest.TestCase):
    def test_default_ok(self):
        g = check_geometry(CNNSpec())
        self.assertTrue(g["ok"], g["problems"])
        self.assertEqual((g["D"], g["RF"], g["M"], g["lengths"]), (20, 40, 19, [197, 96, 19]))
        self.assertEqual(g["first_window_end"], 599)

    def test_v01_sketch_rejected(self):
        g = check_geometry(CNNSpec(layers=[(16, 7, 2), (32, 5, 2), (32, 3, 2)]))
        self.assertFalse(g["ok"])
        self.assertEqual(g["D"], 8)

    def test_cost_limits(self):
        for ch in (4, 8):
            c = cnn_cost(CNNSpec(in_ch=ch))
            self.assertLessEqual(c["params_deployed"], 16000)
            self.assertLessEqual(c["stream_macs_per_s"], 10e6)
            self.assertLess(c["stream_macs_per_s"], c["macs_per_window"] * 20)


class TestCNNEquivalence(unittest.TestCase):
    def setUp(self):
        self.spec = CNNSpec(in_ch=4)
        self.model = random_cnn(self.spec)
        self.scale = np.array([20.0, 15.0, 30.0, 25.0])
        self.f = fold_cnn(self.model, self.scale)
        rng = np.random.default_rng(3)
        self.x = rng.normal(scale=0.05, size=(2600, 4))           # stream from SOS, DSP-output-like

    def window_refs(self, x):
        ends = np.arange(599, x.shape[0], 100)
        torch_logits, folded = [], []
        for t in ends:
            w = x[t - 399:t + 1].T
            with torch.no_grad():
                xt = torch.tensor(w * self.scale[:, None], dtype=torch.float64)[None]
                torch_logits.append(self.model.double()(xt)[0].numpy())
            folded.append(forward_window_folded(self.f, w))
        self.model.float()
        return ends, np.array(torch_logits), np.array(folded)

    def test_window_folded_equals_torch(self):
        _, tl, fl = self.window_refs(self.x)
        np.testing.assert_allclose(fl, tl, rtol=1e-9, atol=1e-9)

    def test_stream_equals_windows_any_chunking(self):
        ends, _, fl = self.window_refs(self.x)
        for chunks in ([2600], [1, 37, 500, 2062], [100] * 26, [7] * 371 + [3]):
            sc = StreamingCNN(self.f, self.spec)
            e_all, l_all, pos = [], [], 0
            for n in chunks:
                e, l = sc.process(self.x[pos:pos + n])
                pos += n
                e_all.append(e)
                l_all.append(l)
            e = np.concatenate(e_all)
            l = np.concatenate([a for a in l_all if len(a)])
            np.testing.assert_array_equal(e, ends, err_msg=str(chunks[:3]))
            np.testing.assert_allclose(l, fl, rtol=1e-9, atol=1e-9)

    def test_reset_is_sos(self):
        sc = StreamingCNN(self.f, self.spec)
        sc.process(self.x[:1234])
        sc.reset()
        e1, l1 = sc.process(self.x[1234:])
        e2, l2 = StreamingCNN(self.f, self.spec).process(self.x[1234:])
        np.testing.assert_array_equal(e1, e2)
        np.testing.assert_allclose(l1, l2, rtol=0, atol=0)

    def test_tiny_training_runs(self):
        rng = np.random.default_rng(0)
        X = torch.tensor(rng.normal(size=(6, 4, 1200)), dtype=torch.float32)
        y_rec = np.array([0, 1, 2, 0, 1, 2])
        X[y_rec == 1] *= 3.0
        U = X.unfold(2, 400, 100)
        rec = np.repeat(np.arange(6), 7)
        k = np.tile(np.arange(2, 9), 6)
        res = train_cnn(U, rec[:28], k[:28], y_rec[rec[:28]], rec[28:], k[28:], y_rec[rec[28:]],
                        np.ones(4), CNNSpec(in_ch=4, n_classes=3), seed=0, max_epochs=3, patience=2)
        self.assertTrue(1 <= len(res.history) <= 3)
        self.assertTrue(0 <= res.best_epoch < len(res.history))


if __name__ == "__main__":
    unittest.main()
