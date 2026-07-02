"""
Detector de rutinas del hogar — versión mejorada (v2)
======================================================
Objetivo: detectar rutinas de encendido a lo largo de TODA la semana
(por día y agregadas) y ventanas de ausencia, de forma robusta y eficiente,
sin entrenamiento (solo DBSCAN + geometría circular).

Qué cambia respecto a tu versión original y POR QUÉ:

1. MEDIA CIRCULAR para reportar la hora de cada rutina.
   Tu versión usaba rutinas['hora_decimal'].mean(). Eso falla si una rutina
   cruza la medianoche: el promedio de 23:30 y 00:30 daría 12:00. La media
   circular (atan2 de sen/cos) da 00:00, que es lo correcto.

2. SE REPORTA CADA CLUSTER POR SEPARADO.
   Tu analizar_rutinas promediaba TODOS los puntos no-ruido juntos. Si hay
   dos rutinas (p. ej. una de mañana y una de noche), las mezclaba en una
   hora intermedia sin sentido. Ahora DBSCAN puede devolver varias rutinas
   y cada una se reporta con su hora y tolerancia.

3. LA "REGLA DE N DÍAS" SE SEPARA DE DBSCAN.
   Usar min_samples=9 y =4 mete tu regla de negocio (frecuencia) dentro de un
   parámetro de DENSIDAD. Además queda pegada a "3 semanas exactas": con más o
   menos datos, el 9 deja de tener sentido. Solución: DBSCAN solo agrupa (con
   un min_samples chico), y la confirmación se hace por COBERTURA = días
   distintos vistos / días posibles. Así el umbral es un %, independiente de
   cuántas semanas tengas.

4. COBERTURA DE TODA LA SEMANA.
   Se analiza por día (Lun..Dom) y en vistas agregadas (entre semana, fin de
   semana y semana completa). La vista "semana completa" además muestra por qué
   conviene separar entre semana / fin de semana (los horarios se funden).

5. AUSENCIA REDISEÑADA.
   Tu Capa 4 aplastaba todos los eventos en un solo reloj de 24 h y buscaba la
   brecha más grande: eso dependía de eventos sueltos (una anomalía tapaba o
   inventaba el hueco). Ahora se mide OCUPACIÓN por franja horaria a lo largo de
   los días: una franja "vacía" es la que casi ningún día tiene actividad. Se
   reportan las ventanas vacías contiguas (con manejo de medianoche).

Datos reales (MongoDB): basta mapear cada documento a
    {"fecha": <date>, "hora_decimal": <hora+min/60>}
y todo lo demás funciona igual. Para rutinas conviene filtrar por el evento
de interés (encender luz); para ausencia conviene usar TODA la actividad de
sensores, no solo un evento.
"""

import numpy as np
import pandas as pd
from sklearn.cluster import DBSCAN

DIAS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]

# eps ≈ 1 hora en el círculo unitario.
# Distancia de cuerda entre dos horas separadas Δt: 2*sin(Δθ/2) con Δθ=2π·Δt/24.
# Para Δt = 1 h -> ≈ 0.2611  (tu 0.26 estaba correcto).
EPS_1H = 2 * np.sin((2 * np.pi / 24) / 2)


# ----------------------------------------------------------------------
# Utilidades de tiempo (reloj de 24 h circular)
# ----------------------------------------------------------------------
def hora_a_decimal(hhmm: str) -> float:
    h, m = map(int, hhmm.split(":"))
    return h + m / 60.0


def decimal_a_hhmm(x: float) -> str:
    x = x % 24
    h = int(x)
    m = int(round((x - h) * 60))
    if m == 60:
        h, m = (h + 1) % 24, 0
    return f"{h:02d}:{m:02d}"


def _ciclico(horas: np.ndarray):
    ang = 2 * np.pi * np.asarray(horas, dtype=float) / 24
    return np.sin(ang), np.cos(ang)


def media_circular(horas) -> float:
    """Hora promedio correcta en un reloj de 24 h (maneja la medianoche)."""
    s, c = _ciclico(horas)
    ang = np.arctan2(s.mean(), c.mean())
    if ang < 0:
        ang += 2 * np.pi
    return ang / (2 * np.pi) * 24


def desv_circular_horas(horas) -> float:
    """Dispersión de un grupo de horas (en horas), en escala circular."""
    s, c = _ciclico(horas)
    R = np.sqrt(s.mean() ** 2 + c.mean() ** 2)
    R = min(max(R, 1e-9), 1.0)
    return np.sqrt(-2 * np.log(R)) / (2 * np.pi) * 24


def confianza(cobertura: float) -> str:
    if cobertura >= 0.80:
        return "ALTA"
    if cobertura >= 0.60:
        return "MEDIA"
    return "BAJA"


