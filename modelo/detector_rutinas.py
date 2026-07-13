"""
Detector de rutinas del hogar SEENGO — v3
=========================================
Modelo PURO (sin dependencias externas: solo librería estándar de Python).
Pensado para correr en una Raspberry Pi de forma periódica sobre una ventana
rodante de eventos.

Tubería:
  1. Normalizar   -> filtra por confianza, convierte a hora LOCAL (tz correcta).
  2. Debounce     -> colapsa ráfagas (mismo device+action en segundos) a 1 acción.
  3. DBSCAN circular (propio, sin sklearn) -> agrupa por hora del día.
  4. Confirmación por COBERTURA (% de días activos), con piso mínimo de días.
  5. Ausencia larga -> mayor racha de días consecutivos SIN ningún evento.

El modelo NO sabe nada de MongoDB ni de la pantalla. Recibe una lista de
eventos (dicts) y devuelve un resultado serializable a JSON.

Esquema de evento de entrada (igual al de tu colección):
    {"deviceId": str, "action": "on|off|toggle",
     "confidence": float, "ts": "<ISO 8601, normalmente UTC>"}
"""
from __future__ import annotations
import math
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from collections import defaultdict

DIAS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]

CONFIG = {
    "tz": "America/Mexico_City",  # zona local del hogar (UTC-6, sin verano)
    "conf_min": 0.70,             # descarta reconocimientos dudosos
    "debounce_seg": 90,           # eventos del mismo par en <90s = 1 acción
    "eps_horas": 1.0,             # radio del cluster en el reloj (±1 h)
    "min_muestras": 3,            # densidad mínima para formar cluster
    "min_dias_absoluto": 7,       # piso: nunca declarar rutina con <7 días
    "min_cobertura": 0.60,        # % de días activos del tipo para confirmar
    "ausencia_aviso": 15,         # días sin actividad -> aviso
    "ausencia_alerta": 25,        # días sin actividad -> alerta
}

# ----------------------------------------------------------------------
# Utilidades de tiempo circular (reloj de 24 h)
# ----------------------------------------------------------------------
def hhmm(x: float) -> str:
    x %= 24
    h = int(x); m = int(round((x - h) * 60))
    if m == 60: h, m = (h + 1) % 24, 0
    return f"{h:02d}:{m:02d}"


def _media_circular(horas):
    sx = sum(math.sin(2 * math.pi * h / 24) for h in horas)
    cx = sum(math.cos(2 * math.pi * h / 24) for h in horas)
    ang = math.atan2(sx, cx)
    if ang < 0: ang += 2 * math.pi
    return ang / (2 * math.pi) * 24


def _desv_circular(horas):
    n = len(horas)
    sx = sum(math.sin(2 * math.pi * h / 24) for h in horas) / n
    cx = sum(math.cos(2 * math.pi * h / 24) for h in horas) / n
    R = min(max(math.hypot(sx, cx), 1e-9), 1.0)
    return math.sqrt(-2 * math.log(R)) / (2 * math.pi) * 24


def _dist_circular(a, b):
    d = abs(a - b) % 24
    return min(d, 24 - d)


