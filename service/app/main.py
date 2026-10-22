import asyncio
import logging
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import settings
from .metrics import MetricsMiddleware
from .schemas import StudyIn

log = logging.getLogger("studies")
logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")

POD = settings.pod_name  # імʼя екземпляра: POD_NAME, а без неї імʼя хоста контейнера
MAX_BIGINT = 2**63 - 1
MAX_OFFSET = 1_000_000_000  # технічна межа, щоб не віддати помилку бази назовні

COLUMNS = (
    "id, patient_code, modality, slice_count, "
    "slice_thickness_mm::float8 AS slice_thickness_mm, device, study_date, created_at, updated_at"
)
SQL_INSERT = (
    "INSERT INTO studies (patient_code, modality, slice_count, slice_thickness_mm, device, study_date) "
    f"VALUES ($1, $2, $3, $4, $5, $6) RETURNING {COLUMNS}"
)
SQL_UPDATE = (
    "UPDATE studies SET patient_code=$2, modality=$3, slice_count=$4, slice_thickness_mm=$5, "
    f"device=$6, study_date=$7, updated_at=now() WHERE id=$1 RETURNING {COLUMNS}"
)
SQL_GET = f"SELECT {COLUMNS} FROM studies WHERE id=$1"
SQL_DELETE = "DELETE FROM studies WHERE id=$1 RETURNING id"
SQL_LIST = f"SELECT {COLUMNS} FROM studies ORDER BY id LIMIT $1 OFFSET $2"
SQL_LIST_FILTERED = f"SELECT {COLUMNS} FROM studies WHERE modality=$1 ORDER BY id LIMIT $2 OFFSET $3"
SQL_COUNT = "SELECT count(*) FROM studies"
SQL_COUNT_FILTERED = "SELECT count(*) FROM studies WHERE modality=$1"

# Помилки, які означають «база недоступна», а не «клієнт помилився»
DB_UNAVAILABLE_ERRORS = (
    asyncpg.PostgresConnectionError,
    asyncpg.TooManyConnectionsError,
    asyncpg.InterfaceError,
    asyncpg.QueryCanceledError,
    asyncio.TimeoutError,
    ConnectionError,
    OSError,
)
STALE_CONNECTION = (asyncpg.ConnectionDoesNotExistError, asyncpg.InterfaceError)


class DbNotReady(Exception):
    pass


def reply(data, status: int = 200, headers: dict | None = None) -> JSONResponse:
    h = {"X-Pod": POD}
    if headers:
        h.update(headers)
    return JSONResponse(data, status_code=status, headers=h)


async def _connect_loop(app: FastAPI) -> None:
    """Фоново чекає на базу. Сервіс при цьому вже відповідає: /healthz дає 503."""
    while True:
        try:
            pool = app.state.pool
            await pool  # ініціалізація: відкриває DB_POOL_MIN зʼєднань, кидає виняток, якщо бази немає
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            app.state.ready = True
            log.info("база доступна, пул ініціалізовано")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("база ще недоступна: %s", exc)
            await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.ready = False
    app.state.pool = asyncpg.create_pool(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_size,  # зʼєднань понад пул немає: asyncpg не має overflow
        timeout=settings.db_connect_timeout,
        command_timeout=settings.db_command_timeout,
        max_inactive_connection_lifetime=300,
    )
    task = asyncio.create_task(_connect_loop(app))
    try:
        yield
    finally:
        task.cancel()
        try:
            await asyncio.wait_for(app.state.pool.close(), timeout=5)
        except Exception:  # noqa: BLE001
            app.state.pool.terminate()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(MetricsMiddleware)


async def db(request: Request, fn, *, retry: bool):
    """Виконує fn(conn). retry дозволено лише для ідемпотентних операцій:
    якщо з пулу дістали «мертве» зʼєднання (база перезапускалась), беремо нове."""
    if not request.app.state.ready:
        raise DbNotReady
    attempts = 2 if retry else 1
    for i in range(attempts):
        try:
            async with request.app.state.pool.acquire() as conn:
                return await fn(conn)
        except STALE_CONNECTION:
            if i + 1 == attempts:
                raise


def parse_id(raw: str) -> int | None:
    """Ідентифікатор, який не може існувати (не число, поза bigint), — це 404, а не 500."""
    if not raw.isascii() or not raw.isdigit():
        return None
    value = int(raw)
    return value if 0 < value <= MAX_BIGINT else None


def to_dict(r: asyncpg.Record) -> dict:
    return {
        "id": r["id"],
        "patient_code": r["patient_code"],
        "modality": r["modality"],
        "slice_count": r["slice_count"],
        "slice_thickness_mm": r["slice_thickness_mm"],
        "device": r["device"],
        "study_date": r["study_date"].isoformat(),
        "created_at": r["created_at"].isoformat(),
        "updated_at": r["updated_at"].isoformat(),
    }


