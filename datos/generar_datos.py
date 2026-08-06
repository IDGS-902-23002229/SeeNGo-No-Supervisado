"""
Generador de datos de prueba para el detector de rutinas SEENGO.
================================================================
Define las TRES CAPAS de datos de prueba sobre un mismo calendario de ~58
días, con un HUECO GLOBAL de 25 días sin ningún evento (para disparar el
detector de ausencia larga):

  - ejemplos_claros()     rutinas nítidas que el modelo DEBE confirmar
  - ejemplos_dificiles()  casos límite: medianoche, doble rutina, drift,
                          confianza baja, cobertura insuficiente
  - ejemplos_basura()     ruido y datos sucios que el modelo DEBE limpiar

Estas tres funciones son la única fuente de verdad de los datos de prueba.
Ejecutar este archivo las escribe a JSON; `datos/sembrar_atlas.py` las
importa y las inserta directamente en MongoDB Atlas (que es el camino real
de la entrega). No dupliques la lógica: extiéndela aquí.

Los timestamps se guardan en UTC (igual que tus datos reales), con la
hora LOCAL objetivo en America/Mexico_City (UTC-6, sin horario de verano).

Cada "interacción" se emite como una RÁFAGA de varios eventos en pocos
segundos (así el modelo tiene que hacer debounce para contar bien).

Ejecutar:  python datos/generar_datos.py
"""
import json, random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Mexico_City")
UTC = ZoneInfo("UTC")
USER = "migueldr12"
rng = random.Random(42)  # determinista: los resultados documentados coinciden

GESTO = {"on": "palma_abierta", "off": "puno", "toggle": "paz"}

# --- Calendario maestro (anclado a HOY, no a fechas fijas) ---------------
# ANTES este calendario eran fechas fijas de may-jun 2026. Problema real: el
# consumidor lee una VENTANA RODANTE de 60 días, así que al correr el proyecto
# meses después la mayor parte de los datos quedaba FUERA de la ventana y el
# tablero salía medio vacío. Ahora se construye hacia atrás desde ayer:
#
#   [ tramo A: 26 d ][ HUECO: 25 d ][ tramo B: 7 d ]
#   \________________ 58 días en total ___________/   -> cabe en los 60
#
# El hueco de 25 días es el que dispara la ALERTA de ausencia (umbral 25).
DIAS_TRAMO_A = 26
DIAS_HUECO   = 25
DIAS_TRAMO_B = 7

INICIO_A = FIN_A = INICIO_B = FIN_B = None   # los fija configurar_calendario()


def configurar_calendario(fin=None):
    """(Re)ancla el calendario maestro.

    `fin` es el último día CON actividad sembrada; por defecto AYER, para no
    sembrar eventos con fecha futura. Los generadores leen estas fechas en el
    momento de ser llamados, así que basta con invocar esta función antes de
    generar. Devuelve el resumen de fechas.

    OJO con dónde cae el hueco: si la colección ya tiene eventos REALES, el
    hueco de ausencia no puede caer encima de ellos o deja de estar vacío y la
    alerta nunca se dispara. `sembrar_atlas.py` calcula `fin` justo para eso.
    """
    global INICIO_A, FIN_A, INICIO_B, FIN_B
    if fin is None:
        hoy = datetime.now(TZ)
        fin = datetime(hoy.year, hoy.month, hoy.day) - timedelta(days=1)
    FIN_B    = fin
    INICIO_B = FIN_B - timedelta(days=DIAS_TRAMO_B - 1)
    FIN_A    = INICIO_B - timedelta(days=DIAS_HUECO + 1)
    INICIO_A = FIN_A - timedelta(days=DIAS_TRAMO_A - 1)
    return {
        "inicio":  INICIO_A,          # datetime, para calcular antigüedad
        "fin":     FIN_B,
        "tramo_a": (INICIO_A.date(), FIN_A.date()),
        "hueco":   ((FIN_A + timedelta(days=1)).date(),
                    (INICIO_B - timedelta(days=1)).date()),
        "tramo_b": (INICIO_B.date(), FIN_B.date()),
    }


configurar_calendario()   # anclado al importar; se puede re-anclar después


def dias(desde, hasta, solo=None):
    """Genera fechas [desde, hasta]. solo='semana'|'finde'|None."""
    d = desde
    while d <= hasta:
        wd = d.weekday()  # 0=Lun .. 6=Dom
        if solo is None or (solo == "semana" and wd <= 4) or (solo == "finde" and wd >= 5):
            yield d
        d += timedelta(days=1)


def ts_utc(fecha, hora_local, minuto=0):
    """Construye el timestamp UTC ISO a partir de una hora LOCAL objetivo."""
    h = int(hora_local) % 24
    m = int(round((hora_local - int(hora_local)) * 60)) + minuto
    base_local = datetime(fecha.year, fecha.month, fecha.day, tzinfo=TZ) \
        + timedelta(hours=h, minutes=m)
    return base_local.astimezone(UTC)


