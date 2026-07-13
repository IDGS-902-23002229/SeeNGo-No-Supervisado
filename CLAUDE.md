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

- `datos/` — JSON de ejemplo para importar a Atlas + `generar_datos.py`
  (semilla fija, reproducible). Esquema idéntico al de la colección real.
- `modelo/detector_rutinas.py` — el detector. **Python puro, sin dependencias.**
  No sabe nada de Mongo ni de la pantalla. Entrada: lista de eventos. Salida:
  dict serializable a JSON.
- `consumidor/consumir_mongo.py` — lee Mongo (o los JSON locales), llama al
  modelo, escribe `resultados.json` y `pantalla/resultados.js`.
- `pantalla/index.html` — vista de un solo archivo (SVG + JS vanilla), sin
  servidor ni librerías. Consume `resultados.js`.

## Tubería del modelo (en orden)

1. **Normalizar**: filtrar por `confidence`, convertir a hora local (tz real).
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

```bash
# Prueba offline (sin Mongo) — la más rápida:
python3 consumidor/consumir_mongo.py --archivos "datos/ejemplos_*.json"
# luego abrir pantalla/index.html

# Contra Atlas:
export MONGO_URI="mongodb+srv://USER:PASS@cluster.mongodb.net/"
pip install "pymongo[srv]"
python3 consumidor/consumir_mongo.py --dias 60
```

Salida esperada de la prueba offline (regresión rápida):
`2935 crudos -> 235 interacciones | 34 días activos | 14 rutinas confirmadas | Ausencia: alerta`

## Qué debe detectar (casos de prueba en los datos)

- Claras: `foco-sala/on` ~19:30 (semana) y ~21:00 (finde) SEPARADAS;
  `foco-sala/off` ~23:10; `foco-recamara/off` ~07:00; `foco-recamara/on` ~22:30.
- Vulnerabilidades: cruce de medianoche → 00:00 (no 12:00);
  `foco-cocina/off` da DOS rutinas (13:30 y 22:30), no una a las 18:00;
  baja confianza → filtrada; toggle escaso → BAJA sin confirmar; drift → una
  rutina de tolerancia ancha.
- Basura (ruido, nunca confirmada): eventos sueltos y una mega-ráfaga de 250
  eventos a las 03:00 en un solo día.
- Ausencia: hueco de 25 días (2026-05-29 a 2026-06-22) → ALERTA.

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

## Servidor en vivo (2026-07-13)

- `consumidor/servidor.py`: tablero web que se auto-actualiza (stdlib
  `http.server` + pymongo, sin deps pesadas). Hilo de fondo refresca desde
  Mongo cada `SEENGO_REFRESH_MIN` min (default 30), corre `analizar()` y deja
  el resultado en memoria + reescribe `pantalla/resultados.js`. Reutiliza
  `leer_mongo` del consumidor (la conexión sigue viviendo en `consumidor/`).
- Endpoints: `/` (pantalla), `/api/resultados.json` (última foto),
  `/api/estado` (salud). `index.html` sondea `/api/resultados.json` cada 2 min
  y sólo re-dibuja si `meta.generado` cambió; en `file://` el fetch falla en
  silencio y se queda con el snapshot (aditivo, no rompe el modo estático).
- `consumidor/seengo.service`: unit systemd de ejemplo para arrancar en la Pi.
- Vars: `SEENGO_PORT` (8000), `SEENGO_DIAS` (60), `SEENGO_REFRESH_MIN` (30).

## Dos fuentes en el tablero (2026-07-13)

- La pantalla ofrece una pestaña que alterna entre `atlas` (Mongo real, en
  vivo) y `local` (set de ejemplo del repo). Redibuja todo el tablero al
  cambiar. La pestaña sólo aparece si hay ≥2 fuentes.
- Formato de datos: envelope `{atlas, local}`. `consumir_mongo.py` escribe
  `window.SEENGO_FUENTES = {atlas, local}` y **conserva la otra fuente** entre
  corridas (rellena solo la suya según `--archivos`→local / `--dias`→atlas).
  El servidor calcula `local` una vez al arrancar y refresca `atlas`; expone
  `/api/fuentes.json` (envelope) y conserva `/api/resultados.json` (solo atlas)
  por compatibilidad. `index.html` acepta el envelope nuevo o el
  `window.SEENGO_RESULTADOS` viejo de una sola fuente (no rompe nada).
- `meta.fuente` ("atlas"/"local") es un campo aditivo; no cambia detección.
  `modelo/` sigue intacto.

## Cuidado especial

- No reintroducir dependencias pesadas en `modelo/`.
- No tocar la lógica de zona horaria sin re-verificar los casos de medianoche.
- No commitear `MONGO_URI` ni `resultados.json` con datos reales sensibles.
