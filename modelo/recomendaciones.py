"""
Capa prescriptiva SEENGO — del patrón detectado a la acción sugerida
====================================================================
Módulo PURO (sólo librería estándar, sin Mongo ni I/O), igual que el detector.

Las fases previas describen (qué pasó) y predicen (qué patrón hay). Ésta es la
del USO DEL CONOCIMIENTO: convierte las rutinas ya detectadas en algo que el
usuario puede aceptar o rechazar. No hay ningún algoritmo nuevo aquí; son
reglas simples sobre la salida de `detector_rutinas.analizar()`.

Reglas implementadas
--------------------
  1. Rutina confirmada  -> candidata a automatizarse (prioridad por confianza).
  2. Encendido sin apagado -> posible consumo olvidado.
  3. Ausencia prolongada  -> simulación de presencia replicando las rutinas.

Cada recomendación lleva:
  tipo, mensaje, prioridad (alta|media|baja), evidencia (qué la sustenta) y
  una `clave` estable para poder guardarla en Mongo sin duplicarla entre
  corridas y asociarle la respuesta del usuario (1 = acepta, 0 = rechaza).

La conexión a Mongo (publicar, leer respuestas) vive en `consumidor/`.
"""
from __future__ import annotations

VERBO = {"on": "encender", "off": "apagar", "toggle": "alternar"}

CONTEXTO = {
    "entre_semana": "entre semana",
    "fin_de_semana": "el fin de semana",
    "todos": "todos los días",
}

# Orden para poder ordenar la lista por urgencia real.
PESO_PRIORIDAD = {"alta": 0, "media": 1, "baja": 2}


def _hhmm_redondeada(hora: float) -> str:
    """Redondea la hora decimal a la media hora más cercana.

    Es lo que hace estable la `clave`: si el modelo mueve una rutina de 19:31
    a 19:33 entre corridas, no queremos una recomendación nueva y duplicada.
    """
    x = (round(hora * 2) / 2) % 24
    h = int(x)
    m = int(round((x - h) * 60))
    return f"{h:02d}:{m:02d}"


def _prioridad_rutina(r):
    """Una rutina confirmada y con confianza ALTA es la mejor candidata a
    automatizarse: hay evidencia sólida de que el usuario la repite."""
    if not r["confirmada"]:
        return "baja"
    return "alta" if r["confianza"] == "ALTA" else "media"


def _evidencia_rutina(device, action, vista, r):
    return {
        "device": device,
        "action": action,
        "vista": vista,
        "hora_hhmm": r["hora_hhmm"],
        "tolerancia_hhmm": r["tolerancia_hhmm"],
        "dias_cubiertos": r["dias_cubiertos"],
        "posibles": r["posibles"],
        "cobertura": r["cobertura"],
        "confianza": r["confianza"],
        "confirmada": r["confirmada"],
    }


