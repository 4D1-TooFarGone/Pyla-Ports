# ═══════════════════════════════════════════════════════════════════════
#  BOLT — per-brawler behavior override (EXAMPLE / PROTOTYPE, not wired up yet)
# ═══════════════════════════════════════════════════════════════════════
#
# This is an example of the "one file per brawler" format we want to move
# to: instead of a row of numbers in a shared traits.json, a brawler that
# plays completely differently gets its own file with real logic in it.
#
# Real Bolt mechanics this file models (from in-game data):
#   - Contact damage: rolling into an enemy's hitbox deals damage
#     automatically — no button press needed, purely a movement/collision
#     effect. Damage scales with current speed.
#   - Speed ("lights", 0-7): starts empty, takes 0.5s of near-straight
#     movement to charge 1 light, 3.5s total for all 7 (max speed). A
#     sharp turn (heading leaves the "center band") rapidly drains lights;
#     stopping completely drains them instantly.
#   - Ammo (attack button): does NOT deal the primary damage — pressing it
#     burns 1 of 2 ammo to double damage for 0.8s, add +2 lights (if
#     moving), and +50% Super charge rate for that window. Reload is very
#     slow, so this is a scarce burst tool, not a repeatable action.
#   - Super ("Overdrive", 4s): no direct speed boost, but lights charge
#     ~3.5x faster while moving, plus a damage-reduction shield and a
#     damaging lightning trail. Charges from movement distance, not from
#     dealing damage (handled by the game's own is_super_ready detection
#     already — we don't need to model the charge source ourselves).
#   - Brawl Ball: speed is capped at 3.5/7 lights while carrying the ball.
#
# Not modeled here (left as TODOs — this is a prototype, not final):
#   - Gadgets (Oil Change, Bouncy Ball) and Star Powers (Toss Up,
#     Unstoppaball) — no loadout info is exposed in context yet.
#   - Exact reload time for ammo (not in brawlers_info.json yet — using a
#     conservative placeholder, see AMMO_RELOAD_SECONDS below).
#
# CONTRACT with the (future) loader in individual_brawlers.pyla:
#   - This file is exec()'d with the exact same context dict the main
#     playstyle gets (player_data, enemy_data, walls, bushes,
#     persistent_data, JOYSTICK_RADIUS, math, time, attack, use_super,
#     is_super_ready, is_path_blocked, find_closest_enemy, ...).
#   - If it sets `movement` itself (like here), the loader should treat
#     combat movement as fully handled and skip the generic
#     decide_combat_movement() logic entirely for this brawler.
#   - BRAWLER_TRAIT is still declared below for documentation / as a
#     fallback if some other part of the system wants generic numbers
#     for Bolt (e.g. no-enemy exploration), even though the combat
#     movement below ignores it.

BRAWLER_TRAIT = {
    "role": "tank",
    "aggression": 1.0,          # unused by the custom logic below, kept for docs/fallback
    "kite_intensity": 0.0,
    "retreat_ratio": 0.0,
    "stick_to_teammates": 0.2,
    "prefers_bushes": False,
    "hold_ground": True,
}


# ═══════════════════════════════════════════════════════
#  GAME MODE HOOKS (placeholder — real mode/objective detection isn't
#  wired up yet, but the branch points already exist here)
# ═══════════════════════════════════════════════════════

current_gamemode = get_context("gamemode", "unknown")

# Real, confirmed mechanic: Bolt's speed bar is capped at 3.5/7 lights
# while he's carrying the ball in Brawl Ball. We don't have ball-carrier
# detection yet, so this defaults to False until that exists — but the
# cap logic below is already correct once `carrying_ball` gets wired in.
carrying_ball = get_context("carrying_ball", False)
BRAWL_BALL_CARRY_MOMENTUM_CAP = 3.5 / 7.0

def gamemode_engage_range_bias():
    # TODO: e.g. in Heist, prioritize charging the safe over chasing
    # enemies once safe/objective detection exists. Placeholder for now.
    return 1.0


