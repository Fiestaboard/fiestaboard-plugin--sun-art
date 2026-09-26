"""Sun Art plugin for FiestaBoard.

Paints the whole board with a sun scene that follows the sun's real position
through the day: a sky, a horizon, a ground band and a sun disc.

The scene is **re-rendered procedurally at the board's native geometry** on
every fetch. The composition is horizontal bands plus a disc, which is
scale-free: the horizon is a fraction of ``board.rows``, the disc is centred
on ``board.cols`` with a radius derived from ``board.rows`` and the tile
aspect, and star density comes from the board's area. Nothing is tiled, letterboxed
or truncated, so a Note (15x3), a Flagship (22x6) and a 120x24 panel each get
a whole, correctly-proportioned scene rather than a crop or a mosaic.
"""

from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import math
import random
import pytz
from astral import LocationInfo
from astral.sun import sun, elevation, azimuth

from src.devices import BoardContext
from src.plugins.base import PluginBase, PluginResult
from src.config import Config
from src.board_chars import BoardChars

logger = logging.getLogger(__name__)

# The board assumed when no board is bound (unit tests, legacy callers).
# Taken from core rather than retyped so there is no dimension literal here.
FALLBACK_BOARD = BoardContext.from_device_type("flagship")

# A split-flap tile is roughly twice as tall as it is wide, so a disc that
# reads as round on the board spans about twice as many columns as rows.
SUN_ASPECT = 2.0

# Stars per tile of sky. Calibrated so a Flagship (22x6 = 132 tiles) gets the
# six stars the hand-drawn night scene had; every other board scales by area.
STAR_DENSITY = 6 / 132

# How far the glow reaches past the disc edge, as a multiple of its radius.
GLOW_SPREAD = 1.25

# The bright core sits inside the disc (noon's white centre).
CORE_SPREAD = 0.75

# The widest the disc may get, as a fraction of the board width. A Flagship's
# sun is about two thirds of the board across; this keeps that proportion on
# boards whose aspect ratio would otherwise let a round disc swallow them.
SUN_MAX_WIDTH_SHARE = 0.32

# Sun stage elevation thresholds (in degrees)
STAGE_THRESHOLDS = {
    "night": (-90, -6),      # Below horizon, no twilight
    "dusk": (-6, 0),         # Civil twilight (sun below horizon but sky still lit)
    "sunset": (0, 5),        # Sun setting (0° to 5°)
    "afternoon": (5, 30),    # Afternoon sun (5° to 30°)
    "noon": (30, 90),        # High sun (30° to 90°)
    "morning": (5, 30),      # Morning sun (5° to 30°)
    "sunrise": (0, 5),       # Sun rising (0° to 5°)
    "dawn": (-6, 0),         # Civil dawn (sun below horizon but sky lighting)
}

# Colour shortcuts, used by the stage table below.
_K = BoardChars.BLACK   # 70
_R = BoardChars.RED     # 63
_O = BoardChars.ORANGE  # 64
_Y = BoardChars.YELLOW  # 65
_B = BoardChars.BLUE    # 67
_V = BoardChars.VIOLET  # 68
_W = BoardChars.WHITE   # 69


@dataclass(frozen=True)
class StageStyle:
    """How one sun stage is painted, in board-relative terms only.

    Every field is a fraction or a colour — there is not a single tile count
    here, which is what lets the same stage render at 15x3 and at 120x24.

    Attributes:
        sky: Colour bands from the top of the board down to the horizon.
        ground: Colour bands from the horizon down to the bottom.
        horizon: Row where the ground starts, as a fraction of ``rows``.
        sun_offset: Disc centre relative to the horizon, measured in disc
            radii — negative is above the horizon, positive below it. Anchored
            to the horizon rather than to the board so the sun keeps the same
            relationship to the scene on a 3-row Note and a 24-row panel;
            anchoring it to ``rows`` instead makes a tall board push the disc
            off the bottom entirely. ``None`` means no sun at all.
        radius: Disc radius as a fraction of the board's fitting dimension.
        disc: Disc colour, or ``None`` for a glow with no visible disc.
        core: Brighter inner colour, or ``None``.
        glow: Colour of the halo around the disc, or ``None``.
        stars: Whether to scatter stars through the sky band.
    """

    sky: Tuple[int, ...]
    ground: Tuple[int, ...]
    horizon: float
    sun_offset: Optional[float] = None
    radius: float = 0.0
    disc: Optional[int] = None
    core: Optional[int] = None
    glow: Optional[int] = None
    stars: bool = False


