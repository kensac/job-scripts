from __future__ import annotations

from fastapi import APIRouter, Depends

from api import db
from api.auth import AuthedUser, require_user

router = APIRouter()


@router.get("/user/stats")
def stats(user: AuthedUser = Depends(require_user)):
    by_status = db.query(
        "SELECT COALESCE(status, '') AS status, COUNT(*) AS count "
        "FROM user_jobs WHERE user_id = %s AND NOT hidden "
        "GROUP BY status ORDER BY count DESC",
        (user.id,),
    )
    by_source = db.query(
        """
        SELECT j.source, COUNT(*) AS total,
               COUNT(*) FILTER (WHERE uj.status IS NOT NULL AND uj.status != '') AS with_status,
               COUNT(*) FILTER (WHERE uj.date_applied IS NOT NULL) AS applied
        FROM user_jobs uj JOIN jobs j ON j.id = uj.job_id
        WHERE uj.user_id = %s AND NOT uj.hidden
        GROUP BY j.source ORDER BY total DESC
        """,
        (user.id,),
    )
    by_source_status = db.query(
        """
        SELECT j.source, COALESCE(uj.status, '') AS status, COUNT(*) AS count
        FROM user_jobs uj JOIN jobs j ON j.id = uj.job_id
        WHERE uj.user_id = %s AND NOT uj.hidden
        GROUP BY j.source, uj.status ORDER BY j.source, count DESC
        """,
        (user.id,),
    )
    over_time = db.query(
        "SELECT date_trunc('week', date_applied)::date AS week, COUNT(*) AS applied "
        "FROM user_jobs WHERE user_id = %s AND date_applied IS NOT NULL "
        "GROUP BY week ORDER BY week",
        (user.id,),
    )
    totals = db.query_one(
        """
        SELECT COUNT(*) AS tracked,
               COUNT(*) FILTER (WHERE date_applied IS NOT NULL) AS applied,
               COUNT(*) FILTER (WHERE hidden) AS hidden
        FROM user_jobs WHERE user_id = %s
        """,
        (user.id,),
    )
    return {
        "totals": totals,
        "by_status": by_status,
        "by_source": by_source,
        "by_source_status": by_source_status,
        "applied_by_week": over_time,
    }
