"""Regression tests for TCGdex palette PNGs and English set ID casing."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
import scryfall_cards_threaded as core
import ptcg_engine as ptcg


class PNGTests(unittest.TestCase):
    def test_palette_and_low_bit_grayscale_decode_and_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            palette = Image.new('P', (3, 1))
            palette.putpalette([255, 0, 0, 0, 255, 0, 0, 0, 255] + [0] * 759)
            palette.putdata([0, 1, 2])
            for bits in (2, 4, 8):
                path = root / ('palette%d.png' % bits)
                palette.save(path, bits=bits)
                self.assertEqual(core.decode_png_rgb(path),
                                 (3, 1, bytes([255, 0, 0, 0, 255, 0, 0, 0, 255])))
            gray = root / 'gray.png'
            Image.new('1', (2, 1), 1).save(gray)
            self.assertEqual(core.decode_png_rgb(gray), (2, 1, b'\xff' * 6))
            source = root / 'palette8.png'
            before = source.read_bytes()
            dest = root / 'cards.pdf'
            with contextlib.redirect_stdout(io.StringIO()):
                result = core.make_pdf([str(source)] * 48, 'A4', str(dest))
            self.assertEqual(result['cards'], 48)
            self.assertEqual(result['pages'], 6)
            self.assertEqual(dest.read_bytes().count(b'/Subtype /Image'), 48)
            self.assertEqual(source.read_bytes(), before)

    def test_rgb_rgba_and_invalid_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for mode, pixel in [('RGB', (17, 37, 89)), ('RGBA', (17, 37, 89, 128))]:
                path = root / (mode + '.png')
                Image.new(mode, (1, 1), pixel).save(path)
                self.assertEqual(core.decode_png_rgb(path), (1, 1, bytes([17, 37, 89])))
            broken = root / 'broken.png'
            broken.write_bytes(b'not a PNG')
            with self.assertRaises(Exception):
                core.decode_png_rgb(broken)


class SetIdentityTests(unittest.TestCase):
    def resolve_set(self, language, code, returned_id):
        client = ptcg.PTCGClient(language, 'unused', core)
        with patch.object(client, 'get', return_value={'id': returned_id, 'cards': []}):
            return client.set_for(code)

    def test_official_mee_alias_uses_canonical_response(self):
        self.assertEqual(self.resolve_set('en', 'MEE', 'mee')['id'], 'mee')

    def test_different_set_and_chinese_case_are_rejected(self):
        for language, code, returned in [('en', 'MEE', 'sve'), ('zh-tw', 'SV2a', 'sv2a')]:
            with self.subTest(language=language), self.assertRaises(ptcg.PTCGError) as error:
                self.resolve_set(language, code, returned)
            self.assertEqual(error.exception.status, 'identity_mismatch')


if __name__ == '__main__':
    unittest.main()
