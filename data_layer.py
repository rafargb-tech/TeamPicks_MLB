"""
TeamPicks_MLB — Capa de datos (modulo)
Version: 0.4.0

Estrategia: Moneyline / desequilibrio pitcheo-ofensiva.
Este modulo NO imprime: devuelve estructuras para que app.py las sirva como JSON.

CAMBIO v0.4.0: scan_slate() escanea el slate completo y devuelve los picks del
dia. FanGraphs se pre-carga una vez por slate; el bullpen se evalua de forma
perezosa (solo para lados que ya pasaron lo demas).
CAMBIO v0.3.0: wRC+ por mano implementado (fallback de temporada via API
principal con month=13/14 + team=0,ts). Primario de 20 dias pendiente (Fase 3b).
CAMBIO v0.2.1: R3 (fatiga por conteo) pasa a modo ESTRICTO via
FATIGUE_R3_RECENCY_DAYS: solo aplica si la salida pesada fue ayer.
CAMBIO v0.2.0: se elimino pybaseball. Toda la data de FanGraphs se obtiene
del API moderno JSON (el endpoint legacy .aspx devuelve 403). El API moderno
incluye xMLBAMID, asi que ya no hace falta crosswalk de IDs.

[VERIFICADO]   MLB Stats API: slate+probables, mano del pitcher, game logs, roster
[VERIFICADO]   FanGraphs API moderno: FIP/WHIP/xFIP/gmLI/SV (campos confirmados)
[STUB]         fetch_team_wrcplus_split(): pendiente Fase 3 (Splits Leaderboards)
"""
from __future__ import annotations
import re
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
MONTH_VS_L       = 13      # "month" en API FanGraphs = split vs LHP
MONTH_VS_R       = 14      # "month" en API FanGraphs = split vs RHP

FATIGUE_WINDOW           = 5    # ventana de la regla 2
FATIGUE_APPEARANCES      = 3    # fatigado si lanzo en >=3 de los ultimos 5 dias
FATIGUE_MAX_PITCHES_LAST = 30   # fatigado si >30 pitcheos en su ultima salida
FATIGUE_R3_RECENCY_DAYS  = 1    # R3 solo aplica si esa salida pesada fue dentro
                                # de N dias. ESTRICTO=1 (solo ayer). [DOCUMENTAR]
CLOSERS_NEEDED_AVAILABLE = 2    # aprueba si >=2 de 3 cerradores estan listos

MLB = "https://statsapi.mlb.com/api/v1"
FG  = "https://www.fangraphs.com/api/leaders/major-league/data"

# Abreviaturas que difieren entre MLB Stats API y FanGraphs
MLB_TO_FG_ABBR = {"AZ": "ARI", "CWS": "CHW", "KC": "KCR", "SD": "SDP",
                  "SF": "SFG", "TB": "TBR", "WSH": "WSN",
                  "OAK": "ATH", "SAC": "ATH"}
UA  = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120 Safari/537.36")}


def _get(url: str) -> dict:
    return json.load(urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=30))


def _strip(s) -> str:
    return re.sub("<[^>]+>", "", str(s)).strip()


