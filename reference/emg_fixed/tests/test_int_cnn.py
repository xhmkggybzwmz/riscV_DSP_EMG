"""INT8 CNN reference: quantisation rules, width checks, and bit-exact window / stream / record agreement."""

import unittest

import numpy as np
import torch

from emg_fixed.cnn.int_model import StreamingIntCNN, forward_window_int, run_record_int
from emg_fixed.cnn.quantize import calibrate, float_record, m_to_fixed, quantise
from emg_fixed.esi import SRC_REPLAY, make_frames, reset_points
from emg_fixed.fixed import Overflow
from emg_fixed.postproc import NO_DECISION, UNCERTAIN, PostProcessor, top2
from emgml.models.cnn import CausalCNN, CNNSpec, fold_cnn

LSB = 2e-5          # mV per code (GRABMyo-like)


def make_q(seed=0):
    torch.manual_seed(seed)
    m = CausalCNN(CNNSpec(in_ch=4))
    for mod in m.features:
        if isinstance(mod, torch.nn.BatchNorm1d):
            mod.running_mean.uniform_(-0.2, 0.2)
            mod.running_var.uniform_(0.5, 2.0)
    m.eval()
    f = fold_cnn(m, np.array([30.0, 25.0, 40.0, 35.0]))
    rng = np.random.default_rng(seed)
    calib = [np.clip(rng.normal(scale=3000, size=(3000, 4)), -32768, 32767).astype(np.int16) for _ in range(4)]
    return f, quantise(f, LSB, calibrate(f, calib, LSB))


def stream(q, y, chunks, reset_at=()):
    sc = StreamingIntCNN(q)
    E, Lg, pos = [], [], 0
    for n in chunks:
        e, l = sc.process(y[pos:pos + n], sos=(pos == 0 or pos in reset_at))
        E.append(e)
        Lg.append(l)
        pos += n
    return np.concatenate(E), np.concatenate(Lg)


class TestQuantRules(unittest.TestCase):
    def test_m_to_fixed(self):
        for m in (3e-7, 0.0123, 0.5, 0.99999, 1.0, 7.3, 1234.5):
            M, s = m_to_fixed(m)
            self.assertTrue(2 ** 15 <= M < 2 ** 16 and s >= 1)
            self.assertLess(abs(M / 2 ** s - m) / m, 2 ** -15)
        with self.assertRaises(ValueError):
            m_to_fixed(0.0)

    def test_quantised_model_ranges(self):
        _, q = make_q()
        for L in q.layers:
            self.assertTrue(np.all(np.abs(L.wq) <= 127))
            self.assertTrue(np.all((L.M >= 2 ** 15) & (L.M < 2 ** 16)) and np.all(L.s >= 1))
            self.assertTrue(np.all(np.abs(L.bq) < 2 ** 31))
        self.assertTrue(np.all(np.abs(q.fc_wq) <= 127))
        self.assertEqual((q.D, q.RF, q.M_gap, q.first_step, q.every), (20, 40, 19, 18, 5))   # settle 0

    def test_worst_case_accumulators_fit(self):
        _, q = make_q()
        L1 = q.layers[0]
        bound1 = int(np.max(np.sum(np.abs(L1.wq), axis=(1, 2)))) * 32768 + int(np.max(np.abs(L1.bq)))
        self.assertLess(bound1, 2 ** 31)
        for L in q.layers[1:]:
            self.assertLess(int(np.max(np.sum(np.abs(L.wq), axis=(1, 2)))) * 255 + int(np.max(np.abs(L.bq))), 2 ** 31)
        self.assertLess(int(np.max(np.sum(np.abs(q.fc_wq), axis=1))) * 255 * 19 + int(np.max(np.abs(q.fc_bq))), 2 ** 31)
        self.assertLess(bound1 * (2 ** 16), 2 ** 47)           # acc * M fits the 48-bit product

    def test_int_close_to_float(self):
        f, q = make_q(1)
        y = np.clip(np.random.default_rng(9).normal(scale=3000, size=(3000, 4)), -32768, 32767).astype(np.int16)
        r = run_record_int(q, y)
        fl = float_record(f, y.astype(np.float64) * LSB, settle=q.settle)
        np.testing.assert_array_equal(r["ends"], fl["ends"])
        deq = r["logits"] * q.s_fc
        err = np.sqrt(np.mean((deq - fl["logits"]) ** 2)) / np.sqrt(np.mean(fl["logits"] ** 2))
        self.assertLess(err, 0.1)


