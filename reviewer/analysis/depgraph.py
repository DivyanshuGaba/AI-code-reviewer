"""Module-level dependency graph: cycles, hubs and transitive dependents. Pure functions."""

from __future__ import annotations

from collections import defaultdict, deque


def reverse(deps: dict[str, set[str]]) -> dict[str, set[str]]:
    rdeps: dict[str, set[str]] = defaultdict(set)
    for src, targets in deps.items():
        for t in targets:
            rdeps[t].add(src)
    return rdeps


def find_cycles(deps: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components with more than one module (Tarjan, iterative)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []
    counter = 0

    for root in sorted(deps):
        if root in index:
            continue
        work: list[tuple[str, list[str]]] = [(root, sorted(deps.get(root, ())))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, pending = work[-1]
            if pending:
                nxt = pending.pop(0)
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, sorted(deps.get(nxt, ()))))
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    result.append(sorted(component))
    return sorted(result)


def transitive_dependents(rdeps: dict[str, set[str]], path: str, depth: int = 3) -> set[str]:
    """Modules that (transitively, up to `depth` hops) import `path`."""
    seen: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(path, 0)])
    while queue:
        node, dist = queue.popleft()
        if dist == depth:
            continue
        for parent in rdeps.get(node, ()):
            if parent not in seen and parent != path:
                seen.add(parent)
                queue.append((parent, dist + 1))
    return seen


def hubs(rdeps: dict[str, set[str]], top: int = 5, min_dependents: int = 3) -> list[str]:
    ranked = sorted(rdeps.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return [path for path, parents in ranked[:top] if len(parents) >= min_dependents]
