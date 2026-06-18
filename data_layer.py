"""
TeamPicks_MLB — Capa de datos (modulo)
Version: 0.7.0

Estrategia: Moneyline / desequilibrio pitcheo-ofensiva.
Este modulo NO imprime: devuelve estructuras para que app.py las sirva como JSON.

CAMBIO v0.7.0: cuotas moneyline (The Odds API, decimal). Solo se consultan si
hay picks (conserva cuota mensual) o con el flag con_cuotas. Mejor precio por
casa. Key en env var THE_ODDS_API_KEY; region en ODDS_REGION (default us).
CAMBIO v0.6.0: wRC+ PRIMARIO de 20 dias por mano implementado (endpoint Splits
Leaderboards), con guard de 100 PA y fallback a temporada. Temporada y 20d salen
del MISMO endpoint (consistencia). Fase 3b completa.
CAMBIO v0.5.0: scan_slate() filtra los juegos que ya empezaron/terminaron
(solo evalua estado "Preview"); reporta los omitidos. Solo muestra picks de
juegos aun apostables.
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
import os
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
SPLIT_VS_L       = 1       # strSplitArr en Splits Leaderboards = vs LHP
SPLIT_VS_R       = 2       # strSplitArr en Splits Leaderboards = vs RHP

FATIGUE_WINDOW           = 5    # ventana de la regla 2
FATIGUE_APPEARANCES      = 3    # fatigado si lanzo en >=3 de los ultimos 5 dias
FATIGUE_MAX_PITCHES_LAST = 30   # fatigado si >30 pitcheos en su ultima salida
FATIGUE_R3_RECENCY_DAYS  = 1    # R3 solo aplica si esa salida pesada fue dentro
                                # de N dias. ESTRICTO=1 (solo ayer). [DOCUMENTAR]
CLOSERS_NEEDED_AVAILABLE = 2    # aprueba si >=2 de 3 cerradores estan listos

MLB = "https://statsapi.mlb.com/api/v1"
FG  = "https://www.fangraphs.com/api/leaders/major-league/data"
FG_SPLITS = "https://www.fangraphs.com/api/leaders/splits/splits-leaders"
ODDS_API = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"

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
            st = g.get("status", {})
            games.append({"gamePk": g["gamePk"],
                          "fecha": g.get("officialDate", day.isoformat()),
                          "estado": st.get("abstractGameState"),
                          "estado_detalle": st.get("detailedState"),
                          "inicio": g.get("gameDate"),
                          "away": side(g["teams"]["away"]),
                          "home": side(g["teams"]["home"])})
    return games


# Estados detallados en los que un juego NO es apostable aunque figure "Preview"
NO_APOSTABLE = {"Postponed", "Cancelled", "Suspended"}


def es_apostable(game: dict) -> bool:
    """Apostable = pre-juego (aun no lanza) y no postpuesto/cancelado."""
    return (game.get("estado") == "Preview"
            and game.get("estado_detalle") not in NO_APOSTABLE)


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
# CAPA 4 — wRC+ DE EQUIPO POR MANO  (primario 20d con guard; fallback temporada)
# Ambos del mismo endpoint: Splits Leaderboards de FanGraphs.
#   strSplitArr=[1] = vs LHP ; [2] = vs RHP ; strType=2 trae wRC+ ; team-level.
# ===========================================================================
def _splits_team_wrc(vs_hand: str, start: str, end: str) -> dict:
    """{fg_abbr: {'wrc_plus','pa'}} de TODOS los equipos vs una mano, en un
    rango de fechas. Fuente: Splits Leaderboards (POST)."""
    code = SPLIT_VS_L if vs_hand == "L" else SPLIT_VS_R
    body = {"strPlayerId": "all", "strSplitArr": [code], "strGroup": "season",
            "strPosition": "B", "strType": "2",
            "strStartDate": start, "strEndDate": end,
            "strSplitTeams": False, "dctFilters": [], "strStatType": "team",
            "strAutoPt": "false", "arrPlayerId": [],
            "strSplitArrPitch": [], "strSplitArrTeam": []}
    req = urllib.request.Request(
        FG_SPLITS, data=json.dumps(body).encode(),
        headers={**UA, "Content-Type": "application/json"}, method="POST")
    d = json.load(urllib.request.urlopen(req, timeout=30))
    k = d.get("k", [])
    if "wRC+" not in k:
        return {}
    iT, iPA, iW = k.index("TeamNameAbb"), k.index("PA"), k.index("wRC+")
    out = {}
    for row in d.get("v", []):
        out[str(row[iT])] = {"wrc_plus": _f(row[iW]), "pa": int(float(row[iPA] or 0))}
    return out


def fetch_team_wrc_tables(season: int, asof: date) -> dict:
    """Pre-carga 4 tablas UNA vez por slate: ventana 20d y temporada, ambas manos."""
    end = (asof - timedelta(days=1)).isoformat()           # hasta ayer (juegos cerrados)
    start20 = (asof - timedelta(days=WRC_WINDOW_DAYS)).isoformat()
    seas_start = f"{season}-03-01"
    return {
        "20d":    {"L": _splits_team_wrc("L", start20, end),
                   "R": _splits_team_wrc("R", start20, end)},
        "season": {"L": _splits_team_wrc("L", seas_start, end),
                   "R": _splits_team_wrc("R", seas_start, end)},
    }


def _wrc_lookup(tables: dict, team_abbr: str, vs_hand: str | None) -> dict | None:
    """Primario: ventana 20d si PA>=100 (source='20d'). Si no, fallback temporada."""
    if vs_hand not in ("L", "R") or not tables:
        return None
    fg = MLB_TO_FG_ABBR.get(team_abbr, team_abbr)
    w20 = tables.get("20d", {}).get(vs_hand, {}).get(fg)
    if w20 and w20["wrc_plus"] is not None and w20["pa"] >= WRC_MIN_PA:
        return {"wrc_plus": w20["wrc_plus"], "pa": w20["pa"], "source": "20d"}
    ws = tables.get("season", {}).get(vs_hand, {}).get(fg)
    if ws and ws["wrc_plus"] is not None:
        return {"wrc_plus": ws["wrc_plus"], "pa": ws["pa"], "source": "season"}
    return None


def fetch_team_wrcplus_split(team_abbr, vs_hand, asof, season):
    """Standalone (jala sus propias tablas) para uso de un solo equipo."""
    tables = fetch_team_wrc_tables(season, asof) if vs_hand in ("L", "R") else {}
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
            wrc_tables = fetch_team_wrc_tables(season, asof)
        except Exception as e:
            wrc_tables = {}
            res["capas"]["wrc_plus"] = f"error: {type(e).__name__}: {e}"
    a["wrc"] = _wrc_lookup(wrc_tables, a["abbr"], h.get("hand"))
    h["wrc"] = _wrc_lookup(wrc_tables, h["abbr"], a.get("hand"))
    res["wrc_plus"] = {a["abbr"]: a["wrc"], h["abbr"]: h["wrc"]}
    res["capas"].setdefault("wrc_plus", "ok (primario 20d con guard; fallback temporada)")

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
# CAPA 6 — CUOTAS (The Odds API)   moneyline / h2h, formato decimal
# ===========================================================================
def _norm_team(name: str | None) -> str:
    n = (name or "").strip()
    return "Athletics" if "Athletics" in n else n


def _ts(iso: str | None) -> float:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def fetch_mlb_odds() -> tuple[list | None, str | None]:
    """Una llamada: todas las cuotas moneyline MLB en decimal.
    Lee la key de la env var THE_ODDS_API_KEY (region: ODDS_REGION, default us).
    """
    key = os.environ.get("THE_ODDS_API_KEY")
    if not key:
        return None, "THE_ODDS_API_KEY no configurada en el entorno"
    region = os.environ.get("ODDS_REGION", "us")
    url = (f"{ODDS_API}?regions={region}&markets=h2h&oddsFormat=decimal&apiKey={key}")
    try:
        return _get(url), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _match_event(events: list, away: str, home: str, inicio: str | None):
    tgt = {_norm_team(away), _norm_team(home)}
    cands = [e for e in events
             if {_norm_team(e.get("away_team")), _norm_team(e.get("home_team"))} == tgt]
    if not cands:
        return None
    if len(cands) > 1:   # doubleheader: el inicio mas cercano
        cands.sort(key=lambda e: abs(_ts(e.get("commence_time")) - _ts(inicio)))
    return cands[0]


def _best_price(event: dict, team_name: str) -> dict | None:
    """Mejor cuota decimal (mas alta) para el equipo, y que casa la ofrece."""
    pn = _norm_team(team_name)
    best, casa = None, None
    for bk in event.get("bookmakers", []):
        for m in bk.get("markets", []):
            if m.get("key") != "h2h":
                continue
            for o in m.get("outcomes", []):
                price = o.get("price")
                if _norm_team(o.get("name")) == pn and isinstance(price, (int, float)):
                    if best is None or price > best:
                        best, casa = price, bk.get("title")
    if best is None:
        return None
    return {"cuota": round(best, 2), "casa": casa,
            "n_casas": len(event.get("bookmakers", []))}


# ===========================================================================
# SCAN DEL SLATE COMPLETO — el producto: picks del dia (+ cuotas)
# ===========================================================================
def scan_slate(day: date, incluir_empezados: bool = False,
               con_cuotas: bool = False) -> dict:
    season = day.year
    slate = get_slate(day)

    # Separar apostables (pre-juego) de los que ya empezaron / terminaron
    omitidos = [{"matchup": f'{g["away"]["abbr"]} @ {g["home"]["abbr"]}',
                 "estado": g.get("estado_detalle")}
                for g in slate if not es_apostable(g)]
    a_evaluar = slate if incluir_empezados else [g for g in slate if es_apostable(g)]

    # Pre-carga FanGraphs UNA sola vez para todo el slate
    rows, wrc_tables, errores = None, {}, []
    if a_evaluar:
        try:
            rows = fetch_fangraphs_pitching(season)
        except Exception as e:
            errores.append(f"fangraphs_pitching: {e}")
        try:
            wrc_tables = fetch_team_wrc_tables(season, day)
        except Exception as e:
            errores.append(f"wrc_tables: {e}")

    picks, evaluados = [], []
    for g in a_evaluar:
        try:
            r = validate_game(g, season, day, rows=rows, wrc_tables=wrc_tables,
                              lazy_bullpen=True)
        except Exception as e:
            evaluados.append({"matchup": f'{g["away"]["abbr"]} @ {g["home"]["abbr"]}',
                              "error": str(e)})
            continue
        evaluados.append({"matchup": r["matchup"], "inicio": g.get("inicio"),
                          "_g": g,
                          "veredictos": {k: v["verdict"]
                                         for k, v in r.get("compuerta", {}).items()}})
        for abbr, v in r.get("compuerta", {}).items():
            if v["verdict"].startswith("PICK"):
                pick_name = (g["away"]["name"] if abbr == g["away"]["abbr"]
                             else g["home"]["name"])
                picks.append({"matchup": r["matchup"], "pick": abbr,
                              "pick_equipo": pick_name, "inicio": g.get("inicio"),
                              "abridores": r["abridores"],
                              "abridores_metrics": r.get("abridores_metrics"),
                              "wrc_plus": r.get("wrc_plus"),
                              "condiciones": v["condiciones"], "_g": g})

    # CAPA 6 — cuotas: solo si hay picks (conserva cuota) o si se pide con_cuotas
    odds_nota = None
    if picks or (con_cuotas and a_evaluar):
        events, err = fetch_mlb_odds()
        if err:
            odds_nota = f"cuotas no disponibles: {err}"
        else:
            for p in picks:
                g = p["_g"]
                ev = _match_event(events, g["away"]["name"], g["home"]["name"], p["inicio"])
                p["cuota"] = _best_price(ev, p["pick_equipo"]) if ev else None
            if con_cuotas:
                for e in evaluados:
                    g = e.get("_g")
                    ev = _match_event(events, g["away"]["name"], g["home"]["name"],
                                      e["inicio"]) if g else None
                    e["cuotas"] = ({g["away"]["abbr"]: _best_price(ev, g["away"]["name"]),
                                    g["home"]["abbr"]: _best_price(ev, g["home"]["name"])}
                                   if ev else None)
            odds_nota = f"cuotas: mejor precio decimal por casa (region={os.environ.get('ODDS_REGION','us')})"

    for p in picks:
        p.pop("_g", None)
    for e in evaluados:
        e.pop("_g", None)

    return {"fecha": day.isoformat(),
            "juegos_totales": len(slate),
            "evaluados_apostables": len(evaluados),
            "omitidos_ya_empezados": omitidos,
            "total_picks": len(picks), "picks": picks,
            "evaluados": evaluados,
            "errores": errores or None,
            "nota_cuotas": odds_nota,
            "nota_wrc": "wRC+ = 20 dias por mano (guard 100 PA) con fallback a temporada"}
