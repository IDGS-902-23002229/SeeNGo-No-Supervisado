"""
Consumidor de datos SEENGO
==========================
Lee los eventos (de MongoDB Atlas o de archivos JSON locales), corre el
modelo y escribe los resultados para la pantalla.

NUNCA pongas la contraseña aquí. Se lee de una variable de entorno:

    export MONGO_URI="mongodb+srv://USUARIO:PASSWORD@cluster.mongodb.net/"
    export MONGO_DB="seengo"          # opcional (default: seengo)
    export MONGO_COLL="sign_events"   # opcional (default: sign_events)

En desarrollo (VS Code) es más cómodo poner esas variables en un archivo
`.env` en la raíz del proyecto (ver `.env.example`); se carga solo si
`python-dotenv` está instalado. `.env` está en `.gitignore`: nunca se sube.

Uso:
    # Desde Mongo Atlas (últimos N días, ventana rodante):
    python consumidor/consumir_mongo.py --dias 60

    # Sin Mongo, para probar con los archivos de ejemplo:
    python consumidor/consumir_mongo.py --archivos datos/ejemplos_*.json

Salida:
    resultados.json              (para inspección / integración)
    pantalla/resultados.js       (lo que carga la vista sin servidor)
"""
import os, sys, json, glob, argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "modelo"))
from detector_rutinas import analizar, CONFIG  # noqa: E402
from sugerencias import generar_sugerencias, resumen_aceptacion  # noqa: E402

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _mapear(doc):
    """De documento Mongo -> esquema que espera el modelo."""
    ts = doc["ts"]
    if not isinstance(ts, str):          # si viene como Date de Mongo
        ts = ts.isoformat()
    return {"deviceId": doc["deviceId"], "action": doc["action"],
            "confidence": float(doc.get("confidence", 1.0)), "ts": ts}


def leer_mongo(dias):
    try:
        from pymongo import MongoClient
    except ImportError:
        sys.exit("Falta pymongo. Instala con:  pip install pymongo")
    uri = os.environ.get("MONGO_URI")
    if not uri:
        sys.exit("Define MONGO_URI en el entorno (no la escribas en el código).")
    db = os.environ.get("MONGO_DB", "seengo")
    coll = os.environ.get("MONGO_COLL", "sign_events")

    cli = MongoClient(uri, serverSelectionTimeoutMS=8000)
    col = cli[db][coll]
    filtro = {}
    if dias:
        desde = datetime.now(ZoneInfo("UTC")) - timedelta(days=dias)
        # cubre ts guardado como string ISO o como Date
        filtro = {"$or": [{"ts": {"$gte": desde.isoformat()}},
                          {"ts": {"$gte": desde}}]}
    docs = list(col.find(filtro, {"deviceId": 1, "action": 1,
                                  "confidence": 1, "ts": 1, "_id": 0}))
    print(f"Mongo: {len(docs)} documentos leídos de {db}.{coll}")
    return [_mapear(d) for d in docs]


def leer_archivos(patrones):
    eventos = []
    for patron in patrones:
        for ruta in sorted(glob.glob(patron)):
            eventos += json.load(open(ruta, encoding="utf-8"))
    print(f"Archivos: {len(eventos)} eventos leídos.")
    return eventos


# ----------------------------------------------------------------------
# Sugerencias en Mongo (colección aparte, default: "sugerencias").
# El modelo genera candidatas; aquí solo se publican (sin duplicar) y se
# leen las respuestas del usuario (`aceptada`: 1 sí / 0 no / null pendiente).
# ----------------------------------------------------------------------
def _cliente():
    try:
        from pymongo import MongoClient
    except ImportError:
        sys.exit("Falta pymongo. Instala con:  pip install pymongo")
    uri = os.environ.get("MONGO_URI")
    if not uri:
        sys.exit("Define MONGO_URI en el entorno (no la escribas en el código).")
    return MongoClient(uri, serverSelectionTimeoutMS=8000)


def _coll_sugerencias(cli):
    db = os.environ.get("MONGO_DB", "seengo")
    coll = os.environ.get("MONGO_COLL_SUG", "sugerencias")
    return cli[db][coll]


def publicar_sugerencias(nuevas):
    """Upsert por `clave`: solo inserta las que no existan. Nunca pisa la
    respuesta (`aceptada`) de una sugerencia ya publicada."""
    if not nuevas:
        return 0
    col = _coll_sugerencias(_cliente())
    creada = datetime.now(ZoneInfo("UTC")).isoformat()
    insertadas = 0
    for s in nuevas:
        r = col.update_one(
            {"clave": s["clave"]},
            {"$setOnInsert": {**s, "creada": creada,
                              "aceptada": None, "respondida": None}},
            upsert=True)
        if r.upserted_id is not None:
            insertadas += 1
    return insertadas


def leer_sugerencias():
    col = _coll_sugerencias(_cliente())
    return list(col.find({}, {"_id": 0}))


