"""Knowledge-graph rules and traversals (pure; operate on plain dicts/sets so they are testable without a database)."""
from __future__ import annotations

from collections import deque

RELATIONS = ["PREREQUISITE_OF", "PART_OF", "RELATED_TO", "EXPLAINS", "USES_FORMULA", "HAS_EXAMPLE",
             "HAS_QUESTION", "SUPPORTED_BY", "CONTRADICTS", "EXTENDS"]
SYMMETRIC = {"RELATED_TO", "CONTRADICTS"}
ACYCLIC = {"PREREQUISITE_OF", "PART_OF", "EXTENDS"}
NODE_KINDS = {"SUBJECT", "LESSON", "TOPIC", "CONCEPT"}
CONTENT_KINDS = {"EXPLANATION", "DEFINITION", "FORMULA", "EXAMPLE", "QUESTION", "SUMMARY"}
KINDS = NODE_KINDS | CONTENT_KINDS | {"CLAIM", "SOURCE"}
_TEACHABLE = {"LESSON", "TOPIC", "CONCEPT"}
_ANY_NON_SOURCE = KINDS - {"SOURCE"}

ALLOWED: dict[str, tuple[set[str], set[str]]] = {   # relation -> (from kinds, to kinds)
    "PREREQUISITE_OF": ({"SUBJECT", "LESSON", "TOPIC", "CONCEPT"}, {"SUBJECT", "LESSON", "TOPIC", "CONCEPT"}),
    "PART_OF": ({"LESSON", "TOPIC", "CONCEPT"} | CONTENT_KINDS, {"SUBJECT", "LESSON", "TOPIC", "CONCEPT"}),
    "RELATED_TO": (_ANY_NON_SOURCE, _ANY_NON_SOURCE),
    "EXPLAINS": ({"EXPLANATION", "DEFINITION", "SUMMARY"}, _TEACHABLE),
    "USES_FORMULA": (_TEACHABLE | {"EXAMPLE", "QUESTION"}, {"FORMULA"}),
    "HAS_EXAMPLE": (_TEACHABLE | {"FORMULA", "DEFINITION"}, {"EXAMPLE"}),
    "HAS_QUESTION": (_TEACHABLE, {"QUESTION"}),
    "SUPPORTED_BY": (_ANY_NON_SOURCE, {"SOURCE"}),
    "CONTRADICTS": ({"CLAIM"} | CONTENT_KINDS, {"CLAIM"} | CONTENT_KINDS),
    "EXTENDS": (NODE_KINDS | CONTENT_KINDS, NODE_KINDS | CONTENT_KINDS),
}
_ASSET_KIND = {"explanation": "EXPLANATION", "definition": "DEFINITION", "glossary": "DEFINITION", "formula": "FORMULA",
               "example": "EXAMPLE", "exercise": "QUESTION", "quiz": "QUESTION", "summary": "SUMMARY"}
# the relation that ties a piece of content to the lesson it belongs to
LESSON_LINK = {"EXPLANATION": "EXPLAINS", "DEFINITION": "EXPLAINS", "SUMMARY": "EXPLAINS",
               "EXAMPLE": "HAS_EXAMPLE", "QUESTION": "HAS_QUESTION", "FORMULA": "USES_FORMULA"}


class GraphRuleError(ValueError):
    pass


def asset_kind(asset_type: str) -> str | None:
    return _ASSET_KIND.get(asset_type)


def validate_edge(relation: str, from_kind: str, to_kind: str, same_entity: bool) -> None:
    if relation not in ALLOWED:
        raise GraphRuleError(f"unknown relation {relation!r}; expected one of {RELATIONS}")
    if same_entity:
        raise GraphRuleError("an element cannot be related to itself")
    src, dst = ALLOWED[relation]
    if from_kind not in src or to_kind not in dst:
        raise GraphRuleError(f"{relation} cannot connect {from_kind} -> {to_kind}; allowed sources {sorted(src)}, "
                             f"allowed targets {sorted(dst)}")


def canonical(relation: str, a: str, b: str) -> tuple[str, str]:
    """Symmetric relations are stored once, in sorted order, so (a,b) and (b,a) are the same edge."""
    return (min(a, b), max(a, b)) if relation in SYMMETRIC else (a, b)


def reachable(adj: dict[str, set[str]], start: str, max_depth: int = 10) -> dict[str, int]:
    """Breadth-first: node -> distance (start excluded). Depth-limited so large graphs are never walked fully."""
    seen, queue = {start: 0}, deque([start])
    while queue:
        cur = queue.popleft()
        if seen[cur] >= max_depth:
            continue
        for nxt in adj.get(cur, ()):
            if nxt not in seen:
                seen[nxt] = seen[cur] + 1
                queue.append(nxt)
    seen.pop(start)
    return seen


def would_create_cycle(adj: dict[str, set[str]], src: str, dst: str) -> bool:
    """Adding src->dst closes a cycle iff src is already reachable from dst."""
    return src == dst or src in reachable(adj, dst, max_depth=10_000)


def learning_path(prereq_of: dict[str, set[str]], target: str, max_depth: int = 20) -> list[str]:
    """Prerequisites of `target` ordered so each comes after everything it depends on.
    `prereq_of[a]` = nodes that are PREREQUISITE_OF a (i.e. what a needs). Cycle-safe."""
    order: list[str] = []
    state: dict[str, int] = {}

    def visit(n: str, depth: int) -> None:
        if state.get(n) or depth > max_depth:
            return
        state[n] = 1
        for p in sorted(prereq_of.get(n, ())):
            visit(p, depth + 1)
        if n != target:
            order.append(n)
        state[n] = 2

    visit(target, 0)
    return order


def missing_in_chain(path: list[str], covered: set[str]) -> list[str]:
    """Prerequisites in the learning path that have no (approved) coverage."""
    return [n for n in path if n not in covered]
