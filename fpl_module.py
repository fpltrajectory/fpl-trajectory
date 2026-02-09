# fpl_module.py
print("THIS fpl_module.py IS LOADING")
import pandas as pd
import requests
import math
from scipy.stats import norm

TEAM_COLORS_BY_NAME = {
    "Arsenal": "#c8102e",
    "Aston Villa": "#6cabdd",
    "Bournemouth": "#1b458f",
    "Brentford": "#e30613",
    "Brighton": "#0057b8",
    "Chelsea": "#034694",
    "Crystal Palace": "#1f3c88",
    "Everton": "#003399",
    "Fulham": "#000000",
    "Liverpool": "#e03a3e",
    "Manchester City": "#6cabdd",
    "Manchester United": "#da291c",
    "Newcastle United": "#241f20",
    "Nottingham Forest": "#ffcd00",
    "Tottenham Hotspur": "#d71920",
    "West Ham United": "#7a263a",
    "Wolverhampton Wanderers": "#fdb913",
    # add promoted teams etc as needed
}

def team_color(team_id):
    ensure_loaded()
    name = team_name(team_id)
    return TEAM_COLORS_BY_NAME.get(name, "#d9d9d9")


# --------------------------
# LAZY LOADED GLOBALS
# --------------------------
_loaded = False

players = []
teams = []
fixtures = []
data = {}

team_xG_per_game = {}
team_xGC_per_game = {}
player_hist_df = None

_FIX_BY_GW_TEAM = {}      # fixture index built after fixtures load
_FPL_TEAM_NAME_TO_ID = {} # team name -> id map built after teams load

_fpl_session = requests.Session()

def ensure_loaded():
    global _loaded, players, teams, fixtures, data
    global team_xG_per_game, team_xGC_per_game, player_hist_df
    global _FIX_BY_GW_TEAM, _FPL_TEAM_NAME_TO_ID

    if _loaded:
        return

    # 1) Load CSVs (local disk)
    att_df = pd.read_csv("FPLXGXGA1.csv", sep=",", quotechar='"')
    att_df.columns = [c.strip().lower() for c in att_df.columns]

    def clean_numeric(series):
        return (
            series.astype(str)
            .str.extract(r"([0-9]+(?:[.,][0-9]+)?)")[0]
            .str.replace(",", ".", regex=False)
            .astype(float)
        )

    def_df = att_df[["team", "matches", "goals", "ga", "xga"]].copy()
    att_only = att_df[["team", "matches", "goals", "xg"]].copy()

    def_df.columns = ["NAME", "PLAYED", "GOALS", "GA", "XGA"]
    att_only.columns = ["NAME", "PLAYED", "GOALS", "XG"]

    def_df["PLAYED"] = def_df["PLAYED"].astype(int)
    att_only["PLAYED"] = att_only["PLAYED"].astype(int)

    att_only["XG"] = clean_numeric(att_only["XG"])
    def_df["XGA"] = clean_numeric(def_df["XGA"])

    team_xG_per_game = {r["NAME"]: r["XG"] / r["PLAYED"] for _, r in att_only.iterrows()}
    team_xGC_per_game = {r["NAME"]: r["XGA"] / r["PLAYED"] for _, r in def_df.iterrows()}

    # 2) Load player historical CSV
    player_hist_df = pd.read_csv("league-players20242025VS.csv", sep=";")
    player_hist_df.columns = player_hist_df.columns.str.strip().str.lower()
    player_hist_df["player_key"] = player_hist_df["player"].astype(str).str.lower().str.strip()

    # 3) Load FPL API (network)
    bootstrap_url = "https://fantasy.premierleague.com/api/bootstrap-static/"
    data = _fpl_session.get(bootstrap_url, timeout=10).json()
    players = data["elements"]
    teams = data["teams"]

    fixtures = _fpl_session.get("https://fantasy.premierleague.com/api/fixtures/", timeout=10).json()

    # 4) Build lookup maps
    _FPL_TEAM_NAME_TO_ID = {t["name"]: t["id"] for t in teams}

    _FIX_BY_GW_TEAM = {}
    for fx in fixtures:
        gw = fx.get("event")
        th = fx.get("team_h")
        ta = fx.get("team_a")
        if gw is None or th is None or ta is None:
            continue
        _FIX_BY_GW_TEAM.setdefault((gw, th), []).append(fx)
        _FIX_BY_GW_TEAM.setdefault((gw, ta), []).append(fx)

    _loaded = True