def body_args(b: StudyIn) -> tuple:
    return (b.patient_code, b.modality, b.slice_count, b.slice_thickness_mm, b.device, b.study_date)


# ---------- обробники помилок ----------

@app.exception_handler(RequestValidationError)
async def on_validation_error(_: Request, exc: RequestValidationError):
    details = []
    for e in exc.errors():
        parts = [str(p) for p in e["loc"] if p not in ("body", "query", "path") and not isinstance(p, int)]
        message = str(e["msg"]).removeprefix("Value error, ")
        details.append({"field": ".".join(parts) or "body", "message": message})
    return reply({"error": "validation_error", "details": details}, 400)


@app.exception_handler(StarletteHTTPException)
async def on_http_error(_: Request, exc: StarletteHTTPException):
    return reply({"error": "http_error", "message": str(exc.detail)}, exc.status_code)


@app.exception_handler(DbNotReady)
async def on_db_not_ready(_: Request, __: DbNotReady):
    return reply({"error": "database_unavailable"}, 503, {"Retry-After": "2"})


async def on_db_unavailable(_: Request, exc: Exception):
    log.warning("база недоступна під час запиту: %r", exc)
    return reply({"error": "database_unavailable"}, 503, {"Retry-After": "2"})


for _exc_type in DB_UNAVAILABLE_ERRORS:
    app.add_exception_handler(_exc_type, on_db_unavailable)


@app.exception_handler(Exception)
async def on_unexpected(_: Request, exc: Exception):
    log.exception("неочікувана помилка: %r", exc)
    return reply({"error": "internal_error"}, 500)


# ---------- ендпоінти ----------

@app.get("/livez")
async def livez():
    """Процес живий. До бази не звертається, на відміну від /healthz."""
    return reply({"status": "alive"})


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST, headers={"X-Pod": POD})


@app.get("/healthz")
async def healthz(request: Request):
    if not request.app.state.ready:
        return reply({"status": "unavailable", "reason": "база ще не підключена"}, 503)
    try:
        await asyncio.wait_for(request.app.state.pool.fetchval("SELECT 1"), timeout=2)
    except Exception as exc:  # noqa: BLE001
        return reply({"status": "unavailable", "reason": type(exc).__name__}, 503)
    return reply({"status": "ok"})


@app.post("/studies")
async def create_study(request: Request, body: StudyIn):
    row = await db(request, lambda c: c.fetchrow(SQL_INSERT, *body_args(body)), retry=False)
    data = to_dict(row)
    return reply(data, 201, {"Location": f"/studies/{data['id']}"})


@app.get("/studies")
async def list_studies(
    request: Request,
    limit: int | None = Query(None, ge=1, le=settings.max_limit),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
    modality: str | None = Query(None, min_length=1, max_length=16),
):
    eff_limit = settings.default_limit if limit is None else limit
    flt = modality.strip().upper() if modality is not None else None

    async def run(conn):
        if flt is None:
            total = await conn.fetchval(SQL_COUNT)
            rows = await conn.fetch(SQL_LIST, eff_limit, offset)
        else:
            total = await conn.fetchval(SQL_COUNT_FILTERED, flt)
            rows = await conn.fetch(SQL_LIST_FILTERED, flt, eff_limit, offset)
        return total, rows

    total, rows = await db(request, run, retry=True)
    return reply({"items": [to_dict(r) for r in rows], "total": total, "limit": eff_limit, "offset": offset})


@app.get("/studies/{study_id}")
async def get_study(request: Request, study_id: str):
    sid = parse_id(study_id)
    row = None if sid is None else await db(request, lambda c: c.fetchrow(SQL_GET, sid), retry=True)
    if row is None:
        return reply({"error": "not_found", "message": f"дослідження {study_id} не існує"}, 404)
    return reply(to_dict(row))


@app.put("/studies/{study_id}")
async def replace_study(request: Request, study_id: str, body: StudyIn):
    sid = parse_id(study_id)
    row = None if sid is None else await db(
        request, lambda c: c.fetchrow(SQL_UPDATE, sid, *body_args(body)), retry=True
    )
    if row is None:
        return reply({"error": "not_found", "message": f"дослідження {study_id} не існує"}, 404)
    return reply(to_dict(row))


@app.delete("/studies/{study_id}")
async def delete_study(request: Request, study_id: str):
    sid = parse_id(study_id)
    row = None if sid is None else await db(request, lambda c: c.fetchrow(SQL_DELETE, sid), retry=False)
    if row is None:
        return reply({"error": "not_found", "message": f"дослідження {study_id} не існує"}, 404)
    return Response(status_code=204, headers={"X-Pod": POD})
