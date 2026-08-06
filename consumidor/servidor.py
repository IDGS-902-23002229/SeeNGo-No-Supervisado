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
  GET  /                        -> pantalla/index.html
  GET  /resultados.js           -> última corrida (para el modo estático)
  GET  /api/salud               -> {ok, ultima_corrida, error, fuente, dias_ventana}
  GET  /api/resultado           -> el resultado completo del modelo
  GET  /api/rutinas?vista=...   -> sólo rutinas confirmadas (lo que usa la app)
  GET  /api/alertas             -> ausencia larga y avisos activos
  GET  /api/recomendaciones     -> capa prescriptiva + aceptación 1/0
  POST /api/refrescar           -> fuerza recálculo inmediato
  POST /api/recomendaciones/responder  {clave, aceptada: 1|0}

Todas responden JSON con CORS abierto, para que la app móvil pueda
consumirlas desde otro origen.

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
    analizar_atlas, CONFIG, responder_recomendacion, refrescar_respuestas,
)

# ----------------------------------------------------------------------
# Configuración desde el entorno
# ----------------------------------------------------------------------
PORT = int(os.environ.get("SEENGO_PORT", "8000"))
DIAS = int(os.environ.get("SEENGO_DIAS", "90"))
REFRESH_MIN = int(os.environ.get("SEENGO_REFRESH_MIN", "30"))

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PANTALLA = os.path.join(RAIZ, "pantalla")
RESULTADOS_JS = os.path.join(PANTALLA, "resultados.js")
ULTIMO_BUENO = os.path.join(RAIZ, "resultados.json")   # red de seguridad

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


def _refrescar_solo_recomendaciones():
    """Tras una respuesta del usuario: relee sólo las respuestas y las remapea
    sobre las recomendaciones de la corrida actual, sin volver a consultar los
    miles de eventos."""
    with _lock:
        actual = ((_estado["resultado"] or {}).get("recomendaciones")) or {}
    resumen = refrescar_respuestas(actual)
    with _lock:
        if _estado["resultado"] is not None:
            _estado["resultado"]["recomendaciones"] = resumen
            _estado["resultado"]["meta"]["generado"] = _ahora_local().isoformat()
    _escribir_js()
    return resumen


# ----------------------------------------------------------------------
# Persistencia: red de seguridad para la demo
# ----------------------------------------------------------------------
# Cada corrida BUENA se guarda en disco. Si mañana se cae el wifi de la
# escuela, el tablero sigue mostrando datos REALES de la última corrida,
# marcados como `obsoleto: true` y con su hora, en vez de quedarse en blanco.
# Esto NO es volver a datos de ejemplo: es el último resultado real, y la
# pantalla lo dice.
def _resultado():
    with _lock:
        return _estado["resultado"]


def _escribir_js():
    """Deja pantalla/resultados.js y resultados.json al día, para que abrir el
    HTML con doble clic (sin servidor) también muestre la última corrida."""
    res = _resultado()
    if res is None:
        return
    with open(RESULTADOS_JS, "w", encoding="utf-8") as fh:
        fh.write("window.SEENGO_RESULTADOS = ")
        json.dump(res, fh, ensure_ascii=False, default=str)
        fh.write(";")
    with open(ULTIMO_BUENO, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=2, default=str)


