# Демо-база для 1С:УНФ и HTTP API

Тестовая база данных PostgreSQL вымышленной компании «Вектор-Сервис»
(продажа компьютерной техники и IT-услуги) и HTTP API, через который
1С:Предприятие 8.3.26 читает и записывает данные.

**Все данные вымышленные.** Названия организаций, ИНН, КПП, адреса,
телефоны, ФИО сотрудников и суммы сгенерированы случайно и не относятся
к реальным лицам и компаниям. Названия товаров — обычные рыночные
наименования, цены примерные.

## Состав

- `unf_demo.sql` — схема и данные PostgreSQL (13 таблиц: номенклатура,
  контрагенты, сотрудники, поступления, реализации, оплаты).
- `api.py` — HTTP API на FastAPI с авторизацией по ключу `X-API-Key`.
- `unf_form_module.bsl` — модуль формы внешней обработки для 1С 8.3.26.
- `requirements.txt` — Python-зависимости API.

## Остатки и маржинальность

API рассчитывает остатки по движениям документов, не хранит остаток как
произвольное поле карточки товара:

- поступление увеличивает остаток;
- реализация уменьшает остаток;
- остаток по складу = сумма поступлений минус сумма реализаций;
- без фильтра по складу `/stock` возвращает остаток каждого товара по каждому складу.

В ответе `GET /nomenclature` дополнительно возвращаются:

- `qty_on_hand` — общий остаток товара по всем складам;
- `margin_amount` — розничная цена минус закупочная цена;
- `margin_percent` — маржинальность: маржа / розничная цена × 100%;
- `markup_percent` — наценка: маржа / закупочная цена × 100%.

В ответе `GET /sales/{doc_id}` каждая строка продажи содержит
`margin_per_unit` и `margin_total`, рассчитанные по текущей закупочной
цене номенклатуры. Это ориентировочная валовая прибыль, не бухгалтерская
себестоимость по партиям.

Примеры запросов (добавляйте заголовок `X-API-Key`):

```http
GET /nomenclature?limit=500
GET /stock
GET /stock?warehouse_id=1
GET /stock?include_zero=true
GET /sales/1
```

Важно: в текущей демо-схеме нет отдельного регистра резервов, списаний,
перемещений и возвратов. Поэтому рассчитанный остаток учитывает только
поступления и реализации из имеющихся таблиц. Для точного складского
учёта эти движения нужно будет добавить отдельно.

## Установка на VPS (Ubuntu, от root)

1. Направьте домен (например, DuckDNS) на IP сервера.
2. Склонируйте репозиторий:

```bash
apt update && apt install -y git
git clone https://github.com/ВАШ_ЛОГИН/unf-demo.git /opt/unf-demo
```

3. Установите PostgreSQL, создайте swap и настройте память:

```bash
apt install -y postgresql postgresql-contrib python3-venv python3-pip caddy
fallocate -l 1G /swapfile && chmod 600 /swapfile
mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' | tee -a /etc/fstab
sudo -u postgres psql -c "ALTER SYSTEM SET shared_buffers='128MB';"
sudo -u postgres psql -c "ALTER SYSTEM SET effective_cache_size='512MB';"
sudo -u postgres psql -c "ALTER SYSTEM SET work_mem='4MB';"
sudo -u postgres psql -c "ALTER SYSTEM SET maintenance_work_mem='64MB';"
sudo -u postgres psql -c "ALTER SYSTEM SET max_connections=20;"
systemctl restart postgresql
```

4. Загрузите базу (один раз, скрипт удаляет базу с тем же именем):

```bash
sudo -u postgres psql -f /opt/unf-demo/unf_demo.sql
```

5. Создайте пользователя БД и ключ API:

```bash
export DBPASS=$(openssl rand -hex 16)
export APIKEY=$(openssl rand -hex 32)
sudo -u postgres psql -d unf_demo -c "CREATE USER unf_user WITH PASSWORD '$DBPASS';"
sudo -u postgres psql -d unf_demo -c "GRANT CONNECT ON DATABASE unf_demo TO unf_user;"
sudo -u postgres psql -d unf_demo -c "GRANT USAGE ON SCHEMA public TO unf_user;"
sudo -u postgres psql -d unf_demo -c "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO unf_user;"
sudo -u postgres psql -d unf_demo -c "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO unf_user;"
tee /etc/unf-api.env >/dev/null <<EOF
UNF_API_KEY=$APIKEY
UNF_DSN=postgresql://unf_user:$DBPASS@127.0.0.1:5432/unf_demo
EOF
chmod 600 /etc/unf-api.env
echo "API-ключ (сохраните): $APIKEY"
```

6. Установите API:

```bash
useradd --system --home /opt/unf-api --shell /usr/sbin/nologin unfapi
mkdir -p /opt/unf-api && chown unfapi:unfapi /opt/unf-api
cp /opt/unf-demo/api.py /opt/unf-api/api.py && chown unfapi:unfapi /opt/unf-api/api.py
sudo -u unfapi python3 -m venv /opt/unf-api/venv
sudo -u unfapi /opt/unf-api/venv/bin/pip install -r /opt/unf-demo/requirements.txt
```

7. Создайте службу `/etc/systemd/system/unf-api.service`:

```ini
[Unit]
Description=UNF API
After=network.target postgresql.service

[Service]
User=unfapi
Group=unfapi
WorkingDirectory=/opt/unf-api
EnvironmentFile=/etc/unf-api.env
ExecStart=/opt/unf-api/venv/bin/uvicorn api:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always
MemoryMax=300M

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now unf-api
```

8. Настройте HTTPS в `/etc/caddy/Caddyfile`:

```caddyfile
ваш-домен.duckdns.org {
    reverse_proxy 127.0.0.1:8000
}
```

```bash
systemctl restart caddy
ufw allow 22/tcp
ufw allow 80,443/tcp
ufw enable
```

Если SSH работает не на порту 22, откройте именно его.

9. Проверка:

```bash
curl -H "X-API-Key: ВАШ_КЛЮЧ" "https://ваш-домен.duckdns.org/nomenclature?limit=3"
curl -H "X-API-Key: ВАШ_КЛЮЧ" "https://ваш-домен.duckdns.org/stock"
```

## Подключение 1С

В `unf_form_module.bsl` впишите свои значения в функции
`АдресСервера()` и `КлючДоступа()`. Подробные шаги по созданию
внешней обработки — в документации к проекту.

## Важно

- `unf_demo.sql` начинается с `DROP DATABASE IF EXISTS unf_demo` —
  повторный запуск удалит все данные.
- API-ключ и пароль базы в репозитории не хранятся: ключ задаётся
  переменной `UNF_API_KEY`, строка подключения — `UNF_DSN`.
  В `unf_form_module.bsl` оставлены заглушки.
- Код 1С проверяйте сначала на тестовой базе: набор обязательных
  реквизитов отличается в разных редакциях УНФ.

## Обновление API после правок

```bash
cd /opt/unf-demo && git pull
cp api.py /opt/unf-api/api.py && chown unfapi:unfapi /opt/unf-api/api.py
systemctl restart unf-api
```
