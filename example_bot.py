"""Mobile zone runner for mechfight/1."""
import math

import mechfight as mf
from arena_nav import Arena


# Relative value per fractional stat improvement. Additive armor is itself a fraction.
SURVIVAL_STATS = {"armor": 4, "hp": 1.8, "walk_speed": 1.8, "boost_speed": 1,
                  "regen_per_s": 0.55, "regen_delay": -0.65, "energy_regen": 0.4,
                  "energy": 0.4, "boost_drain": -0.5, "jet_drain": -0.2}


def nearest(items, pos):
    return min(items, key=lambda item: math.dist(pos, item["pos"]), default=None)


def angle_error(first, second):
    return abs(math.remainder(first - second, math.tau))


def shoot(me, target, height, arena, gun_only=False):
    x, y, z = me["pos"]
    tx, ty, tz = target["pos"]
    dx, dy = tx - x, ty - y
    dz = tz + height - z - me["stats"]["muzzle_height"]
    distance = math.hypot(dx, dy, dz)
    direct = [math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))]
    fire = [False, False]
    aim = None
    tracking = None
    for slot, weapon in enumerate(me["weapons"]):
        if not weapon or (gun_only and weapon["kind"] != "gun"):
            continue
        kind, stats = weapon["kind"], weapon["stats"]
        yaw, pitch = direct
        vx, vy, vz = target.get("vel", (0, 0, 0))
        if kind in ("gun", "mortar"):
            if kind == "gun" and (weapon.get("ammo", 1) <= 0 or weapon.get("reload", 0) > 0):
                continue
            # Refine flight time to the predicted intercept. Three iterations
            # suffice for typical mech speeds and also handle radial velocity.
            flight = distance / stats["speed"]
            for _ in range(3):
                lx, ly = dx + vx * flight, dy + vy * flight
                rise = dz + vz * flight
                horizontal = math.hypot(lx, ly)
                yaw = math.atan2(ly, lx)
                if kind == "mortar":
                    rise += me["stats"]["muzzle_height"] - me["stats"]["height"] - 0.5
                    v2, g = stats["speed"] ** 2, stats["gravity"]
                    disc = v2 ** 2 - g * (g * horizontal ** 2 + 2 * rise * v2)
                    if disc < 0 or min(horizontal, distance) < 2 * stats["splash_radius"]:
                        break
                    pitch = math.atan2(v2 - math.sqrt(disc), g * horizontal)
                    flight = horizontal / (stats["speed"] * math.cos(pitch))
                else:
                    path = math.hypot(horizontal, rise)
                    pitch = math.atan2(rise, horizontal)
                    flight = path / stats["speed"]
            if kind == "mortar":
                if disc < 0 or min(horizontal, distance) < 2 * stats["splash_radius"] or flight > stats["ttl"]:
                    continue
            elif path > stats["range"] or arena.blocked(
                    (x, y, z + me["stats"]["muzzle_height"]), (x + lx, y + ly, tz + height + vz * flight)):
                continue
        elif distance > stats["range"]:
            continue
        elif kind == "lightning" and me["energy"] < stats["energy"]:
            continue
        if not me["stats"]["min_pitch"] <= pitch <= me["stats"]["max_pitch"]:
            continue
        if tracking is None:
            tracking = [yaw, pitch]
        if weapon["cooldown"] > 0:
            continue
        if aim is None:
            aim = [yaw, pitch]
        tolerance = min(0.18, 0.008 + math.atan2(min(2.5, height), max(1, distance)))
        aligned = angle_error(me["aim"][0], yaw) < tolerance and abs(me["aim"][1] - pitch) < tolerance
        if kind == "lightning":
            aligned = angle_error(me["aim"][0], yaw) < math.radians(stats["cone_deg"]) / 2
        if kind == "laser" and weapon.get("charge", 0) > 0:
            # Release a charged beam on target. Keep tracking while charging.
            aim = [yaw, pitch]
            fire[slot] = not aligned or weapon["charge"] < stats["charge"]
        else:
            fire[slot] = aligned
    return aim or tracking or direct, fire


