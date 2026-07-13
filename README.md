# SEENGO — Detector de rutinas del hogar (v3)

Tres piezas **separadas**, más un consumidor que las une:

```
Modelo-DBscan/
├── datos/         # ejemplos JSON para importar a Mongo Atlas (+ generador)
├── modelo/        # el detector, Python puro (sin numpy/pandas/sklearn)
├── consumidor/    # lee Mongo (o los JSON) y produce resultados
└── pantalla/      # vista de un solo archivo, sin servidor ni librerías
```

## 0. Preparar el entorno (una sola vez)

Ya viene un entorno virtual `.venv/` con las dependencias instaladas. Si lo
recreas desde cero:

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

En Windows, `zoneinfo` necesita el paquete `tzdata` (Linux/Raspberry Pi ya
traen la base de datos IANA del sistema, Windows no) — ya está en
`requirements.txt` condicionado a `sys_platform == "win32"`.

**En VS Code:** abre esta carpeta, selecciona el intérprete `.venv` (paleta
de comandos → *Python: Select Interpreter*) y usa:
- `F5` → elige una de las 3 configuraciones en `.vscode/launch.json`
  (probar offline, correr contra Mongo, regenerar datos de ejemplo).
- `Ctrl+Shift+B` → corre la tarea por defecto (`.vscode/tasks.json`): prueba
  offline con los 3 archivos de ejemplo.
- Extensiones recomendadas (VS Code las sugiere solas): Python + Pylance, y
  opcionalmente *Live Server* para ver `pantalla/index.html` con recarga
  automática cada vez que regeneras `resultados.js`.

## 1. Probar YA, sin tocar Mongo

```powershell
.venv\Scripts\python consumidor\consumir_mongo.py --archivos "datos/ejemplos_*.json"
# abre pantalla/index.html en el navegador (doble clic o file://)
```

Esto corre el modelo sobre los 3 archivos de ejemplo, escribe
`resultados.json` y `pantalla/resultados.js`, y la pantalla los muestra.

## 2. Cargar los datos de ejemplo a Mongo Atlas

Los archivos ya están en el esquema de tu colección (`deviceId`, `action`,
`confidence`, `ts` en UTC). No traen `_id`, así que Atlas los asigna solo.

```bash
mongoimport --uri "$MONGO_URI" --db seengo --collection sign_events \
  --jsonArray --file datos/ejemplos_claros.json
# repite con ejemplos_dificiles.json y ejemplos_basura.json
```

## 3. Correr contra Mongo (ventana rodante)

Opción cómoda para VS Code: copia `.env.example` a `.env` y llena tus datos
reales ahí (ese archivo está en `.gitignore`, nunca se sube). Se carga solo
gracias a `python-dotenv`.

```powershell
copy .env.example .env
# edita .env con tu MONGO_URI real
.venv\Scripts\python consumidor\consumir_mongo.py --dias 60
```

O sin `.env`, exportando la variable directo en la terminal:

```powershell
$env:MONGO_URI = "mongodb+srv://USUARIO:PASSWORD@cluster.mongodb.net/"
$env:MONGO_DB = "seengo"
$env:MONGO_COLL = "sign_events"
.venv\Scripts\python consumidor\consumir_mongo.py --dias 60
```

Esto es un proceso **de una sola pasada**: lee Mongo, escribe
`pantalla/resultados.js` y termina. Para ver datos nuevos hay que volver a
correrlo y refrescar la página. Si prefieres que se actualice solo, usa el
servidor en vivo (siguiente sección).

## 4. Tablero en vivo (para dejar corriendo en la Raspberry Pi)

`consumidor/servidor.py` es un servidor web que **se actualiza solo**: cada
30 min (configurable) lee Mongo, corre el modelo y sirve la pantalla. Solo usa
la librería estándar + pymongo; nada pesado.

```powershell
.venv\Scripts\python consumidor\servidor.py
# abre http://localhost:8000/
```

Desde otro dispositivo de la misma red (tu celular, otra compu) entra a
`http://IP-DE-LA-PI:8000/`. La página sondea `/api/fuentes.json` cada 2 min
y se re-dibuja sola cuando hay datos nuevos.

Ajustes por variable de entorno (o en `.env`): `SEENGO_PORT` (8000),
`SEENGO_DIAS` (60), `SEENGO_REFRESH_MIN` (30).

### Dos fuentes en el tablero (Atlas vs. ejemplo)

El tablero puede mostrar **dos orígenes de datos**, elegibles con una pestaña
arriba del título:

- **Atlas** — tus datos reales de MongoDB (en vivo con el servidor).
- **Datos de ejemplo** — el set de referencia del repo, útil para comparar
  contra una casa con semanas de historia (14 rutinas, ausencia, etc.).

La pestaña aparece sólo cuando hay **al menos dos fuentes** disponibles. Cómo
se llenan:

- El **servidor en vivo** calcula ambas solo (Atlas al refrescar, ejemplo una
  vez al arrancar). No tienes que hacer nada.
- En modo manual (sin servidor), corre el consumidor **una vez por fuente** y
  quedan las dos en `resultados.js` (cada corrida conserva la otra):

  ```powershell
  .venv\Scripts\python consumidor\consumir_mongo.py --archivos "datos/ejemplos_*.json"
  .venv\Scripts\python consumidor\consumir_mongo.py --dias 60
  ```

Internamente, `resultados.js` guarda un envelope
`window.SEENGO_FUENTES = { atlas, local }`. La pantalla acepta también el
formato viejo de una sola fuente, así que nada se rompe.

**Importante — por qué no es la pantalla la que habla con Mongo:** el navegador
solo habla con *este servidor*; las credenciales viven en el servidor (la Pi),
nunca llegan al navegador. Por eso el tablero en vivo necesita que Python esté
corriendo detrás; no es un HTML suelto que consulta la base.

**Arranque automático al encender la Pi:** hay un `consumidor/seengo.service`
(unit de systemd) listo para copiar; las instrucciones están dentro del propio
archivo.

## Qué debe detectar (para verificar)

**Rutinas claras (deben salir CONFIRMADAS):**
- `foco-sala / on` ~19:30 entre semana · ~21:00 fin de semana (¡se separan!)
- `foco-sala / off` ~23:10 entre semana
- `foco-recamara / off` ~07:00 entre semana
- `foco-recamara / on` ~22:30 todos los días

**Casos difíciles (vulnerabilidades):**
- `enchufe-sala / on` cruza medianoche → media circular da **00:00**, no 12:00.
- `foco-cocina / off` tiene **DOS** rutinas (13:30 y 22:30), no una a las 18:00.
- `foco-patio / on` con *drift* 19:45→21:15 → una rutina de tolerancia ancha.
- `foco-cocina / on` con confianza baja → **filtrada**, no es rutina.
- `enchufe-sala / toggle` en pocos días → **BAJA**, sin confirmar.

**Basura:** eventos sueltos y una mega-ráfaga de 250 eventos a las 03:00 →
todo queda como *ruido*; nada se confirma.

**Ausencia larga:** hueco de **25 días** (29-may a 22-jun) → **ALERTA**.

## Ajustes (en `modelo/detector_rutinas.py`, dict `CONFIG`)

`conf_min`, `debounce_seg`, `eps_horas`, `min_muestras`, `min_dias_absoluto`,
`min_cobertura`, `ausencia_aviso` (15 d), `ausencia_alerta` (25 d).

## Seguridad

Rota la contraseña de Mongo que quedó expuesta. El consumidor lee la URI de
la variable de entorno `MONGO_URI`; **no** la escribas en el código ni en
`.env.example`. Tu `.env` real está ignorado por git.
