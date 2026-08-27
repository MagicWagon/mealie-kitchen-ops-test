"""
KitchenOps Auto-Tagger v12.7 (Public Release Hybrid)
Tags Mealie recipes by cuisine, protein, cheese, tools, and categories.
Uses the Mealie API for safety, with parallel processing and a Rich UI.
"""

import os
import re
import requests
import logging
import sys
import time
import yaml
import signal
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from rich.theme import Theme

# ==========================================
# 1. UI & LOGGING SETUP
# ==========================================
custom_theme = Theme({
    "info": "cyan",
    "warning": "yellow",
    "error": "bold red",
    "success": "bold green"
})
console = Console(theme=custom_theme)

LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(level=LOG_LEVEL, format='%(message)s', handlers=[logging.NullHandler()])
logger = logging.getLogger("tagger")

os.makedirs("logs", exist_ok=True)
_fh = logging.FileHandler(f"logs/tagger_{datetime.now().strftime('%Y-%m-%d')}.log")
_fh.setFormatter(logging.Formatter('%(asctime)s | %(message)s', datefmt='%H:%M:%S'))
logger.addHandler(_fh)
logger.setLevel(logging.INFO)

# ==========================================
# 2. DEFAULT DICTIONARIES (Failsafe)
# ==========================================
DEFAULT_CHEESE = {
    "Sharp & Aged": "cheddar|parmesan|pecorino|manchego|asiago|gruyere|comte|aged gouda",
    "Soft & Creamy": "mozzarella|burrata|ricotta|brie|camembert|goat cheese|chèvre|cream cheese|mascarpone|neufchatel",
    "Blue & Funky": "gorgonzola|roquefort|stilton|blue cheese|taleggio|danablu",
    "Fresh & Curd": "paneer|chenna|khoya|feta|halloumi|cotija|queso fresco|cheese curds",
    "Melting Cheese": "provolone|fontina|monterey jack|muenster|gouda|swiss|raclette|havarti|edam|jarlsberg",
}

DEFAULT_PROTEIN = {
    "Chicken": {"regex": "chicken|chicken wing|drumstick|chicken thigh|chicken breast|poultry|cornish hen", "exclude": "broth|stock|bouillon|chickpea"},
    "Beef": {"regex": "beef|steak|hamburger|ground beef|ribeye|sirloin|brisket|chuck roast|filet mignon|short rib|flank steak|ground meat", "exclude": "broth|stock|bouillon|beef leaf"},
    "Pork": {"regex": "pork|bacon|ham hock|ham steak|sausage|pork tenderloin|chorizo|prosciutto|pancetta|guanciale|salami|pork belly|pork chop|pork loin|spare rib|pork shoulder", "exclude": "turkey|chicken|hamburger"},
    "Seafood": {"regex": "shrimp|salmon|tuna|cod fish|cod fillet|lobster|scallop|mussel|clam|fish|prawn|crab|squid|octopus|anchovy|sardine|tilapia|mahi mahi|halibut|swordfish|trout", "exclude": "sauce|stock|fish sauce"},
    "Lamb/Goat": {"regex": "lamb|mutton|goat cheese|gyro|merguez", "exclude": "goat cheese|lettuce"},
    "Game Meat": {"regex": "venison|duck|bison|rabbit|quail|goose|elk|pheasant", "exclude": "sauce|duck sauce"},
    "Egg": {"regex": r"egg|eggs|huevos", "exclude": "plant|noodle"},
    "Vegetarian Protein": {"regex": "tofu|tempeh|seitan|lentil|chickpea|black bean|kidney bean|cannellini|edamame|soy curl", "exclude": "pork|beef|chicken"},
}

