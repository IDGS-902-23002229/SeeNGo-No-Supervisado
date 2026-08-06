# SEENGO — Detector de rutinas del hogar

Detecta **rutinas de encendido/apagado** de dispositivos del hogar (controlados
por gestos de mano) y **ausencias prolongadas**, a partir de los eventos que la
aplicación guarda en MongoDB Atlas. Sin entrenamiento: agrupamiento por densidad
sobre la hora del día (DBSCAN circular propio) + estadística circular. Pensado
para correr en una **Raspberry Pi**.

```
Modelo-DBscan/
├── datos/         # generador de datos de prueba + siembra en Atlas
├── modelo/        # el detector, Python puro (sin numpy/pandas/sklearn)
├── consumidor/    # lee Atlas, corre el modelo, sirve la API y el tablero
├── pantalla/      # el tablero, un solo archivo + Chart.js vendorizado
└── archivo/       # iteraciones previas, conservadas como evidencia
```

**Una sola fuente de datos: MongoDB Atlas.** No hay modo offline ni set de
ejemplo de respaldo; si Atlas no responde, el sistema lo dice claramente en vez
de mostrar datos que se confundirían con los reales.

---

## 0. Preparar el entorno (una sola vez)

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
# edita .env con tu MONGO_URI real
```

En Windows, `zoneinfo` necesita el paquete `tzdata` (Linux y Raspberry Pi ya
traen la base IANA del sistema); ya está en `requirements.txt` condicionado a
`sys_platform == "win32"`.

**En VS Code:** abre esta carpeta, selecciona el intérprete `.venv` (paleta de
comandos → *Python: Select Interpreter*) y usa `F5` para elegir entre servidor
en vivo, corrida contra Atlas o siembra.

---

## 1. Sembrar datos de prueba en Atlas

```powershell
.venv\Scripts\python datos\sembrar_atlas.py --dry-run     # sólo cuenta
.venv\Scripts\python datos\sembrar_atlas.py --limpiar     # borra siembra previa y resiembra
```

Inserta **tres capas** que van siempre juntas, porque cada una demuestra una
fase distinta:

| Capa | Qué contiene | Qué demuestra |
|---|---|---|
| **claros** | Rutinas nítidas: sala 19:30 entre semana y 21:00 el fin de semana, recámara 07:00 y 22:30 | El DBSCAN encuentra los patrones |
| **difíciles** | Cruce de medianoche (~00:00), un dispositivo con dos rutinas el mismo día (13:30 y 22:30), rutina con desplazamiento, cobertura insuficiente | La diferencia entre candidata y confirmada |
| **basura** | Confianza 0.20–0.60, ráfagas, duplicados exactos, `deviceId` desconocido, timestamps rotos | La limpieza funcionando en vivo |

**La base queda sucia a propósito.** Si sólo hubiera datos limpios, la fase de
preprocesamiento no tendría nada que mostrar y el embudo saldría plano.

Detalles importantes:

- `--limpiar` borra **sólo** los documentos marcados `origen: "seed"`. Los
  eventos reales de la aplicación nunca se tocan.
- El calendario se ancla **a la fecha de hoy**, no a fechas fijas, y coloca el
  hueco de ausencia de 25 días **justo antes** de los datos reales (si cayera
  encima, se rellenaría y la alerta nunca saltaría).
- Ese calendario ocupa ~83 días, por eso la ventana por defecto es de **90 días**
  (`SEENGO_DIAS`). Con 60 se pierde el primer tramo. El script avisa si no cabe.

## 2. Correr el modelo (una pasada)

```powershell
.venv\Scripts\python consumidor\consumir_mongo.py --dias 90
```

Lee Atlas, corre el modelo y escribe `resultados.json` y
`pantalla/resultados.js`. Si Atlas falla, sale con código distinto de cero.

## 3. Tablero en vivo (lo que se presenta)

```powershell
.venv\Scripts\python consumidor\servidor.py
# abre http://localhost:8000/
```

Se actualiza solo cada 30 minutos (`SEENGO_REFRESH_MIN`), más el botón
**"Actualizar ahora"**. Desde otro dispositivo de la misma red:
`http://IP-DE-LA-PI:8000/`.

La página está organizada por las **cinco fases** de la extracción de
conocimiento, en orden: selección de datos, preprocesamiento, minería,
interpretación/evaluación y uso del conocimiento.

**Por qué la pantalla no habla con Mongo:** el navegador sólo habla con este
servidor; las credenciales viven en el servidor (la Pi) y nunca llegan al
navegador. Si Atlas no responde, el servidor sirve la última corrida buena
guardada en disco, marcada como **obsoleta** y con su hora.

**Arranque automático en la Pi:** `consumidor/seengo.service` (unit de systemd),
con las instrucciones dentro del archivo.

