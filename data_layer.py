"""
TeamPicks_MLB — Capa de datos (modulo)
Version: 0.1.0

Estrategia: Moneyline / desequilibrio pitcheo-ofensiva.
Este modulo NO imprime: devuelve estructuras de datos para que app.py las
sirva como JSON. Cada capa esta marcada segun su estado de validacion.

[VERIFICADO]        MLB Stats API: slate+probables, mano del pitcher, game logs
[VERIFICAR EN RENDER] pybaseball/FanGraphs: FIP/WHIP/xFIP, gmLI/SV, crosswalk IDs
[STUB]              fetch_team_wrcplus_split(): pendiente capturar payload FanGraphs
"""
from __future__ import annotations
import json
import urllib.request
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------------------
# PARAMETROS CONGELADOS (unica fuente de verdad de la estrategia)
# ---------------------------------------------------------------------------
FIP_CUT          = 3.50   # abridor bueno si < ; malo si >
WHIP_GOOD_CUT    = 1.50   # abridor bueno si WHIP < 1.50
WHIP_BAD_CUT     = 1.50   # abridor malo  si WHIP >= 1.50  (banda "1.50 o mas")
WRC_HOT_CUT      = 105     # ofensiva encendida si wRC+ > 105
WRC_WEAK_CUT     = 105     # ofensiva debil     si wRC+ < 105

WRC_WINDOW_DAYS  = 20      # ventana ofensiva
WRC_MIN_PA       = 100     # guard: PA minimas vs esa mano; si menos -> fallback temporada

FATIGUE_WINDOW           = 5    # ventana de la regla 2
FATIGUE_APPEARANCES      = 3    # fatigado si lanzo en >=3 de los ultimos 5 dias
FATIGUE_MAX_PITCHES_LAST = 30   # fatigado si >30 pitcheos en su ultima salida
CLOSERS_NEEDED_AVAILABLE = 2    # aprueba si >=2 de 3 cerradores estan listos

MLB = "https://statsapi.mlb.com/api/v1"


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req, timeout=25))


# ===========================================================================
# CAPA 1 — MLB STATS API   [VERIFICADO]
# ===========================================================================
def get_slate(day: date) -> list[dict]:
    url = (f"{MLB}/schedule?sportId=1&date={day.isoformat()}"
           f"&hydrate=probablePitcher,team")
    data = _get(url)
    games = []
    for d in data.get("dates", []):
        for g in d["games"]:
            def side(s):
                t = s["team"]; sp = s.get("probablePitcher") or {}
                return {"name": t["name"], "abbr": t.get("abbreviation"),
                        "sp_id": sp.get("id"), "sp_name": sp.get("fullName")}
            games.append({"gamePk": g["gamePk"],
                          "fecha": g.get("officialDate", day.isoformat()),
                          "away": side(g["teams"]["away"]),
                          "home": side(g["teams"]["home"])})
    return games


def get_pitch_hand(pid: int | None) -> str | None:
    if not pid:
        return None
    p = _get(f"{MLB}/people/{pid}")["people"][0]
    return p.get("pitchHand", {}).get("code")


def get_pitching_logs(pid: int, season: int) -> list[dict]:
    url = (f"{MLB}/people/{pid}/stats?stats=gameLog&group=pitching"
           f"&season={season}")
    data = _get(url)
    out = []
    for blk in data.get("stats", []):
        for s in blk.get("splits", []):
            st = s.get("stat", {})
            out.append({"date": datetime.strptime(s["date"], "%Y-%m-%d").date(),
                        "pitches": st.get("numberOfPitches") or 0})
    return out


# ===========================================================================
# CAPA 2 — FATIGA DE BULLPEN   (logica local sobre logs VERIFICADOS)
# ===========================================================================
def is_fatigued(logs: list[dict], asof: date) -> tuple[bool, list[str]]:
    prior = sorted(d["date"] for d in logs if d["date"] < asof)
    if not prior:
        return False, []
    pitches = {d["date"]: d["pitches"] for d in logs}
    reasons: list[str] = []

    last = prior[-1]
    if pitches.get(last, 0) > FATIGUE_MAX_PITCHES_LAST:
        reasons.append(f"R3: {pitches[last]} pitcheos en ultima salida ({last})")

    s = set(prior)
    if (asof - timedelta(days=1)) in s and (asof - timedelta(days=2)) in s:
        reasons.append("R1: back-to-back (ayer y antier)")

    lo = asof - timedelta(days=FATIGUE_WINDOW)
    cnt = len([d for d in prior if lo <= d <= asof - timedelta(days=1)])
    if cnt >= FATIGUE_APPEARANCES:
        reasons.append(f"R2: {cnt} apariciones en ultimos 5 dias")

    return (len(reasons) > 0), reasons


