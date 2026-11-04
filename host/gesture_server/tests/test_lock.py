"""Consumer lock (gesture_server.source.acquire_consumer_lock), Linux only: new / existing file,
second holder refused, release, symlink / directory / FIFO refused, concurrent creators."""

import os
import sys
import tempfile
import threading
import unittest

from gesture_server.source import SourceError, acquire_consumer_lock


@unittest.skipUnless(sys.platform.startswith("linux"), "flock / O_NOFOLLOW: Linux (the board) only")
class TestConsumerLock(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "consumer.lock")

    def test_create_hold_refuse_release(self):
        a = acquire_consumer_lock(self.p)
        self.assertTrue(os.path.isfile(self.p))
        with self.assertRaises(SourceError):
            acquire_consumer_lock(self.p)              # a second open file description: refused
        a.close()                                      # released by closing
        b = acquire_consumer_lock(self.p)
        b.close()
        self.assertTrue(os.path.isfile(self.p))        # never removed

    def test_existing_file_opened_without_create(self):
        open(self.p, "w").close()
        os.chmod(self.p, 0o664)
        a = acquire_consumer_lock(self.p)
        self.assertEqual(os.stat(self.p).st_mode & 0o777, 0o664)
        a.close()

    def test_symlink_refused(self):
        target = os.path.join(self.d, "elsewhere")
        open(target, "w").close()
        os.symlink(target, self.p)
        with self.assertRaises(SourceError):
            acquire_consumer_lock(self.p)
        dangling = os.path.join(self.d, "dangling.lock")
        os.symlink(os.path.join(self.d, "missing"), dangling)
        with self.assertRaises(SourceError):
            acquire_consumer_lock(dangling)            # not followed to create the target
        self.assertFalse(os.path.exists(os.path.join(self.d, "missing")))

    def test_directory_and_fifo_refused(self):
        os.mkdir(self.p)
        with self.assertRaises(SourceError):
            acquire_consumer_lock(self.p)
        fifo = os.path.join(self.d, "fifo.lock")
        os.mkfifo(fifo)
        with self.assertRaises(SourceError):
            acquire_consumer_lock(fifo)                # O_NONBLOCK: no hang, refused as non-regular

    def test_concurrent_acquirers_one_wins(self):
        got, refused = [], []
        start = threading.Barrier(8)

        def worker():
            start.wait()
            try:
                got.append(acquire_consumer_lock(self.p))
            except SourceError:
                refused.append(1)
        th = [threading.Thread(target=worker) for _ in range(8)]
        for t in th:
            t.start()
        for t in th:
            t.join()
        self.assertEqual((len(got), len(refused)), (1, 7))
        got[0].close()


if __name__ == "__main__":
    unittest.main()
