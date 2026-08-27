"""KitchenOps Batch Parser — fixes unparsed recipe ingredients via Mealie's NLP API."""

import argparse
import concurrent.futures, copy, json, logging, os, re, signal, sys, threading, time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn, TimeElapsedColumn
from rich.prompt import Confirm
from rich.theme import Theme

from kitchen_ops_catalog import (
    CatalogApi,
    CatalogIndex,
    CatalogReviewer,
    PendingCatalogQueue,
    pending_summary,
    replay_ready_recipes,
)

# Rich Console Setup
custom_theme = Theme({
    "info": "cyan",
    "warning": "yellow",
    "error": "bold red",
    "success": "bold green"
})
console = Console(theme=custom_theme)

LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(level=LOG_LEVEL, format='%(message)s', handlers=[logging.NullHandler()])
logger = logging.getLogger("parser")

# File logging
os.makedirs("logs", exist_ok=True)
_fh = logging.FileHandler(f"logs/parser_{datetime.now().strftime('%Y-%m-%d')}.log")
_fh.setFormatter(logging.Formatter('%(asctime)s | %(message)s', datefmt='%H:%M:%S'))
logger.addHandler(_fh)
logger.setLevel(logging.INFO)

MEALIE_URL: str = os.getenv("MEALIE_URL", "http://localhost:9000").rstrip("/")
API_TOKEN: str = os.getenv("MEALIE_API_TOKEN", "")
MAX_WORKERS: int = int(os.getenv("PARSER_WORKERS", "8"))
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"
CONFIDENCE_THRESHOLD: float = 0.85
HISTORY_FILE: str = "parse_history.json"
REVIEW_FILE: str = os.getenv("PARSER_REVIEW_FILE", "logs/parser_pending_catalog.json")
SAVE_INTERVAL: int = 20

# Database config (optional — enables fast startup)
DB_TYPE: str = os.getenv('DB_TYPE', '').lower().strip()
SQLITE_PATH: str = os.getenv('SQLITE_PATH', '/app/data/mealie.db')
PG_DB: str = os.getenv('POSTGRES_DB', 'mealie')
PG_USER: str = os.getenv('POSTGRES_USER', 'mealie')
PG_PASS: str = os.getenv('POSTGRES_PASSWORD', 'mealie')
PG_HOST: str = os.getenv('POSTGRES_HOST', 'postgres')
PG_PORT: str = os.getenv('POSTGRES_PORT', '5432')

CATALOG_INDEX = CatalogIndex()
HISTORY_SET: set[str] = set()
HISTORY_LOCK = threading.Lock()
thread_local = threading.local()


SHUTDOWN_REQUESTED = False

def signal_handler(sig: int, frame: Any) -> None:
    global SHUTDOWN_REQUESTED
    SHUTDOWN_REQUESTED = True
    console.print("\n[warning]Interrupt received. Stopping threads cleanly...[/warning]")
    save_history()

signal.signal(signal.SIGINT, signal_handler)


def get_session() -> requests.Session:
    if not hasattr(thread_local, "session"):
        thread_local.session = requests.Session()
        thread_local.session.headers.update({
            "Authorization": f"Bearer {API_TOKEN}",
            "Content-Type": "application/json"
        })
        retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        thread_local.session.mount("http://", HTTPAdapter(max_retries=retries))
        thread_local.session.mount("https://", HTTPAdapter(max_retries=retries))
    return thread_local.session


def load_history() -> set[str]:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, IOError) as e:
            console.print(f"[warning]Could not load history file: {e}[/warning]")
            return set()
    return set()


def save_history() -> None:
    with HISTORY_LOCK:
        try:
            with open(HISTORY_FILE, "w") as f:
                json.dump(list(HISTORY_SET), f)
        except IOError as e:
            console.print(f"[error]Could not save history: {e}[/error]")


