# TWAIN API Module

FastAPI module for TWAIN backend with PostgreSQL database connection.

## Setup

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure Database Connection

DB connection properties are read from **AWS Secrets Manager** under the
`TWAIN/database/*` group (`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`,
`DB_PASSWORD`), managed by the Terraform in `../terraform`.

**In AWS (ECS/Fargate):** the task role is `TWAIN-secrets-reader` (see
`ecs-task-definition.json`), so the container reads + decrypts the secrets with
its default credentials — no DB values in the task definition.

**Locally:** set the reader role in `.env` so your IAM user assumes it:
```
AWS_REGION=us-east-1
TWAIN_SECRET_PREFIX=TWAIN/database
TWAIN_SECRETS_ROLE_ARN=arn:aws:iam::730335203321:role/TWAIN-secrets-reader
```

**Offline (no AWS):** set `TWAIN_DB_FROM_ENV=true` and provide the `DB_*` values
directly in `.env` instead.

To change a credential, edit `../terraform/secrets.json` and run
`terraform apply` — do not put DB credentials in this service's env or code.

### 3. Create Greetings Table (First Time Only)
```bash
psql -U postgres -h localhost -d twain_db
```

Then run this SQL:
```sql
CREATE TABLE IF NOT EXISTS greetings (
    id SERIAL PRIMARY KEY,
    message VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO greetings (message) VALUES
    ('Hello, TWAIN!'),
    ('Welcome to the API'),
    ('Greetings from FastAPI');
```

## Running the API

```bash
python main.py
```

The API will start on `http://localhost:8000`

- **API Documentation**: http://localhost:8000/docs
- **Alternative Docs**: http://localhost:8000/redoc

## Endpoints

### Health Check
```
GET /api/health
```
Returns: `{"status": "ok"}`

### Get Greetings
```
GET /api/greetings
```
Returns:
```json
{
  "data": [
    {"message": "Hello, TWAIN!"},
    {"message": "Welcome to the API"}
  ],
  "count": 2,
  "message": "Greetings retrieved successfully"
}
```

### Report an issue about a run

A researcher can report a problem from the run window without leaving TWAIN; the
run's own data is attached server-side, so a maintainer never has to ask what was
being run. See `github_issues.py` (labels + issue body) and `run_issues.py` (the
snapshot + the local record).

```
GET  /api/conversations/{id}/issue-context   # exactly what would be attached, + whether GitHub is configured
POST /api/conversations/{id}/issues          # {category, title, description} -> files the issue
GET  /api/conversations/{id}/issues          # what has already been reported for this run
```

`category` is one of `bug | library | result | other` and maps to a GitHub label
(`BugReport`, `LibraryAddition`, `ResultDiscrepancy`, `RunReport`); every issue
also carries `RunReport`. `library` deliberately reuses the tag the pipeline uses
when discovery reaches for an uninstalled library, so both kinds of request
triage as one list.

The response `status` is:

| status | meaning |
|---|---|
| `created` | the issue was filed; `issue_url` points at it |
| `queued` | no GitHub credentials in this deployment — the report is saved against the run, nothing was filed |
| `failed` | GitHub refused or was unreachable; `error` says why. The report is still saved |

Configuration (all optional — without them the endpoints work and record
locally):

| Env var | Effect |
|---|---|
| `TWAIN_GITHUB_TOKEN` | PAT used to file issues (falls back to `GITHUB_TOKEN`). Needs read+write on Issues for the repo |
| `TWAIN_GITHUB_REPO` | `owner/repo` the issues go to. Required — there is no git remote inside the container |
| `TWAIN_RUN_ISSUES=0` | force off, so a staging deployment can't post to the tracker |

To enable it on ECS, put the PAT in Secrets Manager and add it to
`ecs-task-definition.json` (`TWAIN_GITHUB_REPO` is already in `environment`):

```json
"secrets": [
  {
    "name": "TWAIN_GITHUB_TOKEN",
    "valueFrom": "arn:aws:secretsmanager:us-east-1:730335203321:secret:TWAIN/github/TWAIN_GITHUB_TOKEN"
  }
]
```

The secret must exist before deploying — ECS fails the task if a referenced
secret is missing, which is why the entry is documented here rather than
committed.

## Running Tests

```bash
pytest test_api.py -v
```

Run specific test:
```bash
pytest test_api.py::TestGreetingsEndpoint::test_greetings_endpoint_returns_200 -v
```

## Project Structure

```
api/
├── __init__.py           # Package init
├── main.py               # FastAPI app and endpoints
├── database.py           # Database connection logic
├── github_issues.py      # Filing run reports as GitHub issues (labels, body)
├── run_issues.py         # The run snapshot attached to a report + its local record
├── .env                  # Database credentials (not in git)
├── requirements.txt      # Python dependencies
├── test_api.py           # Test suite
└── README.md             # This file
```