# --------------------------
# FAST LOOKUPS + CACHES
# --------------------------
from functools import lru_cache


def get_player_fixtures(player, gw):
    ensure_loaded()
    team_id = player["team"]
    return _FIX_BY_GW_TEAM.get((gw, team_id), [])


@lru_cache(maxsize=4096)
def fixture_totals_with_bonus(event, team_h, team_a):
    ensure_loaded()
    """
    Compute total_xPts_with_bonus for ALL players in this fixture once,
    then reuse it for every player lookup.
    Returns dict: {player_id: total_xPts_with_bonus}
    """
    # find the exact fixture dict
    fx = None
    for f in fixtures:
        if f.get("event") == event and f.get("team_h") == team_h and f.get("team_a") == team_a:
            fx = f
            break
    if not fx:
        return {}

    # breakdown for everyone in fixture (once)
    breakdowns_by_id = {}
    for p in players:
        if p.get("team") not in (team_h, team_a):
            continue
        if float(p.get("minutes") or 0) <= 0:
            continue
        bd = xpts_breakdown(p, fx)
        if bd:
            breakdowns_by_id[p["id"]] = bd

    if not breakdowns_by_id:
        return {}

    bonus_by_id = expected_bonus_points_for_fixture(fx, breakdowns_by_id, temp=BONUS_SOFTMAX_TEMP)

    out = {}
    for pid, bd in breakdowns_by_id.items():
        out[pid] = round(float(bd["total_xPts"]) + float(bonus_by_id.get(pid, 0.0)), 2)

    return out


POSITION_MAP = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}


def fixture_label_for_gw(player, gw):
    """Nice label: 'vs ABC (H) + vs DEF (A)' for DGWs."""
    fxs = get_player_fixtures(player, gw)
    if not fxs:
        return "—"
    parts = [fixture_opp_label(player, fx) for fx in fxs]
    return " + ".join(parts)


def team_name(team_id):
    ensure_loaded()
    return next(t["name"] for t in teams if t["id"] == team_id)


def get_team_id_by_name(name):
    ensure_loaded()
    return _FPL_TEAM_NAME_TO_ID.get(name)


FPL_POINTS = {
    "goal": {"GK": 6, "DEF": 6, "MID": 5, "FWD": 4},
    "assist": 3,
    "clean_sheet": {"GK": 4, "DEF": 4, "MID": 1},
}

# --------------------------
# LOW-MINUTES SAFETY (EXISTING)
# --------------------------
BASELINE_XG90 = {"GK": 0.02, "DEF": 0.05, "MID": 0.18, "FWD": 0.40}
BASELINE_XA90 = {"GK": 0.02, "DEF": 0.10, "MID": 0.25, "FWD": 0.20}

MIN_TRUST_MINUTES = 180
SHRINK_K = 450

CAP_XG90 = 0.90
CAP_XA90 = 0.70

# --------------------------
# LOW-MINUTES SAFETY (NEW: minutes projection + defcon shrink)
# --------------------------
MIN_RELIABILITY_M0 = 600.0  # ~6-7 full matches

DEFCON_PRIOR_PER90 = {"DEF": 6.0, "MID": 5.0, "GK": 0.0, "FWD": 0.0}

XG_PRIOR_PER90 = {"GK": 0.00, "DEF": 0.06, "MID": 0.20, "FWD": 0.35}
XA_PRIOR_PER90 = {"GK": 0.00, "DEF": 0.05, "MID": 0.18, "FWD": 0.12}


def reliability_weight(minutes, m0=MIN_RELIABILITY_M0):
    ensure_loaded()
    """0..1 weight. Low minutes -> close to 0 (use priors), high minutes -> close to 1."""
    m = float(minutes or 0)
    if m <= 0:
        return 0.0
    return m / (m + float(m0))


# --------------------------
# HISTORICAL DATA GUARDS + MINUTES-BASED SHRINK FOR PER-90
# --------------------------
HIST_MIN_MINUTES_TO_TRUST = 300.0
HIST_MIN_APPS_TO_TRUST = 5.0
HIST_MAX_XG90 = 1.2
HIST_MAX_XA90 = 1.5