# ----------------------------------------------------------------------
# Núcleo: detección de rutinas (agrupamiento por densidad)
# ----------------------------------------------------------------------
def detectar_rutinas(sub: pd.DataFrame, eps: float = EPS_1H, min_muestras: int = 3):
    """
    Agrupa las horas de 'sub' con DBSCAN sobre coordenadas circulares.
    Devuelve (lista_de_rutinas, horas_de_ruido).

    Cada rutina es un dict con:
      hora            -> hora central (media circular)
      tolerancia_h    -> ancho típico de la rutina (dispersión circular)
      n_eventos       -> nº de eventos en el cluster
      dias_cubiertos  -> nº de FECHAS distintas (para la regla de cobertura)
      dias_semana     -> qué días de la semana aparecen
    """
    if len(sub) < max(min_muestras, 1):
        return [], list(sub["hora_decimal"].values)

    s, c = _ciclico(sub["hora_decimal"].values)
    etiquetas = DBSCAN(eps=eps, min_samples=min_muestras).fit_predict(
        np.column_stack([s, c])
    )
    sub = sub.assign(_cluster=etiquetas)

    rutinas = []
    for cl in sorted(set(etiquetas) - {-1}):
        g = sub[sub["_cluster"] == cl]
        rutinas.append(
            {
                "hora": media_circular(g["hora_decimal"].values),
                "tolerancia_h": max(desv_circular_horas(g["hora_decimal"].values), 5 / 60),
                "n_eventos": int(len(g)),
                "dias_cubiertos": int(g["fecha"].nunique()),
                "dias_semana": sorted(g["dia_semana"].unique().tolist()),
            }
        )
    ruido = sorted(sub.loc[sub["_cluster"] == -1, "hora_decimal"].values)
    return sorted(rutinas, key=lambda r: r["hora"]), ruido


def analizar_vista(df, todas_fechas, nombre, filtro_dia,
                   min_dias=None, min_cobertura=0.60, min_muestras=3):
    """
    Ejecuta la detección sobre el subconjunto de días que cumplen 'filtro_dia'
    (función de dia_de_semana -> bool) y confirma cada rutina por cobertura.
    Confirmación: dias_cubiertos >= min_dias  (si se da una regla absoluta)
                  o  cobertura >= min_cobertura.
    """
    mask = df["dia_semana"].map(filtro_dia)
    sub = df[mask].copy()
    posibles = int(sum(filtro_dia(d) for d in todas_fechas.dayofweek))
    rutinas, ruido = detectar_rutinas(sub, min_muestras=min_muestras)

    for r in rutinas:
        r["posibles"] = posibles
        r["cobertura"] = r["dias_cubiertos"] / posibles if posibles else 0.0
        regla_abs = (min_dias is not None) and (r["dias_cubiertos"] >= min_dias)
        r["confirmada"] = regla_abs or (r["cobertura"] >= min_cobertura)
    return {"nombre": nombre, "posibles": posibles, "rutinas": rutinas, "ruido": ruido}


# ----------------------------------------------------------------------
# Ausencia: ocupación por franja horaria a lo largo de los días
# ----------------------------------------------------------------------
def detectar_ausencia(df_actividad, todas_fechas, slot_min=30,
                      umbral_ocupacion=0.15, min_horas=4.0):
    """
    Divide el día en franjas de 'slot_min' minutos. Para cada franja cuenta en
    cuántos días DISTINTOS hubo actividad. Una franja se considera "vacía" si su
    ocupación <= umbral_ocupacion. Devuelve las ventanas vacías contiguas de al
    menos 'min_horas' (con manejo de medianoche).
    """
    n = int(24 * 60 / slot_min)
    total_dias = max(len(todas_fechas), 1)

    dias_con_actividad = [set() for _ in range(n)]
    for _, r in df_actividad.iterrows():
        s = int((r["hora_decimal"] % 24) * 60 / slot_min)
        dias_con_actividad[s].add(pd.Timestamp(r["fecha"]).normalize())

    ocupacion = np.array([len(x) for x in dias_con_actividad]) / total_dias
    vacio = ocupacion <= umbral_ocupacion

    if vacio.all():
        return [(0.0, 24.0, 24.0)]
    if not vacio.any():
        return []

    # rotamos para empezar en una franja NO vacía y así juntar rachas que cruzan
    # la medianoche
    inicio = int(np.argmin(vacio))
    rot = np.roll(vacio, -inicio)

    ventanas, i = [], 0
    while i < n:
        if rot[i]:
            j = i
            while j < n and rot[j]:
                j += 1
            ini_h = ((inicio + i) % n) * slot_min / 60
            dur_h = (j - i) * slot_min / 60
            if dur_h >= min_horas:
                ventanas.append((ini_h, (ini_h + dur_h) % 24, dur_h))
            i = j
        else:
            i += 1
    return sorted(ventanas, key=lambda v: -v[2])  # más larga primero


