# SEENGO — Detector de rutinas del hogar

Contexto para Claude Code. Mantener este archivo corto y de alta señal.

## Qué es

Sistema que detecta **rutinas de encendido/apagado** de dispositivos del hogar
(controlados por gestos de mano) y **ausencias prolongadas**, a partir de
eventos guardados en MongoDB. Sin entrenamiento: agrupamiento por densidad
sobre la hora del día + estadística circular. Pensado para correr periódicamente
en una **Raspberry Pi**.

Usuario/dueño: Miguel. Idioma del código y comentarios: **español**.

## Por qué el diseño es así (contexto crítico)

Nació de un script v2 que asumía 3 semanas de datos limpios. Los datos reales
de Mongo revelaron tres problemas que definen el diseño actual (v3):

1. **Ráfagas.** El reconocedor de gestos dispara decenas de eventos por segundo
   por una sola acción humana (mediana ~2 s entre eventos). Hay que hacer
   **debounce ANTES de agrupar**, o la densidad que ve el clustering es densidad
   de ráfaga, no de comportamiento. ~2900 eventos crudos → ~235 interacciones.
2. **Zona horaria.** Los `ts` se guardan en **UTC**; el hogar está en
   `America/Mexico_City` (UTC-6, sin horario de verano). Convertir SIEMPRE a
   hora local antes de calcular la hora del día. La conversión cambia también
   la fecha (un evento de las 19:30 local cae en el día UTC siguiente).
3. **Pocos días reales.** Nunca declarar una rutina con muy pocos días de
   evidencia, aunque haya muchos eventos.

## Estructura (las 4 piezas van separadas a propósito)

- `datos/` — `generar_datos.py` define las 3 capas de prueba (semilla fija,
  calendario anclado a hoy); `sembrar_atlas.py` las inserta EN ATLAS.
- `modelo/` — el detector. **Python puro, sin dependencias.** No sabe nada de
  Mongo ni de la pantalla. `detector_rutinas.py` (motor), `recomendaciones.py`
  (capa prescriptiva), `fuente_atlas.py` (único que toca red, import perezoso
  de pymongo), `__init__.py` (API pública para la app móvil).
- `consumidor/` — `consumir_mongo.py` lee Atlas y corre el modelo;
  `servidor.py` sirve el tablero y la API REST.
- `pantalla/index.html` — el tablero, un solo archivo, con Chart.js y
  chartjs-chart-matrix **vendorizados** en `pantalla/vendor/` (nunca CDN).
- `archivo/` — iteraciones previas (rutinas.py, rutinas-v2.py,
  modelo_seengo.py, el tablero SVG). No es código en uso.

## Tubería del modelo (en orden)

0. **Selección** (`consumidor`): ventana rodante + proyección explícita de los
   4 campos que el modelo usa. Va en `meta.seleccion`.
1. **Normalizar**: filtrar por `confidence`, descartar `ts` inválidos y
   duplicados exactos (todo contado en `meta.descartes`), convertir a hora
   local (tz real).
2. **Debounce**: colapsar ráfagas del mismo `(deviceId, action)` separadas por
   < `debounce_seg` a una sola interacción (representada por el primer evento).
3. **DBSCAN circular** (implementación propia, ver abajo) por cada par
   `(deviceId, action)`: agrupa por hora del día. eps ≈ 1 h, min_samples chico.
4. **Confirmación por cobertura**: `días_cubiertos / días_activos_del_tipo`,
   con piso absoluto `min_dias_absoluto`. Niveles ALTA/MEDIA/BAJA.
5. **Ausencia larga**: mayor racha de días consecutivos SIN ningún evento.
   aviso ≥ 15 d, alerta ≥ 25 d. En modo Mongo detecta también la racha abierta
   hasta hoy.

Vistas por stream: `entre_semana`, `fin_de_semana`, `semana_completa`.

## Convenciones NO negociables

- **`modelo/` no importa numpy, pandas ni scikit-learn.** Solo librería estándar
  (`math`, `datetime`, `zoneinfo`, `collections`). Es a propósito, para la Pi.
  El DBSCAN circular está reimplementado a mano; no lo reemplaces por sklearn.
- **Nunca hardcodear credenciales de Mongo.** La URI se lee de la variable de
  entorno `MONGO_URI` (opcionales `MONGO_DB`, `MONGO_COLL`). La contraseña que
  se filtró en el chat original debe estar rotada.
- El modelo se mantiene **stateless** y libre de dependencias de I/O; toda la
  conexión a Mongo vive en `consumidor/`.
