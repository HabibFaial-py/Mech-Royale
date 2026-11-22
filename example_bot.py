"""Mobile zone runner for mechfight/1."""
import math

import mechfight as mf
from arena_nav import Arena


UPGRADES = ("nanites", "armor", "servos", "plating", "reactor", "jets", "capacitor", "sensors", "gyros")


def nearest(items, pos):
    return min(items, key=lambda item: math.dist(pos, item["pos"]), default=None)


def angle_error(first, second):
    return abs(math.remainder(first - second, math.tau))


def shoot(me, target, height):
    x, y, z = me["pos"]
    tx, ty, tz = target["pos"]
    dx, dy = tx - x, ty - y
    dz = tz + height - z - me["stats"]["muzzle_height"]
    distance = math.hypot(dx, dy, dz)
    direct = [math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))]
    fire = [False, False]
    aim = None
    for slot, weapon in enumerate(me["weapons"]):
        if not weapon or weapon["cooldown"] > 0:
            continue
        kind, stats = weapon["kind"], weapon["stats"]
        yaw, pitch = direct
        if kind == "mortar":
            horizontal = math.hypot(dx, dy)
            if horizontal < 2 * stats["splash_radius"]:
                continue
            rise = tz + height - z - me["stats"]["height"] - 0.5
            speed2, gravity = stats["speed"] ** 2, stats["gravity"]
            disc = speed2 ** 2 - gravity * (gravity * horizontal ** 2 + 2 * rise * speed2)
            if disc < 0:
                continue
            pitch = math.atan2(speed2 - math.sqrt(disc), gravity * horizontal)
        elif distance > stats["range"]:
            continue
        elif kind == "gun":
            if weapon.get("ammo", 1) <= 0 or weapon.get("reload", 0) > 0:
                continue
            vx, vy, vz = target.get("vel", (0, 0, 0))
            flight = distance / stats["speed"]
            lead_x, lead_y = dx + vx * flight, dy + vy * flight
            yaw = math.atan2(lead_y, lead_x)
            pitch = math.atan2(dz + vz * flight, math.hypot(lead_x, lead_y))
        elif kind == "lightning" and me["energy"] < stats["energy"]:
            continue
        if not me["stats"]["min_pitch"] <= pitch <= me["stats"]["max_pitch"]:
            continue
        if aim is None:
            aim = [yaw, pitch]
        tolerance = min(0.22, 0.035 + 2.5 / max(1, distance))
        aligned = angle_error(me["aim"][0], yaw) < tolerance and abs(me["aim"][1] - pitch) < tolerance
        if kind == "lightning":
            aligned = angle_error(me["aim"][0], yaw) < math.radians(stats["cone_deg"]) / 2
        if kind == "laser" and weapon.get("charge", 0) > 0:
            # Release a charged beam on target. Keep tracking while charging.
            aim = [yaw, pitch]
            fire[slot] = not aligned or weapon["charge"] < stats["charge"]
        else:
            fire[slot] = aligned
    return aim or direct, fire