def generar_recomendaciones(resultado, min_interacciones=3, min_dias=1):
    """De un resultado de `analizar()` -> lista de recomendaciones accionables.

    Se recorren las vistas entre_semana y fin_de_semana (semana_completa
    duplicaría lo mismo). Si el mismo (dispositivo, acción, hora) aparece en
    ambas, se emite UNA sola con contexto "todos los días".
    """
    recomendaciones = []

    # --- Regla 1: rutinas -> candidatas a automatizarse -------------------
    candidatos = {}
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
                c = candidatos.setdefault(
                    k, {"vistas": set(), "mejor": r, "vista_mejor": vista})
                c["vistas"].add(vista)
                if r["cobertura"] > c["mejor"]["cobertura"]:
                    c["mejor"], c["vista_mejor"] = r, vista

    for (dev, act, hhmm), c in sorted(candidatos.items()):
        r = c["mejor"]
        ctx = "todos" if len(c["vistas"]) == 2 else next(iter(c["vistas"]))
        ctx_txt = CONTEXTO.get(ctx, ctx)
        verbo = VERBO.get(act, act)
        if r["confirmada"]:
            msg = (f"Sueles {verbo} {dev} como a las {r['hora_hhmm']} "
                   f"{ctx_txt}. ¿Quieres que SEENGO lo haga por ti?")
        else:
            msg = (f"Puede que estés empezando a {verbo} {dev} sobre las "
                   f"{r['hora_hhmm']} {ctx_txt}, pero aún hay poca evidencia "
                   f"({r['dias_cubiertos']} de {r['posibles']} días). "
                   f"Seguimos observando.")
        recomendaciones.append({
            "tipo": "automatizar_rutina",
            "mensaje": msg,
            "prioridad": _prioridad_rutina(r),
            "evidencia": _evidencia_rutina(dev, act, c["vista_mejor"], r),
            "nivel": r["confianza"],
            "confirmada": r["confirmada"],
            "deviceId": dev,
            "action": act,
            "hora_hhmm": hhmm,
            "contexto": ctx_txt,
            "clave": f"rutina|{dev}|{act}|{hhmm}",
        })

    # --- Regla 2: encendido sin apagado -> posible consumo olvidado -------
    # Si un dispositivo tiene rutina CONFIRMADA de encender pero ninguna de
    # apagar, lo más probable es que se quede prendido hasta que alguien se
    # acuerda. Es la recomendación con más ahorro potencial.
    confirmadas_por_dispositivo = {}
    for s in resultado.get("streams", []):
        tiene = any(r["confirmada"]
                    for v in s.get("vistas", {}).values()
                    for r in v.get("rutinas", []))
        if tiene:
            confirmadas_por_dispositivo.setdefault(s["device"], set()).add(
                s["action"])

    for dev, acciones in sorted(confirmadas_por_dispositivo.items()):
        if "on" in acciones and "off" not in acciones and "toggle" not in acciones:
            ev = next((c for (d, a, _), c in candidatos.items()
                       if d == dev and a == "on"), None)
            recomendaciones.append({
                "tipo": "consumo_olvidado",
                "mensaje": (f"{dev} tiene una rutina clara de encendido, pero "
                            f"ninguna de apagado. Puede quedarse encendido sin "
                            f"que nadie lo note: ¿le ponemos un apagado "
                            f"automático?"),
                "prioridad": "alta",
                "evidencia": (_evidencia_rutina(dev, "on", ev["vista_mejor"],
                                                ev["mejor"]) if ev else
                              {"device": dev, "acciones_detectadas":
                               sorted(acciones)}),
                "nivel": "ALTA",
                "confirmada": True,
                "deviceId": dev,
                "action": "off",
                "hora_hhmm": None,
                "contexto": "sin apagado detectado",
                "clave": f"consumo|{dev}",
            })

    # --- Regla 3: ausencia prolongada -> simulación de presencia ----------
    aus = resultado.get("ausencia_larga") or {}
    if aus.get("nivel") in ("aviso", "alerta"):
        # La simulación es útil precisamente porque YA sabemos qué rutinas
        # replicar: las confirmadas son el guion de "casa habitada".
        base = [r["clave"] for r in recomendaciones
                if r["tipo"] == "automatizar_rutina" and r["confirmada"]]
        recomendaciones.append({
            "tipo": "simulacion_presencia",
            "mensaje": (f"Detectamos {aus['hueco_maximo_dias']} días seguidos "
                        f"sin actividad. Si vuelve a pasar, SEENGO puede "
                        f"simular presencia repitiendo tus rutinas habituales. "
                        f"¿Lo activamos?"),
            "prioridad": "alta" if aus["nivel"] == "alerta" else "media",
            "evidencia": {
                "hueco_maximo_dias": aus["hueco_maximo_dias"],
                "ventana": aus.get("ventana"),
                "rutinas_a_replicar": base,
            },
            "nivel": aus["nivel"].upper(),
            "confirmada": aus["nivel"] == "alerta",
            "deviceId": None,
            "action": None,
            "hora_hhmm": None,
            "contexto": f"hueco de {aus['hueco_maximo_dias']} días",
            "clave": "ausencia|simulacion_presencia",
        })

    recomendaciones.sort(key=lambda r: (PESO_PRIORIDAD.get(r["prioridad"], 9),
                                        r["clave"]))
    return recomendaciones


def resumen_aceptacion(recomendaciones):
    """Listado (con campo `aceptada` 1/0/None) -> resumen para el tablero.

    `aceptada` es la señal binaria con la que se mide si el conocimiento
    extraído le sirve de verdad al usuario: 1 acepta, 0 rechaza, None sin
    responder. La tasa de aceptación es el indicador de utilidad del modelo.
    """
    lista = []
    acept = rech = pend = 0
    for s in recomendaciones:
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
            "prioridad": s.get("prioridad"),
            "evidencia": s.get("evidencia"),
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
    # Pendientes primero (son sobre las que hay que actuar), luego por
    # prioridad y por fecha de respuesta.
    lista.sort(key=lambda x: (x["aceptada"] is not None,
                              PESO_PRIORIDAD.get(x.get("prioridad"), 9),
                              x.get("respondida") or "",
                              x.get("creada") or ""))
    return {
        "total": len(lista),
        "aceptadas": acept,
        "rechazadas": rech,
        "pendientes": pend,
        "tasa_aceptacion": (round(acept / respondidas, 3) if respondidas else None),
        "lista": lista,
    }


# Alias de compatibilidad: el nombre viejo se usó mientras esta capa se
# llamaba "sugerencias". Se conserva para no romper imports existentes.
generar_sugerencias = generar_recomendaciones