# ----------------------------------------------------------------------
# DBSCAN circular propio (1-D, sin dependencias)
# ----------------------------------------------------------------------
def dbscan_circular(horas, eps, min_muestras):
    """Devuelve etiquetas de cluster (-1 = ruido). O(n^2), n pequeño tras
    el debounce, así que es de sobra rápido y evita cargar scikit-learn."""
    n = len(horas)
    if n == 0:
        return []
    vec = [[] for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if _dist_circular(horas[i], horas[j]) <= eps:
                vec[i].append(j); vec[j].append(i)

    etq = [None] * n  # None = sin visitar
    cid = -1
    for i in range(n):
        if etq[i] is not None:
            continue
        if len(vec[i]) + 1 < min_muestras:
            etq[i] = -1  # ruido provisional (puede reclamarse como borde)
            continue
        cid += 1
        etq[i] = cid
        cola = list(vec[i]); k = 0
        while k < len(cola):
            j = cola[k]; k += 1
            if etq[j] == -1:
                etq[j] = cid            # punto de borde
            if etq[j] is not None:
                continue
            etq[j] = cid
            if len(vec[j]) + 1 >= min_muestras:  # también es núcleo -> expande
                cola.extend(vec[j])
    return [e if e is not None else -1 for e in etq]


# ----------------------------------------------------------------------
# 1) Normalización  2) Debounce
# ----------------------------------------------------------------------
def normalizar(eventos, cfg):
    tz = ZoneInfo(cfg["tz"]); utc = ZoneInfo("UTC")
    out, filtrados = [], 0
    for e in eventos:
        if float(e.get("confidence", 1.0)) < cfg["conf_min"]:
            filtrados += 1
            continue
        ts = e["ts"]
        dt = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=utc)
        loc = dt.astimezone(tz)
        out.append({
            "dev": e["deviceId"], "act": e["action"], "dt": loc,
            "fecha": loc.date(),
            "hora": loc.hour + loc.minute / 60 + loc.second / 3600,
        })
    out.sort(key=lambda r: (r["dev"], r["act"], r["dt"]))
    return out, filtrados


def debounce(norm, cfg):
    """Colapsa ráfagas del mismo (device, action) separadas por <debounce_seg
    a una sola interacción, representada por el PRIMER evento."""
    seg = cfg["debounce_seg"]
    inter, last_key, last_dt = [], None, None
    for r in norm:
        key = (r["dev"], r["act"])
        if key != last_key or (r["dt"] - last_dt).total_seconds() > seg:
            inter.append({"dev": r["dev"], "act": r["act"], "dt": r["dt"],
                          "fecha": r["fecha"], "hora": r["hora"], "n": 1})
            last_key, last_dt = key, r["dt"]
        else:
            inter[-1]["n"] += 1
            last_dt = r["dt"]  # extiende la ventana mientras siga la ráfaga
    return inter


# ----------------------------------------------------------------------
# 3+4) Detección y confirmación de rutinas por vista
# ----------------------------------------------------------------------
def _confianza(cob):
    return "ALTA" if cob >= 0.80 else "MEDIA" if cob >= 0.60 else "BAJA"


def _rutinas_de(interacciones, posibles, cfg, min_muestras=None):
    mm = min_muestras or cfg["min_muestras"]
    horas = [it["hora"] for it in interacciones]
    etq = dbscan_circular(horas, cfg["eps_horas"], mm)
    grupos = defaultdict(list)
    for it, e in zip(interacciones, etq):
        grupos[e].append(it)

    rutinas = []
    for cl, g in grupos.items():
        if cl == -1:
            continue
        hs = [x["hora"] for x in g]
        dias_fechas = {x["fecha"] for x in g}
        dias_sem = sorted({x["dt"].weekday() for x in g})
        cob = (len(dias_fechas) / posibles) if posibles else 0.0
        rutinas.append({
            "hora": round(_media_circular(hs), 4),
            "hora_hhmm": hhmm(_media_circular(hs)),
            "tolerancia_h": round(max(_desv_circular(hs), 5 / 60), 4),
            "tolerancia_hhmm": hhmm(max(_desv_circular(hs), 5 / 60)),
            "n_interacciones": len(g),
            "dias_cubiertos": len(dias_fechas),
            "posibles": posibles,
            "cobertura": round(cob, 3),
            "confianza": _confianza(cob),
            "confirmada": len(dias_fechas) >= cfg["min_dias_absoluto"]
                          and cob >= cfg["min_cobertura"],
            "dias_semana": [DIAS[d] for d in dias_sem],
        })
    ruido = sorted(hhmm(h) for h, e in zip(horas, etq) if e == -1)
    return {"posibles": posibles, "rutinas": sorted(rutinas, key=lambda r: r["hora"]),
            "ruido": ruido}


