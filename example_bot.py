"""Survival-first pilot for mechfight/1."""
import math

import mechfight as mf
from arena_nav import Arena


DIRECTIONS = [(math.cos(i * math.tau / 16), math.sin(i * math.tau / 16)) for i in range(16)]
UPGRADE_RANK = {kind: i for i, kind in enumerate(
    ("nanites", "armor", "servos", "plating", "reactor", "jets", "capacitor", "sensors", "gyros")
)}


def safe_radius(zone, speed):
    radius = zone["next_radius"]
    return radius - min(radius * 0.22, max(8, 1.5 * speed))


def zone_goal(arena, pos, zone, stats):
    """Find a nearby open point on the near side of the upcoming circle."""
    x, y, z = pos
    cx, cy = zone["next_center"]
    safe = safe_radius(zone, stats["walk_speed"])
    distance = math.hypot(x - cx, y - cy)
    if distance <= safe:
        return x, y
    angle = math.atan2(y - cy, x - cx)
    bottom, top = z + stats["step_up"], z + stats["height"]
    # Search nearby arcs first, then deeper rings if buildings fill the near edge.
    best = None
    for r in (safe, max(0, safe - 18), safe * 0.65, 0):
        arc = min(0.45, 16 / max(1, r))
        for offset in (0, arc, -arc, 3 * arc, -3 * arc, 1.2, -1.2, math.pi):
            px, py = cx + r * math.cos(angle + offset), cy + r * math.sin(angle + offset)
            travel = math.hypot(px - x, py - y)
            if best and travel >= best[0]:
                continue
            if arena.in_bounds(px, py, stats["radius"]) and not arena.solid(px, py, bottom, top):
                best = travel, px, py
                if offset == 0 and r == safe:
                    return px, py
    return best[1:] if best else (cx, cy)