class Bot:
    name = "hybrid-v4"

    def __init__(self, hello: mf.Hello):
        cfg = hello["config"]
        self.arena = Arena(hello.get("map", {}).get("dir"))
        self.reach = cfg["loot"]["pickup_radius"]
        self.crate_height = cfg["crates"]["height"] / 2
        self.cube_scale = cfg["cubes"]["size_per_cube"]
        self.enemy_height = cfg["mech"]["height"] / 2
        self.upgrades = {}
        for upgrade in cfg["upgrades"]:
            value = 0
            for effect in upgrade["effects"]:
                family, _, stat = effect["path"].partition(".")
                if family == "mech":
                    base = cfg["mech"].get(stat, 1) or 1
                    change = effect.get("mul", 1) - 1 + effect.get("add", 0) / base
                    value += SURVIVAL_STATS.get(stat, 0) * change
            self.upgrades[upgrade["id"]] = value
        self.side = 1 if hello["slot"] % 2 else -1
        self.anchor = None
        self.detour_until = -1
        self.detour_yaw = 0
        self.hit_until = -1
        self.rotate_until = -1
        self.loot_pos = None
        self.loot_until = -1
        self.escape_yaw = None
        self.mode = "ROTATE"
        self.hold_until = -1

    def contacts(self, obs):
        me = obs["self"]
        x, y, z = me["pos"]
        muzzle = (x, y, z + me["stats"]["muzzle_height"])
        threats = []
        targets = []
        for enemy in obs.get("mechs", ()):
            if enemy["kind"] != "mech":
                continue
            ex, ey, ez = enemy["pos"]
            dx, dy = ex - x, ey - y
            distance = math.hypot(dx, dy)
            vx, vy, _ = enemy.get("vel", (0, 0, 0))
            if min(distance, math.hypot(dx + vx, dy + vy)) < (112 if self.mode == "ESCAPE" else 100):
                threats.append((dx, dy, distance))
            height = self.enemy_height * (1 + self.cube_scale * enemy.get("cubes", 0))
            if self.arena.blocked(muzzle, (ex, ey, ez + height)):
                continue
            health = enemy.get("hp", 1) / max(1, enemy.get("max_hp", enemy.get("hp", 1)))
            turn = angle_error(me["aim"][0], math.atan2(dy, dx)) / max(0.1, me["stats"]["turn_rate"])
            score = distance + 60 * health + 8 * turn
            targets.append((score, distance, health, enemy, height))
        return threats, min(targets, key=lambda item: item[0], default=None)

    def steer(self, me, yaw, threat, zone, urgent):
        x, y, z = me["pos"]
        stats = me["stats"]
        def clearance(angle):
            return self.arena.clearance(x, y, math.cos(angle), math.sin(angle),
                                        z + stats["step_up"], z + stats["height"], stats["radius"])
        clear = clearance(yaw)
        patrol = self.mode == "ROTATE" and not urgent and self.arena.bounds is not None
        if clear == 14 and threat is None and not patrol:
            return yaw
        best, chosen = -math.inf, yaw
        # Check only the intended direction and four immediate alternatives.
        for offset in (0, self.side * 0.65, -self.side * 0.65, self.side * 1.3, -self.side * 1.3):
            angle = yaw + offset
            room = clearance(angle) if offset else clear
            px, py = x + room * math.cos(angle), y + room * math.sin(angle)
            score = room - 3 * abs(offset)
            if patrol and room == 14:
                # A wall beside an open lane can provide cover on the next turn.
                side_x, side_y = -8 * math.sin(angle), 8 * math.cos(angle)
                if any(self.arena.solid(px + sign * side_x, py + sign * side_y, z + 3, z + 6)
                       for sign in (-1, 1)):
                    score += 2.5
            if threat and room > 5 and self.arena.cover(px, py, z, threat["pos"]):
                score += 5
            if urgent:
                score -= 2 * max(0, math.dist((px, py), zone["center"]) - zone["radius"])
            if score > best:
                best, chosen = score, angle
        return chosen

    def act(self, obs: mf.Observation) -> mf.Action:
        me, zone, now = obs["self"], obs["zone"], obs["t"]
        x, y, z = pos = me["pos"]
        cx, cy = zone["next_center"]
        radius = zone["next_radius"]
        speed = me["stats"]["walk_speed"]
        radial = math.hypot(cx - x, cy - y)
        to_safety = max(0, radial - radius + min(speed, radius / 3))
        near_edge = math.dist(pos[:2], zone["center"]) > zone["radius"] - speed
        urgent = not me["in_zone"] or (to_safety > 0 and (
            near_edge or now + 2 + 1.4 * to_safety / max(1, speed) >= zone["shrink_end"]
            or now >= zone["shrink_start"]))
        for event in obs.get("events", ()):
            if event["type"] == "damaged" and event.get("weapon") != "zone":
                self.hit_until = now + 1.5
                if event.get("bearing") is not None:
                    self.escape_yaw = event["bearing"] + math.pi
        hurt = now < self.hit_until
        threats, target = self.contacts(obs)
        distance, health, enemy, height = target[1:] if target else (math.inf, 1, None, 0)
        wounded = me["hp"] < me["max_hp"] * 0.65
        group = len(threats) >= 2
        close = distance < 42
        # Preserve V3's ranged pressure in a healthy duel even before a finish
        # is available. Avoid initiating it against a supported opponent.
        isolated = enemy is not None and not any(
            m is not enemy and m["kind"] == "mech" and math.dist(m["pos"], enemy["pos"]) < 85
            for m in obs.get("mechs", ()))
        ranged_duel = (isolated and distance < 160 and me["hp"] >= me["max_hp"] * 0.80
                       and me["hp"] >= enemy.get("hp", me["hp"]) * 0.95)
        favorable = close or (distance < 100 and not wounded) or ranged_duel or (health < 0.55 and distance < 180)
        drone = any(m["kind"] == "npc" and math.dist(pos, m["pos"]) < 45 for m in obs.get("mechs", ()))

        # Safety transitions interrupt immediately. Keep retreating briefly
        # after contact disappears; let a single target cross the fight threshold.
        if urgent:
            mode = "ROTATE"
            self.rotate_until = now + 0.7
        elif group or (wounded and (threats or hurt)) or (hurt and enemy is None):
            mode = "ESCAPE"
            self.hold_until = now + (3 if wounded else 1.5)
        elif self.mode == "ESCAPE" and now < self.hold_until:
            mode = "ESCAPE"
        elif favorable or (self.mode == "FIGHT" and enemy and now < self.hold_until):
            mode = "FIGHT"
            if favorable:
                self.hold_until = now + 0.8
        else:
            mode = "ROTATE"

        # Settle on the near side of large circles. Keep V3's orbit motion,
        # widening the patrol radius so that rotation needn't cross the center.
        yaw = math.atan2(cy - y, cx - x)
        orbit = min(130, radius * 0.65)
        if not urgent and radial < orbit + 6:
            yaw += self.side * (math.pi / 2 + max(-0.7, min(0.7, (orbit - radial) / 12)))

        crate = None
        if mode == "ROTATE" and now >= self.rotate_until and not (urgent or threats or hurt or drone):
            def safe(item, reach):
                p = item["pos"]
                return (math.dist(pos, p) <= reach and abs(p[2] - z) < 4
                        and math.dist(p[:2], zone["center"]) < zone["radius"] - 3
                        and (now < zone["shrink_start"] or math.dist(p[:2], (cx, cy)) < radius)
                        and not self.arena.blocked((x, y, z + 3), (p[0], p[1], p[2] + 3)))
            cube = nearest((c for c in obs.get("cubes", ()) if safe(c, 50)), pos)
            pickup = None
            if me["weapons"][1] is None:
                pickup = min((p for p in obs.get("pickups", ()) if p["kind"] != "gun" and safe(p, 35)),
                             key=lambda p: math.dist(pos, p["pos"]) + (8 if p["kind"] == "mortar" else 0),
                             default=None)
            crate = nearest((c for c in obs.get("crates", ()) if safe(c, 48)), pos)
            loot = cube or pickup or crate
            if loot:
                self.loot_pos = loot["pos"]
                self.loot_until = now + 0.8
                mode = "FARM"
                lx, ly, _ = loot["pos"]
                yaw = math.atan2(ly - y, lx - x)
                if loot is crate and math.hypot(lx - x, ly - y) < 14:
                    yaw += self.side * math.pi / 2
                if loot is not crate:
                    crate = None
            elif self.mode == "FARM" and now < self.loot_until and math.dist(pos, self.loot_pos) > self.reach:
                # Bridge a brief visibility gap between a destroyed crate and its cube.
                mode = "FARM"
                yaw = math.atan2(self.loot_pos[1] - y, self.loot_pos[0] - x)
        if mode == "ESCAPE":
            if threats:
                # Choose the safest short exit, including when opponents on
                # opposite sides would cancel in a weighted-vector average.
                def separation(angle):
                    sx, sy = 14 * math.cos(angle), 14 * math.sin(angle)
                    gain = min(math.hypot(dx - sx, dy - sy) - d for dx, dy, d in threats)
                    margin = zone["radius"] - math.dist((x + sx, y + sy), zone["center"])
                    return gain + min(0, margin - 3) + math.cos(angle - yaw)
                self.escape_yaw = max((yaw + i * math.pi / 4 for i in range(8)), key=separation)
            if self.escape_yaw is not None:
                yaw = self.escape_yaw
        elif mode == "FIGHT" and close:
            toward = math.atan2(enemy["pos"][1] - y, enemy["pos"][0] - x)
            yaw += 0.6 if math.sin(yaw - toward) >= 0 else -0.6
        self.mode = mode
        yaw = self.steer(me, yaw, enemy, zone, urgent)

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
               "boost": me["grounded"] and me["energy"] > 0 and (urgent or hurt or (group and mode == "ESCAPE"))}
        if enemy:
            ex, ey, _ = enemy["pos"]
            act["aim"] = [math.atan2(ey - y, ex - x), 0]
        if mode == "FIGHT" or (close and (hurt or distance < 25)):
            act["aim"], act["fire"] = shoot(me, enemy, height, self.arena)
        elif crate:
            act["aim"], act["fire"] = shoot(me, crate, self.crate_height, self.arena, gun_only=True)

        if mode != "FIGHT" and not any(act.get("fire", ())) and not close:
            for w in me["weapons"]:
                if w and w["kind"] == "gun" and not w.get("reload") and w.get("ammo", 1) <= w["stats"]["mag"] * 0.4:
                    act["reload"] = True
        choices = (obs.get("offer") or {}).get("choices", [])
        if choices:
            act["pick"] = max(range(len(choices)), key=lambda i: self.upgrades.get(choices[i]["id"], 0))
        if me["weapons"][1] is None:
            for item in obs.get("pickups", ()):
                ix, iy, iz = item["pos"]
                if item["kind"] != "gun" and math.hypot(ix - x, iy - y) < self.reach and abs(iz - z) < 4:
                    act["take"] = item["id"]
                    break
        return act


if __name__ == "__main__":
    mf.run(Bot)