---

## 4. Cómo usar el modelo desde la app móvil

Hay dos vías. Cuál conviene depende de si la app tiene un backend en Python.

### Vía 1 — API REST (recomendada si la app es Flutter, Kotlin, Swift o React Native)

La app sólo consume JSON; no necesita Python ni las credenciales de Mongo. El
servidor responde con **CORS abierto**, así que se puede llamar desde otro origen.

| Endpoint | Para qué |
|---|---|
| `GET /api/salud` | ¿el servicio está vivo y qué tan frescos son los datos? |
| `GET /api/resultado` | el análisis completo |
| `GET /api/rutinas?vista=entre_semana` | **sólo las rutinas confirmadas**, ya aplanadas |
| `GET /api/alertas` | ausencia prolongada y avisos activos |
| `GET /api/recomendaciones` | capa prescriptiva + aceptación 1/0 |
| `POST /api/refrescar` | fuerza un recálculo inmediato |
| `POST /api/recomendaciones/responder` | registra `{clave, aceptada: 1\|0}` |

`vista` puede ser `entre_semana`, `fin_de_semana` o `semana_completa`.

```bash
curl "http://IP-DEL-SERVIDOR:8000/api/rutinas?vista=entre_semana"
```

Respuesta real (recortada a una rutina):

```json
{
  "vista": "entre_semana",
  "generado": "2026-08-05T22:51:45.837283-06:00",
  "obsoleto": false,
  "total": 8,
  "rutinas": [
    {
      "hora_hhmm": "07:02",
      "tolerancia_hhmm": "00:06",
      "n_interacciones": 18,
      "dias_cubiertos": 18,
      "posibles": 24,
      "cobertura": 0.75,
      "confianza": "MEDIA",
      "confirmada": true,
      "dias_semana": ["Lun", "Mar", "Mié", "Jue", "Vie"],
      "device": "foco-recamara",
      "action": "off"
    }
  ]
}
```

El campo `obsoleto` importa: si es `true`, los datos son reales pero de una
corrida anterior porque no hubo conexión con Atlas. La app debería avisarlo.

### Vía 2 — Módulo importable (si hay un backend Python de por medio)

```python
from modelo import analizar_desde_atlas, generar_recomendaciones

resultado = analizar_desde_atlas(os.environ["MONGO_URI"])
print(resultado["meta"]["interacciones"])
print(generar_recomendaciones(resultado))
```

Y si el backend ya tiene los eventos y no quiere que el modelo toque la red:

```python
from modelo import analizar
resultado = analizar(eventos)      # eventos = lista de dicts
```

`detector_rutinas.py` **no importa pymongo**: el import es perezoso y vive en
`modelo/fuente_atlas.py`, así que `from modelo import analizar` funciona aunque
pymongo no esté instalado. Esa pureza es lo que permite correr el detector en
la Raspberry Pi sin arrastrar dependencias.

---

## Qué debe detectar (para verificar)

- `foco-sala / on` se **separa**: ~19:30 entre semana y ~21:00 el fin de semana.
- `enchufe-sala / on` cruza medianoche → la media circular da **00:00**, no 12:00.
- `foco-cocina / off` tiene **DOS** rutinas (13:30 y 22:30), no una a las 18:00.
- Candidatas por debajo del umbral de cobertura → **no confirmadas**.
- `dispositivo-fantasma` y la mega-ráfaga de 250 eventos → ruido, nunca rutina.
- Hueco de **25 días** sin actividad → **ALERTA** de ausencia.

## Ajustes (`CONFIG` en `modelo/detector_rutinas.py`)

`tz`, `conf_min` (0.70), `debounce_seg` (90), `eps_horas` (1.0),
`min_muestras` (3), `min_dias_absoluto` (7), `min_cobertura` (0.60),
`ausencia_aviso` (15), `ausencia_alerta` (25).

## Variables de entorno

| Variable | Default | Para qué |
|---|---|---|
| `MONGO_URI` | — | **obligatoria**, nunca en el código |
| `MONGO_DB` | `seengo` | base de datos |
| `MONGO_COLL` | `sign_events` | colección de eventos |
| `MONGO_COLL_SUG` | `sugerencias` | colección de recomendaciones y respuestas |
| `SEENGO_PORT` | `8000` | puerto del tablero |
| `SEENGO_DIAS` | `90` | ventana rodante en días |
| `SEENGO_REFRESH_MIN` | `30` | cada cuánto se relee Atlas |

## Seguridad

La URI se lee de `MONGO_URI`; **no** la escribas en el código ni en
`.env.example`. Tu `.env` real está en `.gitignore` y nunca se sube. Rota la
contraseña de Mongo que quedó expuesta en su momento.
