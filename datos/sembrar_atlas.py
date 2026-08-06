"""
Siembra de datos de prueba directamente en MongoDB Atlas.
=========================================================
Inserta en la colección real las TRES capas de datos que define
`datos/generar_datos.py`. No escribe archivos: el destino es Atlas.

Por qué las tres capas van SIEMPRE juntas
-----------------------------------------
La base tiene que quedar sucia a propósito. Si en Mongo sólo hay datos
limpios, la fase de preprocesamiento no tiene nada que demostrar: el embudo
sale plano y no se ve al modelo limpiando. La capa de basura no es un extra.

  claros     -> el DBSCAN encuentra los patrones          (fase 3)
  difíciles  -> se distingue candidata de confirmada      (fase 4)
  basura     -> se ve la limpieza funcionando en vivo     (fase 2)

Idempotencia
------------
Cada documento sembrado se marca con `origen: "seed"` y una etiqueta de
corrida (`corrida`). `--limpiar` borra SOLO los documentos con
`origen: "seed"`, así que los eventos reales de la aplicación nunca se tocan.

Uso:
    python datos/sembrar_atlas.py --dry-run          # cuenta, no escribe
    python datos/sembrar_atlas.py --limpiar          # borra siembra previa y resiembra
    python datos/sembrar_atlas.py --capas claros,basura

La conexión se lee de MONGO_URI / MONGO_DB / MONGO_COLL (archivo .env).
Nunca escribas la URI aquí.
"""
import os
import sys
import argparse
from datetime import datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Las generadoras viven en generar_datos.py y son la única fuente de verdad
# de los datos de prueba: aquí sólo se cambia el DESTINO (Atlas en vez de
# archivos). Si falta un caso, se extiende allá, no se duplica aquí.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generar_datos import (  # noqa: E402
    ejemplos_claros, ejemplos_dificiles, ejemplos_basura,
    configurar_calendario, DIAS_HUECO, DIAS_TRAMO_B, TZ,
)

CAPAS = {
    "claros": ejemplos_claros,
    "dificiles": ejemplos_dificiles,
    "basura": ejemplos_basura,
}


def _coleccion():
    """Abre la colección real. Sin URI no hay nada que hacer: se sale con
    código distinto de cero (el pipeline no debe continuar a ciegas)."""
    try:
        from pymongo import MongoClient
    except ImportError:
        sys.exit("Falta pymongo. Instala con:  pip install pymongo")
    uri = os.environ.get("MONGO_URI")
    if not uri:
        sys.exit("Define MONGO_URI en el entorno o en .env "
                 "(no la escribas en el código).")
    db = os.environ.get("MONGO_DB", "seengo")
    coll = os.environ.get("MONGO_COLL", "sign_events")
    cli = MongoClient(uri, serverSelectionTimeoutMS=8000)
    return cli[db][coll], f"{db}.{coll}"


def _primer_dia_real(col):
    """Fecha LOCAL del evento REAL (no sembrado) más antiguo, o None.

    Es la pieza clave para colocar el hueco de ausencia: si el hueco cae
    encima de días que sí tienen actividad real, se rellena y la alerta nunca
    se dispara. Anclando el tramo B al primer día real, el hueco queda
    genuinamente vacío y la actividad "se reanuda" justo con los datos de la
    aplicación.
    """
    docs = list(col.find({"origen": {"$ne": "seed"}}, {"ts": 1, "_id": 0})
                   .sort("ts", 1).limit(1))
    if not docs:
        return None
    ts = docs[0]["ts"]
    try:
        dt = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    loc = dt.astimezone(TZ)
    return datetime(loc.year, loc.month, loc.day)


def _verificar_esquema(col):
    """Lee un documento real y avisa si el esquema no es el que se va a
    insertar. La colección es la que manda: si cambió, hay que enterarse
    ANTES de meter 3000 documentos con la forma equivocada."""
    doc = col.find_one({}, {"_id": 0})
    if not doc:
        print("  (colección vacía: no hay esquema previo contra qué comparar)")
        return
    esperados = {"userId", "gesture", "confidence", "deviceId", "action", "ts"}
    reales = set(doc.keys())
    print(f"  esquema real de la colección: {sorted(reales)}")
    faltan = esperados - reales
    sobran = reales - esperados - {"origen", "corrida"}
    if faltan:
        print(f"  AVISO: la colección no trae {sorted(faltan)}")
    if sobran:
        print(f"  AVISO: la colección trae campos extra {sorted(sobran)} "
              f"(se ignoran al sembrar)")
    if not faltan and not sobran:
        print("  coincide con lo que se va a insertar. OK")


