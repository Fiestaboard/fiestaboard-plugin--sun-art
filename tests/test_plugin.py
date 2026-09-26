"""Tests for sun_art plugin."""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
import pytz
import json
from pathlib import Path
import sys

# Ensure astral is mocked before import
if 'astral' not in sys.modules or not hasattr(sys.modules['astral'], 'LocationInfo'):
    mock_astral_module = MagicMock()
    mock_astral_module.LocationInfo = MagicMock()
    mock_astral_module.sun = MagicMock()
    mock_astral_module.sun.sun = MagicMock()
    mock_astral_module.sun.elevation = MagicMock()
    mock_astral_module.sun.azimuth = MagicMock()
    sys.modules['astral'] = mock_astral_module
    sys.modules['astral.sun'] = mock_astral_module.sun

from src.devices import BoardContext
from src.plugins.base import PluginResult
from src.board_chars import BoardChars
from plugins.sun_art import FALLBACK_BOARD, STAGE_STYLES, SunArtPlugin


class TestSunArtPlugin:
    """Test suite for SunArtPlugin."""
    
    def test_plugin_id(self, sample_manifest):
        """Test plugin ID matches directory name."""
        plugin = SunArtPlugin(sample_manifest)
        assert plugin.plugin_id == "sun_art"
    
    def test_validate_config_valid(self, sample_manifest):
        """Test config validation with valid config."""
        plugin = SunArtPlugin(sample_manifest)
        config = {
            "latitude": 37.7749,
            "longitude": -122.4194,
            "refresh_seconds": 300
        }
        errors = plugin.validate_config(config)
        assert len(errors) == 0
    
    def test_validate_config_missing_latitude(self, sample_manifest):
        """Test config validation detects missing latitude."""
        plugin = SunArtPlugin(sample_manifest)
        config = {"longitude": -122.4194}
        errors = plugin.validate_config(config)
        assert len(errors) > 0
        assert any("latitude" in e.lower() for e in errors)
    
    def test_validate_config_missing_longitude(self, sample_manifest):
        """Test config validation detects missing longitude."""
        plugin = SunArtPlugin(sample_manifest)
        config = {"latitude": 37.7749}
        errors = plugin.validate_config(config)
        assert len(errors) > 0
        assert any("longitude" in e.lower() for e in errors)
    
    def test_validate_config_invalid_latitude(self, sample_manifest):
        """Test config validation detects invalid latitude."""
        plugin = SunArtPlugin(sample_manifest)
        config = {"latitude": 100, "longitude": -122.4194}
        errors = plugin.validate_config(config)
        assert len(errors) > 0
        assert any("latitude" in e.lower() for e in errors)
    
    def test_validate_config_invalid_longitude(self, sample_manifest):
        """Test config validation detects invalid longitude."""
        plugin = SunArtPlugin(sample_manifest)
        config = {"latitude": 37.7749, "longitude": 200}
        errors = plugin.validate_config(config)
        assert len(errors) > 0
        assert any("longitude" in e.lower() for e in errors)
    
    def test_validate_config_invalid_refresh(self, sample_manifest):
        """Test config validation detects invalid refresh interval."""
        plugin = SunArtPlugin(sample_manifest)
        config = {
            "latitude": 37.7749,
            "longitude": -122.4194,
            "refresh_seconds": 30  # Below minimum
        }
        errors = plugin.validate_config(config)
        assert len(errors) > 0
        assert any("refresh" in e.lower() for e in errors)
    
    @patch('plugins.sun_art.Config')
    def test_fetch_data_missing_config(self, mock_config, sample_manifest):
        """Test error handling for missing config."""
        mock_config.GENERAL_TIMEZONE = "America/Los_Angeles"
        
        plugin = SunArtPlugin(sample_manifest)
        plugin.config = {}
        
        result = plugin.fetch_data()
        
        assert result.available is False
        assert "latitude" in result.error.lower() or "longitude" in result.error.lower()
    
    @patch('plugins.sun_art.Config')
    @patch('plugins.sun_art.elevation')
    @patch('plugins.sun_art.azimuth')
    @patch('plugins.sun_art.sun')
    def test_fetch_data_success(
        self, mock_sun, mock_azimuth, mock_elevation, mock_config, sample_manifest
    ):
        """Test successful data fetch."""
        mock_config.GENERAL_TIMEZONE = "America/Los_Angeles"
        
        # Mock sun events
        tz = pytz.timezone("America/Los_Angeles")
        now = datetime.now(tz)
        mock_sun.return_value = {
            "sunrise": now.replace(hour=6, minute=30),
            "sunset": now.replace(hour=18, minute=30),
            "noon": now.replace(hour=12, minute=0)
        }
        
        # Mock elevation and azimuth
        mock_elevation.return_value = 45.0  # High sun (noon)
        mock_azimuth.return_value = 180.0
        
        plugin = SunArtPlugin(sample_manifest)
        plugin.config = {
            "latitude": 37.7749,
            "longitude": -122.4194,
            "refresh_seconds": 300
        }
        
        result = plugin.fetch_data()
        
        assert result.available is True
        assert result.error is None
        assert "sun_art" in result.data
        assert "sun_art_array" in result.data
        assert "sun_stage" in result.data
        assert "sun_position" in result.data
        assert "is_daytime" in result.data
        assert "time_to_sunrise" in result.data
        assert "time_to_sunset" in result.data
    
    @patch('plugins.sun_art.Config')
    @patch('plugins.sun_art.elevation')
    @patch('plugins.sun_art.azimuth')
    @patch('plugins.sun_art.sun')
    def test_fetch_data_night_stage(
        self, mock_sun, mock_azimuth, mock_elevation, mock_config, sample_manifest
    ):
        """Test pattern generation for night stage."""
        mock_config.GENERAL_TIMEZONE = "America/Los_Angeles"
        
        tz = pytz.timezone("America/Los_Angeles")
        now = datetime.now(tz)
        mock_sun.return_value = {
            "sunrise": now.replace(hour=6, minute=30),
            "sunset": now.replace(hour=18, minute=30),
            "noon": now.replace(hour=12, minute=0)
        }
        
        # Night: elevation below -12
        mock_elevation.return_value = -15.0
        mock_azimuth.return_value = 0.0
        
        plugin = SunArtPlugin(sample_manifest)
        plugin.config = {
            "latitude": 37.7749,
            "longitude": -122.4194
        }
        
        result = plugin.fetch_data()
        
        assert result.available is True
        assert result.data["sun_stage"] == "night"
        assert result.data["is_daytime"] is False
        assert "sun_art" in result.data
        assert len(result.data["sun_art"].split("\n")) == 6
    
    @patch('plugins.sun_art.Config')
    @patch('plugins.sun_art.elevation')
    @patch('plugins.sun_art.azimuth')
    @patch('plugins.sun_art.sun')
    def test_fetch_data_noon_stage(
        self, mock_sun, mock_azimuth, mock_elevation, mock_config, sample_manifest
    ):
        """Test pattern generation for noon stage."""
        mock_config.GENERAL_TIMEZONE = "America/Los_Angeles"
        
        tz = pytz.timezone("America/Los_Angeles")
        now = datetime.now(tz)
        mock_sun.return_value = {
            "sunrise": now.replace(hour=6, minute=30),
            "sunset": now.replace(hour=18, minute=30),
            "noon": now.replace(hour=12, minute=0)
        }
        
        # Noon: high elevation
        mock_elevation.return_value = 60.0
        mock_azimuth.return_value = 180.0
        
        plugin = SunArtPlugin(sample_manifest)
        plugin.config = {
            "latitude": 37.7749,
            "longitude": -122.4194
        }
        
        result = plugin.fetch_data()
        
        assert result.available is True
        assert result.data["sun_stage"] == "noon"
        assert result.data["is_daytime"] is True
        assert result.data["sun_position"] > 0
    
    def test_determine_sun_stage_night(self, sample_manifest):
        """Test sun stage determination for night (elevation < -12)."""
        plugin = SunArtPlugin(sample_manifest)
        # Both rising and setting should return night when very low
        stage = plugin._determine_sun_stage(-15.0, False)
        assert stage == "night"
        stage = plugin._determine_sun_stage(-15.0, True)
        assert stage == "night"
    
    def test_determine_sun_stage_late_night(self, sample_manifest):
        """Test sun stage determination for late_night (-12 to -6, rising)."""
        plugin = SunArtPlugin(sample_manifest)
        stage = plugin._determine_sun_stage(-9.0, True)
        assert stage == "late_night"
    
    def test_determine_sun_stage_twilight(self, sample_manifest):
        """Test sun stage determination for twilight (-12 to -6, setting)."""
        plugin = SunArtPlugin(sample_manifest)
        stage = plugin._determine_sun_stage(-9.0, False)
        assert stage == "twilight"
    
    def test_determine_sun_stage_dawn(self, sample_manifest):
        """Test sun stage determination for dawn (-6 to -1, rising)."""
        plugin = SunArtPlugin(sample_manifest)
        stage = plugin._determine_sun_stage(-3.0, True)
        assert stage == "dawn"
    
    def test_determine_sun_stage_dusk(self, sample_manifest):
        """Test sun stage determination for dusk (-6 to -1, setting)."""
        plugin = SunArtPlugin(sample_manifest)
        stage = plugin._determine_sun_stage(-3.0, False)
        assert stage == "dusk"
    
    def test_determine_sun_stage_early_sunrise(self, sample_manifest):
        """Test sun stage determination for early_sunrise (-1 to 3, rising)."""
        plugin = SunArtPlugin(sample_manifest)
        stage = plugin._determine_sun_stage(1.0, True)
        assert stage == "early_sunrise"
    
    def test_determine_sun_stage_late_sunset(self, sample_manifest):
        """Test sun stage determination for late_sunset (-1 to 3, setting)."""
        plugin = SunArtPlugin(sample_manifest)
        stage = plugin._determine_sun_stage(1.0, False)
        assert stage == "late_sunset"
    
    def test_determine_sun_stage_sunrise(self, sample_manifest):
        """Test sun stage determination for sunrise (3 to 10, rising)."""
        plugin = SunArtPlugin(sample_manifest)
        stage = plugin._determine_sun_stage(6.0, True)
        assert stage == "sunrise"
    
    def test_determine_sun_stage_sunset(self, sample_manifest):
        """Test sun stage determination for sunset (3 to 10, setting)."""
        plugin = SunArtPlugin(sample_manifest)
        stage = plugin._determine_sun_stage(6.0, False)
        assert stage == "sunset"
    
    def test_determine_sun_stage_morning(self, sample_manifest):
        """Test sun stage determination for morning (10 to 30, rising)."""
        plugin = SunArtPlugin(sample_manifest)
        stage = plugin._determine_sun_stage(20.0, True)
        assert stage == "morning"
    
    def test_determine_sun_stage_afternoon(self, sample_manifest):
        """Test sun stage determination for afternoon (10 to 30, setting)."""
        plugin = SunArtPlugin(sample_manifest)
        stage = plugin._determine_sun_stage(20.0, False)
        assert stage == "afternoon"
    
    def test_determine_sun_stage_noon(self, sample_manifest):
        """Test sun stage determination for noon (>30)."""
        plugin = SunArtPlugin(sample_manifest)
        stage = plugin._determine_sun_stage(45.0, False)
        assert stage == "noon"
    
    def test_generate_pattern_dimensions(self, sample_manifest):
        """Every stage fills exactly the geometry it is asked for."""
        plugin = SunArtPlugin(sample_manifest)

        shapes = [(6, 22), (3, 15), (12, 30), (12, 15), (3, 120), (24, 120)]
        for stage in STAGE_STYLES:
            for rows, cols in shapes:
                pattern = plugin._generate_pattern(stage, rows, cols)
                assert len(pattern) == rows, f"{stage} at {cols}x{rows} has {len(pattern)} rows"
                for row in pattern:
                    assert len(row) == cols, f"{stage} at {cols}x{rows} has {len(row)} columns"

    def test_generate_pattern_has_no_dimension_literals(self, sample_manifest):
        """A taller board must be re-rendered, not padded or tiled.

        A Flagship scene stamped twice into a 22x12 board would repeat the
        horizon; a letterboxed one would leave half the board blank. Checking
        that the horizon lands at a *different* row index rules out both.
        """
        plugin = SunArtPlugin(sample_manifest)

        def horizon_row(rows, cols):
            grid = plugin._generate_pattern("morning", rows, cols)
            # The ground band starts where the left-hand edge stops being sky.
            sky = grid[0][0]
            return next(r for r, row in enumerate(grid) if row[0] != sky)

        assert horizon_row(6, 22) == 3
        assert horizon_row(12, 22) == 6
        assert horizon_row(24, 22) == 12

    def test_board_size_defaults_to_flagship(self, sample_manifest):
        """No bound board is a supported state, not a crash."""
        plugin = SunArtPlugin(sample_manifest)
        assert plugin.board is None
        assert plugin.board_size() == (FALLBACK_BOARD.rows, FALLBACK_BOARD.cols)

    def test_board_size_follows_bound_board(self, sample_manifest):
        plugin = SunArtPlugin(sample_manifest)
        with plugin._bound_board(BoardContext("note_array", rows=12, cols=30)):
            assert plugin.board_size() == (12, 30)
    
    def test_pattern_to_string(self, sample_manifest):
        """Test pattern to string conversion."""
        plugin = SunArtPlugin(sample_manifest)
        pattern = plugin._generate_pattern("noon", 6, 22)
        pattern_str = plugin._pattern_to_string(pattern)
        
        assert isinstance(pattern_str, str)
        lines = pattern_str.split("\n")
        assert len(lines) == 6
    
    def test_data_variables_match_manifest(self, sample_manifest):
        """Test that returned data includes variables declared in manifest."""
        # Load manifest
        manifest_path = Path(__file__).parent.parent / "manifest.json"
        with open(manifest_path) as f:
            manifest = json.load(f)
        
        declared_vars = manifest["variables"]["simple"]
        
        # Mock fetch_data to return sample data
        with patch('plugins.sun_art.Config') as mock_config, \
             patch('plugins.sun_art.elevation') as mock_elevation, \
             patch('plugins.sun_art.azimuth') as mock_azimuth, \
             patch('plugins.sun_art.sun') as mock_sun:
            
            mock_config.GENERAL_TIMEZONE = "America/Los_Angeles"
            tz = pytz.timezone("America/Los_Angeles")
            now = datetime.now(tz)
            mock_sun.return_value = {
                "sunrise": now.replace(hour=6, minute=30),
                "sunset": now.replace(hour=18, minute=30),
                "noon": now.replace(hour=12, minute=0)
            }
            mock_elevation.return_value = 45.0
            mock_azimuth.return_value = 180.0
            
            plugin = SunArtPlugin(sample_manifest)
            plugin.config = {
                "latitude": 37.7749,
                "longitude": -122.4194
            }
            
            result = plugin.fetch_data()
            
            if result.available:
                for var in declared_vars:
                    assert var in result.data, f"Variable '{var}' declared in manifest but not in data"