def _cargar_ultimo_bueno():
    """Recupera del disco la última corrida buena y la marca como obsoleta."""
    try:
        with open(ULTIMO_BUENO, encoding="utf-8") as fh:
            res = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not isinstance(res, dict) or "meta" not in res:
        return None
    res["meta"]["obsoleto"] = True
    print(f"[servidor] usando la última corrida buena del disco "
          f"({res['meta'].get('generado')}), marcada como obsoleta.")
    return res


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

    # -- GET -------------------------------------------------------------
    def do_GET(self):
        ruta = self.path.split("?")[0]
        res = _resultado()

        if ruta == "/api/salud":
            with _lock:
                error = _estado["error"]
            return self._json({
                "ok": res is not None and error is None,
                "fuente": "atlas",
                "ultima_corrida": (res or {}).get("meta", {}).get("generado"),
                "obsoleto": bool((res or {}).get("meta", {}).get("obsoleto")),
                "error": error,
                "dias_ventana": DIAS,
                "refresh_min": REFRESH_MIN,
                "puerto": PORT,
            })

        if res is None:
            if ruta.startswith("/api/"):
                return self._json(
                    {"error": "aún sin datos de Atlas", "obsoleto": False}, 503)
            return super().do_GET()

        if ruta == "/api/resultado":
            return self._json(res)

        if ruta == "/api/rutinas":
            # Lo que consume la app móvil: sólo lo confirmado, ya aplanado,
            # sin obligarla a recorrer streams ni vistas.
            vista = self._param("vista", "entre_semana")
            rutinas = []
            for s in res.get("streams", []):
                v = s.get("vistas", {}).get(vista)
                if not v:
                    continue
                for r in v.get("rutinas", []):
                    if r["confirmada"]:
                        rutinas.append({**r, "device": s["device"],
                                        "action": s["action"]})
            rutinas.sort(key=lambda r: r["hora"])
            return self._json({
                "vista": vista,
                "generado": res["meta"].get("generado"),
                "obsoleto": bool(res["meta"].get("obsoleto")),
                "total": len(rutinas),
                "rutinas": rutinas,
            })

        if ruta == "/api/alertas":
            aus = res.get("ausencia_larga") or {}
            alertas = []
            if aus.get("nivel") in ("aviso", "alerta"):
                alertas.append({
                    "tipo": "ausencia_larga",
                    "nivel": aus["nivel"],
                    "mensaje": aus.get("mensaje"),
                    "dias": aus.get("hueco_maximo_dias"),
                    "ventana": aus.get("ventana"),
                })
            return self._json({
                "generado": res["meta"].get("generado"),
                "obsoleto": bool(res["meta"].get("obsoleto")),
                "total": len(alertas),
                "alertas": alertas,
            })

        if ruta == "/api/recomendaciones":
            rec = res.get("recomendaciones") or {}
            return self._json({
                "generado": res["meta"].get("generado"),
                "obsoleto": bool(res["meta"].get("obsoleto")),
                **rec,
            })

        return super().do_GET()

    # -- POST ------------------------------------------------------------
    def do_POST(self):
        ruta = self.path.split("?")[0]

        if ruta == "/api/refrescar":
            # Botón "Actualizar ahora" del tablero: recalcula sin esperar al
            # ciclo automático.
            try:
                r = refrescar()
                print(f"[servidor] refresco manual: {r['meta']['generado']}")
                return self._json({"ok": True,
                                   "generado": r["meta"]["generado"],
                                   "interacciones": r["meta"]["interacciones"]})
            except Exception as e:                      # noqa: BLE001
                with _lock:
                    _estado["error"] = f"{type(e).__name__}: {e}"
                return self._json({"ok": False, "error": str(e)}, 502)

        if ruta == "/api/recomendaciones/responder":
            # {"clave": ..., "aceptada": 1|0} — la señal binaria con la que se
            # mide si el conocimiento extraído le sirve al usuario.
            try:
                n = int(self.headers.get("Content-Length", 0))
                datos = json.loads(self.rfile.read(n) or b"{}")
                clave = datos["clave"]
                aceptada = int(datos["aceptada"])
                assert aceptada in (0, 1)
            except Exception:                           # noqa: BLE001
                return self._json({"ok": False, "error": "cuerpo inválido"}, 400)
            try:
                if not responder_recomendacion(clave, aceptada):
                    return self._json(
                        {"ok": False, "error": "clave no encontrada"}, 404)
                resumen = _refrescar_solo_recomendaciones()
                print(f"[servidor] recomendación respondida: {clave} -> "
                      f"{'sí' if aceptada else 'no'}")
                return self._json({"ok": True, "recomendaciones": resumen})
            except Exception as e:                      # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, 500)

        return self._json({"error": "ruta no encontrada"}, 404)

    def do_OPTIONS(self):
        """Preflight de CORS: sin esto el navegador de la app móvil bloquea
        cualquier POST hecho desde otro origen."""
        self.send_response(204)
        self.end_headers()

    def _param(self, nombre, defecto=None):
        from urllib.parse import urlparse, parse_qs
        valores = parse_qs(urlparse(self.path).query).get(nombre)
        return valores[0] if valores else defecto

    def end_headers(self):
        # Nada de caché, para NINGUNA respuesta (incluidos index.html y
        # resultados.js). Durante la demo la página se recarga muchas veces y
        # un archivo viejo en caché muestra datos que ya no existen: pasó en
        # pruebas: el navegador seguía corriendo una versión anterior del
        # tablero mientras el disco ya tenía la nueva.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        # CORS: la app móvil consume esta API desde otro origen. Sin estas
        # cabeceras el navegador bloquea la respuesta.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
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

    # Primer refresco. Si Atlas no responde ahora, el servidor igual levanta y
    # recurre a la última corrida buena guardada en disco, marcada como
    # obsoleta. Si tampoco hay, la pantalla muestra "sin conexión con la base".
    try:
        refrescar()
        print("[servidor] datos iniciales de Atlas listos")
    except Exception as e:                              # noqa: BLE001
        with _lock:
            _estado["error"] = f"{type(e).__name__}: {e}"
        print(f"[servidor] primer refresco de Atlas falló: {e}")
        previo = _cargar_ultimo_bueno()
        if previo is not None:
            with _lock:
                _estado["resultado"] = previo

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