# The twelve stages. These reproduce the identity of the hand-drawn 6x22
# originals — night's scattered stars, dawn's violet sky over an orange
# horizon, noon's white-cored sun in a blue sky, dusk's red-to-violet
# gradient with the sun sinking out of frame — as proportions rather than
# as pixels, so they hold at any board size.
STAGE_STYLES: Dict[str, StageStyle] = {
    # 1. NIGHT: black sky, white stars scattered, no horizon.
    "night": StageStyle(sky=(_K,), ground=(_K,), horizon=1.0, stars=True),

    # 2. LATE_NIGHT: stars still out, a violet band rising with an orange
    #    glow where the sun is still well below the horizon.
    "late_night": StageStyle(
        sky=(_K,), ground=(_V,), horizon=0.67,
        sun_offset=1.55, radius=0.50, disc=None, glow=_O, stars=True,
    ),

    # 3. DAWN: violet sky, orange ground, the disc's cap just breaking through.
    "dawn": StageStyle(
        sky=(_V,), ground=(_O,), horizon=0.62,
        sun_offset=1.00, radius=0.55, disc=_Y, glow=_O,
    ),

    # 4. EARLY_SUNRISE: sun peeking over a low horizon, violet sky above orange.
    "early_sunrise": StageStyle(
        sky=(_V,), ground=(_O,), horizon=0.34,
        sun_offset=1.35, radius=0.55, disc=_Y, glow=_O,
    ),

    # 5. SUNRISE: blue sky arriving, a big disc clearing the horizon.
    "sunrise": StageStyle(
        sky=(_B,), ground=(_O,), horizon=0.34,
        sun_offset=0.60, radius=0.68, disc=_Y, glow=_O,
    ),

    # 6. MORNING: blue sky, orange ground, sun half way up.
    "morning": StageStyle(
        sky=(_B,), ground=(_O,), horizon=0.5,
        sun_offset=0.00, radius=0.58, disc=_Y, glow=_O,
    ),

    # 7. NOON: brightest — white core inside a yellow disc, high in a blue sky.
    "noon": StageStyle(
        sky=(_B,), ground=(_O,), horizon=0.5,
        sun_offset=-0.15, radius=0.62, disc=_Y, core=_W,
    ),

    # 8. AFTERNOON: the mirror of morning.
    "afternoon": StageStyle(
        sky=(_B,), ground=(_O,), horizon=0.5,
        sun_offset=0.00, radius=0.58, disc=_Y, glow=_O,
    ),

    # 9. SUNSET: orange sky deepening to red, violet creeping in at the bottom.
    "sunset": StageStyle(
        sky=(_O, _R), ground=(_O, _V), horizon=0.67,
        sun_offset=0.00, radius=0.58, disc=_Y, glow=_O,
    ),

    # 10. LATE_SUNSET: red over orange over violet, sun nearly gone.
    "late_sunset": StageStyle(
        sky=(_R, _O, _V), ground=(_V,), horizon=0.5,
        sun_offset=1.35, radius=0.55, disc=_Y, glow=_O,
    ),

    # 11. DUSK: the same palette, one step further down.
    "dusk": StageStyle(
        sky=(_R, _O, _V), ground=(_V,), horizon=0.5,
        sun_offset=1.35, radius=0.55, disc=_Y, glow=_O,
    ),

    # 12. TWILIGHT: stars returning above a violet band, last glow at the edge.
    "twilight": StageStyle(
        sky=(_K,), ground=(_V,), horizon=0.34,
        sun_offset=1.95, radius=0.50, disc=_Y, glow=_O, stars=True,
    ),
}


