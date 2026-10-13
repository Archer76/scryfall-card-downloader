"""PTCG name normalization and same-language/same-set/rules fallback guards."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import ptcg_engine as ptcg
import test_ptcg
import scryfall_cards_threaded as core


class NameEnergyTests(unittest.TestCase):
    setUp = test_ptcg.PTCGTests.setUp
    tearDown = test_ptcg.PTCGTests.tearDown
    get = test_ptcg.PTCGTests.get
    download = test_ptcg.PTCGTests.download
    run_task = test_ptcg.PTCGTests.run_task

    def card(self, number='223', set_id='svp', name="Professor's Research", category='Trainer', image=False):
        value = {'id': set_id + '-' + number, 'localId': number, 'name': name,
                 'set': {'id': set_id}, 'category': category}
        if category == 'Trainer':
            value.update(effect='Discard your hand and draw 7 cards.', trainerType='Supporter', regulationMark='G')
        elif category == 'Energy':
            value['energyType'] = 'Normal'
        elif category == 'Pokemon':
            value.update(hp=70, types=['Grass'], stage='Basic', attacks=[{'name': 'Leaf', 'damage': 20, 'cost': ['Grass']}])
        if image:
            value['image'] = 'https://assets.tcgdex.net/en/test/' + set_id + '/' + number
        return value

    def source(self, *cards):
        for card in cards:
            self.data['/cards/' + card['id']] = copy.deepcopy(card)
        set_id = cards[0]['set']['id']
        self.data['/sets/' + set_id] = {'id': set_id, 'cards': copy.deepcopy(list(cards))}

    def resolve(self, original, language='en'):
        client = ptcg.PTCGClient(language, str(self.base), core)
        with patch.object(ptcg.PTCGClient, 'get', autospec=True, side_effect=self.get):
            return client.resolve(original['name'], original['set']['id'], original['localId'])

    def test_boss_parentheses_cache_and_wrong_name(self):
        boss = self.card('154', 'swsh2', "Boss's Orders (Giovanni)", image=True)
        self.source(boss)
        code, manifest, _ = self.run_task("1 Boss's Orders （Giovanni (Team Rocket)） RCL 154")
        self.assertEqual(code, 0)
        self.assertEqual(manifest['cards'][0]['selected_id'], 'swsh2-154')
        self.calls.clear()
        self.downloads.clear()
        code, manifest, _ = self.run_task("2 Boss's Orders RCL 154")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.downloads, [])
        self.assertEqual(manifest['cards'][0]['qty'], 2)
        code, manifest, _ = self.run_task("1 Boss's Command RCL 154")
        self.assertEqual(code, 1)
        self.assertEqual(manifest['cards'][0]['status'], 'name_mismatch')

    def test_trainer_fallback_manifest_quantity_cache_and_repair(self):
        original = self.card()
        candidate = self.card('221', image=True)
        candidate['illustrator'] = 'Another artist'
        candidate['rarity'] = 'Rare'
        self.source(original, candidate)
        code, manifest, pdf = self.run_task("3 Professor's Research PR-SV 223")
        self.assertEqual(code, 0)
        entry = manifest['cards'][0]
        self.assertEqual(entry['selected_id'], 'svp-221')
        self.assertEqual(entry['selected_collector_number'], '221')
        self.assertEqual(entry['requested_id'], 'svp-223')
        self.assertEqual(entry['requested_tcgdex_set_id'], entry['tcgdex_set_id'])
        self.assertEqual(entry['fallback_reason'], 'same_set_missing_image')
        self.assertEqual(entry['fallback_policy'], ptcg.FALLBACK_POLICY)
        self.assertEqual(len(pdf.call_args.args[0]), 3)
        self.assertIn('原请求 PR-SV 223（svp-223）', (self.out / 'manifest.txt').read_text(encoding='utf-8'))
        self.calls.clear()
        self.downloads.clear()
        code, cached, _ = self.run_task("2 Professor's Research PR-SV 223")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.downloads, [])
        self.assertTrue(cached['cards'][0]['cache_hit'])
        Path(entry['files'][0]['file']).write_bytes(b'broken')
        code, _, _ = self.run_task("2 Professor's Research PR-SV 223")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.downloads, [candidate['image'] + '/high.png'])

    def test_rules_changes_and_missing_rules_rejected(self):
        original = self.card()
        candidate = self.card('221', image=True)
        for field, value in [('effect', 'Draw 3 cards.'), ('trainerType', 'Item'), ('regulationMark', 'H'), ('legal', {'standard': True})]:
            with self.subTest(field=field):
                changed = copy.deepcopy(candidate)
                changed[field] = value
                self.source(original, changed)
                with self.assertRaises(ptcg.PTCGError) as error:
                    self.resolve(original)
                self.assertEqual(error.exception.status, 'missing_image')
        for card in (original, candidate):
            card.pop('effect', None)
        self.source(original, candidate)
        with self.assertRaises(ptcg.PTCGError) as error:
            self.resolve(original)
        self.assertEqual(error.exception.status, 'missing_image')

    def test_pokemon_attacks_hp_and_types_guard(self):
        original = self.card('001', 'test', 'Plant', 'Pokemon')
        candidate = self.card('002', 'test', 'Plant', 'Pokemon', True)
        self.source(original, candidate)
        self.assertEqual(self.resolve(original)[0]['id'], 'test-002')
        for field, value in [('hp', 80), ('types', ['Fire']), ('attacks', [{'name': 'Leaf', 'damage': 30}])]:
            with self.subTest(field=field):
                changed = copy.deepcopy(candidate)
                changed[field] = value
                self.source(original, changed)
                with self.assertRaises(ptcg.PTCGError):
                    self.resolve(original)

    def test_candidate_identity_and_name_guards(self):
        original = self.card()
        candidate = self.card('221', image=True)
        for field, wrong in [('id', 'wrong-id'), ('localId', '222'), ('set', {'id': 'swsh1'}), ('name', 'Other Trainer')]:
            with self.subTest(field=field):
                self.source(original, candidate)
                self.data['/cards/svp-221'][field] = wrong
                with self.assertRaises(ptcg.PTCGError) as error:
                    self.resolve(original)
                self.assertEqual(error.exception.status, 'missing_image')

    def test_image_language_scheme_host_guards(self):
        original = self.card()
        candidate = self.card('221', image=True)
        for image in ('https://assets.tcgdex.net/zh-tw/SV/SV-P/221', 'https://example.test/en/svp/221', 'http://assets.tcgdex.net/en/svp/221'):
            with self.subTest(image=image):
                candidate['image'] = image
                self.source(original, candidate)
                with self.assertRaises(ptcg.PTCGError) as error:
                    self.resolve(original)
                self.assertEqual(error.exception.status, 'invalid_image')

    def test_energy_same_set_only_and_special_rules_guard(self):
        original = self.card('015', 'mee', 'Basic Darkness Energy', 'Energy')
        candidate = self.card('016', 'mee', 'Basic Darkness Energy', 'Energy', True)
        self.source(original, candidate)
        self.assertEqual(self.resolve(original)[0]['id'], 'mee-016')
        candidate['energyType'] = 'Special'
        candidate['effect'] = 'Provides two Energy.'
        self.source(original, candidate)
        with self.assertRaises(ptcg.PTCGError):
            self.resolve(original)
        self.source(original)
        # Global same-name candidates must never be queried or adopted.
        outside = self.card('81', 'g1', 'Basic Darkness Energy', 'Energy', True)
        self.data['/cards/g1-81'] = outside
        self.calls.clear()
        code, manifest, _ = self.run_task('9 Basic Darkness Energy MEE 15')
        self.assertEqual(code, 1)
        self.assertEqual(manifest['cards'][0]['status'], 'missing_image')
        self.assertFalse(any('/cards?' in path or path == '/cards/g1-81' for _, path in self.calls))

    def test_chinese_same_language_same_set_fallback(self):
        original = self.card('001', 'SV2a', '博士的研究')
        candidate = self.card('002', 'SV2a', '博士的研究', image=True)
        candidate['image'] = 'https://assets.tcgdex.net/zh-tw/SV/SV2a/002'
        self.source(original, candidate)
        card, url = self.resolve(original, 'zh-tw')
        self.assertEqual(card['id'], 'SV2a-002')
        self.assertIn('/zh-tw/', url)
        self.assertTrue(all(language == 'zh-tw' for language, _ in self.calls))

    def test_legacy_or_tampered_fallback_cache_rejected(self):
        original = self.card()
        candidate = self.card('221', image=True)
        self.source(original, candidate)
        _, manifest, _ = self.run_task("1 Professor's Research PR-SV 223")
        cache = ptcg.PTCGPictureCache(manifest['out_dir'], 'en', core)
        key = cache.key('PR-SV', '223')
        good = copy.deepcopy(cache.entries[key])
        mutations = [ {'fallback_reason': 'basic_energy_missing_image'},
                      {'fallback_policy': 'old'}, {'fallback_rule_fingerprint': ''},
                      {'tcgdex_set_id': 'g1'}, {'lang_code': 'zh-tw'} ]
        for change in mutations:
            with self.subTest(change=change):
                cache.entries[key] = dict(good, **change)
                self.assertIsNone(cache.lookup("Professor's Research", 'PR-SV', '223'))
        cache.entries[key] = copy.deepcopy(good)
        cache.entries[key]['files'][0]['url'] = 'https://assets.tcgdex.net/zh-tw/SV/SV-P/221/high.png'
        self.assertIsNone(cache.lookup("Professor's Research", 'PR-SV', '223'))
        cache.entries[key] = good
        self.assertIsNotNone(cache.lookup("Professor's Research", 'PR-SV', '223'))

    def test_request_error_stops_candidate_scan(self):
        original = self.card()
        first = self.card('221', image=True)
        second = self.card('222', image=True)
        self.source(original, first, second)
        self.data['/cards/svp-221'] = ptcg.PTCGError('request_error', 'TCGdex HTTP 429')
        with self.assertRaises(ptcg.PTCGError) as error:
            self.resolve(original)
        self.assertEqual(error.exception.status, 'request_error')
        self.assertNotIn(('en', '/cards/svp-222'), self.calls)


if __name__ == '__main__':
    unittest.main(verbosity=2)