- Estadística de horas siempre **circular** (media/desv con seno/coseno) para
  manejar la medianoche. No usar promedios aritméticos de horas.
- Comentarios y nombres en español.

## Esquema de evento (entrada del modelo y colección Mongo)

```json
{"deviceId": "foco-sala", "action": "on|off|toggle",
 "confidence": 0.92, "ts": "2026-05-01T01:30:00+00:00"}
```

`ts` en UTC (string ISO o Date de Mongo). `gesture` existe en los datos pero es
redundante con `action` (puno=off, palma_abierta=on, paz=toggle); el modelo usa
`action`.

## Cómo correr y verificar

**NO hay modo offline.** La ruta `--archivos` se eliminó a propósito: tener dos
fuentes hacía imposible saber qué se estaba mirando. Todo va contra Atlas.

```bash
# .env con MONGO_URI (nunca en el código). Luego:
python datos/sembrar_atlas.py --limpiar   # siembra las 3 capas en Atlas
python consumidor/consumir_mongo.py       # una pasada
python consumidor/servidor.py             # tablero + API en :8000
```

Los conteos cambian con cada siembra (el calendario se ancla a HOY), así que no
hay una regresión de números fijos. Lo que SÍ debe cumplirse siempre está en la
lista de abajo.

## Qué debe detectar (casos de prueba en los datos)

- Claras: `foco-sala/on` ~19:30 (semana) y ~21:00 (finde) SEPARADAS;
  `foco-sala/off` ~23:10; `foco-recamara/off` ~07:00; `foco-recamara/on` ~22:30.
- Vulnerabilidades: cruce de medianoche → 00:00 (no 12:00);
  `foco-cocina/off` da DOS rutinas (13:30 y 22:30), no una a las 18:00;
  baja confianza → filtrada; toggle escaso → BAJA sin confirmar; drift → una
  rutina de tolerancia ancha.
- Basura (ruido, nunca confirmada): eventos sueltos, mega-ráfaga de 250 eventos
  a las 03:00, `dispositivo-fantasma`, duplicados exactos y `ts` rotos.
- Ausencia: hueco de 25 días → ALERTA. El hueco se coloca justo ANTES de los
  datos reales de la app; si cayera encima se rellenaría y no saltaría nada.

## Ajustes (dict `CONFIG` en `modelo/detector_rutinas.py`)

`tz`, `conf_min` (0.70), `debounce_seg` (90), `eps_horas` (1.0),
`min_muestras` (3), `min_dias_absoluto` (7), `min_cobertura` (0.60),
`ausencia_aviso` (15), `ausencia_alerta` (25).

## Pendientes / ideas abiertas

- El *drift* hoy se absorbe en una rutina de tolerancia ancha; opcional:
  detectarlo y marcarlo como "rutina en movimiento" o partirlo.
- Helper `cargar.py` (pymongo `insert_many`) como alternativa a `mongoimport`.
- Índice en Mongo por `ts` para acelerar la ventana rodante.

## Hecho (setup VS Code, 2026-07-09)

- Proyecto organizado en la raíz (`datos/`, `modelo/`, `consumidor/`,
  `pantalla/`), `.venv/` + `requirements.txt`, `.vscode/` (settings/launch/
  tasks/extensions), `.env.example` + carga opcional vía `python-dotenv`.
- En Windows, `zoneinfo` necesita el paquete `tzdata` (agregado a
  `requirements.txt` solo para `sys_platform == "win32"`; la Pi no lo
  necesita). Sin esto, `ZoneInfo("America/Mexico_City")` truena en Windows.
- Vista de mapa de calor día×hora agregada en `pantalla/index.html`, por
  stream (device+action), dentro de la sección de detalle. Nuevo campo
  `mapa_semanal` (grilla 7x24, Lun..Dom x 00..23) en cada stream de
  `analizar()` — aditivo, no cambia ninguna detección existente.
- Verificado contra Mongo real: los campos de la colección coinciden con lo
  que espera `_mapear()` (`deviceId`, `action`, `confidence`, `ts` str ISO);
  `userId`/`gesture` sobran y se ignoran. No se tocó `modelo/` ni `_mapear()`.

## Servidor y API (2026-08-05)

- `consumidor/servidor.py`: tablero + API REST (stdlib `http.server` + pymongo,
  sin deps pesadas). Hilo de fondo refresca desde Atlas cada
  `SEENGO_REFRESH_MIN` min y reescribe `pantalla/resultados.js` y
  `resultados.json`. Reutiliza `analizar_atlas()` del consumidor (la conexión
  a Mongo sigue viviendo en `consumidor/`).
