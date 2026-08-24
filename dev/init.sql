-- Dev database bootstrap (Phase 0).
-- Demo schema: orders/customers/products — realistic enough for explain/index/migration work.
-- orders is seeded to ~1.2M rows via generate_series so seq-scan / lock-impact / EXPLAIN
-- scenarios in later phases have real numbers to reason about, not toy tables.

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE customers (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email       text NOT NULL UNIQUE,
    full_name   text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE products (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku         text NOT NULL UNIQUE,
    name        text NOT NULL,
    price_cents integer NOT NULL CHECK (price_cents >= 0)
);

CREATE TABLE orders (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id   bigint NOT NULL REFERENCES customers(id),
    status        text NOT NULL DEFAULT 'pending',
    total_cents   integer NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Deliberately only ONE index (customer_id), so Phase 3 (index.advise) and Phase 4
-- (lock-impact analysis) have a real missing-index / real ALTER-on-big-table story
-- instead of a pre-optimized toy schema.
CREATE INDEX idx_orders_customer_id ON orders (customer_id);

-- ~30k customers
INSERT INTO customers (email, full_name)
SELECT
    'customer' || i || '@example.com',
    'Customer ' || i
FROM generate_series(1, 30000) AS i;

-- 500 products
INSERT INTO products (sku, name, price_cents)
SELECT
    'SKU-' || lpad(i::text, 6, '0'),
    'Product ' || i,
    (100 + (i % 5000))
FROM generate_series(1, 500) AS i;

-- ~1.2M orders, customer_id skewed via modulo so distribution isn't perfectly uniform
-- (a handful of customers own a disproportionate slice of orders, like real traffic).
INSERT INTO orders (customer_id, status, total_cents, created_at)
SELECT
    1 + (
        CASE WHEN i % 50 = 0 THEN i % 200          -- hot customers: repeat heavily
             ELSE i % 30000
        END
    ),
    (ARRAY['pending', 'paid', 'shipped', 'cancelled', 'refunded'])[1 + (i % 5)],
    (500 + (i % 20000)),
    now() - ((1200000 - i) || ' seconds')::interval
FROM generate_series(1, 1200000) AS i;

ANALYZE customers;
ANALYZE products;
ANALYZE orders;
