from flask import Flask, render_template, request, jsonify
import requests

from fpl_module import (
    players,
    POSITION_MAP,
    team_name,
    get_player_fixtures,
    get_active_gameweek,
    get_next_gameweek,
    xpts_breakdown_with_bonus,
    get_player_photo,
    top_xpts_players_for_gw,
)

app = Flask(__name__)

@app.get("/health")
def health():
    return "ok", 200


# ----------------------------
# TEAM COLORS
# ----------------------------
TEAM_COLORS = {
    1:  "#c8102e",  # Arsenal
    2:  "#6cabdd",  # Aston Villa
    3:  "#1b458f",  # Bournemouth
    4:  "#e30613",  # Brentford
    5:  "#0057b8",  # Brighton
    6:  "#034694",  # Chelsea
    7:  "#1f3c88",  # Crystal Palace
    8:  "#003399",  # Everton
    9:  "#000000",  # Fulham
    10: "#e03a3e",  # Liverpool
    11: "#6cabdd",  # Manchester City
    12: "#da291c",  # Manchester United
    13: "#241f20",  # Newcastle United
    14: "#ffcd00",  # Nottingham Forest
    15: "#d71920",  # Tottenham
    16: "#7a263a",  # West Ham
    17: "#fdb913",  # Wolves
    18: "#1c2c5b",  # Leeds United
    19: "#eb172b",  # Sunderland
    20: "#004170",  # Burnley
}

def team_color(team_id):
    return TEAM_COLORS.get(team_id, "#d9d9d9")


# ----------------------------
# PAGES
# ----------------------------
@app.route("/")
def home():
    gw = get_next_gameweek()

    # Top 3 captain picks (highest xPts)
    captains = top_xpts_players_for_gw(gw, n=3, min_minutes=1)

    # Add photo_url + team color for the home page cards
    for c in captains:
        pid = c.get("player_id")
        p_obj = next((pl for pl in players if pl.get("id") == pid), None)
        c["photo_url"] = get_player_photo(p_obj) if p_obj else ""
        c["team_color"] = team_color(c.get("team_id"))

        # Optional: keep a clean label for display
        c["xpts"] = c.get("total_xPts")

    return render_template("home.html", gw=gw, captains=captains)

@app.route("/teams")
def teams_page():
    return render_template("teams.html")

@app.route("/stats")
def stats_page():
    return render_template("stats.html")


# ----------------------------
# API: Players xPts (JSON)
# ----------------------------
@app.route("/api/player_xpts")
def api_player_xpts():
    raw = (request.args.get("player_id") or "").strip()
    if not raw.isdigit():
        return jsonify({"error": "Invalid player_id"}), 400

    pid = int(raw)

    gw_raw = (request.args.get("gw") or "").strip()
    if gw_raw.isdigit():
        gw = int(gw_raw)
    else:
        gw = get_next_gameweek()

    p = next((pl for pl in players if pl["id"] == pid), None)
    if not p:
        return jsonify({"error": "Player not found"}), 404

    opp_label = "—"
    x = ""  # keep as string for frontend safety

    fxs = get_player_fixtures(p, gw)
    fx = fxs[0] if fxs else None

    if fx:
        try:
            player_team_id = p.get("team")
            team_h = fx.get("team_h")
            team_a = fx.get("team_a")
            if player_team_id is not None and team_h is not None and team_a is not None:
                is_home = (player_team_id == team_h)
                opp_id = team_a if is_home else team_h
                opp_abbr = team_name(opp_id)[:3].upper()
                opp_label = f"vs {opp_abbr} ({'H' if is_home else 'A'})"
        except Exception:
            opp_label = "—"

        breakdown = xpts_breakdown_with_bonus(p, fx)
        if breakdown and breakdown.get("total_xPts_with_bonus") is not None:
            try:
                x = float(breakdown["total_xPts_with_bonus"])
            except Exception:
                x = breakdown.get("total_xPts_with_bonus")

    return jsonify({
        "player_id": pid,
        "gw": gw,
        "opp": opp_label,
        "xpts": x
    })


FPL_BOOTSTRAP = "https://fantasy.premierleague.com/api/bootstrap-static/"

