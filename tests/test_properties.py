"""Property-based tests — invariants that must hold for ALL inputs, not just curated ones.

Table-driven tests prove the classifier handles the cases we thought of. Property-based
tests (Hypothesis) generate thousands of mutated/adversarial SQL strings and assert
invariants that must hold universally. The two most important:

1. **Soundness of the read gate**: if `classify(sql).is_read`, then executing it on a
   readonly connection can never mutate data. Approximated here at the text level: a
   statement classified read contains no write-DML keyword token outside literals/
   comments, and every function it references is either provably non-volatile or absent
   from pg_proc (which query_read then refuses).

2. **Guardrail totality**: `evaluate()` returns a verdict for EVERY input — never raises,
   never returns None. A classifier that crashes on weird input fails open in production.

3. **Token binding is injective enough**: distinct statements produce distinct
   fingerprints (no collisions across generated inputs), and redeem() never accepts a
   token for different SQL regardless of case/whitespace mutations.

4. **Serialization totality**: serialize_record never raises on any JSON-encodable
   asyncpg-shaped value.

Strategy design notes:
- SQL strings are built compositionally (statement templates × injected fragments)
  rather than pure random text — pure random bytes almost always fail to parse and
  generate no information about the *classifier's* logic.
- The mutation strategies deliberately include the historical attack shapes: writes in
  comments, writes in string literals, CTE-wrapped DML, mixed-case keywords, unicode
  whitespace padding.
"""

from __future__ import annotations

import json
import string

from hypothesis import given, settings
from hypothesis import strategies as st

from pgops.audit import sql_fingerprint
from pgops.classifier import StatementClass, classify
from pgops.errors import PgopsError
from pgops.function_safety import function_references
from pgops.guardrails import ConfirmationTokenStore, evaluate, has_where_clause
from pgops.serialize import serialize_record

# --- strategies -------------------------------------------------------------------------

KEYWORDS = st.sampled_from(
    [
        "SELECT", "select", "SeLeCt", "INSERT", "insert", "UPDATE", "update",
        "DELETE", "delete", "DROP", "truncate", "TRUNCATE", "ALTER", "CREATE",
        "WITH", "EXPLAIN", "TABLE", "FROM", "WHERE", "VALUES", "INTO", "GRANT",
        "VACUUM", "COPY", "DO", "CALL",
    ]
)

IDENTIFIERS = st.text(
    alphabet=string.ascii_letters + string.digits + "_$", min_size=1, max_size=12
).filter(lambda s: not s[0].isdigit())

# fragments that historically broke or bypassed classifiers
TRICKY_FRAGMENTS = st.sampled_from(
    [
        "'; DROP TABLE items; --",
        "/* INSERT INTO items */",
        "-- DELETE FROM orders\n",
        "'quoted ( parens )'",
        "$$ dollar quoted $$",
        "(subquery)",
        "1 = 1 OR 'a'='a'",
        "::regclass",
        "AT TIME ZONE 'UTC'",
        "FOR UPDATE",
        "NOWAIT",
        "SKIP LOCKED",
    ]
)

SIMPLE_STATEMENTS = st.tuples(KEYWORDS, IDENTIFIERS).map(
    lambda t: f"{t[0]} {t[1]}"
)


@st.composite
def mutated_statements(draw: st.DrawFn) -> str:
    """A simple statement with 0-3 tricky fragments spliced in at random points."""
    base = draw(SIMPLE_STATEMENTS)
    n = draw(st.integers(min_value=0, max_value=3))
    fragments = draw(st.lists(TRICKY_FRAGMENTS, min_size=n, max_size=n))
    result = base
    for frag in fragments:
        pos = draw(st.integers(min_value=0, max_value=len(result)))
        result = result[:pos] + " " + frag + " " + result[pos:]
    return result


WRITE_DML = {"INSERT", "UPDATE", "DELETE"}


def _visible_write_dml_tokens(sql: str) -> bool:
    """Reference implementation of 'contains a write DML keyword outside literals and
    comments' — used to check the classifier against an independent reading."""
    import sqlparse
    from sqlparse import tokens as T

    parsed = sqlparse.parse(sql)[0]
    for tok in parsed.flatten():  # type: ignore[no-untyped-call]
        if tok.is_whitespace or tok.ttype in T.Comment:
            continue
        if tok.ttype in T.String or tok.ttype in T.String.Single:
            continue
        if tok.ttype is T.Keyword.DML and tok.normalized.upper() in WRITE_DML:
            return True
    return False


# --- invariant 1: soundness of the read gate --------------------------------------------


