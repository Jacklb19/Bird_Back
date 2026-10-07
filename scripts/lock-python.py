"""Record the installed dependency closure without importing unrelated model tooling."""
from importlib.metadata import distribution
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def lock(roots: list[str], destination: str):
    pending = [Requirement(root) for root in roots]
    extras_by_name = {}
    locked = {}
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
    Path(destination).write_text("\n".join(locked[name] for name in sorted(locked)) + "\n", encoding="utf-8")


# Vercel's requirements parser rejects -c includes, so the runtime file is the flat closure itself.
runtime = [line for line in Path("requirements.in").read_text().splitlines() if line and not line.startswith(("-", "#"))]
lock(runtime, "requirements.txt")
lock(runtime + ["pytest==9.1.1", "uvicorn==0.54.0"], "requirements-dev.lock.txt")