DEFAULT_CUISINE = {
    "Chinese (Cantonese)": "oyster sauce|hoisin|shaoxing|char siu|lap cheong|wonton|five spice",
    "Chinese (Sichuan)": "sichuan pepper|doubanjiang|chili oil|mala|dried chili|black vinegar|facing heaven pepper",
    "Japanese": "miso|mirin|dashi|sake|nori|wasabi|furikake|panko|bonito|kombu|shoyu|katsu",
    "Korean": "gochujang|gochugaru|kimchi|doenjang|rice cake|perilla leaf|bulgogi|japchae",
    "Thai": "fish sauce|curry paste|thai basil|kaffir lime|bird's eye chili|nam pla|pad thai",
    "Vietnamese": "fish sauce|star anise|pho|rice paper|vermicelli|nuoc cham|banh mi",
    "Indonesian / Malaysian": "kecap manis|galangal|sambal|shrimp paste|kaffir lime|turmeric leaf|rendang|nasi",
    "Filipino": "calamansi|cane vinegar|banana ketchup|ube|bagoong|lumpia|adobo",
    "Indian": "garam masala|paneer|ghee|fenugreek|makhani|tandoori|kashmiri chili|amchur|curry leaf|mustard seed|asafoetida|hing|sambar|rasam|urad dal|appam|puttu",
    "Pakistani": "nihari|karahi|shan masala|chapli|haleem|biryani masala",
    "Mexican": "corn tortilla|masa|tomatillo|poblano|cotija|epazote|pepita|mole|queso fresco|guajillo|ancho chili",
    "Tex-Mex": "flour tortilla|fajita seasoning|fajita|nacho|queso|taco seasoning|refried beans",
    "Peruvian": "aji amarillo|aji panca|quinoa|ceviche|pisco|huacatay|rocoto",
    "Brazilian": "dende oil|cassava flour|farofa|cachaca|guarana|tucupi|pao de queijo",
    "US Southern": "buttermilk|collard greens|cornmeal|grits|okra|bacon grease|cajun|creole|andouille|remoulade",
    "Caribbean": "scotch bonnet|jerk seasoning|jerk|plantain|callaloo|allspice|ackee|sorrel",
    "Italian": "pecorino|parmesan|risotto|polenta|balsamic|prosciutto|gorgonzola|truffle|pancetta|nduja|focaccia|pesto",
    "French": "herbes de provence|dijon|tarragon|cognac|gruyere|creme fraiche|bouquet garni|fleur de sel",
    "Spanish": "saffron|chorizo|manchego|sherry|paella|iberico|pimenton|romesco",
    "Greek": "feta|kalamata|phyllo|halloumi|tzatziki|oregano|greek yogurt",
    "German": "sauerkraut|bratwurst|caraway|schnitzel|spaetzle|pretzel|juniper berry",
    "British / Irish": "malt vinegar|english mustard|worcestershire|stilton|golden syrup|stout|guinness|clotted cream|marmite",
    "Eastern European": "pierogi|kielbasa|sauerkraut|poppy seed|borscht|kvass",
    "Levantine (Middle Eastern)": "tahini|za'atar|sumac|bulgur|pomegranate molasses|halva|labneh|freekeh",
    "Persian (Iranian)": "rose water|barberry|dried lime|pomegranate molasses|tahdig|saffron|zereshk",
    "North African (Maghreb)": "preserved lemon|ras el hanout|tagine|harissa|merguez|chermoula",
    "East African (Ethiopian)": "berbere|niter kibbeh|injera|teff|mitmita|awaze",
    "West African": "scotch bonnet|egusi|fufu|jollof|red palm oil|suya|dawadawa",
}

DEFAULT_TEXT = {
    "Extra Spicy": ["extra spicy", "insane heat", "ghost pepper", "habanero", "thai chili", "bird's eye", "scotch bonnet", "carolina reaper", "vindaloo", "phaal"],
    "Spicy": ["spicy", "jalapeno", "hot sauce", "sriracha", "chili flakes", "serrano", "cayenne", "gochujang", "harissa", "sambal", "peri peri"],
    "Comfort Food": ["mac and cheese", "casserole", "meatloaf", "gravy", "pot pie", "stew", "grilled cheese"],
    "One Pot": ["one pot", "sheet pan", "skillet dinner", "dutch oven"],
    "Project Meal": ["sourdough", "ferment", "cure", "smoke", "braise", "confit", "mole"],
    "Vegan": ["vegan", "plant based", "plant-based"],
    "Keto": ["keto", "ketogenic", "low carb", "low-carb"],
    "Gluten Free": ["gluten free", "gluten-free", "gf"],
    "Paleo": ["paleo", "whole30"]
}

DEFAULT_TOOLS = {
    "Air Fryer": ["air fryer", "air-fryer", "airfryer"],
    "Instant Pot": ["instant pot", "pressure cooker", "multicooker"],
    "Slow Cooker": ["slow cooker", "crock pot"],
    "Dutch Oven": ["dutch oven", "le creuset"],
    "Wok": ["wok"],
    "Cast Iron": ["cast iron", "skillet"],
    "Smoker / Grill": ["smoker", "traeger", "charcoal", "grill", "big green egg"],
    "Sous Vide": ["sous vide", "immersion circulator"],
}

