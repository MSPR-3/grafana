from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from math import ceil
from typing import Any, Optional

import databases
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

from prometheus_client import REGISTRY
from prometheus_fastapi_instrumentator import Instrumentator, metrics

# Logging setup
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
database = databases.Database(DATABASE_URL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await database.connect()
        logger.info("PostgreSQL connection established")
    except Exception as exc:
        logger.error("PostgreSQL connection error: %s", exc)
        raise
    yield
    await database.disconnect()
    logger.info("PostgreSQL connection closed")


def to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def normalize_type_train(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.lower().strip()
    if v not in {"jour", "nuit"}:
        raise HTTPException(status_code=400, detail="type doit être 'jour' ou 'nuit'")
    return v


def parse_bbox(value: Optional[str]) -> Optional[tuple[float, float, float, float]]:
    if value is None:
        return None
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 4:
        raise HTTPException(status_code=400, detail="bbox doit être 'lat_min,lng_min,lat_max,lng_max'")
    try:
        lat_min, lng_min, lat_max, lng_max = [float(p) for p in parts]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="bbox invalide") from exc
    if lat_min > lat_max or lng_min > lng_max:
        raise HTTPException(status_code=400, detail="bbox invalide: min > max")
    return lat_min, lng_min, lat_max, lng_max


def normalize_statut(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    raw = value.strip().lower()
    mapping = {
        "succes": "succès",
        "succès": "succès",
        "echec": "échec",
        "échec": "échec",
        "partiel": "partiel",
    }
    if raw not in mapping:
        raise HTTPException(status_code=400, detail="statut invalide : succès | échec | partiel")
    return mapping[raw]


def normalize_groupby(value: str) -> str:
    valid = {"operateur", "ligne", "pays_depart", "pays_arrivee", "jour_nuit"}
    if value not in valid:
        raise HTTPException(status_code=400, detail="groupby invalide")
    return value


app = FastAPI(
    title="ObRail Europe API",
    description="API REST adaptée à la base ObRail Europe !",
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("app.log")
    ]
)
logger = logging.getLogger(__name__)
Instrumentator(
    should_group_status_codes=False,
    should_ignore_untemplated=True,
    should_respect_env_var=False,
    should_instrument_requests_inprogress=True,
    excluded_handlers=[],
    body_handlers=[],
    env_var_name="ENABLE_METRICS",
    inprogress_labels=True,
).instrument(app).expose(app, include_in_schema=False)

@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=404,
        content={"code": 404, "message": "Ressource non trouvée", "detail": exc.detail},
    )


@app.exception_handler(422)
async def validation_error_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=422,
        content={"code": 422, "message": "Paramètre invalide", "detail": "Vérifiez les paramètres"},
    )


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc: Exception):
    logger.error("500 - %s - %s", request.url, exc)
    return JSONResponse(
        status_code=500,
        content={"code": 500, "message": "Erreur serveur interne", "detail": "Une erreur inattendue s'est produite"},
    )


@app.get("/", tags=["Racine"])
async def root():
    return {
        "api": "ObRail Europe",
        "version": "3.0.0",
        "documentation": "/docs",
        "base_url": "/v1",
        "endpoints": [
            "/trajets",
            "/trajets/{id_trajet}",
            "/stats/kpi",
            "/stats/volumes",
            "/stats/comparatif-jour-nuit",
            "/stats/co2",
            "/stats/top-liaisons",
            "/operateurs",
            "/lignes",
            "/gares",
            "/pays",
            "/imports",
            "/imports/stats",
            "/health",
        ],
    }


@app.get("/health", tags=["System"])
async def health():
    db_status = "ok"
    try:
        await database.fetch_val("SELECT 1")
    except Exception:
        db_status = "error"
    return {
        "status": "ok" if db_status == "ok" else "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "version": "1.0.0",
        "components": {"db": db_status},
    }


