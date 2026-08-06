# archivo/ — iteraciones previas

Esta carpeta guarda versiones anteriores del proyecto que **ya no están en uso**.
No son código vivo ni se ejecutan en la entrega: se conservan como evidencia del
proceso de desarrollo, porque muestran cómo evolucionó el modelo desde un
prototipo con librerías pesadas hasta el detector actual, que corre con la sola
librería estándar de Python en una Raspberry Pi. Se movieron con `git mv`, así
que su historial completo sigue disponible con `git log --follow <archivo>`.

Qué es cada cosa:

- **`rutinas.py`** — el primer prototipo del detector (v1).
- **`rutinas-v2.py`** — la segunda iteración, con DBSCAN de scikit-learn y pandas
  sobre un calendario de 3 semanas simuladas. Su documentación interna explica los
  cinco problemas que llevaron a reescribir el modelo (media circular, un cluster
  por rutina, separar la regla de negocio de la densidad, cobertura de toda la
  semana y el rediseño de la ausencia). Es la mejor referencia del *porqué* del
  diseño actual.
- **`modelo_seengo.py`** — un experimento aparte con K-Means y PCA sobre telemetría
  simulada. Se descartó: agrupaba perfiles de consumo, no rutinas horarias, y
  requería `numpy`, `pandas`, `scikit-learn` y `matplotlib`.
- **`pantalla-svg/index.html`** — el primer tablero, dibujado a mano con SVG y
  JavaScript vanilla. Lo reemplazó `pantalla/index.html` (Chart.js), que organiza
  las gráficas según las cinco fases de la extracción de conocimiento.
- **`sugerencias_ejemplo.json`** — respuestas de aceptación de demostración,
  usadas cuando el tablero todavía tenía una segunda fuente de datos "local".
  Al quedar Atlas como fuente única, las recomendaciones y sus respuestas viven
  en la colección `sugerencias` de Mongo y este archivo dejó de leerse.

El código en uso vive en `modelo/`, `consumidor/`, `datos/` y `pantalla/`.