DEFAULT_CATEGORIES = [
    ("Beverage", ["smoothie", "shake", "latte", "lemonade", "lassi", "punch", "tea", "coffee", "cider", "cocoa", "soda", "limeade", "agua fresca", "julius", "frappe", "chai", "milkshake", "mocha", "cold brew", "cappuccino", "espresso", "macchiato", "cocktail", "mocktail", "margarita", "martini", "mojito", "sangria", "piña colada", "mimosa", "shot", "julep", "bellini", "irish cream", "drunken", "slushie", "spritzer", "fizz", "sour", "collins", "toddy", "old fashioned", "negroni", "daiquiri", "buttermilk", "sambaram"]),
    ("Condiment", ["sauce", "rub", "marinade", "pesto", "dressing", "dip", "hummus", "salsa", "jam", "jelly", "pickle", "syrup", "chutney", "relish", "vinaigrette", "glaze", "reduction", "compote", "curd", "butter", "oil", "spice mix", "seasoning", "paste", "spread", "mayonnaise", "ketchup", "mustard", "bbq sauce", "aioli", "remoulade", "sriracha", "gochujang", "harissa"]),
    ("Dessert", ["dessert", "cake", "cookie", "brownie", "fudge", "ice cream", "pudding", "pie", "tart", "sorbet", "gelato", "candy", "chocolate", "truffle", "donut", "doughnut", "shortcake", "cheesecake", "pastry", "postre", "dulce", "galleta", "helado", "paleta", "cinnabunny", "cinnamon roll", "toffee", "pop", "popsicle", "burfi", "jalebi", "sandesh", "sondesh", "panjeeri", "panjiri", "sheera", "caramel", "gummies", "apple dumpling", "crisp", "bunuelos", "tamales dulces", "gelatina", "pay de calabaza", "creamsicle", "mousse", "parfait", "scone", "biscotti", "cobbler", "buckeye", "blondie", "cupcake", "macaron", "meringue", "pavlova", "trifle", "turnover", "strudel", "ambrosia", "kheer", "halwa", "ladoo", "gulab jamun"]),
    ("Breakfast", ["pancake", "waffle", "oats", "oatmeal", "breakfast", "omelet", "scramble", "french toast", "granola", "cereal", "crepe", "hot cake", "muesli", "bagel", "benedict", "hash", "frittata", "quiche", "huevos rancheros", "shakshuka", "idli", "dosa", "vada", "uttapam", "appam", "puttu", "idi appam", "upma"]),
    ("Snack", ["snack", "bite", "energy bite", "energy ball", "pecan", "nut", "mix", "chestnut", "cottage cheese", "bistro box", "popcorn", "chips", "cracker", "dip", "chex mix", "trail mix", "granola bar", "jerky", "deviled egg", "nacho", "finger food", "appetizer", "murukku", "samosa", "pakora"]),
    ("Bread", ["bread", "loaf", "roll", "bun", "baguette", "ciabatta", "focaccia", "sourdough", "flatbread", "pita", "toast", "muffin", "pretzel", "breadstick", "mollete", "naan", "tortilla", "biscuit", "roti", "chapati", "paratha", "kulcha", "pav"]),
    ("Soup", ["soup", "stew", "chowder", "chili", "bisque", "pozole", "ramen", "pho", "stock", "broth", "gazpacho", "consumme", "minestrone", "gumbo", "bouillabaisse", "vysusuoise"]),
    ("Salad", ["salad", "slaw", "coleslaw", "caesar", "caprese", "waldorf", "wedge", "cobb", "niçoise", "tabbouleh"]),
    ("Side Dish", ["side dish", "side", "fries", "wedges", "tots", "rice", "vegetable", "veggie", "corn", "bean", "succotash", "asparagus", "broccoli", "carrot", "cauliflower", "zucchini", "mushroom", "onion", "parsnip", "plantain", "grits", "stuffing", "borlotti", "eggplant", "potato", "yam", "gnocchi", "au gratin", "mash", "puree", "pilaf", "couscous", "quinoa", "thoran", "poriyal", "dal", "sambal"]),
    ("Main Course", ["chicken", "beef", "pork", "steak", "burger", "roast", "stew", "curry", "pizza", "pasta", "lasagna", "spaghetti", "fettuccine", "risotto", "casserole", "enchilada", "burrito", "taco", "fajita", "quesadilla", "meatball", "meatloaf", "ribs", "brisket", "pulled pork", "carnitas", "chop", "tenderloin", "salmon", "tuna", "cod", "halibut", "shrimp", "lobster", "crab", "fish", "tofu", "tempeh", "seitan", "stir fry", "pad thai", "lo mein", "soup", "chili", "chowder", "bisque", "pozole", "ramen", "pho", "udon", "sandwich", "wrap", "gyro", "shawarma", "kebab", "falafel", "biryani", "paella", "jambalaya", "gumbo", "etouffee", "shepherd's pie", "pot pie", "london broil", "empanada", "tamale", "sushi", "sashimi", "poke bowl", "bibimbap", "bulgogi", "macaroni", "ziti", "alfredo", "carbonara", "bolognese", "stroganoff", "vindaloo", "korma", "tikka"])
]

# ==========================================
# 3. CONFIGURATION LOADING
# ==========================================
def load_environment():
    env_path = os.path.join(os.getcwd(), 'config.env')
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('export '):
                    line = line.replace('export ', '', 1).split('#')[0].strip()
                    if '=' in line:
                        k, v = line.split('=', 1)
                        os.environ[k] = v.strip().strip('"').strip("'")

