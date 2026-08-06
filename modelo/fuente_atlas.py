"""
Lectura de eventos desde MongoDB Atlas para uso del modelo.
===========================================================
Este módulo es la ÚNICA parte de `modelo/` que toca la red, y existe sólo
para que una app con backend en Python pueda hacer:

    from modelo import analizar_desde_atlas

`detector_rutinas.py` se mantiene intacto y puro (sin pymongo, sin HTTP, sin
rutas de archivo). El import de pymongo aquí es PEREZOSO —dentro de la
función, no arriba del archivo—, así que `from modelo import analizar`
sigue funcionando en un entorno donde pymongo ni siquiera esté instalado.
Eso es lo que permite seguir corriendo el detector en la Raspberry Pi sin
arrastrar dependencias.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from detector_rutinas import analizar, CONFIG

# Los únicos campos que el modelo necesita (fase 1: selección de datos).
CAMPOS = {"deviceId": 1, "action": 1, "confidence": 1, "ts": 1, "_id": 0}


def _mapear(doc):
    """Documento de Mongo -> esquema que espera el modelo."""
    ts = doc["ts"]
    if not isinstance(ts, str):          # si viene como Date de Mongo
        ts = ts.isoformat()
    return {"deviceId": doc["deviceId"], "action": doc["action"],
            "confidence": float(doc.get("confidence", 1.0)), "ts": ts}


def leer_eventos(uri, db="seengo", coll="sign_events", dias=90):
    """Devuelve la lista de eventos de la ventana rodante, ya mapeados."""
    from pymongo import MongoClient          # import perezoso a propósito

    desde = datetime.now(ZoneInfo("UTC")) - timedelta(days=dias)
    # Cubre `ts` guardado como texto ISO o como Date nativo de Mongo.
    filtro = {"$or": [{"ts": {"$gte": desde.isoformat()}},
                      {"ts": {"$gte": desde}}]}
    cli = MongoClient(uri, serverSelectionTimeoutMS=8000)
    return [_mapear(d) for d in cli[db][coll].find(filtro, CAMPOS)]


def analizar_desde_atlas(uri, db="seengo", coll="sign_events", dias=90):
    """Lee Atlas y devuelve el análisis completo, listo para serializar.

    Ejemplo de uso desde un backend Python:

        from modelo import analizar_desde_atlas
        resultado = analizar_desde_atlas(os.environ["MONGO_URI"])
        print(resultado["meta"]["interacciones"])
    """
    eventos = leer_eventos(uri, db, coll, dias)
    ahora = datetime.now(ZoneInfo(CONFIG["tz"]))
    res = analizar(eventos, ahora=ahora)
    res["meta"]["generado"] = ahora.isoformat()
    res["meta"]["fuente"] = "atlas"
    return res
