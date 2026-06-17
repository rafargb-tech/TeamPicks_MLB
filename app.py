"""
TeamPicks_MLB — App de validacion (v0.1.0)

Rutas:
  GET /                        -> health check
  GET /validar?fecha=YYYY-MM-DD&equipo=NYM
       fecha  : opcional (default = hoy)
       equipo : opcional, abreviatura MLB (ej. NYM). Si se omite, toma el
                primer juego del dia. Acotamos a UN juego para que la prueba
                sea rapida y no castigue la memoria del free tier.
"""
from datetime import date
from flask import Flask, request, jsonify
import data_layer as dl

app = Flask(__name__)


@app.get("/")
def health():
    return jsonify({"status": "ok",
                    "servicio": "TeamPicks_MLB validador",
                    "version": "0.1.0"})


@app.get("/validar")
def validar():
    fecha = request.args.get("fecha")
    equipo = request.args.get("equipo")
    try:
        day = date.fromisoformat(fecha) if fecha else date.today()
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

    resultado = dl.validate_game(juego, season, day)
    return jsonify({"fecha": day.isoformat(),
                    "juegos_en_fecha": len(slate),
                    "juego_analizado": resultado})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