class Bot:
    name = "survival-v1"

    def __init__(self, hello: mf.Hello):
        self.arena = Arena(hello.get("map", {}).get("dir"))
        self.reach = hello["config"]["loot"]["pickup_radius"]
        self.defaults = hello["config"]["mech"]
        self.last_hit = -100
        self.hit_bearing = 0.0
        self.heading = (0, 0)
        self.anchor = None
        self.recover_until = -1
        self.side = 1 if hello["slot"] % 2 else -1
        self.detour = (0, 0)

    def threats(self, obs, pos):
        x, y, _ = pos
        now = obs["t"]
        for event in obs.get("events", ()):
            if event["type"] == "damaged" and event.get("bearing") is not None:
                self.last_hit, self.hit_bearing = now, event["bearing"]
        risk = []
        target = None
        highest = 0
        nearby = 0
        for actor in obs.get("mechs", ()):
            ax, ay, _ = actor["pos"]
            distance = math.hypot(ax - x, ay - y)
            drone = actor["kind"] == "npc"
            weight = max(0, 1 - distance / (55 if drone else 190))
            if drone:
                weight *= 0.45
            else:
                nearby += distance < 90
                weight *= 1.35 if actor.get("charging") else 1
                if weight > highest:
                    target, highest = actor, weight
            if weight:
                risk.append((ax, ay, distance, weight))

        # Bearings point toward the source in world coordinates. Limit sound's
        # influence so a noisy skirmish cannot outweigh the shrinking circle.
        away_x = away_y = 0
        for sound in obs.get("sounds", ()):
            if sound["dist"] == "near" and sound["kind"] not in ("drop", "crate", "death"):
                strength = 0.5 * max(0, 1 - sound.get("age", 0))
                away_x -= strength * math.cos(sound["bearing"])
                away_y -= strength * math.sin(sound["bearing"])
        scale = max(1, math.hypot(away_x, away_y))
        memory = max(0, 1 - (now - self.last_hit) / 3)
        return (risk, target, nearby,
                away_x / scale - 2 * memory * math.cos(self.hit_bearing),
                away_y / scale - 2 * memory * math.sin(self.hit_bearing))

    def observe_progress(self, x, y, now):
        if self.anchor is None or self.heading == (0, 0):
            self.anchor = (x, y, now)
        ax, ay, since = self.anchor
        if now - since >= 1.2:
            if math.hypot(x - ax, y - ay) < 1.5:
                hx, hy = self.heading
                self.detour = (-self.side * hy, self.side * hx)
                self.side *= -1
                self.recover_until = now + 1.1
            self.anchor = (x, y, now)

    def act(self, obs: mf.Observation) -> mf.Action:
        me, zone, now = obs["self"], obs["zone"], obs["t"]
        x, y, z = pos = me["pos"]
        stats = self.defaults | me.get("stats", {})
        speed = stats["walk_speed"]
        gx, gy = zone_goal(self.arena, pos, zone, stats)
        travel = math.hypot(gx - x, gy - y)
        cx, cy = zone["center"]
        edge = math.hypot(x - cx, y - cy) - zone["radius"]
        # Forecast the circle when walking there with time for a detour.
        start = max(now, zone["shrink_start"])
        eta = now + 1.5 * travel / max(speed, 1) + 3
        f = min(1, max(0, (eta - start) / max(0.1, zone["shrink_end"] - start)))
        fx = cx + f * (zone["next_center"][0] - cx)
        fy = cy + f * (zone["next_center"][1] - cy)
        fr = zone["radius"] + f * (zone["next_radius"] - zone["radius"])
        urgent = edge > 0 or (travel > 1 and math.hypot(x - fx, y - fy) > fr - speed)

        risks, target, nearby, away_x, away_y = self.threats(obs, pos)
        wounded = me["hp"] < me["max_hp"] * 0.8
        hit = now - self.last_hit < 2
        close_threat = target is not None and math.dist(pos[:2], target["pos"][:2]) < 35
        danger = hit or nearby >= 2 or close_threat or (nearby > 0 and wounded)
        self.observe_progress(x, y, now)
        recovering = now < self.recover_until
        bottom, top = z + stats["step_up"], z + stats["height"]
        # Holding is an option only when safety permits it.
        must_move = travel > 0.25 or danger
        heading, best, room = (0, 0), -math.inf if must_move else 0.0, 0
        safe = safe_radius(zone, speed)
        evading = bool(risks or away_x or away_y)
        candidates = DIRECTIONS if must_move or evading else ()
        for dx, dy in candidates:
            clear = self.arena.clearance(x, y, dx, dy, bottom, top, stats["radius"])
            if not clear:
                continue
            step = min(clear, speed) if evading else min(clear, speed, travel)
            px, py = x + step * dx, y + step * dy
            progress = travel - math.hypot(px - gx, py - gy)
            # When already safe, moving inside the next circle has no zone cost.
            if travel == 0:
                progress = -max(0, math.dist((px, py), zone["next_center"]) - safe)
            score = (6 if urgent else 1.1) * progress
            score -= 5 * (max(0, math.hypot(px - cx, py - cy) - zone["radius"]) - max(0, edge))
            # Potential differences reward increasing separation from a group,
            # even when two equally near enemies are on opposite sides.
            escape = sum(w * (math.hypot(px - ax, py - ay) - dist) for ax, ay, dist, w in risks)
            score += (2 if danger else 1.4) * escape / max(1, len(risks) ** 0.5)
            score += step * (dx * away_x + dy * away_y)
            if target and self.arena.cover(px, py, z, target["pos"]):
                score += 12 if danger else 7
            if recovering:
                score += 22 * (dx * self.detour[0] + dy * self.detour[1])
            score -= (14 - clear) * 1.8
            score += 2 * (dx * self.heading[0] + dy * self.heading[1])
            score -= 2.5
            if score > best:
                heading, best, room = (dx, dy), score, clear

        self.heading = heading
        moving = heading != (0, 0)
        if not moving and must_move:
            # An unmapped ledge or rasterized corner can leave no clear probe.
            angle = math.atan2(gy - y, gx - x) if travel else self.hit_bearing + math.pi
            if recovering:
                angle = math.atan2(self.detour[1], self.detour[0])
            self.heading = heading = (math.cos(angle), math.sin(angle))
            moving = True

        scale = 0.999999
        if not evading and not danger and travel < speed * 0.1:
            scale *= min(1, travel / max(0.1, speed * 0.1))
        boost = moving and me["grounded"] and (urgent or danger) and me["energy"] > (2 if edge > 0 else 18)
        aim = list(me["aim"])
        act = {"tick": obs["tick"], "move": [scale * d for d in heading], "boost": bool(boost)}
        if recovering and room < 8 and me["grounded"] and me["energy"] > 25:
            act["jump"] = True
        if target:
            tx, ty, tz = target["pos"]
            dist = math.hypot(tx - x, ty - y)
            aim = [math.atan2(ty - y, tx - x), math.atan2(tz - z - 2.5, dist)]
            separating = ((x - tx) * heading[0] + (y - ty) * heading[1]) / max(1, dist)
            aligned = math.cos(aim[0] - me["aim"][0]) > 0.97
            if dist < 35 and (hit or target.get("charging")) and separating < 0.25 and aligned:
                act["fire"] = [bool(w and w["kind"] == "gun") for w in me["weapons"]]
        elif hit:
            aim = [self.hit_bearing, 0]
        elif moving:
            aim = [math.atan2(heading[1], heading[0]), 0]
        act["aim"] = aim

        choices = (obs.get("offer") or {}).get("choices", [])
        if choices:
            act["pick"] = min(range(len(choices)), key=lambda i: UPGRADE_RANK.get(choices[i]["id"], len(UPGRADE_RANK)))
        if not danger and len(me["weapons"]) > 1 and me["weapons"][1] is None:
            for item in obs.get("pickups", ()):
                ix, iy, iz = item["pos"]
                if item["kind"] != "gun" and math.hypot(ix - x, iy - y) < self.reach and abs(iz - z) < 4:
                    act["take"] = item["id"]
                    break
        return act


if __name__ == "__main__":
    mf.run(Bot)