def responder_sugerencia(clave, aceptada):
    """Registra la respuesta del usuario: aceptada=1 (sí) o 0 (no)."""
    col = _coll_sugerencias(_cliente())
    r = col.update_one(
        {"clave": clave},
        {"$set": {"aceptada": int(aceptada),
                  "respondida": datetime.now(ZoneInfo("UTC")).isoformat()}})
    return r.matched_count > 0


def sugerencias_local():
    """Resumen de aceptación del set de ejemplo del repo (fuente 'local').
    OJO: el archivo NO se llama ejemplos_*.json a propósito, para no caer en
    el glob de eventos de ejemplo."""
    ruta = os.path.join(RAIZ, "datos", "sugerencias_ejemplo.json")
    try:
        with open(ruta, encoding="utf-8") as fh:
            return resumen_aceptacion(json.load(fh))
    except FileNotFoundError:
        return resumen_aceptacion([])


def sugerencias_atlas(res):
    """Publica en Mongo las sugerencias nuevas que salgan del análisis y
    devuelve el resumen con TODAS (incluidas las respuestas del usuario)."""
    nuevas = generar_sugerencias(res)
    n = publicar_sugerencias(nuevas)
    if n:
        print(f"Sugerencias nuevas publicadas en Mongo: {n}")
    return resumen_aceptacion(leer_sugerencias())


# ----------------------------------------------------------------------
# Envelope de fuentes:  {"atlas": <resultado|None>, "local": <resultado|None>}
# La pantalla lo usa para ofrecer una pestaña que alterna entre los datos
# reales de Mongo ("atlas") y el set de ejemplo del repo ("local"). Cada corrida
# rellena SU fuente y CONSERVA la otra, así corriendo los dos modos una vez
# quedan ambas disponibles en un mismo resultados.js.
# ----------------------------------------------------------------------
def _leer_envelope():
    """Lee el resultados.json previo si ya es un envelope; si no, empieza vacío."""
    try:
        with open("resultados.json", encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict) and ("atlas" in d or "local" in d):
            return {"atlas": d.get("atlas"), "local": d.get("local")}
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"atlas": None, "local": None}


def _escribir_envelope(env):
    with open("resultados.json", "w", encoding="utf-8") as fh:
        json.dump(env, fh, ensure_ascii=False, indent=2, default=str)
    os.makedirs("pantalla", exist_ok=True)
    with open("pantalla/resultados.js", "w", encoding="utf-8") as fh:
        fh.write("window.SEENGO_FUENTES = ")
        json.dump(env, fh, ensure_ascii=False, default=str)
        fh.write(";")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=60,
                    help="Ventana rodante en días (solo modo Mongo).")
    ap.add_argument("--archivos", nargs="+",
                    help="Rutas/globs a JSON locales (modo sin Mongo).")
    ap.add_argument("--conf-min", type=float, default=CONFIG["conf_min"])
    args = ap.parse_args()

    if args.archivos:
        eventos = leer_archivos(args.archivos)
        ahora = None  # sin racha final abierta al probar con archivos
        fuente = "local"
    else:
        eventos = leer_mongo(args.dias)
        ahora = datetime.now(ZoneInfo(CONFIG["tz"]))  # detecta ausencia en curso
        fuente = "atlas"

    if not eventos:
        sys.exit("No hay eventos que analizar.")

    res = analizar(eventos, cfg={"conf_min": args.conf_min}, ahora=ahora)
    res["meta"]["generado"] = datetime.now(ZoneInfo(CONFIG["tz"])).isoformat()
    res["meta"]["fuente"] = fuente

    # sugerencias + aceptación (aditivo: la pantalla lo muestra si existe)
    res["sugerencias"] = (sugerencias_local() if fuente == "local"
                          else sugerencias_atlas(res))

    env = _leer_envelope()      # conserva la otra fuente si ya existía
    env[fuente] = res
    _escribir_envelope(env)

    m = res["meta"]
    print(f"OK ({fuente})  {m['eventos_crudos']} crudos -> {m['interacciones']} "
          f"interacciones | {m['rango']['dias_activos']} días activos")
    conf = sum(1 for s in res["streams"] for v in s["vistas"].values()
               for r in v["rutinas"] if r["confirmada"])
    print(f"Rutinas confirmadas: {conf} | Ausencia: {res['ausencia_larga']['nivel']}")
    sug = res["sugerencias"]
    print(f"Sugerencias: {sug['total']} ({sug['aceptadas']} sí / "
          f"{sug['rechazadas']} no / {sug['pendientes']} pendientes)")
    otras = [k for k in ("atlas", "local") if env.get(k) and k != fuente]
    if otras:
        print(f"(se conservó la fuente '{otras[0]}' del resultado previo)")
    print("Escrito: resultados.json y pantalla/resultados.js")


if __name__ == "__main__":
    main()
