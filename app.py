from flask import Flask, render_template, request, jsonify
import requests
import fpl_module as fpl

app = Flask(__name__)

@app.get("/health")
def health():
    return "ok", 200


# ----------------------------
# TEAM COLORS
# ----------------------------
def team_color(team_id):
    return fpl.team_color(team_id)


# ----------------------------
# PAGES
# ----------------------------
@app.route("/")
def home():
    fpl.ensure_loaded()

    gw = fpl.get_active_gameweek()
    captains = fpl.top_xpts_players_for_gw(gw, n=3, min_minutes=1)

    for c in captains:
        pid = c.get("player_id")
        p_obj = next((pl for pl in fpl.players if pl.get("id") == pid), None)
        c["photo_url"] = fpl.get_player_photo(p_obj) if p_obj else ""
        c["team_color"] = team_color(c.get("team_id"))
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
    fpl.ensure_loaded()

    raw = (request.args.get("player_id") or "").strip()
    if not raw.isdigit():
        return jsonify({"error": "Invalid player_id"}), 400
    pid = int(raw)

    gw_raw = (request.args.get("gw") or "").strip()
    gw = int(gw_raw) if gw_raw.isdigit() else fpl.get_next_gameweek()

    p = next((pl for pl in fpl.players if pl["id"] == pid), None)
    if not p:
        return jsonify({"error": "Player not found"}), 404

    opp_label = "—"
    x = ""

    fxs = fpl.get_player_fixtures(p, gw)

    if fxs:
        # Opponent label for single or double fixtures
        try:
            opp_label = fpl.fixture_label_for_gw(p, gw)
        except Exception:
            opp_label = "—"

        # Sum xPts across ALL fixtures in that GW
        total_x = 0.0
        got_any = False

        for fx in fxs:
            try:
                event = fx.get("event")
                team_h = fx.get("team_h")
                team_a = fx.get("team_a")

                totals_map = fpl.fixture_totals_with_bonus(event, team_h, team_a)
                val = totals_map.get(p["id"])
                if val is not None:
                    total_x += float(val)
                    got_any = True
            except Exception:
                continue

        if got_any:
            x = round(total_x, 2)

    return jsonify({"player_id": pid, "gw": gw, "opp": opp_label, "xpts": x})


# ----------------------------
# (YOUR EXISTING FPL IMPORT TEAM HELPERS)
# ----------------------------
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
    fpl.ensure_loaded()

    raw = (request.args.get("team_id") or "").strip()
    if not raw.isdigit():
        return jsonify({"error": "Invalid team_id"}), 400
    team_id = int(raw)

    st, entry = fpl_get_json(f"https://fantasy.premierleague.com/api/entry/{team_id}/")
    if st != 200 or not entry:
        return jsonify({"error": "Team not found"}), 404

    total_value = (entry.get("last_deadline_value") or 0) / 10.0
    bank = (entry.get("last_deadline_bank") or 0) / 10.0
    squad_value = total_value - bank

    gw = find_valid_gameweek(team_id)
    if not gw:
        return jsonify({"error": "Could not find a valid gameweek"}), 502

    st, data = fpl_get_json(f"https://fantasy.premierleague.com/api/entry/{team_id}/event/{gw}/picks/")
    if st != 200 or not data or not data.get("picks"):
        return jsonify({"error": "Could not load picks"}), 502

    picks = sorted(data["picks"], key=lambda x: x.get("position", 99))

    slots = []
    for pick in picks:
        pid = pick["element"]
        p = next((pl for pl in fpl.players if pl["id"] == pid), None)
        if not p:
            continue

        pos = fpl.POSITION_MAP.get(p["element_type"])
        price = p.get("now_cost", 0) / 10
        kit = team_color(p.get("team"))

        opp_label = "—"
        x = ""

        fxs = fpl.get_player_fixtures(p, gw)

        if fxs:
            # DGW-friendly label
            try:
                opp_label = fpl.fixture_label_for_gw(p, gw)
            except Exception:
                opp_label = "—"

            # DGW-friendly xPts sum
            total_x = 0.0
            got_any = False

            for fx in fxs:
                event = fx.get("event")
                team_h = fx.get("team_h")
                team_a = fx.get("team_a")
                totals_map = fpl.fixture_totals_with_bonus(event, team_h, team_a)
                val = totals_map.get(p["id"])
                if val is not None:
                    total_x += float(val)
                    got_any = True

            if got_any:
                x = round(total_x, 2)


        slots.append({
            "name": p.get("web_name"),
            "player_id": pid,
            "team": fpl.team_name(p["team"]),
            "team_id": p.get("team"),
            "xpts": x,
            "price": price,
            "opp": opp_label,
            "color": kit,
            "pos": pos,
            "photo_url": fpl.get_player_photo(p),
        })

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
    fpl.ensure_loaded()

    position = (request.args.get("position") or "").strip().upper()
    q = (request.args.get("q") or "").strip().lower()

    out = []
    for p in fpl.players:
        try:
            pid = p.get("id")
            name = (p.get("web_name") or "").strip()
            if not pid or not name:
                continue

            pos = fpl.POSITION_MAP.get(p.get("element_type"))
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
                "team": fpl.team_name(team_id),
                "team_id": team_id,
                "position": pos,
                "price": price,
                "owned_by": owned,
                "team_color": team_color(team_id),
                "photo_url": fpl.get_player_photo(p),
            })
        except Exception:
            continue

    out.sort(key=lambda x: x["owned_by"], reverse=True)
    return jsonify(out[:250])