def rafaga(salida, fecha, hora_local, device, action, n=None,
           conf=(0.80, 1.0), jitter_min=0.0):
    """Emite una interacción como ráfaga de n eventos en pocos segundos."""
    n = n if n is not None else rng.randint(5, 20)
    jm = rng.uniform(-jitter_min, jitter_min)
    t0 = ts_utc(fecha, hora_local, minuto=jm)
    t = t0
    for _ in range(n):
        salida.append({
            "userId": USER,
            "gesture": GESTO[action],
            "confidence": round(rng.uniform(*conf), 2),
            "deviceId": device,
            "action": action,
            "ts": t.isoformat(),
        })
        t = t + timedelta(seconds=rng.uniform(1.5, 5.0))


# =======================================================================
# 1) EJEMPLOS CLAROS  — el modelo DEBE detectar estas rutinas
# =======================================================================
def ejemplos_claros():
    ev = []
    # R1  foco-sala/on  ~19:30 entre semana  -> rutina noche (CONFIRMADA)
    for f in dias(INICIO_A, FIN_A, "semana"):
        rafaga(ev, f, 19.5, "foco-sala", "on", jitter_min=12)
    for f in dias(INICIO_B, FIN_B, "semana"):
        rafaga(ev, f, 19.5, "foco-sala", "on", jitter_min=12)
    # R2  foco-sala/off ~23:10 entre semana  -> apagar antes de dormir
    for f in dias(INICIO_A, FIN_A, "semana"):
        rafaga(ev, f, 23 + 10/60, "foco-sala", "off", jitter_min=10)
    # R3  foco-sala/on  ~21:00 FIN DE SEMANA -> mismo par, otra hora
    for f in dias(INICIO_A, FIN_A, "finde"):
        rafaga(ev, f, 21.0, "foco-sala", "on", jitter_min=15)
    # R4  foco-recamara/off ~07:00 entre semana -> salir de casa
    for f in dias(INICIO_A, FIN_A, "semana"):
        rafaga(ev, f, 7.0, "foco-recamara", "off", jitter_min=12)
    # R5  foco-recamara/on ~22:30 TODOS los días -> lectura antes de dormir
    for f in dias(INICIO_A, FIN_A):
        rafaga(ev, f, 22.5, "foco-recamara", "on", jitter_min=12)
    return ev


# =======================================================================
# 2) EJEMPLOS DIFÍCILES — buscan vulnerabilidades del modelo
# =======================================================================
def ejemplos_dificiles():
    ev = []
    # V1  CRUCE DE MEDIANOCHE: enchufe-sala/on ~00:00 local (23:5x y 00:0x)
    #     La media circular debe dar ~00:00, no ~12:00.
    horas_medianoche = [23.87, 23.95, 0.05, 0.13, 23.92, 0.02, 0.20, 23.98]
    for i, f in enumerate(dias(INICIO_A, FIN_A, "semana")):
        h = horas_medianoche[i % len(horas_medianoche)]
        rafaga(ev, f, h, "enchufe-sala", "on", jitter_min=3)
    # V2  DOS RUTINAS EN EL MISMO PAR: foco-cocina/off a las ~13:30 Y ~22:30
    #     DBSCAN debe devolver DOS rutinas, no promediarlas a ~18:00.
    for f in dias(INICIO_A, FIN_A, "semana"):
        rafaga(ev, f, 13.5, "foco-cocina", "off", jitter_min=15)
        rafaga(ev, f, 22.5, "foco-cocina", "off", jitter_min=15)
    # V3  RUTINA QUE SE DESPLAZA (drift): foco-patio/on de 19:45 -> 21:15
    for i, f in enumerate(dias(INICIO_A, FIN_A, "semana")):
        semana = i // 5
        h = 19.75 + semana * 0.5   # avanza ~30 min por semana
        rafaga(ev, f, h, "foco-patio", "on", jitter_min=8)
    # V4  BAJA CONFIANZA: foco-cocina/on ~15:00 diario pero conf 0.55-0.68
    #     Debe filtrarse: NO es rutina.
    for f in dias(INICIO_A, FIN_A):
        rafaga(ev, f, 15.0, "foco-cocina", "on", conf=(0.55, 0.68), jitter_min=10)
    # V5  ESCASO cerca del umbral: enchufe-sala/toggle ~17:00 solo 4 días
    #     -> confianza BAJA, NO confirmada.
    algunos = list(dias(INICIO_A, FIN_A, "semana"))[:4]
    for f in algunos:
        rafaga(ev, f, 17.0, "enchufe-sala", "toggle", jitter_min=10)
    return ev