# ═══════════════════════════════════════════════════════
#  SPEED ("LIGHTS") MODEL — real timing: 0.5s/light, 3.5s for all 7,
#  instant drain on stop, rapid drain on a sharp turn, ~3.5x faster
#  charge rate while Super (Overdrive) is active.
# ═══════════════════════════════════════════════════════

MAX_TURN_ANGLE_FOR_FULL_SPEED = math.radians(7)   # the indicator's "center band"
FULL_CHARGE_SECONDS = 3.5                          # 7 lights x 0.5s
CHARGE_RATE_PER_SECOND = 1.0 / FULL_CHARGE_SECONDS
OVERDRIVE_CHARGE_MULTIPLIER = 3.5                  # ~0.5s -> ~0.14s per light
SHARP_TURN_DRAIN_PER_SECOND = 1.2                  # TODO: no official number, tuned to feel "rapid"
WALL_CRASH_DRAIN = 0.55                            # one-off hit, not a per-second drain
OVERDRIVE_DURATION_SECONDS = 4.0
LIGHTS_PER_AMMO_USE = 2.0 / 7.0

persistent_data.setdefault("bolt_momentum", 0.0)          # 0.0 - 1.0, 1.0 = all 7 lights
persistent_data.setdefault("bolt_heading", None)          # radians, None until first move
persistent_data.setdefault("bolt_last_update_ts", None)
persistent_data.setdefault("bolt_overdrive_until", 0.0)
persistent_data.setdefault("bolt_ammo", 2.0)
persistent_data.setdefault("bolt_last_ammo_use", 0.0)

def clampf(value, lo, hi):
    return max(lo, min(hi, value))

def angle_of(vec):
    return math.atan2(vec[1], vec[0])

def heading_to_vec(angle, radius=JOYSTICK_RADIUS):
    return (math.cos(angle) * radius, math.sin(angle) * radius)

def angle_diff(target_angle, current_angle):
    return (target_angle - current_angle + math.pi) % (2 * math.pi) - math.pi

def in_overdrive():
    return time.time() < persistent_data["bolt_overdrive_until"]

now = time.time()
dt = 0.0
if persistent_data["bolt_last_update_ts"] is not None:
    dt = clampf(now - persistent_data["bolt_last_update_ts"], 0.0, 0.5)  # cap in case of a freeze/lag spike
persistent_data["bolt_last_update_ts"] = now


# ═══════════════════════════════════════════════════════
#  AMMO (attack button) — NOT the primary damage source. Contact rolling
#  deals damage automatically. This is a scarce (2 charges, slow reload)
#  burst: double damage + 2 lights + faster Super charge for 0.8s.
# ═══════════════════════════════════════════════════════

AMMO_MAX = 2.0
AMMO_RELOAD_SECONDS = 6.0  # TODO: verify real reload time, "very slow" per the wiki — this is a guess
AMMO_REGEN_PER_SECOND = 1.0 / AMMO_RELOAD_SECONDS

if persistent_data["bolt_ammo"] < AMMO_MAX:
    persistent_data["bolt_ammo"] = min(AMMO_MAX, persistent_data["bolt_ammo"] + AMMO_REGEN_PER_SECOND * dt)

def use_ammo_burst():
    persistent_data["bolt_ammo"] = max(0.0, persistent_data["bolt_ammo"] - 1.0)
    persistent_data["bolt_last_ammo_use"] = time.time()
    attack()
    persistent_data["bolt_momentum"] = min(1.0, persistent_data["bolt_momentum"] + LIGHTS_PER_AMMO_USE)


# ═══════════════════════════════════════════════════════
#  PICK A TARGET (enemy to roll into, or somewhere to build up speed)
# ═══════════════════════════════════════════════════════

_brawler_name = brawler or current_brawler
safe_range, attack_range, super_range = get_brawler_range(_brawler_name)

