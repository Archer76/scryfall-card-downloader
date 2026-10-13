"""Offline regression coverage for PTCG names and basic-energy image fallback."""
import copy
from pathlib import Path
import urllib.parse
import unittest

import ptcg_engine as ptcg
import test_ptcg
import scryfall_cards_threaded as core


class NameEnergyTests(unittest.TestCase):
    setUp = test_ptcg.PTCGTests.setUp
    tearDown = test_ptcg.PTCGTests.tearDown
    get = test_ptcg.PTCGTests.get
    download = test_ptcg.PTCGTests.download
    run_task = test_ptcg.PTCGTests.run_task
    def energy(self, card_id='sve-023', name='Darkness Energy', energy_type='Normal', image=None):
        set_id, local_id = card_id.rsplit('-', 1)
        return {'id': card_id, 'name': name, 'localId': local_id,
                'set': {'id': set_id}, 'category': 'Energy',
                'energyType': energy_type, 'image': image}

    def source(self, card):
        self.data['/sets/' + card['set']['id']] = {
            'id': card['set']['id'], 'cards': [copy.deepcopy(card)]}
        self.data['/cards/' + card['id']] = copy.deepcopy(card)

    def search(self, name, cards):
        self.data['/cards?' + urllib.parse.urlencode({'name': name})] = copy.deepcopy(cards)
        for card in cards:
            self.data['/cards/' + card['id']] = copy.deepcopy(card)

    def setup_darkness(self):
        self.source(self.energy())
        special = self.energy('neo1-104', energy_type='Special',
                              image='https://assets.tcgdex.net/en/neo/neo1/104')
        basic = self.energy('sm1-170', image='https://assets.tcgdex.net/en/sm/sm1/170')
        self.search('Darkness Energy', [special, basic])
        return special, basic

    def test_boss_parentheses_and_current_request_on_cache_hit(self):
        boss = {'id': 'swsh2-154', 'localId': '154', 'name': "Boss's Orders (Giovanni)",
                'set': {'id': 'swsh2'}, 'category': 'Trainer',
                'image': 'https://assets.tcgdex.net/en/swsh/swsh2/154'}
        self.source(boss)
        code, manifest, _ = self.run_task("1 Boss's Orders （Giovanni (Team Rocket)） RCL 154")
        self.assertEqual(code, 0)
        self.assertEqual(manifest['cards'][0]['selected_id'], 'swsh2-154')
        self.calls.clear()
        self.downloads.clear()
        code, manifest, _ = self.run_task("2  Boss's   Orders RCL 154")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.downloads, [])
        entry = manifest['cards'][0]
        self.assertTrue(entry['cache_hit'])
        self.assertEqual(entry['input_name'], "Boss's   Orders")
        self.assertEqual(entry['input'], "Boss's   Orders RCL 154")
        self.assertEqual(entry['qty'], 2)
        code, manifest, _ = self.run_task("1 Boss's Command (Giovanni) RCL 154")
        self.assertEqual(code, 1)
        self.assertEqual(manifest['cards'][0]['status'], 'name_mismatch')
        code, manifest, _ = self.run_task("1 Boss's Orders RCL 155")
        self.assertEqual(code, 1)
        self.assertEqual(manifest['cards'][0]['status'], 'missing_card')

    def test_sve_fallback_skips_special_records_actual_manifest_and_cache(self):
        self.setup_darkness()
        code, manifest, _ = self.run_task('8 Basic Darkness Energy SVE 23')
        self.assertEqual(code, 0)
        entry = manifest['cards'][0]
        self.assertEqual(entry['selected_id'], 'sm1-170')
        self.assertEqual(entry['tcgdex_set_id'], 'sm1')
        self.assertEqual(entry['selected_collector_number'], '170')
        self.assertEqual(entry['set_code'], 'SVE')
        self.assertEqual(entry['collector_number'], '23')
        self.assertEqual(entry['requested_set_code'], 'SVE')
        self.assertEqual(entry['requested_collector_number'], '23')
        self.assertEqual(entry['requested_id'], 'sve-023')
        self.assertEqual(entry['requested_tcgdex_set_id'], 'sve')
        self.assertEqual(entry['requested_tcgdex_collector_number'], '023')
        self.assertEqual(entry['fallback_reason'], 'basic_energy_missing_image')
        self.assertIn('sm1 170', Path(entry['files'][0]['file']).name)
        self.assertEqual(entry['files'][0]['url'], 'https://assets.tcgdex.net/en/sm/sm1/170/high.png')
        report = (self.out / 'manifest.txt').read_text(encoding='utf-8')
        self.assertIn('sm1-170 | sm1 170', report)
        self.assertIn('原请求 SVE 23（sve-023）', report)
        self.assertIn(('en', '/cards/neo1-104'), self.calls)
        self.calls.clear()
        self.downloads.clear()
        code, manifest, _ = self.run_task('3 Basic Darkness Energy SVE 023')
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.downloads, [])
        entry = manifest['cards'][0]
        self.assertEqual(entry['collector_number'], '023')
        self.assertEqual(entry['requested_collector_number'], '023')
        self.assertEqual(entry['selected_id'], 'sm1-170')
        self.assertEqual(entry['fallback_reason'], 'basic_energy_missing_image')
        self.assertTrue(entry['cache_hit'])
        Path(entry['files'][0]['file']).write_bytes(b'broken')
        self.calls.clear()
        code, _, _ = self.run_task('3 Basic Darkness Energy SVE 23')
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.downloads, ['https://assets.tcgdex.net/en/sm/sm1/170/high.png'])

    def test_no_special_or_different_energy_substitution(self):
        special, basic = self.setup_darkness()
        water = self.energy('sm1-166', name='Water Energy',
                            image='https://assets.tcgdex.net/en/sm/sm1/166')
        double = self.energy('test-001', name='Double Darkness Energy',
                             image='https://assets.tcgdex.net/en/test/test/001')
        self.search('Darkness Energy', [special, water, double])
        code, manifest, _ = self.run_task('1 Basic Darkness Energy SVE 23')
        self.assertEqual(code, 1)
        self.assertEqual(manifest['cards'][0]['status'], 'missing_image')
        self.assertEqual(self.downloads, [])
        self.assertNotIn(('en', '/cards/sm1-166'), self.calls)
        self.assertNotIn(('en', '/cards/test-001'), self.calls)
        special['image'] = None
        self.source(special)
        self.calls.clear()
        code, manifest, _ = self.run_task('1 Darkness Energy neo1 104')
        self.assertEqual(code, 1)
        self.assertEqual(manifest['cards'][0]['status'], 'missing_image')
        self.assertFalse(any('/cards?' in path for _, path in self.calls))

    def test_unknown_or_non_basic_energy_source_does_not_search(self):
        for energy_type in ('Special', None):
            with self.subTest(energy_type=energy_type):
                self.source(self.energy(energy_type=energy_type))
                self.calls.clear()
                code, manifest, _ = self.run_task('1 Darkness Energy SVE 23')
                self.assertEqual(code, 1)
                self.assertEqual(manifest['cards'][0]['status'], 'missing_image')
                self.assertFalse(any('/cards?' in path for _, path in self.calls))
        self.source(self.energy(name='Double Darkness Energy'))
        self.calls.clear()
        code, manifest, _ = self.run_task('1 Double Darkness Energy SVE 23')
        self.assertEqual(code, 1)
        self.assertEqual(manifest['cards'][0]['status'], 'missing_image')
        self.assertFalse(any('/cards?' in path for _, path in self.calls))

    def test_fallback_candidate_details_must_match_name_and_identity(self):
        special, basic = self.setup_darkness()
        self.search('Darkness Energy', [basic])
        for field, wrong in (('name', 'Water Energy'), ('id', 'wrong-id'), ('localId', '171'), ('category', 'Trainer')):
            with self.subTest(field=field):
                changed = copy.deepcopy(basic)
                changed[field] = wrong
                self.data['/cards/sm1-170'] = changed
                code, manifest, _ = self.run_task('1 Basic Darkness Energy SVE 23')
                self.assertEqual(code, 1)
                self.assertEqual(manifest['cards'][0]['status'], 'missing_image')
                self.assertEqual(self.downloads, [])

    def test_fallback_image_host_and_language_guards(self):
        _, basic = self.setup_darkness()
        for image in ('https://assets.tcgdex.net/zh-tw/SV/SV2a/001',
                      'https://example.test/en/sm/sm1/170',
                      'http://assets.tcgdex.net/en/sm/sm1/170'):
            with self.subTest(image=image):
                basic['image'] = image
                self.search('Darkness Energy', [basic])
                code, manifest, _ = self.run_task('1 Basic Darkness Energy SVE 23')
                self.assertEqual(code, 1)
                self.assertEqual(manifest['cards'][0]['status'], 'invalid_image')
                self.assertEqual(self.downloads, [])
                self.assertTrue(all(language == 'en' for language, _ in self.calls))

    def test_chinese_energy_fallback_and_cache_are_language_isolated(self):
        self.setup_darkness()
        self.run_task('1 Basic Darkness Energy SVE 23')
        self.calls.clear()
        self.downloads.clear()
        # Officially verified Special mislabel: this ID alone is an allowed source.
        self.source(self.energy('S8a-DAR', '基本惡能量', 'Special'))
        candidate = self.energy('SLD-DAR', '基本【惡】能量', 'Normal',
                                'https://assets.tcgdex.net/zh-tw/SL/SLD/DAR')
        self.search('基本惡能量', [])
        self.search('基本【惡】能量', [candidate])
        # Parser currently requires an ASCII numeric collector number. Exercise
        # the metadata resolver with the real nonnumeric IDs directly instead.
        client = ptcg.PTCGClient('zh-tw', str(self.base), core)
        from unittest.mock import patch
        with patch.object(ptcg.PTCGClient, 'get', autospec=True, side_effect=self.get):
            card, url = client.resolve('基本惡能量', 'S8a', 'DAR')
        self.assertEqual(card['id'], 'SLD-DAR')
        self.assertIn('/zh-tw/', url)
        self.assertTrue(all(language == 'zh-tw' for language, _ in self.calls))
        # Numeric Chinese source verifies the full download/cache/manifest route.
        self.source(self.energy('SV2a-099', '基本水能量', 'Normal'))
        water = self.energy('SV1-100', '基本水能量', 'Basic',
                            'https://assets.tcgdex.net/zh-tw/SV/SV1/100')
        self.search('基本水能量', [water])
        self.search('基本【水】能量', [])
        code, manifest, _ = self.run_task('1 基本水能量 SV2a 99', 'ptcg-zh')
        self.assertEqual(code, 0)
        self.assertEqual(manifest['cards'][0]['selected_id'], 'SV1-100')
        self.calls.clear()
        self.downloads.clear()
        code, manifest, _ = self.run_task('2 基本水能量 SV2a 99', 'ptcg-zh')
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.downloads, [])
        self.assertTrue(manifest['cards'][0]['cache_hit'])

    def test_chinese_special_mislabel_does_not_allow_unknown_special(self):
        self.source(self.energy('SV2a-099', '基本惡能量', 'Special'))
        code, manifest, _ = self.run_task('1 基本惡能量 SV2a 99', 'ptcg-zh')
        self.assertEqual(code, 1)
        self.assertEqual(manifest['cards'][0]['status'], 'missing_image')
        self.assertFalse(any('/cards?' in path for _, path in self.calls))
        self.assertEqual(self.downloads, [])

    def test_fallback_request_error_propagates_without_more_candidate_requests(self):
        _, basic = self.setup_darkness()
        second = self.energy('swsh1-201', image='https://assets.tcgdex.net/en/swsh/swsh1/201')
        self.search('Darkness Energy', [basic, second])
        self.data['/cards/sm1-170'] = ptcg.PTCGError('request_error', 'TCGdex HTTP 429')
        code, manifest, _ = self.run_task('1 Basic Darkness Energy SVE 23')
        self.assertEqual(code, 1)
        self.assertEqual(manifest['cards'][0]['status'], 'request_error')
        self.assertNotIn(('en', '/cards/swsh1-201'), self.calls)
        self.assertEqual(self.downloads, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
