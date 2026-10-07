"""Record the installed dependency closure without importing unrelated model tooling."""
from importlib.metadata import distribution
from pathlib import Path
from typing import Final

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

PROJECT: Final = Path(__file__).resolve().parents[1]
# Direct dependencies: runtime pins, and the development pins added on top of them.
RUNTIME_ROOTS: Final = PROJECT / "requirements.in"
DEVELOPMENT_ROOTS: Final = PROJECT / "requirements-dev.txt"
# Vercel's requirements parser rejects -c includes, so the runtime file is the flat closure itself.
RUNTIME_LOCK: Final = PROJECT / "requirements.txt"
DEVELOPMENT_LOCK: Final = PROJECT / "requirements-dev.lock.txt"
# pip options (-r, -c) and comments are not requirements; the runtime closure is already covered by its own roots.
NON_REQUIREMENT_PREFIXES: Final = ("-", "#")


def read_roots(path: Path) -> list[str]:
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith(NON_REQUIREMENT_PREFIXES)]


def lock(roots: list[str], destination: Path) -> None:
    pending = [Requirement(root) for root in roots]
    extras_by_name: dict[str, set[str]] = {}
    locked: dict[str, str] = {}
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        previous = extras_by_name.get(name)
        extras = set(requirement.extras) | (previous or set())
        if previous is not None and extras == previous:
            continue
        extras_by_name[name] = extras
        installed = distribution(name)
        locked[name] = f"{installed.metadata['Name']}=={installed.version}"
        for dependency in installed.requires or []:
            child = Requirement(dependency)
            if child.marker is None or any(child.marker.evaluate({"extra": extra}) for extra in extras | {""}):
                pending.append(child)
    destination.write_text("\n".join(locked[name] for name in sorted(locked)) + "\n", encoding="utf-8")


def main() -> None:
    runtime = read_roots(RUNTIME_ROOTS)
    lock(runtime, RUNTIME_LOCK)
    lock(runtime + read_roots(DEVELOPMENT_ROOTS), DEVELOPMENT_LOCK)


if __name__ == "__main__":
    main()