@app.get("/trajets", tags=["Trajets"])
async def get_trajets(
    page: int = Query(1, ge=1),
    limit: int = Query(15, ge=1, le=100),
    id_operateur: Optional[str] = Query(None),
    id_ligne: Optional[str] = Query(None),
    id_gare_depart: Optional[str] = Query(None),
    id_gare_arrivee: Optional[str] = Query(None),
    code_pays_depart: Optional[str] = Query(None),
    code_pays_arrivee: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    co2_max: Optional[float] = Query(None, ge=0),
    duree_min: Optional[int] = Query(None, ge=0),
    duree_max: Optional[int] = Query(None, ge=0),
    search: Optional[str] = Query(None),
):
    type_normalized = normalize_type_train(type)
    offset = (page - 1) * limit

    conditions: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if id_operateur:
        conditions.append("t.id_operateur = :id_operateur")
        params["id_operateur"] = id_operateur
    if id_ligne:
        conditions.append("t.id_ligne = :id_ligne")
        params["id_ligne"] = id_ligne
    if id_gare_depart:
        conditions.append("t.id_gare_depart = :id_gare_depart")
        params["id_gare_depart"] = id_gare_depart
    if id_gare_arrivee:
        conditions.append("t.id_gare_arrivee = :id_gare_arrivee")
        params["id_gare_arrivee"] = id_gare_arrivee
    if code_pays_depart:
        conditions.append("gd.code_pays = :code_pays_depart")
        params["code_pays_depart"] = code_pays_depart.upper()
    if code_pays_arrivee:
        conditions.append("ga.code_pays = :code_pays_arrivee")
        params["code_pays_arrivee"] = code_pays_arrivee.upper()
    if co2_max is not None:
        conditions.append("t.emission_co2_kg <= :co2_max")
        params["co2_max"] = co2_max
    if duree_min is not None:
        conditions.append("t.duree_minutes >= :duree_min")
        params["duree_min"] = duree_min
    if duree_max is not None:
        conditions.append("t.duree_minutes <= :duree_max")
        params["duree_max"] = duree_max
    if type_normalized:
        conditions.append("CASE WHEN t.heure_depart::time < TIME '18:00:00' THEN 'jour' ELSE 'nuit' END = :type_train")
        params["type_train"] = type_normalized
    if search:
        conditions.append(
            "(" 
            "t.id_trajet ILIKE :search OR "
            "gd.nom_officiel ILIKE :search OR "
            "ga.nom_officiel ILIKE :search OR "
            "o.nom ILIKE :search"
            ")"
        )
        params["search"] = f"%{search}%"

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    count_query = f"""
        SELECT COUNT(*)
        FROM trajet t
        JOIN gare gd ON t.id_gare_depart = gd.id_gare
        JOIN gare ga ON t.id_gare_arrivee = ga.id_gare
        LEFT JOIN operateur o ON t.id_operateur = o.id_operateur
        {where}
    """

    count_params = {k: v for k, v in params.items() if k not in {"limit", "offset"}}

    data_query = f"""
        SELECT
            t.id_trajet,
            t.id_service,
            t.heure_depart,
            t.heure_arrivee,
            t.duree_minutes,
            t.emission_co2_kg,
            l.id_ligne,
            l.nom_ligne,
            o.id_operateur,
            o.nom AS operateur_nom,
            gd.id_gare AS depart_id_gare,
            gd.nom_officiel AS depart_nom,
            gd.ville AS depart_ville,
            gd.code_pays AS depart_code_pays,
            ga.id_gare AS arrivee_id_gare,
            ga.nom_officiel AS arrivee_nom,
            ga.ville AS arrivee_ville,
            ga.code_pays AS arrivee_code_pays
        FROM trajet t
        LEFT JOIN ligne l ON t.id_ligne = l.id_ligne
        LEFT JOIN operateur o ON t.id_operateur = o.id_operateur
        JOIN gare gd ON t.id_gare_depart = gd.id_gare
        JOIN gare ga ON t.id_gare_arrivee = ga.id_gare
        {where}
        ORDER BY t.id_trajet
        LIMIT :limit OFFSET :offset
    """

    try:
        total = await database.fetch_val(count_query, count_params)
        rows = await database.fetch_all(data_query, params)
    except Exception as exc:
        logger.error("Erreur GET /trajets : %s", exc)
        raise HTTPException(status_code=500, detail="Erreur récupération trajets") from exc

    results = []
    for row in rows:
        type_calcul = "jour" if str(row["heure_depart"]) < "18:00:00" else "nuit"
        results.append(
            {
                "id_trajet": row["id_trajet"],
                "id_service": row["id_service"],
                "depart": {
                    "id_gare": row["depart_id_gare"],
                    "nom": row["depart_nom"],
                    "ville": row["depart_ville"],
                    "code_pays": row["depart_code_pays"],
                },
                "arrivee": {
                    "id_gare": row["arrivee_id_gare"],
                    "nom": row["arrivee_nom"],
                    "ville": row["arrivee_ville"],
                    "code_pays": row["arrivee_code_pays"],
                },
                "heure_depart": str(row["heure_depart"]),
                "heure_arrivee": str(row["heure_arrivee"]),
                "duree_minutes": row["duree_minutes"],
                "type_calcul": type_calcul,
                "emission_co2_kg": to_float(row["emission_co2_kg"]),
                "ligne": {"id_ligne": row["id_ligne"], "nom_ligne": row["nom_ligne"]},
                "operateur": {"id_operateur": row["id_operateur"], "nom": row["operateur_nom"]},
            }
        )

    total_int = int(total or 0)
    return {
        "page": page,
        "limit": limit,
        "total": total_int,
        "total_pages": ceil(total_int / limit) if total_int else 0,
        "results": results,
    }