player_pos = get_entity_pos(player_data)
target_pos = None
enemy_distance = None

if is_there_enemy(enemy_data):
    target_pos, enemy_distance = find_closest_enemy(enemy_data, player_pos, walls, "attack")

_target_source = "enemy" if target_pos is not None else "no-enemy-exploration"

if target_pos is None:
    # Nothing to crash into — keep rolling forward to build speed rather
    # than idling (idling would drain the lights to zero for nothing).
    heading = persistent_data["bolt_heading"]
    target_pos = heading_to_vec(heading if heading is not None else -math.pi / 2, radius=300)
    target_pos = (player_pos[0] + target_pos[0], player_pos[1] + target_pos[1])

if debug:
    print(
        f"[bolt] target_source={_target_source} is_there_enemy={is_there_enemy(enemy_data)} "
        f"enemy_data_count={len(enemy_data)} player_pos={tuple(round(v, 1) for v in player_pos)} "
        f"target_pos={tuple(round(v, 1) for v in target_pos)} enemy_distance={enemy_distance}"
    )


# ═══════════════════════════════════════════════════════
#  STEER TOWARD TARGET WITHOUT A SHARP TURN — clamp how much we're
#  allowed to turn in one tick so we stay in the indicator's "center
#  band" and basically never trigger the game's own turn-slowdown.
# ═══════════════════════════════════════════════════════

desired_heading = angle_of((target_pos[0] - player_pos[0], target_pos[1] - player_pos[1]))
current_heading = persistent_data["bolt_heading"]

if current_heading is None:
    steered_heading = desired_heading
    off_center_band = False
else:
    diff = angle_diff(desired_heading, current_heading)
    clamped_diff = clampf(diff, -MAX_TURN_ANGLE_FOR_FULL_SPEED, MAX_TURN_ANGLE_FOR_FULL_SPEED)
    steered_heading = current_heading + clamped_diff
    off_center_band = abs(diff) > MAX_TURN_ANGLE_FOR_FULL_SPEED

if debug:
    print(
        f"[bolt] desired_heading_deg={math.degrees(desired_heading):.1f} "
        f"current_heading_deg={'None' if current_heading is None else round(math.degrees(current_heading), 1)} "
        f"steered_heading_deg={math.degrees(steered_heading):.1f}"
    )

movement = heading_to_vec(steered_heading)

# Bolt commits hard to a heading to keep momentum, so he needs to see a
# wall coming further out than the default ~1-tile lookahead — by the
# time a 1-tile check trips, a fast, committed roll is often already
# touching the wall. Look ahead further so he can curve around early
# instead of eating a crash penalty.
WALL_LOOKAHEAD_TILES = 2.2
wall_lookahead = TILE_SIZE * WALL_LOOKAHEAD_TILES

wall_crash = is_path_blocked(player_data, movement, walls, distance=wall_lookahead)
if wall_crash:
    # Prefer a real route around the obstacle (grid A*, see
    # Play.find_path) over guessing an escape angle — take the direction
    # of the first waypoint the router suggests.
    route_waypoints = find_path(player_pos, target_pos, walls)
    if route_waypoints and len(route_waypoints) > 1:
        next_waypoint = route_waypoints[1] if get_distance(player_pos, route_waypoints[0]) < 1.0 else route_waypoints[0]
        route_heading = angle_of((next_waypoint[0] - player_pos[0], next_waypoint[1] - player_pos[1]))
        candidate = heading_to_vec(route_heading)
        if not is_path_blocked(player_data, candidate, walls, distance=wall_lookahead):
            steered_heading = route_heading
            movement = candidate
            wall_crash = False

    if wall_crash:
        # Pathfinder found nothing usable (fully boxed in, or search cap
        # exceeded) — fall back to guessing progressively wider angles
        # before accepting the full speed hit from crashing.
        for nudge_deg in (15, -15, 30, -30, 45, -45, 60, -60, 90, -90):
            nudge = math.radians(nudge_deg)
            candidate = heading_to_vec(steered_heading + nudge)
            if not is_path_blocked(player_data, candidate, walls, distance=wall_lookahead):
                steered_heading = steered_heading + nudge
                movement = candidate
                wall_crash = False
                break