# ======================================================================
# DATOS DE DEMO
# ======================================================================
def construir_datos():
    # --- (A) Encendido de luces: TUS 21 eventos originales, en orden ---
    eventos_luz = ["19:25", "19:40", "19:15", "14:00", "19:50", "21:10", "20:55",
                   "19:35", "19:20", "19:30", "19:45", "20:00", "21:30", "03:00",
                   "19:20", "19:10", "19:25", "19:35", "08:15", "21:05", "21:15"]

    inicio = pd.Timestamp("2025-06-02")
    inicio = inicio - pd.Timedelta(days=int(inicio.dayofweek))  # forzar lunes
    fechas = pd.date_range(inicio, periods=len(eventos_luz))

    df = pd.DataFrame({"fecha": fechas, "hora_str": eventos_luz})
    df["dia_semana"] = df["fecha"].dt.dayofweek           # derivado de la fecha
    df["hora_decimal"] = df["hora_str"].map(hora_a_decimal)

    # --- (B) Actividad general de sensores (para AUSENCIA) ---
    # Simulación realista: actividad de mañana + noche, mediodía vacío (trabajo)
    # y madrugada vacía. En producción esto es TODA la actividad de la casa.
    rng = np.random.default_rng(7)
    filas = []
    for f in fechas:
        entre_semana = f.dayofweek <= 4
        # mañana
        filas.append((f, float(np.clip(rng.normal(7.0 if entre_semana else 9.0, 0.4), 5, 11))))
        if rng.random() < 0.5:
            filas.append((f, float(np.clip(rng.normal(7.6 if entre_semana else 9.6, 0.4), 5, 11))))
        # noche (2-3 eventos)
        base = 19.5 if entre_semana else 21.0
        for _ in range(int(rng.integers(2, 4))):
            filas.append((f, float(np.clip(rng.normal(base, 1.0), 17, 23.9))))
    df_act = pd.DataFrame(filas, columns=["fecha", "hora_decimal"])
    return df, df_act, fechas


# ======================================================================
# REPORTE
# ======================================================================
def imprimir_vista(v):
    print(f"\n  {v['nombre']}  — {v['posibles']} días posibles")
    if not v["rutinas"]:
        print("    ·  Sin rutina detectada.")
    for r in v["rutinas"]:
        estado = "✅" if r["confirmada"] else "🟡"
        marca = "" if r["confirmada"] else "  (aún no confirmada)"
        print(f"    {estado} {decimal_a_hhmm(r['hora'])} "
              f"(±{decimal_a_hhmm(r['tolerancia_h'])[1:]})"
              f"  · {r['dias_cubiertos']}/{r['posibles']} días"
              f"  · confianza {confianza(r['cobertura'])}{marca}")
    if v["ruido"]:
        horas = ", ".join(decimal_a_hhmm(h) for h in v["ruido"])
        print(f"       Eventos fuera de rutina (perdonados): {len(v['ruido'])} → {horas}")


def main():
    df, df_act, fechas = construir_datos()

    print("=" * 64)
    print(" DETECTOR DE RUTINAS DEL HOGAR (v2)")
    print(f" {len(fechas)} días  |  {fechas.min().date()} a {fechas.max().date()}")
    print("=" * 64)

    # ---- Vistas agregadas (encendido de luces) ----
    print("\n▶ RUTINAS DE ENCENDIDO — VISTAS AGREGADAS")
    imprimir_vista(analizar_vista(df, fechas, "Entre semana (Lun-Vie)",
                                  lambda d: d <= 4, min_dias=9, min_muestras=3))
    imprimir_vista(analizar_vista(df, fechas, "Fin de semana (Sáb-Dom)",
                                  lambda d: d >= 5, min_dias=4, min_muestras=3))
    imprimir_vista(analizar_vista(df, fechas, "Semana completa (Lun-Dom)",
                                  lambda d: True, min_cobertura=0.60, min_muestras=3))

    # ---- Vista por día de la semana (toda la semana) ----
    print("\n▶ RUTINAS POR DÍA DE LA SEMANA")
    for i, nombre in enumerate(DIAS):
        v = analizar_vista(df, fechas, nombre, (lambda x: (lambda d: d == x))(i),
                           min_cobertura=0.60, min_muestras=2)  # pocos datos por día
        imprimir_vista(v)

    # ---- Ausencia ----
    print("\n▶ VENTANAS DE AUSENCIA (a partir de TODA la actividad)")
    ventanas = detectar_ausencia(df_act, fechas, slot_min=30,
                                 umbral_ocupacion=0.15, min_horas=4.0)
    if not ventanas:
        print("    ·  No hay una ventana clara de inactividad recurrente.")
    for ini, fin, dur in ventanas:
        etiqueta = "MODO FUERA DE CASA" if 8 <= ini <= 12 else "reposo"
        print(f"    ⚠️  {decimal_a_hhmm(ini)}–{decimal_a_hhmm(fin)}  "
              f"({dur:.1f} h sin actividad)   [{etiqueta}]")
        if etiqueta == "MODO FUERA DE CASA":
            print(f"        📲 Sugerencia: '¿Aseguramos apagado de luces y standby "
                  f"de TV entre {decimal_a_hhmm(ini)} y {decimal_a_hhmm(fin)} "
                  f"para ahorrar energía?'")


if __name__ == "__main__":
    main()