@app.get("/trajets/{id_trajet}", tags=["Trajets"])
async def get_trajet(id_trajet: str):
    query = """
        SELECT
            t.id_trajet,
            t.id_service,
            t.id_trajet_source,
            t.heure_depart,
            t.heure_arrivee,
            t.duree_minutes,
            t.emission_co2_kg,
            l.id_ligne,
            l.nom_ligne,
            o.id_operateur,
            o.nom AS operateur_nom,
            gd.id_gare AS depart_id_gare,
            gd.nom_officiel AS depart_nom,
            gd.ville AS depart_ville,
            gd.code_pays AS depart_code_pays,
            ld.nom_pays AS depart_pays,
            gd.type_liaison AS depart_type_liaison,
            gd.latitude AS depart_lat,
            gd.longitude AS depart_lng,
            ga.id_gare AS arrivee_id_gare,
            ga.nom_officiel AS arrivee_nom,
            ga.ville AS arrivee_ville,
            ga.code_pays AS arrivee_code_pays,
            la.nom_pays AS arrivee_pays,
            ga.type_liaison AS arrivee_type_liaison,
            ga.latitude AS arrivee_lat,
            ga.longitude AS arrivee_lng
        FROM trajet t
        LEFT JOIN ligne l ON t.id_ligne = l.id_ligne
        LEFT JOIN operateur o ON t.id_operateur = o.id_operateur
        LEFT JOIN gare gd ON t.id_gare_depart = gd.id_gare
        LEFT JOIN gare ga ON t.id_gare_arrivee = ga.id_gare
        LEFT JOIN localisation ld ON gd.code_pays = ld.code_pays AND gd.ville = ld.ville
        LEFT JOIN localisation la ON ga.code_pays = la.code_pays AND ga.ville = la.ville
        WHERE t.id_trajet = :id_trajet
    """
    row = await database.fetch_one(query, {"id_trajet": id_trajet})
    if not row:
        raise HTTPException(status_code=404, detail=f"Trajet {id_trajet} non trouvé")

    type_calcul = "jour" if str(row["heure_depart"]) < "18:00:00" else "nuit"
    return {
        "id_trajet": row["id_trajet"],
        "id_service": row["id_service"],
        "id_trajet_source": row["id_trajet_source"],
        "depart": {
            "id_gare": row["depart_id_gare"],
            "nom_officiel": row["depart_nom"],
            "ville": row["depart_ville"],
            "code_pays": row["depart_code_pays"],
            "pays": row["depart_pays"],
            "type_liaison": row["depart_type_liaison"],
            "latitude": to_float(row["depart_lat"]),
            "longitude": to_float(row["depart_lng"]),
        },
        "arrivee": {
            "id_gare": row["arrivee_id_gare"],
            "nom_officiel": row["arrivee_nom"],
            "ville": row["arrivee_ville"],
            "code_pays": row["arrivee_code_pays"],
            "pays": row["arrivee_pays"],
            "type_liaison": row["arrivee_type_liaison"],
            "latitude": to_float(row["arrivee_lat"]),
            "longitude": to_float(row["arrivee_lng"]),
        },
        "heure_depart": str(row["heure_depart"]),
        "heure_arrivee": str(row["heure_arrivee"]),
        "duree_minutes": row["duree_minutes"],
        "type_calcul": type_calcul,
        "emission_co2_kg": to_float(row["emission_co2_kg"]),
        "ligne": {"id_ligne": row["id_ligne"], "nom_ligne": row["nom_ligne"]},
        "operateur": {"id_operateur": row["id_operateur"], "nom": row["operateur_nom"]},
    }