@app.route("/lineup", methods=["GET", "POST"])
def lineup():
    fpl.ensure_loaded()

    active_gw = fpl.get_active_gameweek()
    requested_gw = request.values.get("gw", type=int)
    gw = active_gw if requested_gw is None else max(active_gw, requested_gw)

    slots = [
        {"name": None, "player_id": "", "xpts": "", "team_id": None, "price": None,
         "opp": None, "color": "#d9d9d9", "pos": None}
        for _ in range(15)
    ]

    slot_types = ["GK"] + ["OUT"] * 10 + [""] * 4
    total = None

    if request.method == "POST":
        ids = request.form.getlist("player_ids")
        total_val = 0.0

        for i in range(min(len(ids), 15)):
            raw = (ids[i] or "").strip()
            if not raw.isdigit():
                continue

            pid = int(raw)
            p = next((pl for pl in fpl.players if pl["id"] == pid), None)
            if not p:
                continue

            pos = fpl.POSITION_MAP.get(p["element_type"])
            price = (p.get("now_cost", 0) / 10)
            kit = team_color(p.get("team"))

            opp_label = "—"
            x = ""

            fxs = fpl.get_player_fixtures(p, gw)

            if fxs:
                # 1) Opponent label for single or double fixtures
                try:
                    opp_label = fpl.fixture_label_for_gw(p, gw)  # e.g. "vs BRE (A) + vs WOL (A)"
                except Exception:
                    opp_label = "—"

                # 2) Sum xPts across ALL fixtures in that GW
                total_x = 0.0
                got_any = False

                for fx in fxs:
                    try:
                        event = fx.get("event")
                        team_h = fx.get("team_h")
                        team_a = fx.get("team_a")

                        totals_map = fpl.fixture_totals_with_bonus(event, team_h, team_a)
                        val = totals_map.get(p["id"])
                        if val is not None:
                            total_x += float(val)
                            got_any = True
                    except Exception:
                        continue

                if got_any:
                    x = round(total_x, 2)


 

            if i <= 10 and x != "":
                try:
                    total_val += float(x)
                except Exception:
                    pass

            slots[i] = {
                "name": p.get("web_name"),
                "player_id": pid,
                "team": fpl.team_name(p["team"]),
                "team_id": p.get("team"),
                "xpts": x,
                "price": price,
                "opp": opp_label,
                "color": kit,
                "pos": pos,
                "photo_url": fpl.get_player_photo(p),
            }

        total = round(total_val, 2)

    return render_template("lineup.html", slots=slots, total=total, slot_types=slot_types, gw=gw, active_gw=active_gw)


@app.route("/predictor", methods=["GET", "POST"])
def predictor():
    fpl.ensure_loaded()

    filters_result = None

    if request.method == "POST":
        min_price = request.form.get("min_price", type=float)
        max_price = request.form.get("max_price", type=float)

        position = (request.form.get("position") or "").strip()
        club = (request.form.get("club") or "").strip().lower()
        player_name = (request.form.get("player_name") or "").strip().lower()

        if min_price is not None:
            min_price = max(3.5, min(16.0, min_price))
        if max_price is not None:
            max_price = max(3.5, min(16.0, max_price))

        filtered_players = []
        for p in fpl.players:
            try:
                if position and fpl.POSITION_MAP.get(p["element_type"]) != position:
                    continue

                if club:
                    team = fpl.team_name(p["team"]).lower()
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
        next_gw = fpl.get_next_gameweek()

        for p in filtered_players:
            gw_points = []
            for gw in range(next_gw, next_gw + 5):
                xpts_val = None
                fxs = fpl.get_player_fixtures(p, gw)

                if fxs:
                    total_x = 0.0
                    got_any = False

                    for fx in fxs:
                        totals = fpl.fixture_totals_with_bonus(fx.get("event"), fx.get("team_h"), fx.get("team_a"))
                        val = totals.get(p["id"])
                        if val is not None:
                            total_x += float(val)
                            got_any = True

                    if got_any:
                        xpts_val = total_x


                gw_points.append({"gw": gw, "xpts": round(xpts_val, 1) if xpts_val is not None else None})

            vals = [g["xpts"] for g in gw_points if g["xpts"] is not None]
            if not vals:
                continue

            total_xpts = sum(vals)
            price = (p.get("now_cost", 0) / 10)
            ownership = float(p.get("selected_by_percent") or 0)

            filters_result.append({
                "player": p["web_name"],
                "player_id": p["id"],
                "photo_url": fpl.get_player_photo(p),
                "position": fpl.POSITION_MAP[p["element_type"]],
                "team_abbr": fpl.team_name(p["team"])[:3].upper(),
                "team_color": "#888",
                "price": price,
                "ownership": ownership,
                "next_gw": next_gw,
                "gw_points": gw_points,
                "total_xpts": round(total_xpts, 1),
            })

        def next_gw_xpts(row):
            gw_list = row.get("gw_points", [])
            if not gw_list:
                return -1
            v = gw_list[0].get("xpts")
            return float(v) if v is not None else -1

        filters_result.sort(key=next_gw_xpts, reverse=True)

    return render_template("predictor.html", filters_result=filters_result, search_result=None)


if __name__ == "__main__":
    app.run(debug=True, port=5050)