def bullpen_status(closer_ids: list[int], season: int, asof: date) -> dict:
    detail = []; available = 0
    for pid in closer_ids[:3]:
        fat, why = is_fatigued(get_pitching_logs(pid, season), asof)
        if not fat:
            available += 1
        detail.append({"id": pid, "fatigado": fat, "motivos": why})
    return {"detalle": detail, "disponibles": available,
            "aprueba": available >= CLOSERS_NEEDED_AVAILABLE}


# ===========================================================================
# CAPA 3 — FANGRAPHS via pybaseball   [VERIFICAR EN RENDER]
# ===========================================================================
def get_starter_metrics(df, mlbam_ids: list[int]) -> dict:
    """df = salida de pitching_stats() (se jala UNA vez en validate_game)."""
    import pandas as pd
    from pybaseball import playerid_reverse_lookup
    ids = [str(i) for i in mlbam_ids if i]
    xwalk = playerid_reverse_lookup(ids, key_type="mlbam")
    fg_by_mlbam = {int(r.key_mlbam): int(r.key_fangraphs)
                   for r in xwalk.itertuples() if pd.notna(r.key_fangraphs)}
    out = {}
    for mid in mlbam_ids:
        fg = fg_by_mlbam.get(mid)
        out[mid] = None
        if fg is not None:
            row = df[df["IDfg"] == fg]
            if not row.empty:
                r = row.iloc[0]
                out[mid] = {"FIP": round(float(r["FIP"]), 2),
                            "WHIP": round(float(r["WHIP"]), 2),
                            "xFIP": round(float(r["xFIP"]), 2)}
    return out


def get_top_closers(df, team_abbr: str) -> dict:
    """Top 3 por gmLI (operativo) y por SV (referencia). Devuelve name + mlbam."""
    import pandas as pd
    from pybaseball import playerid_reverse_lookup
    pen = df[df["Team"] == team_abbr].copy()

    def top3(col):
        if pen.empty or col not in pen.columns:
            return []
        sub = pen.sort_values(col, ascending=False).head(3)
        fg_ids = [str(int(i)) for i in sub["IDfg"]]
        xwalk = playerid_reverse_lookup(fg_ids, key_type="fangraphs")
        mlbam = {int(r.key_fangraphs): int(r.key_mlbam)
                 for r in xwalk.itertuples() if pd.notna(r.key_mlbam)}
        return [{"name": str(n), "mlbam": mlbam.get(int(fg))}
                for n, fg in zip(sub["Name"], sub["IDfg"])]

    return {"by_gmLI": top3("gmLI"), "by_SV": top3("SV")}


# ===========================================================================
# CAPA 4 — wRC+ DE EQUIPO POR MANO   [STUB — Fase 3]
# ===========================================================================
def fetch_team_wrcplus_split(team_abbr: str, vs_hand: str | None, asof: date) -> dict:
    """Debe devolver {'wrc_plus': float, 'pa': int, 'source': '20d'|'season'}.
    Ventana 20d -> guard 100 PA -> fallback temporada. Fuente: Splits
    Leaderboards de FanGraphs (payload por capturar en la Fase 3).
    """
    raise NotImplementedError("Pendiente: capturar payload del Splits Leaderboard")