@app.get("/stats/kpi", tags=["Stats"])
async def get_stats_kpi():
    query = """
        SELECT
            (SELECT COUNT(*) FROM trajet) AS total_trajets,
            (SELECT COUNT(*) FROM trajet WHERE heure_depart::time < TIME '18:00:00') AS trajets_jour,
            (SELECT COUNT(*) FROM trajet WHERE heure_depart::time >= TIME '18:00:00') AS trajets_nuit,
            (SELECT COUNT(*) FROM operateur) AS total_operateurs,
            (SELECT COUNT(*) FROM ligne) AS total_lignes,
            (SELECT COUNT(*) FROM gare) AS total_gares,
            (SELECT COUNT(DISTINCT code_pays) FROM gare) AS total_pays,
            (SELECT COALESCE(SUM(emission_co2_kg), 0) FROM trajet) AS co2_total_kg,
            (SELECT COALESCE(AVG(emission_co2_kg), 0) FROM trajet) AS co2_moyen_kg,
            (SELECT COALESCE(AVG(duree_minutes), 0) FROM trajet) AS duree_moyenne_minutes
    """
    row = await database.fetch_one(query)
    return {
        "total_trajets": int(row["total_trajets"]),
        "trajets_jour": int(row["trajets_jour"]),
        "trajets_nuit": int(row["trajets_nuit"]),
        "total_operateurs": int(row["total_operateurs"]),
        "total_lignes": int(row["total_lignes"]),
        "total_gares": int(row["total_gares"]),
        "total_pays": int(row["total_pays"]),
        "co2_total_kg": to_float(row["co2_total_kg"]),
        "co2_moyen_kg": to_float(row["co2_moyen_kg"]),
        "duree_moyenne_minutes": round(to_float(row["duree_moyenne_minutes"]) or 0),
    }


@app.get("/stats/volumes", tags=["Stats"])
async def get_stats_volumes(groupby: str = Query("operateur")):
    groupby = normalize_groupby(groupby)

    if groupby == "operateur":
        rows = await database.fetch_all(
            """
            SELECT o.id_operateur, o.nom,
                   COUNT(t.id_trajet) AS trajets,
                   COALESCE(SUM(t.emission_co2_kg), 0) AS co2_total_kg
            FROM trajet t
            JOIN operateur o ON t.id_operateur = o.id_operateur
            GROUP BY o.id_operateur, o.nom
            ORDER BY trajets DESC
            """
        )
        total = sum(int(r["trajets"]) for r in rows)
        repartition = [
            {
                "id_operateur": r["id_operateur"],
                "nom": r["nom"],
                "trajets": int(r["trajets"]),
                "co2_total_kg": to_float(r["co2_total_kg"]),
                "part": round(int(r["trajets"]) / total, 3) if total else 0,
            }
            for r in rows
        ]
        return {"groupby": groupby, "total": total, "repartition": repartition}

    if groupby == "ligne":
        rows = await database.fetch_all(
            """
            SELECT l.id_ligne, l.nom_ligne,
                   COUNT(t.id_trajet) AS trajets,
                   COALESCE(SUM(t.emission_co2_kg), 0) AS co2_total_kg
            FROM trajet t
            JOIN ligne l ON t.id_ligne = l.id_ligne
            GROUP BY l.id_ligne, l.nom_ligne
            ORDER BY trajets DESC
            """
        )
        total = sum(int(r["trajets"]) for r in rows)
        return {
            "groupby": groupby,
            "total": total,
            "repartition": [
                {
                    "id_ligne": r["id_ligne"],
                    "nom_ligne": r["nom_ligne"],
                    "trajets": int(r["trajets"]),
                    "co2_total_kg": to_float(r["co2_total_kg"]),
                    "part": round(int(r["trajets"]) / total, 3) if total else 0,
                }
                for r in rows
            ],
        }

    if groupby == "pays_depart":
        rows = await database.fetch_all(
            """
            SELECT gd.code_pays,
                   COUNT(t.id_trajet) AS trajets,
                   COALESCE(SUM(t.emission_co2_kg), 0) AS co2_total_kg
            FROM trajet t
            JOIN gare gd ON t.id_gare_depart = gd.id_gare
            GROUP BY gd.code_pays
            ORDER BY trajets DESC
            """
        )
        total = sum(int(r["trajets"]) for r in rows)
        return {
            "groupby": groupby,
            "total": total,
            "repartition": [
                {
                    "code_pays": r["code_pays"],
                    "trajets": int(r["trajets"]),
                    "co2_total_kg": to_float(r["co2_total_kg"]),
                    "part": round(int(r["trajets"]) / total, 3) if total else 0,
                }
                for r in rows
            ],
        }

    if groupby == "pays_arrivee":
        rows = await database.fetch_all(
            """
            SELECT ga.code_pays,
                   COUNT(t.id_trajet) AS trajets,
                   COALESCE(SUM(t.emission_co2_kg), 0) AS co2_total_kg
            FROM trajet t
            JOIN gare ga ON t.id_gare_arrivee = ga.id_gare
            GROUP BY ga.code_pays
            ORDER BY trajets DESC
            """
        )
        total = sum(int(r["trajets"]) for r in rows)
        return {
            "groupby": groupby,
            "total": total,
            "repartition": [
                {
                    "code_pays": r["code_pays"],
                    "trajets": int(r["trajets"]),
                    "co2_total_kg": to_float(r["co2_total_kg"]),
                    "part": round(int(r["trajets"]) / total, 3) if total else 0,
                }
                for r in rows
            ],
        }

    rows = await database.fetch_all(
        """
        SELECT CASE WHEN heure_depart::time < TIME '18:00:00' THEN 'jour' ELSE 'nuit' END AS type,
               COUNT(*) AS trajets,
               COALESCE(AVG(duree_minutes), 0) AS duree_moyenne_minutes,
               COALESCE(AVG(emission_co2_kg), 0) AS co2_moyen_kg
        FROM trajet
        GROUP BY type
        ORDER BY type
        """
    )
    total = sum(int(r["trajets"]) for r in rows)
    return {
        "groupby": "jour_nuit",
        "total": total,
        "repartition": [
            {
                "type": r["type"],
                "trajets": int(r["trajets"]),
                "duree_moyenne_minutes": round(to_float(r["duree_moyenne_minutes"]) or 0),
                "co2_moyen_kg": round(to_float(r["co2_moyen_kg"]) or 0, 2),
                "part": round(int(r["trajets"]) / total, 3) if total else 0,
            }
            for r in rows
        ],
    }