def connect_db() -> Optional[object]:
    """Try to connect to the database (Postgres or SQLite). Returns connection or None."""
    if not DB_TYPE:
        return None

    try:
        if DB_TYPE == "postgres":
            import psycopg2
            conn = psycopg2.connect(dbname=PG_DB, user=PG_USER, password=PG_PASS, host=PG_HOST, port=PG_PORT)
            conn.autocommit = True
            return conn
            
        elif DB_TYPE == "sqlite":
            import sqlite3
            if not os.path.exists(SQLITE_PATH):
                console.print(f"[warning]SQLite DB not found at {SQLITE_PATH}[/warning]")
                console.print(f"  ❌ File not found. Check your volume mount in docker-compose.yml.")
                console.print(f"     Expected: /app/data/mealie.db (inside container)")
                return None
            
            # Diagnostic permissions check
            if not os.access(SQLITE_PATH, os.R_OK):
                console.print(f"  ❌ File is not readable. Check permissions.")
                # SELinux / Ownership Hint
                console.print(f"  💡 Hint: If you use Podman/Fedora, you may need the ':z' suffix on your volume.")
            
            # Connect in Read-Only mode if possible (URI) specific to sqlite3
            # But standard connect is fine as we only SELECT.
            conn = sqlite3.connect(f"file:{SQLITE_PATH}?mode=ro", uri=True)
            return conn
            
    except Exception as e:
        console.print(f"[warning]DB connection failed ({DB_TYPE}), falling back to API: {e}[/warning]")
    return None


def refresh_catalog(api: CatalogApi) -> None:
    """Fetch canonical foods, units, abbreviations, and aliases from Mealie."""
    with console.status(
        "[bold green]Prime Catalog: Fetching foods, units & aliases...[/bold green]",
        spinner="dots",
    ):
        api.refresh(CATALOG_INDEX)
    console.print(
        f"[info]Catalog ready: {len(CATALOG_INDEX.items['food'])} foods, "
        f"{len(CATALOG_INDEX.items['unit'])} units.[/info]"
    )


