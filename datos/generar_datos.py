"""
Generador de datos de prueba para el detector de rutinas SEENGO.
================================================================
Produce 3 archivos JSON listos para importar a MongoDB Atlas, sobre un
mismo calendario de ~60 días. Todos comparten un HUECO GLOBAL de 25 días
sin ningún evento (para probar el detector de ausencia larga).

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

# --- Calendario maestro --------------------------------------------------
INICIO_A = datetime(2026, 5, 1)     # rutinas corren aquí (4 semanas)
FIN_A    = datetime(2026, 5, 28)
# HUECO GLOBAL: 2026-05-29 .. 2026-06-22  (25 días sin NINGÚN evento)
INICIO_B = datetime(2026, 6, 23)    # se reanuda la actividad
FIN_B    = datetime(2026, 6, 30)


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
    rafaga(ev, datetime(2026, 5, 12), 3.0, "foco-sala", "off", n=250)
    # G3  "doble-tap" caótico: toggles rápidos a horas aleatorias, pocos días
    for f in list(dias(INICIO_A, FIN_A))[::6]:
        h = rng.uniform(0, 24)
        for _ in range(rng.randint(4, 8)):
            rafaga(ev, f, h, "enchufe-sala", rng.choice(acts),
                   n=rng.randint(1, 2), jitter_min=2)
    return ev


def escribir(nombre, eventos):
    eventos.sort(key=lambda e: e["ts"])
    ruta = f"datos/{nombre}"
    with open(ruta, "w", encoding="utf-8") as fh:
        json.dump(eventos, fh, ensure_ascii=False, indent=2)
    print(f"  {ruta:36s} -> {len(eventos):5d} eventos")


if __name__ == "__main__":
    print("Generando datos de prueba (semilla fija = reproducible)...")
    escribir("ejemplos_claros.json", ejemplos_claros())
    escribir("ejemplos_dificiles.json", ejemplos_dificiles())
    escribir("ejemplos_basura.json", ejemplos_basura())
    print("Listo. Hueco global de ausencia: 2026-05-29 a 2026-06-22 (25 días).")