def main():
    ap = argparse.ArgumentParser(
        description="Siembra las capas de datos de prueba en MongoDB Atlas.")
    ap.add_argument("--limpiar", action="store_true",
                    help="Borra la siembra anterior (origen='seed') antes de insertar. "
                         "No toca los eventos reales de la aplicación.")
    ap.add_argument("--dias", type=int, default=90,
                    help="Ventana en días con la que después se leerá la colección "
                         "(default 90). Sirve para avisar si el calendario sembrado "
                         "no cabría en esa ventana.")
    ap.add_argument("--capas", default="claros,dificiles,basura",
                    help="Capas a sembrar, separadas por coma (default: las tres).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Sólo cuenta lo que insertaría; no escribe en Atlas.")
    args = ap.parse_args()

    pedidas = [c.strip() for c in args.capas.split(",") if c.strip()]
    desconocidas = [c for c in pedidas if c not in CAPAS]
    if desconocidas:
        sys.exit(f"Capa(s) desconocida(s): {desconocidas}. "
                 f"Válidas: {sorted(CAPAS)}")
    if "basura" not in pedidas:
        print("AVISO: sin la capa 'basura' el embudo de preprocesamiento "
              "queda plano y no hay limpieza que demostrar.\n")

    # Conectar primero: el calendario depende de dónde estén los datos reales.
    col = destino = None
    try:
        col, destino = _coleccion()
    except SystemExit:
        if not args.dry_run:
            raise
        print("(sin conexión a Atlas: --dry-run usa el calendario por defecto)\n")

    # Colocar el hueco ANTES de los datos reales. Si el hueco cayera encima de
    # días con actividad real, se rellenaría y la alerta jamás se dispararía.
    fin = None
    primer_real = _primer_dia_real(col) if col is not None else None
    if primer_real is not None:
        # El tramo B arranca justo el día en que empiezan los datos reales:
        # así la actividad "se reanuda" a la vez que la de la aplicación.
        fin = primer_real + timedelta(days=DIAS_TRAMO_B - 1)

    cal = configurar_calendario(fin)
    print("Calendario de siembra:")
    print(f"  tramo A : {cal['tramo_a'][0]} -> {cal['tramo_a'][1]}")
    print(f"  HUECO   : {cal['hueco'][0]} -> {cal['hueco'][1]}  "
          f"({DIAS_HUECO} días sin actividad -> alerta de ausencia)")
    print(f"  tramo B : {cal['tramo_b'][0]} -> {cal['tramo_b'][1]}")
    if primer_real is not None:
        print(f"  (anclado a los datos reales, que empiezan el "
              f"{primer_real.date()}: el hueco queda justo antes)")

    # Aviso de ventana: si el inicio del calendario queda fuera de la ventana
    # con la que se leerá después, el tablero saldría a medias.
    hoy = datetime.now(TZ)
    antiguedad = (datetime(hoy.year, hoy.month, hoy.day) - cal["inicio"]).days
    print(f"  antigüedad del dato más viejo: {antiguedad} días")
    if antiguedad > args.dias:
        print(f"  AVISO: no cabe en una ventana de {args.dias} días. "
              f"Lee con --dias {antiguedad + 5} o mayor.")
    else:
        print(f"  cabe en la ventana de {args.dias} días. OK")
    print()

    # Generar por capa, marcando cada documento con su procedencia.
    corrida = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    porCapa, docs = {}, []
    for nombre in pedidas:
        eventos = CAPAS[nombre]()
        for e in eventos:
            e["origen"] = "seed"       # permite --limpiar sin tocar lo real
            e["capa"] = nombre         # permite contar por capa en Atlas
            e["corrida"] = corrida
        porCapa[nombre] = len(eventos)
        docs.extend(eventos)

    print("Eventos generados por capa:")
    for nombre in pedidas:
        print(f"  {nombre:12s} {porCapa[nombre]:6d}")
    print(f"  {'TOTAL':12s} {len(docs):6d}\n")

    if args.dry_run:
        print("--dry-run: no se escribió nada en Atlas.")
        return

    print(f"Destino: {destino}")
    _verificar_esquema(col)
    print()

    if args.limpiar:
        borrados = col.delete_many({"origen": "seed"}).deleted_count
        print(f"--limpiar: {borrados} documentos de siembra previa borrados "
              f"(los reales no se tocaron).")

    col.insert_many(docs, ordered=False)
    print(f"Insertados: {len(docs)} documentos.\n")

    # Conteo REAL leído de vuelta de Atlas: es lo que se compara contra el
    # primer escalón del embudo en el tablero.
    print("Conteo verificado en Atlas:")
    for nombre in pedidas:
        print(f"  capa {nombre:12s} {col.count_documents({'origen': 'seed', 'capa': nombre}):6d}")
    sembrados = col.count_documents({"origen": "seed"})
    total = col.count_documents({})
    print(f"  {'sembrados':17s} {sembrados:6d}")
    print(f"  {'reales (app)':17s} {total - sembrados:6d}")
    print(f"  {'TOTAL colección':17s} {total:6d}")


if __name__ == "__main__":
    main()
