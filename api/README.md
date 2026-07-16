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
├── .env                  # Database credentials (not in git)
├── requirements.txt      # Python dependencies
├── test_api.py           # Test suite
└── README.md             # This file
```
