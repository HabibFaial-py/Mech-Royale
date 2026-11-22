"""Small height-aware occupancy grid for local steering on the competition map."""
import json
import math
from pathlib import Path


class Arena:
    cell = 2.5

    def __init__(self, directory):
        self.columns = {}
        self.bounds = None
        try:
            with (Path(directory) / "v2" / "world.json").open(encoding="utf-8") as f:
                world = json.load(f)
            bounds = world["bounds"]
            self.bounds = tuple(bounds[k] for k in ("minX", "maxX", "minY", "maxY"))
            for prism in world["prisms"]:
                self._index(prism)
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            self.bounds = None
            self.columns.clear()

    def _index(self, prism):
        # Fill horizontal spans between successive crossings. Including inner
        # rings in the same scan leaves courtyard cells open.
        rings = [prism["ring"], *prism.get("holes", [])]
        crossings = {}
        for ring in rings:
            for i in range(0, len(ring), 2):
                x0, y0 = ring[i - 2], ring[i - 1]
                x1, y1 = ring[i], ring[i + 1]
                start = math.ceil(min(y0, y1) / self.cell - 0.5)
                stop = math.ceil(max(y0, y1) / self.cell - 0.5)
                for row in range(start, stop):
                    y = (row + 0.5) * self.cell
                    x = x0 + (x1 - x0) * (y - y0) / (y1 - y0)
                    crossings.setdefault(row, []).append(x)
        span = (prism["base"], prism["top"])
        for row, xs in crossings.items():
            xs.sort()
            for left, right in zip(xs[::2], xs[1::2]):
                for col in range(math.ceil(left / self.cell - 0.5),
                                 math.ceil(right / self.cell - 0.5)):
                    self.columns.setdefault((col, row), []).append(span)

    def solid(self, x, y, bottom, top):
        spans = self.columns.get((math.floor(x / self.cell), math.floor(y / self.cell)), ())
        for base, roof in spans:
            if base < top and roof > bottom:
                return True
        return False

    def in_bounds(self, x, y, margin):
        if self.bounds is None:
            return True
        west, east, south, north = self.bounds
        return west + margin <= x <= east - margin and south + margin <= y <= north - margin

    def clearance(self, x, y, dx, dy, bottom, top, radius):
        """Approximate swept body clearance over a short, bounded horizon."""
        if self.bounds is None and not self.columns:
            return 14
        for distance in (2, 5, 8, 11, 14):
            px, py = x + dx * distance, y + dy * distance
            if not self.in_bounds(px, py, radius):
                return distance - 2
            for side in (0, radius, -radius):
                if self.solid(px - dy * side, py + dx * side, bottom, top):
                    return distance - 2
        return 14

    def cover(self, x, y, z, enemy):
        dx, dy, dz = enemy[0] - x, enemy[1] - y, enemy[2] - z
        distance = math.hypot(dx, dy)
        for step in (3, 6, 10, 15, 21):
            if step >= distance:
                break
            f = step / distance
            height = z + 6 + f * dz
            if self.solid(x + f * dx, y + f * dy, height, height):
                return True
        return False

    def blocked(self, start, end):
        """Sample a direct sight line against the preindexed prism columns."""
        if not self.columns:
            return False
        x, y, z = start
        dx, dy, dz = (b - a for a, b in zip(start, end))
        steps = max(1, math.ceil(math.hypot(dx, dy) / self.cell))
        for i in range(1, steps):
            f = i / steps
            height = z + f * dz
            if self.solid(x + f * dx, y + f * dy, height, height):
                return True
        return False