load_environment()

MEALIE_URL = os.getenv("MEALIE_URL", "http://localhost:9000").rstrip('/')
# API Token configuration
API_TOKEN = os.getenv("MEALIE_API_TOKEN")
DRY_RUN = os.getenv("DRY_RUN", "True").lower() == "true"
MIN_CUISINE_MATCHES = 3
SHUTDOWN_REQUESTED = False
try:
    BULK_BATCH_SIZE = max(1, int(os.getenv("TAGGER_BULK_BATCH_SIZE", "500")))
except (TypeError, ValueError):
    BULK_BATCH_SIZE = 500

# Signal handler for graceful termination
def signal_handler(sig, frame):
    global SHUTDOWN_REQUESTED
    if not SHUTDOWN_REQUESTED:
        console.print("\n[error]🛑 Graceful shutdown requested. Stopping threads...[/error]")
        SHUTDOWN_REQUESTED = True
    else:
        console.print("\n[error]🛑 Force quitting...[/error]")
        sys.exit(1)

signal.signal(signal.SIGINT, signal_handler)

try:
    MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4")) 
except (ValueError, TypeError):
    MAX_WORKERS = 4

try:
    with open("config/tagging.yaml", "r") as f:
        CONFIG = yaml.safe_load(f) or {}
        console.print("[info]Loaded custom rules from config/tagging.yaml[/info]")
except FileNotFoundError:
    # Intentionally silent on FileNotFoundError so as not to clutter standard usage
    CONFIG = {}
except Exception as e:
    console.print(f"[warning]Error reading config/tagging.yaml ({e}). Falling back to built-in default tags...[/warning]")
    CONFIG = {}

CHEESE_TYPES = CONFIG.get("cheese_types", DEFAULT_CHEESE)
PROTEIN_TAGS = CONFIG.get("protein_tags", DEFAULT_PROTEIN)
CUISINE_FINGERPRINTS = CONFIG.get("cuisine_fingerprints", DEFAULT_CUISINE)
TEXT_ONLY_TAGS = CONFIG.get("text_tags", DEFAULT_TEXT)
TOOLS_MATCHES = CONFIG.get("tools_matches", DEFAULT_TOOLS)
CATEGORY_WATERFALL = CONFIG.get("categories", DEFAULT_CATEGORIES)

# ==========================================
# 4. CORE LOGIC & UTILITIES
# ==========================================
def fetch_all_summaries(headers):
    all_items = []
    page, per_page = 1, 500
    try:
        with console.status("[bold green]Fetching recipe list from Mealie...") as status:
            while True:
                try:
                    url = f"{MEALIE_URL}/api/recipes?page={page}&perPage={per_page}"
                    r = requests.get(url, headers=headers, timeout=30)
                    r.raise_for_status()
                    items = r.json().get('items', [])
                    if not items: break
                    all_items.extend(items)
                    status.update(f"[bold green]Fetched {len(all_items)} summaries...")
                    if len(items) < per_page: break
                    page += 1
                except Exception as e:
                    console.print(f"[error]Fetch error on page {page}: {e}[/error]")
                    sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[warning]🛑 Interrupted by user during fetch. Exiting cleanly...[/warning]")
        sys.exit(1)
        
    return all_items

def check_match(text: str, include_regex: str, exclude_regex: str = None) -> bool:
    include_regex = include_regex.replace(r'\y', r'\b')
    if not re.search(fr"\b({include_regex})\b", text, re.I):
        return False
    if exclude_regex:
        exclude_regex = exclude_regex.replace(r'\y', r'\b')
        if re.search(fr"\b({exclude_regex})\b", text, re.I):
            return False
    return True


def _add_case_insensitive(values: set[str], value: str) -> None:
    if value.casefold() not in {existing.casefold() for existing in values}:
        values.add(value)


ORGANIZER_RESOURCES = ("tags", "categories", "tools")