# Proportional penalty (already used in xPts)
MIN_PENALTY_MINUTES_M0 = 900.0
MIN_PENALTY_STARTS_S0 = 6.0
MIN_PENALTY_FLOOR = 0.35
MIN_PENALTY_POWER = 1.15


def safe_float(x, default=None):
    ensure_loaded()
    try:
        if x is None:
            return default
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def hist_row_sample_ok(hist_row):
    ensure_loaded()
    if hist_row is None:
        return False
    hmins = safe_float(hist_row.get("min"), 0.0) or 0.0
    happs = safe_float(hist_row.get("apps"), 0.0) or 0.0
    return (hmins >= HIST_MIN_MINUTES_TO_TRUST) and (happs >= HIST_MIN_APPS_TO_TRUST)


def guarded_hist_per90(hist_row):
    ensure_loaded()
    if not hist_row_sample_ok(hist_row):
        return None, None

    hxg90 = safe_float(hist_row.get("xg90"), None)
    hxa90 = safe_float(hist_row.get("xa90"), None)

    if hxg90 is not None and not (0.0 <= hxg90 <= HIST_MAX_XG90):
        hxg90 = None
    if hxa90 is not None and not (0.0 <= hxa90 <= HIST_MAX_XA90):
        hxa90 = None

    return hxg90, hxa90


# --------------------------
# BONUS MODEL TUNING (NEW)
# --------------------------
# 1) Lower temperature => stars win bonus more often (less "flat" probability)
BONUS_SOFTMAX_TEMP = 4.0

# 2) Add a stable "bonus tendency" adjustment using FPL season BPS & bonus totals
BONUS_PRIOR_M0 = 1100.0  # larger => stronger shrink for low minutes

# Priors for bps/bonus per90 (rough but safe)
BPS_PER90_PRIOR = {"GK": 16.0, "DEF": 18.0, "MID": 16.0, "FWD": 14.5}
BONUS_PER90_PRIOR = {"GK": 0.20, "DEF": 0.22, "MID": 0.22, "FWD": 0.25}

# Coefficients mapping bps/bonus tendency into your "bps-like score" space
BPS_STYLE_COEF = 0.30
BONUS_TEND_COEF = 5.00

# 3) Make goals matter a bit more for FWD so "scores -> bonus" happens more often
GOAL_W = {"GK": 16.0, "DEF": 18.0, "MID": 16.0, "FWD": 17.0}
ASSIST_W = 10.0

# Existing pieces (CS, defcon) kept
CS_W = {"GK": 10.0, "DEF": 10.0, "MID": 4.0}
DEFCON_W = 6.0


def bps_per90(player):
    ensure_loaded()
    mins = float(player.get("minutes") or 0.0)
    if mins <= 0:
        return 0.0
    return float(player.get("bps") or 0.0) / (mins / 90.0)


def bonus_per90(player):
    ensure_loaded()
    mins = float(player.get("minutes") or 0.0)
    if mins <= 0:
        return 0.0
    return float(player.get("bonus") or 0.0) / (mins / 90.0)


def bonus_tendency_score(player, minute_factor):
    ensure_loaded()
    """
    Returns an additive bps-like score adjustment.
    Uses season bps/bonus per90, shrunk strongly for low minutes.
    """
    pos = POSITION_MAP[player["element_type"]]
    mins = float(player.get("minutes") or 0.0)
    w = mins / (mins + BONUS_PRIOR_M0) if mins > 0 else 0.0

    style_prior = float(BPS_PER90_PRIOR.get(pos, 16.0))
    bon_prior = float(BONUS_PER90_PRIOR.get(pos, 0.22))

    style = bps_per90(player)
    bon = bonus_per90(player)

    style_shrunk = w * style + (1.0 - w) * style_prior
    bon_shrunk = w * bon + (1.0 - w) * bon_prior

    # Centered adjustments so priors add ~0 effect
    style_adj = (style_shrunk - style_prior)
    bon_adj = (bon_shrunk - bon_prior)

    return (BPS_STYLE_COEF * style_adj + BONUS_TEND_COEF * bon_adj) * float(minute_factor)


# --------------------------
# BONUS HELPERS
# --------------------------
def softmax(xs, temp=8.0):
    ensure_loaded()
    xs = [float(x) for x in xs]
    if not xs:
        return []
    m = max(xs)
    exps = [math.exp((x - m) / float(temp)) for x in xs]
    s = sum(exps) or 1.0
    return [e / s for e in exps]


