# SEENGO — migración a Atlas en vivo, API para móvil y tablero único

> Guárdame en la raíz del repo como `SPEC-entrega.md` y ejecútame.
> Hay una presentación evaluada mañana. Prioriza que **funcione y se pueda demostrar**
> por encima de que esté elegante.

---

## 0. Antes de tocar nada

Este documento se escribió contra un estado anterior del repo. **Verifica la realidad primero.**
Lee y repórtame en una lista corta:

1. `README.md` y `CLAUDE.md`
2. `modelo/detector_rutinas.py` — el dict `CONFIG`, la función `analizar()`, `mapa_semanal()`,
   `ausencia_larga()`, `_confianza()`
3. `consumidor/consumir_mongo.py` y `consumidor/servidor.py`
4. `pantalla/` — qué archivos existen hoy
5. `datos/generar_datos.py` — qué genera y con qué forma
6. Qué hay en la raíz: `rutinas.py`, `rutinas-v2.py`, `modelo_seengo.py`
7. Confirma que `.env` está en `.gitignore` y que **ninguna credencial está commiteada**.
   Si encuentras una URI de Mongo dentro de algún archivo versionado, **detente y avísame**.

Si algo de lo que sigue contradice el repo, **gana el repo**: dímelo antes de improvisar.

---

## 1. Contexto y objetivo

SEENGO detecta rutinas del hogar con un modelo no supervisado (DBSCAN circular sobre la hora
del día). Hoy el proyecto arrastra tres problemas:

- El modelo se alimenta de archivos JSON de ejemplo en `datos/`, no de la base real.
- Hay dos tableros (uno en SVG nativo, uno en Chart.js) y dos fuentes de datos (`atlas` y
  `local`), lo que confunde.
- Hay versiones viejas del modelo tiradas en la raíz.
- No existe forma de que la app móvil use el modelo.

**Objetivo:** que el modelo trabaje **únicamente contra MongoDB Atlas**, que haya **un solo
tablero** (Chart.js), y que quede un **pipeline consumible desde la app móvil** por dos vías:
API REST y módulo Python importable.

---

## 2. Restricciones

- **Credenciales solo en `.env`.** Nunca en código, nunca en un commit, nunca en el navegador.
  Usa `.env.example` con placeholders. Si `.env` no está en `.gitignore`, agrégalo y avísame.
- **`modelo/detector_rutinas.py` se mantiene puro**: sin `pymongo`, sin HTTP, sin rutas de
  archivo. Recibe una lista de eventos, devuelve un dict serializable. Esa pureza es lo que
  permite que la app móvil lo importe directo.
- **No inventes campos nuevos en la salida del modelo** sin avisarme. Si algo del tablero o la
  API necesita un dato que el modelo no da, propónmelo y lo decido yo.
- Dependencias mínimas: librería estándar de Python + `pymongo`. Nada de FastAPI, Flask ni
  frameworks nuevos. El servidor actual ya usa `http.server`; extiéndelo.

---

## 3. Tareas

### 3.1 Limpieza del repo

Crea `archivo/` y mueve ahí, **con `git mv` para conservar el historial**:

- `rutinas.py`
- `rutinas-v2.py`
- `modelo_seengo.py`
- `pantalla/index.html` (el tablero SVG antiguo) → `archivo/pantalla-svg/index.html`

Agrega `archivo/README.md` de un párrafo explicando que son iteraciones previas conservadas
como evidencia del proceso, no código en uso. **No borres nada.**

### 3.2 Un solo tablero

- Renombra `pantalla/index-chartjs.html` → `pantalla/index.html`.
- Elimina por completo el concepto de fuente `local` / pestaña de fuentes / zona de arrastre de
  archivos. **Una sola fuente: Atlas.**
- Si Atlas no responde, la página muestra un estado claro de "sin conexión con la base" con la
  hora del último dato bueno. No inventa datos ni cae en un set local.

### 3.3 Datos de siembra en Atlas — *esto vale 5 puntos de la rúbrica*

Crea `datos/sembrar_atlas.py`. Inserta eventos **directamente en Atlas**, no en archivos.

**Reutiliza lo que ya existe.** `datos/generar_datos.py` ya tiene las tres funciones generadoras
(`ejemplos_claros()`, `ejemplos_dificiles()`, `ejemplos_basura()`) que hoy escriben archivos JSON.
**Impórtalas, no las reescribas.** El script de siembra solo cambia el destino: en vez de
`escribir()` a disco, `insert_many()` a la colección de Atlas. Léelas primero y confírmame que las
tres siguen produciendo lo que dice la tabla; si alguna se quedó corta, extiéndela ahí mismo en
lugar de duplicar lógica en el script nuevo.

**Punto crítico: la base tiene que quedar sucia a propósito.** La rúbrica evalúa la fase de
preprocesamiento (limpieza de ruido, outliers, duplicados, inconsistentes). Si en Mongo solo hay
datos limpios, no hay nada que demostrar. El modelo tiene que limpiar **en vivo, frente al
maestro**. Las tres capas y lo que cada una prueba:

