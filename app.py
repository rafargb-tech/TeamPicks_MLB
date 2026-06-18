"""
TeamPicks_MLB — App (v0.4.0)

Rutas:
  GET /                        -> health check
  GET /picks?fecha=YYYY-MM-DD  -> EL PRODUCTO: escanea el slate completo y
                                  devuelve los picks del dia (default = hoy)
  GET /validar?fecha=YYYY-MM-DD&equipo=NYM
                               -> debug de UN juego con detalle completo
                                  (bullpen siempre evaluado)
"""
from datetime import date
from flask import Flask, request, jsonify
import data_layer as dl

app = Flask(__name__)


def _parse_day(fecha):
    return date.fromisoformat(fecha) if fecha else date.today()


@app.get("/")
def health():
    return jsonify({"status": "ok",
                    "servicio": "TeamPicks_MLB",
                    "version": "0.4.0"})


@app.get("/picks")
def picks():
    try:
        day = _parse_day(request.args.get("fecha"))
    except ValueError:
        return jsonify({"error": "fecha invalida, usa YYYY-MM-DD"}), 400
    return jsonify(dl.scan_slate(day))


@app.get("/validar")
def validar():
    fecha = request.args.get("fecha")
    equipo = request.args.get("equipo")
    try:
        day = _parse_day(fecha)
    except ValueError:
        return jsonify({"error": "fecha invalida, usa YYYY-MM-DD"}), 400
    season = day.year

    try:
        slate = dl.get_slate(day)
    except Exception as e:
        return jsonify({"error_slate": f"{type(e).__name__}: {e}"}), 502

    if not slate:
        return jsonify({"fecha": day.isoformat(), "juegos": 0,
                        "msg": "sin juegos en esa fecha"}), 200

    juego = None
    if equipo:
        equipo = equipo.upper()
        juego = next((g for g in slate
                      if equipo in (g["away"]["abbr"], g["home"]["abbr"])), None)
    if juego is None:
        juego = slate[0]

    resultado = dl.validate_game(juego, season, day, lazy_bullpen=False)
    return jsonify({"fecha": day.isoformat(),
                    "juegos_en_fecha": len(slate),
                    "juego_analizado": resultado})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