# --------------------------
# LOW-MINUTES PENALTY (PROPORTIONAL)
# --------------------------
def combined_confidence_multiplier(player):
    ensure_loaded()
    """
    0.35..1.0 multiplier.
    Low season minutes + low starts -> closer to floor, so their goals/xG/etc are rewarded less.
    """
    mins = float(player.get("minutes") or 0.0)
    starts = float(player.get("starts") or 0.0)

    w_mins = mins / (mins + MIN_PENALTY_MINUTES_M0) if mins > 0 else 0.0
    w_starts = starts / (starts + MIN_PENALTY_STARTS_S0) if starts > 0 else 0.0

    w = math.sqrt(w_mins * w_starts)
    w = max(0.0, min(1.0, float(w))) ** float(MIN_PENALTY_POWER)

    return float(MIN_PENALTY_FLOOR + (1.0 - MIN_PENALTY_FLOOR) * w)


def expected_bps_score(player, fixture, xG, xA, cs_prob, minute_factor, dcon_prob_val):
    ensure_loaded()
    """
    BPS-like proxy:
      - Uses xG/xA/cs/defcon just like you had
      - PLUS a minutes-shrunk season "bonus tendency" using FPL bps + bonus
      - Slightly higher goal weight for FWD (helps Haaland-type profiles)
      - Slightly reduces low-minutes volatility via your confidence multiplier
    """
    pos = POSITION_MAP[player["element_type"]]

    bps = 6.0 * float(minute_factor)

    bps += float(GOAL_W.get(pos, 15.0)) * float(xG)
    bps += float(ASSIST_W) * float(xA)

    if pos in ["GK", "DEF"]:
        bps += CS_W["DEF"] * float(cs_prob) * float(minute_factor)
    elif pos == "MID":
        bps += CS_W["MID"] * float(cs_prob) * float(minute_factor)

    if dcon_prob_val is not None:
        bps += DEFCON_W * float(dcon_prob_val) * float(minute_factor)

    # NEW: bonus tendency / style signal
    bps += bonus_tendency_score(player, minute_factor)

    # Keep your low-min stabilizer (small effect)
    conf_mult = combined_confidence_multiplier(player)
    bps *= (0.85 + 0.15 * conf_mult)

    return float(bps)


def expected_bonus_points_for_fixture(fixture, breakdowns_by_player_id, temp=BONUS_SOFTMAX_TEMP):
    ensure_loaded()
    team_h = fixture["team_h"]
    team_a = fixture["team_a"]

    match_players = [
        p for p in players
        if p.get("team") in (team_h, team_a) and float(p.get("minutes") or 0) > 0
    ]

    ids = []
    scores = []
    for p in match_players:
        pid = p.get("id")
        bd = breakdowns_by_player_id.get(pid)
        if not bd:
            continue

        xG = float(bd.get("xG") or 0.0)
        xA = float(bd.get("xA") or 0.0)
        cs_prob = float(bd.get("cs_prob") or 0.0)
        minute_factor = float(bd.get("minute_factor") or 0.0)
        dcon_prob = bd.get("defcon_prob")
        dcon_prob = None if dcon_prob is None else float(dcon_prob)

        score = expected_bps_score(p, fixture, xG, xA, cs_prob, minute_factor, dcon_prob)
        ids.append(pid)
        scores.append(score)

    if not ids:
        return {}

    p1 = softmax(scores, temp=temp)
    expected_bonus = {pid: 0.0 for pid in ids}

    for pid, prob in zip(ids, p1):
        expected_bonus[pid] += 3.0 * prob

    for i, pid_i in enumerate(ids):
        ids_rest = [pid for j, pid in enumerate(ids) if j != i]
        scores_rest = [s for j, s in enumerate(scores) if j != i]
        if not ids_rest:
            continue

        p_rest = softmax(scores_rest, temp=temp)

        for pid_r, pr in zip(ids_rest, p_rest):
            expected_bonus[pid_r] += 2.0 * (p1[i] * pr)

        for k, pid_k in enumerate(ids_rest):
            p_second = p1[i] * p_rest[k]
            ids_third = [pid for j, pid in enumerate(ids_rest) if j != k]
            scores_third = [s for j, s in enumerate(scores_rest) if j != k]
            if not ids_third:
                continue
            p_third = softmax(scores_third, temp=temp)
            for pid_t, pt in zip(ids_third, p_third):
                expected_bonus[pid_t] += 1.0 * (p_second * pt)

    return expected_bonus