def fpl_get_json(url):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://fantasy.premierleague.com/",
    }
    try:
        r = requests.get(url, headers=headers, timeout=10)
    except Exception:
        return 0, None

    if r.status_code != 200:
        return r.status_code, None

    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, None


def find_valid_gameweek(team_id):
    status, bs = fpl_get_json(FPL_BOOTSTRAP)
    if status != 200 or not bs or "events" not in bs:
        return None

    events = bs["events"]

    candidates = []
    current = next((e for e in events if e.get("is_current")), None)
    next_gw = next((e for e in events if e.get("is_next")), None)
    finished = [e for e in events if e.get("finished")]

    if current:
        candidates.append(current["id"])
    if next_gw:
        candidates.append(next_gw["id"])
    if finished:
        candidates.append(max(finished, key=lambda e: e["id"])["id"])

    for gw in candidates:
        url = f"https://fantasy.premierleague.com/api/entry/{team_id}/event/{gw}/picks/"
        st, data = fpl_get_json(url)
        if st == 200 and data and data.get("picks"):
            return gw

    return None


@app.route("/api/gameweeks")
def api_gameweeks():
    st, bs = fpl_get_json(FPL_BOOTSTRAP)
    if st != 200 or not bs or "events" not in bs:
        return jsonify({"error": "Could not load gameweeks"}), 502

    events = bs["events"]
    out = [{
        "id": e.get("id"),
        "name": e.get("name"),
        "is_current": bool(e.get("is_current")),
        "is_next": bool(e.get("is_next")),
        "finished": bool(e.get("finished")),
    } for e in events if e.get("id") is not None]

    return jsonify({"events": out})


@app.route("/api/import_team")
def api_import_team():
    raw = (request.args.get("team_id") or "").strip()
    if not raw.isdigit():
        return jsonify({"error": "Invalid team_id"}), 400

    team_id = int(raw)

    # Confirm team exists
    st, entry = fpl_get_json(f"https://fantasy.premierleague.com/api/entry/{team_id}/")

    total_value = (entry.get("last_deadline_value") or 0) / 10.0
    bank = (entry.get("last_deadline_bank") or 0) / 10.0

    # Real squad value = total - bank
    squad_value = total_value - bank


    if st != 200 or not entry:
        return jsonify({"error": "Team not found"}), 404

    # Find a GW that works
    gw = find_valid_gameweek(team_id)
    if not gw:
        return jsonify({"error": "Could not find a valid gameweek"}), 502

    # Fetch picks
    st, data = fpl_get_json(
        f"https://fantasy.premierleague.com/api/entry/{team_id}/event/{gw}/picks/"
    )
    if st != 200 or not data or not data.get("picks"):
        return jsonify({"error": "Could not load picks"}), 502

    picks = sorted(data["picks"], key=lambda x: x.get("position", 99))

    slots = []
    for pick in picks:
        pid = pick["element"]
        p = next((pl for pl in players if pl["id"] == pid), None)
        if not p:
            continue

        pos = POSITION_MAP.get(p["element_type"])
        price = p.get("now_cost", 0) / 10
        kit = team_color(p.get("team"))

        opp_label = "—"
        x = ""

        fxs = get_player_fixtures(p, gw)
        fx = fxs[0] if fxs else None
        if fx:
            try:
                player_team_id = p.get("team")
                team_h = fx.get("team_h")
                team_a = fx.get("team_a")
                is_home = player_team_id == team_h
                opp_id = team_a if is_home else team_h
                opp_abbr = team_name(opp_id)[:3].upper()
                opp_label = f"vs {opp_abbr} ({'H' if is_home else 'A'})"

                breakdown = xpts_breakdown_with_bonus(p, fx)
                if breakdown and breakdown.get("total_xPts_with_bonus") is not None:
                    x = breakdown["total_xPts_with_bonus"]
            except Exception:
                pass

        slots.append({
            "name": p.get("web_name"),
            "player_id": pid,
            "team": team_name(p["team"]),
            "team_id": p.get("team"),
            "xpts": x,
            "price": price,
            "opp": opp_label,
            "color": kit,
            "pos": pos,
            "photo_url": get_player_photo(p),

        })

    # Ensure exactly 15 slots
    while len(slots) < 15:
        slots.append({
            "name": None,
            "player_id": "",
            "xpts": "",
            "team_id": None,
            "price": None,
            "opp": "—",
            "color": "#d9d9d9",
            "pos": None
        })

    return jsonify({
        "team_id": team_id,
        "team_name": entry.get("name"),
        "manager": f"{entry.get('player_first_name','')} {entry.get('player_last_name','')}",
        "gw": gw,

        "bank": round(bank, 1),
        "squad_value": round(squad_value, 1),
        "total_value": round(total_value, 1),

        "slots": slots[:15]
    })