# =======================================================================
# 3) EJEMPLOS BASURA — ruido; debe quedarse como "perdonado", nunca rutina
# =======================================================================
def ejemplos_basura():
    ev = []
    devs = ["foco-sala", "enchufe-sala", "foco-cocina", "foco-recamara"]
    acts = ["on", "off", "toggle"]
    # G1  eventos sueltos aleatorios, sin repetición de hora
    for f in dias(INICIO_A, FIN_A):
        for _ in range(rng.randint(0, 2)):
            rafaga(ev, f, rng.uniform(0, 24), rng.choice(devs),
                   rng.choice(acts), n=rng.randint(1, 3))
    # G2  MEGA-RÁFAGA en UN solo día (día de pruebas): 250 eventos a las 03:00
    #     El debounce debe colapsarla a 1 interacción y NO crear rutina 03:00.
    rafaga(ev, INICIO_A + timedelta(days=11), 3.0, "foco-sala", "off", n=250)
    # G3  "doble-tap" caótico: toggles rápidos a horas aleatorias, pocos días
    for f in list(dias(INICIO_A, FIN_A))[::6]:
        h = rng.uniform(0, 24)
        for _ in range(rng.randint(4, 8)):
            rafaga(ev, f, h, "enchufe-sala", rng.choice(acts),
                   n=rng.randint(1, 2), jitter_min=2)

    # -- Defectos para que la fase de PREPROCESAMIENTO tenga qué limpiar -----
    # Sin estos, el embudo no muestra nada y no se puede demostrar la limpieza.

    # G4  CONFIANZA BASURA (0.20-0.60): el reconocedor "vio" algo pero apenas
    #     le creyó. `conf_min`=0.70 los tira ANTES de agrupar -> son el primer
    #     escalón visible del embudo.
    for f in dias(INICIO_A, FIN_A):
        if rng.random() < 0.5:
            rafaga(ev, f, rng.uniform(0, 24), rng.choice(devs),
                   rng.choice(acts), n=rng.randint(2, 5), conf=(0.20, 0.60))

    # G5  DUPLICADOS EXACTOS: el MISMO evento con el MISMO ts, repetido.
    #     Ocurre cuando el cliente reintenta un envío que en realidad sí llegó.
    #     No son acciones humanas distintas: el modelo debe contarlos una vez.
    for f in list(dias(INICIO_A, FIN_A))[::7]:
        ts_repetido = ts_utc(f, 20.0).isoformat()
        for _ in range(rng.randint(3, 6)):
            ev.append({
                "userId": USER, "gesture": GESTO["on"], "confidence": 0.91,
                "deviceId": "foco-sala", "action": "on",
                "ts": ts_repetido,        # idéntico a propósito, sin avanzar
            })

    # G6  DISPOSITIVO DESCONOCIDO: un deviceId que no está dado de alta.
    #     No se filtra a propósito: aparece como su propio stream y nunca
    #     alcanza cobertura, que es justo lo que debe verse (ruido
    #     identificable, no una rutina).
    for f in list(dias(INICIO_A, FIN_A))[::9]:
        rafaga(ev, f, rng.uniform(0, 24), "dispositivo-fantasma",
               rng.choice(acts), n=rng.randint(1, 3))

    # G7  TIMESTAMPS ROTOS: texto que no es fecha, fecha imposible y vacíos.
    #     El modelo los cuenta en meta.descartes["ts_invalido"] y continúa;
    #     antes reventaban datetime.fromisoformat() y tumbaban el análisis.
    for ts_roto in ("no-es-una-fecha", "2026-13-45T99:99:99+00:00",
                    "", "0000-00-00"):
        ev.append({
            "userId": USER, "gesture": GESTO["off"], "confidence": 0.95,
            "deviceId": "foco-cocina", "action": "off", "ts": ts_roto,
        })
    return ev


def escribir(nombre, eventos):
    eventos.sort(key=lambda e: e["ts"])
    ruta = f"datos/{nombre}"
    with open(ruta, "w", encoding="utf-8") as fh:
        json.dump(eventos, fh, ensure_ascii=False, indent=2)
    print(f"  {ruta:36s} -> {len(eventos):5d} eventos")


if __name__ == "__main__":
    print("Generando datos de prueba (semilla fija = reproducible)...")
    cal = configurar_calendario()
    escribir("ejemplos_claros.json", ejemplos_claros())
    escribir("ejemplos_dificiles.json", ejemplos_dificiles())
    escribir("ejemplos_basura.json", ejemplos_basura())
    print(f"Listo. Hueco de ausencia: {cal['hueco'][0]} a {cal['hueco'][1]} "
          f"({DIAS_HUECO} días).")
    print("Para sembrar en Atlas en vez de en archivos: datos/sembrar_atlas.py")