@app.get("/stats/comparatif-jour-nuit", tags=["Stats"])
async def get_stats_comparatif_jour_nuit():
    row = await database.fetch_one(
        """
        SELECT
            COUNT(*) FILTER (WHERE heure_depart::time < TIME '18:00:00') AS nb_jour,
            COUNT(*) FILTER (WHERE heure_depart::time >= TIME '18:00:00') AS nb_nuit,
            COALESCE(AVG(duree_minutes) FILTER (WHERE heure_depart::time < TIME '18:00:00'), 0) AS duree_jour,
            COALESCE(AVG(duree_minutes) FILTER (WHERE heure_depart::time >= TIME '18:00:00'), 0) AS duree_nuit,
            COALESCE(AVG(emission_co2_kg) FILTER (WHERE heure_depart::time < TIME '18:00:00'), 0) AS co2_moy_jour,
            COALESCE(AVG(emission_co2_kg) FILTER (WHERE heure_depart::time >= TIME '18:00:00'), 0) AS co2_moy_nuit,
            COALESCE(SUM(emission_co2_kg) FILTER (WHERE heure_depart::time < TIME '18:00:00'), 0) AS co2_total_jour,
            COALESCE(SUM(emission_co2_kg) FILTER (WHERE heure_depart::time >= TIME '18:00:00'), 0) AS co2_total_nuit
        FROM trajet
        """
    )
    return {
        "indicateurs": [
            {"label": "Nombre de trajets", "jour": int(row["nb_jour"]), "nuit": int(row["nb_nuit"]), "unite": ""},
            {
                "label": "Durée moyenne",
                "jour": round(to_float(row["duree_jour"]) or 0),
                "nuit": round(to_float(row["duree_nuit"]) or 0),
                "unite": "min",
            },
            {
                "label": "Émission CO₂ moyenne",
                "jour": round(to_float(row["co2_moy_jour"]) or 0, 2),
                "nuit": round(to_float(row["co2_moy_nuit"]) or 0, 2),
                "unite": "kg",
            },
            {
                "label": "Émission CO₂ totale",
                "jour": round(to_float(row["co2_total_jour"]) or 0, 2),
                "nuit": round(to_float(row["co2_total_nuit"]) or 0, 2),
                "unite": "kg",
            },
        ]
    }


@app.get("/stats/co2", tags=["Stats"])
async def get_stats_co2():
    global_row = await database.fetch_one(
        """
        SELECT
            COALESCE(SUM(emission_co2_kg), 0) AS co2_total_kg,
            COALESCE(AVG(emission_co2_kg), 0) AS co2_moyen_kg_par_trajet
        FROM trajet
        """
    )
    operateurs = await database.fetch_all(
        """
        SELECT o.id_operateur, o.nom,
               COALESCE(SUM(t.emission_co2_kg), 0) AS co2_total_kg,
               COALESCE(AVG(t.emission_co2_kg), 0) AS co2_moyen_kg
        FROM trajet t
        JOIN operateur o ON t.id_operateur = o.id_operateur
        GROUP BY o.id_operateur, o.nom
        ORDER BY co2_total_kg DESC
        """
    )
    lignes_top = await database.fetch_all(
        """
        SELECT l.id_ligne, l.nom_ligne,
               COALESCE(SUM(t.emission_co2_kg), 0) AS co2_total_kg,
               COUNT(*) AS trajets
        FROM trajet t
        JOIN ligne l ON t.id_ligne = l.id_ligne
        GROUP BY l.id_ligne, l.nom_ligne
        ORDER BY co2_total_kg DESC
        LIMIT 10
        """
    )

    return {
        "co2_total_kg": to_float(global_row["co2_total_kg"]),
        "co2_moyen_kg_par_trajet": to_float(global_row["co2_moyen_kg_par_trajet"]),
        "par_operateur": [
            {
                "id_operateur": row["id_operateur"],
                "nom": row["nom"],
                "co2_total_kg": to_float(row["co2_total_kg"]),
                "co2_moyen_kg": to_float(row["co2_moyen_kg"]),
            }
            for row in operateurs
        ],
        "par_ligne_top10": [
            {
                "id_ligne": row["id_ligne"],
                "nom_ligne": row["nom_ligne"],
                "co2_total_kg": to_float(row["co2_total_kg"]),
                "trajets": int(row["trajets"]),
            }
            for row in lignes_top
        ],
    }


