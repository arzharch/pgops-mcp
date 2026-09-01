-- Dev database bootstrap (Phase 0).
-- Demo schema: orders/customers/products — realistic enough for explain/index/migration work.
-- orders is seeded to ~1.2M rows via generate_series so seq-scan / lock-impact / EXPLAIN
-- scenarios in later phases have real numbers to reason about, not toy tables.

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS vector;

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

-- Vector Schemas

CREATE TABLE document_embeddings (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    content text NOT NULL,
    embedding vector(384) -- Common dimension for all-MiniLM-L6-v2
);

CREATE TABLE product_embeddings (
    product_id bigint PRIMARY KEY REFERENCES products(id),
    embedding vector(128) -- Smaller dimension for testing different index types
);

-- Seed document embeddings (10k rows) with random vectors
INSERT INTO document_embeddings (content, embedding)
SELECT 
    'Document content ' || i,
    (SELECT array_agg(random()) FROM generate_series(1, 384))::vector
FROM generate_series(1, 10000) AS i;

-- Seed product embeddings (all 500 products) with random vectors
INSERT INTO product_embeddings (product_id, embedding)
SELECT 
    id,
    (SELECT array_agg(random()) FROM generate_series(1, 128))::vector
FROM products;

-- Create an HNSW index on document_embeddings using cosine distance (Standard everyday vector index)
CREATE INDEX idx_docs_embedding_hnsw ON document_embeddings USING hnsw (embedding vector_cosine_ops);

-- Create an IVFFlat index on product_embeddings (Older/rarer index type)
-- IVFFlat needs data to build the clusters, so it's created after inserts
CREATE INDEX idx_products_embedding_ivfflat ON product_embeddings USING ivfflat (embedding vector_l2_ops) WITH (lists = 10);

ANALYZE document_embeddings;
ANALYZE product_embeddings;
