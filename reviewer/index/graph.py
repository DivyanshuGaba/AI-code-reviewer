"""Resolve call sites to the symbols they probably call.

Resolution is static and name-based, so it is deliberately conservative: exact
signals first (self-methods, same-file definitions, imports), then a repo-wide name
match only when the candidate set is small. Anything that cannot be resolved stays
unresolved rather than producing a guess. Edges carry the rule that produced them
so downstream stages can weigh them.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import PurePosixPath

from reviewer.index.models import CallSite, Edge, ImportRef, Resolution, Symbol

EdgeFn = Callable[[list[Symbol], Resolution], list[Edge]]

# A bare method name shared by more symbols than this (`get`, `run`...) is noise.
MAX_AMBIGUOUS = 5
_MAX_REEXPORT_DEPTH = 3


def _module_names(path: str) -> list[str]:
    """Dotted names a file can be imported as: full path, plus suffixes of 2+ parts."""
    p = PurePosixPath(path)
    parts = list(p.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return []
    names = [".".join(parts)]
    for i in range(1, len(parts) - 1):  # drop leading dirs such as `src`, keep >=2 parts
        names.append(".".join(parts[i:]))
    return names


class _Resolver:
    def __init__(self, symbols: Iterable[Symbol], imports: Iterable[ImportRef], paths: set[str]):
        self.paths = paths
        self.by_id: dict[str, Symbol] = {}
        self.by_path_qual: dict[tuple[str, str], Symbol] = {}
        self.top_level: dict[tuple[str, str], list[Symbol]] = defaultdict(list)
        self.methods: dict[tuple[str, str, str], list[Symbol]] = defaultdict(list)
        self.by_name: dict[str, list[Symbol]] = defaultdict(list)
        for s in symbols:
            self.by_id[s.id] = s
            self.by_path_qual.setdefault((s.path, s.qualname), s)
            if s.kind == "module":
                continue
            self.by_name[s.name].append(s)
            if s.parent is None:
                self.top_level[(s.path, s.name)].append(s)
            elif s.kind == "method":
                self.methods[(s.path, s.parent, s.name)].append(s)

        self.imports: dict[str, list[ImportRef]] = defaultdict(list)
        for imp in imports:
            self.imports[imp.path].append(imp)

        self.exact_modules: dict[str, str] = {}
        self.suffix_modules: dict[str, set[str]] = defaultdict(set)
        for path in paths:
            names = _module_names(path)
            if not names:
                continue
            self.exact_modules[names[0]] = path
            for alias in names[1:]:
                self.suffix_modules[alias].add(path)

    # -- module and export lookup ------------------------------------------------

    def module_path(self, module: str, from_path: str) -> str | None:
        if module.startswith("."):
            return self._relative_module(module, from_path)
        if module in self.exact_modules:
            return self.exact_modules[module]
        candidates = self.suffix_modules.get(module, set())
        return next(iter(candidates)) if len(candidates) == 1 else None

    def _relative_module(self, module: str, from_path: str) -> str | None:
        dots = len(module) - len(module.lstrip("."))
        rest = module[dots:]
        base = PurePosixPath(from_path).parent
        for _ in range(dots - 1):
            base = base.parent
        target = base / rest.replace(".", "/") if rest else base
        for candidate in (f"{target}.py", f"{target}/__init__.py"):
            candidate = candidate.removeprefix("./")
            if candidate in self.paths:
                return candidate
        return None

    def lookup_exported(self, path: str, name: str, depth: int = 0) -> list[Symbol]:
        """Find top-level `name` in a file, following re-exports through imports."""
        found = self.top_level.get((path, name))
        if found:
            return list(found)
        if depth >= _MAX_REEXPORT_DEPTH:
            return []
        for imp in self.imports.get(path, []):
            if imp.name and imp.name != "*" and imp.bound_name == name:
                target = self.module_path(imp.module, path)
                if target and target != path:
                    hit = self.lookup_exported(target, imp.name, depth + 1)
                    if hit:
                        return hit
            elif imp.name == "*":
                target = self.module_path(imp.module, path)
                if target and target != path:
                    hit = self.lookup_exported(target, name, depth + 1)
                    if hit:
                        return hit
        return []

    # -- call resolution -----------------------------------------------------------

    def enclosing_class(self, caller: Symbol) -> Symbol | None:
        sym: Symbol | None = caller
        while sym is not None:
            if sym.kind == "class":
                return sym
            sym = self.by_path_qual.get((sym.path, sym.parent)) if sym.parent else None
        return None

    def resolve(self, call: CallSite) -> list[Edge]:
        caller = self.by_id.get(call.caller_id)
        if caller is None:
            return []

        def edges(targets: list[Symbol], resolution: Resolution) -> list[Edge]:
            return [
                Edge(caller_id=caller.id, callee_id=t.id, line=call.line, resolution=resolution)
                for t in targets
                if t.id != caller.id or resolution == "self"  # keep real recursion only
            ]

        name, qualifier = call.callee_name, call.qualifier

        if qualifier in ("self", "cls"):
            cls = self.enclosing_class(caller)
            if cls is not None:
                hit = self.methods.get((caller.path, cls.qualname, name))
                if hit:
                    return edges(hit, "self")
            return self._by_name(call, edges, methods_only=True)

        if qualifier is None:
            local = self.top_level.get((caller.path, name))
            if local:
                return edges(list(local), "local")
            bound = [i for i in self.imports.get(caller.path, []) if i.bound_name == name]
            for imp in bound:
                if imp.name is None or imp.name == "*":
                    continue
                target = self.module_path(imp.module, caller.path)
                if target:
                    hit = self.lookup_exported(target, imp.name)
                    if hit:
                        return edges(hit, "import")
            if bound:
                return []  # imported from outside the repo
            for imp in self.imports.get(caller.path, []):
                if imp.name == "*":
                    target = self.module_path(imp.module, caller.path)
                    if target:
                        hit = self.lookup_exported(target, name)
                        if hit:
                            return edges(hit, "import")
            return self._by_name(call, edges, methods_only=False)

        return self._resolve_qualified(call, caller, edges)

    def _resolve_qualified(self, call: CallSite, caller: Symbol, edges: EdgeFn) -> list[Edge]:
        name = call.callee_name
        qualifier = call.qualifier or ""
        parts = qualifier.split(".")
        base, rest = parts[0], parts[1:]

        # Class defined in this file: Foo.method()
        if not rest:
            local_cls = [
                s for s in self.top_level.get((caller.path, base), []) if s.kind == "class"
            ]
            for cls in local_cls:
                hit = self.methods.get((caller.path, cls.qualname, name))
                if hit:
                    return edges(hit, "local")

        bound = [i for i in self.imports.get(caller.path, []) if i.bound_name == base]
        for imp in bound:
            if imp.name is None:
                # `import a.b as c` -> c.x is a.b.x ; `import a.b` -> a.b.x is a.b.x
                module = ".".join([imp.module, *rest]) if imp.alias else qualifier
                target = self.module_path(module, caller.path)
                if target:
                    hit = self.lookup_exported(target, name)
                    if hit:
                        return edges(hit, "import")
            elif imp.name != "*":
                sep = "" if imp.module.endswith(".") else "."
                submodule = imp.module + sep + ".".join([imp.name, *rest])
                target = self.module_path(submodule, caller.path)
                if target:
                    hit = self.lookup_exported(target, name)
                    if hit:
                        return edges(hit, "import")
                if not rest:  # from m import Foo ; Foo.method()
                    source = self.module_path(imp.module, caller.path)
                    if source:
                        for cls in self.lookup_exported(source, imp.name):
                            if cls.kind == "class":
                                hit = self.methods.get((cls.path, cls.qualname, name))
                                if hit:
                                    return edges(hit, "import")
        if bound:
            return []  # attribute of something imported from outside the repo
        return self._by_name(call, edges, methods_only=True)

    def _by_name(self, call: CallSite, edges: EdgeFn, methods_only: bool) -> list[Edge]:
        wanted = ("method",) if methods_only else ("function", "class")
        candidates = [s for s in self.by_name.get(call.callee_name, []) if s.kind in wanted]
        if not candidates or len(candidates) > MAX_AMBIGUOUS:
            return []
        return edges(candidates, "name" if len(candidates) == 1 else "ambiguous")


def file_dependencies(imports: list[ImportRef], paths: set[str]) -> dict[str, set[str]]:
    """Which indexed files each file imports (module-level dependency graph, no self-loops)."""
    resolver = _Resolver([], imports, paths)
    deps: dict[str, set[str]] = {p: set() for p in paths}
    for imp in imports:
        if imp.path not in deps:
            continue  # the importing file is outside the requested set (e.g. a test file)
        targets: set[str] = set()
        if imp.name and imp.name != "*":
            sep = "" if imp.module.endswith(".") else "."
            sub = resolver.module_path(imp.module + sep + imp.name, imp.path)
            if sub:
                targets.add(sub)  # `from pkg import module`
        base = resolver.module_path(imp.module, imp.path) if imp.module else None
        if base and not targets:
            targets.add(base)
        deps[imp.path] |= {t for t in targets if t != imp.path}
    return deps


def resolve_edges(
    symbols: list[Symbol], imports: list[ImportRef], calls: list[CallSite]
) -> list[Edge]:
    """Resolve every call site; unresolvable calls produce no edge."""
    resolver = _Resolver(symbols, imports, {s.path for s in symbols})
    seen: set[tuple[str, str, int]] = set()
    result: list[Edge] = []
    for call in calls:
        for edge in resolver.resolve(call):
            key = (edge.caller_id, edge.callee_id, edge.line)
            if key not in seen:
                seen.add(key)
                result.append(edge)
    return result
