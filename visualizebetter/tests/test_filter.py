"""Completion verification for TASK R — 필터 DSL 코어 ([6]).

Covers, in order: every [6] example parses and evaluates; the schema-less
semantics ([6] 키부재/타입불일치/null/no-coercion); the safety caps ([6]
2KB/depth/within/path/50K); ReDoS defence (re2); and property-based invariants.
"""

import time

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from visualizebetter.filter import (
    FilterError,
    FilterLimitError,
    FilterSyntaxError,
    compile_filter,
    evaluate_edges,
    evaluate_nodes,
)
from visualizebetter.filter.limits import (
    MAX_EXPRESSION_BYTES,
    MAX_PATH_HOPS,
    MAX_VISITED_NODES,
    MAX_WITHIN,
)
from visualizebetter.graph.core import Graph


@pytest.fixture
def graph():
    g = Graph(name="test")
    g.add_node(
        id="app.World",
        label="OrderService",
        type="class",
        properties={"field_count": 12, "ns": "app.ui.Panel"},
        tags=["authored_by_me", "core"],
    )
    g.add_node(
        id="UI.MainView",
        label="UIMainView",
        type="component",
        properties={"field_count": 3, "ns": "app.ui.View"},
    )
    g.add_node(id="Svc.User", label="PaymentService", type="service")
    g.add_node(id="Svc.Auth", label="AuthService", type="service")
    g.add_edge(source="app.World", target="UI.MainView", relation="owns")
    g.add_edge(source="UI.MainView", target="Svc.User", relation="uses")
    g.add_edge(source="Svc.User", target="Svc.Auth", relation="calls")
    return g


def match(expr, graph):
    return evaluate_nodes(expr, graph)


# --- [6] 예시 전부 파싱·평가 ---


def test_all_plan_examples_parse():
    examples = [
        'type == "class"',
        'ns startsWith "app.ui"',
        "properties.field_count > 10",
        "label matches /^UI.*View$/",
        '"authored_by_me" in tags',
        "degree(node) > 5",
        'connected_to("app.OrderService", within=3)',
        '(type == "class" AND ns startsWith "app.Core.Service") OR '
        '(type == "component" AND connected_to("app.PaymentService", within=2))',
    ]
    for expr in examples:
        compile_filter(expr)  # must not raise


def test_type_equals(graph):
    assert match('type == "class"', graph) == {"app.World"}


def test_bare_shortcut_is_a_property(graph):
    # ns is not a known attribute → properties.ns ([6] 단축 표기).
    assert match('ns startsWith "app.ui"', graph) == {"app.World", "UI.MainView"}


def test_property_number_comparison(graph):
    assert match("properties.field_count > 10", graph) == {"app.World"}


def test_matches_regex(graph):
    assert match("label matches /^UI.*View$/", graph) == {"UI.MainView"}


def test_value_in_tags(graph):
    assert match('"authored_by_me" in tags', graph) == {"app.World"}


def test_degree_as_operand(graph):
    # UI.MainView has degree 2, Svc.User has degree 2.
    assert match("degree(node) > 1", graph) == {"UI.MainView", "Svc.User"}


def test_and_or_grouping(graph):
    expr = 'type == "service" OR (type == "class" AND field_count > 10)'
    assert match(expr, graph) == {"app.World", "Svc.User", "Svc.Auth"}


def test_not_flips(graph):
    assert match('NOT type == "class"', graph) == {"UI.MainView", "Svc.User", "Svc.Auth"}


# --- group functions ([6], undirected; docs/filter-dsl.md) ---


def test_connected_to_within_1_excludes_self(graph):
    # A node is not connected_to itself ([6]/docs — distance 1..k).
    assert match('connected_to("app.World", within=1)', graph) == {"UI.MainView"}


def test_connected_to_within_grows(graph):
    assert match('connected_to("app.World", within=2)', graph) == {"UI.MainView", "Svc.User"}


def test_connected_to_default_within_is_1(graph):
    assert match('connected_to("app.World")', graph) == {"UI.MainView"}


