"""
Servidor web en vivo SEENGO
===========================
Tablero que se actualiza SOLO: cada `SEENGO_REFRESH_MIN` minutos consulta
MongoDB, corre el modelo y deja el resultado listo para la pantalla. No hay
que ejecutar Python a mano cada vez.

Sirve DOS fuentes de datos que la pantalla ofrece en una pestaña:
  - atlas -> lo real de MongoDB Atlas (se refresca en vivo)
  - local -> el set de ejemplo del repo (referencia, se calcula 1 vez al arrancar)

Pensado para dejarlo corriendo en la Raspberry Pi (ver `seengo.service` para
arrancarlo al encender). Solo usa la librería estándar de Python + pymongo
(la misma dependencia que ya usa el consumidor); no agrega nada pesado.

Arquitectura (importante):
  - El navegador NUNCA habla con Mongo. Habla con ESTE servidor.
  - Este servidor es quien tiene las credenciales (vía `MONGO_URI` / .env) y
    corre el modelo. Así la contraseña jamás llega al navegador.

Rutas que sirve:
  GET /                      -> pantalla/index.html
  GET /resultados.js         -> últimas fuentes (para el modo estático)
  GET /api/fuentes.json      -> {"atlas": ..., "local": ...}  (lo que sondea la vista)
  GET /api/resultados.json   -> solo atlas (compatibilidad)
  GET /api/estado            -> salud: cuándo se generó, si hubo error, etc.

Variables de entorno (todas opcionales, con default):
  MONGO_URI / MONGO_DB / MONGO_COLL  -> igual que el consumidor (de .env)
  SEENGO_PORT         (8000)   puerto donde escucha
  SEENGO_DIAS         (60)     ventana rodante en días que se consulta a Mongo
  SEENGO_REFRESH_MIN  (30)     cada cuántos minutos se refresca desde Mongo

Uso:
  python consumidor/servidor.py
  # luego abre http://localhost:8000/  (o http://IP-DE-LA-PI:8000/ desde otro
  # dispositivo de la misma red)
"""
import os, sys, json, time, glob, threading
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Reutiliza la lógica de Mongo y de archivos del consumidor (no se duplica).
sys.path.insert(0, os.path.dirname(__file__))
from consumir_mongo import (  # noqa: E402
    leer_mongo, leer_archivos, analizar, CONFIG,
    sugerencias_local, sugerencias_atlas, leer_sugerencias,
    responder_sugerencia,
)
from sugerencias import resumen_aceptacion  # noqa: E402

# ----------------------------------------------------------------------
# Configuración desde el entorno
# ----------------------------------------------------------------------
PORT = int(os.environ.get("SEENGO_PORT", "8000"))
DIAS = int(os.environ.get("SEENGO_DIAS", "60"))
REFRESH_MIN = int(os.environ.get("SEENGO_REFRESH_MIN", "30"))

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PANTALLA = os.path.join(RAIZ, "pantalla")
RESULTADOS_JS = os.path.join(PANTALLA, "resultados.js")
EJEMPLOS = os.path.join(RAIZ, "datos", "ejemplos_*.json")

# Estado compartido entre el hilo que refresca y los que atienden HTTP.
_lock = threading.Lock()
_estado = {"atlas": None, "local": None, "error": None}


def _ahora_local():
    return datetime.now(ZoneInfo(CONFIG["tz"]))


# ----------------------------------------------------------------------
# Cálculo de cada fuente
# ----------------------------------------------------------------------
def _analizar(eventos, ahora, fuente):
    res = analizar(eventos, ahora=ahora)
    res["meta"]["generado"] = _ahora_local().isoformat()
    res["meta"]["fuente"] = fuente
    return res


def calcular_local():
    """Corre el modelo sobre los JSON de ejemplo del repo. Estático: 1 sola vez."""
    archivos = sorted(glob.glob(EJEMPLOS))
    if not archivos:
        print(f"[servidor] sin datos de ejemplo en {EJEMPLOS} (fuente 'local' omitida)")
        return None
    eventos = leer_archivos([EJEMPLOS])
    res = _analizar(eventos, None, "local")    # ahora=None: sin racha abierta
    res["sugerencias"] = sugerencias_local()
    return res


def refrescar_atlas():
    """Lee Mongo y corre el modelo. Lanza excepción si algo falla."""
    eventos = leer_mongo(DIAS)
    res = _analizar(eventos, _ahora_local(), "atlas")   # detecta ausencia en curso
    res["sugerencias"] = sugerencias_atlas(res)  # publica nuevas + lee respuestas
    with _lock:
        _estado["atlas"] = res
        _estado["error"] = None
    _escribir_js()
    return res