def xpts_breakdown_with_bonus(player, fixture):
    ensure_loaded()
    """
    Returns the same breakdown as xpts_breakdown, but adds:
      - xBonus (expected bonus points for that match)
      - total_xPts_with_bonus
    """
    # Base breakdown for the player
    bd = xpts_breakdown(player, fixture)
    if not bd:
        return None

    # Collect ALL players in this fixture (both teams) and compute their breakdowns
    team_h = fixture["team_h"]
    team_a = fixture["team_a"]

    entries = []
    for p in players:
        if p.get("team") not in (team_h, team_a):
            continue
        if float(p.get("minutes") or 0) <= 0:
            continue

        p_bd = xpts_breakdown(p, fixture)
        if p_bd:
            entries.append((p["id"], p_bd))

    breakdowns_by_id = {pid: pbd for pid, pbd in entries}

    # Expected bonus for that fixture
    bonus_by_id = expected_bonus_points_for_fixture(
        fixture,
        breakdowns_by_id,
        temp=BONUS_SOFTMAX_TEMP
    )

    xbonus = float(bonus_by_id.get(player["id"], 0.0))
    total_with_bonus = float(bd["total_xPts"]) + xbonus

    bd["xBonus"] = round(xbonus, 2)
    bd["total_xPts_with_bonus"] = round(total_with_bonus, 2)
    return bd

# --------------------------
# MINUTES PROJECTION
# --------------------------
def project_minutes_next_gw(player):
    ensure_loaded()
    mins = float(player.get("minutes") or 0)
    starts = float(player.get("starts") or 0)

    starter_prior = 72.0
    sub_prior = 22.0

    if starts >= 1 and mins > 0:
        avg = mins / starts
        avg = max(55.0, min(90.0, avg))
        w_starts = starts / (starts + 3.0)
        proj = (w_starts * avg) + ((1.0 - w_starts) * starter_prior)
        return max(10.0, min(90.0, proj))
    else:
        if mins > 0:
            return max(10.0, min(35.0, mins))
        return sub_prior


def expected_appearance_points(proj_mins):
    ensure_loaded()
    m = float(proj_mins or 0)
    k = 0.25
    p60 = 1.0 / (1.0 + math.exp(-k * (m - 60.0)))
    return 1.0 + p60


# --------------------------
# DEBUG HELPERS (FOR BUENDIA ISSUE)
# --------------------------
def debug_player_raw(name_contains="buend"):
    ensure_loaded()
    for p in players:
        if name_contains.lower() in p.get("web_name", "").lower():
            print("\nRAW PLAYER FROM FPL API")
            print("web_name:", p.get("web_name"))
            print("id:", p.get("id"))
            print("team:", team_name(p.get("team")))
            print("pos:", POSITION_MAP[p.get("element_type")])
            print("minutes:", p.get("minutes"))
            print("starts:", p.get("starts"))
            print("expected_goals:", p.get("expected_goals"))
            print("goals_scored:", p.get("goals_scored"))
            print("expected_assists:", p.get("expected_assists"))
            print("assists:", p.get("assists"))
            print("bonus:", p.get("bonus"))
            print("bps:", p.get("bps"))
            return
    print("Player not found in FPL API for:", name_contains)


def debug_team_factors_for_fixture(player, fixture):
    ensure_loaded()
    team_id = player["team"]
    opp_id = fixture["team_a"] if fixture["team_h"] == team_id else fixture["team_h"]

    league_avg_xgc = sum(team_xGC_per_game.values()) / len(team_xGC_per_game)
    opp_xgc = team_xGC_per_game.get(team_name(opp_id), league_avg_xgc)
    factor = opp_xgc / league_avg_xgc

    print("\nFIXTURE FACTOR DEBUG")
    print("player team:", team_name(team_id))
    print("opponent:", team_name(opp_id))
    print("home:", fixture["team_h"] == team_id)
    print("league_avg_xgc:", round(league_avg_xgc, 3))
    print("opp_xgc_per_game:", round(opp_xgc, 3))
    print("factor:", round(factor, 3))


