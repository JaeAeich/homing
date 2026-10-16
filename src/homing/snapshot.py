"""`homing snapshot`: walk a recipe, save each page's redacted HTML."""

from pathlib import Path

from homing.engine import stages
from homing.engine.runtime import RunSpec, close_runtime, runtime_for
from homing.recipe.loader import validate_recipe_file
from homing.redact import REDACTOR

FIXTURES = Path("tests/fixtures")


async def take_snapshots(
    recipe_path: Path, inputs_path: Path, base_url: str | None
) -> list[Path]:
    """Log in if needed, run non-side-effect stages, save HTML after each."""
    recipe = validate_recipe_file(recipe_path)
    spec = RunSpec(
        run_id=f"snapshot-{recipe.portal}",
        recipe_path=str(recipe_path),
        inputs_path=str(inputs_path),
        base_url=base_url,
    )
    out_dir = FIXTURES / recipe.portal
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    try:
        session = await stages.check_session(spec)
        if not session.failed and not session.alive:
            session = await stages.login(spec, session)
        if session.failed:
            return saved
        runtime = await runtime_for(spec)
        for stage in recipe.stages:
            if stage.side_effect:
                continue
            result = await stages.run_stage(spec, stage.key)
            target = out_dir / f"{stage.key}.html"
            target.write_text(
                REDACTOR.html(await runtime.current_page().content()),
                encoding="utf-8",
            )
            saved.append(target)
            if result.failed:
                break
    finally:
        await close_runtime(spec.run_id)
    return saved
