# KitchenOps 🔪
[![GitHub Release](https://img.shields.io/github/v/release/D0rk4ce/mealie-kitchen-ops?include_prereleases&sort=semver)](https://github.com/D0rk4ce/mealie-kitchen-ops/releases)
**The Auto-Tagger, Batch Parser, and Library Cleaner Suite for Mealie**

KitchenOps is a production-ready suite of maintenance tools for [Mealie](https://mealie.io/). It automates the tagging, parsing, and sanitation of massive recipe libraries.

## 🚀 Key Features

*   **Data-Driven Architecture**: All tagging rules (cuisines, ingredients, tools) and cleaning logic are externalized in **YAML configuration files**. Customize the behavior without touching a line of code.
*   **Beautiful CLI**: Built with `rich`, featuring real-time progress bars, status spinners, and formatted reports.
*   **Production Ready**: Includes robust error handling, automated retries, and comprehensive logging.
*   **Catalog Review**: Safely approve new ingredients and units, preserve notes, map aliases to existing items, and retry blocked recipes.

## 🛠️ The Suite

| Tool | Script | Method | Complexity | Speed | Notes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **🧹 Auto-Cleaner** | `kitchen_ops_cleaner.py` | API | Low | 🚀 Fastest | Removes junk recipes, broken content, and listicles. |
| **🏷️ Auto-Tagger** | `kitchen_ops_tagger.py` | API | High | ⚡ Fast | Tags recipes by cuisine, protein, cheese, etc. |
| **🔧 Batch Parser** | `kitchen_ops_parser.py` | API | Low | 🐢 Slow | Fixes unparsed ingredients using Mealie's NLP engine. |
| **📚 Catalog Review** | `kitchen_ops_parser.py --review-catalog` | API | Low | ⚡ Fast | Reviews queued ingredients, units, notes, and aliases, then retries blocked recipes. |
- **⚡ DB Accelerator:** 
  - Massive speedup for finding unparsed recipes (~20m → <1s)
  - Instant library scanning for Cleaner (~7h → <1s)
  - **Optional:** Works with configured Postgres OR SQLite (Read-Only)
- **🛡️ Safety First:**
  - **Dry Run** by default.
- **✨ Setup Wizard:** Interactive CLI guides you through first-run configuration.
- **🔄 Smart Workflow:** "Run All" command handles the entire pipeline in one go.

> [!TIP]
> **Performance Tip:** Configuring a Database (SQLite or Postgres) triggers **Accelerator Mode**, reducing startup times from hours to seconds (1000x faster).
> *Note: SQLite acceleration uses **Read-Only** mode to ensure safety.*

---

## ⚙️ Configuration

KitchenOps is configured via environment variables (for connection details) and YAML files (for logic rules).

### 1. Environment Variables (.env)

#### 🟢 Basic Settings (Parser, Cleaner, & Common)

| Variable | Default | Description |
| :--- | :--- | :--- |
| `MEALIE_URL` | - | Your Mealie instance URL (e.g. `http://PLACEHOLDER_MEALIE_IP:9000`). |
| `MEALIE_API_TOKEN` | - | API token from Mealie → User Profile → API Tokens. |
| `DRY_RUN` | `true` | Set to `false` to apply changes. |
| `SCRIPT_TO_RUN` | `parser` | Choose `tagger`, `parser`, `catalog-review`, `cleaner`, or `all`. |
| `LOG_LEVEL` | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `PARSER_WORKERS` | `2` | Number of concurrent parsing threads. |
| `PARSER_REVIEW_FILE` | `logs/parser_pending_catalog.json` | Persistent queue for recipes waiting on ingredients, units, notes, or aliases. |
| `PARSER_HISTORY_FILE` | `/app/state/parse_history.json` | Persistent record of recipes completed by the parser. |
| `CLEANER_WORKERS` | `2` | Number of concurrent integrity-check threads. |
| `TAGGER_BULK_BATCH_SIZE` | `500` | Maximum recipes per bulk tag/category assignment request. |

#### 🔴 Database Settings (Accelerator Mode)

> **Optional.** Enables "Accelerator Mode" for Parser and Cleaner. (Supports Postgres & SQLite in Read-Only mode).

| Variable | Default | Description |
| :--- | :--- | :--- |
| `DB_TYPE` | `sqlite` | Database backend: `sqlite` or `postgres`. |
| `SQLITE_PATH` | `/app/data/mealie.db` | Path to SQLite database file. |
| `POSTGRES_HOST` | `postgres` | Postgres server hostname or IP. |
| `POSTGRES_PORT` | `5432` | Postgres server port. |
| `POSTGRES_DB` | `mealie` | Postgres database name. |
| `POSTGRES_USER` | `mealie` | Postgres username. |
| `POSTGRES_PASSWORD` | `mealie` | Postgres password. |

### 2. Logic Rules (YAML)

To customize how KitchenOps behaves, edit the files in the `config/` directory:

*   **`config/tagging.yaml`**: Define regex patterns for Proteins, Cuisines, Cheese categories, Text tags, and Tool detection.
    *   *Example:* Add "Air Fryer" detection by adding `air fryer` to the `tools_matches` list.
*   **`config/cleaning.yaml`**: Define the "blacklisted keywords" for the Library Cleaner.
    *   *Example:* Add "giveaway" or "review" to automatically flag those pages as junk.

---

## 🖥️ Portainer: Manual-Only Operation

The included `docker-compose.yml` starts KitchenOps in an idle state. Starting or redeploying the container does not contact Mealie or run any maintenance job; it only keeps the container available for a Portainer console session.

To run a job:

1. In Portainer, open the `mealie-kitchen-ops` container and select **Console**.
2. Connect using `/bin/sh`.
3. Launch exactly the job you want:

```bash
./entrypoint.sh parser
./entrypoint.sh cleaner
./entrypoint.sh tagger
./entrypoint.sh catalog-review
./entrypoint.sh all
```

The positional command takes precedence over `SCRIPT_TO_RUN`. Interactive console runs retain the dry-run and final confirmation prompts. When a job finishes, exit the console; the container's separate idle process remains running for the next manual job.

---

## 📦 Quick Start (Docker)

```bash
# 1. Create your .env file
cp .env.example .env
# Edit .env with your settings (add your API token, Mealie URL, etc.)

# 2. Pull the image
docker pull ghcr.io/d0rk4ce/mealie-kitchen-ops:latest

# 3a. Run interactively — you'll get a selection menu!
docker run -it --rm \
  --env-file .env \
  -v $(pwd)/config:/app/config \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/state:/app/state \
  ghcr.io/d0rk4ce/mealie-kitchen-ops:latest

# 3b. Or choose a specific tool directly:
docker run -it --rm \
  --env-file .env \
  -v $(pwd)/config:/app/config \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/state:/app/state \
  ghcr.io/d0rk4ce/mealie-kitchen-ops:latest parser
```

`SCRIPT_TO_RUN=parser` remains supported for existing automated deployments.

Run `--help` for a full usage guide:
```bash
docker run --rm ghcr.io/d0rk4ce/mealie-kitchen-ops:latest --help
```


### 📦 Quick Start (Podman)

Podman users (Fedora/Bazzite) often need the `:z` suffix for SELinux compatibility.

```bash
# 1. Create your .env file
cp .env.example .env
# Edit .env with your settings

# 2. Run interactively
podman run -it --rm \
  --env-file .env \
  -v $(pwd)/config:/app/config:z \
  -v $(pwd)/logs:/app/logs:z \
  -v $(pwd)/state:/app/state:z \
  ghcr.io/d0rk4ce/mealie-kitchen-ops:latest
```

## 📚 Catalog Review Workflow

The Batch Parser resolves canonical food and unit names, plurals, abbreviations, and existing aliases automatically. If Mealie's parser proposes a catalog item without an ID, KitchenOps leaves that entire recipe unchanged and records the proposal in `logs/parser_pending_catalog.json` instead of sending an invalid update.

At the start of a live parser run, KitchenOps replays queued recipes that have become fully resolvable from the current catalog without sending their ingredients through NLP again. Recipes that still need a catalog decision are skipped until catalog review. Dry runs never replay queued updates. Pressing Ctrl+C checkpoints the queue and history, skips catalog review, and exits with status 130.

Parser completion history is stored in `/app/state/parse_history.json` by default. Existing `/app/parse_history.json` history is migrated automatically and left in place. Mount `/app/state` whenever the container may be replaced.

KitchenOps also restores leaked ingredient-parser fraction placeholders such as `#3$4` back to `3/4` before displaying or saving a proposal.

On an interactive live run, KitchenOps first shows compact queue counts and asks whether to start review. Declining exits immediately without building the full review index. During review, entries are grouped in this order: Ingredients, Units, then Notes. Each entry shows the suggested fields, how many recipes use the text, up to two recipe examples, and possible existing matches. Choose from the same action numbers wherever they apply:

1. Create the suggested item.
2. Change details, then create.
3. Use an existing item and remember this name.
4. Use an existing item this time only.
5. Treat this line as an ingredient.
6. Keep this line as a note.
7. Defer until later.
0. Finish review.

KitchenOps still detects prose and equipment-related wording internally so existing queue and recipe replay behavior remains compatible, but equipment is not a review category and no equipment choices are shown. A measured line that includes extra instructions is presented as an ingredient-versus-note decision. Existing saved equipment decisions continue to be honored when queued recipes are replayed.

Choosing Note preserves the original text exactly as a note-only ingredient row. Choosing Ingredient suppresses future classification warnings and resumes normal food/unit resolution. Existing saved equipment decisions are still applied during recipe replay, but new review sessions do not create or change equipment decisions. These decisions apply only to occurrences with identical normalized original text; differently worded lines remain independent.

Create and alias actions run in submission order on a background worker, so the next review item appears immediately instead of waiting for a full Mealie catalog refresh. The status line shows how many actions are pending. Names, plurals, aliases, and unit abbreviations affected by a pending creation are temporarily reserved so they are not reviewed twice.

Completed actions update the in-memory catalog directly. Successful background actions are summarized at the end of the session so another item is never shown alongside a completion message for an unrelated item. A failed action and its related reservations return to the review list with the error visible. KitchenOps records submitted and completed decisions in `logs/parser_pending_catalog.json.journal` and checkpoints the main queue in the background, preserving decisions if the process is interrupted.

Explicit Note, Equipment, and Ingredient decisions are retained in the version-1 queue's `lineDispositions` registry. Future parser runs consult that registry before sending matching lines to Mealie's ingredient parser, preventing a decided note or equipment line from returning as a proposed food. Existing queue files are upgraded in memory with optional line-review fields and require no migration.

Choosing Finish review stops new decisions but waits for submitted catalog actions to finish and saves a final queue checkpoint before recipe updates begin.

At the end of review, KitchenOps prints a grouped summary of created ingredients and units, remembered names, one-time mappings, notes, confirmed ingredients, deferred items, automatic matches, and failures. There is no bulk terminal grid or CSV import workflow.

To resume the review later:

```bash
docker run -it --rm \
  --env-file .env \
  -e DRY_RUN=false \
  -e SCRIPT_TO_RUN=catalog-review \
  -v $(pwd)/config:/app/config \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/state:/app/state \
  ghcr.io/d0rk4ce/mealie-kitchen-ops:latest
```

After every flagged line and all required foods and units for a recipe are resolved, KitchenOps refetches the recipe, verifies that its ingredients have not changed, and applies the stored ingredients and tool relationships together. Dry-run mode may populate the review queue, but it never creates catalog items or tools, updates recipes, or marks recipes as completed.

For unattended runs, pending catalog review is an expected outcome and exits successfully after saving the queue. API failures still produce a failed run.

---

## 🗄️ Database Setup (Accelerator Mode)

> [!TIP]
> **Optional:** Database configuration is strictly optional. All tools natively use the Mealie API.

However, configuring a read-only database connection enables **Accelerator Mode** for the Parser and Cleaner, leveraging **direct SQL** to instantly scan massive libraries rather than paginating slowly through the API.
> *   **Parser/Cleaner:** Optional (Enables "Accelerator Mode"). **Works with Postgres OR SQLite**.

### 📂 SQLite

Mount your `mealie.db` file in `docker-compose.yml`. KitchenOps connects in read-only mode to fetch recipe candidates instantly. No need to stop the Mealie container!

---

### 🐘 Postgres Connection Setup

> [!TIP]
> **Where are my passwords?**
> * **Standard Install:** Check your `docker-compose.yml` or `.env` file for `POSTGRES_PASSWORD`.
> * **Community Script Install:** Look for a `mealie.creds` file:
>    * `/root/mealie.creds` (Root-level script installs)
>    * `~/mealie/mealie.creds` (Standard home directory)

Unlike SQLite, Postgres users do **not** need to stop their containers! 🚀

#### 1. Environment Configuration

Your `.env` file needs the `POSTGRES_` prefixed variables:

```ini
DB_TYPE=postgres

POSTGRES_DB=mealie
POSTGRES_USER=mealie
POSTGRES_PASSWORD=PLACEHOLDER_DB_PASSWORD
POSTGRES_HOST=PLACEHOLDER_POSTGRES_IP
POSTGRES_PORT=5432
```

#### 2. Server-Side Permissions

By default, Postgres may block external connections from your machine. Verify these two files **on the database server**:

| File | Required Setting | Purpose |
| :--- | :--- | :--- |
| `postgresql.conf` | `listen_addresses = '*'` | Listen beyond localhost |
| `pg_hba.conf` | `host all all PLACEHOLDER_SUBNET/24 md5` | Allow your local network |

After editing, restart Postgres: `sudo systemctl restart postgresql`

#### 3. Running Locally (Dev / Manual)

To run scripts directly on your machine (outside Docker), install dependencies and load environment variables:

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Load .env and run
export $(grep -v '^#' .env | xargs) && python3 kitchen_ops_tagger.py
```

---

## 🔧 Troubleshooting

| Problem | Solution |
| :--- | :--- |
| `FATAL: Group ID not found` | Database is empty or connection failed. Verify credentials and that Mealie has been used at least once. |
| `MEALIE_API_TOKEN is not set` | All tools require an API token. Generate one in Mealie → User Profile → API Tokens. |
| `connection refused` (Postgres) | Check `pg_hba.conf` and `postgresql.conf` on the DB server. Ensure the port is open in your firewall. |
| `Failed to load config/tagging.yaml` | Ensure you are running the script from the project root or that the `config/` directory is mounted correctly in Docker. |
| `Permission denied` (Podman) | SELinux is blocking access. ensure you use the `:z` suffix on your volume mounts (e.g. `-v ./data:/app/data:z`). |

---

## 📄 License
MIT License.