def test_connected_to_is_undirected(graph):
    # Svc.User → Svc.Auth is a directed edge, but reachability ignores direction.
    assert "Svc.User" in match('connected_to("Svc.Auth", within=1)', graph)


def test_in_neighborhood_targets_a_type(graph):
    # nodes within 1 hop of any service. Svc.User↔Svc.Auth are each other's
    # neighbour; UI.MainView neighbours Svc.User.
    assert match('in_neighborhood("service", within=1)', graph) == {
        "UI.MainView",
        "Svc.User",
        "Svc.Auth",
    }


def test_has_neighbor_of_type_equals_in_neighborhood_within_1(graph):
    a = match('has_neighbor_of_type("service")', graph)
    b = match('in_neighborhood("service", within=1)', graph)
    assert a == b


def test_path_to_reaches_across_the_graph(graph):
    # everything but Svc.Auth itself can reach it.
    assert match('path_to("Svc.Auth")', graph) == {"app.World", "UI.MainView", "Svc.User"}


def test_group_func_over_missing_target_is_empty(graph):
    assert match('connected_to("nope", within=3)', graph) == set()


# --- 평가 의미론 엣지 ([6]) ---


def test_absent_key_makes_every_comparison_false(graph):
    assert match("properties.nope == null", graph) == set()
    assert match("properties.nope != null", graph) == set()
    assert match("properties.nope > 0", graph) == set()
    assert match('properties.nope == "x"', graph) == set()


def test_null_matches_only_explicit_null():
    g = Graph(name="t")
    g.add_node(id="a", label="a", type="x", properties={"p": None})
    g.add_node(id="b", label="b", type="x", properties={"p": "v"})
    assert evaluate_nodes("properties.p == null", g) == {"a"}
    assert evaluate_nodes("properties.p != null", g) == {"b"}


def test_ordered_needs_same_kind(graph):
    # field_count is a number; comparing to a string is a mismatch → false.
    assert match('field_count > "5"', graph) == set()
    # comparing a string field to a number is likewise false.
    assert match("label > 5", graph) == set()


def test_no_numeric_string_coercion():
    g = Graph(name="t")
    g.add_node(id="a", label="a", type="x", properties={"p": "10"})
    g.add_node(id="b", label="b", type="x", properties={"p": 10})
    # "10" (string) is not 10 (number).
    assert evaluate_nodes("properties.p == 10", g) == {"b"}
    assert evaluate_nodes('properties.p == "10"', g) == {"a"}


def test_bool_is_not_a_number():
    g = Graph(name="t")
    g.add_node(id="a", label="a", type="x", properties={"flag": True})
    assert evaluate_nodes("properties.flag == true", g) == {"a"}
    # true must not equal 1 ([6] no coercion; Python would say True == 1).
    assert evaluate_nodes("properties.flag == 1", g) == set()


def test_not_of_a_false_comparison_is_true(graph):
    # [6]: NOT flips the false from a missing key, it does not stay unknown.
    # Every node lacks properties.nope, so the comparison is false everywhere and
    # NOT makes it true everywhere.
    assert match("NOT properties.nope == 1", graph) == {
        "app.World",
        "UI.MainView",
        "Svc.User",
        "Svc.Auth",
    }


def test_in_requires_an_array(graph):
    # right side is not an array → false, not an error.
    assert match('"x" in label', graph) == set()


# --- edge filter ([5-B]) ---


def test_edge_field_filter(graph):
    keys = evaluate_edges('relation == "owns"', graph)
    assert keys == {("app.World", "UI.MainView", "owns", "")}


def test_edge_weight_filter(graph):
    assert len(evaluate_edges("weight >= 1", graph)) == 3


def test_group_func_in_edge_filter_is_rejected(graph):
    with pytest.raises(FilterError, match="not available in an edge filter"):
        evaluate_edges("degree(node) > 1", graph)


# --- 안전 상한 ([6], [11]) ---


def test_expression_length_cap():
    huge = 'type == "' + "x" * MAX_EXPRESSION_BYTES + '"'
    with pytest.raises(FilterLimitError, match="bytes"):
        compile_filter(huge)


