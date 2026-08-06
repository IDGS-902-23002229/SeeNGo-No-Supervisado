"""
Consumidor de datos SEENGO — únicamente contra MongoDB Atlas
============================================================
Lee los eventos de la colección real, corre el modelo y escribe el resultado
para la pantalla. **No hay modo local**: si Atlas no responde, esto falla con
código de salida distinto de cero. Antes existía una ruta `--archivos` que
leía JSON de ejemplo; se quitó a propósito, porque tener dos fuentes hacía
imposible saber qué se estaba mirando en el tablero.

NUNCA pongas la contraseña aquí. Se lee de una variable de entorno:

    MONGO_URI="mongodb+srv://USUARIO:PASSWORD@cluster.mongodb.net/"
    MONGO_DB="seengo"          # opcional (default: seengo)
    MONGO_COLL="sign_events"   # opcional (default: sign_events)

En desarrollo es más cómodo ponerlas en un archivo `.env` en la raíz (ver
`.env.example`); se carga solo si `python-dotenv` está instalado. `.env`
está en `.gitignore`: nunca se sube.

Uso:
    python consumidor/consumir_mongo.py            # ventana por defecto
    python consumidor/consumir_mongo.py --dias 90

Salida:
    resultados.json              (para inspección / integración)
    pantalla/resultados.js       (lo que carga la vista sin servidor)
"""
import os, sys, json, argparse
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

# Ventana rodante por defecto. Son 90 y no 60 porque el calendario que siembra
# `datos/sembrar_atlas.py` ocupa ~83 días (rutinas + hueco de ausencia de 25
# días + reanudación junto con los datos reales). Con 60 se pierde el primer
# tramo y el tablero sale a medias.
DIAS_DEFECTO = int(os.environ.get("SEENGO_DIAS", "90"))

# ----------------------------------------------------------------------
# FASE 1 · SELECCIÓN DE DATOS
# ----------------------------------------------------------------------
# Estos son los ÚNICOS campos que el modelo necesita. Se piden explícitamente
# en la proyección en vez de traer el documento completo: la colección guarda
# además `userId`, `gesture`, `origen`, `capa` y `corrida`, que no aportan
# nada a la detección y sólo costarían red y memoria en la Raspberry Pi.
# `gesture` en particular es redundante: es una traducción 1-a-1 de `action`.
CAMPOS = {"deviceId": 1, "action": 1, "confidence": 1, "ts": 1, "_id": 0}


def _host_sin_credenciales(uri):
    """Devuelve sólo el host del cluster, sin usuario ni contraseña.
    Se muestra en el tablero para documentar de dónde salieron los datos;
    la credencial jamás debe salir del servidor."""
    if not uri:
        return "(sin URI)"
    sin_esquema = uri.split("://", 1)[-1]
    host = sin_esquema.split("@")[-1]        # descarta usuario:contraseña
    return host.split("/")[0].split("?")[0]


def _mapear(doc):
    """De documento Mongo -> esquema que espera el modelo."""
    ts = doc["ts"]
    if not isinstance(ts, str):          # si viene como Date de Mongo
        ts = ts.isoformat()
    return {"deviceId": doc["deviceId"], "action": doc["action"],
            "confidence": float(doc.get("confidence", 1.0)), "ts": ts}


def leer_mongo(dias=None):
    """Lee la ventana rodante desde Atlas.

    Devuelve (eventos, seleccion) donde `seleccion` documenta la fase 1:
    de qué cluster, base, colección y ventana salieron los datos, y con qué
    proyección. Lanza RuntimeError si Atlas no responde: NO se cae a datos
    locales, porque un tablero con datos inventados es peor que uno vacío.
    """
    dias = DIAS_DEFECTO if dias is None else dias
    try:
        from pymongo import MongoClient
    except ImportError:
        raise RuntimeError("Falta pymongo. Instala con:  pip install pymongo")

    uri = os.environ.get("MONGO_URI")
    if not uri:
        raise RuntimeError(
            "Falta MONGO_URI. Ponla en .env o en el entorno "
            "(nunca en el código).")
    db = os.environ.get("MONGO_DB", "seengo")
    coll = os.environ.get("MONGO_COLL", "sign_events")

    desde = datetime.now(ZoneInfo("UTC")) - timedelta(days=dias)
    # El `$or` cubre las dos formas en que puede estar guardado `ts` en la
    # colección: texto ISO 8601 (como lo escribe la app) o Date nativo.
    filtro = {"$or": [{"ts": {"$gte": desde.isoformat()}},
                      {"ts": {"$gte": desde}}]}

    try:
        cli = MongoClient(uri, serverSelectionTimeoutMS=8000)
        cli.admin.command("ping")                 # falla rápido y claro
        docs = list(cli[db][coll].find(filtro, CAMPOS))
    except Exception as e:                        # noqa: BLE001
        raise RuntimeError(
            f"No se pudo leer {db}.{coll} en Atlas: {type(e).__name__}: {e}")

    seleccion = {
        "cluster": _host_sin_credenciales(uri),
        "base": db,
        "coleccion": coll,
        "campos": [c for c in CAMPOS if c != "_id"],
        "ventana_dias": dias,
        "desde": desde.isoformat(),
        "documentos_leidos": len(docs),
    }
    print(f"Mongo: {len(docs)} documentos leídos de {db}.{coll} "
          f"(ventana de {dias} días)")
    return [_mapear(d) for d in docs], seleccion


