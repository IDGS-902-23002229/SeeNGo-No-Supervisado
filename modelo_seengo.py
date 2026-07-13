import pandas as pd
import numpy as np
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler
import matplotlib.pyplot as plt

# 1. Simulación de datos extraídos de MongoDB
# Variables: Hora del día, luminosidad (luxes), consumo (Wh), confianza MediaPipe
np.random.seed(42)
datos_telemetria = {
    "hora_dia": np.random.randint(0, 24, 300),
    "luminosidad_luxes": np.random.randint(10, 800, 300),
    "consumo_wh": np.random.randint(5, 100, 300),
    "confianza_ia": np.random.uniform(0.75, 0.99, 300)
}
df_seengo = pd.DataFrame(datos_telemetria)
X = df_seengo.values

# 2. OPTIMIZACIÓN CRÍTICA: Estandarización de los datos
# (Si no hacemos esto, la luminosidad domina el modelo porque sus números son más grandes)
scaler = StandardScaler()
X_scaled = scaler.fit_transform(X)

# 3. Modelo de Agrupación (K-Means)
# Buscamos 2 perfiles de uso (Ej. "Uso Eficiente" vs "Alto Consumo")
kmeans = KMeans(n_clusters=2, random_state=42)
df_seengo["cluster_asignado"] = kmeans.fit_predict(X_scaled)

print("Entrenamiento completado. Clústeres asignados con éxito.")

# 4. Reducción de Dimensionalidad (PCA)
# Comprimimos las 4 dimensiones a 2 para poder graficarlas en un plano
pca = PCA(n_components=2)
X_pca = pca.fit_transform(X_scaled)

# 5. Visualización del Modelo SeeNGo
plt.figure(figsize=(8, 6))
scatter = plt.scatter(X_pca[:, 0], X_pca[:, 1], c=df_seengo["cluster_asignado"], cmap="coolwarm", s=60, edgecolors='k')
plt.xlabel("Componente Principal 1 (Varianza comprimida)")
plt.ylabel("Componente Principal 2 (Varianza comprimida)")
plt.title("Clustering PCA - Perfiles de Uso SeeNGo (Datos Normalizados)")
plt.colorbar(scatter, label="ID del Clúster (Perfil de Uso)")
plt.grid(True, linestyle='--', alpha=0.6)
plt.show()