@app.route("/api/players")
def api_players():
    position = (request.args.get("position") or "").strip().upper()
    q = (request.args.get("q") or "").strip().lower()

    out = []
    for p in players:
        try:
            pid = p.get("id")
            name = (p.get("web_name") or "").strip()
            if not pid or not name:
                continue

            pos = POSITION_MAP.get(p.get("element_type"))
            if not pos:
                continue

            if position:
                if position == "OUT":
                    if pos == "GK":
                        continue
                else:
                    if pos != position:
                        continue

            if q and q not in name.lower():
                continue

            team_id = p.get("team")
            price = (p.get("now_cost", 0) / 10)
            owned = float(p.get("selected_by_percent") or 0.0)

            out.append({
                "id": pid,
                "name": name,
                "team": team_name(team_id),
                "team_id": team_id,
                "position": pos,
                "price": price,
                "owned_by": owned,
                "team_color": team_color(team_id),
                "photo_url": get_player_photo(p),
            })
        except Exception:
            continue

    out.sort(key=lambda x: x["owned_by"], reverse=True)
    return jsonify(out[:250])


@app.route("/lineup", methods=["GET", "POST"])
def lineup():
    # ----------------------------
    # 1) Decide GW (FOR BOTH GET + POST) with forward-only clamp
    # ----------------------------
    active_gw = get_active_gameweek()

    # Works for:
    # - /lineup?gw=21  (querystring)
    # - POST form field named "gw"
    requested_gw = request.values.get("gw", type=int)

    if requested_gw is None:
        gw = active_gw
    else:
        # forward-only: never allow anything below active_gw
        gw = max(active_gw, requested_gw)

    # ----------------------------
    # 2) Default empty 15 slots
    # ----------------------------
    slots = [
        {
            "name": None,
            "player_id": "",
            "xpts": "",
            "team_id": None,
            "price": None,
            "opp": None,
            "color": "#d9d9d9",
            "pos": None
        }
        for _ in range(15)
    ]

    slot_types = ["GK"] + ["OUT"] * 10 + [""] * 4
    total = None

    # ----------------------------
    # 3) If POST: read players, calculate xPts for THIS gw
    # ----------------------------
    if request.method == "POST":
        ids = request.form.getlist("player_ids")
        total_val = 0.0

        for i in range(min(len(ids), 15)):
            raw = (ids[i] or "").strip()
            if not raw.isdigit():
                continue

            pid = int(raw)
            p = next((pl for pl in players if pl["id"] == pid), None)
            if not p:
                continue

            pos = POSITION_MAP.get(p["element_type"])
            price = (p.get("now_cost", 0) / 10)
            kit = team_color(p.get("team"))

            opp_label = None
            fxs = get_player_fixtures(p, gw)
            fx = fxs[0] if fxs else None

            if fx:
                try:
                    player_team_id = p.get("team")
                    team_h = fx.get("team_h")
                    team_a = fx.get("team_a")
                    if player_team_id is not None and team_h is not None and team_a is not None:
                        is_home = (player_team_id == team_h)
                        opp_id = team_a if is_home else team_h
                        opp_abbr = team_name(opp_id)[:3].upper()
                        opp_label = f"vs {opp_abbr} ({'H' if is_home else 'A'})"
                except Exception:
                    opp_label = None

            x = ""
            if fx:
                breakdown = xpts_breakdown_with_bonus(p, fx)
                if breakdown and breakdown.get("total_xPts_with_bonus") is not None:
                    x = breakdown["total_xPts_with_bonus"]

            # Sum first 11 only (0..10)
            if i <= 10 and x != "":
                try:
                    total_val += float(x)
                except Exception:
                    pass

            slots[i] = {
                "name": p.get("web_name"),
                "player_id": pid,
                "team": team_name(p["team"]),
                "team_id": p.get("team"),
                "xpts": x,
                "price": price,
                "opp": opp_label,
                "color": kit,
                "pos": pos,
                "photo_url": get_player_photo(p),
            }

        total = round(total_val, 2)

    # ----------------------------
    # 4) Render (gw is ALWAYS defined now)
    # ----------------------------
    return render_template(
        "lineup.html",
        slots=slots,
        total=total,
        slot_types=slot_types,
        gw=gw,
        active_gw=active_gw
    )