| Capa | Función | Qué contiene | Qué demuestra en la exposición |
|---|---|---|---|
| **Buenos / claros** | `ejemplos_claros()` | Rutinas nítidas que el modelo debe confirmar con confianza ALTA: sala 19:30±8min de lunes a viernes con cobertura casi total; una que cruce la medianoche (~00:00) para lucir la media circular; un dispositivo con dos rutinas el mismo día (13:30 y 22:30) | **Fase 3:** el DBSCAN encuentra los patrones |
| **Complicados / difíciles** | `ejemplos_dificiles()` | Clústeres reales pero por debajo del umbral de cobertura (5 de 26 días); una rutina con desplazamiento entre semanas (±30 min) que debe salir con tolerancia ancha | **Fase 4:** se ve la diferencia entre candidata y confirmada, que es exactamente "evaluar si el patrón es conocimiento útil" |
| **Negativos / basura** | `ejemplos_basura()` | `confidence` bajo (0.2–0.6); ráfagas de 5–10 eventos idénticos en menos de 90 s; timestamps duplicados exactos; un `deviceId` desconocido; algún timestamp fuera de rango o malformado | **Fase 2:** se ve la limpieza funcionando en vivo |

Las tres capas se insertan **siempre juntas**. La basura no es un extra ni algo que se pueda
omitir: sin ella el embudo de la fase 2 no tiene nada que mostrar y se pierden 5 puntos.

Requisitos del script:

- Idempotente. Marca los documentos sembrados con `origen: "seed"` y una etiqueta de corrida, y
  ofrece `--limpiar` para borrar solo los de siembra antes de reinsertar.
- Flags: `--limpiar`, `--dias N` (default 60), `--capas claros,dificiles,basura` (default las
  tres), `--dry-run` que muestre el conteo por capa sin escribir nada.
- Al terminar imprime el resumen por capa y el total. Ese conteo es lo que vas a comparar contra
  el primer escalón del embudo.
- Un hueco de ~25 días consecutivos sin ningún evento, para disparar la alerta de ausencia.
- Respeta el esquema real de la colección. **Léelo de Atlas antes de escribir**, no lo asumas.

### 3.4 El modelo, solo contra Atlas

Reescribe `consumidor/consumir_mongo.py`:

- Quita las rutas `--archivos` y todo lo que lea de `datos/*.json`.
- Ventana rodante configurable (`--dias`, default el que ya use el repo).
- Proyección explícita de campos en la consulta: pide solo los que el modelo necesita, no el
  documento completo. Esa proyección **es** la fase 1 de la rúbrica, así que déjala visible y
  comentada.
- Si Atlas falla, error claro y código de salida distinto de cero. Nada de caer a datos locales.

### 3.5 API REST — *pipeline para la app móvil, vía 1*

Extiende `consumidor/servidor.py`. Endpoints:

```
GET  /                          -> pantalla/index.html
GET  /api/salud                 -> {ok, ultima_corrida, error, fuente:"atlas", dias_ventana}
GET  /api/resultado             -> el envelope completo del modelo
GET  /api/rutinas?vista=...     -> solo rutinas confirmadas de esa vista (lo que consume la app)
GET  /api/alertas               -> ausencia larga y cualquier aviso activo
GET  /api/recomendaciones       -> capa prescriptiva (ver 3.7)
POST /api/refrescar             -> fuerza recálculo inmediato (para el botón del tablero)
```

- Caché con TTL: no vuelvas a correr el modelo en cada petición. Refresco automático cada
  `SEENGO_REFRESH_MIN` minutos, más el refresco manual del POST.
- **CORS habilitado** (`Access-Control-Allow-Origin`), o la app móvil no podrá consumir.
- Respuestas siempre JSON con `Content-Type` correcto, incluidos los errores.
- **Red de seguridad para la demo:** guarda en disco el último resultado bueno y sírvelo con una
  marca `obsoleto: true` y su timestamp si Atlas no responde. Si mañana se cae el wifi de la
  escuela, el tablero sigue mostrando datos reales de la última corrida en lugar de quedarse en
  blanco. Esto **no** es volver a los datos fake: es el último resultado real, etiquetado como
  tal y visible en la interfaz.

### 3.6 Módulo importable — *pipeline para la app móvil, vía 2*

Deja `modelo/` listo para hacer `from modelo import analizar_desde_atlas`:

- `modelo/__init__.py` que exporte la API pública: `analizar()` (pura, sin red) y
  `analizar_desde_atlas(uri, db, coll, dias)`.
- Docstrings con un ejemplo de uso de tres líneas.
- Sección nueva en el README: **"Cómo usar el modelo desde la app móvil"**, con las dos vías,
  cuándo conviene cada una (REST si la app es Flutter/Kotlin/React Native y solo consume;
  módulo si hay un backend Python de por medio), y un ejemplo de petición con su respuesta JSON
  real recortada.