# --------------------------
# CLEAN SHEET / DEFCON / XPTS
# --------------------------
def clean_sheet_prob(player, fixture):
    ensure_loaded()
    pos = POSITION_MAP[player["element_type"]]
    if pos not in ["GK", "DEF", "MID"]:
        return 0.0

    team = team_name(player["team"])
    opp_id = fixture["team_a"] if fixture["team_h"] == player["team"] else fixture["team_h"]
    opp = team_name(opp_id)
    home = fixture["team_h"] == player["team"]

    league_avg_xg = sum(team_xG_per_game.values()) / len(team_xG_per_game)

    team_xgc = team_xGC_per_game.get(team, league_avg_xg)
    opp_xg = team_xG_per_game.get(opp, league_avg_xg)

    def_strength = team_xgc / league_avg_xg
    att_strength = opp_xg / league_avg_xg

    lam = league_avg_xg * def_strength * att_strength
    lam *= 0.90 if home else 1.10

    cs_prob = math.exp(-lam)
    return max(0.05, min(cs_prob, 0.65))


def def_contrib_per90(player):
    ensure_loaded()
    mins = float(player.get("minutes") or 0)
    if mins <= 0:
        return 0.0
    return float(player.get("defensive_contribution", 0) or 0) / (mins / 90.0)


def defcon_prob(player):
    ensure_loaded()
    pos = POSITION_MAP[player["element_type"]]
    if pos not in ["DEF", "MID"]:
        return None

    mins = float(player.get("minutes") or 0)
    w = reliability_weight(mins)

    raw_per90 = def_contrib_per90(player)
    prior = DEFCON_PRIOR_PER90.get(pos, 5.0)

    shrunk_per90 = (w * raw_per90) + ((1.0 - w) * prior)

    threshold = 10.0 if pos == "DEF" else 12.0
    sigma = max(2.0, (1.0 - w) * 6.0 + w * 3.0)

    prob = 1.0 - norm.cdf(threshold, loc=shrunk_per90, scale=sigma)
    return max(0.0, min(float(prob), 1.0))


def get_last_season_row(player):
    ensure_loaded()
    key = player["web_name"].lower().strip()

    rows = player_hist_df[player_hist_df["player_key"] == key]
    if len(rows):
        return rows.iloc[0]

    rows = player_hist_df[player_hist_df["player_key"].str.startswith(key)]
    return rows.iloc[0] if len(rows) else None


def adjusted_xG_xA(player, fixture):
    ensure_loaded()
    mins = float(player.get("minutes") or 0)
    if mins <= 0:
        return 0, 0, 0, 0

    pos = POSITION_MAP[player["element_type"]]
    w_rel = reliability_weight(mins)

    cur_xG = float(player.get("expected_goals") or 0)
    cur_xA = float(player.get("expected_assists") or 0)
    goals = float(player.get("goals_scored") or 0)
    assists = float(player.get("assists") or 0)

    if mins < MIN_TRUST_MINUTES:
        cur_xG90 = BASELINE_XG90.get(pos, 0.10)
        cur_xA90 = BASELINE_XA90.get(pos, 0.10)
    else:
        cur_xG90 = cur_xG / (mins / 90.0) if mins else 0.0
        cur_xA90 = cur_xA / (mins / 90.0) if mins else 0.0

    xG90 = w_rel * cur_xG90 + (1.0 - w_rel) * XG_PRIOR_PER90.get(pos, 0.15)
    xA90 = w_rel * cur_xA90 + (1.0 - w_rel) * XA_PRIOR_PER90.get(pos, 0.10)

    hist = get_last_season_row(player)
    hxg, hxa = guarded_hist_per90(hist)

    if hxg is not None or hxa is not None:
        hist_w = min(0.60, 1.0 - w_rel)
        if hxg is not None:
            xG90 = (1.0 - hist_w) * xG90 + hist_w * hxg
        if hxa is not None:
            xA90 = (1.0 - hist_w) * xA90 + hist_w * hxa
    else:
        hist_xG90 = BASELINE_XG90.get(pos, 0.10)
        hist_xA90 = BASELINE_XA90.get(pos, 0.10)
        w = mins / (mins + SHRINK_K)
        xG90 = w * xG90 + (1.0 - w) * hist_xG90
        xA90 = w * xA90 + (1.0 - w) * hist_xA90

    if mins >= MIN_TRUST_MINUTES:
        if cur_xG > 0:
            mult_g = 1.0 + max(min((goals - cur_xG) / cur_xG, 0.25), -0.25)
            xG90 *= (w_rel * mult_g + (1.0 - w_rel) * 1.0)
        if cur_xA > 0:
            mult_a = 1.0 + max(min((assists - cur_xA) / cur_xA, 0.25), -0.25)
            xA90 *= (w_rel * mult_a + (1.0 - w_rel) * 1.0)

    xG90 = min(float(xG90), CAP_XG90)
    xA90 = min(float(xA90), CAP_XA90)

    team_id = player["team"]
    opp_id = fixture["team_a"] if fixture["team_h"] == team_id else fixture["team_h"]
    league_avg_xgc = sum(team_xGC_per_game.values()) / len(team_xGC_per_game)
    opp_xgc = team_xGC_per_game.get(team_name(opp_id), league_avg_xgc)
    factor = opp_xgc / league_avg_xgc

    return xG90, xA90, xG90 * factor, xA90 * factor