VISTAS = {
    "entre_semana": lambda wd: wd <= 4,
    "fin_de_semana": lambda wd: wd >= 5,
    "semana_completa": lambda wd: True,
}


def analizar_stream(inter, fechas_activas, cfg):
    """Un stream = un par (device, action). Corre las 3 vistas agregadas."""
    vistas = {}
    for nombre, filtro in VISTAS.items():
        sub = [it for it in inter if filtro(it["dt"].weekday())]
        posibles = sum(1 for f in fechas_activas if filtro(f.weekday()))
        vistas[nombre] = _rutinas_de(sub, posibles, cfg)
    return vistas


def mapa_semanal(inter):
    """Cuenta interacciones por (día de la semana, hora entera): grilla 7x24
    para la vista de mapa de calor. No depende de las vistas (usa todos los
    días); cada celda es [Lun..Dom] x [00..23]."""
    grid = [[0] * 24 for _ in range(7)]
    for it in inter:
        grid[it["dt"].weekday()][int(it["hora"])] += 1
    return grid


# ----------------------------------------------------------------------
# 5) Ausencia larga (mayor racha de días sin NINGÚN evento)
# ----------------------------------------------------------------------
def ausencia_larga(fechas_activas, cfg, ahora=None):
    fa = sorted(fechas_activas)
    peor, ventana = 0, None
    for a, b in zip(fa, fa[1:]):
        vacios = (b - a).days - 1
        if vacios > peor:
            peor, ventana = vacios, (a + timedelta(days=1), b - timedelta(days=1))
    # racha abierta hasta "ahora" (casa que podría seguir vacía)
    if ahora is not None:
        ref = ahora.date() if isinstance(ahora, datetime) else ahora
        vacios = (ref - fa[-1]).days
        if vacios > peor:
            peor, ventana = vacios, (fa[-1] + timedelta(days=1), ref)

    nivel = ("alerta" if peor >= cfg["ausencia_alerta"]
             else "aviso" if peor >= cfg["ausencia_aviso"] else "ninguno")
    if nivel == "alerta":
        msg = (f"Sin actividad durante {peor} días seguidos. La casa parece "
               f"deshabitada: conviene activar simulación de presencia o avisar.")
    elif nivel == "aviso":
        msg = (f"Sin actividad durante {peor} días seguidos. Posible ausencia "
               f"prolongada; vigilar antes de escalar a alerta.")
    else:
        msg = "Sin ausencias prolongadas relevantes."
    return {
        "hueco_maximo_dias": peor,
        "nivel": nivel,
        "ventana": ({"desde": ventana[0].isoformat(), "hasta": ventana[1].isoformat()}
                    if ventana else None),
        "mensaje": msg,
    }


# ----------------------------------------------------------------------
# Orquestador
# ----------------------------------------------------------------------
def analizar(eventos, cfg=None, ahora=None):
    cfg = {**CONFIG, **(cfg or {})}
    norm, filtrados = normalizar(eventos, cfg)
    inter = debounce(norm, cfg)

    fechas_activas = {r["fecha"] for r in norm}
    por_par = defaultdict(list)
    for it in inter:
        por_par[(it["dev"], it["act"])].append(it)

    streams = []
    for (dev, act), lst in sorted(por_par.items()):
        streams.append({
            "device": dev, "action": act,
            "interacciones": len(lst),
            "mapa_semanal": mapa_semanal(lst),
            "vistas": analizar_stream(lst, fechas_activas, cfg),
        })

    fa = sorted(fechas_activas)
    return {
        "meta": {
            "tz": cfg["tz"],
            "eventos_crudos": len(eventos),
            "eventos_filtrados_confianza": filtrados,
            "interacciones": len(inter),
            "rango": {
                "desde": fa[0].isoformat() if fa else None,
                "hasta": fa[-1].isoformat() if fa else None,
                "dias_activos": len(fa),
            },
            "config": cfg,
        },
        "ausencia_larga": ausencia_larga(fechas_activas, cfg, ahora) if fa else None,
        "streams": streams,
    }
