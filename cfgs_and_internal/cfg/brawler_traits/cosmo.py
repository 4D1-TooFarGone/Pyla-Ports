# ═══════════════════════════════════════════════════════════════════════
#  COSMO — per-brawler behavior override
# ═══════════════════════════════════════════════════════════════════════
#
# Real Cosmo mechanics this file models (from the Brawl Stars wiki):
#   - Attack ("Orbit"): fires 3 planets that orbit outward in a circle.
#     They grow in size AND damage the farther they travel — he does his
#     listed *minimum* damage at point-blank range and his *maximum*
#     damage at the outer edge of his 9-tile range. Unlike a normal
#     ranged brawler (who's happy anywhere inside attack_range), Cosmo
#     specifically wants to fight as close to his max range as possible
#     and actively avoid short range, where his own attack is weak and
#     he has only moderate health to tank a return hit.
#   - Attack ignores walls: the wiki explicitly notes the orbiting curve
#     lets him hit around small walls/pillars — modeled generically via
#     brawlers_info.json's ignore_walls_for_attacks=true, so try/attack
#     logic below doesn't require a clean line of sight.
#   - Super ("Gravitation Pull", ~9.33 tiles, ignores line-of-sight):
#     marks a hit enemy so his and his teammates' projectiles strongly
#     home into them for 6s — a near-guaranteed pick. Modeled here as:
#     fire whenever a target is hittable within super_range, same trigger
#     shape as a normal aimed damage-super, since we don't have visibility
#     into teammates' reload state to time it more cleverly than that.
#   - Gadget "Planetary Pushback" (AoE knockback) is the natural pick for
#     a brawler that wants distance — modeled as a defensive panic button
#     when something closes inside his danger zone, on the same cooldown
#     pattern the generic engine uses (persistent_data["last_gadget_use"]).
#
# CONTRACT with the loader in individual_brawlers.pyla: this file is
# exec()'d into the same globals as the main script (player_data,
# enemy_data, walls, teammate_data, persistent_data, JOYSTICK_RADIUS,
# TILE_SIZE, math, time, attack, use_super, use_gadget, is_super_ready,
# is_gadget_ready, is_path_blocked, is_enemy_hittable, is_there_poison_gas,
# find_closest_enemy, find_closest_teammate, get_distance, normalize_move,
# is_blocked, first_unblocked, move_toward, move_away_from, strafe_around,
# random_safe_movement, clampf, attack_range/safe_range/super_range,
# player_pos, brawler_info, debug, ...) — see move_toward/strafe_around/
# etc. defined earlier in individual_brawlers.pyla's "VECTOR / MOVEMENT
# HELPERS" section, all already available here.
#
# Because this sets `movement` itself, the loader treats combat movement
# as fully handled and skips its own generic combat/no-enemy logic for
# this frame — so, unlike a trait-only file (e.g. barley.py), this one is
# responsible for poison-gas avoidance and no-enemy wandering too, not
# just combat positioning.

BRAWLER_TRAIT = {
    "role": "controller",
    "aggression": 0.15,          # unused by the custom logic below, kept for docs/fallback
    "kite_intensity": 0.9,
    "retreat_ratio": 0.85,
    "stick_to_teammates": 0.35,
    "prefers_bushes": False,
    "hold_ground": False,
}

# Below this fraction of attack_range, his attack is doing close to
# minimum damage and he's in easy return-hit range — treat as "too close".
# Also the trigger point for the Planetary Pushback gadget: the wiki tip
# is to use it "right before an enemy closes the distance", i.e. exactly
# when he'd otherwise start retreating, not only once already at melee
# range — a knockback is a more decisive way to enforce that retreat.
RETREAT_THRESHOLD_RATIO = 0.55
GADGET_COOLDOWN_SECONDS = 3.0

allow_wall_attack = bool(brawler_info.get("ignore_walls_for_attacks", False))


# ═══════════════════════════════════════════════════════
#  POISON GAS (individual_brawlers.pyla's own avoid_poison_gas() isn't
#  defined yet at this point in the script, so this is a small self
#  contained copy of the same logic).
# ═══════════════════════════════════════════════════════

def _cosmo_avoid_poison_gas():
    poison_gas = is_there_poison_gas(player_data)
    x = 0
    y = 0
    if poison_gas["up"] or poison_gas["down"]:
        y = JOYSTICK_RADIUS if poison_gas["up"] > poison_gas["down"] else -JOYSTICK_RADIUS
    if poison_gas["left"] or poison_gas["right"]:
        x = JOYSTICK_RADIUS if poison_gas["left"] > poison_gas["right"] else -JOYSTICK_RADIUS
    if x == 0 and y == 0:
        return None
    move = normalize_move(x, y)
    return first_unblocked([move, (x, 0), (0, y)], move)