- Endpoints: `/`, `/api/salud`, `/api/resultado`, `/api/rutinas?vista=`,
  `/api/alertas`, `/api/recomendaciones`, `POST /api/refrescar`,
  `POST /api/recomendaciones/responder`. CORS abierto (+ OPTIONS) para la app
  móvil. `Cache-Control: no-store` en TODO: el navegador llegó a servir un
  index.html viejo de caché mientras el disco ya tenía el nuevo.
- Red de seguridad: si Atlas no responde al arrancar, se sirve la última
  corrida buena de `resultados.json` marcada con `meta.obsoleto = true`, y la
  pantalla lo dice. NO es volver a datos de ejemplo.
- `consumidor/seengo.service`: unit systemd para arrancar en la Pi.
- Vars: `SEENGO_PORT` (8000), `SEENGO_DIAS` (90), `SEENGO_REFRESH_MIN` (30).

## Una sola fuente: Atlas (2026-08-05)

- Se ELIMINÓ la fuente `local` y el envelope `{atlas, local}`: tener dos
  orígenes hacía imposible saber qué se estaba mirando, y permitía que una
  demo "funcionara" sin tocar la base real. `resultados.js` vuelve a ser
  `window.SEENGO_RESULTADOS` con un solo resultado.
- `consumir_mongo.py` ya no acepta `--archivos`. Si Atlas falla: mensaje claro
  y exit 1, nunca caída a datos locales.
- `meta.seleccion` (cluster, base, colección, campos, ventana, documentos) es
  aditivo y lo pone el CONSUMIDOR, no el modelo. El host del cluster se guarda
  sin usuario ni contraseña.

## Recomendaciones y aceptación (2026-08-05)

- `modelo/recomendaciones.py` (antes `sugerencias.py`, puro y sin deps):
  `generar_recomendaciones(resultado)` con tres reglas sobre la salida del
  modelo — rutina confirmada → automatizar; encendido sin apagado → consumo
  olvidado; ausencia → simulación de presencia. Cada una lleva `tipo`,
  `mensaje`, `prioridad`, `evidencia` y una `clave` estable
  (`rutina|dev|act|hhmm`, hora redondeada a 30 min).
- Colección Mongo `sugerencias` (env `MONGO_COLL_SUG`); se conserva el nombre
  para no huerfanar lo ya guardado. Upsert por `clave` con `$set` para los
  campos descriptivos (si cambian los datos, el texto los sigue) y
  `$setOnInsert` SÓLO para `aceptada`/`creada`/`respondida`. Con
  `$setOnInsert` en todo, el mensaje quedaba congelado: se llegó a ver
  "23 días" junto a una alerta de 25.
- Se listan sólo las recomendaciones de la corrida ACTUAL, enriquecidas con
  las respuestas guardadas, para que no aparezcan huérfanas de análisis viejos.
- `res["recomendaciones"]` es aditivo; la pantalla oculta la sección si falta.

## Siembra en Atlas (2026-08-05)

- `datos/sembrar_atlas.py` importa las tres generadoras de `generar_datos.py`
  (no las reescribe) e inserta en Atlas. `--limpiar` borra sólo
  `origen: "seed"`: los eventos reales de la app nunca se tocan.
- El calendario se ancla a HOY (antes eran fechas fijas de may-jun 2026, y al
  correr el proyecto meses después casi todo caía fuera de la ventana).
- El hueco de ausencia se coloca JUSTO ANTES del primer dato real. Si cae
  encima de días con actividad real se rellena y la alerta nunca salta.
- El calendario ocupa ~83 días, por eso `SEENGO_DIAS` es 90 y no 60.
- `ejemplos_basura()` incluye confianza 0.20-0.60, duplicados exactos,
  `deviceId` desconocido y `ts` rotos: sin eso el embudo de preprocesamiento
  sale plano y no hay limpieza que demostrar.
- `normalizar()` NO revienta con un `ts` malformado: lo cuenta en
  `meta.descartes` y sigue. Antes un solo `ts` roto tumbaba `analizar()`.

## Cuidado especial

- No reintroducir dependencias pesadas en `modelo/`.
- No tocar la lógica de zona horaria sin re-verificar los casos de medianoche.
- No commitear `MONGO_URI` ni `resultados.json` con datos reales sensibles.
