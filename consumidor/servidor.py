"""
Servidor web en vivo SEENGO
===========================
Tablero que se actualiza SOLO: cada `SEENGO_REFRESH_MIN` minutos consulta
MongoDB, corre el modelo y deja el resultado listo para la pantalla. No hay
que ejecutar Python a mano cada vez.

UNA sola fuente de datos: MongoDB Atlas. No hay set local de respaldo; si
Atlas no responde, la pantalla lo dice claramente en vez de mostrar datos de
ejemplo que se confundirían con los reales.

Pensado para dejarlo corriendo en la Raspberry Pi (ver `seengo.service` para
arrancarlo al encender). Solo usa la librería estándar de Python + pymongo
(la misma dependencia que ya usa el consumidor); no agrega nada pesado.

Arquitectura (importante):
  - El navegador NUNCA habla con Mongo. Habla con ESTE servidor.
  - Este servidor es quien tiene las credenciales (vía `MONGO_URI` / .env) y
    corre el modelo. Así la contraseña jamás llega al navegador.

Rutas que sirve:
  GET /                      -> pantalla/index.html
  GET /resultados.js         -> última corrida (para el modo estático)
  GET /api/resultados.json   -> el resultado completo del modelo
  GET /api/estado            -> salud: cuándo se generó, si hubo error, etc.

Variables de entorno (todas opcionales, con default):
  MONGO_URI / MONGO_DB / MONGO_COLL  -> igual que el consumidor (de .env)
  SEENGO_PORT         (8000)   puerto donde escucha
  SEENGO_DIAS         (90)     ventana rodante en días que se consulta a Mongo
  SEENGO_REFRESH_MIN  (30)     cada cuántos minutos se refresca desde Mongo

Uso:
  python consumidor/servidor.py
  # luego abre http://localhost:8000/  (o http://IP-DE-LA-PI:8000/ desde otro
  # dispositivo de la misma red)
"""
import os, sys, json, time, threading
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Reutiliza la lógica del consumidor (la conexión a Mongo vive allá, no aquí).
sys.path.insert(0, os.path.dirname(__file__))
from consumir_mongo import (  # noqa: E402
    analizar_atlas, CONFIG, leer_sugerencias, responder_sugerencia,
)
from sugerencias import resumen_aceptacion  # noqa: E402

# ----------------------------------------------------------------------
# Configuración desde el entorno
# ----------------------------------------------------------------------
PORT = int(os.environ.get("SEENGO_PORT", "8000"))
DIAS = int(os.environ.get("SEENGO_DIAS", "90"))
REFRESH_MIN = int(os.environ.get("SEENGO_REFRESH_MIN", "30"))

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PANTALLA = os.path.join(RAIZ, "pantalla")
RESULTADOS_JS = os.path.join(PANTALLA, "resultados.js")

# Estado compartido entre el hilo que refresca y los que atienden HTTP.
# UNA sola fuente: Atlas. Ya no hay set local de respaldo — si Atlas falla se
# conserva la última foto BUENA y se marca como obsoleta, que no es lo mismo
# que inventar datos.
_lock = threading.Lock()
_estado = {"resultado": None, "error": None}


def _ahora_local():
    return datetime.now(ZoneInfo(CONFIG["tz"]))


# ----------------------------------------------------------------------
# Refresco
# ----------------------------------------------------------------------
def refrescar():
    """Lee Atlas y corre el modelo. Lanza RuntimeError si Atlas no responde."""
    res = analizar_atlas(DIAS)
    with _lock:
        _estado["resultado"] = res
        _estado["error"] = None
    _escribir_js()
    return res


def _refrescar_solo_sugerencias():
    """Tras una respuesta del usuario: relee la colección de sugerencias y
    actualiza el resumen sin re-consultar todos los eventos."""
    resumen = resumen_aceptacion(leer_sugerencias())
    with _lock:
        if _estado["resultado"] is not None:
            _estado["resultado"]["sugerencias"] = resumen
            _estado["resultado"]["meta"]["generado"] = _ahora_local().isoformat()
    _escribir_js()
    return resumen


# ----------------------------------------------------------------------
# Persistencia para el modo estático (file://)
# ----------------------------------------------------------------------
def _resultado():
    with _lock:
        return _estado["resultado"]


def _escribir_js():
    """Deja pantalla/resultados.js al día, para que abrir el HTML con doble
    clic (sin servidor) también muestre la última corrida buena."""
    res = _resultado()
    if res is None:
        return
    with open(RESULTADOS_JS, "w", encoding="utf-8") as fh:
        fh.write("window.SEENGO_RESULTADOS = ")
        json.dump(res, fh, ensure_ascii=False, default=str)
        fh.write(";")


def _bucle_refresco():
    """Hilo de fondo: refresca cada REFRESH_MIN minutos, para siempre."""
    while True:
        time.sleep(REFRESH_MIN * 60)
        try:
            res = refrescar()
            print(f"[servidor] refrescado {res['meta']['generado']} — "
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
        if ruta == "/api/resultados.json":
            res = _resultado()
            return self._json(res if res is not None
                              else {"error": "aún sin datos de Atlas",
                                    "streams": []})
        if ruta == "/api/estado":
            res = _resultado()
            with _lock:
                error = _estado["error"]
            return self._json({
                "ok": res is not None and error is None,
                "fuente": "atlas",
                "generado": (res or {}).get("meta", {}).get("generado"),
                "error": error,
                "dias": DIAS, "refresh_min": REFRESH_MIN, "puerto": PORT,
            })
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

    def end_headers(self):
        # Nada de caché, para NINGUNA respuesta (incluidos index.html y
        # resultados.js). Durante la demo la página se recarga muchas veces y
        # un archivo viejo en caché muestra datos que ya no existen: pasó en
        # pruebas: el navegador seguía corriendo una versión anterior del
        # tablero mientras el disco ya tenía la nueva.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def _json(self, obj, status=200):
        cuerpo = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def log_message(self, *args):
        pass  # silencioso: el refresco ya imprime lo relevante


def main():
    print(f"SEENGO servidor en vivo · puerto {PORT} · ventana {DIAS} d · "
          f"refresco cada {REFRESH_MIN} min")

    # Primer refresco. Si Atlas no responde ahora, el servidor igual levanta:
    # la pantalla mostrará el estado de "sin conexión" en vez de datos falsos.
    try:
        refrescar()
        print("[servidor] datos iniciales de Atlas listos")
    except Exception as e:                              # noqa: BLE001
        with _lock:
            _estado["error"] = f"{type(e).__name__}: {e}"
        print(f"[servidor] primer refresco de Atlas falló: {e}")

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