def xpts_breakdown(player, fixture):
    ensure_loaded()
    pos = POSITION_MAP[player["element_type"]]
    mins_season = float(player.get("minutes") or 0)
    if mins_season <= 0:
        return None

    proj_mins = project_minutes_next_gw(player)
    minute_factor = max(0.0, min(1.0, proj_mins / 90.0))

    xG90, xA90, xG90_adj, xA90_adj = adjusted_xG_xA(player, fixture)

    xG = xG90_adj * minute_factor
    xA = xA90_adj * minute_factor

    cs_prob = clean_sheet_prob(player, fixture) if pos in ["GK", "DEF", "MID"] else 0.0
    cs_pts = cs_prob * FPL_POINTS["clean_sheet"].get(pos, 0)

    dcon_prob_val = defcon_prob(player)
    dcon_pts = (dcon_prob_val * 2) if dcon_prob_val is not None else 0.0
    dcon_pts *= minute_factor

    appearance_pts = expected_appearance_points(proj_mins)

    goal_pts = xG * FPL_POINTS["goal"].get(pos, 0)
    assist_pts = xA * FPL_POINTS["assist"]

    conf_mult = combined_confidence_multiplier(player)
    variable_pts = goal_pts + assist_pts + cs_pts + dcon_pts
    total_pts = appearance_pts + (conf_mult * variable_pts)

    return {
        "player_id": player.get("id"),
        "name": player.get("web_name"),
        "pos": pos,
        "proj_mins": round(float(proj_mins), 1),
        "minute_factor": round(float(minute_factor), 3),
        "mins_conf_mult": round(float(conf_mult), 3),
        "xG": round(float(xG), 3),
        "xA": round(float(xA), 3),
        "cs_prob": round(float(cs_prob), 3),
        "defcon_prob": None if dcon_prob_val is None else round(float(dcon_prob_val), 3),
        "appearance_pts": round(float(appearance_pts), 2),
        "goal_pts": round(float(goal_pts), 2),
        "assist_pts": round(float(assist_pts), 2),
        "cs_pts": round(float(cs_pts), 2),
        "defcon_pts": round(float(dcon_pts), 2),
        "total_xPts": round(float(total_pts), 2),
    }


# --------------------------
# PLAYER UTILITIES
# --------------------------
def get_player_photo(player):
    ensure_loaded()
    if not player.get("photo"):
        return "/static/img/placeholder.png"
    photo_id = player["photo"].split(".")[0]
    return f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{photo_id}.png?FORCE_TEST=YES"


def get_player(name, team_name=None):
    ensure_loaded()
    name = name.lower()
    matches = [p for p in players if name in p["web_name"].lower()]
    if team_name:
        team_id = get_team_id_by_name(team_name)
        if team_id is not None:
            matches = [p for p in matches if p["team"] == team_id]

    return matches[0] if matches else None


def get_next_gameweek():
    ensure_loaded()
    for e in data["events"]:
        if e["is_next"]:
            return e["id"]

def get_active_gameweek():
    ensure_loaded()
    """
    Returns the GW you should be allowed to view:
    - If a GW is currently live and NOT finished -> return that GW
    - Otherwise -> return next GW
    """
    current = None
    nxt = None

    for e in data["events"]:
        if e.get("is_current"):
            current = e
        if e.get("is_next"):
            nxt = e

    # If there is a current GW and it isn't finished, that's the active one
    if current and not current.get("finished"):
        return current.get("id")

    # Otherwise move to next
    if nxt:
        return nxt.get("id")

    # Fallback: last event id
    return max(e.get("id", 0) for e in data["events"] if e.get("id") is not None)


