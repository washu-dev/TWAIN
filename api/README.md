# TWAIN API Module

FastAPI module for TWAIN backend with PostgreSQL database connection.

## Setup

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure Database Connection
Edit `.env` file with your PostgreSQL credentials:
```
DB_HOST=localhost
DB_PORT=5432
DB_NAME=twain_db
DB_USER=postgres
DB_PASSWORD=your_password_here
```

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