@app.get("/stats/top-liaisons", tags=["Stats"])
async def get_stats_top_liaisons(limit: int = Query(10, ge=1, le=100)):
    rows = await database.fetch_all(
        """
        SELECT
            gd.id_gare AS depart_id_gare,
            gd.nom_officiel AS depart_nom,
            gd.ville AS depart_ville,
            gd.code_pays AS depart_code_pays,
            ga.id_gare AS arrivee_id_gare,
            ga.nom_officiel AS arrivee_nom,
            ga.ville AS arrivee_ville,
            ga.code_pays AS arrivee_code_pays,
            o.nom AS operateur,
            COUNT(*) AS trajets,
            COALESCE(AVG(t.duree_minutes), 0) AS duree_moyenne_minutes,
            CASE
                WHEN COUNT(*) FILTER (WHERE t.heure_depart::time < TIME '18:00:00') >=
                     COUNT(*) FILTER (WHERE t.heure_depart::time >= TIME '18:00:00')
                THEN 'jour'
                ELSE 'nuit'
            END AS type_dominant
        FROM trajet t
        JOIN gare gd ON t.id_gare_depart = gd.id_gare
        JOIN gare ga ON t.id_gare_arrivee = ga.id_gare
        LEFT JOIN operateur o ON t.id_operateur = o.id_operateur
        GROUP BY
            gd.id_gare, gd.nom_officiel, gd.ville, gd.code_pays,
            ga.id_gare, ga.nom_officiel, ga.ville, ga.code_pays,
            o.nom
        ORDER BY trajets DESC
        LIMIT :limit
        """,
        {"limit": limit},
    )
    return {
        "results": [
            {
                "depart": {
                    "id_gare": row["depart_id_gare"],
                    "nom": row["depart_nom"],
                    "ville": row["depart_ville"],
                    "code_pays": row["depart_code_pays"],
                },
                "arrivee": {
                    "id_gare": row["arrivee_id_gare"],
                    "nom": row["arrivee_nom"],
                    "ville": row["arrivee_ville"],
                    "code_pays": row["arrivee_code_pays"],
                },
                "operateur": row["operateur"],
                "trajets": int(row["trajets"]),
                "duree_moyenne_minutes": round(to_float(row["duree_moyenne_minutes"]) or 0),
                "type_dominant": row["type_dominant"],
            }
            for row in rows
        ]
    }


@app.get("/operateurs", tags=["Référentiels"])
async def get_operateurs():
    rows = await database.fetch_all(
        """
        SELECT
            o.id_operateur,
            o.nom,
            COUNT(t.id_trajet) AS nb_trajets,
            COUNT(DISTINCT t.id_ligne) AS nb_lignes
        FROM operateur o
        LEFT JOIN trajet t ON t.id_operateur = o.id_operateur
        GROUP BY o.id_operateur, o.nom
        ORDER BY nb_trajets DESC
        """
    )
    return {
        "operateurs": [
            {
                "id_operateur": row["id_operateur"],
                "nom": row["nom"],
                "nb_trajets": int(row["nb_trajets"]),
                "nb_lignes": int(row["nb_lignes"]),
            }
            for row in rows
        ]
    }