def get_recipes_needing_parsing_db(conn) -> Optional[list[dict]]:
    """
    Fetch only recipes that actually have unparsed ingredients.
    This replaces downloading 100k recipes just to filter them in Python.
    Returns list of dicts with 'slug' key, or None if failed.
    """
    try:
        cursor = conn.cursor()
        console.print("[bold green]DB Scan: Finding recipes with unparsed ingredients...[/bold green]")
        
        # We need recipes where at least one ingredient is loose text (no food_id AND no unit_id AND not a note)
        # Note checking is tricky in SQL across dialects, but generally if it has no food_id it's a candidate.
        # However, purely note ingredients effectively have no food_id too.
        # A safer bet for "unparsed" is usually just missing food_id, as standard lines get parsed into food/unit.
        
        # Tries 'recipes_ingredients' (standard per tagger code)
        query = """
            SELECT DISTINCT r.slug 
            FROM recipes r
            JOIN recipes_ingredients ri ON r.id = ri.recipe_id
            WHERE ri.food_id IS NULL
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        
        # We return a list of dicts to match what get_all_recipes returns (conceptually)
        # though process_recipe only needs the slug.
        return [{"slug": r[0]} for r in rows]
        
    except Exception as e:
        console.print(f"[warning]DB Candidate Scan failed ({e}), falling back to API...[/warning]")
        return None


def get_all_recipes() -> list[dict]:
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {API_TOKEN}"})
    recipes: list[dict] = []
    page = 1
    
    with console.status("[bold green]Fetching recipe index...[/bold green]", spinner="dots"):
        while True:
            try:
                r = session.get(f"{MEALIE_URL}/api/recipes?page={page}&perPage=2000", timeout=15)
                if r.status_code != 200:
                    break
                items = r.json().get("items", [])
                if not items:
                    break
                recipes.extend(items)
                page += 1
            except requests.RequestException as e:
                console.print(f"[warning]Index fetch failed page {page}: {e}[/warning]")
                break
    return recipes


@dataclass
class ProcessResult:
    status: str
    slug: str
    blocked_record: Optional[dict[str, Any]] = None
    error: str = ""


def _raw_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("note") or "")
    return str(item or "")


_PARSER_FRACTION_PLACEHOLDER = re.compile(r"#(\d+)\$(\d+)")


def _restore_parser_fraction_placeholders(value: Any) -> Any:
    """Restore fraction tokens that occasionally leak from ingredient-parser output."""
    if isinstance(value, str):
        return _PARSER_FRACTION_PLACEHOLDER.sub(r"\1/\2", value)
    if isinstance(value, list):
        return [_restore_parser_fraction_placeholders(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _restore_parser_fraction_placeholders(item)
            for key, item in value.items()
        }
    return value


def _resolve_catalog_reference(
    kind: str,
    target: dict[str, Any],
    ingredient_index: int,
    raw_item: Any,
    missing: list[dict[str, Any]],
) -> None:
    reference = target.get(kind)
    if not reference:
        return
    if not isinstance(reference, dict):
        missing.append(
            {
                "kind": kind,
                "name": str(reference),
                "proposal": {"name": str(reference)},
                "ingredientIndex": ingredient_index,
                "raw": _raw_text(raw_item),
                "ambiguous": False,
            }
        )
        target[kind] = {"name": str(reference)}
        return

    name = str(reference.get("name") or "").strip()
    resolved, ambiguous = CATALOG_INDEX.resolve(kind, name) if name else (None, False)
    if resolved:
        target[kind] = {"id": resolved["id"], "name": resolved["name"]}
        return
    if reference.get("id"):
        # The parser may return a valid ID that is outside this household's paginated catalog.
        return

    proposal_fields = (
        ("name", "pluralName", "description", "aliases")
        if kind == "food"
        else (
            "name",
            "pluralName",
            "description",
            "fraction",
            "abbreviation",
            "pluralAbbreviation",
            "useAbbreviation",
            "aliases",
        )
    )
    proposal = {
        key: copy.deepcopy(reference[key])
        for key in proposal_fields
        if key in reference and reference[key] is not None
    }
    proposal.setdefault("name", name)
    missing.append(
        {
            "kind": kind,
            "name": name,
            "proposal": proposal,
            "ingredientIndex": ingredient_index,
            "raw": _raw_text(raw_item),
            "ambiguous": ambiguous,
        }
    )


def process_recipe(slug: str) -> ProcessResult:
    if SHUTDOWN_REQUESTED:
        return ProcessResult("failed", slug, error="shutdown requested")
        
    session = get_session()
    try:
        r = session.get(f"{MEALIE_URL}/api/recipes/{slug}", timeout=15)
        if r.status_code != 200:
            return ProcessResult("failed", slug, error=f"recipe GET returned {r.status_code}")
        full_recipe = r.json()
    except requests.RequestException as exc:
        return ProcessResult("failed", slug, error=str(exc))

    raw_ingredients = copy.deepcopy(full_recipe.get("recipeIngredient", []))
    to_parse: list[str] = []
    to_parse_indices: list[int] = []
    clean_ingredients: list[Optional[dict]] = []

    for i, item in enumerate(raw_ingredients):
        if isinstance(item, str):
            to_parse.append(item)
            to_parse_indices.append(i)
            clean_ingredients.append(None)
        elif isinstance(item, dict) and item.get("note") and not item.get("unit") and not item.get("food"):
            to_parse.append(item["note"])
            to_parse_indices.append(i)
            clean_ingredients.append(None)
        else:
            clean_ingredients.append(item)

    if not to_parse:
        return ProcessResult("success", slug)

    # NLP pass
    try:
        r_nlp = session.post(
            f"{MEALIE_URL}/api/parser/ingredients",
            json={"ingredients": to_parse, "parser": "nlp", "language": "en"},
            timeout=30
        )
        if r_nlp.status_code != 200:
            return ProcessResult("failed", slug, error=f"NLP parser returned {r_nlp.status_code}")
        nlp_results = r_nlp.json()
        retry_sub_indices: list[int] = []
        retry_texts: list[str] = []

        for idx, text in enumerate(to_parse):
            if idx >= len(nlp_results):
                retry_sub_indices.append(to_parse_indices[idx])
                retry_texts.append(text)
                continue
            res = nlp_results[idx]
            score = res.get("confidence", {}).get("average", 0)
            actual_index = to_parse_indices[idx]
            if score < CONFIDENCE_THRESHOLD:
                retry_sub_indices.append(actual_index)
                retry_texts.append(text)
            else:
                clean_ingredients[actual_index] = res

        # AI escalation
        if retry_texts:
            try:
                r_ai = session.post(
                    f"{MEALIE_URL}/api/parser/ingredients",
                    json={"ingredients": retry_texts, "parser": "openai", "language": "en"},
                    timeout=45
                )
                if r_ai.status_code == 200:
                    for ai_idx, ai_res in enumerate(r_ai.json()):
                        if ai_idx < len(retry_sub_indices):
                            clean_ingredients[retry_sub_indices[ai_idx]] = ai_res
            except requests.RequestException as exc:
                return ProcessResult("failed", slug, error=f"OpenAI parser failed: {exc}")

    except requests.RequestException as exc:
        return ProcessResult("failed", slug, error=str(exc))

    # Reconstruct
    final_list: list[Any] = []
    missing: list[dict[str, Any]] = []
    for i, item in enumerate(clean_ingredients):
        if item is None:
            final_list.append(raw_ingredients[i])
        else:
            target = _restore_parser_fraction_placeholders(
                copy.deepcopy(item.get("ingredient", item))
            )
            for bad_key in ("referenceId", "id", "recipeId", "stepId", "labelId"):
                target.pop(bad_key, None)
            _resolve_catalog_reference("food", target, i, raw_ingredients[i], missing)
            _resolve_catalog_reference("unit", target, i, raw_ingredients[i], missing)
            final_list.append(target)

    if missing:
        for item in missing:
            logger.info(
                "MISSING %s: %r | recipe=%s index=%s raw=%r",
                item["kind"],
                item.get("name"),
                slug,
                item["ingredientIndex"],
                item.get("raw"),
            )
        return ProcessResult(
            "blocked",
            slug,
            blocked_record={
                "slug": slug,
                "sourceIngredients": raw_ingredients,
                "proposedIngredients": final_list,
                "missing": missing,
                "queuedAt": datetime.now().isoformat(timespec="seconds"),
            },
        )

    full_recipe["recipeIngredient"] = final_list

    if DRY_RUN:
        return ProcessResult("dry_run", slug)

    try:
        r_update = session.put(f"{MEALIE_URL}/api/recipes/{slug}", json=full_recipe, timeout=15)
        if 200 <= r_update.status_code < 300:
            return ProcessResult("success", slug)
        return ProcessResult("failed", slug, error=f"recipe PUT returned {r_update.status_code}: {r_update.text[:300]}")
    except requests.RequestException as exc:
        return ProcessResult("failed", slug, error=str(exc))


def format_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s >= 86400:
        return f"{s // 86400}d {(s % 86400) // 3600}h {(s % 3600) // 60}m"
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    if s >= 60:
        return f"{s // 60}m {s % 60}s"
    return f"{s}s"


def review_pending_catalog(
    api: CatalogApi,
    queue: PendingCatalogQueue,
    *,
    ask_first: bool,
) -> int:
    if not queue.recipes:
        console.print("[success]No recipes are waiting on catalog review.[/success]")
        return 0

    pending_summary(console, queue, CATALOG_INDEX)
    if DRY_RUN:
        console.print(
            "[warning]Dry Run is enabled. Catalog decisions and recipe updates are disabled.[/warning]"
        )
        console.print("Run with DRY_RUN=false and SCRIPT_TO_RUN=catalog-review to apply decisions.")
        return 0
    if not sys.stdin.isatty():
        console.print(
            "[warning]Catalog review is pending. Run interactively with "
            "SCRIPT_TO_RUN=catalog-review.[/warning]"
        )
        return 0
    if ask_first and not Confirm.ask("Review pending foods and units now?", default=True, console=console):
        console.print("Review deferred; the queue has been saved.")
        return 0

    reviewer = CatalogReviewer(queue, CATALOG_INDEX, api, console)
    action_failures = reviewer.review()
    try:
        api.refresh(CATALOG_INDEX)
        stats = replay_ready_recipes(
            queue,
            CATALOG_INDEX,
            api,
            HISTORY_SET,
            logger=lambda message: logger.info(message),
        )
    except Exception as exc:
        console.print(f"[error]Catalog replay failed: {exc}[/error]")
        return 1
    save_history()
    console.print(
        "Recipe retry: "
        f"[green]{stats['updated']} updated[/green], "
        f"[yellow]{stats['waiting']} waiting[/yellow], "
        f"[yellow]{stats['stale']} stale[/yellow], "
        f"[red]{stats['failed']} failed[/red]"
    )
    return 1 if action_failures or stats["failed"] else 0


def main() -> int:
    global HISTORY_SET
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--review-catalog",
        action="store_true",
        help="review queued foods and units, then retry affected recipes",
    )
    args = parser.parse_args()

    console.rule("[bold cyan]KitchenOps Batch Parser[/bold cyan]")
    console.print(f"Mealie: [underline]{MEALIE_URL}[/underline] | Workers: {MAX_WORKERS} | Dry Run: {DRY_RUN}")
    logger.info(f"Started | Mealie: {MEALIE_URL} | Workers: {MAX_WORKERS} | Dry Run: {DRY_RUN}")

    if not API_TOKEN:
        console.print("[error]MEALIE_API_TOKEN is not set. Cannot proceed.[/error]")
        return 1

    api = CatalogApi(MEALIE_URL, API_TOKEN, session=get_session())
    queue = PendingCatalogQueue(REVIEW_FILE).load()
    if queue.corrupt_backup:
        console.print(
            f"[warning]The review queue was corrupt and was preserved at "
            f"{queue.corrupt_backup}.[/warning]"
        )
    try:
        refresh_catalog(api)
    except Exception as exc:
        console.print(f"[error]Could not load Mealie catalog: {exc}[/error]")
        return 1
    HISTORY_SET = load_history()

    if args.review_catalog:
        return review_pending_catalog(api, queue, ask_first=False)

    start_time = time.time()
    
    # DB Acceleration Strategy
    db_conn = connect_db()
    candidates = None
    
    if db_conn:
        console.print(f"[info]DB Connection established ({DB_TYPE}). Accelerated mode active.[/info]")
        candidates = get_recipes_needing_parsing_db(db_conn)
        db_conn.close()
    
    # Fallback if DB failed or not configured
    if candidates is None:
        candidates = get_all_recipes()

    todo = [
        recipe
        for recipe in candidates
        if recipe["slug"] not in HISTORY_SET or recipe["slug"] in queue.recipes
    ]
    
    console.print(f"[info]Recipes: {len(candidates)} total, {len(HISTORY_SET)} already done, {len(todo)} remaining[/info]")
    logger.info(f"Recipes: {len(candidates)} total, {len(HISTORY_SET)} done, {len(todo)} remaining")

    if not todo:
        console.print("[success]All recipes parsed! Nothing to do.[/success]")
        if queue.recipes:
            return review_pending_catalog(api, queue, ask_first=True)
        return 0

    count = 0
    dry_run_count = 0
    blocked = 0
    failed = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        console=console
    ) as progress:
        task = progress.add_task(f"Parsing {len(todo)} recipes...", total=len(todo))
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_slug = {executor.submit(process_recipe, r["slug"]): r["slug"] for r in todo}
            for future in concurrent.futures.as_completed(future_to_slug):
                if SHUTDOWN_REQUESTED:
                    for f in future_to_slug:
                        f.cancel()
                    break
                    
                slug = future_to_slug[future]
                try:
                    result = future.result()
                    if result.status == "success":
                        queue.remove_recipe(slug)
                        if not DRY_RUN:
                            with HISTORY_LOCK:
                                HISTORY_SET.add(slug)
                        count += 1
                        logger.info(f"OK: {slug}")
                        if not DRY_RUN and count % SAVE_INTERVAL == 0:
                            save_history()
                    elif result.status == "dry_run":
                        dry_run_count += 1
                        logger.info(f"DRY RUN: {slug}")
                    elif result.status == "blocked" and result.blocked_record:
                        blocked += 1
                        with HISTORY_LOCK:
                            HISTORY_SET.discard(slug)
                        queue.upsert_recipe(result.blocked_record)
                        queue.save()
                        logger.info(f"BLOCKED: {slug} — pending catalog review")
                    else:
                        failed += 1
                        logger.info(f"FAIL: {slug} — {result.error}")
                except Exception as e:
                    failed += 1
                    logger.info(f"ERROR: {slug} — {e}")
                progress.advance(task)

    elapsed = time.time() - start_time
    queue.save()
    if not DRY_RUN:
        save_history()
    console.rule("[bold green]Batch Parse Complete[/bold green]")
    console.print(
        f"Processed: [green]{count}[/green] | Dry Run: [cyan]{dry_run_count}[/cyan] | "
        f"Catalog Review: [yellow]{blocked}[/yellow] | Failed: [red]{failed}[/red] | "
        f"Total: [cyan]{len(todo)}[/cyan]"
    )
    console.print(f"⏱️  Elapsed: {format_elapsed(elapsed)}")
    if count > 0:
        rate = count / (elapsed / 60) if elapsed > 0 else 0
        console.print(f"📊 Rate: {rate:.1f} recipes/min")
    logger.info(
        f"Complete | OK: {count} | Dry Run: {dry_run_count} | Blocked: {blocked} | "
        f"Failed: {failed} | Elapsed: {format_elapsed(elapsed)}"
    )
    review_status = 0
    if queue.recipes:
        review_status = review_pending_catalog(api, queue, ask_first=True)
    return 1 if failed or review_status else 0


if __name__ == "__main__":
    raise SystemExit(main())
