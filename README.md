# 📊 Obrail — Monitoring & Observabilité

Stack de monitoring complète pour le projet **Obrail**, une plateforme de données ferroviaires. Ce repository contient la configuration Prometheus et Promtail pour la collecte des métriques et des logs.

---

## 🧱 Stack technique

| Outil | Rôle | Port |
|-------|------|------|
| **Prometheus** | Collecte des métriques toutes les 15s | 9090 |
| **Grafana** | Visualisation des dashboards | 3000 |
| **cAdvisor** | Métriques des conteneurs Docker | 8080 |
| **postgres_exporter** | Métriques PostgreSQL | 9187 |
| **prometheus-fastapi-instrumentator** | Métriques HTTP de l'API | 8000 |
| **Promtail** | Collecte des logs applicatifs | — |

---

## 📁 Structure du repository

```
grafana/
├── prometheus.yml        # Configuration des cibles Prometheus
├── promtail-config.yml   # Configuration Promtail (logs → Loki)
└── README.md
```

---

## 🚀 Lancement

### Prérequis

- Docker Desktop installé et démarré
- L'API Obrail qui tourne sur le port `8000`
- PostgreSQL qui tourne sur le port `5434`

### 1. Lancer cAdvisor

```powershell
docker run -d --name cadvisor -p 8080:8080 `
  --volume=/:/rootfs:ro `
  --volume=/var/run:/var/run:ro `
  --volume=/sys:/sys:ro `
  --volume=/var/lib/docker/:/var/lib/docker:ro `
  gcr.io/cadvisor/cadvisor:latest
```

### 2. Lancer postgres_exporter

```powershell
docker run -d `
  --name postgres_exporter `
  -p 9187:9187 `
  -e DATA_SOURCE_NAME="postgresql://obrail_user:obrail_pass@host.docker.internal:5434/obrail?sslmode=disable" `
  prometheuscommunity/postgres-exporter
```

### 3. Lancer Prometheus

```powershell
docker run -d --name prometheus -p 9090:9090 `
  -v ${PWD}/prometheus.yml:/etc/prometheus/prometheus.yml `
  prom/prometheus
```

### 4. Lancer Grafana

```powershell
docker run -d --name grafana -p 3000:3000 grafana/grafana
```

---

## ⚙️ Configuration Prometheus

Le fichier `prometheus.yml` définit trois cibles surveillées :

| Job | Cible | Description |
|-----|-------|-------------|
| `obrail-api` | `host.docker.internal:8000` | API FastAPI — métriques HTTP |
| `cadvisor` | `host.docker.internal:8080` | Conteneurs Docker — CPU, RAM, réseau |
| `postgres` | `host.docker.internal:9187` | Base de données PostgreSQL |

L'intervalle de collecte est de **15 secondes**.

---

## 📈 Dashboards Grafana

Quatre dashboards ont été configurés manuellement dans Grafana :

### 🔵 Dashboard API
- Latence p50 / p95 / p99
- Taux de requêtes par seconde par endpoint
- Taux d'erreurs 5xx
- Requêtes en cours de traitement

### 🟠 Dashboard Infrastructure Docker
- CPU total des conteneurs
- Mémoire utilisée vs disponible
- Trafic réseau entrant/sortant
- Métriques par conteneur

### 🟣 Dashboard PostgreSQL
- Statut de la base (UP/DOWN)
- Connexions actives
- Cache hit ratio
- Transactions par seconde
- Taille des tables métier (`gare`, `ligne`, `trajet`, `localisation`, `operateur`)
- Locks actifs et WAL

### 🟢 Dashboard Métier
- Consultations par endpoint sur 24h
- Répartition des pages (donut chart)
- Fréquence d'usage en temps réel
- Classement des pages par popularité

---

## 📝 Logging applicatif

Le logging est configuré dans `main.py` via le module Python `logging` :

```python
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),       # affiche dans le terminal
        logging.FileHandler("app.log") # écrit dans app.log
    ]
)
```

Les logs sont écrits simultanément dans le **terminal** et dans le fichier **`app.log`**.

Le fichier `promtail-config.yml` est prévu pour envoyer ces logs vers **Loki** afin de les visualiser dans Grafana.

---

## 🔴 Alertes configurées

| Alerte | Seuil | Sévérité |
|--------|-------|----------|
| Latence p95 API | > 500ms / 5 min | ⚠️ Warning |
| Taux d'erreurs 5xx | > 1% / 2 min | 🔴 Critical |
| Endpoint /health indisponible | 3 échecs / 3 min | 🔴 Critical |
| CPU conteneur API | > 80% / 10 min | ⚠️ Warning |
| Mémoire conteneur API | > 90% | 🔴 Critical |
| Connexions PostgreSQL | > 90% du pool / 5 min | ⚠️ Warning |
| Transactions idle in transaction | > 20 / 5 min | ⚠️ Warning |
| Échec import quotidien | 0 insertion / 24h | 🔴 Critical |

---