@app.get("/lignes", tags=["Référentiels"])
async def get_lignes(
    id_operateur: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
):
    conditions: list[str] = []
    params: dict[str, Any] = {}

    if id_operateur:
        conditions.append("t.id_operateur = :id_operateur")
        params["id_operateur"] = id_operateur
    if search:
        conditions.append("(l.id_ligne ILIKE :search OR l.nom_ligne ILIKE :search)")
        params["search"] = f"%{search}%"

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    lines = await database.fetch_all(
        f"""
        SELECT
            l.id_ligne,
            l.nom_ligne,
            COUNT(t.id_trajet) AS nb_trajets,
            COALESCE(AVG(t.emission_co2_kg), 0) AS co2_moyen_kg,
            COALESCE(AVG(t.duree_minutes), 0) AS duree_moyenne_minutes
        FROM ligne l
        LEFT JOIN trajet t ON t.id_ligne = l.id_ligne
        {where}
        GROUP BY l.id_ligne, l.nom_ligne
        ORDER BY nb_trajets DESC, l.nom_ligne
        """,
        params,
    )

    operator_rows = await database.fetch_all(
        f"""
        SELECT DISTINCT t.id_ligne, o.id_operateur, o.nom
        FROM trajet t
        JOIN operateur o ON o.id_operateur = t.id_operateur
        LEFT JOIN ligne l ON l.id_ligne = t.id_ligne
        {where}
        """,
        params,
    )

    by_line: dict[str, list[dict[str, Any]]] = {}
    for row in operator_rows:
        by_line.setdefault(row["id_ligne"], []).append({"id_operateur": row["id_operateur"], "nom": row["nom"]})

    return {
        "lignes": [
            {
                "id_ligne": row["id_ligne"],
                "nom_ligne": row["nom_ligne"],
                "nb_trajets": int(row["nb_trajets"]),
                "operateurs": by_line.get(row["id_ligne"], []),
                "co2_moyen_kg": to_float(row["co2_moyen_kg"]),
                "duree_moyenne_minutes": round(to_float(row["duree_moyenne_minutes"]) or 0),
            }
            for row in lines
        ]
    }


@app.get("/gares", tags=["Référentiels"])
async def get_gares(
    code_pays: Optional[str] = Query(None),
    type_liaison: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    bbox: Optional[str] = Query(None),
):
    bbox_values = parse_bbox(bbox)

    conditions: list[str] = []
    params: dict[str, Any] = {}

    if code_pays:
        conditions.append("g.code_pays = :code_pays")
        params["code_pays"] = code_pays.upper()
    if type_liaison:
        conditions.append("g.type_liaison = :type_liaison")
        params["type_liaison"] = type_liaison
    if search:
        conditions.append("(g.id_gare ILIKE :search OR g.nom_officiel ILIKE :search OR g.ville ILIKE :search)")
        params["search"] = f"%{search}%"
    if bbox_values:
        lat_min, lng_min, lat_max, lng_max = bbox_values
        conditions.append("g.latitude BETWEEN :lat_min AND :lat_max")
        conditions.append("g.longitude BETWEEN :lng_min AND :lng_max")
        params.update({"lat_min": lat_min, "lat_max": lat_max, "lng_min": lng_min, "lng_max": lng_max})

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    rows = await database.fetch_all(
        f"""
        SELECT
            g.id_gare,
            g.nom_officiel,
            g.ville,
            g.code_pays,
            l.nom_pays AS pays,
            g.type_liaison,
            g.latitude,
            g.longitude,
            COUNT(t1.id_trajet) AS nb_departs,
            COUNT(t2.id_trajet) AS nb_arrivees
        FROM gare g
        LEFT JOIN localisation l ON l.code_pays = g.code_pays AND l.ville = g.ville
        LEFT JOIN trajet t1 ON t1.id_gare_depart = g.id_gare
        LEFT JOIN trajet t2 ON t2.id_gare_arrivee = g.id_gare
        {where}
        GROUP BY g.id_gare, g.nom_officiel, g.ville, g.code_pays, l.nom_pays, g.type_liaison, g.latitude, g.longitude
        ORDER BY g.code_pays, g.ville, g.nom_officiel
        """,
        params,
    )

    return {
        "gares": [
            {
                "id_gare": row["id_gare"],
                "nom_officiel": row["nom_officiel"],
                "ville": row["ville"],
                "code_pays": row["code_pays"],
                "pays": row["pays"],
                "type_liaison": row["type_liaison"],
                "latitude": to_float(row["latitude"]),
                "longitude": to_float(row["longitude"]),
                "nb_departs": int(row["nb_departs"]),
                "nb_arrivees": int(row["nb_arrivees"]),
            }
            for row in rows
        ]
    }


@app.get("/pays", tags=["Référentiels"])
async def get_pays():
    rows = await database.fetch_all(
        """
        SELECT
            g.code_pays,
            MAX(l.nom_pays) AS nom_pays,
            COUNT(DISTINCT g.id_gare) AS nb_gares,
            COUNT(t.id_trajet) AS nb_trajets_depart
        FROM gare g
        LEFT JOIN localisation l ON l.code_pays = g.code_pays
        LEFT JOIN trajet t ON t.id_gare_depart = g.id_gare
        GROUP BY g.code_pays
        ORDER BY g.code_pays
        """
    )
    return {
        "pays": [
            {
                "code_pays": row["code_pays"],
                "nom_pays": row["nom_pays"],
                "nb_gares": int(row["nb_gares"]),
                "nb_trajets_depart": int(row["nb_trajets_depart"]),
            }
            for row in rows
        ]
    }


