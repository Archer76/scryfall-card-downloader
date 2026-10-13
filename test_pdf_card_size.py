"""Parse real PDF page streams to verify physical card dimensions and grids."""
import contextlib
import io
from pathlib import Path
import re
import tempfile
import unittest

from PIL import Image
import scryfall_cards_threaded as core


def page_geometry(path):
    """Resolve each Page's Contents object rather than matching image payloads."""
    data = Path(path).read_bytes()
    objects = dict(re.findall(rb'(\d+) 0 obj\n(.*?)\nendobj', data, re.S))
    pages = []
    for body in objects.values():
        if not re.search(rb'/Type /Page\b', body):
            continue
        content_id = re.search(rb'/Contents (\d+) 0 R', body).group(1)
        stream = objects[content_id].split(b'stream\n', 1)[1].rsplit(b'\nendstream', 1)[0]
        matrices = [tuple(map(float, match)) for match in re.findall(
            rb'q ([\d.-]+) 0 0 ([\d.-]+) ([\d.-]+) ([\d.-]+) cm /Im\d+ Do Q', stream)]
        frames = [tuple(map(float, match)) for match in re.findall(
            rb'([\d.-]+) ([\d.-]+) ([\d.-]+) ([\d.-]+) re S', stream)]
        pages.append((matrices, frames))
    return pages


class PDFCardSizeTests(unittest.TestCase):
    def test_real_pdf_sizes_positions_frames_and_page_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'card.png'
            Image.new('RGB', (8, 12), (23, 97, 181)).save(image)
            for paper, columns, capacity in [('A4', 3, 9), ('A3', 6, 18)]:
                for size in [(63, 88), (59, 86)]:
                    for cut_marks in [True, False]:
                        with self.subTest(paper=paper, size=size, cut_marks=cut_marks):
                            pdf = Path(directory) / 'cards.pdf'
                            # Omit the parameter for the default-size positive control.
                            kwargs = {} if size == (63, 88) else {'card_size_mm': size}
                            with contextlib.redirect_stdout(io.StringIO()):
                                result = core.make_pdf([str(image)] * (capacity + 1), paper,
                                                       str(pdf), cut_marks=cut_marks, **kwargs)
                            self.assertEqual((result['pages'], result['per_page'], result['cols'], result['rows']),
                                             (2, capacity, columns, 3))
                            pages = page_geometry(pdf)
                            self.assertEqual([len(p[0]) for p in pages], [capacity, 1])
                            width, height = (value * 72 / 25.4 for value in size)
                            pw, ph = (value * 72 / 25.4 for value in core.PAPERS[paper])
                            mx, my = (pw - columns * width) / 2, (ph - 3 * height) / 2
                            for matrices, frames in pages:
                                self.assertEqual(len(frames), len(matrices) if cut_marks else 0)
                                for index, matrix in enumerate(matrices):
                                    x = mx + (index % columns) * width
                                    y = ph - my - (index // columns + 1) * height
                                    for actual, expected in zip(matrix, (width, height, x, y)):
                                        self.assertAlmostEqual(actual, expected, delta=0.0051)
                                    if cut_marks:
                                        for actual, expected in zip(frames[index], (x, y, width, height)):
                                            self.assertAlmostEqual(actual, expected, delta=0.0051)


if __name__ == '__main__':
    unittest.main()
