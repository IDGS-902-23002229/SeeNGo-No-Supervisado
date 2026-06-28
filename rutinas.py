import pandas as pd
import numpy as np
from sklearn.cluster import DBSCAN
import matplotlib.pyplot as plt

# ==========================================
# 1. SIMULACIÓN DE DATOS (2-3 Semanas de MongoDB)
# ==========================================
np.random.seed(42)

# Simulamos historial para poder cumplir tu regla de 9 días y 4 días
# Hábito real: Entre semana prende a las ~19:30. 
# Hábito de fin de semana: prende a las ~21:00.
# Hábito de AUSENCIA: El usuario trabaja y la casa está sola de 08:00 a 16:00 todos los días.
datos = [
    # --- SEMANA 1 ---
    {"dia_semana": 0, "hora_str": "19:25"}, {"dia_semana": 1, "hora_str": "19:40"},
    {"dia_semana": 2, "hora_str": "19:15"}, {"dia_semana": 3, "hora_str": "14:00"}, # Jueves roto
    {"dia_semana": 4, "hora_str": "19:50"}, {"dia_semana": 5, "hora_str": "21:10"},
    {"dia_semana": 6, "hora_str": "20:55"},
    # --- SEMANA 2 ---
    {"dia_semana": 0, "hora_str": "19:35"}, {"dia_semana": 1, "hora_str": "19:20"},
    {"dia_semana": 2, "hora_str": "19:30"}, {"dia_semana": 3, "hora_str": "19:45"},
    {"dia_semana": 4, "hora_str": "20:00"}, {"dia_semana": 5, "hora_str": "21:30"},
    {"dia_semana": 6, "hora_str": "03:00"}, # Domingo loco
    # --- SEMANA 3 ---
    {"dia_semana": 0, "hora_str": "19:20"}, {"dia_semana": 1, "hora_str": "19:10"},
    {"dia_semana": 2, "hora_str": "19:25"}, {"dia_semana": 3, "hora_str": "19:35"},
    {"dia_semana": 4, "hora_str": "08:15"}, # Mañana rara
    {"dia_semana": 5, "hora_str": "21:05"}, {"dia_semana": 6, "hora_str": "21:15"}
]

df = pd.DataFrame(datos)

# Convertir hora a decimal
def time_to_decimal(time_str):
    h, m = map(int, time_str.split(':'))
    return h + m / 60.0

df['hora_decimal'] = df['hora_str'].apply(time_to_decimal)

# ==========================================
# 2. TRANSFORMACIÓN CÍCLICA
# ==========================================
df['hora_sin'] = np.sin(2 * np.pi * df['hora_decimal'] / 24)
df['hora_cos'] = np.cos(2 * np.pi * df['hora_decimal'] / 24)

df_semana = df[df['dia_semana'] <= 4].copy()
df_finde = df[df['dia_semana'] > 4].copy()

# ==========================================
# 3. CAPA 1 y 2: REGLAS ESTRICTAS DE RUTINA
# ==========================================
# eps=0.26 (Rango de 1 hora)
# min_samples=9 (Para entre semana) y min_samples=4 (Para fines)
dbscan_semana = DBSCAN(eps=0.26, min_samples=9)
df_semana['cluster'] = dbscan_semana.fit_predict(df_semana[['hora_sin', 'hora_cos']])

dbscan_finde = DBSCAN(eps=0.26, min_samples=4)
df_finde['cluster'] = dbscan_finde.fit_predict(df_finde[['hora_sin', 'hora_cos']])

def analizar_rutinas(df_analisis, contexto):
    rutinas = df_analisis[df_analisis['cluster'] != -1]
    anomalias = df_analisis[df_analisis['cluster'] == -1]
    
    print(f"\n--- Resultados para {contexto} ---")
    if not rutinas.empty:
        hora_promedio = rutinas['hora_decimal'].mean()
        horas = int(hora_promedio)
        minutos = int((hora_promedio - horas) * 60)
        print(f"✅ Rutina Confirmada: Luces encendidas a las {horas:02d}:{minutos:02d}.")
        print(f"   Días consistentes: {len(rutinas)}")
        print(f"   Días 'rotos' perdonados por el modelo: {len(anomalias)}")
    else:
        print("❌ Aún no se alcanzan los días requeridos para confirmar una rutina.")

analizar_rutinas(df_semana, "Entre Semana")
analizar_rutinas(df_finde, "Fines de Semana")

# ==========================================
# 4. CAPA 4: RUTINA "MODO AUSENTE" (Sin actividad 4-6 hrs en 14 días)
# ==========================================
print("\n--- Capa 4: Monitoreo Histórico de Ausencia (Modo Fuera de Casa) ---")

# Buscamos huecos recurrentes de inactividad en el historial
# Ordenamos todas las interacciones del día de menor a mayor
horas_ordenadas = np.sort(df['hora_decimal'].values)

# Añadimos la primera hora sumándole 24 para calcular la distancia que cruza la medianoche
horas_circulares = np.append(horas_ordenadas, horas_ordenadas[0] + 24)

# Calculamos la diferencia entre cada evento
diferencias = np.diff(horas_circulares)

# Buscamos la brecha de tiempo más grande sin interacciones en los últimos 14 días
brecha_maxima = np.max(diferencias)
indice_brecha = np.argmax(diferencias)

# Verificamos si esa brecha cumple tu regla de ser entre 4 y 6 horas (o más)
if brecha_maxima >= 5.0:  # Tomamos 5 horas como tu punto medio entre 4 y 6
    hora_inicio_ausencia = horas_ordenadas[indice_brecha]
    hora_fin_ausencia = hora_inicio_ausencia + brecha_maxima
    
    # Ajustar formato si cruza la medianoche
    if hora_fin_ausencia >= 24:
        hora_fin_ausencia -= 24
        
    inicio_str = f"{int(hora_inicio_ausencia):02d}:{int((hora_inicio_ausencia % 1) * 60):02d}"
    fin_str = f"{int(hora_fin_ausencia):02d}:{int((hora_fin_ausencia % 1) * 60):02d}"
    
    print(f"⚠️ PATRÓN DE AUSENCIA DETECTADO:")
    print(f"   El sistema notó que durante los últimos 14 días, NUNCA hay actividad en la casa")
    print(f"   por una ventana de {brecha_maxima:.1f} horas seguidas (Entre las {inicio_str} y las {fin_str}).")
    print(f"📲 SUGERENCIA A APP: 'Notamos que no usas el sistema entre {inicio_str} y {fin_str}. ¿Quieres que aseguremos el apagado de luces y standby de TV en ese horario para ahorrar luz?'")
else:
    print("🏠 La casa tiene actividad dispersa, no hay una ventana clara de 5+ horas de ausencia.")
