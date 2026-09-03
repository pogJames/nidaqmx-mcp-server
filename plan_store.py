"""Plan persistence: one JSON file per plan under `data/plans/`.

Plans are authored documents, not records — they're written once, reviewed, and edited
occasionally. Files keep them readable and editable outside this server, and put their
history in git rather than in a version table. Point NI_DATA_DIR at a tracked directory
to get exactly that.

Run records are the opposite shape (append-heavy, queried across runs, written while
being read) and will land in SQLite when the execution layer arrives. Nothing here
knows about hardware, MCP, or plan semantics.
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("NI_DATA_DIR") or Path(__file__).parent / "data")
PLANS_DIR = DATA_DIR / "plans"

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def is_slug(plan_id: str) -> bool:
    """Plan ids become filenames, so they're restricted to a lowercase slug. This is
    also what keeps a plan_id from reaching outside PLANS_DIR."""
    return bool(_SLUG.fullmatch(plan_id))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path(plan_id: str) -> Path:
    if not is_slug(plan_id):
        raise ValueError(
            f"invalid plan_id {plan_id!r}: use lowercase letters, digits, - and _")
    return PLANS_DIR / f"{plan_id}.json"


def exists(plan_id: str) -> bool:
    return _path(plan_id).exists()


def save(plan: dict) -> Path:
    """Write atomically — a torn file would be a plan that silently stops loading.
    `created_at` is preserved across edits; `updated_at` always moves."""
    path = _path(plan["plan_id"])
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    previous = load(plan["plan_id"])
    stamped = {
        **plan,
        "created_at": (previous or {}).get("created_at") or now(),
        "updated_at": now(),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(stamped, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def load(plan_id: str) -> dict | None:
    path = _path(plan_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_all() -> list[dict]:
    """Summaries of every stored plan, newest edit first. A file that fails to parse is
    reported rather than skipped — a plan you can't load is exactly what you need told
    about."""
    if not PLANS_DIR.exists():
        return []
    out = []
    for path in sorted(PLANS_DIR.glob("*.json")):
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            out.append({"plan_id": path.stem, "error": f"invalid JSON: {exc}"})
            continue
        axes = plan.get("loop", {}).get("axes", [])
        out.append({
            "plan_id": plan.get("plan_id", path.stem),
            "title": plan.get("title", ""),
            "description": plan.get("description", ""),
            "axes": [a.get("name") for a in axes],
            "step_count": len(plan.get("steps", [])),
            "created_at": plan.get("created_at"),
            "updated_at": plan.get("updated_at"),
        })
    out.sort(key=lambda p: p.get("updated_at") or "", reverse=True)
    return out


def delete(plan_id: str) -> bool:
    path = _path(plan_id)
    if not path.exists():
        return False
    path.unlink()
    return True