# ----------------------------------------------------------------------
# Sugerencias en Mongo (colección aparte, default: "sugerencias").
# El modelo genera candidatas; aquí solo se publican (sin duplicar) y se
# leen las respuestas del usuario (`aceptada`: 1 sí / 0 no / null pendiente).
# ----------------------------------------------------------------------
def _cliente():
    try:
        from pymongo import MongoClient
    except ImportError:
        raise RuntimeError("Falta pymongo. Instala con:  pip install pymongo")
    uri = os.environ.get("MONGO_URI")
    if not uri:
        raise RuntimeError("Falta MONGO_URI (ponla en .env, no en el código).")
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


def sugerencias_atlas(res):
    """Publica en Mongo las sugerencias nuevas que salgan del análisis y
    devuelve el resumen con TODAS (incluidas las respuestas del usuario)."""
    nuevas = generar_sugerencias(res)
    n = publicar_sugerencias(nuevas)
    if n:
        print(f"Sugerencias nuevas publicadas en Mongo: {n}")
    return resumen_aceptacion(leer_sugerencias())


# ----------------------------------------------------------------------
# Análisis completo (lo reutiliza el servidor)
# ----------------------------------------------------------------------
def analizar_atlas(dias=None, conf_min=None):
    """Lee Atlas, corre el modelo y arma el resultado listo para la pantalla."""
    eventos, seleccion = leer_mongo(dias)
    if not eventos:
        raise RuntimeError(
            "Atlas respondió pero la ventana no trae eventos. "
            "¿Sembraste la colección? -> python datos/sembrar_atlas.py --limpiar")

    cfg = {"conf_min": conf_min} if conf_min is not None else None
    ahora = datetime.now(ZoneInfo(CONFIG["tz"]))   # detecta ausencia en curso
    res = analizar(eventos, cfg=cfg, ahora=ahora)
    res["meta"]["generado"] = ahora.isoformat()
    res["meta"]["fuente"] = "atlas"
    res["meta"]["seleccion"] = seleccion           # fase 1, para el tablero
    res["sugerencias"] = sugerencias_atlas(res)
    return res


def escribir_salidas(res):
    """Deja el resultado en disco: JSON para integración y JS para la vista."""
    with open(os.path.join(RAIZ, "resultados.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=2, default=str)
    os.makedirs(os.path.join(RAIZ, "pantalla"), exist_ok=True)
    with open(os.path.join(RAIZ, "pantalla", "resultados.js"), "w",
              encoding="utf-8") as fh:
        fh.write("window.SEENGO_RESULTADOS = ")
        json.dump(res, fh, ensure_ascii=False, default=str)
        fh.write(";")


def main():
    ap = argparse.ArgumentParser(
        description="Corre el detector de rutinas sobre MongoDB Atlas.")
    ap.add_argument("--dias", type=int, default=DIAS_DEFECTO,
                    help=f"Ventana rodante en días (default {DIAS_DEFECTO}).")
    ap.add_argument("--conf-min", type=float, default=CONFIG["conf_min"],
                    help="Confianza mínima para no descartar un evento.")
    args = ap.parse_args()

    try:
        res = analizar_atlas(args.dias, args.conf_min)
    except RuntimeError as e:
        # Sin datos reales no se sigue: mejor fallar fuerte que mostrar un
        # tablero que parece bueno y no lo es.
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    escribir_salidas(res)

    m = res["meta"]
    sel = m["seleccion"]
    print(f"\nFase 1 · selección : {sel['base']}.{sel['coleccion']} en "
          f"{sel['cluster']}")
    print(f"                     campos {sel['campos']}")
    d = m["descartes"]
    print(f"Fase 2 · limpieza  : {m['eventos_crudos']} crudos -> "
          f"{m['interacciones']} interacciones")
    print(f"                     descartados: {d['confianza']} por confianza, "
          f"{d['ts_invalido']} por ts inválido, {d['duplicados']} duplicados")
    conf = sum(1 for s in res["streams"] for v in s["vistas"].values()
               for r in v["rutinas"] if r["confirmada"])
    print(f"Fase 3/4 · minería : {len(res['streams'])} streams, "
          f"{conf} rutinas confirmadas en {m['rango']['dias_activos']} días activos")
    sug = res["sugerencias"]
    print(f"Fase 5 · uso       : ausencia={res['ausencia_larga']['nivel']}, "
          f"{sug['total']} sugerencias ({sug['aceptadas']} sí / "
          f"{sug['rechazadas']} no / {sug['pendientes']} pendientes)")
    print("\nEscrito: resultados.json y pantalla/resultados.js")


if __name__ == "__main__":
    main()