# ═══════════════════════════════════════════════════════
#  NO ENEMY — same shape as individual_brawlers.pyla's own
#  no_enemy_movement(), also not defined yet at this point.
# ═══════════════════════════════════════════════════════

def _cosmo_no_enemy_movement():
    closest_teammate_coords, teammate_distance = find_closest_teammate(teammate_data, player_pos, walls)
    if debug:
        print(
            f"[cosmo] no_enemy: teammate_count={len(teammate_data)} "
            f"closest_teammate_coords={closest_teammate_coords} teammate_distance={teammate_distance} "
            f"follow_threshold={TILE_SIZE * 3:.1f}"
        )
    if closest_teammate_coords is not None:
        if teammate_distance is None or teammate_distance > TILE_SIZE * 3:
            if debug:
                print(f"[cosmo] no_enemy branch=follow_teammate -> {closest_teammate_coords}")
            return move_toward(closest_teammate_coords)
    forward = (0, -JOYSTICK_RADIUS)
    if not is_blocked(forward):
        if debug:
            print("[cosmo] no_enemy branch=forward")
        return forward
    if debug:
        print("[cosmo] no_enemy branch=random_safe_movement (forward blocked)")
    return random_safe_movement()


gas_movement = _cosmo_avoid_poison_gas()

enemy_coords = None
enemy_distance = None
if is_there_enemy(enemy_data):
    enemy_coords, enemy_distance = find_closest_enemy(enemy_data, player_pos, walls, "attack")

if debug:
    print(
        f"[cosmo] enemy_coords={enemy_coords} enemy_distance={enemy_distance} "
        f"gas_movement={gas_movement} attack_range={attack_range:.1f} "
        f"retreat_below={attack_range * RETREAT_THRESHOLD_RATIO:.1f}"
    )

# ═══════════════════════════════════════════════════════
#  ATTACK / SUPER / GADGET — independent of movement, same as the
#  generic engine: always take a free shot when one's available.
# ═══════════════════════════════════════════════════════

if enemy_coords is not None:
    enemy_hittable = is_enemy_hittable(player_pos, enemy_coords, walls, "attack")
    if enemy_distance <= attack_range and (enemy_hittable or allow_wall_attack):
        attack()

    if is_super_ready and enemy_distance <= super_range and is_enemy_hittable(player_pos, enemy_coords, walls, "super"):
        if debug:
            print(f"[cosmo] SUPER (Gravitation Pull) marking target at distance={enemy_distance:.1f}")
        use_super()

    if (
        is_gadget_ready
        and enemy_distance <= attack_range * RETREAT_THRESHOLD_RATIO
        and time.time() - persistent_data["last_gadget_use"] > GADGET_COOLDOWN_SECONDS
    ):
        if debug:
            print(f"[cosmo] GADGET (Planetary Pushback) at distance={enemy_distance:.1f}")
        use_gadget()
        persistent_data["last_gadget_use"] = time.time()


# ═══════════════════════════════════════════════════════
#  MOVEMENT — always try to sit near the outer edge of his own range,
#  where his orbiting planets deal close to max damage, instead of the
#  generic "somewhere inside attack_range" behavior.
# ═══════════════════════════════════════════════════════

if gas_movement is not None:
    movement = gas_movement
elif enemy_coords is None:
    movement = _cosmo_no_enemy_movement()
else:
    retreat_threshold = attack_range * RETREAT_THRESHOLD_RATIO
    orbit_band_edge = attack_range * 1.05

    if enemy_distance < retreat_threshold:
        # Point-blank: his own attack is near minimum damage here and he
        # only has moderate health, so back off rather than trade.
        branch = "retreat-too-close"
        movement = move_away_from(enemy_coords)
    elif enemy_distance <= orbit_band_edge:
        # Anywhere from "too close" out to just past max range: this is
        # the whole band where his planets are already dealing solid to
        # near-max damage, so keep strafing here instead of closing in
        # further or standing still (harder to hit, still landing shots).
        branch = "orbit-band-strafe"
        movement = strafe_around(enemy_coords)
    else:
        branch = "close-to-range"
        movement = move_toward(enemy_coords)

    if is_blocked(movement):
        if debug:
            print(f"[cosmo] movement={tuple(round(v, 1) for v in movement)} branch={branch} BLOCKED, falling back")
        if enemy_distance < retreat_threshold:
            movement = first_unblocked(
                [move_away_from(enemy_coords), strafe_around(enemy_coords), random_safe_movement()], movement
            )
        else:
            movement = first_unblocked(
                [move_toward(enemy_coords), strafe_around(enemy_coords), random_safe_movement()], movement
            )

    if debug:
        print(f"[cosmo] branch={branch} enemy_distance={enemy_distance:.1f} -> movement={tuple(round(v, 1) for v in movement)}")
