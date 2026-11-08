import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image
from nav_grid_editor.services.maps import MapService


class EditorImprovementsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.svc = MapService(data_root=self.root, grid2d_dir=self.root / 'grids')

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_cell_document_cannot_overwrite_new_height(self):
        payload = dict(origin=[0, 0, 0], cell_size=1, cells=[[0, 0]], blocked=[])
        self.assertTrue(self.svc.save_grid2d('m', '4', payload)['ok'])
        stale = self.svc.get_grid2d('m', '4')['data']
        region = dict(x0=0, x1=1, z0=0, z1=1, y_min=10, y_max=20)
        self.assertTrue(self.svc.save_grid_floor_regions('m', '4', '', [region])['ok'])
        stale['preserve_floor_regions'] = True
        stale['cells'].append([1, 0])
        self.assertTrue(self.svc.save_grid2d('m', '4', stale)['ok'])
        saved = self.svc.get_grid2d('m', '4')['data']
        self.assertEqual(saved['floor_regions'], [region])
        self.assertEqual(len(saved['cells']), 2)

    def test_sparse_cache_isolated_and_invalidated(self):
        payload = dict(origin=[0, 0, 0], cell_size=1, cells=[[0, 0]], blocked=[])
        self.svc.save_grid2d('m', '4', payload)
        first = self.svc.get_grid2d('m', '4')
        first['data']['cells'].clear()
        self.assertEqual(self.svc.get_grid2d('m', '4')['data']['cells'], [[0, 0]])
        payload['cells'].append([1, 0])
        self.svc.save_grid2d('m', '4', payload)
        self.assertEqual(len(self.svc.get_grid2d('m', '4')['data']['cells']), 2)

    def test_layer_preview_and_edge_tile_preserve_pixels(self):
        from unittest.mock import patch
        image = Image.new('RGBA', (1500, 1100), (20, 40, 60, 128))
        source = self.root / 'assets/tiles/test.png'
        source.parent.mkdir(parents=True)
        image.save(source)
        with patch('nav_grid_editor.services.maps.service.find_layer', return_value={'id': 't'}), patch('nav_grid_editor.services.maps.service.layer_cache_relpath', return_value=Path('test.png')), patch.object(self.svc, 'get_layer_image', side_effect=lambda *args: source.read_bytes()):
            preview = self.svc.get_layer_part('m', '4', 't', edge=1024)
            with Image.open(BytesIO(preview)) as result:
                self.assertLessEqual(max(result.size), 1024)
            tile = self.svc.get_layer_part('m', '4', 't', x=1, y=1)
            with Image.open(BytesIO(tile)) as result:
                self.assertEqual(result.size, (476, 76))
                self.assertEqual(result.getpixel((0, 0)), (20, 40, 60, 128))
            self.assertIsNone(self.svc.get_layer_part('m', '4', 't', x=99, y=99))
