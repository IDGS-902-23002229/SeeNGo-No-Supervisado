"""
Sugerencias SEENGO y resumen de aceptación
==========================================
Módulo PURO (solo librería estándar, sin Mongo ni I/O), igual que el detector.

Dos responsabilidades:

1. `generar_sugerencias(resultado)` — convierte las rutinas que detectó
   `detector_rutinas.analizar()` en sugerencias accionables para el usuario
   ("Sueles encender foco-sala ~19:30 entre semana, ¿lo automatizamos?").
   Cada sugerencia lleva una `clave` estable (device|action|hora redondeada)
   para que el consumidor pueda publicarlas en Mongo SIN duplicarlas entre
   corridas.

2. `resumen_aceptacion(sugerencias)` — dado el listado de sugerencias ya
   respondidas (o no) por el usuario, arma el resumen para el tablero:
   `aceptada` es 1 (sí), 0 (no) o None (pendiente).

La conexión a Mongo (leer respuestas, publicar nuevas) vive en `consumidor/`,
nunca aquí.
"""
from __future__ import annotations

VERBO = {"on": "encender", "off": "apagar", "toggle": "alternar"}

CONTEXTO = {
    "entre_semana": "entre semana",
    "fin_de_semana": "el fin de semana",
    "todos": "todos los días",
}


def _hhmm_redondeada(hora: float) -> str:
    """Redondea la hora decimal a la media hora más cercana (clave estable:
    el drift pequeño entre corridas no debe crear sugerencias duplicadas)."""
    x = (round(hora * 2) / 2) % 24
    h = int(x)
    m = int(round((x - h) * 60))
    return f"{h:02d}:{m:02d}"


def generar_sugerencias(resultado, min_interacciones=3, min_dias=1):
    """De un resultado de `analizar()` -> lista de sugerencias.

    Se toman los clusters de las vistas entre_semana y fin_de_semana (la
    semana_completa duplicaría). Si el mismo (device, action, hora) aparece
    en ambas vistas se emite UNA sugerencia con contexto "todos los días".
    También se sugiere simulación de presencia si hay aviso/alerta de ausencia.
    """
    candidatos = {}  # (dev, act, hhmm) -> {vistas:set, mejor:rutina}
    for s in resultado.get("streams", []):
        for vista in ("entre_semana", "fin_de_semana"):
            v = s.get("vistas", {}).get(vista)
            if not v:
                continue
            for r in v.get("rutinas", []):
                if (r["n_interacciones"] < min_interacciones
                        or r["dias_cubiertos"] < min_dias):
                    continue
                k = (s["device"], s["action"], _hhmm_redondeada(r["hora"]))
                c = candidatos.setdefault(k, {"vistas": set(), "mejor": r})
                c["vistas"].add(vista)
                if r["cobertura"] > c["mejor"]["cobertura"]:
                    c["mejor"] = r

    sugerencias = []
    for (dev, act, hhmm), c in sorted(candidatos.items()):
        r = c["mejor"]
        ctx = ("todos" if len(c["vistas"]) == 2 else next(iter(c["vistas"])))
        ctx_txt = CONTEXTO.get(ctx, ctx)
        verbo = VERBO.get(act, act)
        sugerencias.append({
            "tipo": "rutina",
            "deviceId": dev,
            "action": act,
            "hora_hhmm": hhmm,
            "contexto": ctx_txt,
            "nivel": r["confianza"],           # ALTA / MEDIA / BAJA
            "confirmada": r["confirmada"],
            "mensaje": (f"Sueles {verbo} {dev} como a las {r['hora_hhmm']} "
                        f"{ctx_txt}. ¿Quieres que SEENGO lo haga por ti?"),
            "clave": f"rutina|{dev}|{act}|{hhmm}",
        })

    aus = resultado.get("ausencia_larga") or {}
    if aus.get("nivel") in ("aviso", "alerta"):
        sugerencias.append({
            "tipo": "ausencia",
            "deviceId": None,
            "action": None,
            "hora_hhmm": None,
            "contexto": f"hueco de {aus['hueco_maximo_dias']} días",
            "nivel": aus["nivel"].upper(),
            "confirmada": aus["nivel"] == "alerta",
            "mensaje": ("Detectamos una ausencia prolongada "
                        f"({aus['hueco_maximo_dias']} días sin actividad). "
                        "¿Activamos simulación de presencia cuando no estés?"),
            "clave": "ausencia|simulacion_presencia",
        })
    return sugerencias


def resumen_aceptacion(sugerencias):
    """Listado (con campo `aceptada` 1/0/None) -> resumen para el tablero."""
    lista = []
    acept = rech = pend = 0
    for s in sugerencias:
        a = s.get("aceptada")
        if a == 1:
            acept += 1
        elif a == 0:
            rech += 1
        else:
            a = None
            pend += 1
        lista.append({
            "clave": s.get("clave"),
            "mensaje": s.get("mensaje"),
            "tipo": s.get("tipo"),
            "nivel": s.get("nivel"),
            "deviceId": s.get("deviceId"),
            "action": s.get("action"),
            "hora_hhmm": s.get("hora_hhmm"),
            "contexto": s.get("contexto"),
            "aceptada": a,
            "creada": s.get("creada"),
            "respondida": s.get("respondida"),
        })
    respondidas = acept + rech
    # pendientes primero, luego las más recientes
    lista.sort(key=lambda x: (x["aceptada"] is not None, x.get("respondida") or "",
                              x.get("creada") or ""), reverse=False)
    return {
        "total": len(lista),
        "aceptadas": acept,
        "rechazadas": rech,
        "pendientes": pend,
        "tasa_aceptacion": (round(acept / respondidas, 3) if respondidas else None),
        "lista": lista,
    }