### 3.7 Capa prescriptiva — *fase 5 de la rúbrica*

El proyecto ya es descriptivo y predictivo; falta el "uso del conocimiento". Agrega en el modelo
una función que derive **recomendaciones accionables** de las rutinas ya detectadas. Nada de un
algoritmo nuevo: reglas simples sobre la salida existente.

Ideas: si hay ausencia en nivel de alerta → sugerir simulación de presencia replicando las
rutinas confirmadas; si una rutina de encendido no tiene su apagado correspondiente → posible
consumo olvidado; si hay rutinas ALTA → candidatas a automatizarse.

Cada recomendación con: `tipo`, `mensaje`, `prioridad`, y `evidencia` (qué rutina la sustenta).
Exponerla en `/api/recomendaciones` y en el tablero.

### 3.8 El tablero, organizado por las cinco fases de la rúbrica

Esto es lo más importante de la presentación. El maestro califica con una lista de cinco fases;
la página debe recorrerlas **en ese orden y con esos nombres**, cada una en su sección:

| Sección | Contenido |
|---|---|
| **1. Selección de datos** | Cluster, base, colección, campos proyectados, ventana de tiempo y número de documentos leídos. Puede ser una tarjeta de texto, no necesita gráfica. |
| **2. Preprocesamiento** | El embudo: crudos → descartados por confianza → tras debounce → interacciones reales. Agrega el desglose de **por qué** se descartó cada grupo. |
| **3. Minería de datos** | Gráfica de rutinas sobre el eje de 24 h + mapa de calor 7×24. Menciona en la tarjeta: DBSCAN circular, `eps`, `min_muestras`. |
| **4. Interpretación / Evaluación** | Dona de confianza, cobertura por rutina, y la distinción visible entre confirmada y candidata descartada. |
| **5. Uso del conocimiento** | Panel de alerta de ausencia, lista de recomendaciones prescriptivas, y una tarjeta que documente los endpoints disponibles para la app móvil. |

Además, en el encabezado: la fuente (Atlas), la hora de la última corrida, un botón
**"Actualizar ahora"** que pegue a `POST /api/refrescar`, y el aviso de datos obsoletos si aplica.

Chart.js y `chartjs-chart-matrix` servidos desde `pantalla/vendor/`, no desde CDN.

---

## 4. Verificación — no me digas que está listo hasta que

- [ ] `python datos/sembrar_atlas.py --limpiar` corre y reporta el conteo de las tres capas
      (claros, difíciles y basura) por separado
- [ ] En Atlas hay documentos de las **tres** capas, no solo de los claros: verifícalo con un
      conteo por `origen` / capa y pégamelo
- [ ] `python consumidor/consumir_mongo.py` lee de Atlas y su salida de consola coincide con lo
      sembrado
- [ ] `python consumidor/servidor.py` levanta y `/api/salud` responde `ok: true`
- [ ] Los seis endpoints devuelven JSON válido; `curl` de cada uno pegado en tu reporte
- [ ] El tablero abre en el navegador con las cinco secciones en orden y sin errores en consola
- [ ] "Actualizar ahora" recalcula y cambia la hora de última corrida
- [ ] Apagando el wifi, el tablero sigue mostrando el último resultado marcado como obsoleto
- [ ] `git grep -i "mongodb+srv"` **no devuelve nada** en archivos versionados
- [ ] `archivo/` existe y `pantalla/index.html` es el de Chart.js

En los datos sembrados deben verse: la rutina de sala separándose entre semana / fin de semana,
la que cruza medianoche ubicada en ~00:00 (no en las 12:00), el dispositivo con dos rutinas, la
candidata sin confirmar, y la alerta de ausencia.

---

## 5. Cómo quiero que trabajes

- **Plan primero.** Antes de escribir código, dame el plan y espera mi visto bueno.
- **Commits por etapa**, en este orden, para que si algo se rompe pueda volver atrás:
  1. limpieza del repo (`archivo/`)
  2. script de siembra
  3. consumidor solo-Atlas
  4. API REST + capa prescriptiva
  5. tablero reorganizado por fases
  6. README y documentación del pipeline móvil
- Cada commit debe dejar el repo en estado funcional.
- Si algo te bloquea más de dos intentos, **para y pregúntame**. Mañana hay presentación; prefiero
  decidir yo que descubrir un atajo tuyo a media exposición.
- Comentarios en español, explicando el porqué. Parte de este código va fotografiado en una
  entrega escolar.

## 6. Al terminar, dame

1. El árbol de archivos final
2. La salida real de `sembrar_atlas.py` y de `consumir_mongo.py`
3. Un `curl` de ejemplo por endpoint con su respuesta recortada
4. La lista de commits
5. Cualquier diferencia entre este documento y el repo real que hayas encontrado
6. **Un guion de demo de 5 minutos**: qué abrir, en qué orden, y qué decir en cada una de las
   cinco fases de la rúbrica
