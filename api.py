"""HTTP API между 1С и PostgreSQL (база unf_demo).
Запуск: uvicorn api:app --host 127.0.0.1 --port 8000
Переменные окружения: UNF_API_KEY (ключ доступа), UNF_DSN (строка подключения к PostgreSQL).
"""
import hmac
import os
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from typing import List, Literal, Optional

import psycopg
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

API_KEY = os.environ.get("UNF_API_KEY", "")
DSN = os.environ.get("UNF_DSN", "dbname=unf_demo")

pool = ConnectionPool(
    DSN, min_size=1, max_size=4, kwargs={"row_factory": dict_row}, open=False
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if len(API_KEY) < 24:
        raise RuntimeError("UNF_API_KEY должен быть не короче 24 символов")
    pool.open()
    yield
    pool.close()


app = FastAPI(title="UNF API", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


def check_key(x_api_key: str = Header(default="")):
    if not API_KEY or not hmac.compare_digest(x_api_key.encode(), API_KEY.encode()):
        raise HTTPException(status_code=401, detail="Неверный API-ключ")


router = APIRouter(dependencies=[Depends(check_key)])


def fetch(sql: str, params: tuple = ()):
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchall()


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------- чтение ----------
@router.get("/nomenclature")
def nomenclature(limit: int = Query(500, ge=1, le=5000), offset: int = Query(0, ge=0)):
    return fetch(
        """SELECT n.code, n.name, g.name AS group_name, n.unit, n.is_service,
                  n.vat_rate, n.retail_price, n.purchase_price
           FROM nomenclature n LEFT JOIN nomenclature_groups g ON g.id = n.group_id
           ORDER BY n.code LIMIT %s OFFSET %s""",
        (limit, offset),
    )


@router.get("/counterparties")
def counterparties(kind: Optional[Literal["Покупатель", "Поставщик"]] = None,
                   limit: int = Query(500, ge=1, le=5000), offset: int = Query(0, ge=0)):
    return fetch(
        """SELECT id, name, inn, kpp, kind, city, address, phone
           FROM counterparties WHERE (%s::text IS NULL OR kind = %s::text)
           ORDER BY id LIMIT %s OFFSET %s""",
        (kind, kind, limit, offset),
    )


@router.get("/employees")
def employees():
    return fetch(
        """SELECT e.tab_number, e.full_name, e.position, d.name AS department,
                  e.salary, e.hire_date
           FROM employees e LEFT JOIN departments d ON d.id = e.department_id
           ORDER BY e.tab_number"""
    )


@router.get("/sales")
def sales(date_from: Optional[date] = None, date_to: Optional[date] = None,
          limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
    return fetch(
        """SELECT s.id, s.doc_number, s.doc_date, c.name AS customer,
                  e.full_name AS manager, s.total
           FROM sales_docs s
           JOIN counterparties c ON c.id = s.customer_id
           LEFT JOIN employees e ON e.id = s.manager_id
           WHERE (%s::date IS NULL OR s.doc_date >= %s::date)
             AND (%s::date IS NULL OR s.doc_date <= %s::date)
           ORDER BY s.doc_date, s.id LIMIT %s OFFSET %s""",
        (date_from, date_from, date_to, date_to, limit, offset),
    )


@router.get("/sales/{doc_id}")
def sale(doc_id: int):
    head = fetch(
        """SELECT s.id, s.doc_number, s.doc_date, c.name AS customer, s.total
           FROM sales_docs s JOIN counterparties c ON c.id = s.customer_id
           WHERE s.id = %s""",
        (doc_id,),
    )
    if not head:
        raise HTTPException(status_code=404, detail="Документ не найден")
    lines = fetch(
        """SELECT n.code, n.name, l.qty, l.price, l.amount
           FROM sales_lines l JOIN nomenclature n ON n.id = l.item_id
           WHERE l.doc_id = %s ORDER BY l.id""",
        (doc_id,),
    )
    return {**head[0], "lines": lines}


# ---------- запись ----------
class CounterpartyIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    inn: Optional[str] = Field(default=None, max_length=12)
    kpp: Optional[str] = Field(default=None, max_length=9)
    kind: Literal["Покупатель", "Поставщик"] = "Покупатель"
    city: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None


@router.post("/counterparties", status_code=201)
def create_counterparty(body: CounterpartyIn):
    with pool.connection() as conn:
        if body.inn:
            ex = conn.execute(
                "SELECT id FROM counterparties WHERE inn = %s AND kind = %s",
                (body.inn, body.kind),
            ).fetchone()
            if ex:
                return {"id": ex["id"], "existing": True}
        row = conn.execute(
            """INSERT INTO counterparties(name, inn, kpp, kind, city, address, phone)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (body.name, body.inn, body.kpp, body.kind, body.city, body.address, body.phone),
        ).fetchone()
    return {"id": row["id"], "existing": False}


class SaleLineIn(BaseModel):
    item_id: int
    qty: Decimal = Field(gt=0)
    price: Decimal = Field(ge=0)


class SaleIn(BaseModel):
    doc_number: str = Field(min_length=1, max_length=20)
    doc_date: date
    customer_id: int
    manager_id: Optional[int] = None
    warehouse_id: int = 1
    lines: List[SaleLineIn] = Field(min_length=1)


@router.post("/sales", status_code=201)
def create_sale(body: SaleIn):
    total = sum((l.qty * l.price for l in body.lines), Decimal("0")).quantize(Decimal("0.01"))
    try:
        with pool.connection() as conn:
            row = conn.execute(
                """INSERT INTO sales_docs(doc_number, doc_date, customer_id, manager_id,
                                          warehouse_id, total)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                (body.doc_number, body.doc_date, body.customer_id, body.manager_id,
                 body.warehouse_id, total),
            ).fetchone()
            for l in body.lines:
                conn.execute(
                    "INSERT INTO sales_lines(doc_id, item_id, qty, price) VALUES (%s,%s,%s,%s)",
                    (row["id"], l.item_id, l.qty, l.price),
                )
    except psycopg.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="Документ с таким номером уже есть")
    except psycopg.errors.ForeignKeyViolation:
        raise HTTPException(status_code=400, detail="Неверный customer_id, manager_id или item_id")
    return {"id": row["id"], "total": total}


app.include_router(router)
