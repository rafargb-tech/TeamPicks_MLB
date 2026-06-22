"""
TeamPicks_MLB — App (v1.2.0)

Rutas:
  GET /                        -> DASHBOARD (capa de visualizacion)
  GET /health                  -> health check JSON (version)
  GET /picks?fecha=YYYY-MM-DD  -> EL PRODUCTO: escanea el slate completo y
                                  devuelve los picks del dia (default = hoy)
                                  (flag con_cuotas=1 anexa cuotas a todos)
  GET /validar?fecha=YYYY-MM-DD&equipo=NYM
                                  -> debug de UN juego con detalle completo
                                  (bullpen siempre evaluado)
"""
import os
from datetime import date
from flask import Flask, request, jsonify, Response
import data_layer as dl

app = Flask(__name__)
VERSION = "1.2.0"
HERE = os.path.dirname(os.path.abspath(__file__))


def _parse_day(fecha):
    return date.fromisoformat(fecha) if fecha else date.today()


@app.get("/")
def dashboard():
    try:
        with open(os.path.join(HERE, "dashboard.html"), encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html")
    except FileNotFoundError:
        return jsonify({"error": "dashboard.html no encontrado en el repo"}), 404


@app.get("/health")
def health():
    return jsonify({"status": "ok",
                    "servicio": "TeamPicks_MLB",
                    "version": VERSION})


@app.get("/picks")
def picks():
    try:
        day = _parse_day(request.args.get("fecha"))
    except ValueError:
        return jsonify({"error": "fecha invalida, usa YYYY-MM-DD"}), 400
    incluir = request.args.get("incluir_empezados") in ("1", "true", "si")
    con_cuotas = request.args.get("con_cuotas") in ("1", "true", "si")
    return jsonify(dl.scan_slate(day, incluir_empezados=incluir, con_cuotas=con_cuotas))


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
