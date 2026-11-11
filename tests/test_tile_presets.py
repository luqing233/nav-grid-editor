"""A clean install can browse/edit with presets; local captures always override them."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from nav_grid_editor.commands.build_presets import build_presets
from nav_grid_editor.services.maps import MapService, TileStore, merge_tiles


class TilePresetsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.tiles = self.root / 'tiles'
        self.preset = self.tiles / 'presets/m/4/-3_7.webp'
        self.preset.parent.mkdir(parents=True)
        Image.new('RGB', (256, 256), (200, 20, 30)).save(self.preset, 'WEBP', quality=90)
        composite = self.tiles / 'maps/m/4/m_4.webp'
        composite.parent.mkdir(parents=True)
        composite.write_bytes(self.preset.read_bytes())
        self.store = TileStore(self.tiles)

    def test_clean_install_finds_presets_and_keeps_original_tile_coordinates(self):
        self.assertEqual(self.store.list_maps(), {'m': ['4']})
        self.assertEqual(list(self.store.iter_tiles('m', '4')), [(-3, 7, self.preset)])
        svc = MapService(data_root=self.root, tiles_root=self.tiles, grid2d_dir=self.root / 'grids')
        composite = self.tiles / 'maps/m/4/m_4.webp'
        composite.parent.mkdir(parents=True, exist_ok=True)
        composite.write_bytes(self.preset.read_bytes())
        self.assertEqual(svc.serve_tile('m', '4', -3, 7), self.preset.read_bytes())
        info = svc.saved_map_info('m', '4')
        self.assertEqual((info['minX'], info['maxX'], info['minY'], info['maxY']), (-3, -3, 7, 7))
        self.assertIsNotNone(svc.ensure_overview('m', '4'))
        result = svc.save_grid2d('m', '4', {'origin': [0, 0, 0], 'cell_size': 1,
                                          'cells': [[0, 0]], 'blocked': [[1, 1]]})
        self.assertTrue(result['ok'], result)

    def test_local_capture_wins_and_merge_never_copies_presets(self):
        local = self.tiles / 'latest/m/4/-3_7.png'
        local.parent.mkdir(parents=True)
        local.write_bytes(b'new local tile')
        self.assertEqual(self.store.tile_bytes('m', '4', -3, 7), b'new local tile')
        self.assertEqual(list(self.store.iter_tiles('m', '4')), [(-3, 7, local)])
        local.unlink()
        self.assertEqual(merge_tiles(self.tiles)['copied'], 0)
        self.assertFalse(local.with_suffix('.webp').exists())
        self.assertEqual(self.store.tile_bytes('m', '4', -3, 7), self.preset.read_bytes())

    def test_layer_preset_loads_without_network(self):
        layers = self.tiles / 'layers'
        layers.mkdir()
        (layers / 'layer.webp').write_bytes(self.preset.read_bytes())
        (layers / 'catalog.json').write_text(json.dumps({'schema_version': 1, 'layers': [{
            'id': 'layer', 'map_name': 'm', 'path': 'layer.webp', 'filename': 'layer.webp',
            'bounds': {'west': 0, 'east': 256, 'south': 0, 'north': 256}}]}))
        svc = MapService(data_root=self.root, tiles_root=self.tiles, grid2d_dir=self.root / 'grids')
        with patch('nav_grid_editor.services.maps.service._download_layer_image', side_effect=AssertionError('network')):
            self.assertEqual(len(svc.list_layers('m', '4')), 1)
            self.assertEqual(svc.get_layer_image('m', '4', 'layer'), self.preset.read_bytes())
            self.assertIsNotNone(svc.get_layer_part('m', '4', 'layer'))

    def test_builder_preserves_geometry_and_hashes_and_rejects_existing_target(self):
        output = self.root / 'published'
        result = build_presets(self.tiles, output)
        self.assertEqual(result['tiles'], 1)
        manifest = json.loads((output / 'manifest.json').read_text())
        self.assertEqual(manifest['encoding']['quality'], 90)
        record = manifest['files'][0]
        raw = (output / record['path']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), record['sha256'])
        with Image.open(output / record['path']) as image:
            self.assertEqual(image.size, (256, 256))
            self.assertEqual(image.format, 'WEBP')
        with self.assertRaises(ValueError):
            build_presets(self.tiles, output)

    def test_failed_build_does_not_publish_partial_preset(self):
        (self.tiles / 'maps/m/4/m_4.webp').write_bytes(b'broken')
        output = self.root / 'published'
        with self.assertRaises(Exception):
            build_presets(self.tiles, output)
        self.assertFalse(output.exists())

    def test_builder_fills_missing_capture_tiles_from_complete_composite(self):
        self.preset.with_name('-1_7.webp').write_bytes(self.preset.read_bytes())
        composite = self.tiles / 'maps/m/4/m_4.webp'
        image = Image.new('RGB', (768, 256), (20, 180, 30))
        image.save(composite, 'WEBP', lossless=True)
        output = self.root / 'published'
        self.assertEqual(build_presets(self.tiles, output)['tiles'], 3)
        with Image.open(output / 'm/4/-2_7.webp') as tile:
            self.assertGreater(tile.getpixel((128, 128))[1], 170)

    def test_builder_rejects_misaligned_composite(self):
        Image.new('RGB', (512, 256)).save(self.tiles / 'maps/m/4/m_4.webp')
        with self.assertRaisesRegex(ValueError, '坐标边界'):
            build_presets(self.tiles, self.root / 'published')


if __name__ == '__main__':
    unittest.main()