class SunArtPlugin(PluginBase):
    """Sun art plugin.

    Renders a full-board sun scene at whatever geometry the board reports,
    from a Note (15x3) to a 120x24 note array. The scene is re-rendered for
    each board rather than scaled, tiled or cropped.
    """

    def __init__(self, manifest: Dict[str, Any]):
        """Initialize the sun art plugin."""
        super().__init__(manifest)
        # Holds the *sun calculation* only — stage, elevation, event times.
        # Deliberately never the rendered art: rendering is board-specific,
        # and a rendered frame cached here would short-circuit ahead of
        # PluginBase's geometry-keyed cache and serve one board's picture to
        # another. Everything in here is geometry-independent by construction.
        self._sun_cache: Optional[Dict[str, Any]] = None
        self._sun_cache_date: Optional[str] = None

    @property
    def plugin_id(self) -> str:
        return "sun_art"

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        """Validate sun art configuration."""
        errors = []

        lat = config.get("latitude")
        lon = config.get("longitude")

        if lat is None:
            errors.append("Latitude is required")
        elif not isinstance(lat, (int, float)) or not (-90 <= lat <= 90):
            errors.append("Latitude must be a number between -90 and 90")

        if lon is None:
            errors.append("Longitude is required")
        elif not isinstance(lon, (int, float)) or not (-180 <= lon <= 180):
            errors.append("Longitude must be a number between -180 and 180")

        refresh_seconds = config.get("refresh_seconds", 300)
        if not isinstance(refresh_seconds, int) or refresh_seconds < 60:
            errors.append("Refresh interval must be at least 60 seconds")

        return errors

    def on_config_change(self, old_config: Dict[str, Any], new_config: Dict[str, Any]) -> None:
        """Drop the cached sun calculation so a config change takes effect now.

        The cache is keyed only on the date and its age, so without this a
        change to `latitude`/`longitude` would keep serving the position
        calculated for the old location for up to refresh_seconds.
        """
        self._sun_cache = None
        self._sun_cache_date = None
        logger.debug("Cleared cached sun position after config change")

    def board_size(self) -> Tuple[int, int]:
        """The bound board's ``(rows, cols)``, defaulting to a Flagship.

        ``self.board`` is ``None`` outside a board-scoped render, which is a
        supported state rather than an error.
        """
        board = self.board
        if board is None:
            return FALLBACK_BOARD.rows, FALLBACK_BOARD.cols
        return board.rows, board.cols

    def fetch_data(self) -> PluginResult:
        """Fetch sun art data and render the scene for the bound board."""
        lat = self.config.get("latitude")
        lon = self.config.get("longitude")

        if lat is None or lon is None:
            return PluginResult(
                available=False,
                error="Latitude and longitude are required"
            )

        try:
            # Get timezone from general settings
            timezone_str = Config.GENERAL_TIMEZONE
            tz = pytz.timezone(timezone_str)
            now = datetime.now(tz)

            state = self._sun_state(lat, lon, now, tz)

            # Render for THIS board. Never cached: two boards share the sun
            # but not the picture of it.
            rows, cols = self.board_size()
            pattern_array = self._generate_pattern(state["sun_stage"], rows, cols)
            lines = self._pattern_to_lines(pattern_array)

            data = {
                "sun_art": "\n".join(lines),
                "sun_art_array": pattern_array,  # Note: This will be serialized as JSON
                "sun_stage": state["sun_stage"],
                "sun_position": state["sun_position"],
                "is_daytime": state["is_daytime"],
                "time_to_sunrise": state["time_to_sunrise"],
                "time_to_sunset": state["time_to_sunset"],
            }

            return PluginResult(
                available=True,
                data=data,
                formatted_lines=lines,
            )

        except Exception as e:
            logger.exception("Error fetching sun art data")
            return PluginResult(
                available=False,
                error=str(e)
            )

    def get_formatted_display(self) -> Optional[List[str]]:
        """Whole-board scene for the "single plugin" page type.

        Sized from the bound board like every other render path, so this hook
        is held to the same bounds as ``formatted_lines``.
        """
        result = self.get_data(self.board)
        if not result.available:
            return None
        return result.formatted_lines

    def _sun_state(
        self, lat: float, lon: float, now: datetime, tz: pytz.BaseTzInfo
    ) -> Dict[str, Any]:
        """Current sun stage and event times, recalculated at most once per
        refresh interval.

        Everything returned is board-independent, which is what makes caching
        it safe across geometries.
        """
        today = now.strftime("%Y-%m-%d")
        if self._sun_cache and self._sun_cache_date == today:
            refresh_seconds = self.config.get("refresh_seconds", 300)
            cache_age = (now - self._sun_cache.get("calculated_at", now)).total_seconds()
            if cache_age < refresh_seconds:
                logger.debug(f"Using cached sun position (age: {cache_age:.0f}s)")
                return self._sun_cache

        sun_data = self._calculate_sun_position(lat, lon, now, tz)
        sun_stage = self._determine_sun_stage(
            sun_data["elevation"],
            sun_data["is_rising"]
        )
        time_to_sunrise, time_to_sunset = self._calculate_next_events(lat, lon, now, tz)

        state = {
            "sun_stage": sun_stage,
            "sun_position": round(sun_data["elevation"], 1),
            "is_daytime": sun_data["elevation"] > 0,
            "time_to_sunrise": time_to_sunrise,
            "time_to_sunset": time_to_sunset,
            "calculated_at": now,
        }

        self._sun_cache = state
        self._sun_cache_date = today
        return state

    def _calculate_sun_position(
        self, lat: float, lon: float, dt: datetime, tz: pytz.BaseTzInfo
    ) -> Dict[str, Any]:
        """Calculate current sun position.

        Args:
            lat: Latitude
            lon: Longitude
            dt: Current datetime (timezone-aware)
            tz: Timezone object

        Returns:
            Dictionary with elevation, azimuth, and is_rising flag
        """
        # Create location info
        location = LocationInfo(
            name="Location",
            region="Region",
            timezone=tz.zone,
            latitude=lat,
            longitude=lon
        )

        # Get sun data for today
        s = sun(location.observer, date=dt.date(), tzinfo=tz)

        # Calculate elevation and azimuth angles
        sun_elevation = elevation(location.observer, dt)
        sun_azimuth = azimuth(location.observer, dt)

        # Determine if sun is rising or setting
        # Compare current time to sunrise and sunset
        sunrise = s["sunrise"]
        sunset = s["sunset"]

        # If before sunrise or after sunset, sun is below horizon
        if dt < sunrise:
            # Before sunrise - sun is rising (but below horizon)
            is_rising = True
        elif dt > sunset:
            # After sunset - sun is setting (below horizon)
            is_rising = False
        else:
            # During day - determine if before or after solar noon
            noon = s["noon"]
            is_rising = dt < noon

        return {
            "elevation": sun_elevation,
            "azimuth": sun_azimuth,
            "is_rising": is_rising,
            "sunrise": sunrise,
            "sunset": sunset,
            "noon": s["noon"],
        }

    def _determine_sun_stage(self, elevation: float, is_rising: bool) -> str:
        """Determine current sun stage based on elevation and direction.

        Uses 12 stages for smooth visual progression:
        Rising: night -> late_night -> dawn -> early_sunrise -> sunrise -> morning -> noon
        Setting: noon -> afternoon -> sunset -> late_sunset -> dusk -> twilight -> night

        Args:
            elevation: Sun elevation angle in degrees
            is_rising: True if sun is rising, False if setting

        Returns:
            Sun stage name (one of the 12 stages)
        """
        if is_rising:
            # Sun is rising (before solar noon)
            if elevation < -12:
                return "night"
            elif elevation < -6:
                return "late_night"
            elif elevation < -1:
                return "dawn"
            elif elevation < 3:
                return "early_sunrise"
            elif elevation < 10:
                return "sunrise"
            elif elevation < 30:
                return "morning"
            else:
                return "noon"
        else:
            # Sun is setting (after solar noon)
            if elevation >= 30:
                return "noon"
            elif elevation >= 10:
                return "afternoon"
            elif elevation >= 3:
                return "sunset"
            elif elevation >= -1:
                return "late_sunset"
            elif elevation >= -6:
                return "dusk"
            elif elevation >= -12:
                return "twilight"
            else:
                return "night"

    def _generate_pattern(self, stage: str, rows: int, cols: int) -> List[List[int]]:
        """Render *stage* as a ``rows`` x ``cols`` grid of character codes.

        The scene is built from board-relative quantities only:

        * the horizon sits at ``style.horizon * rows``;
        * sky and ground colour bands are distributed over the rows each one
          actually has, so a 3-row board gets the same gradient compressed
          rather than a crop of a 6-row one;
        * the disc is centred horizontally on the board, its height is a
          fraction of ``rows`` and its width follows from the tile aspect,
          capped so it keeps the same share of a wide board as of a narrow
          one;
        * star count is a fixed density times the sky's area.

        Args:
            stage: Sun stage name (one of the 12 stages)
            rows: Board height in tiles
            cols: Board width in tiles

        Returns:
            ``rows`` lists of ``cols`` character codes.
        """
        # Resolve the name first: an unknown stage must behave exactly like
        # night, star placement included, and the star seed uses the name.
        if stage not in STAGE_STYLES:
            stage = "night"
        style = STAGE_STYLES[stage]
        rows = max(1, int(rows))
        cols = max(1, int(cols))

        horizon_row = min(rows, max(0, round(style.horizon * rows)))

        grid: List[List[int]] = []
        for r in range(rows):
            if r < horizon_row:
                colour = self._band_colour(style.sky, r, horizon_row)
            else:
                colour = self._band_colour(style.ground, r - horizon_row, rows - horizon_row)
            grid.append([colour] * cols)

        if style.stars and horizon_row > 0:
            self._scatter_stars(grid, stage, horizon_row, cols)

        if style.sun_offset is not None:
            self._paint_sun(grid, style, rows, cols, horizon_row)

        return grid

    @staticmethod
    def _band_colour(bands: Tuple[int, ...], index: int, span: int) -> int:
        """Colour for row *index* of a *span*-row region painted with *bands*.

        Bands are spread evenly over however many rows the region has, so the
        same gradient reads correctly whether it has one row or twelve.
        """
        if not bands:
            return BoardChars.BLACK
        if span <= 0:
            return bands[0]
        position = int(index / span * len(bands))
        return bands[min(position, len(bands) - 1)]

    @staticmethod
    def _scatter_stars(grid: List[List[int]], stage: str, sky_rows: int, cols: int) -> None:
        """Sprinkle stars through the sky band at a fixed density.

        Seeded on the stage and the geometry so a given board always draws
        the same sky (no flicker between refreshes) while a different board
        gets its own arrangement rather than a crop of someone else's.
        """
        area = sky_rows * cols
        count = max(1, round(area * STAR_DENSITY))
        count = min(count, area)
        rng = random.Random(f"{stage}:{sky_rows}x{cols}")
        for position in rng.sample(range(area), count):
            grid[position // cols][position % cols] = BoardChars.WHITE

    @staticmethod
    def _paint_sun(
        grid: List[List[int]], style: StageStyle, rows: int, cols: int, horizon_row: int
    ) -> None:
        """Paint the glow, disc and core onto *grid*, outermost zone first.

        The disc's half-height is a fraction of ``rows`` and its half-width
        follows from the tile aspect, then is capped at a share of ``cols``.
        Without that cap a squarer board (30x12) turns a sun that reads as
        two-thirds of a Flagship's width into one that floods the whole
        board, because the same "round" disc is twice as wide as it is tall.
        """
        centre_x = cols / 2.0
        half_height = style.radius * rows
        half_width = min(half_height * SUN_ASPECT, cols * SUN_MAX_WIDTH_SHARE)
        if half_height <= 0 or half_width <= 0:
            return
        centre_y = horizon_row + style.sun_offset * half_height

        zones = [
            (GLOW_SPREAD, style.glow),
            (1.0, style.disc),
            (CORE_SPREAD, style.core),
        ]

        for spread, colour in zones:
            if colour is None or spread <= 0:
                continue
            radius_y = half_height * spread
            radius_x = half_width * spread
            for r in range(rows):
                dy = (r + 0.5) - centre_y
                if abs(dy) >= radius_y:
                    continue
                row_width = radius_x * math.sqrt(1.0 - (dy / radius_y) ** 2)
                for c in range(cols):
                    if abs((c + 0.5) - centre_x) <= row_width:
                        grid[r][c] = colour

    def _pattern_to_lines(self, pattern: List[List[int]]) -> List[str]:
        """Convert a pattern grid to one board-text line per row.

        Each row becomes exactly ``len(row)`` tiles: a colour marker such as
        ``{yellow}`` is four characters but one tile, which is why the board's
        own tile counter — not ``len()`` — is the measure of width.
        """
        color_map = {
            BoardChars.RED: "{red}",
            BoardChars.ORANGE: "{orange}",
            BoardChars.YELLOW: "{yellow}",
            BoardChars.GREEN: "{green}",
            BoardChars.BLUE: "{blue}",
            BoardChars.VIOLET: "{violet}",
            BoardChars.WHITE: "{white}",
            BoardChars.BLACK: "{black}",
        }

        lines = []
        for row in pattern:
            line = ""
            for code in row:
                if code in color_map:
                    line += color_map[code]
                elif code == BoardChars.SPACE:
                    line += " "
                else:
                    # Character code - convert to character
                    line += self._code_to_char(code)
            lines.append(line)
        return lines

    def _pattern_to_string(self, pattern: List[List[int]]) -> str:
        """Convert pattern array to a newline-separated string.

        Args:
            pattern: Grid of character codes, already sized to the board

        Returns:
            Newline-separated string with color markers
        """
        return "\n".join(self._pattern_to_lines(pattern))

    def _code_to_char(self, code: int) -> str:
        """Convert character code to character string.

        Args:
            code: Character code (0-71)

        Returns:
            Character string
        """
        if 1 <= code <= 26:
            return chr(ord('A') + code - 1)
        elif 27 <= code <= 35:
            return str(code - 26)
        elif code == 36:
            return "0"
        else:
            return " "

    def _calculate_next_events(
        self, lat: float, lon: float, now: datetime, tz: pytz.BaseTzInfo
    ) -> Tuple[str, str]:
        """Calculate time until next sunrise and sunset.

        Args:
            lat: Latitude
            lon: Longitude
            now: Current datetime
            tz: Timezone

        Returns:
            Tuple of (time_to_sunrise, time_to_sunset) as "HH:MM" strings
        """
        location = LocationInfo(
            name="Location",
            region="Region",
            timezone=tz.zone,
            latitude=lat,
            longitude=lon
        )

        # Get today's sun events
        s_today = sun(location.observer, date=now.date(), tzinfo=tz)
        sunrise_today = s_today["sunrise"]
        sunset_today = s_today["sunset"]

        # Get tomorrow's sun events
        tomorrow = now.date() + timedelta(days=1)
        s_tomorrow = sun(location.observer, date=tomorrow, tzinfo=tz)
        sunrise_tomorrow = s_tomorrow["sunrise"]
        sunset_tomorrow = s_tomorrow["sunset"]

        # Determine next sunrise
        if now < sunrise_today:
            next_sunrise = sunrise_today
        else:
            next_sunrise = sunrise_tomorrow

        # Determine next sunset
        if now < sunset_today:
            next_sunset = sunset_today
        else:
            next_sunset = sunset_tomorrow

        # Calculate time differences
        delta_sunrise = next_sunrise - now
        delta_sunset = next_sunset - now

        # Format as HH:MM
        def format_timedelta(td: timedelta) -> str:
            total_seconds = int(td.total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            return f"{hours:02d}:{minutes:02d}"

        return format_timedelta(delta_sunrise), format_timedelta(delta_sunset)


# Export the plugin class
Plugin = SunArtPlugin