def _fetch_catalog(resource: str, headers: Dict) -> Dict[str, dict]:
    """Return all Mealie organizer entities keyed by case-insensitive name."""
    items = []
    page = 1
    per_page = 500

    while True:
        response = requests.get(
            f"{MEALIE_URL}/api/organizers/{resource}?page={page}&perPage={per_page}",
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        page_items = data.get("items", []) if isinstance(data, dict) else data
        items.extend(page_items or [])

        total_pages = data.get("total_pages") if isinstance(data, dict) else None
        if not page_items or (total_pages and page >= total_pages) or (not total_pages and len(page_items) < per_page):
            break
        page += 1

    return {
        item["name"].casefold(): item
        for item in items
        if isinstance(item, dict) and item.get("name")
    }


def fetch_relationship_catalog(headers: Dict) -> Dict[str, Dict[str, dict]]:
    """Load organizer catalogs once before the scan/write phases."""
    return {resource: _fetch_catalog(resource, headers) for resource in ORGANIZER_RESOURCES}


def _canonical_relationship(item: dict, resource: str) -> dict:
    """Keep only the fields required by Mealie relationship schemas."""
    required = ("name", "slug") if resource != "tools" else ("id", "name", "slug")
    missing = [field for field in required if not item.get(field)]
    if missing:
        raise ValueError(f"Mealie {resource} entry {item.get('name', '<unnamed>')!r} is missing {', '.join(missing)}")
    return {field: item[field] for field in ("id", "name", "slug") if field in item}


def _relationship_payload(names: Iterable[str], original: Iterable[dict], catalog: Dict[str, dict], resource: str):
    """Resolve relationship names to complete API objects, retaining existing objects as a fallback."""
    original_by_name = {
        item.get("name", "").casefold(): item
        for item in original
        if isinstance(item, dict) and item.get("name")
    }
    payload = []
    for name in sorted(names, key=str.casefold):
        item = catalog.get(name.casefold()) or original_by_name.get(name.casefold())
        if item is None:
            raise ValueError(f"Mealie {resource} catalog has no entry for {name!r}")
        payload.append(_canonical_relationship(item, resource))
    return payload


def _recipe_categories(recipe: Dict[str, Any]) -> list[dict]:
    """Mealie calls this relationship recipeCategory in both GET and PATCH schemas."""
    return recipe.get("recipeCategory", []) or []


def process_single_recipe(summary: Dict, headers: Dict, relationship_catalog=None):
    """Scan one recipe and return a write-free tagging proposal."""
    slug = summary['slug']
    if SHUTDOWN_REQUESTED:
        return {"slug": slug, "error": True, "shutdown_requested": True}

    result = {
        "id": summary.get("id"),
        "slug": slug,
        "tags_added": [],
        "categories_added": [],
        "tools_added": [],
        "desired_tags": [],
        "desired_categories": [],
        "desired_tools": [],
        "original_tags": [],
        "original_categories": [],
        "original_tools": [],
        "error": False,
    }
    
    try:
        resp = requests.get(f"{MEALIE_URL}/api/recipes/{slug}", headers=headers, timeout=15)
        resp.raise_for_status()
        recipe = resp.json()

        result["id"] = recipe.get("id") or result["id"]
        
        # Text Blobs
        ing_text = " ".join([(i.get('food') or {}).get('name', '') + " " + i.get('note', '') for i in recipe.get('recipeIngredients', [])])
        inst_text = " ".join([step.get('text', '') for step in recipe.get('recipeInstructions', [])])
        cat_text = f"{recipe.get('name', '')} {slug}"
        
        current_tags = {t['name'] for t in recipe.get('tags', []) if t.get('name')}
        original_tags = set(current_tags)

        current_cats = {c['name'] for c in _recipe_categories(recipe) if c.get('name')}
        original_cats = set(current_cats)

        current_tools = {t['name'] for t in recipe.get('tools', []) if t.get('name')}
        original_tools = set(current_tools)

        result["original_tags"] = recipe.get("tags", [])
        result["original_categories"] = _recipe_categories(recipe)
        result["original_tools"] = recipe.get("tools", [])

        # 1. Proteins
        for tag, rules in PROTEIN_TAGS.items():
            if check_match(ing_text, rules.get('regex', ''), rules.get('exclude')):
                _add_case_insensitive(current_tags, tag)

        # 2. Cheese
        for tag, regex in CHEESE_TYPES.items():
            if check_match(ing_text, regex):
                _add_case_insensitive(current_tags, tag)

        # 3. Cuisine
        for cuisine, regex in CUISINE_FINGERPRINTS.items():
            matches = len(re.findall(fr"\b({regex})\b", ing_text, re.I))
            if matches >= MIN_CUISINE_MATCHES:
                _add_case_insensitive(current_tags, cuisine)
                
        # 4. Text Tags
        for tag, keywords in TEXT_ONLY_TAGS.items():
            chain = "|".join(keywords).replace("'", "''")
            if check_match(cat_text, chain): 
                _add_case_insensitive(current_tags, tag)

        # 5. Tools
        for tool, keywords in TOOLS_MATCHES.items():
            chain = "|".join(keywords)
            if check_match(inst_text, chain):
                _add_case_insensitive(current_tools, tool)

        # 6. Categories (Waterfall)
        if not current_cats:
            for cat in CATEGORY_WATERFALL:
                cat_name = cat[0] if isinstance(cat, (list, tuple)) else list(cat.keys())[0]
                keywords = cat[1] if isinstance(cat, (list, tuple)) else list(cat.values())[0]
                
                pattern = "|".join(keywords).replace("'", "''")
                if check_match(cat_text, pattern):
                    _add_case_insensitive(current_cats, cat_name)
                    break 

        result["tags_added"] = sorted(current_tags - original_tags)
        result["categories_added"] = sorted(current_cats - original_cats)
        result["tools_added"] = sorted(current_tools - original_tools)
        result["desired_tags"] = sorted(current_tags)
        result["desired_categories"] = sorted(current_cats)

        if result["tools_added"]:
            result["desired_tools"] = sorted(current_tools)

        return result
    except Exception as e:
        logger.error(f"Error processing {slug}: {e}")
        result["error"] = True
        return result


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _matched_names(proposals: Iterable[Dict], field: str) -> set[str]:
    return {
        name
        for proposal in proposals
        if not proposal.get("error")
        for name in proposal.get(field, [])
    }


def _ensure_missing_organizers(
    resource: str,
    names: Iterable[str],
    catalog: Dict[str, dict],
    headers: Dict,
) -> tuple[Dict[str, dict], list[str]]:
    missing = sorted(
        {name for name in names if name.casefold() not in catalog},
        key=str.casefold,
    )
    for name in missing:
        response = requests.post(
            f"{MEALIE_URL}/api/organizers/{resource}",
            json={"name": name},
            headers=headers,
            timeout=30,
        )
        if response.status_code not in (200, 201, 409):
            raise RuntimeError(
                f"Unable to create Mealie {resource[:-1]} {name!r}: "
                f"HTTP {response.status_code}: {response.text[:300]}"
            )

    if missing:
        catalog = _fetch_catalog(resource, headers)
    return catalog, missing


def _post_bulk(path: str, payload: dict, headers: Dict):
    return requests.post(
        f"{MEALIE_URL}{path}",
        json=payload,
        headers=headers,
        timeout=60,
    )


def _record_bulk_success(stats: Dict, field: str, proposals: list[Dict], name: str) -> None:
    stats[field] += len(proposals)
    stats["updated_slugs"].update(proposal["slug"] for proposal in proposals)
    if field == "tags":
        stats["tag_counts"][name] += len(proposals)


def _apply_recipe_organizer_fallback(
    proposal: Dict,
    fields: set[str],
    catalog: Dict[str, Dict[str, dict]],
    headers: Dict,
) -> tuple[str, bool, str | None]:
    payload = {}
    try:
        if "tags" in fields:
            payload["tags"] = _relationship_payload(
                proposal["desired_tags"],
                proposal["original_tags"],
                catalog["tags"],
                "tags",
            )
        if "categories" in fields:
            payload["recipeCategory"] = _relationship_payload(
                proposal["desired_categories"],
                proposal["original_categories"],
                catalog["categories"],
                "categories",
            )
    except ValueError as exc:
        return proposal["slug"], False, str(exc)

    response = requests.patch(
        f"{MEALIE_URL}/api/recipes/{proposal['slug']}",
        json=payload,
        headers=headers,
        timeout=30,
    )
    if not response.ok:
        return proposal["slug"], False, f"HTTP {response.status_code}: {response.text[:300]}"
    return proposal["slug"], True, None


def apply_tag_category_updates(
    proposals: list[Dict],
    catalog: Dict[str, Dict[str, dict]],
    headers: Dict,
) -> Dict:
    """Apply tags/categories in bulk, falling back to standard APIs or recipe PATCH."""
    stats = {
        "tags": 0,
        "categories": 0,
        "tools": 0,
        "errors": 0,
        "updated_slugs": set(),
        "tag_counts": defaultdict(int),
    }
    fallback_fields: Dict[str, set[str]] = defaultdict(set)
    custom_available = True
    standard_available = True

    for field, resource, standard_path, payload_key in (
        ("tags_added", "tags", "/api/recipes/bulk-actions/tag", "tags"),
        ("categories_added", "categories", "/api/recipes/bulk-actions/categorize", "categories"),
    ):
        groups: Dict[str, list[Dict]] = defaultdict(list)
        for proposal in proposals:
            if proposal.get("error"):
                continue
            for name in proposal.get(field, []):
                groups[name.casefold()].append(proposal)

        for key, group in groups.items():
            name = next(name for name in group[0][field] if name.casefold() == key)
            item = catalog[resource].get(key)
            if item is None:
                stats["errors"] += len(group)
                logger.error(f"Unresolved Mealie {resource[:-1]} {name!r}; skipping {len(group)} recipes")
                continue
            canonical = _canonical_relationship(item, resource)

            for batch in _chunks(group, BULK_BATCH_SIZE):
                if custom_available and all(proposal.get("id") for proposal in batch):
                    response = _post_bulk(
                        "/api/recipes/bulk-actions/organize",
                        {
                            "recipes": [proposal["id"] for proposal in batch],
                            "operation": "add",
                            "tags": [canonical] if resource == "tags" else [],
                            "categories": [canonical] if resource == "categories" else [],
                        },
                        headers,
                    )
                    if response.status_code not in (404, 405):
                        if response.ok:
                            _record_bulk_success(stats, "tags" if resource == "tags" else "categories", batch, name)
                            continue
                        stats["errors"] += len(batch)
                        logger.error(
                            f"Bulk organizer update failed for {resource[:-1]} {name!r}: "
                            f"HTTP {response.status_code}: {response.text[:300]}"
                        )
                        continue
                    custom_available = False

                if standard_available:
                    response = _post_bulk(
                        standard_path,
                        {
                            "recipes": [proposal["slug"] for proposal in batch],
                            payload_key: [canonical],
                        },
                        headers,
                    )
                    if response.status_code not in (404, 405):
                        if response.ok:
                            _record_bulk_success(stats, "tags" if resource == "tags" else "categories", batch, name)
                            continue
                        stats["errors"] += len(batch)
                        logger.error(
                            f"Standard bulk update failed for {resource[:-1]} {name!r}: "
                            f"HTTP {response.status_code}: {response.text[:300]}"
                        )
                        continue
                    standard_available = False

                for proposal in batch:
                    fallback_fields[proposal["slug"]].add("tags" if resource == "tags" else "categories")

    if fallback_fields:
        by_slug = {proposal["slug"]: proposal for proposal in proposals}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(
                    _apply_recipe_organizer_fallback,
                    by_slug[slug],
                    fields,
                    catalog,
                    headers,
                ): slug
                for slug, fields in fallback_fields.items()
            }
            for future in as_completed(futures):
                slug, succeeded, error = future.result()
                proposal = by_slug[slug]
                if succeeded:
                    fields = fallback_fields[slug]
                    if "tags" in fields:
                        stats["tags"] += len(proposal.get("tags_added", []))
                        for tag in proposal.get("tags_added", []):
                            stats["tag_counts"][tag] += 1
                    if "categories" in fields:
                        stats["categories"] += len(proposal.get("categories_added", []))
                    stats["updated_slugs"].add(slug)
                else:
                    stats["errors"] += 1
                    logger.error(f"Fallback organizer update failed for {slug}: {error}")

    return stats