class TestSunArtEdgeCases:
    """Edge case tests for sun art plugin."""
    
    @patch('plugins.sun_art.Config')
    def test_fetch_data_exception_handling(self, mock_config, sample_manifest):
        """Test exception handling during data fetch."""
        mock_config.GENERAL_TIMEZONE = "America/Los_Angeles"
        
        plugin = SunArtPlugin(sample_manifest)
        plugin.config = {
            "latitude": 37.7749,
            "longitude": -122.4194
        }
        
        # Force an exception
        with patch('plugins.sun_art.LocationInfo', side_effect=Exception("Test error")):
            result = plugin.fetch_data()
            assert result.available is False
            assert result.error is not None
    
    def test_generate_pattern_all_stages(self, sample_manifest):
        """Test pattern generation for all sun stages."""
        plugin = SunArtPlugin(sample_manifest)
        
        for stage in STAGE_STYLES:
            pattern = plugin._generate_pattern(stage, 6, 22)
            # Verify pattern is valid (board-sized, all codes are valid)
            assert len(pattern) == 6
            for row in pattern:
                assert len(row) == 22
                for code in row:
                    assert 0 <= code <= 71, f"Invalid character code {code} in {stage} pattern"

    def test_unknown_stage_falls_back_to_night(self, sample_manifest):
        plugin = SunArtPlugin(sample_manifest)
        assert plugin._generate_pattern("not-a-stage", 6, 22) == plugin._generate_pattern("night", 6, 22)
    
    @patch('plugins.sun_art.Config')
    @patch('plugins.sun_art.elevation')
    @patch('plugins.sun_art.azimuth')
    @patch('plugins.sun_art.sun')
    def test_fetch_data_cache_hit(
        self, mock_sun, mock_azimuth, mock_elevation, mock_config, sample_manifest
    ):
        """Test that cached data is returned when cache is valid."""
        mock_config.GENERAL_TIMEZONE = "America/Los_Angeles"
        
        tz = pytz.timezone("America/Los_Angeles")
        now = datetime.now(tz)
        mock_sun.return_value = {
            "sunrise": now.replace(hour=6, minute=30),
            "sunset": now.replace(hour=18, minute=30),
            "noon": now.replace(hour=12, minute=0)
        }
        mock_elevation.return_value = 45.0
        mock_azimuth.return_value = 180.0
        
        plugin = SunArtPlugin(sample_manifest)
        plugin.config = {
            "latitude": 37.7749,
            "longitude": -122.4194,
            "refresh_seconds": 300
        }
        
        # First fetch - should calculate
        result1 = plugin.fetch_data()
        assert result1.available is True
        assert plugin._sun_cache is not None
        assert "sun_art" not in plugin._sun_cache, (
            "the plugin's own cache must hold no rendered output: it is not "
            "keyed by geometry and would serve one board's art to another"
        )

        # Second fetch within refresh interval - should use cache
        result2 = plugin.fetch_data()
        assert result2.available is True
        # Verify cache was used (sun calculation should not be called again)
        assert mock_elevation.call_count == 1  # Only called once (first fetch)
    
    @patch('plugins.sun_art.Config')
    @patch('plugins.sun_art.elevation')
    @patch('plugins.sun_art.azimuth')
    @patch('plugins.sun_art.sun')
    def test_fetch_data_config_change_invalidates_cache(
        self, mock_sun, mock_azimuth, mock_elevation, mock_config, sample_manifest
    ):
        """Test a config change drops the cache instead of serving the old location."""
        mock_config.GENERAL_TIMEZONE = "America/Los_Angeles"
        
        tz = pytz.timezone("America/Los_Angeles")
        now = datetime.now(tz)
        mock_sun.return_value = {
            "sunrise": now.replace(hour=6, minute=30),
            "sunset": now.replace(hour=18, minute=30),
            "noon": now.replace(hour=12, minute=0)
        }
        mock_elevation.return_value = 45.0
        mock_azimuth.return_value = 180.0
        
        plugin = SunArtPlugin(sample_manifest)
        plugin.config = {
            "latitude": 37.7749,
            "longitude": -122.4194,
            "refresh_seconds": 300
        }
        
        result1 = plugin.fetch_data()
        assert result1.available is True
        assert plugin._sun_cache is not None

        # Same cache window, different location: the cached position is now wrong
        plugin.config = {
            "latitude": 40.7128,
            "longitude": -74.0060,
            "refresh_seconds": 300
        }
        assert plugin._sun_cache is None

        result2 = plugin.fetch_data()
        assert result2.available is True
        assert mock_elevation.call_count == 2  # Recalculated for the new location
    
    @patch('plugins.sun_art.Config')
    @patch('plugins.sun_art.elevation')
    @patch('plugins.sun_art.azimuth')
    @patch('plugins.sun_art.sun')
    def test_calculate_sun_position_during_day(
        self, mock_sun, mock_azimuth, mock_elevation, mock_config, sample_manifest
    ):
        """Test sun position calculation during daytime (between sunrise and sunset, before noon)."""
        mock_config.GENERAL_TIMEZONE = "America/Los_Angeles"
        
        tz = pytz.timezone("America/Los_Angeles")
        # Set time to 10 AM (after sunrise, before noon)
        now = datetime.now(tz).replace(hour=10, minute=0, second=0, microsecond=0)
        sunrise = now.replace(hour=6, minute=30)
        sunset = now.replace(hour=18, minute=30)
        noon = now.replace(hour=12, minute=0)
        
        mock_sun.return_value = {
            "sunrise": sunrise,
            "sunset": sunset,
            "noon": noon
        }
        mock_elevation.return_value = 30.0
        mock_azimuth.return_value = 120.0
        
        plugin = SunArtPlugin(sample_manifest)
        plugin.config = {
            "latitude": 37.7749,
            "longitude": -122.4194
        }
        
        sun_data = plugin._calculate_sun_position(37.7749, -122.4194, now, tz)
        
        assert sun_data["elevation"] == 30.0
        assert sun_data["azimuth"] == 120.0
        assert sun_data["is_rising"] is True  # Before noon, so rising
    
    @patch('plugins.sun_art.Config')
    @patch('plugins.sun_art.elevation')
    @patch('plugins.sun_art.azimuth')
    @patch('plugins.sun_art.sun')
    def test_calculate_sun_position_after_sunset(
        self, mock_sun, mock_azimuth, mock_elevation, mock_config, sample_manifest
    ):
        """Test sun position calculation after sunset (is_rising = False)."""
        mock_config.GENERAL_TIMEZONE = "America/Los_Angeles"
        
        tz = pytz.timezone("America/Los_Angeles")
        # Set time to 8 PM (after sunset)
        now = datetime.now(tz).replace(hour=20, minute=0, second=0, microsecond=0)
        sunrise = now.replace(hour=6, minute=30)
        sunset = now.replace(hour=18, minute=30)
        noon = now.replace(hour=12, minute=0)
        
        mock_sun.return_value = {
            "sunrise": sunrise,
            "sunset": sunset,
            "noon": noon
        }
        mock_elevation.return_value = -10.0
        mock_azimuth.return_value = 0.0
        
        plugin = SunArtPlugin(sample_manifest)
        plugin.config = {
            "latitude": 37.7749,
            "longitude": -122.4194
        }
        
        sun_data = plugin._calculate_sun_position(37.7749, -122.4194, now, tz)
        
        assert sun_data["elevation"] == -10.0
        assert sun_data["is_rising"] is False  # After sunset, so setting
    
    def test_pattern_to_string_with_spaces(self, sample_manifest):
        """Test pattern to string conversion includes space characters."""
        plugin = SunArtPlugin(sample_manifest)
        # Create a pattern with spaces
        pattern = [[BoardChars.SPACE] * 22 for _ in range(6)]
        pattern[0][0] = BoardChars.YELLOW
        pattern[0][1] = BoardChars.SPACE
        pattern[0][2] = BoardChars.O
        
        pattern_str = plugin._pattern_to_string(pattern)
        lines = pattern_str.split("\n")
        assert len(lines) == 6
        # First line should have yellow, space, and O
        assert "{yellow}" in lines[0]
        assert "O" in lines[0]
    
    def test_code_to_char_numbers(self, sample_manifest):
        """Test _code_to_char with number codes."""
        plugin = SunArtPlugin(sample_manifest)
        
        # Test numbers 1-9 (codes 27-35)
        assert plugin._code_to_char(27) == "1"
        assert plugin._code_to_char(28) == "2"
        assert plugin._code_to_char(35) == "9"
        
        # Test zero (code 36)
        assert plugin._code_to_char(36) == "0"
        
        # Test O character (code 15) - this should hit the elif BoardChars.O branch
        o_code = BoardChars.O
        assert o_code == 15, "BoardChars.O should be code 15"
        result = plugin._code_to_char(o_code)
        assert result == "O", f"Expected 'O' for code {o_code}, got '{result}'"
        
        # Test else branch (unknown code)
        assert plugin._code_to_char(999) == " "
    
    @patch('plugins.sun_art.Config')
    @patch('plugins.sun_art.sun')
    def test_calculate_next_events_after_today(
        self, mock_sun, mock_config, sample_manifest
    ):
        """Test _calculate_next_events when current time is after today's sunrise/sunset."""
        mock_config.GENERAL_TIMEZONE = "America/Los_Angeles"
        
        tz = pytz.timezone("America/Los_Angeles")
        # Set time to 8 PM (after sunset)
        now = datetime.now(tz).replace(hour=20, minute=0, second=0, microsecond=0)
        
        today = now.date()
        tomorrow = today + timedelta(days=1)
        
        sunrise_today = now.replace(hour=6, minute=30)
        sunset_today = now.replace(hour=18, minute=30)
        sunrise_tomorrow = now.replace(hour=6, minute=30) + timedelta(days=1)
        sunset_tomorrow = now.replace(hour=18, minute=30) + timedelta(days=1)
        
        def sun_side_effect(observer, date, tzinfo):
            if date == today:
                return {
                    "sunrise": sunrise_today,
                    "sunset": sunset_today,
                    "noon": now.replace(hour=12, minute=0)
                }
            else:
                return {
                    "sunrise": sunrise_tomorrow,
                    "sunset": sunset_tomorrow,
                    "noon": now.replace(hour=12, minute=0) + timedelta(days=1)
                }
        
        mock_sun.side_effect = sun_side_effect
        
        plugin = SunArtPlugin(sample_manifest)
        time_to_sunrise, time_to_sunset = plugin._calculate_next_events(
            37.7749, -122.4194, now, tz
        )
        
        # Should return time to tomorrow's sunrise (since we're after today's)
        assert time_to_sunrise is not None
        assert time_to_sunset is not None
        # Format should be HH:MM
        assert ":" in time_to_sunrise
        assert ":" in time_to_sunset


