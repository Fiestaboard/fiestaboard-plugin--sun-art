"""Board-geometry conformance for the sun_art plugin.

Sun Art paints the whole board, so it has to be correct on every board
FiestaBoard supports: a Note (15x3), a Flagship (22x6) and any note array up
to 120x24 -- which is what a FiestaPanel is. The checks themselves live in
FiestaBoard core (``src/plugins/geometry_conformance``) so the definition of
"supports every board" is shared rather than re-litigated per plugin.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytz

# Ensure astral is mocked before import (see tests/conftest.py).
if 'astral' not in sys.modules or not hasattr(sys.modules['astral'], 'LocationInfo'):
    mock_astral_module = MagicMock()
    mock_astral_module.LocationInfo = MagicMock()
    mock_astral_module.sun = MagicMock()
    sys.modules['astral'] = mock_astral_module
    sys.modules['astral.sun'] = mock_astral_module.sun

from src.devices import BoardContext
from src.plugins.geometry_conformance import (
    STANDARD_GEOMETRIES,
    assert_board_conformance,
)
from src.text_to_board import count_tiles

from plugins.sun_art import STAGE_STYLES, SunArtPlugin

_MANIFEST_PATH = Path(__file__).resolve().parent.parent / "manifest.json"


def _manifest() -> dict:
    """The real manifest, with per-variable ``max_length`` hoisted to the
    top-level ``max_lengths`` map the conformance suite reads.

    ``PluginManifest.from_dict`` does exactly this merge in core, so the
    suite ends up checking the bounds the page editor will actually use.
    """
    manifest = json.loads(_MANIFEST_PATH.read_text())
    max_lengths = dict(manifest.get("max_lengths") or {})
    for name, meta in (manifest.get("variables", {}).get("simple") or {}).items():
        if isinstance(meta, dict) and isinstance(meta.get("max_length"), int):
            max_lengths.setdefault(name, meta["max_length"])
    manifest["max_lengths"] = max_lengths
    return manifest


MANIFEST = _manifest()

CONFIG = {
    "latitude": 37.7749,
    "longitude": -122.4194,
    "refresh_seconds": 300,
    "enabled": True,
}


@pytest.fixture
def stubbed_astral():
    """Patch out every astral call so nothing reaches the network or the clock.

    The conformance suite renders the plugin a couple of dozen times, so the
    stubs have to hold for the whole test, not just one fetch.
    """
    tz = pytz.timezone("America/Los_Angeles")
    now = datetime.now(tz).replace(hour=10, minute=0, second=0, microsecond=0)
    events = {
        "sunrise": now.replace(hour=6, minute=30),
        "sunset": now.replace(hour=18, minute=30),
        "noon": now.replace(hour=12, minute=0),
    }

    def sun_for(observer, date, tzinfo):
        offset = timedelta(days=(date - now.date()).days)
        return {key: value + offset for key, value in events.items()}

    with patch('plugins.sun_art.Config') as config, \
            patch('plugins.sun_art.sun', side_effect=sun_for), \
            patch('plugins.sun_art.elevation', return_value=20.0), \
            patch('plugins.sun_art.azimuth', return_value=120.0), \
            patch('plugins.sun_art.LocationInfo', MagicMock()):
        config.GENERAL_TIMEZONE = "America/Los_Angeles"
        yield


def make_plugin() -> SunArtPlugin:
    """A fresh, configured plugin with the network already stubbed."""
    plugin = SunArtPlugin(MANIFEST)
    plugin.config = dict(CONFIG)
    return plugin


def test_renders_on_every_board_shape(stubbed_astral):
    report = assert_board_conformance(
        make_plugin,
        manifest=MANIFEST,
        # Sun Art fills the board, so every extra row is content: a taller
        # board must render more rows, never the same number letterboxed.
        strict_growth=True,
        require_note_array_preview=True,
    )
    # Every geometry must be filled edge to edge, not partly blank.
    for geometry in STANDARD_GEOMETRIES:
        assert report.rows_by_geometry[geometry.label] == geometry.rows, (
            f"{geometry.label} rendered "
            f"{report.rows_by_geometry[geometry.label]}/{geometry.rows} rows\n"
            + report.summary()
        )


@pytest.mark.parametrize("rows,cols", [
    (6, 22),    # flagship
    (3, 15),    # note
    (12, 30),   # 65" FiestaPanel
    (12, 15),   # 1 wide x 4 tall - NARROWER than a flagship
    (3, 120),   # 8 wide x 1 tall - wider but shorter
    (24, 120),  # the largest array the platform allows
])
def test_every_stage_fills_every_board(stubbed_astral, rows, cols):
    """Each stage covers the board exactly, measured in tiles not characters."""
    plugin = make_plugin()
    board = BoardContext("note_array", rows=rows, cols=cols)

    for stage in STAGE_STYLES:
        lines = plugin._pattern_to_lines(plugin._generate_pattern(stage, rows, cols))
        assert len(lines) == rows, f"{stage} at {cols}x{rows}: {len(lines)} rows"
        for index, line in enumerate(lines):
            assert count_tiles(line) == cols, (
                f"{stage} at {cols}x{rows}: row {index} is "
                f"{count_tiles(line)} tiles, board is {cols}"
            )

    with plugin._bound_board(board):
        result = plugin.fetch_data()
    assert result.available, result.error
    assert len(result.formatted_lines) == rows
    assert all(count_tiles(line) == cols for line in result.formatted_lines)


def test_art_is_not_reused_between_boards(stubbed_astral):
    """One instance serves every board, so the art must not be cached flat.

    The plugin keeps a cache of the sun *calculation*; if the rendered art
    ever went in there it would short-circuit ahead of PluginBase's
    geometry-keyed cache and hand a Note the Flagship's picture.
    """
    plugin = make_plugin()

    def art_for(rows, cols):
        board = BoardContext("note_array", rows=rows, cols=cols)
        return plugin.get_data(board).data["sun_art"]

    note = art_for(3, 15)
    panel = art_for(12, 30)
    # Re-render the first shape after the second: a geometry-blind cache
    # returns the panel's art here.
    assert art_for(3, 15) == note
    assert art_for(12, 30) == panel
    assert len(note.split("\n")) == 3
    assert len(panel.split("\n")) == 12


def test_preview_rows_match_what_the_code_renders(stubbed_astral):
    """Manifest previews must be output the renderer can actually produce.

    They were hand-drawn gradients before, advertising a reflow the code did
    not do. Regenerate them from ``_generate_pattern`` and they stay honest.
    """
    plugin = make_plugin()
    renderable = {
        (rows, cols): {
            "".join("{%d}" % code for code in row)
            for stage in STAGE_STYLES
            for row in plugin._generate_pattern(stage, rows, cols)
        }
        for rows, cols in [(6, 22), (3, 15), (6, 30)]
    }

    for preview in MANIFEST["previews"]:
        rows = preview["rows"]
        if preview["device_type"] == "note_array":
            shape = (preview["notes_tall"] * 3, preview["notes_wide"] * 15)
        elif preview["device_type"] == "note":
            shape = (3, 15)
        else:
            shape = (6, 22)
        assert len(rows) == shape[0], f"{preview['device_type']} preview height"
        for row in rows:
            assert count_tiles(row) == shape[1], (
                f"{preview['device_type']} preview row is {count_tiles(row)} "
                f"tiles, board is {shape[1]}"
            )
            assert row in renderable[shape], (
                f"{preview['device_type']} preview row is not something the "
                f"renderer produces: {row}"
            )


def test_declared_max_length_matches_the_largest_board(stubbed_astral):
    """``sun_art`` declares the whole board, so its bound is the whole board."""
    plugin = make_plugin()
    largest = BoardContext("note_array", rows=24, cols=120)
    with plugin._bound_board(largest):
        art = plugin.fetch_data().data["sun_art"]
    assert count_tiles(art) == MANIFEST["max_lengths"]["sun_art"]