def _apply_tool_update(proposal: Dict, catalog: Dict[str, dict], headers: Dict):
    try:
        tools = _relationship_payload(
            proposal["desired_tools"],
            proposal["original_tools"],
            catalog,
            "tools",
        )
    except ValueError as exc:
        return proposal["slug"], False, str(exc)

    response = requests.patch(
        f"{MEALIE_URL}/api/recipes/{proposal['slug']}",
        json={"tools": tools},
        headers=headers,
        timeout=30,
    )
    if not response.ok:
        return proposal["slug"], False, f"HTTP {response.status_code}: {response.text[:300]}"
    return proposal["slug"], True, None


def apply_tool_updates(proposals: list[Dict], catalog: Dict[str, dict], headers: Dict) -> Dict:
    stats = {"tools": 0, "errors": 0, "updated_slugs": set()}
    candidates = [proposal for proposal in proposals if proposal.get("tools_added") and not proposal.get("error")]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(_apply_tool_update, proposal, catalog, headers): proposal["slug"]
            for proposal in candidates
        }
        for future in as_completed(futures):
            slug, succeeded, error = future.result()
            if succeeded:
                stats["tools"] += 1
                stats["updated_slugs"].add(slug)
            else:
                stats["errors"] += 1
                logger.error(f"Tool update failed for {slug}: {error}")
    return stats