class TestBitExact(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f, cls.q = make_q(2)
        rng = np.random.default_rng(5)
        cls.y = np.clip(rng.normal(scale=4000, size=(2600, 4)), -32768, 32767).astype(np.int16)

    def window_ref(self, y, ends):
        return np.array([forward_window_int(self.q, y[t - 399:t + 1].T)["logits"] for t in ends])

    def test_three_paths_agree(self):
        r = run_record_int(self.q, self.y)
        np.testing.assert_array_equal(r["ends"], np.arange(399, 2600, 100))
        w = self.window_ref(self.y, r["ends"])
        np.testing.assert_array_equal(r["logits"], w)
        for chunks in ([2600], [1, 37, 500, 2062], [100] * 26, [599, 1, 2000], [7] * 371 + [3]):
            e, l = stream(self.q, self.y, chunks)
            np.testing.assert_array_equal(e, r["ends"], err_msg=str(chunks[:4]))
            np.testing.assert_array_equal(l, r["logits"])
            np.testing.assert_array_equal(l.argmax(1), w.argmax(1))

    def test_single_sample_chunks(self):
        y = self.y[:900]
        e, l = stream(self.q, y, [1] * 900)
        r = run_record_int(self.q, y)
        np.testing.assert_array_equal(e, r["ends"])
        np.testing.assert_array_equal(l, r["logits"])

    def test_window_exactly_filled(self):
        # settle 0: first decision at sample index 399, i.e. after the 400th sample
        for n, k in ((399, 0), (400, 1), (401, 1), (499, 1), (500, 2)):
            e, _ = stream(self.q, self.y[:n], [n])
            self.assertEqual(len(e), k, n)

    def test_layer_outputs_agree(self):
        sc = StreamingIntCNN(self.q, record=True)
        for a, b in ((0, 333), (333, 1500), (1500, 2600)):
            sc.process(self.y[a:b], sos=(a == 0))
        r = run_record_int(self.q, self.y, keep=True)
        for i in range(3):
            got = np.concatenate([a for _, a in sc.log["act"][i]], axis=1)
            np.testing.assert_array_equal(got, r["act"][i])
            gacc = np.concatenate([a for _, a in sc.log["acc"][i]], axis=1)
            np.testing.assert_array_equal(gacc, r["acc"][i])
        np.testing.assert_array_equal(np.array([g for _, g in sc.log["gap"]]), r["gap"])

    def test_sos_gap_and_seq_discontinuity(self):
        fr = make_frames(self.y, SRC_REPLAY, seq0=0xFFFFFF00, gap_at=(1300,), seq_jump_at={1300: 17})
        rp = reset_points(fr)
        np.testing.assert_array_equal(rp, [0, 1300])
        codes = fr.lanes[:, :4]
        sc = StreamingIntCNN(self.q)
        e1, l1 = sc.process(codes[:1300], sos=True)
        e2, l2 = sc.process(codes[1300:], gap=True)
        r1, r2 = run_record_int(self.q, codes[:1300]), run_record_int(self.q, codes[1300:])
        np.testing.assert_array_equal(l1, r1["logits"])
        np.testing.assert_array_equal(e2, r2["ends"])                # schedule restarts after GAP
        np.testing.assert_array_equal(l2, r2["logits"])
        self.assertEqual(int(fr.seq[0]), 0xFFFFFF00)
        self.assertEqual(int(fr.seq[300]), 0x0000002C)                # 32-bit wrap
        sc.process(codes[:1300], sos=True)
        _, l3 = sc.process(codes[1300:])                             # no reset: must differ
        self.assertFalse(np.array_equal(l3[:len(l2)], l2))

    def test_extremes_and_saturation(self):
        sq = np.repeat(np.array([32767, -32768] * 13, dtype=np.int16), 100)[:, None].repeat(4, axis=1)
        sq[:, 1] = -sq[:, 1]
        r = run_record_int(self.q, sq, keep=True)
        self.assertGreater(int(np.sum(r["act"][0] == 255)), 0)       # activations clip at 255
        e, l = stream(self.q, sq, [123, 1000, 1477])
        np.testing.assert_array_equal(l, r["logits"])
        np.testing.assert_array_equal(l, self.window_ref(sq, e))

    def test_long_run_random_chunks(self):
        rng = np.random.default_rng(11)
        y = np.clip(rng.normal(scale=6000, size=(20000, 4)), -32768, 32767).astype(np.int16)
        chunks = []
        while sum(chunks) < 20000:
            chunks.append(int(min(rng.integers(1, 700), 20000 - sum(chunks))))
        e, l = stream(self.q, y, chunks)
        r = run_record_int(self.q, y)
        np.testing.assert_array_equal(e, r["ends"])
        np.testing.assert_array_equal(l, r["logits"])
        self.assertEqual(len(e), (20000 - 399 + 99) // 100)

    def test_width_violation_detected(self):
        _, q = make_q(3)
        q.layers[0].bq = q.layers[0].bq + (1 << 31)
        with self.assertRaises(Overflow):
            run_record_int(q, self.y[:700])

    def test_types(self):
        with self.assertRaises(TypeError):
            forward_window_int(self.q, self.y[:400].T.astype(np.int32))


class TestPostProc(unittest.TestCase):
    def test_top2_ties(self):
        self.assertEqual(top2([5, 9, 9, 1]), (1, 0))
        self.assertEqual(top2([-3, -1, -2]), (1, 1))

    def test_vote_rules(self):
        L = lambda c: [10 if k == c else 0 for k in range(6)]
        p = PostProcessor(K=5)
        outs = [p.step(L(c))["out"] for c in [1, 1, 2, 1, 2, 2, 2, 3, 3]]
        # first 4 decisions: NO_DECISION; [1,1,2,1,2] -> 1 has 3/5; then [1,2,1,2,2] -> 2 has 3
        self.assertEqual(outs, [NO_DECISION] * 4 + [1, 2, 2, 2, 2])
        p.reset()
        self.assertEqual(p.step(L(4))["out"], NO_DECISION)

    def test_uncertain_and_hold(self):
        p = PostProcessor(K=3, T=5)
        r = [p.step(x) for x in ([10, 0, 0, 0, 0, 0], [10, 8, 0, 0, 0, 0], [10, 0, 0, 0, 0, 0],
                                 [0, 10, 9, 0, 0, 0], [0, 10, 9, 0, 0, 0], [0, 10, 9, 0, 0, 0])]
        self.assertEqual([x["raw"] for x in r], [0, UNCERTAIN, 0, UNCERTAIN, UNCERTAIN, UNCERTAIN])
        self.assertEqual([x["out"] for x in r], [NO_DECISION, NO_DECISION, 0, 0, 0, 0])   # hold

    def test_k1_is_raw(self):
        p = PostProcessor(K=1)
        self.assertEqual([p.step(x)["out"] for x in ([1, 2, 3, 0, 0, 0], [9, 0, 0, 0, 0, 0])], [2, 0])
        with self.assertRaises(ValueError):
            PostProcessor(K=4)


if __name__ == "__main__":
    unittest.main()


class TestDecisionBoundary(unittest.TestCase):
    """E3 P0: decision schedule without warm-up (settle = 0)."""

    @classmethod
    def setUpClass(cls):
        cls.f, cls.q = make_q(6)
        cls.q.settle = 0
        rng = np.random.default_rng(8)
        cls.y = np.clip(rng.normal(scale=3000, size=(1200, 4)), -32768, 32767).astype(np.int16)

    def test_table(self):
        for n, k in ((399, 0), (400, 1), (499, 1), (500, 2), (599, 2), (600, 3)):
            e, _ = stream(self.q, self.y[:n], [n])
            self.assertEqual(len(e), k, n)
            e1, _ = stream(self.q, self.y[:n], [1] * n)              # one sample per chunk
            self.assertEqual(len(e1), k, n)
            self.assertEqual(len(run_record_int(self.q, self.y[:n])["ends"]), k, n)
        e, _ = stream(self.q, self.y[:600], [600])
        np.testing.assert_array_equal(e, [399, 499, 599])

    def test_first_window_is_samples_0_399(self):
        r = run_record_int(self.q, self.y[:400])
        np.testing.assert_array_equal(r["logits"][0], forward_window_int(self.q, self.y[:400].T)["logits"])

    def test_after_reset_same_boundary(self):
        sc = StreamingIntCNN(self.q)
        sc.process(self.y[:700], sos=True)
        e, _ = sc.process(self.y[700:1100], gap=True)
        np.testing.assert_array_equal(e, [399])                       # counted from the GAP frame

    def test_settle0_matches_settle200_from_599_on(self):
        import copy
        q200 = copy.copy(self.q)
        q200.settle = 200
        a, b = run_record_int(self.q, self.y), run_record_int(q200, self.y)
        np.testing.assert_array_equal(b["ends"], a["ends"][2:])
        np.testing.assert_array_equal(b["logits"], a["logits"][2:])