def fixture_opp_label(player, fixture):
    ensure_loaded()
    try:
        player_team_id = player.get("team")
        team_h = fixture.get("team_h")
        team_a = fixture.get("team_a")
        if player_team_id is None or team_h is None or team_a is None:
            return "—"

        is_home = player_team_id == team_h
        opp_id = team_a if is_home else team_h
        opp_abbr = team_name(opp_id)[:3].upper()
        return f"vs {opp_abbr} ({'H' if is_home else 'A'})"
    except Exception:
        return "—"


def top_xpts_players_for_gw(gw, n=20, min_minutes=1):
    ensure_loaded()
    results = []

    for p in players:
        try:
            mins = float(p.get("minutes") or 0)
            if mins < min_minutes:
                continue

            fxs = get_player_fixtures(p, gw)
            if not fxs:
                continue

            total = 0.0
            breakdowns = []

            for fx in fxs:
                event = fx.get("event")
                team_h = fx.get("team_h")
                team_a = fx.get("team_a")

                # fast cached: totals for every player in the fixture
                totals_map = fixture_totals_with_bonus(event, team_h, team_a)
                total += float(totals_map.get(p["id"], 0.0))

                # optional: keep per-fixture breakdown for debugging/UI later
                bd = xpts_breakdown(p, fx)
                if bd:
                    bd = dict(bd)
                    bd["opp"] = fixture_opp_label(p, fx)
                    breakdowns.append(bd)

            results.append(
                {
                    "player_id": p["id"],
                    "name": p.get("web_name"),
                    "team": team_name(p["team"]),
                    "team_id": p.get("team"),
                    "position": POSITION_MAP[p["element_type"]],
                    "price": (p.get("now_cost", 0) / 10),
                    "owned_by": float(p.get("selected_by_percent") or 0.0),
                    "opp": fixture_label_for_gw(p, gw),
                    "breakdowns": breakdowns,
                    "total_xPts": round(total, 2),
                }
            )

        except Exception:
            continue

    results.sort(key=lambda r: r["total_xPts"], reverse=True)
    return results[: max(1, int(n))]


if __name__ == "__main__":
    gw = get_next_gameweek()

    # 1) Raw FPL API values for Buendia
    debug_player_raw("buend")

    # 2) Fixture factor details for Buendia's next GW fixture
    buend = get_player("buend")
    fx = get_player_fixtures(buend, gw)[0]
    debug_team_factors_for_fixture(buend, fx)

    # 3) Top 100 list
    top = top_xpts_players_for_gw(gw, n=100)

    print("\n==============================")
    print(f"TOP 100 EXPECTED POINTS – GW {gw}")
    print("==============================")

    for i, p in enumerate(top, start=1):
        bd = p["breakdown"]

        bonus = float(bd.get("xBonus", 0.0))
        conf = float(bd.get("mins_conf_mult", 1.0))

        goal_pts = float(bd.get("goal_pts", 0.0))
        assist_pts = float(bd.get("assist_pts", 0.0))
        cs_pts = float(bd.get("cs_pts", 0.0))
        defcon_pts = float(bd.get("defcon_pts", 0.0))
        appearance_pts = float(bd.get("appearance_pts", 0.0))

        variable_raw = goal_pts + assist_pts + cs_pts + defcon_pts
        variable_adj = conf * variable_raw
        base_adj = appearance_pts + variable_adj
        total_calc = base_adj + bonus

        print(
            f"{i:>3}. {p['name']:<18} {p['position']} {p['opp']:<12} "
            f"| Tot {float(p['total_xPts']):>5.2f} (chk {total_calc:>5.2f}) "
            f"| mins {bd.get('proj_mins', 0):>5} "
            f"| conf {conf:>4.2f} "
            f"| G {goal_pts:>4.2f} A {assist_pts:>4.2f} CS {cs_pts:>4.2f} DC {defcon_pts:>4.2f} "
            f"| var*conf {variable_adj:>4.2f} "
            f"| app {appearance_pts:>4.2f} "
            f"| bon {bonus:>4.2f}"
        )

    print("==============================\n")