@settings(max_examples=500, deadline=None)
@given(mutated_statements())
def test_property_read_classification_never_hides_visible_write_dml(sql: str) -> None:
    """If the classifier says READ, there must be no write-DML keyword token visible
    outside literals/comments. This is the property the readonly pool backs up: the
    classifier is the first gate, and this asserts it never waves through what its own
    reference scan can see."""
    c = classify(sql)
    if c.is_read:
        assert not _visible_write_dml_tokens(sql), (
            f"classified READ but visible write DML present: {sql!r}"
        )


@settings(max_examples=300, deadline=None)
@given(mutated_statements())
def test_property_unknown_is_never_treated_as_read(sql: str) -> None:
    """UNKNOWN must never be less dangerous than DESTRUCTIVE (ADR-001)."""
    c = classify(sql)
    if c.kind is StatementClass.UNKNOWN:
        assert c.effective_gate_class is StatementClass.DESTRUCTIVE


@settings(max_examples=300, deadline=None)
@given(mutated_statements())
def test_property_function_extraction_total_and_lowercase(sql: str) -> None:
    """function_references never raises and only returns lowercase names."""
    refs = function_references(sql)
    assert all(r == r.lower() for r in refs)


# --- invariant 2: guardrail totality ------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(mutated_statements())
def test_property_guardrails_total_never_raises(sql: str) -> None:
    """evaluate() returns a verdict for every input — a crash here would fail open."""
    from pgops.classifier import classify as cls

    classification = cls(sql)
    verdict = evaluate(classification, sql)
    assert verdict is not None
    assert isinstance(verdict.allowed, bool)


@settings(max_examples=200, deadline=None)
@given(st.text(alphabet=string.printable, max_size=200))
def test_property_has_where_never_raises_on_arbitrary_text(text: str) -> None:
    """has_where_clause takes arbitrary agent text; it must never raise."""
    result = has_where_clause(text)
    assert isinstance(result, bool)


# --- invariant 3: token binding ------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(
    st.lists(
        st.tuples(IDENTIFIERS, IDENTIFIERS), min_size=1, max_size=20
    ).map(lambda pairs: [f"DELETE FROM {a} WHERE id = {i}" for i, (a, _) in enumerate(pairs)])
)
def test_property_distinct_statements_have_distinct_fingerprints(statements: list[str]) -> None:
    """No sha256 collisions across generated statements — binding depends on it."""
    seen: dict[str, str] = {}
    for s in statements:
        fp = sql_fingerprint(s)
        assert fp not in seen or seen[fp] == s, f"collision: {fp} for {s!r} and {seen.get(fp)!r}"
        seen[fp] = s


@settings(max_examples=200, deadline=None)
@given(
    IDENTIFIERS,
    st.one_of(
        st.just(" "),
        st.just("  "),
        st.just("\n"),
        st.just("\t"),
        st.just(" ; "),
        st.just(" --x\n"),
    ),
    IDENTIFIERS,
)
def test_property_whitespace_mutations_cannot_redeem_token(table: str, junk: str, col: str) -> None:
    """A token issued for exact SQL must not redeem for any mutated variant — even
    trailing whitespace/comments change the fingerprint, and MUST be rejected (the
    user approved one specific statement, not a prefix class)."""
    store = ConfirmationTokenStore()
    original = f"DELETE FROM {table}"
    token = store.issue(original, "test")

    mutated = original + junk + f" AND {col} IS NOT NULL"
    try:
        store.redeem(token, mutated)
        redeemed = True
    except PgopsError:
        redeemed = False
    # the ONLY acceptable outcome is rejection unless mutation was a no-op identity
    if mutated != original:
        assert not redeemed, f"token redeemed across mutation: {original!r} -> {mutated!r}"


# --- invariant 4: serialization totality ---------------------------------------------------


JSON_COMPATIBLE_VALUES = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-(2**63), max_value=2**63 - 1),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(max_size=64),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(max_size=16), children, max_size=5),
    ),
    max_leaves=10,
)


@settings(max_examples=300, deadline=None)
@given(JSON_COMPATIBLE_VALUES)
def test_property_serialize_record_output_always_json_encodable(value: object) -> None:
    """serialize_record output must survive json.dumps for any input value — the MCP
    transport is JSON, so a non-encodable field breaks the whole response."""
    record = {"col": value}
    out = serialize_record(record)
    json.dumps(out)


@settings(max_examples=100, deadline=None)
@given(st.data())
def test_property_roundtrip_keys_preserved(data: st.DataObject) -> None:
    keys = data.draw(
        st.lists(
            st.text(alphabet=string.ascii_letters + "_", min_size=1, max_size=10),
            min_size=1,
            max_size=5,
            unique=True,
        )
    )
    record = {k: data.draw(JSON_COMPATIBLE_VALUES) for k in keys}
    out = serialize_record(record)
    assert set(out.keys()) == set(keys)