@app.get("/imports", tags=["Imports"])
async def get_imports(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    statut: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
):
    statut_norm = normalize_statut(statut)
    offset = (page - 1) * limit

    conditions: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if statut_norm:
        conditions.append("LOWER(hi.statut) IN (:statut_1, :statut_2)")
        params["statut_1"] = statut_norm
        params["statut_2"] = "echec" if statut_norm == "échec" else statut_norm
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="since doit être une date ISO-8601") from exc
        if since_dt.tzinfo is not None:
            since_dt = since_dt.astimezone(timezone.utc).replace(tzinfo=None)
        conditions.append("hi.date_import >= :since")
        params["since"] = since_dt

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    count_params = {k: v for k, v in params.items() if k not in {"limit", "offset"}}

    total = await database.fetch_val(f"SELECT COUNT(*) FROM historique_import hi {where}", count_params)
    rows = await database.fetch_all(
        f"""
        SELECT hi.id_import, hi.date_import, hi.nb_lignes_importees, hi.statut, hi.message
        FROM historique_import hi
        {where}
        ORDER BY hi.date_import DESC
        LIMIT :limit OFFSET :offset
        """,
        params,
    )

    return {
        "page": page,
        "limit": limit,
        "total": int(total or 0),
        "imports": [
            {
                "id_import": int(row["id_import"]),
                "date_import": to_iso(row["date_import"]),
                "nb_lignes_importees": int(row["nb_lignes_importees"]),
                "statut": row["statut"],
                "message": row["message"],
            }
            for row in rows
        ],
    }


@app.get("/imports/stats", tags=["Imports"])
async def get_imports_stats():
    row = await database.fetch_one(
        """
        SELECT
            COUNT(*) AS total_imports,
            COUNT(*) FILTER (WHERE LOWER(statut) = 'succès' OR LOWER(statut) = 'succes') AS imports_reussis,
            COUNT(*) FILTER (WHERE LOWER(statut) = 'échec' OR LOWER(statut) = 'echec') AS imports_echoues,
            COUNT(*) FILTER (WHERE LOWER(statut) = 'partiel') AS imports_partiels,
            COALESCE(SUM(nb_lignes_importees), 0) AS lignes_importees_total,
            MAX(date_import) AS dernier_import_date
        FROM historique_import
        """
    )

    last_row = await database.fetch_one(
        """
        SELECT date_import, statut, nb_lignes_importees
        FROM historique_import
        ORDER BY date_import DESC
        LIMIT 1
        """
    )

    total = int(row["total_imports"])
    ok = int(row["imports_reussis"])
    ko = int(row["imports_echoues"])
    partial = int(row["imports_partiels"])

    return {
        "total_imports": total,
        "imports_reussis": ok,
        "imports_echoues": ko,
        "imports_partiels": partial,
        "taux_reussite": round(ok / total, 3) if total else 0,
        "dernier_import": {
            "date_import": to_iso(last_row["date_import"]) if last_row else None,
            "statut": last_row["statut"] if last_row else None,
            "nb_lignes_importees": int(last_row["nb_lignes_importees"]) if last_row else 0,
        },
        "lignes_importees_total": int(row["lignes_importees_total"]),
    }


# Backward-compatible endpoints kept for existing consumers/tests
@app.get("/dashboard", tags=["Compat"])
async def get_dashboard():
    return await get_stats_kpi()


@app.get("/emissions/stats", tags=["Compat"])
async def get_emissions_stats_compat():
    payload = await get_stats_volumes(groupby="operateur")
    return [
        {
            "operateur": item["nom"],
            "nb_trajets": item["trajets"],
            "emission_moyenne_kg": None,
        }
        for item in payload["repartition"]
    ]


@app.get("/localisations", tags=["Compat"])
async def get_localisations_compat(code_pays: Optional[str] = Query(None, min_length=2, max_length=2)):
    params: dict[str, Any] = {}
    where = ""
    if code_pays:
        where = "WHERE code_pays = :code_pays"
        params["code_pays"] = code_pays.upper()
    rows = await database.fetch_all(
        f"SELECT DISTINCT code_pays, nom_pays, ville FROM localisation {where} ORDER BY code_pays, ville",
        params,
    )
    return [dict(r) for r in rows]