def format_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s >= 3600: return f"{s // 3600}h {(s % 3600) // 60}m"
    if s >= 60: return f"{s // 60}m {s % 60}s"
    return f"{s}s"

# ==========================================
# 5. ORCHESTRATOR & REPORT
# ==========================================
def main():
    console.rule("[bold cyan]KitchenOps Auto-Tagger (API Edition)[/bold cyan]")
    
    if not API_TOKEN:
        console.print("[error]No API_TOKEN found in config.env! Cannot proceed.[/error]")
        sys.exit(1)

    headers = {"Authorization": f"Bearer {API_TOKEN}"}
    start_time = time.time()
    
    summaries = fetch_all_summaries(headers)
    total = len(summaries)
    if total == 0:
        console.print("[warning]No recipes found on the server.[/warning]")
        sys.exit(0)

    try:
        relationship_catalog = fetch_relationship_catalog(headers)
    except Exception as exc:
        logger.error(f"Unable to load Mealie tag/category/tool catalogs: {exc}")
        sys.exit(1)

    proposals = []
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=console
        ) as progress:
            task = progress.add_task(f"Scanning {total} recipes...", total=total)
            
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {
                    executor.submit(process_single_recipe, s, headers, relationship_catalog): s
                    for s in summaries
                }
                
                for future in as_completed(futures):
                    if SHUTDOWN_REQUESTED:
                        for f in futures:
                            f.cancel()
                        break
                        
                    res = future.result()
                    
                    if res.get("shutdown_requested"):
                        continue

                    proposals.append(res)
                    progress.update(task, advance=1, description=f"Scanned: {len(proposals)} | Total: {total}")
    except KeyboardInterrupt:
        executor.shutdown(wait=False, cancel_futures=True)
        console.print("\n[warning]🛑 Interrupted by user during tagging. Shutting down cleanly...[/warning]")
        sys.exit(1)

    planned_tag_names = _matched_names(proposals, "tags_added")
    planned_category_names = _matched_names(proposals, "categories_added")
    missing_tag_names = sorted(
        (name for name in planned_tag_names if name.casefold() not in relationship_catalog["tags"]),
        key=str.casefold,
    )
    missing_category_names = sorted(
        (name for name in planned_category_names if name.casefold() not in relationship_catalog["categories"]),
        key=str.casefold,
    )

    if DRY_RUN:
        console.print(
            f"[warning]Dry Run: would add {len(planned_tag_names)} tag types and "
            f"{len(planned_category_names)} category types.[/warning]"
        )
        if missing_tag_names or missing_category_names:
            console.print(
                f"[warning]Would create {len(missing_tag_names)} tags and "
                f"{len(missing_category_names)} categories.[/warning]"
            )
        organizer_stats = {
            "tags": sum(len(proposal.get("tags_added", [])) for proposal in proposals),
            "categories": sum(len(proposal.get("categories_added", [])) for proposal in proposals),
            "tools": sum(bool(proposal.get("tools_added")) for proposal in proposals),
            "errors": sum(bool(proposal.get("error")) for proposal in proposals),
            "updated_slugs": set(),
            "tag_counts": defaultdict(int),
        }
        for proposal in proposals:
            for tag in proposal.get("tags_added", []):
                organizer_stats["tag_counts"][tag] += 1
        tool_stats = {"tools": 0, "errors": 0, "updated_slugs": set()}
    else:
        try:
            relationship_catalog["tags"], created_tags = _ensure_missing_organizers(
                "tags", planned_tag_names, relationship_catalog["tags"], headers
            )
            relationship_catalog["categories"], created_categories = _ensure_missing_organizers(
                "categories", planned_category_names, relationship_catalog["categories"], headers
            )
            if created_tags or created_categories:
                logger.info(
                    f"Created organizers | Tags: {len(created_tags)} | Categories: {len(created_categories)}"
                )
        except Exception as exc:
            logger.error(f"Unable to create missing tag/category organizers: {exc}")
            sys.exit(1)

        organizer_stats = apply_tag_category_updates(proposals, relationship_catalog, headers)
        tool_stats = apply_tool_updates(proposals, relationship_catalog["tools"], headers)

    updated_slugs = organizer_stats["updated_slugs"] | tool_stats["updated_slugs"]
    updated_count = len(updated_slugs)
    cuisine_counts = {c: organizer_stats["tag_counts"].get(c, 0) for c in CUISINE_FINGERPRINTS.keys()}
    untagged_count = sum(
        not proposal.get("error")
        and not proposal.get("tags_added")
        and not proposal.get("categories_added")
        and not proposal.get("tools_added")
        for proposal in proposals
    )

    # Final Report Output
    console.print("\n")
    table = Table(title="Cuisine Market Share (New Additions)")
    table.add_column("Cuisine", style="cyan")
    table.add_column("Added", style="green", justify="right")
    
    for cuisine, count in sorted(cuisine_counts.items(), key=lambda x: x[1], reverse=True):
        if count > 0:
            table.add_row(cuisine, str(count))
            
    if table.row_count > 0:
        console.print(table)
        
    console.print(f"\n[bold green]Successful tag assignments:[/bold green] {organizer_stats['tags']}")
    console.print(f"[bold green]Successful category assignments:[/bold green] {organizer_stats['categories']}")
    console.print(f"[bold green]Successful tool updates:[/bold green] {tool_stats['tools']}")
    console.print(f"[bold red]Write errors:[/bold red] {organizer_stats['errors'] + tool_stats['errors']}")
    console.print(f"[bold red]Untagged Recipes Left (Approx):[/bold red] {untagged_count}")

    elapsed = time.time() - start_time
    console.print(f"\n⏱️  Elapsed: {format_elapsed(elapsed)}")
    logger.info(f"Complete | Updated: {updated_count} | Elapsed: {format_elapsed(elapsed)}")
    console.rule("[bold green]Complete[/bold green]")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[warning]🛑 Script interrupted by user. Exiting cleanly...[/warning]")
        sys.exit(1)
