"""
SEENGO · modelo — detector de rutinas del hogar
===============================================
API pública del paquete. Dos formas de usarlo desde otra aplicación:

1) PURA, sin red — le pasas tú los eventos (no requiere pymongo):

       from modelo import analizar
       resultado = analizar(eventos)          # eventos = lista de dicts

2) CONTRA ATLAS — el paquete lee la base y analiza:

       from modelo import analizar_desde_atlas
       resultado = analizar_desde_atlas(os.environ["MONGO_URI"])

Y la capa prescriptiva, que convierte las rutinas en acciones sugeridas:

       from modelo import generar_recomendaciones
       recomendaciones = generar_recomendaciones(resultado)

`detector_rutinas` no importa pymongo ni toca disco: esa pureza es lo que
permite correrlo en una Raspberry Pi y lo que hace seguro importarlo desde
cualquier backend. `analizar_desde_atlas` sí necesita pymongo, pero lo
importa de forma perezosa, así que la opción 1 funciona aunque pymongo no
esté instalado.
"""
import os
import sys

# El paquete se importa tanto como `modelo.x` (paquete instalado) como con
# `sys.path.insert(...,"modelo")` desde consumidor/. Añadir esta carpeta al
# path hace que ambas formas funcionen sin duplicar código.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from detector_rutinas import (  # noqa: E402
    analizar, CONFIG, dbscan_circular, mapa_semanal, ausencia_larga,
)
from recomendaciones import (  # noqa: E402
    generar_recomendaciones, resumen_aceptacion,
)


def analizar_desde_atlas(uri, db="seengo", coll="sign_events", dias=90):
    """Lee MongoDB Atlas y devuelve el análisis completo.

    Se importa aquí dentro (no arriba) para que `from modelo import analizar`
    no exija tener pymongo instalado.
    """
    from fuente_atlas import analizar_desde_atlas as _impl
    return _impl(uri, db, coll, dias)


__all__ = [
    "analizar", "analizar_desde_atlas", "generar_recomendaciones",
    "resumen_aceptacion", "CONFIG", "dbscan_circular", "mapa_semanal",
    "ausencia_larga",
]