class TestManifestMetadata:
    """Tests for the rich metadata format in the manifest."""

    def test_manifest_uses_dict_simple_format(self):
        manifest_path = Path(__file__).parent.parent / "manifest.json"
        with open(manifest_path) as f:
            manifest = json.load(f)
        simple = manifest["variables"]["simple"]
        assert isinstance(simple, dict)

    def test_all_variables_have_descriptions(self):
        manifest_path = Path(__file__).parent.parent / "manifest.json"
        with open(manifest_path) as f:
            manifest = json.load(f)
        for var_name, meta in manifest["variables"]["simple"].items():
            assert "description" in meta and meta["description"], f"Variable '{var_name}' missing description"

    def test_all_variables_have_valid_groups(self):
        manifest_path = Path(__file__).parent.parent / "manifest.json"
        with open(manifest_path) as f:
            manifest = json.load(f)
        groups = set(manifest["variables"].get("groups", {}).keys())
        for var_name, meta in manifest["variables"]["simple"].items():
            group = meta.get("group", "")
            if group:
                assert group in groups, f"Variable '{var_name}' references undefined group '{group}'"

    def test_groups_are_defined(self):
        manifest_path = Path(__file__).parent.parent / "manifest.json"
        with open(manifest_path) as f:
            manifest = json.load(f)
        groups = manifest["variables"].get("groups", {})
        assert len(groups) > 0
        for group_id, group_def in groups.items():
            assert "label" in group_def, f"Group '{group_id}' missing label"
