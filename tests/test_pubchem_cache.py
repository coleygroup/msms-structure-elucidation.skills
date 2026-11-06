"""Repeated feature queries share complete responses, preserving query identity."""
from concurrent.futures import ThreadPoolExecutor
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from msms_structure_elucidation import pubchem


class ResponseCacheTests(unittest.TestCase):
    def test_concurrent_identical_queries_have_one_network_owner(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'MSMS_PUBCHEM_CACHE_DIR': tmp, 'MSMS_PUBCHEM_CACHE_TTL_SECONDS': '60'}):
            counter = []
            def fetch(*args):
                counter.append(1)
                time.sleep(.02)
                return {'PropertyTable': {'Properties': [{'CID': 1}]}}
            with patch.object(pubchem, '_get_uncached', side_effect=fetch), ThreadPoolExecutor(8) as pool:
                results = list(pool.map(lambda _: pubchem._get('compound/fastformula/C2H6O/JSON'), range(16)))
            self.assertEqual(len(counter), 1)
            self.assertTrue(all(row == results[0] for row in results))

    def test_query_bodies_ttl_and_async_responses_are_not_conflated(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'MSMS_PUBCHEM_CACHE_DIR': tmp, 'MSMS_PUBCHEM_CACHE_TTL_SECONDS': '60'}):
            with patch.object(pubchem, '_get_uncached', return_value={'answer': 1}) as fetch:
                pubchem._get('properties', {'cid': '1'})
                pubchem._get('properties', {'cid': '2'})
                pubchem._get('properties', {'cid': '1'})
                self.assertEqual(fetch.call_count, 2)
                with patch.dict(os.environ, {'MSMS_PUBCHEM_CACHE_TTL_SECONDS': '0'}):
                    pubchem._get('properties', {'cid': '1'})
                self.assertEqual(fetch.call_count, 3)
            with patch.object(pubchem, '_get_uncached', return_value={'Waiting': {'ListKey': 'ephemeral'}}) as fetch:
                pubchem._get('async')
                pubchem._get('async')
                self.assertEqual(fetch.call_count, 2)

    def test_failed_fetch_is_not_published_as_an_empty_success(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'MSMS_PUBCHEM_CACHE_DIR': tmp}):
            with patch.object(pubchem, '_get_uncached', side_effect=RuntimeError('offline')):
                with self.assertRaisesRegex(RuntimeError, 'offline'):
                    pubchem._get('query')
            with patch.object(pubchem, '_get_uncached', return_value={}) as fetch:
                self.assertEqual(pubchem._get('query'), {})
                self.assertEqual(pubchem._get('query'), {})
                self.assertEqual(fetch.call_count, 1)


if __name__ == '__main__':
    unittest.main()