def _refrescar_solo_sugerencias():
    """Tras una respuesta del usuario: relee la colección de sugerencias y
    actualiza el resumen de atlas sin re-consultar todos los eventos."""
    resumen = resumen_aceptacion(leer_sugerencias())
    with _lock:
        if _estado["atlas"] is not None:
            _estado["atlas"]["sugerencias"] = resumen
            _estado["atlas"]["meta"]["generado"] = _ahora_local().isoformat()
    _escribir_js()
    return resumen


# ----------------------------------------------------------------------
# Persistencia para el modo estático (file://)
# ----------------------------------------------------------------------
def _envelope():
    with _lock:
        return {"atlas": _estado["atlas"], "local": _estado["local"]}


def _escribir_js():
    """Deja pantalla/resultados.js al día con ambas fuentes, para que el modo
    estático (doble clic en index.html) también las muestre."""
    env = _envelope()
    with open(RESULTADOS_JS, "w", encoding="utf-8") as fh:
        fh.write("window.SEENGO_FUENTES = ")
        json.dump(env, fh, ensure_ascii=False, default=str)
        fh.write(";")


def _bucle_refresco():
    """Hilo de fondo: refresca atlas cada REFRESH_MIN minutos, para siempre."""
    while True:
        time.sleep(REFRESH_MIN * 60)
        try:
            res = refrescar_atlas()
            print(f"[servidor] atlas refrescado {res['meta']['generado']} — "
                  f"{res['meta']['interacciones']} interacciones")
        except Exception as e:                          # noqa: BLE001
            with _lock:
                _estado["error"] = f"{type(e).__name__}: {e}"
            print(f"[servidor] ERROR al refrescar (se conserva la foto previa): {e}")


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------
class Handler(SimpleHTTPRequestHandler):
    """Sirve la carpeta pantalla/ + los endpoints JSON."""

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=PANTALLA, **kw)

    def do_GET(self):
        ruta = self.path.split("?")[0]
        if ruta == "/api/fuentes.json":
            return self._json(_envelope())
        if ruta == "/api/resultados.json":          # compatibilidad: solo atlas
            with _lock:
                atlas = _estado["atlas"]
            return self._json(atlas if atlas is not None
                              else {"error": "aún sin datos", "streams": []})
        if ruta == "/api/estado":
            with _lock:
                salud = {
                    "atlas_generado": (_estado["atlas"] or {}).get("meta", {}).get("generado"),
                    "local_generado": (_estado["local"] or {}).get("meta", {}).get("generado"),
                    "error": _estado["error"],
                }
            salud.update({"dias": DIAS, "refresh_min": REFRESH_MIN, "puerto": PORT})
            return self._json(salud)
        return super().do_GET()

    def do_POST(self):
        """POST /api/sugerencias/responder  {"clave": ..., "aceptada": 1|0}
        Registra en Mongo la respuesta del usuario a una sugerencia."""
        if self.path.split("?")[0] != "/api/sugerencias/responder":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            datos = json.loads(self.rfile.read(n) or b"{}")
            clave = datos["clave"]
            aceptada = int(datos["aceptada"])
            assert aceptada in (0, 1)
        except Exception:                              # noqa: BLE001
            return self._json({"ok": False, "error": "cuerpo inválido"}, 400)
        try:
            if not responder_sugerencia(clave, aceptada):
                return self._json({"ok": False, "error": "clave no encontrada"}, 404)
            resumen = _refrescar_solo_sugerencias()
            print(f"[servidor] sugerencia respondida: {clave} -> "
                  f"{'sí' if aceptada else 'no'}")
            return self._json({"ok": True, "sugerencias": resumen})
        except Exception as e:                          # noqa: BLE001
            return self._json({"ok": False, "error": str(e)}, 500)

    def _json(self, obj, status=200):
        cuerpo = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def log_message(self, *args):
        pass  # silencioso: el refresco ya imprime lo relevante


def main():
    print(f"SEENGO servidor en vivo · puerto {PORT} · ventana {DIAS} d · "
          f"refresco cada {REFRESH_MIN} min")

    # 1) Fuente local (estática): se calcula una vez y no cambia.
    try:
        local = calcular_local()
        with _lock:
            _estado["local"] = local
    except Exception as e:                              # noqa: BLE001
        print(f"[servidor] no se pudo calcular 'local': {e}")

    # 2) Primer refresco de atlas. Si Mongo no responde ahora, seguimos y
    #    servimos lo que haya (incluida la fuente local ya calculada).
    try:
        refrescar_atlas()
        print("[servidor] datos iniciales de Atlas listos")
    except Exception as e:                              # noqa: BLE001
        with _lock:
            _estado["error"] = f"{type(e).__name__}: {e}"
        print(f"[servidor] primer refresco de Atlas falló: {e}")

    _escribir_js()   # deja resultados.js con lo que se haya podido calcular

    threading.Thread(target=_bucle_refresco, daemon=True).start()

    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Listo. Abre http://localhost:{PORT}/  (Ctrl+C para detener)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDetenido.")
        httpd.server_close()


if __name__ == "__main__":
    main()