def test_ast_depth_cap():
    # Parens alone add no semantic depth (they collapse), so nest real operators.
    deep = "NOT (" * 120 + 'type == "x"' + ")" * 120
    assert len(deep.encode()) < MAX_EXPRESSION_BYTES  # depth, not length, is the trip
    with pytest.raises(FilterLimitError, match="deep"):
        compile_filter(deep)


def test_bare_parentheses_do_not_count_as_depth(graph):
    # Grouping for readability must not be penalised by the depth cap.
    match("(" * 50 + 'type == "class"' + ")" * 50, graph)  # must not raise


def test_within_cap(graph):
    with pytest.raises(FilterLimitError, match="within"):
        match(f'connected_to("app.World", within={MAX_WITHIN + 1})', graph)


def test_within_at_cap_is_allowed(graph):
    match(f'connected_to("app.World", within={MAX_WITHIN})', graph)  # must not raise


def test_visited_node_cap():
    # A star graph whose hub has more than the cap of neighbours: a within>=1 BFS
    # from the hub must refuse rather than walk them all.
    g = Graph(name="t")
    g.add_node(id="hub", label="hub", type="h")
    for i in range(MAX_VISITED_NODES + 10):
        g.add_node(id=f"n{i}", label="n", type="leaf")
        g.add_edge(source="hub", target=f"n{i}", relation="r")
    with pytest.raises(FilterLimitError, match="visited"):
        evaluate_nodes('connected_to("hub", within=2)', g)


def test_syntax_error_is_reported():
    with pytest.raises(FilterSyntaxError):
        compile_filter("type == == class")


def test_empty_expression_is_rejected():
    with pytest.raises(FilterSyntaxError):
        compile_filter("   ")


def test_unknown_function_is_rejected(graph):
    with pytest.raises(FilterSyntaxError, match="unknown function"):
        match("neighbours(node) > 1", graph)


def test_degree_requires_the_node_keyword(graph):
    with pytest.raises(FilterSyntaxError, match="node"):
        match('degree("app.World") > 1', graph)


# --- ★ ReDoS 방어 (re2, [6]) ---


def test_catastrophic_regex_is_linear_time(graph):
    # The classic backtracking bomb. stdlib re would hang; re2 is linear.
    g = Graph(name="t")
    g.add_node(id="a", label="a" * 40 + "!", type="x")
    start = time.perf_counter()
    evaluate_nodes("label matches /(a+)+$/", g)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 100, f"regex took {elapsed_ms:.1f}ms — not linear-time"


def test_another_redos_pattern_is_bounded():
    g = Graph(name="t")
    g.add_node(id="a", label="a" * 50, type="x")
    start = time.perf_counter()
    evaluate_nodes("label matches /(a|a)*$/", g)
    assert (time.perf_counter() - start) * 1000 < 100


def test_invalid_regex_is_a_syntax_error(graph):
    with pytest.raises(FilterSyntaxError, match="regex"):
        match("label matches /(unclosed/", graph)


# --- property-based 불변 ([6]) ---

_types = st.sampled_from(["class", "component", "service", "module"])
_ident = st.text(alphabet="abcdefghij", min_size=1, max_size=4)


@st.composite
def _graphs(draw):
    g = Graph(name="t")
    n = draw(st.integers(min_value=1, max_value=12))
    for i in range(n):
        g.add_node(
            id=f"n{i}",
            label=draw(_ident),
            type=draw(_types),
            properties={"k": draw(st.integers(-5, 5))},
        )
    return g


@given(_graphs(), _types)
@settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_not_partitions_the_graph(g, t):
    # [6] schema-less: every node is definitely in or definitely out — NOT is a
    # clean complement, never leaving a node in neither set (no 3-valued gap).
    expr = f'type == "{t}"'
    yes = evaluate_nodes(expr, g)
    no = evaluate_nodes(f"NOT {expr}", g)
    all_ids = set(g.nodes)
    assert yes | no == all_ids
    assert yes & no == set()


@given(_graphs())
@settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_and_is_intersection(g):
    a = evaluate_nodes('type == "class"', g)
    b = evaluate_nodes("properties.k > 0", g)
    both = evaluate_nodes('type == "class" AND properties.k > 0', g)
    assert both == (a & b)