# ===========================================================================
# CAPA 5 — COMPUERTA
# ===========================================================================
def evaluate_pick(pick: dict, opp: dict) -> dict:
    sp_p, sp_o = pick.get("metrics"), opp.get("metrics")
    wp, wo = pick.get("wrc"), opp.get("wrc")
    c = {
        "abridor_propio_FIP<3.50":  (sp_p["FIP"] < FIP_CUT) if sp_p else None,
        "abridor_propio_WHIP<1.50": (sp_p["WHIP"] < WHIP_GOOD_CUT) if sp_p else None,
        "abridor_rival_FIP>3.50":   (sp_o["FIP"] > FIP_CUT) if sp_o else None,
        "abridor_rival_WHIP>=1.50": (sp_o["WHIP"] >= WHIP_BAD_CUT) if sp_o else None,
        "ofensiva_propia_wRC+>105": (wp["wrc_plus"] > WRC_HOT_CUT) if wp else None,
        "ofensiva_rival_wRC+<105":  (wo["wrc_plus"] < WRC_WEAK_CUT) if wo else None,
        "bullpen_>=2_listos":       pick.get("bullpen", {}).get("aprueba"),
    }
    if any(v is None for v in c.values()):
        verdict = "PENDIENTE (falta wRC+ y/o datos)"
    elif all(c.values()):
        verdict = f"PICK {pick['abbr']}"
    else:
        verdict = "no califica"
    return {"verdict": verdict, "condiciones": c}


# ===========================================================================
# ORQUESTADOR — valida UN juego (acota costo y memoria en free tier)
# ===========================================================================
def validate_game(game: dict, season: int, asof: date) -> dict:
    a, h = game["away"], game["home"]
    res = {"gamePk": game["gamePk"], "fecha": game["fecha"],
           "matchup": f'{a["abbr"]} @ {h["abbr"]}',
           "abridores": f'{a["sp_name"]} vs {h["sp_name"]}',
           "capas": {}}

    # 2A — manos (VERIFICADO)
    try:
        a["hand"] = get_pitch_hand(a["sp_id"]); h["hand"] = get_pitch_hand(h["sp_id"])
        res["manos"] = {a["abbr"]: a["hand"], h["abbr"]: h["hand"]}
        res["capas"]["manos"] = "ok"
    except Exception as e:
        a["hand"] = h["hand"] = None
        res["capas"]["manos"] = f"error: {e}"

    # FanGraphs: una sola jalada de pitching_stats para todo el juego
    df = None
    try:
        from pybaseball import pitching_stats, cache
        cache.enable()
        df = pitching_stats(season, season, qual=0)
        res["capas"]["fangraphs_pitching"] = f"ok ({len(df)} filas, {len(df.columns)} cols)"
    except Exception as e:
        res["capas"]["fangraphs_pitching"] = f"error: {type(e).__name__}: {e}"

    # 2B — abridores
    if df is not None:
        try:
            m = get_starter_metrics(df, [a["sp_id"], h["sp_id"]])
            a["metrics"] = m.get(a["sp_id"]); h["metrics"] = m.get(h["sp_id"])
            res["abridores_metrics"] = {a["abbr"]: a["metrics"], h["abbr"]: h["metrics"]}
            res["capas"]["abridores"] = "ok"
        except Exception as e:
            res["capas"]["abridores"] = f"error: {type(e).__name__}: {e}"

    # 2C — cerradores + fatiga
    if df is not None:
        for team in (a, h):
            try:
                closers = get_top_closers(df, team["abbr"])
                ids = [c["mlbam"] for c in closers["by_gmLI"] if c["mlbam"]]
                team["bullpen"] = bullpen_status(ids, season, asof)
                team["bullpen"]["listas"] = closers
            except Exception as e:
                team["bullpen"] = {"aprueba": None, "error": str(e)}
        res["bullpen"] = {a["abbr"]: a.get("bullpen"), h["abbr"]: h.get("bullpen")}
        res["capas"]["cerradores"] = "ok"

    # CAPA 4 — wRC+ (STUB): ofensiva de cada uno vs la mano del abridor rival
    for off, vs in ((a, h), (h, a)):
        try:
            off["wrc"] = fetch_team_wrcplus_split(off["abbr"], vs.get("hand"), asof)
        except NotImplementedError:
            off["wrc"] = None
    res["capas"]["wrc_plus"] = "STUB (pendiente Fase 3)"

    # CAPA 5 — compuerta (auto-excluyente: a lo mas un lado pasa)
    if a.get("metrics") is not None or h.get("metrics") is not None:
        res["compuerta"] = {a["abbr"]: evaluate_pick(a, h),
                            h["abbr"]: evaluate_pick(h, a)}

    return res