@app.route("/predictor", methods=["GET", "POST"])
def predictor():
    filters_result = None

    if request.method == "POST":
        min_price = request.form.get("min_price", type=float)
        max_price = request.form.get("max_price", type=float)

        position = (request.form.get("position") or "").strip()
        club = (request.form.get("club") or "").strip().lower()
        player_name = (request.form.get("player_name") or "").strip().lower()

        gw_min = request.form.get("gameweek_min", type=int)
        gw_max = request.form.get("gameweek_max", type=int)

        if min_price is not None:
            min_price = max(3.5, min(16.0, min_price))
        if max_price is not None:
            max_price = max(3.5, min(16.0, max_price))

        if gw_min is not None:
            gw_min = max(23, min(38, gw_min))
        if gw_max is not None:
            gw_max = max(23, min(38, gw_max))

        filtered_players = []
        for p in players:
            try:
                if position and POSITION_MAP.get(p["element_type"]) != position:
                    continue

                if club:
                    team = team_name(p["team"]).lower()
                    if club not in team:
                        continue

                if player_name:
                    name = (p.get("web_name") or "").lower()
                    if player_name not in name:
                        continue

                price = (p.get("now_cost", 0) / 10)
                if min_price is not None and price < min_price:
                    continue
                if max_price is not None and price > max_price:
                    continue

                filtered_players.append(p)
            except Exception:
                continue

        filters_result = []
        next_gw = get_next_gameweek()

        for p in filtered_players:
            gw_points = []
            for gw in range(next_gw, next_gw + 5):
                xpts_val = None

                fxs = get_player_fixtures(p, gw)
                if fxs:
                    fx = fxs[0]
                    breakdown = xpts_breakdown_with_bonus(p, fx)
                    if breakdown and breakdown.get("total_xPts_with_bonus") is not None:
                        try:
                            xpts_val = float(breakdown["total_xPts_with_bonus"])
                        except Exception:
                            xpts_val = None

                gw_points.append({
                    "gw": gw,
                    "xpts": round(xpts_val, 1) if xpts_val is not None else None
                })

            vals = [g["xpts"] for g in gw_points if g["xpts"] is not None]
            if not vals:
                continue

            total_xpts = sum(vals)

            price = (p.get("now_cost", 0) / 10)
            ownership = float(p.get("selected_by_percent") or 0)

            mins = float(p.get("minutes") or 0)
            starts = float(p.get("starts") or 0)
            total_points = float(p.get("total_points") or 0)

            # Estimate appearances from minutes (≈ 25 mins per appearance for bench/cameos)
            est_apps = max(int(round(mins / 25.0)), int(starts), 1)

            pts_per_app = total_points / est_apps if est_apps > 0 else None
            pts_per_90  = (total_points / mins) * 90.0 if mins > 0 else None

            filters_result.append({
                "player": p["web_name"],
                "player_id": p["id"],
                "photo_url": get_player_photo(p),
                "position": POSITION_MAP[p["element_type"]],
                "team_abbr": team_name(p["team"])[:3].upper(),
                "team_color": "#888",
                "price": price,
                "ownership": ownership,
                "next_gw": next_gw,
                "gw_points": gw_points,
                "total_xpts": round(total_xpts, 1),

                "pts_per_app": pts_per_app,
                "pts_per_90": pts_per_90,
                "mins": mins,
                "starts": starts,
                "est_apps": est_apps,
            })

        def next_gw_xpts(p):
            gw = p.get("gw_points", [])
            if not gw:
                return -1
            v = gw[0].get("xpts")
            return float(v) if v is not None else -1

        filters_result.sort(key=next_gw_xpts, reverse=True)


    return render_template("predictor.html", filters_result=filters_result, search_result=None)


if __name__ == "__main__":
    app.run(debug=True, port=5050)