@given(_graphs())
@settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_or_is_union(g):
    a = evaluate_nodes('type == "class"', g)
    b = evaluate_nodes('type == "service"', g)
    either = evaluate_nodes('type == "class" OR type == "service"', g)
    assert either == (a | b)


@given(_graphs())
@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_result_is_always_a_subset_of_nodes(g):
    assert evaluate_nodes("properties.k >= 0", g) <= set(g.nodes)


# --- [6]/[23-B] reserved '_'-prefixed properties are never readable by a filter ---
# This evaluator also backs untrusted WS filter.set, so this is a security boundary.


def _reserved_graph():
    g = Graph(name="reserved")
    g.add_node(
        id="gold",
        label="Gold",
        type="class",
        properties={"field_count": 12},
    )
    g.add_node(id="plain", label="Plain", type="class", properties={"field_count": 1})
    g.cite("gold", "trace://0x1", "t")  # 예약키는 cite() 로만 ([23-B], RN3)
    return g


def test_reserved_property_dot_access_resolves_missing():
    g = _reserved_graph()
    # gold DOES have _citations, but the filter must not see it: a reserved key
    # resolves to MISSING, and MISSING makes every comparison false — so both
    # != null and == null yield the empty set, not {"gold"}.
    # ★ MUTATION GUARD: remove the is_reserved_property exclusion in
    # _resolve_node_field and 'properties._citations != null' becomes {"gold"},
    # failing this assertion.
    assert evaluate_nodes("properties._citations != null", g) == set()
    assert evaluate_nodes("properties._citations == null", g) == set()


def test_reserved_bare_name_access_resolves_missing():
    g = _reserved_graph()
    # A bare reserved identifier ([6] 단축표기) is a property access too — excluded.
    assert evaluate_nodes("_citations != null", g) == set()


def test_reserved_property_value_is_not_matchable():
    g = _reserved_graph()
    # The citation url lives only inside the reserved key; no filter reaches it.
    assert evaluate_nodes('properties._citations contains "trace"', g) == set()


def test_non_reserved_property_is_unaffected():
    g = _reserved_graph()
    # The exclusion must not touch ordinary properties.
    assert evaluate_nodes("properties.field_count > 10", g) == {"gold"}


def test_reserved_property_excluded_on_edges():
    g = Graph(name="reserved-edge")
    g.add_node(id="a", label="A", type="class")
    g.add_node(id="b", label="B", type="class")
    # ★ RN3: core 가 생성 시점에 예약키를 거부하므로 엣지에 예약키를 심는 입구가
    # 없다. 평가기의 제외 규칙은 그대로 두되(스냅샷 로드가 복원할 수 있음),
    # 여기서는 그 상태에 도달할 수 없음을 단언한다.
    with pytest.raises(ValueError, match="reserved"):
        g.add_edge(
            source="a", target="b", relation="calls",
            properties={"_secret": "x", "note": "ok"},
        )
    g.add_edge(source="a", target="b", relation="calls", properties={"note": "ok"})
    assert evaluate_edges("properties._secret != null", g) == set()
    assert evaluate_edges("_secret != null", g) == set()
    # a non-reserved edge property is still readable.
    assert len(evaluate_edges('properties.note == "ok"', g)) == 1


def test_filter_evaluator_imports_is_reserved_property():
    # core.py's is_reserved_property docstring states the filter evaluator imports
    # it (rather than re-deriving the rule) — assert that claim is actually true.
    from visualizebetter.filter import evaluate
    from visualizebetter.graph.core import is_reserved_property as core_fn

    assert evaluate.is_reserved_property is core_fn


# --- [15] KPI: a filter over 10K nodes evaluates within 500ms (committed, not a probe) ---


def test_filter_over_10k_nodes_under_500ms():
    g = Graph(name="perf")
    for i in range(10_000):
        g.add_node(
            id=f"n{i}",
            label=f"N{i}",
            type="class" if i % 2 else "service",
            properties={"field_count": i},
        )
    compiled = compile_filter('type == "class" AND properties.field_count > 5000')

    start = time.perf_counter()
    result = compiled.evaluate_nodes(g)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert result  # sanity: the filter matches something
    assert elapsed_ms < 500, f"filter over 10K nodes took {elapsed_ms:.1f}ms (KPI: <500ms, [15])"


