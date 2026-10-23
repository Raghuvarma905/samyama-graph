"""Offline preflight regressions; run with python3 -m unittest discover -s scripts -p test_falkordb_corpus_run.py."""
import unittest
from unittest.mock import patch

from falkordb_corpus_run import RespError, ensure_fresh_samyama, reset_falkordb, main


class FakeConn:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def call(self, *args):
        self.calls.append(args)
        return next(self.replies)


EMPTY = [[['count(n)'], [0]], [['label', 'property', 'type']],
         [['label', 'property', 'type']]]


class FreshServerTests(unittest.TestCase):
    def test_fresh_server(self):
        conn = FakeConn(EMPTY)
        ensure_fresh_samyama(conn)
        self.assertEqual(len(conn.calls), 3)

    def test_leftover_nodes(self):
        with self.assertRaisesRegex(RuntimeError, 'restart a dedicated'):
            ensure_fresh_samyama(FakeConn([[['count(n)'], [3]]]))

    def test_schema_without_nodes(self):
        for slot in (1, 2):
            replies = list(EMPTY)
            replies[slot] = [replies[slot][0], ['Person', 'age', 'INDEX']]
            with self.subTest(slot=slot), self.assertRaisesRegex(RuntimeError, 'restart a dedicated'):
                ensure_fresh_samyama(FakeConn(replies))

    def test_inspection_errors(self):
        for reply in (RespError('denied'), [['count(n)'], [RespError('nested')]]):
            with self.subTest(reply=reply), self.assertRaisesRegex(RuntimeError, 'could not inspect'):
                ensure_fresh_samyama(FakeConn([reply]))

    def test_malformed_count_is_not_empty(self):
        for reply in (None, [], [[], [0]], [['count(n)']], [['count(n)'], [False]],
                      [['count(n)'], ['0']], [['count(n)'], [0, 0]], [['count(n)'], 0]):
            with self.subTest(reply=reply), self.assertRaisesRegex(RuntimeError, 'unexpected'):
                ensure_fresh_samyama(FakeConn([reply]))

    def test_malformed_schema_is_not_empty(self):
        with self.assertRaisesRegex(RuntimeError, 'unexpected'):
            ensure_fresh_samyama(FakeConn([EMPTY[0], None]))

    def test_refusal_precedes_file_and_database_changes(self):
        fk = FakeConn([])
        sg = FakeConn([[['count(n)'], [1]]])
        with patch('falkordb_corpus_run.Conn', side_effect=[fk, sg]), \
             patch('pathlib.Path.read_text', return_value='RETURN 1;'), \
             patch('pathlib.Path.write_text') as write:
            self.assertEqual(main(['--falkordb', '1', '--samyama', '2', '--import-dir', '/unused']), 2)
        self.assertEqual(fk.calls, [])
        write.assert_not_called()


class FalkorResetTests(unittest.TestCase):
    def test_absent_corpus_does_not_delete_other_graphs(self):
        conn = FakeConn([["unrelated"]])
        reset_falkordb(conn)
        self.assertEqual(conn.calls, [("GRAPH.LIST",)])

    def test_existing_corpus_is_deleted_and_verified(self):
        conn = FakeConn([["corpus", "unrelated"], "Graph removed", ["unrelated"]])
        reset_falkordb(conn)
        self.assertEqual(conn.calls, [("GRAPH.LIST",), ("GRAPH.DELETE", "corpus"), ("GRAPH.LIST",)])

    def test_denied_deletion_is_fatal(self):
        with self.assertRaisesRegex(RuntimeError, "could not reset.*NOPERM"):
            reset_falkordb(FakeConn([["corpus"], RespError("NOPERM GRAPH.DELETE denied")]))

    def test_listing_errors_and_malformed_replies_are_fatal(self):
        for reply in (RespError("NOPERM"), None, {}, [123]):
            with self.subTest(reply=reply), self.assertRaises(RuntimeError):
                reset_falkordb(FakeConn([reply]))

    def test_success_reply_is_not_enough_when_graph_remains(self):
        with self.assertRaisesRegex(RuntimeError, "remains after reset"):
            reset_falkordb(FakeConn([["corpus"], "Graph removed", ["corpus"]]))

    def test_denied_reset_prevents_seed_measurement_and_csv_write(self):
        fk = FakeConn([["corpus"], RespError("NOPERM GRAPH.DELETE denied")])
        sg = FakeConn(EMPTY)
        with patch('falkordb_corpus_run.Conn', side_effect=[fk, sg]), \
             patch('pathlib.Path.read_text', return_value='RETURN 1;'), \
             patch('pathlib.Path.write_text') as write:
            self.assertEqual(main(['--falkordb', '1', '--samyama', '2', '--import-dir', '/unused']), 2)
        self.assertEqual(fk.calls, [("GRAPH.LIST",), ("GRAPH.DELETE", "corpus")])
        self.assertEqual(len(sg.calls), 3)  # only the read-only Samyama preflight
        write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