def _f(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


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
                        "id": t.get("id"),
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


def get_team_pitcher_ids(team_id: int, season: int) -> set[int]:
    """IDs MLBAM de los pitchers en el roster activo (para ubicar cerradores)."""
    data = _get(f"{MLB}/teams/{team_id}/roster?rosterType=active&season={season}")
    ids = set()
    for p in data.get("roster", []):
        if p.get("position", {}).get("type") == "Pitcher":
            ids.add(p["person"]["id"])
    return ids


# ===========================================================================
# CAPA 2 — FATIGA DE BULLPEN   (logica local sobre logs VERIFICADOS)
# ===========================================================================
def is_fatigued(logs: list[dict], asof: date) -> tuple[bool, list[str]]:
    prior = sorted(d["date"] for d in logs if d["date"] < asof)
    if not prior:
        return False, []
    pitches = {d["date"]: d["pitches"] for d in logs}
    reasons: list[str] = []

    # R3 (modo ESTRICTO): solo cuenta si la salida pesada fue reciente.
    # Una sola salida de +30 pitcheos cansa el dia siguiente; a 2+ dias ya
    # esta disponible. La carga acumulada la cubren R1/R2.
    last = prior[-1]
    if (pitches.get(last, 0) > FATIGUE_MAX_PITCHES_LAST
            and last >= asof - timedelta(days=FATIGUE_R3_RECENCY_DAYS)):
        reasons.append(f"R3: {pitches[last]} pitcheos el {last}")

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
# CAPA 3 — FANGRAPHS API MODERNO   [VERIFICADO]
# ===========================================================================
def fetch_fangraphs_pitching(season: int) -> list[dict]:
    """Una sola llamada: todos los pitchers de la liga con sus metricas."""
    url = (f"{FG}?pos=all&stats=pit&lg=all&qual=0"
           f"&season={season}&season1={season}"
           f"&type=8&pageitems=3000&pagenum=1&month=0&team=0&ind=0")
    raw = _get(url)
    rows = []
    for r in raw.get("data", []):
        mid = r.get("xMLBAMID")
        rows.append({
            "mlbam": int(mid) if mid is not None else None,
            "name": _strip(r.get("Name")),
            "throws": r.get("Throws"),
            "FIP": _f(r.get("FIP")), "WHIP": _f(r.get("WHIP")),
            "xFIP": _f(r.get("xFIP")), "gmLI": _f(r.get("gmLI")),
            "SV": _f(r.get("SV")), "IP": _f(r.get("IP")),
        })
    return rows


def get_starter_metrics(rows: list[dict], mlbam_ids: list[int]) -> dict:
    by_id = {r["mlbam"]: r for r in rows if r["mlbam"] is not None}
    out = {}
    for mid in mlbam_ids:
        r = by_id.get(mid)
        out[mid] = {"FIP": r["FIP"], "WHIP": r["WHIP"], "xFIP": r["xFIP"]} if r else None
    return out


def get_top_closers(rows: list[dict], team_pitcher_ids: set[int]) -> dict:
    """Top 3 por gmLI (operativo) y por SV (referencia), entre los pitchers
    del roster del equipo. Sin cruce de abreviaturas: se filtra por MLBAM ID."""
    pen = [r for r in rows if r["mlbam"] in team_pitcher_ids]

    def top3(key):
        ranked = sorted([r for r in pen if r.get(key) is not None],
                        key=lambda r: r[key], reverse=True)[:3]
        return [{"name": r["name"], "mlbam": r["mlbam"], key: r[key]} for r in ranked]

    return {"by_gmLI": top3("gmLI"), "by_SV": top3("SV")}


# ===========================================================================
# CAPA 4 — wRC+ DE EQUIPO POR MANO  (fallback temporada; primario 20d = Fase 3b)
# ===========================================================================
def _team_wrc_by_hand(season: int, vs_hand: str) -> dict:
    """{fg_abbr: {'wrc_plus','pa'}} de TODOS los equipos vs una mano (temporada)."""
    month = MONTH_VS_L if vs_hand == "L" else MONTH_VS_R
    url = (f"{FG}?pos=all&stats=bat&lg=all&qual=0&type=8"
           f"&season={season}&season1={season}&ind=0&pageitems=2000"
           f"&month={month}&team=0,ts")
    raw = _get(url)
    out = {}
    for r in raw.get("data", []):
        out[_strip(r.get("Team"))] = {"wrc_plus": _f(r.get("wRC+")),
                                      "pa": int(r.get("PA") or 0)}
    return out


def fetch_team_wrc_tables(season: int) -> dict:
    """Pre-carga las dos tablas (vs L y vs R) UNA vez para todo el slate."""
    return {"L": _team_wrc_by_hand(season, "L"),
            "R": _team_wrc_by_hand(season, "R")}


def _wrc_lookup(tables: dict, team_abbr: str, vs_hand: str | None) -> dict | None:
    """Busca wRC+ del equipo vs una mano en las tablas pre-cargadas.
    PRIMARIO 20d: pendiente (Fase 3b). FALLBACK temporada: aqui, source='season'.
    """
    if vs_hand not in ("L", "R") or not tables:
        return None
    fg = MLB_TO_FG_ABBR.get(team_abbr, team_abbr)
    row = tables.get(vs_hand, {}).get(fg)
    if not row or row["wrc_plus"] is None:
        return None
    return {"wrc_plus": row["wrc_plus"], "pa": row["pa"], "source": "season"}


def fetch_team_wrcplus_split(team_abbr, vs_hand, asof, season):
    """Standalone (jala su propia tabla) para uso de un solo equipo."""
    tables = fetch_team_wrc_tables(season) if vs_hand in ("L", "R") else {}
    return _wrc_lookup(tables, team_abbr, vs_hand)


# ===========================================================================
# CAPA 5 — COMPUERTA (evaluacion por etapas: barato -> caro)
# ===========================================================================
def _nonbullpen_conditions(pick: dict, opp: dict) -> dict:
    """Las 6 condiciones que NO dependen del bullpen (datos ya en memoria)."""
    sp_p, sp_o = pick.get("metrics"), opp.get("metrics")
    wp, wo = pick.get("wrc"), opp.get("wrc")
    return {
        "abridor_propio_FIP<3.50":  (sp_p["FIP"] < FIP_CUT) if sp_p else None,
        "abridor_propio_WHIP<1.50": (sp_p["WHIP"] < WHIP_GOOD_CUT) if sp_p else None,
        "abridor_rival_FIP>3.50":   (sp_o["FIP"] > FIP_CUT) if sp_o else None,
        "abridor_rival_WHIP>=1.50": (sp_o["WHIP"] >= WHIP_BAD_CUT) if sp_o else None,
        "ofensiva_propia_wRC+>105": (wp["wrc_plus"] > WRC_HOT_CUT) if wp else None,
        "ofensiva_rival_wRC+<105":  (wo["wrc_plus"] < WRC_WEAK_CUT) if wo else None,
    }


def _alive(conds: dict) -> bool:
    """True solo si TODAS las condiciones no-bullpen son True (el lado sigue vivo)."""
    return all(v is True for v in conds.values())


def _verdict(conds: dict, bullpen_ok, abbr: str) -> dict:
    c = dict(conds)
    c["bullpen_>=2_listos"] = bullpen_ok
    if any(v is None for v in conds.values()):
        v = "PENDIENTE (faltan datos)"
    elif not _alive(conds):
        v = "no califica"            # ya falla algo barato; el bullpen ni se mira
    elif bullpen_ok is True:
        v = f"PICK {abbr}"
    elif bullpen_ok is False:
        v = "no califica"
    else:
        v = "PENDIENTE (bullpen)"
    return {"verdict": v, "condiciones": c}


# ===========================================================================
# ORQUESTADOR — valida UN juego (reutiliza data pre-cargada si se le pasa)
# ===========================================================================
def validate_game(game, season, asof, rows=None, wrc_tables=None,
                  lazy_bullpen=True):
    a, h = game["away"], game["home"]
    res = {"gamePk": game["gamePk"], "fecha": game["fecha"],
           "matchup": f'{a["abbr"]} @ {h["abbr"]}',
           "abridores": f'{a["sp_name"]} vs {h["sp_name"]}',
           "capas": {}}

    # 2A — manos
    try:
        a["hand"] = get_pitch_hand(a["sp_id"]); h["hand"] = get_pitch_hand(h["sp_id"])
        res["manos"] = {a["abbr"]: a["hand"], h["abbr"]: h["hand"]}
        res["capas"]["manos"] = "ok"
    except Exception as e:
        a["hand"] = h["hand"] = None
        res["capas"]["manos"] = f"error: {e}"

    # FanGraphs pitcheo (usa pre-cargado si viene)
    if rows is None:
        try:
            rows = fetch_fangraphs_pitching(season)
            res["capas"]["fangraphs_pitching"] = f"ok ({len(rows)} pitchers)"
        except Exception as e:
            res["capas"]["fangraphs_pitching"] = f"error: {type(e).__name__}: {e}"

    # 2B — abridores
    if rows is not None:
        try:
            m = get_starter_metrics(rows, [a["sp_id"], h["sp_id"]])
            a["metrics"] = m.get(a["sp_id"]); h["metrics"] = m.get(h["sp_id"])
            res["abridores_metrics"] = {a["abbr"]: a["metrics"], h["abbr"]: h["metrics"]}
            res["capas"]["abridores"] = "ok"
        except Exception as e:
            res["capas"]["abridores"] = f"error: {type(e).__name__}: {e}"

    # CAPA 4 — wRC+ por mano (tablas pre-cargadas o jaladas aqui)
    if wrc_tables is None:
        try:
            wrc_tables = fetch_team_wrc_tables(season)
        except Exception as e:
            wrc_tables = {}
            res["capas"]["wrc_plus"] = f"error: {type(e).__name__}: {e}"
    a["wrc"] = _wrc_lookup(wrc_tables, a["abbr"], h.get("hand"))
    h["wrc"] = _wrc_lookup(wrc_tables, h["abbr"], a.get("hand"))
    res["wrc_plus"] = {a["abbr"]: a["wrc"], h["abbr"]: h["wrc"]}
    res["capas"].setdefault("wrc_plus",
                            "ok (fallback temporada; primario 20d pendiente)")

    # CAPA 5 — etapa barata: condiciones no-bullpen
    conds = {a["abbr"]: _nonbullpen_conditions(a, h),
             h["abbr"]: _nonbullpen_conditions(h, a)}

    # etapa cara: bullpen SOLO para lados vivos (o todos si lazy_bullpen=False)
    for team, opp in ((a, h), (h, a)):
        alive = _alive(conds[team["abbr"]])
        if rows is not None and (alive or not lazy_bullpen):
            try:
                pen_ids = get_team_pitcher_ids(team["id"], season)
                closers = get_top_closers(rows, pen_ids)
                ids = [c["mlbam"] for c in closers["by_gmLI"] if c["mlbam"]]
                team["bullpen"] = bullpen_status(ids, season, asof)
                team["bullpen"]["listas"] = closers
            except Exception as e:
                team["bullpen"] = {"aprueba": None, "error": str(e)}
        else:
            team["bullpen"] = {"aprueba": None, "evaluado": False}
    res["bullpen"] = {a["abbr"]: a.get("bullpen"), h["abbr"]: h.get("bullpen")}
    res["capas"]["cerradores"] = "ok"

    # veredictos
    res["compuerta"] = {
        a["abbr"]: _verdict(conds[a["abbr"]], a["bullpen"].get("aprueba"), a["abbr"]),
        h["abbr"]: _verdict(conds[h["abbr"]], h["bullpen"].get("aprueba"), h["abbr"]),
    }
    return res


# ===========================================================================
# SCAN DEL SLATE COMPLETO — el producto: picks del dia
# ===========================================================================
def scan_slate(day: date) -> dict:
    season = day.year
    slate = get_slate(day)

    # Pre-carga FanGraphs UNA sola vez para todo el slate
    rows, wrc_tables, errores = None, {}, []
    try:
        rows = fetch_fangraphs_pitching(season)
    except Exception as e:
        errores.append(f"fangraphs_pitching: {e}")
    try:
        wrc_tables = fetch_team_wrc_tables(season)
    except Exception as e:
        errores.append(f"wrc_tables: {e}")

    picks, evaluados = [], []
    for g in slate:
        try:
            r = validate_game(g, season, day, rows=rows, wrc_tables=wrc_tables,
                              lazy_bullpen=True)
        except Exception as e:
            evaluados.append({"matchup": f'{g["away"]["abbr"]} @ {g["home"]["abbr"]}',
                              "error": str(e)})
            continue
        evaluados.append({"matchup": r["matchup"],
                          "veredictos": {k: v["verdict"]
                                         for k, v in r.get("compuerta", {}).items()}})
        for abbr, v in r.get("compuerta", {}).items():
            if v["verdict"].startswith("PICK"):
                picks.append({"matchup": r["matchup"], "pick": abbr,
                              "abridores": r["abridores"],
                              "condiciones": v["condiciones"]})

    return {"fecha": day.isoformat(), "juegos": len(slate),
            "total_picks": len(picks), "picks": picks,
            "evaluados": evaluados,
            "errores": errores or None,
            "nota_wrc": "wRC+ = fallback temporada (primario 20d pendiente Fase 3b)"}