stopped = math.hypot(movement[0], movement[1]) < 1.0

if debug:
    print(f"[bolt] wall_count={len(walls)} wall_lookahead={wall_lookahead:.0f} wall_crash={wall_crash}")

momentum = persistent_data["bolt_momentum"]
if wall_crash:
    momentum = max(0.0, momentum - WALL_CRASH_DRAIN)
elif stopped:
    momentum = 0.0  # stopping fully drains the lights, per the real trait
elif off_center_band:
    momentum = max(0.0, momentum - SHARP_TURN_DRAIN_PER_SECOND * dt)
else:
    charge_rate = CHARGE_RATE_PER_SECOND * (OVERDRIVE_CHARGE_MULTIPLIER if in_overdrive() else 1.0)
    momentum = min(1.0, momentum + charge_rate * dt)

if carrying_ball:
    # TODO: only meaningful once ball-carrier detection exists (see
    # `carrying_ball` above) — real mechanic, not a guess.
    momentum = min(momentum, BRAWL_BALL_CARRY_MOMENTUM_CAP)

persistent_data["bolt_heading"] = steered_heading
persistent_data["bolt_momentum"] = momentum

if debug:
    print(
        f"[bolt] dt={dt:.3f} wall_crash={wall_crash} stopped={stopped} off_center_band={off_center_band} "
        f"momentum={momentum:.3f} heading_deg={math.degrees(steered_heading):.1f} "
        f"movement={tuple(round(v, 1) for v in movement)} ammo={persistent_data['bolt_ammo']:.2f} "
        f"overdrive={in_overdrive()}"
    )


# ═══════════════════════════════════════════════════════
#  AMMO BURST DECISION — spend a scarce charge either to punish an
#  imminent collision (double damage right as we hit) or to recover
#  speed after getting knocked out of the center band. Never both greedy
#  and constant: only 2 charges, very slow reload.
# ═══════════════════════════════════════════════════════

CONTACT_IMMINENT_RANGE = 160.0  # rough "about to collide" distance, tune against real hitbox size
AMMO_BURST_COOLDOWN = 1.0       # don't blow both charges within the same second

about_to_collide = enemy_distance is not None and enemy_distance <= CONTACT_IMMINENT_RANGE
lost_the_edge = momentum < 0.4 and (wall_crash or off_center_band)

if persistent_data["bolt_ammo"] >= 1.0 and time.time() - persistent_data["bolt_last_ammo_use"] > AMMO_BURST_COOLDOWN:
    if about_to_collide or lost_the_edge:
        if debug:
            print(f"[bolt] ammo burst triggered (about_to_collide={about_to_collide} lost_the_edge={lost_the_edge})")
        use_ammo_burst()


# ═══════════════════════════════════════════════════════
#  SUPER: go into Overdrive when there's something worth charging into.
#  (Charges from movement distance, which the game already tracks via
#  is_super_ready — we don't need to model the charge source ourselves.)
# ═══════════════════════════════════════════════════════

if is_super_ready and target_pos is not None and is_there_enemy(enemy_data):
    _, engage_distance = find_closest_enemy(enemy_data, player_pos, walls, "super")
    # brawlers_info still has super_range=0 for bolt (not tuned yet) — fall back
    # to attack_range as a "close enough to charge" proxy until that's fixed.
    effective_super_range = super_range if super_range > 0 else attack_range
    if engage_distance is not None and engage_distance <= effective_super_range * gamemode_engage_range_bias():
        if debug:
            print(f"[bolt] SUPER (overdrive) triggered at engage_distance={engage_distance:.1f}")
        use_super()
        persistent_data["bolt_overdrive_until"] = time.time() + OVERDRIVE_DURATION_SECONDS