class Bot:
    name = "hybrid-v6"

    def __init__(self, hello: mf.Hello):
        self.reach = hello["config"]["loot"]["pickup_radius"]
        self.arena = Arena(hello.get("map", {}).get("dir"))
        self.pickup_attempt = None
        self.stalled_pickups = set()
        self.crate_height = hello["config"]["crates"]["height"] / 2
        self.side = 1 if hello["slot"] % 2 else -1
        self.anchor = None
        self.detour_until = -1
        self.detour_yaw = 0
        self.hit_until = -1

    def pickup_move(self, obs):
        me, zone, now = obs["self"], obs["zone"], obs["t"]
        x, y, z = me["pos"]
        stats = me["stats"]
        radius = stats["radius"]
        bottom, top = z + stats["step_up"], z + stats["height"]
        held = {w["kind"] for w in me["weapons"] if w}
        attempt = self.pickup_attempt
        pickups = sorted(obs.get("pickups", ()), key=lambda p: (
            not attempt or p["id"] != attempt[0], math.dist(me["pos"], p["pos"])))
        for item in pickups:
            item_id = item["id"]
            if item_id in self.stalled_pickups or item["kind"] == "gun" or item["kind"] in held:
                continue
            ix, iy, iz = item["pos"]
            distance = math.hypot(ix - x, iy - y)
            if not self.reach <= distance <= 30 or abs(iz - z) >= 4:
                continue
            if math.dist((ix, iy), zone["center"]) > zone["radius"] - 3:
                continue
            if now >= zone["shrink_start"] and math.dist((ix, iy), zone["next_center"]) > zone["next_radius"]:
                continue
            if attempt and item_id == attempt[0] and now >= attempt[1]:
                if attempt[2] - distance < 2:
                    self.stalled_pickups.add(item_id)
                    continue
            dx, dy = (ix - x) / distance, (iy - y) / distance
            # Walk only far enough to collect it. Check the full corridor
            # for the mech's body, including bridges and the arena boundary.
            walk = distance - 0.75 * self.reach
            steps = math.ceil(walk / 1.5)
            for i in range(1, steps + 1):
                step = walk * i / steps
                px, py = x + dx * step, y + dy * step
                if not self.arena.in_bounds(px, py, radius) or any(
                    self.arena.solid(px - dy * side, py + dx * side, bottom, top)
                    for side in (0, radius, -radius)
                ):
                    break
            else:
                if not attempt or item_id != attempt[0] or now >= attempt[1]:
                    self.pickup_attempt = (item_id, now + 0.9, distance)
                return [dx, dy]
        return None

    def act(self, obs: mf.Observation) -> mf.Action:
        me, zone, now = obs["self"], obs["zone"], obs["t"]
        pos = me["pos"]
        x, y, z = pos
        cx, cy = zone["next_center"]
        radius = zone["next_radius"]
        speed = me["stats"]["walk_speed"]
        radial = math.hypot(cx - x, cy - y)
        to_safety = max(0, radial - radius + min(speed, radius / 3))
        near_edge = math.dist(pos[:2], zone["center"]) > zone["radius"] - speed
        urgent = not me["in_zone"] or (to_safety > 0 and (
            near_edge or now + 2 + to_safety / max(1, speed) >= zone["shrink_end"]
            or now >= zone["shrink_start"]))
        if any(e["type"] == "damaged" for e in obs.get("events", ())):
            self.hit_until = now + 1
        hurt = now < self.hit_until
        enemy = nearest((m for m in obs.get("mechs", ()) if m["kind"] == "mech"), pos)
        close = enemy is not None and math.dist(pos, enemy["pos"]) < 45

        # Follow the starter's route. Inside the destination circle, circle a
        # short distance from its center so we keep moving and seeing new ground.
        yaw = math.atan2(cy - y, cx - x)
        orbit = min(24, radius * 0.45)
        if not urgent and radial < orbit + 3:
            yaw += self.side * (math.pi / 2 + max(-0.7, min(0.7, (orbit - radial) / 8)))

        crate = None
        drone_near = any(m["kind"] == "npc" and math.dist(pos, m["pos"]) < 45
                         for m in obs.get("mechs", ()))
        if not urgent and not hurt and enemy is None and not drone_near:
            def safe_loot(item, reach):
                p = item["pos"]
                return (math.dist(pos, p) <= reach and abs(p[2] - z) < 4
                        and math.dist(p[:2], zone["center"]) < zone["radius"] - 3
                        and (now < zone["shrink_start"] or math.dist(p[:2], (cx, cy)) < radius))
            cube = nearest((c for c in obs.get("cubes", ()) if safe_loot(c, 45)), pos)
            crate = nearest((c for c in obs.get("crates", ()) if safe_loot(c, 48)), pos)
            loot = cube or crate
            if loot:
                lx, ly, _ = loot["pos"]
                yaw = math.atan2(ly - y, lx - x)
                if not cube and math.hypot(lx - x, ly - y) < 14:
                    yaw += self.side * math.pi / 2
        elif close and not urgent:
            # A bounded sidestep keeps a forward component along the zone route.
            toward = math.atan2(enemy["pos"][1] - y, enemy["pos"][0] - x)
            side = 1 if math.sin(yaw - toward) >= 0 else -1
            yaw += side * 0.6

        # After a full second without progress, try one fixed lateral heading.
        if self.anchor is None:
            self.anchor = (x, y, now)
        ax, ay, since = self.anchor
        jump = False
        if now - since >= 1.2:
            if math.hypot(x - ax, y - ay) < 1.5:
                self.detour_yaw = yaw + self.side * 1.15
                self.detour_until = now + 0.8
                jump = me["grounded"]
            self.anchor = (x, y, now)
        if now < self.detour_until:
            yaw = self.detour_yaw

        act = {"tick": obs["tick"], "move": [math.cos(yaw), math.sin(yaw)],
               "aim": [yaw, 0], "jump": jump,
               "boost": me["grounded"] and me["energy"] > 0 and (urgent or hurt)}
        target = enemy or crate
        if target:
            height = self.crate_height if enemy is None else 4.5 * (1 + 0.05 * enemy.get("cubes", 0))
            act["aim"], act["fire"] = shoot(me, target, height)

        choices = (obs.get("offer") or {}).get("choices", [])
        if choices:
            ids = [choice["id"] for choice in choices]
            act["pick"] = next((ids.index(kind) for kind in UPGRADES if kind in ids), 0)
        if len(me["weapons"]) > 1 and me["weapons"][1] is None:
            for item in obs.get("pickups", ()):
                ix, iy, iz = item["pos"]
                if item["kind"] != "gun" and math.hypot(ix - x, iy - y) < self.reach and abs(iz - z) < 4:
                    act["take"] = item["id"]
                    break
            if ("take" not in act and self.arena.bounds is not None and me["grounded"]
                    and not (urgent or hurt or drone_near) and now >= self.detour_until
                    and (enemy is None or math.dist(enemy["pos"], pos) > 80)):
                move = self.pickup_move(obs)
                if move is not None:
                    act["move"] = move
                    return act
        self.pickup_attempt = None
        return act


if __name__ == "__main__":
    mf.run(Bot)