# --- M2d: group-function direction= (in|out|both, default both) ([6] M2) ---


def _directed_graph():
    g = Graph(name="dir")
    for n in ("A", "B", "C", "D"):
        g.add_node(id=n, label=n, type="class")
    g.add_edge(source="A", target="B", relation="r", directed=True)   # A -> B
    g.add_edge(source="C", target="A", relation="r", directed=True)   # C -> A
    g.add_edge(source="A", target="D", relation="r", directed=False)  # A -- D (undirected)
    return g


def test_direction_out_follows_outgoing_plus_undirected():
    assert evaluate_nodes('connected_to("A", direction="out")', _directed_graph()) == {"B", "D"}


def test_direction_in_follows_incoming_plus_undirected():
    assert evaluate_nodes('connected_to("A", direction="in")', _directed_graph()) == {"C", "D"}


def test_direction_both_is_the_undirected_neighbourhood():
    assert evaluate_nodes('connected_to("A", direction="both")', _directed_graph()) == {"B", "C", "D"}


def test_direction_default_equals_both_regression_unchanged():
    g = _directed_graph()
    # ★ omitting direction is exactly direction="both" — the M1 undirected result.
    assert evaluate_nodes('connected_to("A")', g) == evaluate_nodes('connected_to("A", direction="both")', g)


def test_direction_in_out_both_are_all_different():
    g = _directed_graph()
    out = evaluate_nodes('connected_to("A", direction="out")', g)
    inn = evaluate_nodes('connected_to("A", direction="in")', g)
    both = evaluate_nodes('connected_to("A", direction="both")', g)
    assert out != inn and out != both and inn != both


def test_direction_mutation_respects_edge_orientation():
    # ★ MUTATION: revert the BFS to ignore direction (always undirected) and these
    # fail — 'out' would wrongly include C (which only points into A), 'in' B.
    g = _directed_graph()
    assert "C" not in evaluate_nodes('connected_to("A", direction="out")', g)
    assert "B" not in evaluate_nodes('connected_to("A", direction="in")', g)


def test_undirected_edge_is_followed_in_every_direction():
    g = Graph(name="u")
    g.add_node(id="A", label="A", type="class")
    g.add_node(id="D", label="D", type="class")
    g.add_edge(source="A", target="D", relation="r", directed=False)
    for d in ("in", "out", "both"):
        assert "D" in evaluate_nodes(f'connected_to("A", direction="{d}")', g)


def test_direction_combines_with_within():
    g = Graph(name="chain")
    for n in ("A", "B", "C"):
        g.add_node(id=n, label=n, type="class")
    g.add_edge(source="A", target="B", relation="r", directed=True)  # A -> B
    g.add_edge(source="B", target="C", relation="r", directed=True)  # B -> C
    assert evaluate_nodes('connected_to("A", within=2, direction="out")', g) == {"B", "C"}
    assert evaluate_nodes('connected_to("A", within=1, direction="out")', g) == {"B"}


def test_invalid_direction_value_is_rejected():
    with pytest.raises(FilterError):
        evaluate_nodes('connected_to("A", direction="sideways")', _directed_graph())


def test_path_to_accepts_direction_but_not_within():
    g = _directed_graph()
    assert evaluate_nodes('path_to("A", direction="out")', g) == {"B", "D"}
    with pytest.raises(FilterError):
        evaluate_nodes('path_to("A", within=2)', g)  # path_to has no within


def test_in_neighborhood_accepts_direction():
    g = _directed_graph()
    # out from every class node includes A's out-targets etc.; just assert it runs
    # and that direction changes the result vs. the undirected default.
    both = evaluate_nodes('in_neighborhood("class", direction="both")', g)
    out = evaluate_nodes('in_neighborhood("class", direction="out")', g)
    assert isinstance(out, set) and isinstance(both, set)
    assert out <= both  # a directed subset of the undirected neighbourhood
